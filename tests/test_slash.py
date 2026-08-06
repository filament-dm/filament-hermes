"""Tests for the deterministic backchannel slash-command layer.

``slash.py`` is pure-stdlib and side-effect-free, so it is loaded standalone
(importing the package would pull in Hermes). ``reactive.py`` is loaded the
same way to pin the vocabularies the two modules deliberately restate.

The parser is the product surface: the resolver tests below (typos, token
order, fillers, ambiguity) are the behavior the principal actually types
against, so they are deliberately heavy.
"""

import importlib.util
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "hermes_filament_fcm"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


slash = _load("slash")
reactive = _load("reactive")


CHANNELS = [("!welcome:fil", "welcome"), ("!general:fil", "general")]
MCP = ["linear", "gcal"]


def parse(body, channels=CHANNELS, mcp_servers=MCP, **kwargs):
    return slash.parse(body, channels=channels, mcp_servers=mcp_servers, **kwargs)


# ── Vocabulary sync with reactive.py ─────────────────────────────────
# slash.py restates these so it stays standalone-loadable; these tests are
# what keeps the restatement honest.


def test_rows_match_reactive_default_capabilities():
    assert list(slash.ROWS) == list(reactive.DEFAULT_CAPABILITIES)
    for row in slash.ROWS:
        assert row in reactive.BUILTIN_BUNDLES


def test_aliases_match_reactive_builtin_bundles():
    for alias in slash.DEPRECATED_ALIASES:
        assert alias in reactive.BUILTIN_BUNDLES


def test_constants_match_reactive():
    assert slash.MCP_PREFIX == reactive.MCP_BUNDLE_PREFIX
    assert (
        slash.FEATURE_ADVANCED_TOOL_CONTROLS
        == reactive.FEATURE_ADVANCED_TOOL_CONTROLS
    )
    assert slash.FEATURE_ADVANCED_TOOL_CONTROLS in reactive.KNOWN_FEATURES


def test_row_descriptions_cover_every_row():
    assert set(slash.ROW_DESCRIPTIONS) == set(slash.ROWS)


# ── /tools resolution ────────────────────────────────────────────────


def test_tools_typo_resolves_mcp_server():
    result = parse("/tools #welcome linaer off")
    assert isinstance(result, slash.ToolsCommand)
    assert result.room_id == "!welcome:fil"
    assert result.target == "mcp:linear"
    assert result.verb == "revoke"


def test_tools_bare_mcp_name_gets_prefix():
    result = parse("/tools #welcome gcal on")
    assert result == slash.ToolsCommand(
        room_id="!welcome:fil",
        channel_name="welcome",
        target="mcp:gcal",
        verb="grant",
    )


def test_tools_explicit_mcp_prefix():
    result = parse("/tools #welcome mcp:gcal on")
    assert isinstance(result, slash.ToolsCommand)
    assert result.target == "mcp:gcal"


def test_tools_token_order_is_free():
    expected = parse("/tools #welcome linear off")
    assert isinstance(expected, slash.ToolsCommand)
    permutations = [
        "/tools #welcome off linear",
        "/tools linear #welcome off",
        "/tools linear off #welcome",
        "/tools off #welcome linear",
        "/tools off linear #welcome",
    ]
    for body in permutations:
        assert parse(body) == expected, body


def test_tools_filler_words_are_skipped():
    result = parse("/tools in #welcome disable the linear")
    assert isinstance(result, slash.ToolsCommand)
    assert result.target == "mcp:linear"
    assert result.verb == "revoke"


def test_tools_channel_without_hash_and_with_typo():
    for token in ("welcome", "welcom", "#welcom"):
        result = parse(f"/tools {token} post off")
        assert isinstance(result, slash.ToolsCommand), token
        assert result.room_id == "!welcome:fil"


def test_tools_channel_by_room_id():
    result = parse("/tools !welcome:fil linear on")
    assert isinstance(result, slash.ToolsCommand)
    assert result.room_id == "!welcome:fil"
    assert result.channel_name == "welcome"


def test_tools_verb_synonyms():
    for word in ("enable", "on", "grant", "allow"):
        result = parse(f"/tools #welcome linear {word}")
        assert result.verb == "grant", word
    for word in ("disable", "off", "revoke", "block", "deny"):
        result = parse(f"/tools #welcome linear {word}")
        assert result.verb == "revoke", word


def test_tools_rows_and_deprecated_aliases_are_targets():
    for target in slash.ROWS + slash.DEPRECATED_ALIASES:
        result = parse(f"/tools #welcome {target} on")
        assert isinstance(result, slash.ToolsCommand), target
        assert result.target == target


def test_tools_custom_bundle_is_a_target():
    result = parse("/tools #welcome calendar on", bundles=["calendar"])
    assert isinstance(result, slash.ToolsCommand)
    assert result.target == "calendar"


def test_tools_mcp_servers_as_mapping():
    # The adapter passes {server: tool_count}; names must resolve the same.
    result = parse("/tools #welcome linear off", mcp_servers={"linear": 62})
    assert isinstance(result, slash.ToolsCommand)
    assert result.target == "mcp:linear"


def test_tools_unknown_channel_is_unparsed_and_quotes_token():
    result = parse("/tools #nosuch linear on")
    assert isinstance(result, slash.Unparsed)
    assert "nosuch" in result.problem


def test_tools_unknown_target_is_unparsed():
    result = parse("/tools #welcome zzz on")
    assert isinstance(result, slash.Unparsed)
    assert '"zzz"' in result.problem


def test_tools_missing_verb_reports_what_is_still_needed():
    result = parse("/tools #welcome linear")
    assert isinstance(result, slash.Unparsed)
    assert "on/off" in result.problem
    assert "#welcome" in result.problem


def test_tools_cross_vocabulary_ambiguity_returns_candidates():
    # A channel named like the "post" row: an unprefixed token must not be
    # silently guessed either way.
    channels = [*CHANNELS, ("!post:fil", "post")]
    result = parse("/tools post welcome on", channels=channels)
    assert isinstance(result, slash.Ambiguous)
    assert any("#post" in c for c in result.candidates)
    assert "post" in result.candidates


def test_tools_hash_prefix_forces_channel_classification():
    channels = [*CHANNELS, ("!post:fil", "post")]
    # "#post" is a channel; the later bare "post" can then only be the row.
    result = parse("/tools #post post on", channels=channels)
    assert isinstance(result, slash.ToolsCommand)
    assert result.room_id == "!post:fil"
    assert result.target == "post"


def test_tools_near_tied_channels_are_ambiguous_not_guessed():
    channels = [("!d1:fil", "deploy"), ("!d2:fil", "deploys")]
    result = parse("/tools deploi post off", channels=channels)
    assert isinstance(result, slash.Ambiguous)
    assert len(result.candidates) == 2
    assert any("deploy" in c for c in result.candidates)


def test_tools_bare_command_and_help_route_to_help():
    assert parse("/tools") == slash.HelpRequest("tools")
    assert parse("/tools help") == slash.HelpRequest("tools")


# ── /wake ────────────────────────────────────────────────────────────


def test_wake_basic_and_order_free():
    expected = slash.WakeCommand(
        room_id="!welcome:fil", channel_name="welcome", mode="all"
    )
    assert parse("/wake #welcome all") == expected
    assert parse("/wake all welcome") == expected


def test_wake_mode_typo_and_off():
    result = parse("/wake mentoin #welcome")
    assert isinstance(result, slash.WakeCommand)
    assert result.mode == "mention"
    assert parse("/wake #welcome off").mode == "off"


def test_wake_unknown_mode_is_unparsed():
    result = parse("/wake #welcome sometimes")
    assert isinstance(result, slash.Unparsed)
    assert "sometimes" in result.problem


# ── /guidance ────────────────────────────────────────────────────────


def test_guidance_free_text_preserved_verbatim():
    result = parse("/guidance #welcome Be  brief;   escalate billing.")
    assert isinstance(result, slash.GuidanceCommand)
    assert result.room_id == "!welcome:fil"
    assert result.text == "Be  brief;   escalate billing."


def test_guidance_leading_fillers_before_channel():
    result = parse("/guidance for the #welcome answer in haiku")
    assert isinstance(result, slash.GuidanceCommand)
    assert result.room_id == "!welcome:fil"
    # Fillers before the channel are grammar; everything after is verbatim.
    assert result.text == "answer in haiku"


def test_guidance_reserved_words_in_text_are_not_commands():
    result = parse("/guidance #welcome help people turn things off")
    assert isinstance(result, slash.GuidanceCommand)
    assert result.text == "help people turn things off"


def test_guidance_clear_is_case_insensitive():
    for token in ("clear", "Clear", "CLEAR"):
        result = parse(f"/guidance #welcome {token}")
        assert isinstance(result, slash.GuidanceCommand), token
        assert result.text is None


def test_guidance_without_text_asks_for_it():
    result = parse("/guidance #welcome")
    assert isinstance(result, slash.Unparsed)
    assert "clear" in result.problem


def test_guidance_help_and_bare():
    assert parse("/guidance") == slash.HelpRequest("guidance")
    assert parse("/guidance help") == slash.HelpRequest("guidance")


def test_guidance_unknown_channel():
    result = parse("/guidance #nosuch be nice")
    assert isinstance(result, slash.Unparsed)
    assert "nosuch" in result.problem


# ── /feature ─────────────────────────────────────────────────────────


def test_feature_on_off_and_typo():
    features = dict(reactive.KNOWN_FEATURES)
    result = parse("/feature advanced_tool_controls on", features=features)
    assert result == slash.FeatureCommand(
        feature="advanced_tool_controls", enabled=True
    )
    result = parse("/feature advnced_tool_controls off", features=features)
    assert result == slash.FeatureCommand(
        feature="advanced_tool_controls", enabled=False
    )


def test_feature_unknown_name_is_unparsed():
    result = parse("/feature warp_drive on", features={"advanced_tool_controls": ""})
    assert isinstance(result, slash.Unparsed)
    assert "warp_drive" in result.problem


# ── Command routing / help ───────────────────────────────────────────


def test_help_and_bare_slash():
    assert parse("/help") == slash.HelpRequest(None)
    assert parse("/") == slash.HelpRequest(None)


def test_command_word_fuzzy():
    assert parse("/tols #welcome linear on") == parse("/tools #welcome linear on")
    assert isinstance(parse("/confg"), slash.ShowConfig)


def test_config_show_variants():
    assert isinstance(parse("/config"), slash.ShowConfig)
    assert isinstance(parse("/config show"), slash.ShowConfig)
    assert isinstance(parse("/config shwo"), slash.ShowConfig)
    assert parse("/config help") == slash.HelpRequest("config")


def test_unknown_command_is_unparsed_with_index_in_reply():
    result = parse("/frobnicate everything")
    assert isinstance(result, slash.Unparsed)
    reply = slash.render_reply(result)
    assert "frobnicate" in reply
    for command in ("config", "tools", "wake", "guidance", "feature"):
        assert f"/{command}" in reply


def test_render_reply_ambiguous_asks_did_you_mean():
    channels = [*CHANNELS, ("!post:fil", "post")]
    result = parse("/tools post welcome on", channels=channels)
    reply = slash.render_reply(result)
    assert "did you mean" in reply
    assert "#post" in reply
    assert "Usage:" in reply


def test_render_reply_unparsed_includes_usage_example():
    reply = slash.render_reply(parse("/tools #welcome zzz on"))
    assert "/tools #welcome linear off" in reply


# ── Help text builders ───────────────────────────────────────────────


def test_help_index_lists_all_commands_and_help_hint():
    text = slash.help_index()
    for command in ("config", "tools", "wake", "guidance", "feature"):
        assert f"/{command}" in text
    assert "/tools help" in text


def test_help_tools_shows_rows_mcp_servers_and_examples():
    text = slash.help_tools(CHANNELS, {"linear": 62, "gcal": None}, ["cli"])
    for row in slash.ROWS:
        assert row in text
        assert slash.ROW_DESCRIPTIONS[row] in text
    assert "mcp:linear (62 tools)" in text
    assert "mcp:gcal" in text
    assert "not per-channel switchable yet" in text
    assert "cli" in text
    # Examples use a real channel and advertise the fuzzy tolerance.
    assert "/tools #welcome" in text
    assert "spelling close enough" in text


def test_help_tools_without_mcp_servers():
    text = slash.help_tools(CHANNELS, {}, [])
    assert "none" in text


def test_help_wake_and_guidance_use_live_channel():
    assert "/wake #welcome all" in slash.help_wake(CHANNELS)
    assert "#welcome" in slash.help_guidance(CHANNELS)
    assert "clear" in slash.help_guidance(CHANNELS)


def test_help_feature_lists_known_features():
    text = slash.help_feature(reactive.KNOWN_FEATURES)
    assert "advanced_tool_controls" in text


def test_help_for_dispatches():
    assert slash.help_for(None) == slash.help_index()
    assert slash.help_for("tools", channels=CHANNELS) == slash.help_tools(
        CHANNELS, (), ()
    )
    assert slash.help_for("config") == slash.help_config()


# ── Compiled mutations: /tools ───────────────────────────────────────


def _tools(target, verb, room_id="!welcome:fil", name="welcome"):
    return slash.ToolsCommand(
        room_id=room_id, channel_name=name, target=target, verb=verb
    )


def test_apply_tools_grant_materializes_from_default_and_sets_flag():
    mutation = slash.apply_tools(_tools("mcp:gcal", "grant"), {}, {})
    assert mutation.changed
    assert mutation.capability_policy["per_channel"]["!welcome:fil"] == [
        *slash.ROWS,
        "mcp:gcal",
    ]
    # The opt-in coupling: a per-channel edit turns the gating feature on in
    # the same write.
    assert mutation.feature_flags == {"advanced_tool_controls": True}
    assert mutation.sections == ("capability_policy", "feature_flags")
    assert "✓ Enabled gcal in #welcome" in mutation.reply
    assert "advanced tool controls are now on" in mutation.reply


def test_apply_tools_revoke_row_from_default():
    mutation = slash.apply_tools(
        _tools("post", "revoke"), {}, {"advanced_tool_controls": True}
    )
    assert mutation.changed
    assert mutation.capability_policy["per_channel"]["!welcome:fil"] == [
        "read_history",
        "directory",
        "escalate",
    ]
    # Flag already on → capability_policy is the only section written back.
    assert mutation.feature_flags is None
    assert mutation.sections == ("capability_policy",)


def test_apply_tools_echo_matches_product_copy():
    policy = {
        "per_channel": {"!welcome:fil": [*slash.ROWS, "mcp:linear"]}
    }
    mutation = slash.apply_tools(
        _tools("mcp:linear", "revoke"), policy, {"advanced_tool_controls": True}
    )
    assert mutation.reply == (
        "✓ Disabled linear in #welcome "
        "(tools now: read_history, post, directory, escalate)"
    )


def test_apply_tools_materializes_from_custom_default_list():
    policy = {"default_capabilities": ["read_history"]}
    mutation = slash.apply_tools(_tools("escalate", "grant"), policy, {})
    assert mutation.capability_policy["per_channel"]["!welcome:fil"] == [
        "read_history",
        "escalate",
    ]
    # The default list itself is untouched — override semantics, not edit.
    assert mutation.capability_policy["default_capabilities"] == ["read_history"]


def test_apply_tools_grant_already_present_is_a_noop():
    mutation = slash.apply_tools(_tools("post", "grant"), {}, {})
    assert not mutation.changed
    assert mutation.sections == ()
    assert mutation.capability_policy is None
    assert "already" in mutation.reply


def test_apply_tools_revoke_absent_is_a_noop():
    mutation = slash.apply_tools(_tools("mcp:gcal", "revoke"), {}, {})
    assert not mutation.changed
    assert mutation.sections == ()
    assert "isn't enabled" in mutation.reply


def test_apply_tools_preserves_other_channels():
    policy = {"per_channel": {"!other:fil": ["post"]}}
    mutation = slash.apply_tools(_tools("directory", "revoke"), policy, {})
    assert mutation.capability_policy["per_channel"]["!other:fil"] == ["post"]


def test_apply_tools_sanitizes_channel_name_in_echo():
    mutation = slash.apply_tools(
        _tools("post", "revoke", name="bad\nname\x07here"), {}, {}
    )
    assert "\n" not in mutation.reply  # the name can't break out of its line
    assert "bad name" in mutation.reply  # whitespace collapsed
    assert "\x07" not in mutation.reply  # non-printables dropped


def test_apply_tools_unnamed_channel_falls_back_to_room_id():
    mutation = slash.apply_tools(_tools("post", "revoke", name=""), {}, {})
    assert "!welcome:fil" in mutation.reply


# ── Compiled mutations: /wake, /guidance, /feature ───────────────────


def test_apply_wake_sets_per_channel_mode_and_preserves_entry():
    policy = {
        "reactive_wake": "mention",
        "per_channel": {"!welcome:fil": {"reply_style": "channel"}},
    }
    command = slash.WakeCommand(
        room_id="!welcome:fil", channel_name="welcome", mode="all"
    )
    mutation = slash.apply_wake(command, policy)
    assert mutation.changed
    assert mutation.sections == ("wake_policy",)
    entry = mutation.wake_policy["per_channel"]["!welcome:fil"]
    assert entry == {"reply_style": "channel", "reactive_wake": "all"}
    assert "✓ Wake mode for #welcome: all" in mutation.reply


def test_apply_wake_explicit_same_mode_is_a_noop():
    policy = {"per_channel": {"!welcome:fil": {"reactive_wake": "all"}}}
    command = slash.WakeCommand(
        room_id="!welcome:fil", channel_name="welcome", mode="all"
    )
    mutation = slash.apply_wake(command, policy)
    assert not mutation.changed
    assert mutation.sections == ()


def test_apply_wake_pins_mode_even_when_global_default_matches():
    # An explicit per-channel pin is a real edit even if the global default
    # already resolves the same way — it survives a later default change.
    command = slash.WakeCommand(
        room_id="!welcome:fil", channel_name="welcome", mode="mention"
    )
    mutation = slash.apply_wake(command, {"reactive_wake": "mention"})
    assert mutation.changed
    entry = mutation.wake_policy["per_channel"]["!welcome:fil"]
    assert entry["reactive_wake"] == "mention"


def test_apply_guidance_set_clear_roundtrip():
    command = slash.GuidanceCommand(
        room_id="!welcome:fil", channel_name="welcome", text="Be  brief."
    )
    mutation = slash.apply_guidance(command, {"!other:fil": "keep"})
    assert mutation.changed
    assert mutation.sections == ("channel_instructions",)
    assert mutation.channel_instructions == {
        "!other:fil": "keep",
        "!welcome:fil": "Be  brief.",  # verbatim, interior spacing intact
    }
    clear = slash.GuidanceCommand(
        room_id="!welcome:fil", channel_name="welcome", text=None
    )
    cleared = slash.apply_guidance(clear, mutation.channel_instructions)
    assert cleared.changed
    assert cleared.channel_instructions == {"!other:fil": "keep"}
    assert "Cleared" in cleared.reply


def test_apply_guidance_clear_when_absent_is_a_noop():
    clear = slash.GuidanceCommand(
        room_id="!welcome:fil", channel_name="welcome", text=None
    )
    mutation = slash.apply_guidance(clear, {})
    assert not mutation.changed
    assert mutation.sections == ()


def test_apply_feature_toggle_and_noop():
    command = slash.FeatureCommand(feature="advanced_tool_controls", enabled=True)
    mutation = slash.apply_feature(command, {})
    assert mutation.changed
    assert mutation.feature_flags == {"advanced_tool_controls": True}
    assert mutation.sections == ("feature_flags",)
    again = slash.apply_feature(command, mutation.feature_flags)
    assert not again.changed
    assert "already" in again.reply


# ── /config show rendering ───────────────────────────────────────────


def test_render_config_show_uses_channel_names():
    text = slash.render_config_show(
        capability_policy={
            "default_capabilities": list(slash.ROWS),
            "per_channel": {"!welcome:fil": ["read_history", "mcp:linear"]},
        },
        wake_policy={
            "reactive_wake": "mention",
            "per_channel": {"!general:fil": {"reactive_wake": "all"}},
        },
        channel_instructions={"!welcome:fil": "Be brief."},
        feature_flags={"advanced_tool_controls": True},
        channels=CHANNELS,
    )
    assert "#welcome: read_history, mcp:linear" in text
    assert "#general: all" in text
    assert "#welcome: set (9 chars)" in text
    assert "advanced_tool_controls: on" in text
    # Room ids the channel list knows are shown as names, not ids.
    assert "!welcome:fil" not in text


def test_render_config_show_defaults_and_unknown_rooms():
    text = slash.render_config_show(
        capability_policy={},
        wake_policy={"per_channel": {"!gone:fil": {"reactive_wake": "off"}}},
        channel_instructions={},
        feature_flags={},
        channels=CHANNELS,
    )
    assert "default: read_history, post, directory, escalate" in text
    assert "default: mention" in text
    # A room the channel list doesn't know still shows (by id).
    assert "!gone:fil: off" in text
    assert "advanced_tool_controls: off" in text
    assert "none" in text  # guidance section
