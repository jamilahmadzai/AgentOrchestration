"""Artifact ingestion validation and metadata storage."""

import hashlib
import re
import time
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Dict, Optional

from fastapi import HTTPException

MAX_ARTIFACT_BODY_BYTES = 1024 * 1024
_ARTIFACT_NAME_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)


@dataclass(frozen=True)
class ArtifactRecord:
    agent_id: str
    name: str
    size: int
    sha256: str
    content_type: str
    created_at: float


class ArtifactIngestionService:
    def __init__(self, max_body_bytes: int = MAX_ARTIFACT_BODY_BYTES):
        self.max_body_bytes = max_body_bytes
        self._records: Dict[str, ArtifactRecord] = {}

    def clear(self) -> None:
        self._records.clear()

    def count(self) -> int:
        return len(self._records)

    def get(
        self,
        agent_id: str,
        artifact_name: str,
    ) -> Optional[ArtifactRecord]:
        return self._records.get(self._record_key(agent_id, artifact_name))

    async def ingest(
        self,
        *,
        agent_id: str,
        artifact_name: str,
        content_length: Optional[str],
        content_type: Optional[str],
        read_body: Callable[[int], Awaitable[bytes]],
        agent_exists: Callable[[str], bool],
    ) -> ArtifactRecord:
        declared_size = self._validate_request(
            agent_id,
            artifact_name,
            content_length,
        )

        body = await read_body(self.max_body_bytes)
        body_size = len(body)
        if body_size == 0:
            raise HTTPException(
                status_code=400,
                detail="Artifact body is required",
            )
        if body_size > self.max_body_bytes:
            raise HTTPException(
                status_code=413,
                detail="Artifact body too large",
            )
        if declared_size is not None and declared_size != body_size:
            raise HTTPException(
                status_code=400,
                detail="Content-Length does not match body size",
            )

        if not agent_exists(agent_id):
            raise HTTPException(status_code=404, detail="Agent not found")

        record = ArtifactRecord(
            agent_id=agent_id,
            name=artifact_name,
            size=body_size,
            sha256=hashlib.sha256(body).hexdigest(),
            content_type=content_type or "application/octet-stream",
            created_at=time.time(),
        )
        self._records[self._record_key(agent_id, artifact_name)] = record
        return record

    def _validate_request(
        self,
        agent_id: str,
        artifact_name: str,
        content_length: Optional[str],
    ) -> Optional[int]:
        if not agent_id or not agent_id.strip():
            raise HTTPException(status_code=400, detail="Agent id is required")
        if (
            not _ARTIFACT_NAME_PATTERN.fullmatch(artifact_name)
            or artifact_name in {".", ".."}
        ):
            raise HTTPException(
                status_code=400,
                detail="Invalid artifact name",
            )
        if content_length is None:
            return None
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Invalid Content-Length",
            ) from exc
        if declared_size <= 0:
            raise HTTPException(
                status_code=400,
                detail="Artifact body is required",
            )
        if declared_size > self.max_body_bytes:
            raise HTTPException(
                status_code=413,
                detail="Artifact body too large",
            )
        return declared_size

    def _record_key(self, agent_id: str, artifact_name: str) -> str:
        return f"{agent_id}:{artifact_name}"


artifact_service = ArtifactIngestionService()


async def read_body_with_limit(
    chunks: AsyncIterator[bytes],
    max_body_bytes: int,
) -> bytes:
    body = bytearray()
    total_size = 0
    async for chunk in chunks:
        total_size += len(chunk)
        if total_size > max_body_bytes:
            raise HTTPException(
                status_code=413,
                detail="Artifact body too large",
            )
        body.extend(chunk)
    return bytes(body)
