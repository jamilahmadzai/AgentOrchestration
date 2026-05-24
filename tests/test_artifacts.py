import pytest

from src.common.artifacts import ArtifactStore, SuspiciousArtifactMetadataError


def test_same_name_different_content_creates_distinct_blobs_and_history():
    store = ArtifactStore()

    first = store.put("report.json", b'{"status":"queued"}')
    second = store.put("report.json", b'{"status":"done"}')

    assert first.content_digest != second.content_digest
    assert first.blob_ref != second.blob_ref
    assert store.blob_count == 2
    assert store.artifact_count == 2
    assert store.latest_for_name("report.json") == second
    assert store.history_for_name("report.json") == [first, second]
    assert (
        store.audit_records[-1].decision
        == "new_blob_for_verified_digest_mismatch"
    )


def test_same_content_deduplicates_by_verified_digest_not_name():
    store = ArtifactStore()

    first = store.put("task-a/output.txt", b"same bytes", {"task": "a"})
    second = store.put("task-b/output.txt", b"same bytes", {"task": "b"})

    assert first.content_digest == second.content_digest
    assert first.blob_ref == second.blob_ref
    assert store.get_blob(first.blob_ref) == b"same bytes"
    assert store.blob_count == 1
    assert store.artifact_count == 2


def test_digest_metadata_mismatch_stores_new_blob_but_rejects_artifact():
    store = ArtifactStore()
    trusted = store.put("result.bin", b"trusted-content")

    with pytest.raises(SuspiciousArtifactMetadataError) as exc:
        store.put(
            "result.bin",
            b"different-content",
            {"content_digest": trusted.content_digest},
        )

    assert exc.value.content_digest != trusted.content_digest
    assert store.get_blob(exc.value.blob_ref) == b"different-content"
    assert store.blob_count == 2
    assert store.artifact_count == 1
    assert store.latest_for_name("result.bin") == trusted
    assert (
        store.audit_records[-1].decision
        == "reject_suspicious_metadata_reuse"
    )
    assert store.audit_records[-1].details["field"] == "content_digest"


def test_blob_ref_metadata_mismatch_stores_new_blob_but_rejects_artifact():
    store = ArtifactStore()
    trusted = store.put("result.bin", b"trusted-content")

    with pytest.raises(SuspiciousArtifactMetadataError) as exc:
        store.put(
            "result.bin",
            b"different-content",
            {"blob_ref": trusted.blob_ref},
        )

    assert exc.value.blob_ref != trusted.blob_ref
    assert store.get_blob(exc.value.blob_ref) == b"different-content"
    assert store.blob_count == 2
    assert store.artifact_count == 1
    assert store.latest_for_name("result.bin") == trusted
    assert store.audit_records[-1].details["field"] == "blob_ref"


def test_artifact_metadata_contains_immutable_verified_blob_reference():
    store = ArtifactStore()

    record = store.put("result.bin", b"trusted-content", {"owner": "task-1"})

    assert record.metadata["owner"] == "task-1"
    assert record.metadata["blob_ref"] == record.blob_ref
    assert record.metadata["content_digest"] == record.content_digest
    assert record.metadata["content_digest_algorithm"] == "sha256"
    with pytest.raises(TypeError):
        record.metadata["blob_ref"] = "sha256:tampered"


def test_rejects_invalid_inputs_before_creating_blobs():
    store = ArtifactStore()

    with pytest.raises(ValueError):
        store.put(" ", b"content")
    with pytest.raises(TypeError):
        store.put("result.bin", "not-bytes")

    assert store.blob_count == 0
    assert store.artifact_count == 0
