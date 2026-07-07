"""CLI smoke tests."""

from __future__ import annotations

from click.testing import CliRunner

from cli import cli


def test_version_option():
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "legacy-reverse" in result.output
