"""Service-to-service authentication helpers."""

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set


class AuthError(Exception):
    """Raised when a service token cannot authorize the request."""


class AuthzError(AuthError):
    """Raised when a valid service token lacks required authorization."""


@dataclass(frozen=True)
class Principal:
    subject: str
    audience: Set[str]
    scopes: Set[str]
    workspace_role: str
    token_id: Optional[str] = None


def _decode_segment(segment: str) -> Dict[str, Any]:
    try:
        padding = "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(f"{segment}{padding}".encode("ascii"))
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise AuthError("Malformed service token") from exc
    if not isinstance(value, dict):
        raise AuthError("Malformed service token")
    return value


def _normalise_audience(value: Any) -> Set[str]:
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, (list, tuple, set)):
        if not all(isinstance(item, str) for item in value):
            raise AuthError("Invalid token audience")
        return {item for item in value if item}
    raise AuthError("Invalid token audience")


def _normalise_scopes(payload: Dict[str, Any]) -> Set[str]:
    scope = payload.get("scope")
    if isinstance(scope, str):
        return {item for item in scope.split() if item}
    if scope is not None:
        raise AuthError("Invalid token scope")

    scopes = payload.get("scp")
    if isinstance(scopes, str):
        return {item for item in scopes.split() if item}
    if isinstance(scopes, (list, tuple, set)):
        if not all(isinstance(item, str) for item in scopes):
            raise AuthError("Invalid token scope")
        return {item for item in scopes if item}
    if scopes is not None:
        raise AuthError("Invalid token scope")
    return set()


def _csv_set(value: str) -> Set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


class ServiceTokenValidator:
    def __init__(
        self,
        secret: str,
        audience: str = "agent-workers",
        required_scope: str = "agent:worker",
        allowed_roles: Optional[Set[str]] = None,
        revoked_token_ids: Optional[Set[str]] = None,
        issuer: Optional[str] = None,
        now: Optional[float] = None,
    ):
        if not secret:
            raise ValueError("service JWT secret is required")
        self.secret = secret
        self.audience = audience
        self.required_scope = required_scope
        self.allowed_roles = allowed_roles or {"owner", "admin", "operator"}
        self.revoked_token_ids = revoked_token_ids or set()
        self.issuer = issuer
        self.now = now

    @classmethod
    def from_env(cls) -> "ServiceTokenValidator":
        return cls(
            secret=os.getenv("AO_SERVICE_JWT_SECRET", ""),
            audience=os.getenv("AO_SERVICE_JWT_AUDIENCE", "agent-workers"),
            required_scope=os.getenv(
                "AO_SERVICE_JWT_REQUIRED_SCOPE",
                "agent:worker",
            ),
            allowed_roles=_csv_set(
                os.getenv(
                    "AO_SERVICE_JWT_ALLOWED_ROLES",
                    "owner,admin,operator",
                )
            ),
            revoked_token_ids=_csv_set(
                os.getenv("AO_REVOKED_SERVICE_JTIS", "")
            ),
            issuer=os.getenv("AO_SERVICE_JWT_ISSUER") or None,
        )

    def validate(self, token: str) -> Principal:
        header, payload = self._decode_and_verify(token)
        if header.get("typ") not in (None, "JWT"):
            raise AuthError("Unsupported token type")

        now = self.now if self.now is not None else time.time()
        self._validate_times(payload, now)

        if self.issuer and payload.get("iss") != self.issuer:
            raise AuthError("Invalid token issuer")

        audience = _normalise_audience(payload.get("aud"))
        if self.audience not in audience:
            raise AuthzError("Insufficient token audience")

        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise AuthError("Anonymous principal")

        token_id = payload.get("jti")
        if token_id is not None and not isinstance(token_id, str):
            raise AuthError("Invalid token id")
        if isinstance(token_id, str) and token_id in self.revoked_token_ids:
            raise AuthError("Revoked token")

        scopes = _normalise_scopes(payload)
        if self.required_scope and self.required_scope not in scopes:
            raise AuthzError("Insufficient token scope")

        workspace_role = payload.get("workspace_role", payload.get("role"))
        if not isinstance(workspace_role, str):
            raise AuthError("Missing workspace role")
        if workspace_role not in self.allowed_roles:
            raise AuthzError("Insufficient workspace role")

        return Principal(
            subject=subject,
            audience=audience,
            scopes=scopes,
            workspace_role=workspace_role,
            token_id=token_id if isinstance(token_id, str) else None,
        )

    def _decode_and_verify(
        self,
        token: str,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        parts = token.split(".")
        if len(parts) != 3 or not all(parts):
            raise AuthError("Malformed service token")

        header = _decode_segment(parts[0])
        payload = _decode_segment(parts[1])
        if header.get("alg") != "HS256":
            raise AuthError("Unsupported token algorithm")

        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        expected = hmac.new(
            self.secret.encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
        padding = "=" * (-len(parts[2]) % 4)
        try:
            signature = base64.urlsafe_b64decode(
                f"{parts[2]}{padding}".encode("ascii")
            )
        except (ValueError, TypeError) as exc:
            raise AuthError("Malformed service token") from exc
        if not hmac.compare_digest(signature, expected):
            raise AuthError("Invalid token signature")
        return header, payload

    @staticmethod
    def _validate_times(payload: Dict[str, Any], now: float) -> None:
        exp = payload.get("exp")
        if (
            isinstance(exp, bool)
            or not isinstance(exp, (int, float))
            or now >= exp
        ):
            raise AuthError("Expired token")

        nbf = payload.get("nbf")
        if isinstance(nbf, bool):
            raise AuthError("Invalid not-before claim")
        if isinstance(nbf, (int, float)) and now < nbf:
            raise AuthError("Token not active yet")
        if nbf is not None and not isinstance(nbf, (int, float)):
            raise AuthError("Invalid not-before claim")

        iat = payload.get("iat")
        if isinstance(iat, bool):
            raise AuthError("Invalid issued-at claim")
        if isinstance(iat, (int, float)) and iat > now + 60:
            raise AuthError("Token issued in the future")
        if iat is not None and not isinstance(iat, (int, float)):
            raise AuthError("Invalid issued-at claim")
