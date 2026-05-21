import argparse

import pytest

from src.cli.main import cli, non_negative_int


def test_non_negative_int_rejects_negative_values():
    with pytest.raises(
        argparse.ArgumentTypeError,
        match="zero or a positive integer",
    ):
        non_negative_int("-5")


def test_logs_tail_rejects_negative_values(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ao", "logs", "agent-1", "--tail", "-5"])

    with pytest.raises(SystemExit) as exc_info:
        cli()

    assert exc_info.value.code == 2
    assert "must be zero or a positive integer" in capsys.readouterr().err


def test_logs_tail_accepts_zero(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ao", "logs", "agent-1", "--tail", "0"])

    cli()

    assert "Fetching logs for agent: agent-1" in capsys.readouterr().out


def test_logs_tail_accepts_positive_values(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ao", "logs", "agent-1", "--tail", "25"])

    cli()

    assert "Fetching logs for agent: agent-1" in capsys.readouterr().out
