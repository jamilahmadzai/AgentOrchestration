from pathlib import Path

from scripts.audit_docker_context import (
    DEFAULT_PROBES,
    audit_docker_context,
    format_report,
    is_ignored,
    load_dockerignore,
    main,
)


ROOT = Path(__file__).resolve().parents[1]


def test_root_dockerignore_blocks_required_local_files():
    rules = load_dockerignore(ROOT)
    missing = [
        probe for probe in DEFAULT_PROBES if not is_ignored(probe, rules)
    ]

    assert missing == []


def test_root_dockerignore_keeps_examples_available():
    rules = load_dockerignore(ROOT)

    assert not is_ignored(".env.example", rules)
    assert not is_ignored(".env.sample", rules)


def test_current_repository_passes_docker_context_audit():
    audit = audit_docker_context(ROOT)

    assert audit.ok, format_report(audit)


def test_audit_reports_missing_ignore_coverage(tmp_path):
    (tmp_path / ".dockerignore").write_text("*.log\n", encoding="utf-8")

    audit = audit_docker_context(tmp_path, probes=[".env", "app.log"])

    assert audit.missing_ignores == [".env"]
    assert not audit.ok
    assert "Missing .dockerignore coverage" in format_report(audit)


def test_audit_reports_unignored_real_context_entries(tmp_path):
    (tmp_path / ".dockerignore").write_text(".env\n", encoding="utf-8")
    (tmp_path / "debug").mkdir()
    (tmp_path / "debug" / "trace.json").write_text("{}\n", encoding="utf-8")

    audit = audit_docker_context(tmp_path, probes=[".env"])

    assert audit.unignored_prohibited == ["debug"]
    assert not audit.ok


def test_audit_reports_broad_shell_and_json_context_copies(tmp_path):
    (tmp_path / ".dockerignore").write_text(
        ".env\nlogs/\n.venv/\n",
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_text(
        "\n".join(
            [
                "FROM python:3.11-slim",
                "COPY . /app",
                "ADD [\"./\", \"/app\"]",
            ]
        ),
        encoding="utf-8",
    )

    audit = audit_docker_context(
        tmp_path,
        probes=[".env", "logs/app.log", ".venv/bin/python"],
    )

    assert audit.broad_copy_patterns == [
        "Dockerfile:2: COPY . /app",
        "Dockerfile:3: ADD [\"./\", \"/app\"]",
    ]
    assert not audit.ok


def test_audit_reports_broad_copy_after_line_continuation(tmp_path):
    (tmp_path / ".dockerignore").write_text(".env\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.11-slim\nCOPY \\\n  . \\\n  /app\n",
        encoding="utf-8",
    )

    audit = audit_docker_context(tmp_path, probes=[".env"])

    assert audit.broad_copy_patterns == ["Dockerfile:2: COPY . /app"]


def test_audit_allows_multistage_copy_from_named_stage(tmp_path):
    (tmp_path / ".dockerignore").write_text(".env\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text(
        "\n".join(
            [
                "FROM python:3.11-slim AS builder",
                "RUN mkdir /out",
                "FROM python:3.11-slim",
                "COPY --from=builder . /app",
            ]
        ),
        encoding="utf-8",
    )

    audit = audit_docker_context(tmp_path, probes=[".env"])

    assert audit.ok, format_report(audit)


def test_audit_ignores_dockerfiles_inside_excluded_local_dirs(tmp_path):
    (tmp_path / ".dockerignore").write_text(
        ".env\nnode_modules/\n",
        encoding="utf-8",
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "Dockerfile").write_text(
        "COPY . /unused\n",
        encoding="utf-8",
    )

    audit = audit_docker_context(tmp_path, probes=[".env"])

    assert audit.ok, format_report(audit)


def test_audit_passes_for_narrow_copy_policy(tmp_path):
    (tmp_path / ".dockerignore").write_text(
        ".env\nlogs/\n.venv/\ndebug/\n",
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_text(
        "\n".join(
            [
                "FROM python:3.11-slim",
                "COPY pyproject.toml README.md /app/",
                "COPY src/ /app/src/",
            ]
        ),
        encoding="utf-8",
    )

    audit = audit_docker_context(
        tmp_path,
        probes=[
            ".env",
            "logs/app.log",
            ".venv/bin/python",
            "debug/request.json",
        ],
    )

    assert audit.ok, format_report(audit)


def test_audit_cli_returns_nonzero_for_policy_failure(tmp_path):
    (tmp_path / ".dockerignore").write_text("*.log\n", encoding="utf-8")
    (tmp_path / ".env").write_text("local=true\n", encoding="utf-8")

    assert main(["--root", str(tmp_path)]) == 1
