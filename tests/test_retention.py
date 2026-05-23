from datetime import date

import pytest

from src.common.retention import (
    RetentionException,
    RetentionExceptionRegistry,
    RetentionExceptionValidationError,
)


TODAY = date(2026, 5, 23)


def test_retention_exception_requires_owner_reason_expiration_and_review():
    exception = RetentionException(
        category="artifact-cache",
        owner=" ",
        reason="incident review",
        expires_at=date(2026, 6, 1),
        review_at=date(2026, 5, 30),
    )

    with pytest.raises(RetentionExceptionValidationError) as exc_info:
        exception.validate(today=TODAY)

    assert [issue.field for issue in exc_info.value.issues] == ["owner"]


def test_registry_reports_all_missing_governance_fields_together():
    exception = RetentionException(
        category="",
        owner="",
        reason="",
        expires_at=None,
        review_at=None,
    )
    registry = RetentionExceptionRegistry([exception])

    with pytest.raises(RetentionExceptionValidationError) as exc_info:
        registry.validate_governance(today=TODAY)

    fields = [issue.field for issue in exc_info.value.issues]
    assert fields == [
        "category",
        "owner",
        "reason",
        "expires_at",
        "review_at",
    ]


def test_expired_exceptions_fail_governance_validation():
    registry = RetentionExceptionRegistry(
        [
            RetentionException(
                category="validation-payloads",
                owner="privacy",
                reason="customer dispute",
                expires_at=date(2026, 5, 22),
                review_at=date(2026, 5, 20),
            )
        ]
    )

    with pytest.raises(RetentionExceptionValidationError) as exc_info:
        registry.validate_governance(today=TODAY)

    assert "expired on 2026-05-22" in str(exc_info.value)


def test_review_date_must_not_follow_expiration():
    exception = RetentionException(
        category="debug-exports",
        owner="security",
        reason="incident review",
        expires_at=date(2026, 6, 1),
        review_at=date(2026, 6, 2),
    )

    with pytest.raises(RetentionExceptionValidationError) as exc_info:
        exception.validate(today=TODAY)

    assert exc_info.value.issues[0].field == "review_at"


def test_active_report_groups_entries_by_owner_and_sorts_categories():
    registry = RetentionExceptionRegistry()
    registry.add(
        RetentionException(
            category="validation-payloads",
            owner="data-governance",
            reason="customer dispute",
            expires_at=date(2026, 6, 1),
            review_at=date(2026, 5, 30),
        ),
        today=TODAY,
    )
    registry.add(
        RetentionException(
            category="artifact-cache",
            owner="security",
            reason="incident review",
            expires_at=date(2026, 6, 15),
            review_at=date(2026, 6, 1),
        ),
        today=TODAY,
    )
    registry.add(
        RetentionException(
            category="failed-validation",
            owner="data-governance",
            reason="audit sampling",
            expires_at=date(2026, 6, 20),
            review_at=date(2026, 6, 10),
        ),
        today=TODAY,
    )

    report = registry.active_by_owner(today=TODAY)

    assert list(report) == ["data-governance", "security"]
    assert [
        entry["category"] for entry in report["data-governance"]
    ] == ["failed-validation", "validation-payloads"]
    assert report["security"] == [
        {
            "category": "artifact-cache",
            "reason": "incident review",
            "expires_at": "2026-06-15",
            "review_at": "2026-06-01",
        }
    ]


def test_active_report_excludes_expired_but_still_flags_missing_metadata():
    registry = RetentionExceptionRegistry(
        [
            RetentionException(
                category="expired-cache",
                owner="security",
                reason="old hold",
                expires_at=date(2026, 5, 1),
                review_at=date(2026, 4, 20),
            ),
            RetentionException(
                category="active-cache",
                owner="security",
                reason="incident review",
                expires_at=date(2026, 6, 1),
                review_at=date(2026, 5, 30),
            ),
        ]
    )

    assert registry.active_by_owner(today=TODAY)["security"] == [
        {
            "category": "active-cache",
            "reason": "incident review",
            "expires_at": "2026-06-01",
            "review_at": "2026-05-30",
        }
    ]

    invalid = RetentionException(
        category="",
        owner="security",
        reason="missing category",
        expires_at=date(2026, 6, 1),
        review_at=date(2026, 5, 30),
    )
    registry = RetentionExceptionRegistry([invalid])

    with pytest.raises(RetentionExceptionValidationError):
        registry.active_by_owner(today=TODAY)


def test_exception_can_be_loaded_from_mapping_aliases_with_trimmed_iso_dates():
    exception = RetentionException.from_mapping(
        {
            "data_category": " exports ",
            "data_owner": " privacy ",
            "justification": " subject access request ",
            "expiration_date": "2026-07-01",
            "review_date": "2026-06-15T09:00:00",
        }
    )

    exception.validate(today=TODAY)
    assert exception.category == "exports"
    assert exception.owner == "privacy"
    assert exception.review_at == date(2026, 6, 15)


def test_bad_imported_dates_fail_as_missing_governance_metadata():
    exception = RetentionException.from_mapping(
        {
            "category": "exports",
            "owner": "privacy",
            "reason": "subject access request",
            "expires_at": "not a date",
            "review_at": "2026-99-99",
        }
    )

    with pytest.raises(RetentionExceptionValidationError) as exc_info:
        exception.validate(today=TODAY)

    assert [issue.field for issue in exc_info.value.issues] == [
        "expires_at",
        "review_at",
    ]


def test_add_rejects_expired_exception_before_it_enters_registry():
    registry = RetentionExceptionRegistry()

    with pytest.raises(RetentionExceptionValidationError):
        registry.add(
            RetentionException(
                category="short-lived-cache",
                owner="privacy",
                reason="temporary hold",
                expires_at=date(2026, 5, 1),
                review_at=date(2026, 4, 20),
            ),
            today=TODAY,
        )

    assert len(registry) == 0
