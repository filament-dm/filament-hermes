"""profile_seed: a default-named Filament profile takes the Hermes agent's name
and avatar; an owner-chosen one is never touched.

Loaded standalone (stdlib-only module, no Hermes)."""

import asyncio
import base64
import importlib.util
import sys
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent / "hermes_filament_fcm" / "profile_seed.py"
)
_spec = importlib.util.spec_from_file_location("profile_seed_standalone", _PATH)
ps = importlib.util.module_from_spec(_spec)
sys.modules["profile_seed_standalone"] = ps
_spec.loader.exec_module(ps)

_PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


# ── the server's default name ───────────────────────────────────────


def test_default_names_are_recognized():
    assert ps.is_default_agent_name("Test One's agent", "Test One")
    assert ps.is_default_agent_name("Test One's agent 13", "Test One")
    assert ps.is_default_agent_name("  test  one's AGENT 2 ", "Test One")
    assert ps.is_default_agent_name("My agent", None)
    assert ps.is_default_agent_name("My agent 3", "Test One")
    assert ps.is_default_agent_name(None, "Test One")
    assert ps.is_default_agent_name("", "Test One")


def test_owner_chosen_names_are_not_default():
    assert not ps.is_default_agent_name("Hermes", "Test One")
    assert not ps.is_default_agent_name("Test One's agent smith", "Test One")
    assert not ps.is_default_agent_name("Other's agent", "Test One")
    assert not ps.is_default_agent_name("Test One's agent", None)


# ── the name Hermes gives the agent ─────────────────────────────────

_STOCK_SOUL = (
    "You are Hermes Agent, built by Nous Research. Be direct: match the length "
    "of your reply to the weight of the ask."
)


def test_stock_hermes_is_named_hermes():
    assert ps.seed_name(
        profile_meta=None, soul_text=_STOCK_SOUL, profile_name=None
    ) == ("Hermes")
    assert (
        ps.seed_name(profile_meta=None, soul_text=None, profile_name="default")
        == "Hermes"
    )


def test_profile_display_name_wins():
    meta = {
        "display_name": " Research  Bot ",
        "ui_meta": {"hermes-bots": {"title": "X"}},
    }
    assert (
        ps.seed_name(profile_meta=meta, soul_text="You are Ada.", profile_name="r")
        == "Research Bot"
    )


def test_desktop_bot_title_next():
    meta = {"ui_meta": {"hermes-bots": {"title": "Scout"}}}
    assert ps.seed_name(profile_meta=meta, soul_text=None, profile_name=None) == "Scout"


def test_soul_persona_name():
    assert ps.soul_name("You are Ada, a careful assistant.") == "Ada"
    assert ps.soul_name("# Persona\n\nYou are Ada Lovelace. You love math.") == (
        "Ada Lovelace"
    )
    assert ps.soul_name("- **You are Jarvis** — dry wit.") == "Jarvis"
    assert ps.soul_name("You are Ada and you answer briefly.") == "Ada"
    assert ps.soul_name("You are named Rex.") == "Rex"


def test_soul_without_a_name():
    assert ps.soul_name(_STOCK_SOUL) is None
    assert ps.soul_name("You are a pirate. Talk like one.") is None
    assert ps.soul_name("You are an expert in Python.") is None
    assert ps.soul_name("Be terse.") is None
    # The scaffold's comment block mentions Hermes; it is no persona.
    assert ps.soul_name("<!--\nYou are Hermes.\n-->\n") is None
    assert ps.soul_name(None) is None


def test_named_profile_id_before_the_fallback():
    assert (
        ps.seed_name(profile_meta={}, soul_text=_STOCK_SOUL, profile_name="coder")
        == "coder"
    )
    assert (
        ps.seed_name(profile_meta={}, soul_text=None, profile_name="custom") == "Hermes"
    )


def test_a_profile_explicitly_named_hermes_stays_hermes():
    assert (
        ps.seed_name(
            profile_meta={"display_name": "Hermes Agent"},
            soul_text="You are Ada.",
            profile_name="coder",
        )
        == "Hermes"
    )


# ── the avatar ──────────────────────────────────────────────────────


def test_profile_avatar_before_the_hermes_icon(tmp_path):
    home, root = tmp_path / "home", tmp_path / "hermes-agent"
    icon = root / "apps" / "desktop" / "assets" / "icon.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(_PNG)
    assert ps.find_avatar(home, root) == icon
    (home / "assets").mkdir(parents=True)
    (home / "assets" / "avatar.webp").write_bytes(b"RIFF0000WEBP")
    assert ps.find_avatar(home, root) == home / "assets" / "avatar.webp"
    (home / "assets" / "avatar.png").write_bytes(_PNG)
    assert ps.find_avatar(home, root) == home / "assets" / "avatar.png"


def test_no_avatar(tmp_path):
    assert ps.find_avatar(tmp_path, None) is None
    assert ps.find_avatar(tmp_path, tmp_path / "missing") is None


def test_avatar_data_url(tmp_path):
    png = tmp_path / "a.png"
    png.write_bytes(_PNG)
    url = ps.avatar_data_url(png)
    assert url == "data:image/png;base64," + base64.b64encode(_PNG).decode()
    svg = tmp_path / "a.svg"
    svg.write_text("<svg/>")
    assert ps.avatar_data_url(svg) is None
    assert ps.avatar_data_url(tmp_path / "missing.png") is None


# ── seeding ─────────────────────────────────────────────────────────


class _Server:
    """set_profile as the server answers it: a taken name is refused inside an
    ok result; an owner-locked name is ignored and the profile returned."""

    def __init__(self, taken=(), locked=False, image_error=None, rpc_error=None):
        self.taken = {ps.normalize_name(n) for n in taken}
        self.locked = locked
        self.image_error = image_error
        self.rpc_error = rpc_error
        self.name = "Test One's agent 13"
        self.calls = []

    async def __call__(self, tool, args):
        self.calls.append((tool, dict(args)))
        assert tool == "set_profile"
        assert len(args) == 1, "name and image go in separate calls"
        if self.rpc_error:
            return None, self.rpc_error
        if "name" in args:
            if ps.normalize_name(args["name"]) in self.taken:
                return {
                    "error": "Another agent owned by your principal already uses "
                    "that name. Pick a different one."
                }, None
            if not self.locked:
                self.name = args["name"]
            return {"display_name": self.name}, None
        if self.image_error:
            return {"error": self.image_error}, None
        return {"display_name": self.name, "avatar_url": "mxc://x/y"}, None


def _seed(server, *, display_name="Test One's agent 13", wanted="Hermes", avatar=None):
    return asyncio.run(
        ps.seed_profile(
            server,
            display_name=display_name,
            owner_name="Test One",
            wanted_name=wanted,
            avatar=avatar,
        )
    )


def _png(tmp_path):
    path = tmp_path / "avatar.png"
    path.write_bytes(_PNG)
    return path


def test_seeds_name_and_avatar_separately(tmp_path):
    server = _Server()
    result = _seed(server, avatar=_png(tmp_path))
    assert result == ps.SeedResult(name="Hermes", image=True)
    assert [list(args) for _, args in server.calls] == [["name"], ["image"]]
    assert server.calls[1][1]["image"].startswith("data:image/png;base64,")


def test_taken_name_is_numbered():
    server = _Server(taken=["Hermes", "Hermes 2"])
    assert _seed(server).name == "Hermes 3"
    assert [a["name"] for _, a in server.calls] == ["Hermes", "Hermes 2", "Hermes 3"]


def test_gives_up_after_five_names_but_still_sets_the_avatar(tmp_path):
    server = _Server(taken=["Hermes"] + [f"Hermes {n}" for n in range(2, 6)])
    result = _seed(server, avatar=_png(tmp_path))
    assert result == ps.SeedResult(name=None, image=True)
    assert len([c for c in server.calls if "name" in c[1]]) == ps.NAME_ATTEMPTS


def test_other_refusals_stop_without_numbering():
    class _Refuses(_Server):
        async def __call__(self, tool, args):
            self.calls.append((tool, dict(args)))
            return {"error": "Name must be at most 64 characters."}, None

    server = _Refuses()
    assert _seed(server) is None
    assert len(server.calls) == 1


def test_owner_locked_name_is_not_reported_as_set():
    server = _Server(locked=True)
    assert _seed(server) is None


def test_owner_chosen_name_is_left_alone(tmp_path):
    server = _Server()
    assert _seed(server, display_name="Carl", avatar=_png(tmp_path)) is None
    assert server.calls == []


def test_rejected_image_and_rpc_errors_are_not_success(tmp_path):
    assert (
        _seed(_Server(image_error="bad image"), wanted=None, avatar=_png(tmp_path))
        is None
    )
    assert _seed(_Server(rpc_error="HTTP 500"), avatar=_png(tmp_path)) is None


def test_is_error_envelope_is_a_refusal():
    envelope = {"result": {"isError": True, "content": [{"text": "nope"}]}}
    assert ps.set_profile_error(envelope, None) == "the server refused it"
    assert ps.set_profile_error({"display_name": "x"}, None) is None


def test_never_raises():
    async def boom(tool, args):
        raise RuntimeError("network down")

    assert _seed(boom) is None
