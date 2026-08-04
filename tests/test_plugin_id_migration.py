"""Tests for the plugin-id rename (`filament-fcm` → `filament`).

The id names the install directory, the entry in `plugins.enabled`, and the
argument to `hermes plugins update <id>`. An existing install has the old one in
both places, so install.sh replaces the directory and this migration rewrites the
config. Getting the config half wrong disables the platform on an upgrade, which
is silent — hence the tests.

`setup_cli` needs Hermes, so `migrate_enabled` is loaded from source: it is pure
and depends on nothing else in the module.
"""

import ast
import os
import re
import shutil
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SETUP_CLI = _ROOT / "hermes_filament_fcm" / "setup_cli.py"


_WANTED = (
    "PLUGIN_ID",
    "LEGACY_PLUGIN_ID",
    "migrate_enabled",
    "_find_hermes_home",
    "legacy_dir_is_ours",
    "running_from",
    "retire_legacy_plugin_dir",
)


def _load_from_setup_cli():
    """Exec just the ids and the pure migration helpers out of setup_cli.

    The module's own imports pull in Hermes, so the pieces under test are lifted
    out and given the few names they use.
    """
    tree = ast.parse(_SETUP_CLI.read_text())
    ns: dict = {
        "os": os,
        "shutil": shutil,
        "Path": Path,
        # running_from asks where it is executing from.
        "__file__": str(_SETUP_CLI),
        # Loud in the real thing, silent here.
        "print_info": lambda *a, **k: None,
        "print_warning": lambda *a, **k: None,
    }
    for node in tree.body:
        keep = (isinstance(node, ast.FunctionDef) and node.name in _WANTED) or (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id in _WANTED for t in node.targets)
        )
        if keep:
            exec(compile(ast.Module([node], []), str(_SETUP_CLI), "exec"), ns)
    return ns


_ns = _load_from_setup_cli()
migrate_enabled = _ns["migrate_enabled"]
legacy_dir_is_ours = _ns["legacy_dir_is_ours"]
retire_legacy_plugin_dir = _ns["retire_legacy_plugin_dir"]
running_from = _ns["running_from"]
PLUGIN_ID = _ns["PLUGIN_ID"]
LEGACY_PLUGIN_ID = _ns["LEGACY_PLUGIN_ID"]


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "plugins").mkdir()
    return tmp_path


def _make_legacy_install(home, *, ours=True):
    d = home / "plugins" / LEGACY_PLUGIN_ID
    d.mkdir()
    if ours:
        (d / "hermes_filament_fcm").mkdir()
        (d / "plugin.yaml").write_text(f"name: {LEGACY_PLUGIN_ID}\n")
    return d


# ── the ids agree across the three places that carry them ────────────


def test_manifest_name_matches_plugin_id():
    """plugin.yaml's name is what Hermes installs the directory as, so a drift
    here means install.sh writes one id and the loader looks for another."""
    manifest = (_ROOT / "plugin.yaml").read_text()
    assert re.search(rf"^name:\s*{PLUGIN_ID}\s*$", manifest, re.M)


def test_install_sh_agrees():
    installer = (_ROOT / "install.sh").read_text()
    assert f"PLUGIN_ID={PLUGIN_ID}\n" in installer
    assert f"LEGACY_PLUGIN_ID={LEGACY_PLUGIN_ID}\n" in installer


def test_install_sh_leaves_the_legacy_tree_to_setup():
    """install.sh must NOT remove it: the setup step it runs rewrites
    plugins.enabled first and removes the tree second, so a setup that never gets
    there still has the old id enabled and the old tree present to serve it.
    Removing it here would leave a failed setup with nothing loadable."""
    installer = (_ROOT / "install.sh").read_text()
    assert 'rm -rf "$LEGACY_PLUGIN_DIR"' not in installer


def test_state_directory_is_not_renamed():
    """The runtime state path is hardcoded, not derived from either name. That
    is what lets the rename leave FCM credentials, standing instructions and the
    wake policy in place — so it must keep saying filament-fcm."""
    pkg = _ROOT / "hermes_filament_fcm"
    for rel in ("credentials.py", "reactive.py"):
        assert 'filament-fcm"' in (pkg / rel).read_text()


# ── migrate_enabled ──────────────────────────────────────────────────


def test_legacy_id_is_replaced():
    assert migrate_enabled([LEGACY_PLUGIN_ID]) == [PLUGIN_ID]


def test_absent_id_is_added():
    assert migrate_enabled([]) == [PLUGIN_ID]


def test_already_migrated_is_unchanged():
    """Returning the input unchanged is what suppresses a pointless rewrite of
    config.yaml (and the "renamed" message) on a re-run."""
    assert migrate_enabled([PLUGIN_ID]) == [PLUGIN_ID]


def test_other_plugins_keep_their_position():
    assert migrate_enabled(["a", LEGACY_PLUGIN_ID, "b"]) == ["a", PLUGIN_ID, "b"]


def test_other_plugins_are_never_dropped():
    assert migrate_enabled(["memory", "chrome"]) == ["memory", "chrome", PLUGIN_ID]


def test_both_ids_listed_collapses_to_one():
    """A hand-edited config could carry both; enabling the same plugin twice is
    at best noise and at worst a double registration."""
    assert migrate_enabled([LEGACY_PLUGIN_ID, PLUGIN_ID]) == [PLUGIN_ID]
    assert migrate_enabled([PLUGIN_ID, LEGACY_PLUGIN_ID]) == [PLUGIN_ID]


def test_duplicates_are_collapsed():
    assert migrate_enabled([LEGACY_PLUGIN_ID, LEGACY_PLUGIN_ID]) == [PLUGIN_ID]


@pytest.mark.parametrize("bogus", ["filament-fcm-extra", "myfilament", "fcm"])
def test_similar_names_are_left_alone(bogus):
    """Only an exact match migrates — a substring rule would rename someone
    else's plugin."""
    assert bogus in migrate_enabled([bogus])


# ── retiring the legacy directory ────────────────────────────────────
#
# This deletes a directory, so the gate matters more than the happy path.


def test_retires_a_leftover_when_the_current_install_exists(hermes_home):
    (hermes_home / "plugins" / PLUGIN_ID / "hermes_filament_fcm").mkdir(parents=True)
    legacy = _make_legacy_install(hermes_home)
    assert retire_legacy_plugin_dir() is True
    assert not legacy.exists()


def test_moves_the_legacy_tree_when_it_is_the_only_install(hermes_home):
    """The reproduced bug: `hermes plugins update filament-fcm` leaves the new
    code in the legacy directory, and deleting it there removed the only plugin
    the agent had, enabling an id that pointed at nothing."""
    legacy = _make_legacy_install(hermes_home)
    (legacy / "marker").write_text("keep me")
    assert retire_legacy_plugin_dir() is True
    current = hermes_home / "plugins" / PLUGIN_ID
    assert (current / "hermes_filament_fcm").is_dir()
    assert (current / "marker").read_text() == "keep me"
    assert not legacy.exists()


def test_the_move_keeps_the_git_remote(hermes_home):
    """Moving rather than reinstalling is what leaves future `plugins update`
    working."""
    legacy = _make_legacy_install(hermes_home)
    (legacy / ".git").mkdir()
    retire_legacy_plugin_dir()
    assert (hermes_home / "plugins" / PLUGIN_ID / ".git").is_dir()


def test_never_deletes_the_tree_it_is_running_from(hermes_home, monkeypatch):
    """Belt to the move's braces: with a current install present the legacy tree
    is a leftover and gets removed — unless this code is executing out of it."""
    (hermes_home / "plugins" / PLUGIN_ID / "hermes_filament_fcm").mkdir(parents=True)
    legacy = _make_legacy_install(hermes_home)
    monkeypatch.setitem(_ns, "running_from", lambda p: True)
    assert retire_legacy_plugin_dir() is False
    assert legacy.exists()


def test_absent_legacy_dir_is_a_no_op(hermes_home):
    assert retire_legacy_plugin_dir() is False


def test_refuses_a_directory_that_is_not_ours(hermes_home):
    """Someone else's plugin, or a stray directory, at the legacy path. Deleting
    it would destroy work that was never ours."""
    stray = _make_legacy_install(hermes_home, ours=False)
    (stray / "somebody_elses_code.py").write_text("# not ours\n")
    assert retire_legacy_plugin_dir() is False
    assert stray.exists()


def test_refuses_a_symlink(hermes_home):
    """A symlinked plugin dir is how someone develops against a checkout;
    rmtree would refuse anyway, but not before deciding to try."""
    real = hermes_home / "checkout"
    (real / "hermes_filament_fcm").mkdir(parents=True)
    link = hermes_home / "plugins" / LEGACY_PLUGIN_ID
    link.symlink_to(real)
    assert legacy_dir_is_ours(link) is False
    assert retire_legacy_plugin_dir() is False
    assert real.exists()


def test_never_touches_the_current_plugin(hermes_home):
    """The new id lives at a different path; retiring must not reach it."""
    current = hermes_home / "plugins" / PLUGIN_ID
    (current / "hermes_filament_fcm").mkdir(parents=True)
    _make_legacy_install(hermes_home)
    retire_legacy_plugin_dir()
    assert current.exists()


def test_leaves_other_plugins_alone(hermes_home):
    other = hermes_home / "plugins" / "hello-world"
    other.mkdir()
    _make_legacy_install(hermes_home)
    retire_legacy_plugin_dir()
    assert other.exists()


def test_state_directory_survives(hermes_home):
    """The whole reason the rename is cheap: state lives outside the plugin
    tree, so retiring the tree must not take the agent's credentials with it."""
    state = hermes_home / ".hermes" / LEGACY_PLUGIN_ID
    state.mkdir(parents=True)
    (state / "fcm_credentials.json").write_text("{}")
    (state / "instructions.md").write_text("standing instructions")
    _make_legacy_install(hermes_home)
    retire_legacy_plugin_dir()
    assert (state / "fcm_credentials.json").exists()
    assert (state / "instructions.md").exists()


def test_is_idempotent(hermes_home):
    _make_legacy_install(hermes_home)
    assert retire_legacy_plugin_dir() is True
    assert retire_legacy_plugin_dir() is False


def test_running_from_detects_the_executing_tree():
    """The guard's own primitive: this module lives in the repo, not in tmp."""
    assert running_from(Path(__file__).resolve().parent.parent) is True
    assert running_from(Path("/nonexistent-elsewhere")) is False


def test_migrate_legacy_install_enables_before_touching_the_tree():
    """Config first. Either order closes the both-enabled window, but only this
    one is safe when the second step does not happen: a tree left behind is
    disabled and inert, whereas a tree removed before config is rewritten leaves
    nothing loadable."""
    src = _SETUP_CLI.read_text()
    body = src.split("def migrate_legacy_install")[1].split("def _enable_plugin")[0]
    assert body.index("_enable_plugin()") < body.index("retire_legacy_plugin_dir()")


def test_connect_validates_before_migrating():
    """Nothing mutating — least of all a directory move — before the token is
    known good."""
    src = _SETUP_CLI.read_text()
    body = src.split("def connect(")[1].split("def _restart_gateway")[0]
    assert body.index("_wait_for_finalization") < body.index("migrate_legacy_install()")


# ── no command anywhere still names the old id ────────────────────────


def test_no_source_file_tells_anyone_to_run_the_legacy_id():
    """A drift guard, because this has been missed three times.

    The id is spelled out by hand in several places — plugin.yaml, install.sh,
    setup_cli, and deps.py, which cannot import PLUGIN_ID because it is
    deliberately stdlib-only. Each one that lags tells an operator to run a
    command against a plugin that does not exist, and the platform stays down
    while they follow instructions that cannot work.

    tests/ is exempt: the migration tests name the legacy id on purpose when
    describing what an old install looks like.
    """
    pattern = re.compile(
        r"plugins\s+(?:update|enable|disable|install|remove)\s+" + LEGACY_PLUGIN_ID
    )
    offenders = []
    for path in _ROOT.rglob("*"):
        if path.suffix not in {".py", ".md", ".sh", ".yaml", ".yml"}:
            continue
        rel = path.relative_to(_ROOT)
        if rel.parts[0] in {"vendor", "tests", ".git"}:
            continue
        if pattern.search(path.read_text(errors="replace")):
            offenders.append(str(rel))
    assert not offenders, f"these still name the legacy plugin id: {offenders}"
