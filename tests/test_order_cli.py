"""order_cli environment wiring: .env loader, broker selection, and the
AlpacaCLIBroker adapter (with an injected fake executor — offline)."""
from __future__ import annotations

import pytest

from gated_agent.cli_executor import ExecResult
from gated_agent.order_cli import (
    AlpacaCLIBroker,
    StubCLIBroker,
    broker_from_env,
    load_env,
)

LEGS = [
    {"occ_symbol": "SPY260904P00625000", "side": "sell", "qty": 1, "limit": 1.30},
    {"occ_symbol": "SPY260904P00620000", "side": "buy", "qty": 1, "limit": 1.05},
]


def clear_keys(monkeypatch):
    for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY",
              "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"):
        # touch-then-delete registers the key with monkeypatch, so even values
        # written by code under test (load_env) are rolled back on teardown
        monkeypatch.setenv(k, "sentinel")
        monkeypatch.delenv(k)


# ── load_env ─────────────────────────────────────────────────────────────

def test_load_env_reads_file_and_maps_names(tmp_path, monkeypatch):
    clear_keys(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("# comment\nALPACA_API_KEY_ID=abc\n"
                   "ALPACA_API_SECRET_KEY='xyz'\n", encoding="utf-8")
    loaded = load_env(env)
    import os
    assert os.environ["ALPACA_API_KEY"] == "abc"      # mapped alias
    assert os.environ["ALPACA_SECRET_KEY"] == "xyz"   # quotes stripped
    assert "ALPACA_API_KEY" in loaded


def test_load_env_never_overwrites_environment(tmp_path, monkeypatch):
    clear_keys(monkeypatch)
    monkeypatch.setenv("ALPACA_API_KEY", "from-env")
    env = tmp_path / ".env"
    env.write_text("ALPACA_API_KEY=from-file\n", encoding="utf-8")
    load_env(env)
    import os
    assert os.environ["ALPACA_API_KEY"] == "from-env"


def test_load_env_missing_file_is_noop(tmp_path, monkeypatch):
    clear_keys(monkeypatch)
    assert load_env(tmp_path / "nope.env") == {}


def test_load_env_skips_template_placeholders(tmp_path, monkeypatch):
    """An unedited .env.example line must behave as absent — a placeholder
    ANTHROPIC_API_KEY would otherwise activate the LLM red-teamer with a
    bogus key and fail-closed veto every order."""
    clear_keys(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=your_key_here\n"
                   "ALPACA_API_KEY=your_paper_key_here\n"
                   "ALPACA_SECRET_KEY=real-secret\n", encoding="utf-8")
    loaded = load_env(env)
    import os
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "ALPACA_API_KEY" not in os.environ
    assert os.environ["ALPACA_SECRET_KEY"] == "real-secret"
    assert set(loaded) == {"ALPACA_SECRET_KEY"}


# ── broker selection ─────────────────────────────────────────────────────

def test_no_keys_selects_stub(tmp_path, monkeypatch):
    clear_keys(monkeypatch)
    broker = broker_from_env(env_file=tmp_path / "absent.env")
    assert isinstance(broker, StubCLIBroker)


def test_placeholder_keys_still_select_stub(tmp_path, monkeypatch):
    clear_keys(monkeypatch)
    monkeypatch.setenv("ALPACA_API_KEY", "your_paper_key_here")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "your_paper_secret_here")
    broker = broker_from_env(env_file=tmp_path / "absent.env")
    assert isinstance(broker, StubCLIBroker)


def test_real_keys_select_cli_adapter_dry_run_default(tmp_path, monkeypatch):
    clear_keys(monkeypatch)
    monkeypatch.setenv("ALPACA_API_KEY", "PKREALLOOKING")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s3cret")
    broker = broker_from_env(env_file=tmp_path / "absent.env")
    assert isinstance(broker, AlpacaCLIBroker)
    assert broker.dry_run is True                     # safety default survives


# ── AlpacaCLIBroker adapter ──────────────────────────────────────────────

def fake_executor(result: ExecResult):
    seen = []

    def run(legs, *, dry_run=True, **kw):
        seen.append({"legs": legs, "dry_run": dry_run})
        return result

    return run, seen


def test_adapter_dry_run_status_and_preview():
    ok = ExecResult(True, True, {"order_class": "mleg"}, "{}")
    run, seen = fake_executor(ok)
    broker = AlpacaCLIBroker(dry_run=True, executor=run)
    rec = broker.submit_order("SPY", LEGS, "k1")
    assert rec["status"] == "dry_run" and rec["ok"]
    assert seen[0]["dry_run"] is True
    (preview,) = rec["cli_commands"]
    assert "--order-class mleg" in preview
    assert "--limit-price -0.25" in preview           # credit sign preserved
    assert broker.submitted == [rec]


def test_adapter_submitted_status_when_live():
    ok = ExecResult(True, False, {"id": "abc"}, "{}")
    run, seen = fake_executor(ok)
    broker = AlpacaCLIBroker(dry_run=False, executor=run)
    rec = broker.submit_order("SPY", LEGS, "k1")
    assert rec["status"] == "submitted"
    assert seen[0]["dry_run"] is False


def test_adapter_error_status_on_cli_failure():
    bad = ExecResult(False, True, None, "boom")
    run, _ = fake_executor(bad)
    broker = AlpacaCLIBroker(dry_run=True, executor=run)
    rec = broker.submit_order("SPY", LEGS, "k1")
    assert rec["status"] == "error" and rec["raw"] == "boom"
