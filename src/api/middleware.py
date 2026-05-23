"""API middleware components."""

import contextvars
import hashlib
import time
import logging
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Callable, Optional
from uuid import uuid4
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

ALLOWED_REQUEST_ROLES = {"anonymous", "viewer", "operator", "admin", "service"}
HEADER_SAFE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "._:-"
)
MAX_CONTEXT_HEADER_LENGTH = 128

correlation_id_var: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("correlation_id", default=None)
)
request_id_var: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("request_id", default=None)
)
tenant_id_var: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("tenant_id", default=None)
)
request_role_var: contextvars.ContextVar[str] = (
    contextvars.ContextVar("request_role", default="anonymous")
)


@dataclass(frozen=True)
class RequestContext:
    correlation_id: str
    request_id: str
    tenant_id: str
    role: str
    scope_hash: str


class CorrelationScopeRegistry:
    """Bounded in-memory binding of correlation IDs to tenant/role scopes."""

    def __init__(self, max_entries: int = 2048):
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._scopes: OrderedDict[str, str] = OrderedDict()
        self._lock = RLock()

    def bind(self, correlation_id: str, scope_hash: str) -> bool:
        with self._lock:
            current_scope = self._scopes.get(correlation_id)
            if current_scope is not None and current_scope != scope_hash:
                return False
            if current_scope is not None:
                self._scopes.move_to_end(correlation_id)
                return True
            self._scopes[correlation_id] = scope_hash
            while len(self._scopes) > self.max_entries:
                self._scopes.popitem(last=False)
            return True

    def clear(self) -> None:
        with self._lock:
            self._scopes.clear()


correlation_scope_registry = CorrelationScopeRegistry()


def get_request_context() -> Optional[RequestContext]:
    correlation_id = correlation_id_var.get()
    request_id = request_id_var.get()
    tenant_id = tenant_id_var.get()
    role = request_role_var.get()
    if not correlation_id or not request_id or not tenant_id:
        return None
    return RequestContext(
        correlation_id=correlation_id,
        request_id=request_id,
        tenant_id=tenant_id,
        role=role,
        scope_hash=_scope_hash(tenant_id, role),
    )


def get_correlation_id() -> Optional[str]:
    return correlation_id_var.get()


def get_request_id() -> Optional[str]:
    return request_id_var.get()


def get_tenant_id() -> Optional[str]:
    return tenant_id_var.get()


def get_request_role() -> str:
    return request_role_var.get()


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        context = _build_request_context(request)
        tokens = (
            correlation_id_var.set(context.correlation_id),
            request_id_var.set(context.request_id),
            tenant_id_var.set(context.tenant_id),
            request_role_var.set(context.role),
        )
        try:
            mismatch = _context_header_mismatch(request)
            if mismatch is not None:
                logger.warning(
                    "rejected request context mismatch",
                    extra=_log_context(context),
                )
                return _context_response(mismatch, context, "rejected")

            if not correlation_scope_registry.bind(
                context.correlation_id,
                context.scope_hash,
            ):
                logger.warning(
                    "rejected cross-tenant correlation id",
                    extra=_log_context(context),
                )
                return _context_response(
                    Response(
                        status_code=409,
                        content=(
                            "Correlation ID is already bound to another "
                            "context"
                        ),
                    ),
                    context,
                    "rejected",
                )

            try:
                response = await call_next(request)
            except Exception:
                logger.exception("request failed", extra=_log_context(context))
                response = Response(
                    status_code=500,
                    content="Internal server error",
                )
            decision = "failed" if response.status_code >= 500 else "accepted"
            return _context_response(response, context, decision)
        finally:
            correlation_id_var.reset(tokens[0])
            request_id_var.reset(tokens[1])
            tenant_id_var.reset(tokens[2])
            request_role_var.reset(tokens[3])


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        if (
            request.url.path.startswith("/api/v2")
            and request.url.path != "/api/v2/auth/token"
        ):
            token = request.headers.get("Authorization", "")
            if not token.startswith("Bearer "):
                return Response(status_code=401, content="Unauthorized")
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_requests: int = 100, window: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window = window
        self._requests = {}

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        if client_ip not in self._requests:
            self._requests[client_ip] = []

        self._requests[client_ip] = [
            t for t in self._requests[client_ip]
            if now - t < self.window
        ]

        if len(self._requests[client_ip]) >= self.max_requests:
            return Response(status_code=429, content="Too many requests")

        self._requests[client_ip].append(now)
        return await call_next(request)


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start
        logger.info(
            "%s %s %s %.3fs correlation_id=%s scope=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration,
            get_correlation_id() or "-",
            get_request_context().scope_hash if get_request_context() else "-",
        )
        return response


def _build_request_context(request: Request) -> RequestContext:
    tenant_id = _safe_header(
        _state_or_scope(
            request,
            "tenant_id",
            "workspace_id",
            "auth_workspace_id",
        )
        or request.headers.get("X-Tenant-ID")
        or request.headers.get("X-Workspace-ID"),
        "anonymous",
    )
    role = _safe_header(
        _state_or_scope(request, "role", "active_role", "auth_role")
        or request.headers.get("X-Role"),
        "anonymous",
    ).lower()
    correlation_id = _safe_header(
        request.headers.get("X-Correlation-ID"),
        str(uuid4()),
    )
    request_id = _safe_header(
        request.headers.get("X-Request-ID"),
        str(uuid4()),
    )
    return RequestContext(
        correlation_id=correlation_id,
        request_id=request_id,
        tenant_id=tenant_id,
        role=role,
        scope_hash=_scope_hash(tenant_id, role),
    )


def _context_header_mismatch(request: Request) -> Optional[Response]:
    tenant = request.headers.get("X-Tenant-ID")
    workspace = request.headers.get("X-Workspace-ID")
    if tenant and workspace and tenant.strip() != workspace.strip():
        return Response(status_code=403, content="Tenant context mismatch")
    state_workspace = _state_or_scope(
        request,
        "tenant_id",
        "workspace_id",
        "auth_workspace_id",
    )
    header_workspace = (
        _safe_header(tenant or workspace, "")
        if (tenant or workspace)
        else None
    )
    if (
        state_workspace
        and header_workspace
        and state_workspace != header_workspace
    ):
        return Response(status_code=403, content="Tenant context mismatch")
    state_role = _state_or_scope(request, "role", "active_role", "auth_role")
    state_role = state_role.lower() if state_role else None
    raw_header_role = request.headers.get("X-Role")
    header_role = (
        _safe_header(raw_header_role, "").lower()
        if raw_header_role
        else None
    )
    if raw_header_role and (
        not header_role or header_role not in ALLOWED_REQUEST_ROLES
    ):
        return Response(status_code=403, content="Unsupported request role")
    if state_role and header_role and state_role != header_role:
        return Response(status_code=403, content="Role context mismatch")
    return None


def _context_response(
    response: Response,
    context: RequestContext,
    decision: str,
) -> Response:
    response.headers["X-Correlation-ID"] = context.correlation_id
    response.headers["X-Request-ID"] = context.request_id
    response.headers["X-Context-Scope"] = context.scope_hash
    response.headers["X-Context-Decision"] = decision
    return response


def _safe_header(value: Optional[str], default: str) -> str:
    if value is None:
        return default
    value = value.strip()
    if not value:
        return default
    value = value[:MAX_CONTEXT_HEADER_LENGTH]
    if any(character not in HEADER_SAFE_CHARS for character in value):
        return default
    return value


def _scope_hash(tenant_id: str, role: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}:{role}".encode("utf-8")).hexdigest()
    return digest[:24]


def _state_or_scope(request: Request, *names: str) -> Optional[str]:
    for name in names:
        state_value = getattr(request.state, name, None)
        if state_value:
            return _safe_header(str(state_value), "")
        scope_value = request.scope.get(name)
        if scope_value:
            return _safe_header(str(scope_value), "")
    return None


def _log_context(context: RequestContext) -> dict:
    return {
        "correlation_id": context.correlation_id,
        "request_id": context.request_id,
        "context_scope": context.scope_hash,
    }

# 2019-03-01T18:35:19 update

# 2019-04-03T13:22:05 update

# 2019-04-30T17:18:49 update

# 2019-08-20T09:29:03 update

# 2019-08-30T15:52:06 update

# 2019-11-23T16:58:42 update

# 2020-02-18T10:04:07 update

# 2020-04-21T17:35:30 update

# 2020-05-22T11:10:34 update

# 2020-07-02T12:31:26 update

# 2020-07-05T13:52:59 update

# 2020-08-21T20:36:45 update

# 2021-01-19T09:17:15 update

# 2021-01-29T11:34:24 update

# 2021-02-04T15:21:21 update

# 2021-04-19T19:23:15 update

# 2021-05-20T16:50:15 update

# 2021-06-22T19:23:44 update

# 2021-09-09T13:44:55 update

# 2021-09-16T09:30:20 update

# 2021-10-14T20:42:33 update

# 2021-12-28T16:39:14 update

# 2022-01-26T19:07:27 update

# 2022-01-28T08:03:41 update

# 2022-03-23T12:17:02 update

# 2022-04-06T12:12:27 update

# 2022-04-21T14:53:01 update

# 2022-06-30T08:37:32 update

# 2022-07-06T10:44:45 update

# 2022-11-02T11:12:47 update

# 2022-11-15T20:54:21 update

# 2022-11-23T14:13:34 update

# 2023-01-26T10:03:44 update

# 2023-02-09T17:08:10 update

# 2023-02-16T10:04:00 update

# 2023-03-14T11:52:03 update

# 2023-04-10T12:42:07 update

# 2023-04-26T10:43:39 update

# 2023-06-27T08:18:07 update

# 2023-08-30T15:30:40 update

# 2023-08-30T14:10:05 update

# 2023-10-09T18:32:46 update

# 2023-11-21T20:35:55 update

# 2024-03-07T19:17:39 update

# 2024-04-01T18:06:19 update

# 2024-07-18T15:37:34 update

# 2024-07-25T09:21:53 update

# 2024-08-12T14:24:22 update

# 2024-11-18T08:50:54 update

# 2025-04-08T12:43:05 update

# 2025-06-03T08:10:47 update

# 2025-06-12T08:37:52 update

# 2025-06-17T08:36:56 update

# 2025-07-02T18:09:42 update

# 2025-07-22T12:39:21 update

# 2025-10-13T12:13:46 update

# 2025-12-05T09:44:22 update

# 2025-12-22T18:34:47 update

# 2026-01-26T15:36:23 update

# 2026-02-13T12:36:40 update

# 2026-02-26T11:07:15 update

# 2026-03-19T11:00:17 update

# 2026-03-27T12:58:53 update

# 2026-05-12T17:19:36 update
