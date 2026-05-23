"""Audit Docker build-context policy without requiring Docker."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Optional, Sequence


DEFAULT_PROBES = (
    ".env",
    ".env.local",
    ".env.production",
    "config/.env.production",
    "secrets/private.key",
    "secrets/service.pem",
    "__pycache__/module.pyc",
    ".pytest_cache/CACHEDIR.TAG",
    ".mypy_cache/meta.json",
    ".ruff_cache/content",
    ".tox/py/.pkg",
    ".nox/session/bin/python",
    ".venv/bin/python",
    "venv/bin/python",
    "env/bin/python",
    "node_modules/pkg/index.js",
    "app.log",
    "logs/app.log",
    "debug/trace.json",
    "debug-output/request.json",
    "tmp/session.json",
    "scratch/context.tar",
    "outputs/run.json",
    "artifacts/result.json",
    "coverage.tmp",
    ".coverage",
    "coverage.xml",
    "htmlcov/index.html",
    ".DS_Store",
    "Thumbs.db",
    ".idea/workspace.xml",
    ".vscode/settings.json",
    "dist/package.whl",
    "build/lib/module.py",
    "local.sqlite",
)

PROHIBITED_SEGMENTS = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".vscode",
    "__pycache__",
    "artifacts",
    "build",
    "debug",
    "debug-output",
    "dist",
    "env",
    "htmlcov",
    "logs",
    "node_modules",
    "outputs",
    "scratch",
    "temp",
    "tmp",
    "venv",
}
PROHIBITED_NAMES = {".coverage", ".DS_Store", "Thumbs.db", "coverage.xml"}
PROHIBITED_SUFFIXES = {
    ".bak",
    ".crt",
    ".csr",
    ".db",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".swp",
    ".tmp",
}


@dataclass(frozen=True)
class IgnoreRule:
    pattern: str
    negated: bool
    directory_only: bool

    def matches(self, candidate: str) -> bool:
        path = candidate.strip("/")
        pattern = self.pattern.strip("/")
        if not path or not pattern:
            return False

        if self.directory_only:
            return _matches_directory(pattern, path)
        if "/" in pattern:
            return fnmatch.fnmatchcase(path, pattern)
        return any(
            fnmatch.fnmatchcase(part, pattern)
            for part in PurePosixPath(path).parts
        )


@dataclass(frozen=True)
class DockerContextAudit:
    missing_ignores: Sequence[str]
    unignored_prohibited: Sequence[str]
    broad_copy_patterns: Sequence[str]

    @property
    def ok(self) -> bool:
        return not (
            self.missing_ignores
            or self.unignored_prohibited
            or self.broad_copy_patterns
        )


def load_dockerignore(root: Path) -> List[IgnoreRule]:
    dockerignore = root / ".dockerignore"
    if not dockerignore.exists():
        return []

    rules = []
    for raw_line in dockerignore.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:].strip()
        rules.append(
            IgnoreRule(
                pattern=line.rstrip("/"),
                negated=negated,
                directory_only=line.endswith("/"),
            )
        )
    return rules


def is_ignored(candidate: str, rules: Iterable[IgnoreRule]) -> bool:
    ignored = False
    for rule in rules:
        if rule.matches(candidate):
            ignored = not rule.negated
    return ignored


def audit_docker_context(
    root: Path,
    probes: Sequence[str] = DEFAULT_PROBES,
) -> DockerContextAudit:
    root = root.resolve()
    rules = load_dockerignore(root)
    return DockerContextAudit(
        missing_ignores=sorted(
            probe for probe in probes if not is_ignored(probe, rules)
        ),
        unignored_prohibited=scan_unignored_prohibited(root, rules),
        broad_copy_patterns=scan_broad_copy_patterns(root),
    )


def scan_unignored_prohibited(
    root: Path,
    rules: Sequence[IgnoreRule],
) -> List[str]:
    violations = []
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        relative_dir = _relative_path(current_path, root)
        kept_dirs = []

        for dirname in dirnames:
            relative = _join_relative(relative_dir, dirname)
            if is_ignored(relative, rules):
                continue
            if is_prohibited_context_path(relative):
                violations.append(relative)
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            relative = _join_relative(relative_dir, filename)
            if not is_ignored(relative, rules) and is_prohibited_context_path(
                relative
            ):
                violations.append(relative)

    return sorted(set(violations))


def scan_broad_copy_patterns(root: Path) -> List[str]:
    violations = []
    for dockerfile in _dockerfiles(root):
        relative = _relative_path(dockerfile, root)
        logical_lines = _dockerfile_logical_lines(dockerfile)
        for line_number, line in logical_lines:
            if _uses_broad_build_context_source(line):
                violations.append(f"{relative}:{line_number}: {line}")
    return violations


def is_prohibited_context_path(candidate: str) -> bool:
    path = candidate.strip("/")
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else path
    if any(part in PROHIBITED_SEGMENTS for part in parts):
        return True
    if name in PROHIBITED_NAMES:
        return True
    if name.startswith(".env") and name not in {".env.example", ".env.sample"}:
        return True
    return any(name.endswith(suffix) for suffix in PROHIBITED_SUFFIXES)


def format_report(audit: DockerContextAudit) -> str:
    sections = []
    if audit.missing_ignores:
        sections.append(
            ["Missing .dockerignore coverage:"]
            + [f"  - {item}" for item in audit.missing_ignores]
        )
    if audit.unignored_prohibited:
        sections.append(
            ["Unignored prohibited context entries:"]
            + [f"  - {item}" for item in audit.unignored_prohibited]
        )
    if audit.broad_copy_patterns:
        sections.append(
            ["Broad Dockerfile COPY/ADD patterns:"]
            + [f"  - {item}" for item in audit.broad_copy_patterns]
        )
    if not sections:
        return "Docker context policy passed."
    return "\n".join(line for section in sections for line in section)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when Docker build-context policy is violated."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root to audit.",
    )
    args = parser.parse_args(argv)
    audit = audit_docker_context(args.root)
    print(format_report(audit))
    return 0 if audit.ok else 1


def _matches_directory(pattern: str, candidate: str) -> bool:
    if "/" in pattern:
        return candidate == pattern or candidate.startswith(f"{pattern}/")
    return any(
        part == pattern or fnmatch.fnmatchcase(part, pattern)
        for part in PurePosixPath(candidate).parts
    )


def _dockerfiles(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if any(part in {".git", ".venv"} for part in path.parts):
            continue
        if path.is_file() and _is_dockerfile(path):
            yield path


def _is_dockerfile(path: Path) -> bool:
    return (
        path.name == "Dockerfile"
        or path.name.startswith("Dockerfile.")
        or path.name.endswith(".Dockerfile")
        or path.suffix == ".dockerfile"
    )


def _dockerfile_logical_lines(path: Path) -> Iterable[tuple[int, str]]:
    start_line = 0
    pending = ""
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not pending:
            start_line = line_number
        if line.endswith("\\"):
            pending += line[:-1].rstrip() + " "
            continue
        logical = (pending + line).strip()
        pending = ""
        if logical and not logical.startswith("#"):
            yield start_line, logical
    if pending.strip():
        yield start_line, pending.strip()


def _uses_broad_build_context_source(line: str) -> bool:
    instruction, _, remainder = line.partition(" ")
    if instruction.upper() not in {"ADD", "COPY"}:
        return False

    options, payload = _split_docker_instruction(remainder.strip())
    if instruction.upper() == "COPY" and _has_from_option(options):
        return False

    sources = _copy_sources(payload)
    return any(source in {".", "./"} for source in sources)


def _split_docker_instruction(remainder: str) -> tuple[list[str], str]:
    options = []
    payload = remainder
    while payload.startswith("--"):
        option, separator, rest = payload.partition(" ")
        options.append(option)
        if not separator:
            return options, ""
        payload = rest.lstrip()
    return options, payload


def _has_from_option(options: Sequence[str]) -> bool:
    return any(
        option == "--from" or option.startswith("--from=")
        for option in options
    )


def _copy_sources(payload: str) -> list[str]:
    if not payload:
        return []
    if payload.startswith("["):
        try:
            values = json.loads(payload)
        except json.JSONDecodeError:
            return []
        return [str(value) for value in values[:-1]]

    try:
        values = shlex.split(payload)
    except ValueError:
        return []
    return values[:-1]


def _relative_path(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return path.as_posix()
    return "" if relative == Path(".") else relative.as_posix()


def _join_relative(relative_dir: str, name: str) -> str:
    return name if not relative_dir else f"{relative_dir}/{name}"


if __name__ == "__main__":
    raise SystemExit(main())
