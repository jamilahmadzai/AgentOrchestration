"""Content-addressed artifact storage helpers."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple


class SuspiciousArtifactMetadataError(ValueError):
    """Raised when caller metadata conflicts with verified artifact content."""

    def __init__(
        self,
        message: str,
        *,
        blob_ref: str,
        content_digest: str,
    ) -> None:
        super().__init__(message)
        self.blob_ref = blob_ref
        self.content_digest = content_digest


@dataclass(frozen=True)
class BlobRecord:
    blob_ref: str
    content_digest: str
    size: int
    created_at: float


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    logical_name: str
    blob_ref: str
    content_digest: str
    size: int
    created_at: float
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class ArtifactAuditRecord:
    logical_name: str
    decision: str
    blob_ref: str
    content_digest: str
    details: Mapping[str, Any]


class ArtifactStore:
    """In-memory content-addressed artifact store.

    The store verifies the content digest before any deduplication decision.
    Logical names and caller metadata are descriptive; immutable blob
    references are derived only from verified bytes.
    """

    def __init__(self) -> None:
        self._blobs: Dict[str, Tuple[BlobRecord, bytes]] = {}
        self._artifacts: Dict[str, ArtifactRecord] = {}
        self._logical_history: Dict[str, List[str]] = {}
        self._audit_records: List[ArtifactAuditRecord] = []

    def put(
        self,
        logical_name: str,
        content: bytes,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ArtifactRecord:
        name = self._normalize_logical_name(logical_name)
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")

        metadata_copy = dict(metadata or {})
        content_digest = hashlib.sha256(content).hexdigest()
        blob_ref = self._blob_ref(content_digest)
        blob = self._store_blob(content_digest, blob_ref, content)

        suspicious = self._metadata_mismatch_reason(
            metadata_copy,
            content_digest,
            blob_ref,
        )
        if suspicious:
            self._record_audit(
                name,
                "reject_suspicious_metadata_reuse",
                blob.blob_ref,
                blob.content_digest,
                suspicious,
            )
            raise SuspiciousArtifactMetadataError(
                "artifact metadata does not match verified content digest",
                blob_ref=blob.blob_ref,
                content_digest=blob.content_digest,
            )

        previous_artifact_ids = self._logical_history.get(name, [])
        if previous_artifact_ids:
            latest = self._artifacts[previous_artifact_ids[-1]]
            decision = (
                "deduplicate_verified_digest"
                if latest.content_digest == content_digest
                else "new_blob_for_verified_digest_mismatch"
            )
        else:
            decision = "store_new_verified_blob"

        artifact = self._record_artifact(name, blob, metadata_copy)
        self._record_audit(
            name,
            decision,
            blob.blob_ref,
            blob.content_digest,
            {
                "blob_count": len(self._blobs),
                "artifact_count": len(self._artifacts),
            },
        )
        return artifact

    def get_blob(self, blob_ref: str) -> bytes:
        digest = self._digest_from_blob_ref(blob_ref)
        return self._blobs[digest][1]

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        return self._artifacts[artifact_id]

    def latest_for_name(self, logical_name: str) -> Optional[ArtifactRecord]:
        name = self._normalize_logical_name(logical_name)
        artifact_ids = self._logical_history.get(name)
        if not artifact_ids:
            return None
        return self._artifacts[artifact_ids[-1]]

    def history_for_name(self, logical_name: str) -> List[ArtifactRecord]:
        name = self._normalize_logical_name(logical_name)
        return [
            self._artifacts[artifact_id]
            for artifact_id in self._logical_history.get(name, [])
        ]

    @property
    def blob_count(self) -> int:
        return len(self._blobs)

    @property
    def artifact_count(self) -> int:
        return len(self._artifacts)

    @property
    def audit_records(self) -> Tuple[ArtifactAuditRecord, ...]:
        return tuple(self._audit_records)

    def _store_blob(
        self,
        content_digest: str,
        blob_ref: str,
        content: bytes,
    ) -> BlobRecord:
        existing = self._blobs.get(content_digest)
        if existing is not None:
            return existing[0]

        blob = BlobRecord(
            blob_ref=blob_ref,
            content_digest=content_digest,
            size=len(content),
            created_at=time.time(),
        )
        self._blobs[content_digest] = (blob, bytes(content))
        return blob

    def _record_artifact(
        self,
        logical_name: str,
        blob: BlobRecord,
        metadata: Mapping[str, Any],
    ) -> ArtifactRecord:
        artifact_id = f"{logical_name}:{blob.content_digest}"
        stored_metadata = {
            **metadata,
            "blob_ref": blob.blob_ref,
            "content_digest": blob.content_digest,
            "content_digest_algorithm": "sha256",
        }
        artifact = ArtifactRecord(
            artifact_id=artifact_id,
            logical_name=logical_name,
            blob_ref=blob.blob_ref,
            content_digest=blob.content_digest,
            size=blob.size,
            created_at=time.time(),
            metadata=MappingProxyType(stored_metadata),
        )
        self._artifacts[artifact_id] = artifact
        history = self._logical_history.setdefault(logical_name, [])
        if artifact_id not in history:
            history.append(artifact_id)
        return artifact

    def _record_audit(
        self,
        logical_name: str,
        decision: str,
        blob_ref: str,
        content_digest: str,
        details: Mapping[str, Any],
    ) -> None:
        self._audit_records.append(
            ArtifactAuditRecord(
                logical_name=logical_name,
                decision=decision,
                blob_ref=blob_ref,
                content_digest=content_digest,
                details=MappingProxyType(dict(details)),
            )
        )

    @staticmethod
    def _normalize_logical_name(logical_name: str) -> str:
        if not isinstance(logical_name, str) or not logical_name.strip():
            raise ValueError("logical_name must be a non-empty string")
        return logical_name.strip()

    @staticmethod
    def _blob_ref(content_digest: str) -> str:
        return f"sha256:{content_digest}"

    @staticmethod
    def _digest_from_blob_ref(blob_ref: str) -> str:
        if not isinstance(blob_ref, str) or not blob_ref.startswith("sha256:"):
            raise KeyError(blob_ref)
        return blob_ref.removeprefix("sha256:")

    @staticmethod
    def _metadata_mismatch_reason(
        metadata: Mapping[str, Any],
        content_digest: str,
        blob_ref: str,
    ) -> Optional[Dict[str, str]]:
        claimed_digest = metadata.get("content_digest")
        if claimed_digest is not None and claimed_digest != content_digest:
            return {
                "field": "content_digest",
                "reason": "claimed digest does not match verified content",
            }

        claimed_blob_ref = metadata.get("blob_ref")
        if claimed_blob_ref is not None and claimed_blob_ref != blob_ref:
            return {
                "field": "blob_ref",
                "reason": "claimed blob_ref does not match verified content",
            }

        return None
