"""The failure family found in live-fire testing on 2026-08-25:
*works when run by hand, fails in the context it will actually run in.*

The context that matters is a Windows scheduled task. `schtasks /Create /TR`
cannot set a start-in directory, so the task inherits %windir%\\system32 as its
CWD and a PATH that need not contain the developer's shims. Three concrete
bugs were fixed on 08-25 (broker receipt, position_manager .env, CLAUDE_BIN
import-time freeze); the tests below cover the *rest* of the family and, more
importantly, are structural — they fail on the NEXT entry point or module-level
constant that reintroduces the pattern, not merely on the instances known today.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from gated_agent import paths, redteam_mcp as rt
from gated_agent.ledger import Ledger
from gated_agent.order_cli import load_env

SRC = Path(paths.__file__).resolve().parent
MODULES = sorted(p for p in SRC.glob("*.py") if p.name != "__init__.py")


def _module_scope_nodes(tree: ast.Module):
    """Top-level statements only — function/class bodies run at call time."""
    return [n for n in tree.body
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef))]


# ── type 3: constants frozen before load_env() ───────────────────────────

def test_no_module_level_environment_reads():
    """An os.environ read at module scope freezes before .env is loaded.

    This is exactly the CLAUDE_BIN bug: the absolute path written in .env
    could never take effect, precisely in the non-interactive context where
    it was the only thing that would have worked. Any new one must be moved
    into a function (call-time resolution) or justified here.
    """
    offenders = []
    for path in MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _module_scope_nodes(tree):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr in ("environ",
                                                                   "getenv"):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], (
        "module-scope environment reads freeze before load_env() runs: "
        + ", ".join(offenders))


def test_no_module_level_path_probes():
    """shutil.which() at module scope freezes the PATH answer at import.

    Same defect one layer over: a scheduled task's PATH is not the shell's,
    and the probe must therefore happen when the binary is actually needed.
    """
    offenders = []
    for path in MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _module_scope_nodes(tree):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "which"):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], ("module-scope PATH probes freeze at import: "
                            + ", ".join(offenders))


def test_claude_bin_probes_path_at_call_time(monkeypatch):
    """With no claude on PATH and no override, resolution falls back to the
    bare name — which fails to launch, which fail-closes into a veto."""
    monkeypatch.setattr(rt, "CLAUDE_BIN", None)
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setenv("PATH", "")
    assert rt._claude_bin() == "claude"


def test_claude_bin_reads_env_written_after_import(monkeypatch):
    """The scenario the 08-25 fix was for: .env is parsed long after this
    module was imported, and its value must still win."""
    monkeypatch.setattr(rt, "CLAUDE_BIN", None)
    monkeypatch.setenv("CLAUDE_BIN", r"C:\somewhere\claude.exe")
    assert rt._claude_bin() == r"C:\somewhere\claude.exe"


# ── type 2: every entry point must load .env itself ──────────────────────

def _main_block(tree: ast.Module):
    for node in tree.body:
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and getattr(node.test.left, "id", None) == "__name__"):
            return node
    return None


def test_every_main_entry_point_loads_dotenv():
    """`python -m gated_agent.<module>` is a scheduled-task payload shape.

    run.main() loaded .env; the standalone position_manager entry did not, and
    crashed live on 08-25. This asserts the property for EVERY __main__ block
    that exists now or is added later — the whole point of the sweep.
    """
    missing = []
    for path in MODULES:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        block = _main_block(tree)
        if block is None:
            continue
        body = ast.get_source_segment(text, block) or ""
        # The block may load .env itself, or delegate to a function in this
        # module that does (run.py -> main()). Resolve one level, which is as
        # deep as any entry point here goes.
        called = {n.func.id for n in ast.walk(block)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        for fn in tree.body:
            if isinstance(fn, ast.FunctionDef) and fn.name in called:
                body += ast.get_source_segment(text, fn) or ""
        if "load_env" not in body:
            missing.append(path.name)
    assert missing == [], (
        "python -m entry points that never load .env: " + ", ".join(missing))


def test_entry_points_are_the_ones_we_think():
    """Guard against a new entry point slipping in unreviewed."""
    found = {p.name for p in MODULES
             if _main_block(ast.parse(p.read_text(encoding="utf-8")))}
    assert found == {"run.py", "position_manager.py", "chain_fetcher.py"}


def test_dashboard_loads_dotenv():
    """The Streamlit entry is executed as a top-level script, so it cannot use
    relative imports — it was the one entry point with no .env path at all,
    working on Cloud (secrets.toml) and failing on the box that runs the agent.
    """
    text = (SRC / "dashboard.py").read_text(encoding="utf-8")
    assert "load_env" in text


# ── the CWD dependency underneath all of it ──────────────────────────────

def test_ledger_default_path_is_cwd_independent(tmp_path, monkeypatch):
    """The ledger is the single source of truth for dedup, once-per-day
    idempotency, the direction-flip guard and the daily loss halt. A
    CWD-relative default meant the scheduled task either could not write it
    (System32 is read-only) or wrote a SECOND one — and a fresh ledger reports
    "no orders today" to all four safety properties at once.
    """
    monkeypatch.chdir(tmp_path)
    assert Ledger().path == paths.LEDGER_DIR / "decisions.jsonl"
    assert Ledger().path.is_absolute()


def test_ledger_relative_override_is_anchored_too(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert Ledger("ledger/alt.jsonl").path == paths.ROOT / "ledger/alt.jsonl"


def test_ledger_absolute_override_is_respected(tmp_path):
    assert Ledger(tmp_path / "l.jsonl").path == tmp_path / "l.jsonl"


def test_load_env_finds_repo_root_dotenv_from_a_foreign_cwd(
        tmp_path, monkeypatch):
    """load_env() itself resolved ".env" against the CWD, so the 08-25
    position_manager fix (which called it) still found nothing under the
    scheduled task. The fix has to be one layer deeper than the crash."""
    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    (fake_root / ".env").write_text("GATED_AGENT_PROBE=from_repo_root\n",
                                    encoding="utf-8")
    elsewhere = tmp_path / "system32"
    elsewhere.mkdir()

    monkeypatch.setattr(paths, "ROOT", fake_root)
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("GATED_AGENT_PROBE", "sentinel")
    monkeypatch.delenv("GATED_AGENT_PROBE")     # registers for rollback

    assert load_env() == {"GATED_AGENT_PROBE": "from_repo_root"}
    assert os.environ["GATED_AGENT_PROBE"] == "from_repo_root"


def test_load_env_missing_everywhere_is_still_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    assert load_env() == {}


def test_runtime_artifacts_all_share_one_anchor():
    """close_log / position state / mcp config / ledger must land in the same
    place regardless of who started the process, or the dashboard reads one
    file while the agent writes another."""
    from gated_agent import position_manager as pm
    for p in (pm.STATE, pm.LOG, pm.CONFIG_PATH, Ledger().path):
        assert p.is_absolute()
        assert paths.ROOT in p.parents


@pytest.mark.parametrize("name", ["run", "position_manager", "chain_fetcher",
                                  "ledger", "order_cli", "cli_executor",
                                  "redteam_mcp", "gates", "signals",
                                  "options_mapper", "negctl", "paths"])
def test_modules_import_with_an_empty_path(name, monkeypatch):
    """Import must not depend on finding any external binary."""
    import importlib
    monkeypatch.setenv("PATH", "")
    importlib.reload(importlib.import_module(f"gated_agent.{name}"))
