"""Retention exception governance helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional


@dataclass(frozen=True)
class RetentionValidationIssue:
    """A single retention exception validation finding."""

    field: str
    message: str
    category: str = ""
    owner: str = ""
    code: str = "invalid"


class RetentionExceptionValidationError(ValueError):
    """Raised when retention exception governance validation fails."""

    def __init__(self, issues: Iterable[RetentionValidationIssue]):
        self.issues = list(issues)
        message = "; ".join(issue.message for issue in self.issues)
        super().__init__(message or "Retention exception validation failed")


@dataclass(frozen=True)
class RetentionException:
    """Temporary retention exception with accountable ownership."""

    category: str
    owner: str
    reason: str
    expires_at: Optional[date]
    review_at: Optional[date]

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
    ) -> "RetentionException":
        category = _first_present(data, "category", "data_category")
        owner = _first_present(data, "owner", "data_owner")
        reason = _first_present(data, "reason", "justification")
        expires_at = _first_present(
            data,
            "expires_at",
            "expires_on",
            "expiration_date",
        )
        review_at = _first_present(
            data,
            "review_at",
            "review_on",
            "review_date",
        )

        return cls(
            category=_clean_text(category),
            owner=_clean_text(owner),
            reason=_clean_text(reason),
            expires_at=_parse_date(expires_at),
            review_at=_parse_date(review_at),
        )

    def validation_issues(
        self,
        *,
        today: Optional[date] = None,
    ) -> List[RetentionValidationIssue]:
        today = today or _today()
        label = self.category or "unknown"
        issues: List[RetentionValidationIssue] = []

        for field, value in (
            ("category", self.category),
            ("owner", self.owner),
            ("reason", self.reason),
        ):
            if not isinstance(value, str) or not value.strip():
                issues.append(
                    RetentionValidationIssue(
                        field,
                        f"Retention exception requires non-empty {field}",
                        category=self.category,
                        owner=self.owner,
                        code="required",
                    )
                )

        if not isinstance(self.expires_at, date):
            issues.append(
                RetentionValidationIssue(
                    "expires_at",
                    "Retention exception requires an expiration date",
                    category=self.category,
                    owner=self.owner,
                    code="required",
                )
            )
        elif self.is_expired(today=today):
            issues.append(
                RetentionValidationIssue(
                    "expires_at",
                    f"Retention exception for {label} expired on "
                    f"{self.expires_at.isoformat()}",
                    category=self.category,
                    owner=self.owner,
                    code="expired",
                )
            )

        if not isinstance(self.review_at, date):
            issues.append(
                RetentionValidationIssue(
                    "review_at",
                    "Retention exception requires a review date",
                    category=self.category,
                    owner=self.owner,
                    code="required",
                )
            )
        elif (
            isinstance(self.expires_at, date)
            and self.review_at > self.expires_at
        ):
            issues.append(
                RetentionValidationIssue(
                    "review_at",
                    f"Retention exception for {label} has a review date "
                    "after expiration",
                    category=self.category,
                    owner=self.owner,
                    code="invalid_window",
                )
            )

        return issues

    def validate(self, *, today: Optional[date] = None) -> None:
        issues = self.validation_issues(today=today)
        if issues:
            raise RetentionExceptionValidationError(issues)

    def is_expired(self, *, today: Optional[date] = None) -> bool:
        today = today or _today()
        return isinstance(self.expires_at, date) and self.expires_at < today

    def report_entry(self) -> Dict[str, str]:
        return {
            "category": self.category,
            "reason": self.reason,
            "expires_at": _format_date(self.expires_at),
            "review_at": _format_date(self.review_at),
        }


class RetentionExceptionRegistry:
    """In-memory registry for owner-accountable retention exceptions."""

    def __init__(
        self,
        exceptions: Optional[Iterable[RetentionException]] = None,
    ):
        self._exceptions: List[RetentionException] = list(exceptions or [])

    def add(
        self,
        exception: RetentionException,
        *,
        today: Optional[date] = None,
    ) -> None:
        exception.validate(today=today)
        self._exceptions.append(exception)

    def validate_governance(self, *, today: Optional[date] = None) -> None:
        issues: List[RetentionValidationIssue] = []
        for exception in self._exceptions:
            issues.extend(exception.validation_issues(today=today))
        if issues:
            raise RetentionExceptionValidationError(issues)

    def active_by_owner(
        self,
        *,
        today: Optional[date] = None,
    ) -> Dict[str, List[Dict[str, str]]]:
        today = today or _today()
        grouped: Dict[str, List[Dict[str, str]]] = {}

        for exception in self._exceptions:
            metadata_issues = [
                issue
                for issue in exception.validation_issues(today=today)
                if issue.code != "expired"
            ]
            if metadata_issues:
                raise RetentionExceptionValidationError(metadata_issues)
            if exception.is_expired(today=today):
                continue
            grouped.setdefault(exception.owner, []).append(
                exception.report_entry()
            )

        return {
            owner: sorted(entries, key=lambda entry: entry["category"])
            for owner, entries in sorted(grouped.items())
        }

    def list(self) -> List[RetentionException]:
        return list(self._exceptions)

    def __len__(self) -> int:
        return len(self._exceptions)


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        value = value.strip()
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None
    return None


def _first_present(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


def _format_date(value: Optional[date]) -> str:
    return value.isoformat() if isinstance(value, date) else ""


def _clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _today() -> date:
    return datetime.now(timezone.utc).date()
