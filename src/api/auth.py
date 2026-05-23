"""Authentication helpers for protected API routes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Optional, Set


BROWSER_AUDIENCE = "browser-ui"
SERVICE_AUDIENCE = "agent-worker-api"
ACCEPTED_AUDIENCES = frozenset({BROWSER_AUDIENCE, SERVICE_AUDIENCE})
JWT_SECRET_ENV = "AO_JWT_SECRET"
DEFAULT_JWT_SECRET = "agent-orchestrator-dev-secret"
MAX_TOKEN_AGE_SECONDS = 60 * 60
CLOCK_SKEW_SECONDS = 5

READ_SCOPES = frozenset({"agents:read", "agents:write", "agents:*", "*"})
WRITE_SCOPES = frozenset({"agents:write", "agents:*", "*"})
READ_ROLES = frozenset({"viewer", "worker", "operator", "admin", "owner"})
WRITE_ROLES = frozenset({"operator", "admin", "owner"})


class AuthenticationError(Exception):
    """Raised when bearer credentials cannot authenticate a request."""


class AuthorizationError(Exception):
    """Raised when authenticated credentials lack route permission."""


@dataclass(frozen=True)
class Principal:
    subject: str
    audience: str
    scopes: Set[str]
    workspace_id: str
    workspace_role: str
    token_id: str


_revoked_token_ids: Set[str] = set()


def revoke_token(token_id: str) -> None:
    _revoked_token_ids.add(token_id)


def clear_revoked_tokens() -> None:
    _revoked_token_ids.clear()


def parse_bearer_token(authorization: str) -> str:
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2:
        raise AuthenticationError("malformed authorization header")

    scheme, token = parts[0], parts[1].strip()
    if scheme.casefold() != "bearer":
        raise AuthenticationError("unsupported authorization scheme")
    if not token or len(token.split()) != 1:
        raise AuthenticationError("malformed bearer token")
    return token


class TokenAuthenticator:
    """Validates signed bearer tokens before protected route handling."""

    def authenticate(
        self,
        token: str,
        method: str,
        now: Optional[int] = None,
    ) -> Principal:
        payload = self._decode_and_verify(token)
        current_time = int(time.time()) if now is None else now

        token_id = _required_string(payload, "jti")
        subject = _required_string(payload, "sub")
        is_anonymous = (
            payload.get("anonymous") is True
            or subject.casefold() == "anonymous"
        )
        if is_anonymous:
            raise AuthenticationError("anonymous principal")
        if token_id in self._revoked_ids() or payload.get("revoked") is True:
            raise AuthenticationError("revoked token")

        issued_at = _required_int(payload, "iat")
        expires_at = _required_int(payload, "exp")
        not_before = _optional_int(payload, "nbf")
        if expires_at <= current_time:
            raise AuthenticationError("expired token")
        not_yet_valid = (
            not_before is not None
            and not_before > current_time + CLOCK_SKEW_SECONDS
        )
        if not_yet_valid:
            raise AuthenticationError("token not yet valid")
        if issued_at > current_time + CLOCK_SKEW_SECONDS:
            raise AuthenticationError("token issued in the future")
        if current_time - issued_at > MAX_TOKEN_AGE_SECONDS:
            raise AuthenticationError("stale token")

        audience = _accepted_audience(payload.get("aud"))
        workspace_id = _required_string(payload, "workspace_id")
        workspace_role = _required_string(payload, "workspace_role").casefold()
        scopes = _scopes(payload.get("scope", payload.get("scopes", "")))

        _authorize_role(workspace_role, method)
        _authorize_scope(scopes, method)

        return Principal(
            subject=subject,
            audience=audience,
            scopes=scopes,
            workspace_id=workspace_id,
            workspace_role=workspace_role,
            token_id=token_id,
        )

    def _decode_and_verify(self, token: str) -> dict:
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthenticationError("malformed JWT")

        header = _decode_json(parts[0])
        payload = _decode_json(parts[1])
        if header.get("alg") != "HS256":
            raise AuthenticationError("unsupported JWT algorithm")

        signing_input = f"{parts[0]}.{parts[1]}"
        expected = _signature(signing_input)
        if not hmac.compare_digest(parts[2], expected):
            raise AuthenticationError("invalid JWT signature")
        return payload

    def _revoked_ids(self) -> Set[str]:
        configured = {
            token_id.strip()
            for token_id in os.getenv("AO_REVOKED_TOKEN_IDS", "").split(",")
            if token_id.strip()
        }
        return _revoked_token_ids | configured


def _decode_json(value: str) -> dict:
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthenticationError("malformed JWT") from exc
    if not isinstance(decoded, dict):
        raise AuthenticationError("malformed JWT")
    return decoded


def _signature(signing_input: str) -> str:
    digest = hmac.new(
        _secret(),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _b64encode(digest)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _secret() -> bytes:
    return os.getenv(JWT_SECRET_ENV, DEFAULT_JWT_SECRET).encode("utf-8")


def _required_string(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AuthenticationError(f"missing {key}")
    return value.strip()


def _required_int(payload: dict, key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool):
        raise AuthenticationError(f"invalid {key}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise AuthenticationError(f"invalid {key}") from exc


def _optional_int(payload: dict, key: str) -> Optional[int]:
    if key not in payload:
        return None
    return _required_int(payload, key)


def _accepted_audience(value: Any) -> str:
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise AuthenticationError("invalid audience")
    for audience in values:
        if audience in ACCEPTED_AUDIENCES:
            return str(audience)
    raise AuthenticationError("invalid audience")


def _scopes(value: Any) -> Set[str]:
    if isinstance(value, str):
        return {scope for scope in value.split() if scope}
    if isinstance(value, Iterable):
        return {str(scope) for scope in value if str(scope)}
    return set()


def _authorize_scope(scopes: Set[str], method: str) -> None:
    required = READ_SCOPES if _is_read_method(method) else WRITE_SCOPES
    if scopes.isdisjoint(required):
        raise AuthorizationError("insufficient scope")


def _authorize_role(role: str, method: str) -> None:
    required = READ_ROLES if _is_read_method(method) else WRITE_ROLES
    if role not in required:
        raise AuthorizationError("insufficient workspace role")


def _is_read_method(method: str) -> bool:
    return method.upper() in {"GET", "HEAD", "OPTIONS"}


authenticator = TokenAuthenticator()
