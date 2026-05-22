"""Webhook subscription and delivery API."""

import hashlib
import json
import time
import uuid
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

ALLOWED_EVENT_TYPES = frozenset(
    {
        "agent.registered",
        "agent.started",
        "agent.stopped",
        "agent.failed",
        "task.created",
        "task.completed",
        "task.failed",
        "workflow.completed",
    }
)


class SubscriptionCreate(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    target_url: str = Field(..., min_length=1)
    event_types: List[str] = Field(..., min_length=1)
    secret: Optional[str] = None


class SubscriptionUpdate(BaseModel):
    target_url: Optional[str] = None
    enabled: Optional[bool] = None


class DeliveryCreate(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    subscription_id: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    payload: Dict[str, Any] = Field(default_factory=dict)
    delivery_id: Optional[str] = None


class DeliveryRetry(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    retry_id: Optional[str] = None


def _now() -> float:
    return time.time()


def _validate_target_url(target_url: str) -> None:
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_target_url",
                "message": "target_url must be an HTTP(S) URL",
            },
        )


def _normalize_event_type(event_type: str) -> str:
    return event_type.strip().lower()


def _normalize_event_types(event_types: List[str]) -> List[str]:
    normalized: List[str] = []
    for event_type in event_types:
        candidate = _normalize_event_type(event_type)
        if candidate and candidate not in normalized:
            normalized.append(candidate)
    return normalized


def _payload_hash(payload: Dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class WebhookStore:
    def __init__(self):
        self._lock = Lock()
        self._subscriptions: Dict[str, Dict[str, Any]] = {}
        self._deliveries: Dict[str, Dict[str, Any]] = {}
        self._delivery_keys: Dict[Tuple[str, str, int, str], str] = {}

    def reset(self) -> None:
        with self._lock:
            self._subscriptions.clear()
            self._deliveries.clear()
            self._delivery_keys.clear()

    def create_subscription(
        self,
        request: SubscriptionCreate,
    ) -> Dict[str, Any]:
        event_types = _normalize_event_types(request.event_types)
        invalid = [
            event_type
            for event_type in event_types
            if event_type not in ALLOWED_EVENT_TYPES
        ]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_event_types",
                    "invalid_event_types": invalid,
                    "allowed_event_types": sorted(ALLOWED_EVENT_TYPES),
                },
            )
        _validate_target_url(request.target_url)

        timestamp = _now()
        subscription_id = str(uuid.uuid4())
        subscription = {
            "id": subscription_id,
            "workspace_id": request.workspace_id,
            "target_url": request.target_url,
            "event_types": event_types,
            "enabled": True,
            "created_at": timestamp,
            "updated_at": timestamp,
            "_secret": request.secret,
            "_endpoint_version": 1,
        }
        with self._lock:
            self._subscriptions[subscription_id] = subscription
            return self._public_subscription(subscription)

    def list_subscriptions(self, workspace_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                self._public_subscription(subscription)
                for subscription in self._subscriptions.values()
                if subscription["workspace_id"] == workspace_id
            ]

    def update_subscription(
        self,
        subscription_id: str,
        workspace_id: str,
        request: SubscriptionUpdate,
    ) -> Dict[str, Any]:
        with self._lock:
            subscription = self._subscription_for_workspace(
                subscription_id,
                workspace_id,
            )
            if request.target_url is not None:
                _validate_target_url(request.target_url)
                if request.target_url != subscription["target_url"]:
                    subscription["target_url"] = request.target_url
                    subscription["_endpoint_version"] += 1
            if request.enabled is not None:
                subscription["enabled"] = request.enabled
            subscription["updated_at"] = _now()
            return self._public_subscription(subscription)

    def create_delivery(self, request: DeliveryCreate) -> Dict[str, Any]:
        event_type = _normalize_event_type(request.event_type)
        if event_type not in ALLOWED_EVENT_TYPES:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_event_type",
                    "event_type": event_type,
                    "allowed_event_types": sorted(ALLOWED_EVENT_TYPES),
                },
            )

        delivery_id = request.delivery_id or str(uuid.uuid4())
        payload_hash = _payload_hash(request.payload)
        timestamp = _now()

        with self._lock:
            subscription = self._subscription_for_workspace(
                request.subscription_id,
                request.workspace_id,
            )
            if not subscription["enabled"]:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "subscription_disabled"},
                )
            if event_type not in subscription["event_types"]:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "event_type_not_allowed_for_subscription",
                        "event_type": event_type,
                    },
                )

            delivery_key = (
                request.workspace_id,
                request.subscription_id,
                subscription["_endpoint_version"],
                delivery_id,
            )
            existing_id = self._delivery_keys.get(delivery_key)
            fingerprint = self._delivery_fingerprint(
                request.subscription_id,
                event_type,
                payload_hash,
            )
            if existing_id:
                existing = self._deliveries[existing_id]
                if existing["_fingerprint"] != fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail={"error": "delivery_id_conflict"},
                    )
                return self._public_delivery(existing)

            record_id = str(uuid.uuid4())
            delivery = {
                "id": record_id,
                "delivery_id": delivery_id,
                "subscription_id": request.subscription_id,
                "workspace_id": request.workspace_id,
                "event_type": event_type,
                "status": "delivered",
                "attempts": 1,
                "created_at": timestamp,
                "updated_at": timestamp,
                "_callback_url": subscription["target_url"],
                "_endpoint_version": subscription["_endpoint_version"],
                "_payload_hash": payload_hash,
                "_fingerprint": fingerprint,
                "_retry_keys": set(),
            }
            self._deliveries[record_id] = delivery
            self._delivery_keys[delivery_key] = record_id
            return self._public_delivery(delivery)

    def list_deliveries(self, workspace_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                self._public_delivery(delivery)
                for delivery in self._deliveries.values()
                if delivery["workspace_id"] == workspace_id
            ]

    def get_delivery(
        self,
        delivery_record_id: str,
        workspace_id: str,
    ) -> Dict[str, Any]:
        with self._lock:
            delivery = self._delivery_for_workspace(
                delivery_record_id,
                workspace_id,
            )
            return self._public_delivery(delivery)

    def retry_delivery(
        self,
        delivery_record_id: str,
        request: DeliveryRetry,
    ) -> Dict[str, Any]:
        retry_id = request.retry_id or f"default:{delivery_record_id}"
        with self._lock:
            delivery = self._delivery_for_workspace(
                delivery_record_id,
                request.workspace_id,
            )
            subscription = self._subscription_for_workspace(
                delivery["subscription_id"],
                request.workspace_id,
            )
            if not subscription["enabled"]:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "subscription_disabled"},
                )
            if (
                subscription["_endpoint_version"]
                != delivery["_endpoint_version"]
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"error": "endpoint_rotated"},
                )
            if delivery["event_type"] not in subscription["event_types"]:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "event_type_not_allowed_for_subscription",
                    },
                )
            if retry_id not in delivery["_retry_keys"]:
                delivery["_retry_keys"].add(retry_id)
                delivery["attempts"] += 1
                delivery["updated_at"] = _now()
            return self._public_delivery(delivery)

    def _subscription_for_workspace(
        self,
        subscription_id: str,
        workspace_id: str,
    ) -> Dict[str, Any]:
        subscription = self._subscriptions.get(subscription_id)
        if not subscription or subscription["workspace_id"] != workspace_id:
            raise HTTPException(
                status_code=404,
                detail={"error": "subscription_not_found"},
            )
        return subscription

    def _delivery_for_workspace(
        self,
        delivery_record_id: str,
        workspace_id: str,
    ) -> Dict[str, Any]:
        delivery = self._deliveries.get(delivery_record_id)
        if not delivery or delivery["workspace_id"] != workspace_id:
            raise HTTPException(
                status_code=404,
                detail={"error": "delivery_not_found"},
            )
        return delivery

    @staticmethod
    def _delivery_fingerprint(
        subscription_id: str,
        event_type: str,
        payload_hash: str,
    ) -> str:
        return f"{subscription_id}:{event_type}:{payload_hash}"

    @staticmethod
    def _public_subscription(subscription: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": subscription["id"],
            "workspace_id": subscription["workspace_id"],
            "target_url": subscription["target_url"],
            "event_types": list(subscription["event_types"]),
            "enabled": subscription["enabled"],
            "created_at": subscription["created_at"],
            "updated_at": subscription["updated_at"],
        }

    @staticmethod
    def _public_delivery(delivery: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": delivery["id"],
            "delivery_id": delivery["delivery_id"],
            "subscription_id": delivery["subscription_id"],
            "workspace_id": delivery["workspace_id"],
            "event_type": delivery["event_type"],
            "status": delivery["status"],
            "attempts": delivery["attempts"],
            "created_at": delivery["created_at"],
            "updated_at": delivery["updated_at"],
        }


webhook_store = WebhookStore()
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/subscriptions", status_code=201)
async def create_subscription(request: SubscriptionCreate):
    return webhook_store.create_subscription(request)


@router.get("/subscriptions")
async def list_subscriptions(workspace_id: str = Query(..., min_length=1)):
    return {"subscriptions": webhook_store.list_subscriptions(workspace_id)}


@router.patch("/subscriptions/{subscription_id}")
async def update_subscription(
    subscription_id: str,
    request: SubscriptionUpdate,
    workspace_id: str = Query(..., min_length=1),
):
    return webhook_store.update_subscription(
        subscription_id,
        workspace_id,
        request,
    )


@router.post("/deliveries", status_code=201)
async def create_delivery(request: DeliveryCreate):
    return webhook_store.create_delivery(request)


@router.get("/deliveries")
async def list_deliveries(workspace_id: str = Query(..., min_length=1)):
    return {"deliveries": webhook_store.list_deliveries(workspace_id)}


@router.get("/deliveries/{delivery_record_id}")
async def get_delivery(
    delivery_record_id: str,
    workspace_id: str = Query(..., min_length=1),
):
    return webhook_store.get_delivery(delivery_record_id, workspace_id)


@router.post("/deliveries/{delivery_record_id}/retry")
async def retry_delivery(delivery_record_id: str, request: DeliveryRetry):
    return webhook_store.retry_delivery(delivery_record_id, request)
