"""``/fil-upgrade``: pull the plugin tree, restart into it, say what happened.

The command is deterministic - parsed and executed without a model turn,
like the rest of the ``/fil-`` surface. What these pin is the part that has
to survive the process ending: nothing is announced by the process that
upgrades, because succeeding means it stops existing, so the marker written
before the restart is what lets the next process report the result.

Parsing is exercised against the real ``slash.parse``; the adapter half is
source-level, since importing it needs Hermes.
"""

import ast
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"
_ADAPTER = _PKG / "adapter.py"


def _load(name):
    """Load one module under a stub package - importing the real package
    pulls in Hermes, but these modules use relative imports, so they need a
    parent to resolve against (same pattern as test_server_guide_lifetime)."""
    pkg_name = "hermes_filament_fcm"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(_PKG)]
        sys.modules[pkg_name] = pkg
    full = f"{pkg_name}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


slash = _load("slash")
credentials = _load("credentials")
self_update = _load("self_update")

CredentialStore = credentials.CredentialStore
PendingUpgradeStore = self_update.PendingUpgradeStore
build_complete_notice = self_update.build_complete_notice
build_failure_notice = self_update.build_failure_notice
build_start_notice = self_update.build_start_notice
version_on_disk = self_update.version_on_disk


class TestParsing:
    def test_bare_command_is_an_upgrade_request(self):
        assert isinstance(
            slash.parse("/fil-upgrade", channels=[]), slash.UpgradeRequest
        )

    def test_case_and_spacing_do_not_matter(self):
        result = slash.parse("  /FIL-UPGRADE  ", channels=[])
        assert isinstance(result, slash.UpgradeRequest)

    def test_arguments_get_help_rather_than_an_upgrade(self):
        # "/fil-upgrade to 0.9" reads like it pins a version. Answering with
        # help is cheaper than upgrading to something nobody asked for.
        result = slash.parse("/fil-upgrade to 0.9", channels=[])
        assert isinstance(result, slash.HelpRequest)
        assert result.command == "upgrade"

    def test_it_is_listed_in_the_index(self):
        assert "/fil-upgrade" in slash.help_index()

    def test_a_typo_still_resolves(self):
        # The whole surface is fuzzy-matched; upgrade should be no different.
        assert isinstance(slash.parse("/fil-upgrad", channels=[]), slash.UpgradeRequest)


class TestFeatureFlagExemption:
    """The slash surface is off by default and turns on through set_feature,
    which needs a model turn on a build new enough to have that tool. An
    agent too old to be asked is the agent upgrade exists for."""

    def test_upgrade_runs_with_the_flag_off(self):
        assert slash.is_always_on("/fil-upgrade") is True

    def test_help_runs_with_the_flag_off_too(self):
        # It is what tells the principal the surface exists, and the
        # first-contact hello points at it - inert by default makes that
        # hello a lie.
        assert slash.is_always_on("/fil-help") is True

    def test_the_config_surface_does_not(self):
        assert slash.is_always_on("/fil-config #welcome post off") is False

    def test_a_fuzzy_spelling_is_exempt_too(self):
        # parse() accepts /fil-upgrad, so a stricter gate here would send
        # exactly that spelling to the model while the exact one ran
        # deterministically.
        assert slash.is_always_on("/fil-upgrad") is True
        assert isinstance(slash.parse("/fil-upgrad", channels=[]), slash.UpgradeRequest)

    def test_the_exemption_is_gated_on_the_owner(self):
        # /fil-upgrade pulls code and restarts the gateway. The backchannel
        # is the control plane by room, not by speaker, so a guest must not
        # inherit a deterministic command that cannot decline.
        body = _adapter_body("_handle_control_message")
        assert "slash.is_always_on(slash_body) and is_owner" in body
        assert body.index("is_owner =") < body.index("slash.is_always_on")

    def test_a_foreign_slash_is_not_ours_to_exempt(self):
        assert slash.is_always_on("/restart") is False

    def test_the_adapter_consults_the_exemption(self):
        source = _ADAPTER.read_text()
        assert "slash.is_always_on(slash_body)" in source


class TestPendingMarker:
    def test_round_trips_through_the_state_dir(self, tmp_path):
        store = PendingUpgradeStore(CredentialStore(tmp_path))
        store.save("0.11.0", "!cc:server")
        loaded = store.load()
        assert loaded["target_version"] == "0.11.0"
        assert loaded["room_id"] == "!cc:server"

    def test_clearing_leaves_nothing_to_re_announce(self, tmp_path):
        store = PendingUpgradeStore(CredentialStore(tmp_path))
        store.save("0.11.0", "!cc:server")
        store.clear()
        assert store.load() is None

    def test_a_corrupt_marker_reads_as_nothing_pending(self, tmp_path):
        # Anything else raises on every connect, forever.
        (tmp_path / "pending_upgrade.json").write_text(json.dumps(["not", "a", "dict"]))
        assert PendingUpgradeStore(CredentialStore(tmp_path)).load() is None


def _adapter_body(name: str) -> str:
    """One adapter method's source - importing the adapter needs Hermes."""
    source = _ADAPTER.read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} not found in adapter.py")


class TestAdapterOrder:
    """Source-level, like tests/test_server_guide_lifetime.py: importing the
    adapter pulls in Hermes. What matters here is ordering, and ordering is
    readable from the source."""

    def _body(self, name: str) -> str:
        tree = ast.parse(_ADAPTER.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                return ast.get_source_segment(_ADAPTER.read_text(), node) or ""
        raise AssertionError(f"{name} not found in adapter.py")

    def test_the_marker_is_written_before_the_restart(self):
        # After the restart there is no "we" left to write it.
        body = self._body("_run_upgrade")
        assert body.index("_pending_upgrade.save") < body.index(
            "request_gateway_restart"
        )

    def test_a_refused_restart_clears_the_marker(self):
        body = self._body("_run_upgrade")
        assert "_pending_upgrade.clear()" in body

    def test_nothing_restarts_when_the_pull_changed_nothing(self):
        # Already current, or a checkout tracking something other than main:
        # restarting would cost the agent a reconnect and change nothing.
        body = self._body("_run_upgrade")
        assert body.index("version_on_disk") < body.index("request_gateway_restart")

    def test_the_marker_is_cleared_before_the_result_is_posted(self):
        # Cleared whatever the outcome: a marker that survived its restart
        # re-announces on every start.
        body = self._body("_announce_completed_upgrade")
        assert body.index("_pending_upgrade.clear()") < body.index("_post_backchannel")


class TestDirtyTree:
    """``--ff-only`` is not the guard it reads like: git refuses only when the
    incoming commits touch a locally modified file. Edits to any other file
    fast-forward, and the gateway restarts into pulled code plus local edits."""

    def _repo(self, tmp_path):
        def git(*args):
            subprocess.run(
                ["git", *args], cwd=tmp_path, check=True, capture_output=True
            )

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        (tmp_path / "pyproject.toml").write_text('version = "0.1.0"\n')
        git("add", "-A")
        git("commit", "-qm", "init")
        return tmp_path

    def test_a_clean_tree_is_clean(self, tmp_path):
        ok, why = self_update.working_tree_is_clean(self._repo(tmp_path))
        assert ok is True
        assert why == ""

    def test_an_edit_to_an_untouched_file_still_blocks(self, tmp_path):
        # The case --ff-only lets through.
        repo = self._repo(tmp_path)
        (repo / "SOUL.md").write_text("local edit")
        ok, why = self_update.working_tree_is_clean(repo)
        assert ok is False
        assert "uncommitted changes" in why
        assert "SOUL.md" in why

    def test_a_modified_tracked_file_blocks(self, tmp_path):
        repo = self._repo(tmp_path)
        (repo / "pyproject.toml").write_text('version = "9.9.9"\n')
        ok, _why = self_update.working_tree_is_clean(repo)
        assert ok is False


class TestMarkerDurability:
    def test_save_reports_success(self, tmp_path):
        store = PendingUpgradeStore(CredentialStore(tmp_path))
        assert store.save("0.11.0", "!cc:server") is True

    def test_save_reports_a_marker_that_did_not_land(self, tmp_path, monkeypatch):
        # _write_json logs filesystem errors and returns normally, so reading
        # back is the only way to know. Restarting without a durable marker
        # leaves the principal with the start notice and then silence.
        store = PendingUpgradeStore(CredentialStore(tmp_path))
        monkeypatch.setattr(store._store, "save_pending_upgrade", lambda data: None)
        assert store.save("0.11.0", "!cc:server") is False

    def test_the_tree_is_checked_before_anything_is_pulled(self):
        body = _adapter_body("_run_upgrade")
        assert "working_tree_is_clean" in body
        assert body.index("working_tree_is_clean") < body.index("git_pull")

    def test_the_upgrade_aborts_when_the_marker_is_not_durable(self):
        body = _adapter_body("_run_upgrade")
        assert "if not self._pending_upgrade.save(" in body


class TestCorruptMarker:
    def test_a_non_string_target_does_not_reach_is_newer(self, tmp_path):
        # {"target_version": 1} reaches is_newer, which calls .strip() on it
        # and raises out of the connect path: every reconnect would fail
        # while the file sat on disk.
        body = _adapter_body("_announce_completed_upgrade")
        assert "isinstance(target, str)" in body
        assert body.index("isinstance(target, str)") < body.index("is_newer(target")

    def test_is_newer_really_raises_on_a_non_string(self):
        version = _load("_version")
        with pytest.raises(AttributeError):
            version.is_newer(1, "0.10.1")


class TestPostResult:
    def test_a_returned_error_envelope_counts_as_failure(self):
        # call_tool reports a server-side rejection as {"error": ...} rather
        # than raising, so ignoring the result counts it as delivered.
        body = _adapter_body("_post_backchannel")
        assert 'result.get("error")' in body
        assert "return False" in body


class TestTheAgentKnowsTheSurfaceExists:
    """A /fil- command is answered before any turn, so the model never sees
    one and has no evidence the surface exists. Asked "can you upgrade
    yourself", it guesses - and the observed guess was "that's not a command
    I run", about the command that upgrades it."""

    def _prompt(self, slash_enabled: bool = False) -> str:
        framing = _load("framing")
        return framing.command_map_prompt(slash_enabled)

    def test_it_names_the_upgrade_command(self):
        assert "/fil-upgrade" in self._prompt()

    def test_it_names_the_restart_command(self):
        assert "/restart" in self._prompt()

    def test_it_says_upgrade_is_one_step(self):
        # Observed live: the agent advised /fil-upgrade "then /restart".
        # /fil-upgrade already restarts, so that is a second needless bounce
        # at the moment an agent is least robust.
        prompt = self._prompt()
        assert "one step" in prompt
        assert "restarts you" in prompt

    def test_it_says_the_terminal_is_not_the_route(self):
        # The terminal is blocked from inside the gateway, and an agent that
        # tries anyway either refuses confusingly or claims a restart it did
        # not perform.
        assert "terminal" in self._prompt()

    def test_the_control_turn_carries_it(self):
        body = _adapter_body("_handle_control_message")
        assert "command_map_prompt" in body

    def test_a_handed_over_command_does_not_carry_it(self):
        # Nothing about a gateway command reaches a model turn, so there is
        # no prompt to attach.
        body = _adapter_body("_handle_control_message")
        assert body.index("if not gateway_command:") < body.index("command_map_prompt")

    def test_it_offers_config_only_when_config_works(self):
        # /fil-config is inert while the surface is off; naming it would have
        # the agent recommend a command that does nothing.
        assert "/fil-config" in self._prompt(slash_enabled=True)
        assert "/fil-config" not in self._prompt(slash_enabled=False)


class TestFailureMessages:
    """What the principal reads when an upgrade cannot run. Observed live: a
    failed pull printed several hundred "[new branch]" lines into the
    backchannel with the real error buried at the bottom, under the heading
    "Couldn't upgrade to vthe latest version"."""

    def test_no_version_is_named_when_none_is_known(self):
        # Nothing has been pulled yet, so there is no version to name.
        notice = build_failure_notice(None, "no repository to pull")
        assert "vthe latest" not in notice
        assert "Couldn't upgrade:" in notice

    def test_a_known_version_is_still_named(self):
        assert "v0.11.0" in build_failure_notice("0.11.0", "something broke")

    def test_fetch_chatter_is_stripped(self):
        noisy = "\n".join(
            ["From https://github.com/filament-dm/filament-hermes"]
            + [f" * [new branch] b{i} -> origin/b{i}" for i in range(400)]
            + ["You are not currently on a branch.", "See git-pull(1) for details."]
        )
        summary = self_update.git_error_summary(noisy)
        assert "[new branch]" not in summary
        assert "not currently on a branch" in summary

    def test_the_reason_is_capped(self):
        assert len(self_update.git_error_summary("x" * 5000)) <= 300

    def test_a_failure_notice_stays_readable(self):
        noisy = "\n".join(f" * [new branch] b{i} -> origin/b{i}" for i in range(400))
        notice = build_failure_notice(None, noisy + "\nfatal: refusing to merge")
        assert len(notice) < 600
        assert "refusing to merge" in notice


class TestDetachedHead:
    """FILAMENT_FCM_REF accepts a commit as well as a branch, and installing
    at a commit leaves a detached HEAD where git pull cannot work at all."""

    def _repo(self, tmp_path, detach: bool):
        def git(*a):
            subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        (tmp_path / "pyproject.toml").write_text('version = "0.1.0"\n')
        git("add", "-A")
        git("commit", "-qm", "init")
        if detach:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=tmp_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            git("checkout", "-q", head)
        return tmp_path

    def test_a_branch_checkout_can_pull(self, tmp_path):
        ok, _why = self_update.on_a_tracking_branch(self._repo(tmp_path, False))
        assert ok is True

    def test_a_detached_head_is_named_not_attempted(self, tmp_path):
        ok, why = self_update.on_a_tracking_branch(self._repo(tmp_path, True))
        assert ok is False
        assert "fixed commit" in why

    def test_the_upgrade_checks_it_before_pulling(self):
        body = _adapter_body("_run_upgrade")
        assert "on_a_tracking_branch" in body
        assert body.index("on_a_tracking_branch") < body.index("git_pull")


class TestWhereTheCommandsAreNamed:
    """The commands are handled before any turn, so the model never sees one
    and cannot be relied on to describe them. The two messages every
    principal reads - the first hello, and the update alert - state them
    directly instead."""

    def test_the_first_hello_names_them(self):
        framing = _load("framing")
        for enabled in (True, False):
            line = framing.command_summary(enabled)
            assert "/fil-upgrade" in line
            assert "/fil-help" in line

    def test_the_hello_offers_config_only_when_it_works(self):
        framing = _load("framing")
        assert "/fil-config" in framing.command_summary(True)
        assert "/fil-config" not in framing.command_summary(False)

    def test_the_hello_asks_the_flag(self):
        # The hello text lives in framing.first_contact_hello, which ends
        # with command_summary; the adapter's job is to ask the flag and
        # pass it through.
        body = _adapter_body("_maybe_greet")
        assert "first_contact_hello" in body
        assert "FEATURE_SLASH_COMMANDS" in body
        framing = _load("framing")
        assert "/fil-upgrade" in framing.first_contact_hello("A", False)

    def test_the_update_alert_offers_the_command(self):
        update_check = _load("update_check")
        note = update_check.build_reminder("0.2.0", "0.1.0")
        assert "/fil-upgrade" in note

    def test_the_update_alert_does_not_send_them_to_a_shell(self):
        update_check = _load("update_check")
        note = update_check.build_reminder("0.2.0", "0.1.0")
        assert "gateway restart" not in note


class TestNotices:
    def test_start_names_the_version_it_is_going_to(self):
        assert "0.11.0" in build_start_notice("0.11.0")

    def test_completion_names_the_version_actually_running(self):
        assert "0.11.0" in build_complete_notice("0.11.0")

    def test_failure_offers_a_route_that_works(self):
        # Never `hermes gateway restart`: pasted into a terminal it restarts
        # the gateway in that terminal's foreground, which is not what the
        # instruction reads like it does.
        notice = build_failure_notice("0.11.0", "git pull failed")
        assert "git pull failed" in notice
        assert "gateway restart" not in notice


def test_version_on_disk_reads_the_tree_not_the_import():
    # The point of reading from disk: after a pull these differ, and the
    # difference is what says the pull landed new code.
    assert version_on_disk() is not None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
