"""Webhook endpoint registration and delivery state."""

from __future__ import annotations

import ipaddress
import posixpath
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urlsplit,
    urlunsplit,
)
from uuid import uuid4


class WebhookValidationError(ValueError):
    """Raised when webhook input is invalid or unsafe."""


class WebhookNotFoundError(LookupError):
    """Raised when a workspace cannot access a webhook endpoint."""


class WebhookDisabledError(RuntimeError):
    """Raised when delivery targets a disabled endpoint."""


@dataclass(frozen=True)
class WebhookEndpoint:
    endpoint_id: str
    workspace_id: str
    callback_url: str
    normalized_url: str
    secret: str
    disabled: bool
    created_at: float
    updated_at: float

    def public_dict(self, created: bool) -> Dict[str, Any]:
        return {
            "endpoint_id": self.endpoint_id,
            "workspace_id": self.workspace_id,
            "callback_url": self.callback_url,
            "disabled": self.disabled,
            "created": created,
        }


@dataclass(frozen=True)
class WebhookDelivery:
    delivery_id: str
    endpoint_id: str
    workspace_id: str
    event_id: str
    status: str
    created_at: float

    def public_dict(self, created: bool) -> Dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "endpoint_id": self.endpoint_id,
            "workspace_id": self.workspace_id,
            "event_id": self.event_id,
            "status": self.status,
            "created": created,
        }


class WebhookRegistry:
    """In-memory webhook registry with idempotent registration and delivery."""

    def __init__(self) -> None:
        self._endpoints: Dict[str, WebhookEndpoint] = {}
        self._by_workspace_url: Dict[Tuple[str, str], str] = {}
        self._deliveries: Dict[str, WebhookDelivery] = {}
        self._deliveries_by_event: Dict[Tuple[str, str], str] = {}

    def register(
        self,
        workspace_id: str,
        callback_url: str,
    ) -> Tuple[WebhookEndpoint, bool]:
        workspace_id = _require_text("workspace_id", workspace_id)
        normalized_url = normalize_webhook_url(callback_url)
        key = (workspace_id, normalized_url)
        existing_id = self._by_workspace_url.get(key)

        if existing_id is not None:
            existing = self._endpoints[existing_id]
            if not existing.disabled:
                return existing, False

        now = time.time()
        endpoint = WebhookEndpoint(
            endpoint_id=str(uuid4()),
            workspace_id=workspace_id,
            callback_url=normalized_url,
            normalized_url=normalized_url,
            secret=str(uuid4()),
            disabled=False,
            created_at=now,
            updated_at=now,
        )
        self._endpoints[endpoint.endpoint_id] = endpoint
        self._by_workspace_url[key] = endpoint.endpoint_id
        return endpoint, True

    def disable(self, workspace_id: str, endpoint_id: str) -> WebhookEndpoint:
        endpoint = self._get_workspace_endpoint(workspace_id, endpoint_id)
        disabled = WebhookEndpoint(
            endpoint_id=endpoint.endpoint_id,
            workspace_id=endpoint.workspace_id,
            callback_url=endpoint.callback_url,
            normalized_url=endpoint.normalized_url,
            secret=endpoint.secret,
            disabled=True,
            created_at=endpoint.created_at,
            updated_at=time.time(),
        )
        self._endpoints[endpoint.endpoint_id] = disabled
        return disabled

    def deliver(
        self,
        workspace_id: str,
        endpoint_id: str,
        event_id: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[WebhookDelivery, bool]:
        endpoint = self._get_workspace_endpoint(workspace_id, endpoint_id)
        if endpoint.disabled:
            raise WebhookDisabledError("webhook endpoint is disabled")

        event_id = _require_text("event_id", event_id)
        key = (endpoint.endpoint_id, event_id)
        existing_id = self._deliveries_by_event.get(key)
        if existing_id is not None:
            return self._deliveries[existing_id], False

        if payload is not None and not isinstance(payload, dict):
            raise WebhookValidationError("payload must be an object")

        delivery = WebhookDelivery(
            delivery_id=str(uuid4()),
            endpoint_id=endpoint.endpoint_id,
            workspace_id=endpoint.workspace_id,
            event_id=event_id,
            status="accepted",
            created_at=time.time(),
        )
        self._deliveries[delivery.delivery_id] = delivery
        self._deliveries_by_event[key] = delivery.delivery_id
        return delivery, True

    def endpoint_count(self) -> int:
        return len(self._endpoints)

    def delivery_count(self) -> int:
        return len(self._deliveries)

    def _get_workspace_endpoint(
        self,
        workspace_id: str,
        endpoint_id: str,
    ) -> WebhookEndpoint:
        workspace_id = _require_text("workspace_id", workspace_id)
        endpoint_id = _require_text("endpoint_id", endpoint_id)
        endpoint = self._endpoints.get(endpoint_id)
        if endpoint is None or endpoint.workspace_id != workspace_id:
            raise WebhookNotFoundError("webhook endpoint not found")
        return endpoint


def normalize_webhook_url(callback_url: str) -> str:
    """Return a canonical, safe HTTPS callback URL for duplicate checks."""
    raw_url = _require_text("callback_url", callback_url)
    parts = urlsplit(raw_url)

    if parts.scheme.lower() != "https":
        raise WebhookValidationError("callback_url must use https")
    if not parts.hostname:
        raise WebhookValidationError("callback_url must include a hostname")
    if parts.username or parts.password:
        raise WebhookValidationError(
            "callback_url must not include credentials"
        )
    host = parts.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise WebhookValidationError("callback_url host is not allowed")

    _reject_private_ip_host(host)
    host = host.encode("idna").decode("ascii")

    try:
        port = parts.port
    except ValueError as exc:
        raise WebhookValidationError(
            "callback_url has an invalid port"
        ) from exc

    netloc = _format_netloc(host, port)
    path = _normalize_path(parts.path)
    query = _normalize_query(parts.query)
    return urlunsplit(("https", netloc, path, query, ""))


def _normalize_path(path: str) -> str:
    decoded = unquote(path or "/")
    normalized = posixpath.normpath(decoded)
    if normalized in ("", "."):
        normalized = "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if normalized != "/":
        normalized = normalized.rstrip("/")
    return quote(normalized, safe="/:@!$&'()*+,;=-._~")


def _format_netloc(host: str, port: Optional[int]) -> str:
    bracketed_host = f"[{host}]" if ":" in host else host
    if port in (None, 443):
        return bracketed_host
    return f"{bracketed_host}:{port}"


def _normalize_query(query: str) -> str:
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    return urlencode(sorted(pairs), doseq=True)


def _reject_private_ip_host(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return

    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise WebhookValidationError("callback_url IP address is not allowed")


def _require_text(field_name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WebhookValidationError(
            f"{field_name} must be a non-empty string"
        )
    return value.strip()
