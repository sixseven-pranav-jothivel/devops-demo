"""Engine mechanics: idempotence, switching, refusal on edited files, byte-exact reset."""

from __future__ import annotations

import ast
import fcntl
import hashlib
import json
import threading
from pathlib import Path

import pytest

from demo import cli
from demo.engine import (
    EXIT_REFUSED,
    MARKER_PREFIX,
    DemoError,
    Engine,
    RefusalError,
    atomic_write,
    load_config,
    sha256_file,
    tree_digest,
)

pytestmark = pytest.mark.demo

ENTRIES = ["app", "pyproject.toml", "uv.lock", "Dockerfile"]
REPO = Path(__file__).resolve().parents[2]


def available_scenarios(repo: Path) -> list[str]:
    """Scenario ids whose target files exist in the copy (bad_container needs a Dockerfile)."""
    eng = Engine(repo)
    return [
        s.id
        for s in eng.config.scenarios
        if s.patch and all((repo / f).exists() or _is_created(s, f) for f in s.files)
    ]


def _is_created(scenario: object, rel: str) -> bool:
    patch = getattr(scenario, "patch", ())
    return any(op.kind == "create_file" and op.file == rel for op in patch)


def scenario_ids() -> list[str]:
    return [s.id for s in load_config(REPO).scenarios if s.patch]


def digest(root: Path) -> str:
    return tree_digest(root, [e for e in ENTRIES if (root / e).exists()])


def hashes(root: Path, engine: Engine, scenario_id: str) -> dict[str, str | None]:
    return {rel: sha256_file(root / rel) for rel in engine.config.get(scenario_id).files}


@pytest.fixture(params=scenario_ids())
def scenario_id(request: pytest.FixtureRequest, repo: Path) -> str:
    sid: str = request.param
    if sid not in available_scenarios(repo):
        pytest.skip(f"{sid}: target files are not present yet (e.g. no Dockerfile)")
    return sid


def test_inject_changes_only_the_declared_files_and_reset_is_byte_exact(
    scenario_id: str, repo: Path, engine: Engine
) -> None:
    scenario = engine.config.get(scenario_id)
    before = digest(repo)
    engine.inject(scenario_id, allow_runtime=True)
    assert digest(repo) != before
    for rel in scenario.files:
        text = (repo / rel).read_text("utf-8")
        assert MARKER_PREFIX + scenario_id in text
        if rel.endswith(".py"):
            ast.parse(text)  # an injected Python file must still be syntactically valid
    assert engine.status()["active"] == scenario_id
    engine.reset()
    assert digest(repo) == before
    assert engine.read_state() is None
    assert not (repo / ".demo-state" / "backup" / scenario_id).exists()


def test_inject_is_idempotent(scenario_id: str, repo: Path, engine: Engine) -> None:
    engine.inject(scenario_id, allow_runtime=True)
    once = digest(repo)
    state_once = engine.read_state()
    report = engine.inject(scenario_id, allow_runtime=True)
    assert "already active" in report.messages[0]
    assert digest(repo) == once
    assert engine.read_state() == state_once


def test_reset_without_state_is_a_successful_noop(repo: Path, engine: Engine) -> None:
    before = digest(repo)
    report = engine.reset()
    assert "nothing to reset" in report.messages[0]
    assert digest(repo) == before


def test_switching_scenarios_restores_the_baseline_first(repo: Path, engine: Engine) -> None:
    before = digest(repo)
    engine.inject("bad_lint")
    engine.inject("bad_unit_test")
    assert engine.status()["active"] == "bad_unit_test"
    assert MARKER_PREFIX not in (repo / "app/services/orders.py").read_text("utf-8")
    assert "bad_unit_test" in (repo / "app/services/pricing.py").read_text("utf-8")
    engine.inject("clean")
    assert digest(repo) == before
    assert engine.read_state() is None


def test_scenarios_that_share_a_file_do_not_stack(repo: Path, engine: Engine) -> None:
    """bad_lint and bad_integration_test both patch orders.py; switching must not mix them."""
    baseline = (repo / "app/services/orders.py").read_bytes()
    engine.inject("bad_lint")
    engine.inject("bad_integration_test")
    text = (repo / "app/services/orders.py").read_text("utf-8")
    assert "import os" not in text
    assert "bad_integration_test" in text
    engine.reset()
    assert (repo / "app/services/orders.py").read_bytes() == baseline


def test_reset_refuses_when_a_defect_file_was_edited(repo: Path, engine: Engine) -> None:
    engine.inject("bad_lint")
    target = repo / "app/services/orders.py"
    edited = target.read_text("utf-8") + "\n# my own change\n"
    target.write_text(edited, encoding="utf-8")
    with pytest.raises(RefusalError) as excinfo:
        engine.reset()
    assert excinfo.value.code == EXIT_REFUSED
    assert "app/services/orders.py" in str(excinfo.value)
    assert "--force" in str(excinfo.value)
    assert target.read_text("utf-8") == edited  # nothing was touched
    assert engine.read_state() is not None


def test_reset_force_restores_but_keeps_a_copy_of_the_edit(repo: Path, engine: Engine) -> None:
    baseline = (repo / "app/services/orders.py").read_bytes()
    engine.inject("bad_lint")
    target = repo / "app/services/orders.py"
    target.write_text("# my work\n", encoding="utf-8")
    engine.reset(force=True)
    assert target.read_bytes() == baseline
    saved = list((repo / ".demo-state" / "discarded").rglob("orders.py"))
    assert len(saved) == 1
    assert saved[0].read_text("utf-8") == "# my work\n"


def test_inject_refuses_to_switch_over_user_edits(repo: Path, engine: Engine) -> None:
    engine.inject("bad_lint")
    target = repo / "app/services/orders.py"
    target.write_text("# my work\n", encoding="utf-8")
    with pytest.raises(RefusalError):
        engine.inject("bad_format")
    assert target.read_text("utf-8") == "# my work\n"
    assert engine.status()["active"] == "bad_lint"


def test_reinjecting_the_active_scenario_over_an_edit_is_refused(
    repo: Path, engine: Engine
) -> None:
    engine.inject("bad_lint")
    target = repo / "app/services/orders.py"
    target.write_text(target.read_text("utf-8") + "# edit\n", encoding="utf-8")
    with pytest.raises(RefusalError):
        engine.inject("bad_lint")


def test_user_edit_before_inject_makes_the_old_text_check_fail_loudly(
    repo: Path, engine: Engine
) -> None:
    target = repo / "app/services/pricing.py"
    target.write_text(
        target.read_text("utf-8").replace("return subtotal - discount + tax", "return 0"),
        encoding="utf-8",
    )
    with pytest.raises(DemoError, match="exactly once, found 0"):
        engine.inject("bad_unit_test")
    assert engine.read_state() is None  # nothing half-applied


def test_orphan_markers_without_state_are_refused(repo: Path, engine: Engine) -> None:
    target = repo / "app/services/orders.py"
    target.write_text("# DEMO-DEFECT:bad_lint\n" + target.read_text("utf-8"), encoding="utf-8")
    with pytest.raises(RefusalError, match="not at baseline"):
        engine.inject("bad_lint")


def test_create_file_refuses_to_overwrite_an_existing_different_file(
    repo: Path, engine: Engine
) -> None:
    existing = repo / "app/services/legacy_import.py"
    existing.write_text("# someone's real module\n", encoding="utf-8")
    with pytest.raises(RefusalError, match="already exists"):
        engine.inject("bad_coverage")
    assert existing.read_text("utf-8") == "# someone's real module\n"


def test_create_file_force_overwrites_but_reset_restores_the_original(
    repo: Path, engine: Engine
) -> None:
    existing = repo / "app/services/legacy_import.py"
    existing.write_text("# someone's real module\n", encoding="utf-8")
    engine.inject("bad_coverage", force=True)
    assert "DEMO-DEFECT:bad_coverage" in existing.read_text("utf-8")
    engine.reset()
    assert existing.read_text("utf-8") == "# someone's real module\n"


def test_reset_deletes_created_files_and_empty_directories(repo: Path, engine: Engine) -> None:
    engine.inject("bad_coverage")
    created = repo / "app/services/legacy_import.py"
    assert created.is_file()
    engine.reset()
    assert not created.exists()


def test_interrupted_injection_is_recovered_by_reset(repo: Path, engine: Engine) -> None:
    """Simulate a crash after the state file was written but before every file was replaced."""
    before = digest(repo)
    scenario = engine.config.get("bad_dependency")
    contents = engine.compute(scenario)
    files = {}
    for rel, data in contents.items():
        original = (repo / rel).read_bytes()
        backup = repo / ".demo-state" / "backup" / scenario.id / rel
        atomic_write(backup, original)
        files[rel] = {
            "existed": True,
            "before_sha256": hashlib.sha256(original).hexdigest(),
            "after_sha256": hashlib.sha256(data).hexdigest(),
            "mode": 0o644,
        }
    state = {"version": 1, "scenario": scenario.id, "status": "applying", "files": files}
    (repo / ".demo-state" / "state.json").write_text(json.dumps(state), encoding="utf-8")
    atomic_write(
        repo / "pyproject.toml", contents["pyproject.toml"]
    )  # only one of two files written
    engine.reset()
    assert digest(repo) == before


def test_a_failed_patch_leaves_no_state_and_no_changes(repo: Path, engine: Engine) -> None:
    before = digest(repo)
    pricing = repo / "app/services/pricing.py"
    pricing.write_text(
        pricing.read_text("utf-8") + "\n    return subtotal - discount + tax\n", encoding="utf-8"
    )
    broken = digest(repo)
    assert broken != before
    # make the old text occur twice: inject must abort before touching anything
    with pytest.raises(DemoError, match="exactly once, found 2"):
        engine.inject("bad_unit_test")
    assert digest(repo) == broken
    assert engine.read_state() is None


def test_corrupt_backup_aborts_the_restore_instead_of_writing_garbage(
    repo: Path, engine: Engine
) -> None:
    engine.inject("bad_lint")
    backup = repo / ".demo-state" / "backup" / "bad_lint" / "app/services/orders.py"
    backup.write_text("garbage", encoding="utf-8")
    with pytest.raises(DemoError, match="sha256"):
        engine.reset()


def test_runtime_scenarios_are_refused_unless_acknowledged(repo: Path, engine: Engine) -> None:
    before = digest(repo)
    with pytest.raises(RefusalError, match="runtime failures are disabled"):
        engine.inject("slow_api")
    assert digest(repo) == before
    engine.inject("slow_api", allow_runtime=True)
    assert "slow_api" in (repo / "app/api/products.py").read_text("utf-8")


def test_unknown_scenario_lists_the_known_ones(engine: Engine) -> None:
    with pytest.raises(DemoError, match=r"Known scenarios: .*bad_lint"):
        engine.inject("nope")


def test_status_reports_modified_files(repo: Path, engine: Engine) -> None:
    engine.inject("bad_lint")
    assert engine.status()["files"] == {"app/services/orders.py": "injected"}
    (repo / "app/services/orders.py").write_text("x = 1\n", encoding="utf-8")
    assert engine.status()["files"] == {"app/services/orders.py": "MODIFIED"}


def test_cli_exit_codes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--root", str(repo), "list"]) == 0
    assert cli.main(["--root", str(repo), "inject", "bad_lint"]) == 0
    (repo / "app/services/orders.py").write_text("x = 1\n", encoding="utf-8")
    assert cli.main(["--root", str(repo), "reset"]) == EXIT_REFUSED
    assert "edited after the defect was injected" in capsys.readouterr().err
    assert cli.main(["--root", str(repo), "reset", "--force"]) == 0
    assert cli.main(["--root", str(repo), "inject", "nope"]) == 1
    assert cli.main(["--root", str(repo), "show", "bad_dependency"]) == 0
    assert "reproduce" in capsys.readouterr().out


def test_file_modes_are_preserved_and_created_files_are_world_readable(
    repo: Path, engine: Engine
) -> None:
    target = repo / "app/services/orders.py"
    target.chmod(0o640)
    engine.inject("bad_lint")
    assert target.stat().st_mode & 0o777 == 0o640
    engine.reset()
    assert target.stat().st_mode & 0o777 == 0o640
    engine.inject("bad_coverage")
    assert (repo / "app/services/legacy_import.py").stat().st_mode & 0o777 == 0o644


def test_reset_leaves_no_demo_state_directory(repo: Path, engine: Engine) -> None:
    engine.inject("bad_lint")
    assert (repo / ".demo-state" / "state.json").exists()
    engine.reset()
    assert not (repo / ".demo-state").exists()
    assert engine.status()["active"] is None


def test_noop_reset_and_refused_inject_leave_no_demo_state_directory(
    repo: Path, engine: Engine
) -> None:
    engine.reset()
    assert not (repo / ".demo-state").exists()
    existing = repo / "app/services/legacy_import.py"
    existing.write_text("# real\n", encoding="utf-8")
    with pytest.raises(RefusalError):
        engine.inject("bad_coverage")
    assert not (repo / ".demo-state").exists()


def test_failed_inject_leaves_no_demo_state_directory(repo: Path, engine: Engine) -> None:
    target = repo / "app/services/pricing.py"
    target.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(DemoError):
        engine.inject("bad_unit_test")
    assert not (repo / ".demo-state").exists()


def test_forced_reset_keeps_discarded_edits_and_nothing_else(repo: Path, engine: Engine) -> None:
    engine.inject("bad_lint")
    (repo / "app/services/orders.py").write_text("# my work\n", encoding="utf-8")
    engine.reset(force=True)
    state_dir = repo / ".demo-state"
    assert sorted(p.name for p in state_dir.iterdir()) == ["discarded"]
    kept = list((state_dir / "discarded").rglob("orders.py"))
    assert len(kept) == 1
    assert kept[0].read_text("utf-8") == "# my work\n"
    assert engine.status()["active"] is None


def test_stale_lock_file_from_a_crashed_run_does_not_block(repo: Path, engine: Engine) -> None:
    state_dir = repo / ".demo-state"
    state_dir.mkdir()
    (state_dir / "lock").write_text("", encoding="utf-8")  # left behind, no process holds the flock
    engine.inject("bad_lint")
    engine.reset()
    assert not state_dir.exists()


def test_a_held_lock_is_honoured_until_released(repo: Path, engine: Engine) -> None:
    state_dir = repo / ".demo-state"
    state_dir.mkdir()
    holder = (state_dir / "lock").open("w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    done = threading.Event()

    def worker() -> None:
        engine.inject("bad_lint")
        done.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert not done.wait(0.5)  # blocked while another process holds the lock
    holder.close()  # release (as the kernel does when a process dies)
    thread.join(timeout=20)
    assert done.is_set()
    assert engine.status()["active"] == "bad_lint"
