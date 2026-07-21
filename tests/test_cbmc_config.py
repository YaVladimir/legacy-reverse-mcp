"""utils/cbmc_config.py — config resolution and subprocess plumbing for CBMC.

This module previously had zero coverage, which let a guaranteed crash ship:
``_load_env_file`` called ``.partition()`` on a ``re.Match`` and raised
AttributeError on the first ``KEY=VALUE`` line of any ``.env``. These tests pin
the three seams the batch pipeline depends on: (1) the .env loader actually
loads; (2) binary resolution honors the trust boundary — a repo-supplied
``binary_path``/.env names an executable and must be opt-in via
LEGACY_REVERSE_TRUST_REPO_CONFIG=1; the ``[cbmc] project`` source selector is
subject to the same boundary; (3) ``cbmc_call`` builds typed argv correctly and
parses both single-line and pretty-printed JSON output."""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

import utils.cbmc_config as cc


@pytest.fixture
def env(monkeypatch):
    """Isolated os.environ copy: the module mutates os.environ via setdefault, so
    tests must not leak keys into the real environment (or see keys from it)."""
    fake = dict(os.environ)
    for k in ("LEGACY_REVERSE_CBMC_BIN", "LEGACY_REVERSE_TRUST_REPO_CONFIG"):
        fake.pop(k, None)
    monkeypatch.setattr(os, "environ", fake)
    return fake


# --- .env loader (the shipped AttributeError regression) ---------------------

def test_load_env_file_plain_pair(tmp_path, env):
    (tmp_path / ".env").write_text("FOO_CBMC_TEST=bar\n", encoding="utf-8")
    assert cc._load_env_file(tmp_path / ".env") is True  # crashed before the fix
    assert env["FOO_CBMC_TEST"] == "bar"


def test_load_env_file_quotes_and_comments(tmp_path, env):
    (tmp_path / ".env").write_text(
        '# comment\nQUOTED_CBMC_TEST="a b"\nSINGLE_CBMC_TEST=\'x\'\n', encoding="utf-8"
    )
    assert cc._load_env_file(tmp_path / ".env") is True
    assert env["QUOTED_CBMC_TEST"] == "a b"
    assert env["SINGLE_CBMC_TEST"] == "x"


def test_load_env_file_does_not_override_existing(tmp_path, env):
    env["KEEP_CBMC_TEST"] = "original"
    (tmp_path / ".env").write_text("KEEP_CBMC_TEST=overwritten\n", encoding="utf-8")
    cc._load_env_file(tmp_path / ".env")
    assert env["KEEP_CBMC_TEST"] == "original"  # setdefault semantics


def test_load_env_file_missing(tmp_path, env):
    assert cc._load_env_file(tmp_path / "nope.env") is False


# --- binary resolution: trust boundary + project pinning ---------------------

def _write_toml(repo, body):
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "legacy-reverse.toml").write_text(body, encoding="utf-8")


def test_resolve_env_var_wins_and_keeps_trusted_toml_project(tmp_path, env):
    _write_toml(tmp_path, '[cbmc]\nproject = "pinned"\nbinary_path = "/repo/evil"\n')
    env["LEGACY_REVERSE_CBMC_BIN"] = "/usr/bin/cbmc"
    env["LEGACY_REVERSE_TRUST_REPO_CONFIG"] = "1"
    binary, cfg = cc.resolve_cbmc_config(tmp_path)
    assert binary == "/usr/bin/cbmc"
    # A trusted project pin survives every binary-resolution branch.
    assert cfg.get("project") == "pinned"


def test_resolve_empty_env_var_disables(tmp_path, env):
    env["LEGACY_REVERSE_CBMC_BIN"] = ""
    binary, _cfg = cc.resolve_cbmc_config(tmp_path)
    assert binary is None


def test_resolve_repo_toml_binary_ignored_without_trust(tmp_path, env, capsys):
    """A repo-committed binary_path names an executable — analyzing a hostile repo
    must not execute it. Without the trust flag it is announced and skipped."""
    _write_toml(tmp_path, '[cbmc]\nproject = "pinned"\nbinary_path = "/repo/evil"\n')
    binary, cfg = cc.resolve_cbmc_config(tmp_path)
    assert binary != "/repo/evil"
    assert binary == cc._default_binary_path()
    output = capsys.readouterr().out
    assert "binary_path" in output
    assert "project" in output
    assert "LEGACY_REVERSE_TRUST_REPO_CONFIG" in output
    assert "project" not in cfg
    assert "binary_path" not in cfg


def test_resolve_repo_project_ignored_without_trust(tmp_path, env, capsys):
    """A repo project pin can select and expose another local CBMC source index."""
    _write_toml(tmp_path, '[cbmc]\nproject = "other-local-project"\n')
    binary, cfg = cc.resolve_cbmc_config(tmp_path)
    assert binary == cc._default_binary_path()
    assert "project" not in cfg
    assert "source selector" in capsys.readouterr().out


def test_resolve_repo_toml_binary_honored_with_trust(tmp_path, env):
    _write_toml(tmp_path, '[cbmc]\nbinary_path = "/repo/cbmc"\n')
    env["LEGACY_REVERSE_TRUST_REPO_CONFIG"] = "1"
    binary, _cfg = cc.resolve_cbmc_config(tmp_path)
    assert binary == "/repo/cbmc"


def test_resolve_repo_dotenv_only_with_trust(tmp_path, env):
    (tmp_path / ".env").write_text("LEGACY_REVERSE_CBMC_BIN=/dotenv/cbmc\n", encoding="utf-8")
    binary, _ = cc.resolve_cbmc_config(tmp_path)
    assert binary == cc._default_binary_path()  # untrusted: .env not even loaded
    assert "LEGACY_REVERSE_CBMC_BIN" not in os.environ

    os.environ["LEGACY_REVERSE_TRUST_REPO_CONFIG"] = "1"
    binary, _ = cc.resolve_cbmc_config(tmp_path)
    assert binary == "/dotenv/cbmc"


# --- cbmc_call: argv construction + JSON parsing -----------------------------

@pytest.fixture
def fake_run(monkeypatch, tmp_path):
    """A fake binary on disk + captured subprocess.run. Returns a dict with the
    captured argv and a setter for the fake process output."""
    bin_path = tmp_path / "cbmc-bin"
    bin_path.write_text("", encoding="utf-8")
    state = {"binary": str(bin_path), "argv": None,
             "stdout": "{}", "stderr": "", "returncode": 0}

    def run(argv, **kwargs):
        state["argv"] = argv
        return SimpleNamespace(returncode=state["returncode"],
                               stdout=state["stdout"], stderr=state["stderr"])

    monkeypatch.setattr(subprocess, "run", run)
    return state


def test_cbmc_call_argv_dict_and_bools(fake_run):
    result, info = cc.cbmc_call(
        "search_graph",
        {"project": "p", "limit": 5, "flag_on": True, "flag_off": False},
        binary=fake_run["binary"],
    )
    assert result == {}
    argv = fake_run["argv"]
    assert argv[1:3] == ["cli", "search_graph"]
    assert argv[3:] == [
        "--project", "p", "--limit", "5", "--flag_on", "true", "--flag_off", "false",
    ]
    assert "" not in argv


def test_cbmc_call_repeats_array_flags(fake_run):
    result, _ = cc.cbmc_call(
        "get_architecture",
        {"project": "p", "aspects": ["layers", "clusters"]},
        binary=fake_run["binary"],
    )
    assert result == {}
    assert fake_run["argv"][3:] == [
        "--project", "p", "--aspects", "layers", "--aspects", "clusters",
    ]


def test_cbmc_call_parses_pretty_printed_json_after_warnings(fake_run):
    payload = {"results": [{"qualified_name": "a.B"}]}
    fake_run["stdout"] = "warning: index is stale\n" + json.dumps(payload, indent=2) + "\n"
    result, info = cc.cbmc_call("search_graph", "{}", binary=fake_run["binary"])
    assert result == payload  # multi-line JSON must parse, not "bad JSON"
    assert "error" not in info


def test_cbmc_call_parses_single_line_json(fake_run):
    fake_run["stdout"] = 'note\n{"ok": 1}\ntrailing noise\n'
    result, _ = cc.cbmc_call("t", None, binary=fake_run["binary"])
    assert result == {"ok": 1}


def test_cbmc_call_no_json(fake_run):
    fake_run["stdout"] = "nothing here\n"
    result, info = cc.cbmc_call("t", None, binary=fake_run["binary"])
    assert result is None
    assert info["error"] == "no JSON output"


def test_cbmc_call_nonzero_exit(fake_run):
    fake_run["returncode"] = 2
    fake_run["stderr"] = "boom"
    result, info = cc.cbmc_call("t", None, binary=fake_run["binary"])
    assert result is None
    assert "boom" in info["error"]


def test_cbmc_call_missing_binary(tmp_path):
    result, info = cc.cbmc_call("t", None, binary=str(tmp_path / "no-such-bin"))
    assert result is None
    assert "binary not found" in info["error"]
