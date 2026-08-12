"""Tests for the deterministic backchannel slash-command layer.

``slash.py`` is pure-stdlib and side-effect-free, so it is loaded standalone
(importing the package would pull in Hermes). ``reactive.py`` is loaded the
same way to pin the vocabularies the two modules deliberately restate.

The parser is the product surface: the resolver tests below (typos, token
order, fillers, ambiguity) are the behavior the principal actually types
against, so they are deliberately heavy. The surface is consolidated under
``/fil-config`` subforms; the retired top-level commands answer with a
redirect, pinned here too.
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


def test_bundle_members_match_reactive_builtin_bundles():
    assert {k: list(v) for k, v in slash.BUNDLE_MEMBERS.items()} == (
        reactive.BUILTIN_BUNDLES
    )


# ── The intercept boundary ───────────────────────────────────────────


def test_is_fil_command_boundary():
    # The adapter's intercept boundary in both directions: /fil-* is ours
    # (case-insensitive prefix, even when unknown), everything else — other
    # slash namespaces included — is not.
    for body in ("/fil-help", "  /FIL-config list", "/fil-frobnicate", "/fil-"):
        assert slash.is_fil_command(body), body
    for body in ("/tools #welcome post off", "/help", "/filament", "/", "hi", "", None):
        assert not slash.is_fil_command(body), body


def test_help_and_bare_prefix():
    assert parse("/fil-help") == slash.HelpRequest(None)
    assert parse("/fil-") == slash.HelpRequest(None)


def test_prefix_is_case_insensitive():
    expected = parse("/fil-config #welcome linear off")
    assert isinstance(expected, slash.ToolsCommand)
    assert parse("/FIL-config #welcome linear off") == expected
    assert parse("/Fil-Config #welcome linear off") == expected


# ── /fil-config: tools grant resolution ──────────────────────────────


def test_tools_typo_resolves_mcp_server():
    result = parse("/fil-config #welcome linaer off")
    assert isinstance(result, slash.ToolsCommand)
    assert result.room_id == "!welcome:fil"
    assert result.target == "mcp:linear"
    assert result.verb == "revoke"


def test_tools_bare_mcp_name_gets_prefix():
    result = parse("/fil-config #welcome gcal on")
    assert result == slash.ToolsCommand(
        room_id="!welcome:fil",
        channel_name="welcome",
        target="mcp:gcal",
        verb="grant",
    )


def test_tools_explicit_mcp_prefix():
    result = parse("/fil-config #welcome mcp:gcal on")
    assert isinstance(result, slash.ToolsCommand)
    assert result.target == "mcp:gcal"


def test_tools_token_order_is_free():
    expected = parse("/fil-config #welcome linear off")
    assert isinstance(expected, slash.ToolsCommand)
    permutations = [
        "/fil-config #welcome off linear",
        "/fil-config linear #welcome off",
        "/fil-config linear off #welcome",
        "/fil-config off #welcome linear",
        "/fil-config off linear #welcome",
    ]
    for body in permutations:
        assert parse(body) == expected, body


def test_tools_keyword_is_optional_and_accepted():
    expected = parse("/fil-config #welcome linear off")
    assert isinstance(expected, slash.ToolsCommand)
    assert parse("/fil-config #welcome tools linear off") == expected
    assert parse("/fil-config tools #welcome linear off") == expected


def test_tools_filler_words_are_skipped():
    result = parse("/fil-config in #welcome disable the linear")
    assert isinstance(result, slash.ToolsCommand)
    assert result.target == "mcp:linear"
    assert result.verb == "revoke"


def test_tools_channel_without_hash_and_with_typo():
    for token in ("welcome", "welcom", "#welcom"):
        result = parse(f"/fil-config {token} post off")
        assert isinstance(result, slash.ToolsCommand), token
        assert result.room_id == "!welcome:fil"


def test_tools_channel_by_room_id():
    result = parse("/fil-config !welcome:fil linear on")
    assert isinstance(result, slash.ToolsCommand)
    assert result.room_id == "!welcome:fil"
    assert result.channel_name == "welcome"


def test_tools_verb_synonyms():
    for word in ("enable", "on", "grant", "allow"):
        result = parse(f"/fil-config #welcome linear {word}")
        assert result.verb == "grant", word
    for word in ("disable", "off", "revoke", "block", "deny"):
        result = parse(f"/fil-config #welcome linear {word}")
        assert result.verb == "revoke", word


def test_tools_rows_and_deprecated_aliases_are_targets():
    for target in slash.ROWS + slash.DEPRECATED_ALIASES:
        result = parse(f"/fil-config #welcome {target} on")
        assert isinstance(result, slash.ToolsCommand), target
        assert result.target == target


def test_tools_custom_bundle_is_a_target():
    result = parse("/fil-config #welcome calendar on", bundles=["calendar"])
    assert isinstance(result, slash.ToolsCommand)
    assert result.target == "calendar"


def test_tools_mcp_servers_as_mapping():
    # The adapter passes {server: tool_count}; names must resolve the same.
    result = parse("/fil-config #welcome linear off", mcp_servers={"linear": 62})
    assert isinstance(result, slash.ToolsCommand)
    assert result.target == "mcp:linear"


def test_tools_unknown_channel_is_unparsed_and_quotes_token():
    result = parse("/fil-config #nosuch linear on")
    assert isinstance(result, slash.Unparsed)
    assert "nosuch" in result.problem


def test_tools_unknown_target_is_unparsed():
    result = parse("/fil-config #welcome zzz on")
    assert isinstance(result, slash.Unparsed)
    assert '"zzz"' in result.problem


def test_tools_missing_verb_reports_what_is_still_needed():
    result = parse("/fil-config #welcome linear")
    assert isinstance(result, slash.Unparsed)
    assert "on/off" in result.problem
    assert "#welcome" in result.problem


def test_tools_verb_without_target_still_asks():
    result = parse("/fil-config #welcome off")
    assert isinstance(result, slash.Unparsed)
    assert "bundle" in result.problem


def test_tools_target_and_verb_without_channel_still_ask():
    result = parse("/fil-config linear off")
    assert isinstance(result, slash.Unparsed)
    assert "channel" in result.problem


def test_tools_cross_vocabulary_ambiguity_returns_candidates():
    # A channel named like the "post" row: an unprefixed token must not be
    # silently guessed either way.
    channels = [*CHANNELS, ("!post:fil", "post")]
    result = parse("/fil-config post welcome on", channels=channels)
    assert isinstance(result, slash.Ambiguous)
    assert any("#post" in c for c in result.candidates)
    assert "post" in result.candidates


def test_tools_hash_prefix_forces_channel_classification():
    channels = [*CHANNELS, ("!post:fil", "post")]
    # "#post" is a channel; the later bare "post" can then only be the row.
    result = parse("/fil-config #post post on", channels=channels)
    assert isinstance(result, slash.ToolsCommand)
    assert result.room_id == "!post:fil"
    assert result.target == "post"


def test_tools_near_tied_channels_are_ambiguous_not_guessed():
    channels = [("!d1:fil", "deploy"), ("!d2:fil", "deploys")]
    result = parse("/fil-config deploi post off", channels=channels)
    assert isinstance(result, slash.Ambiguous)
    assert len(result.candidates) == 2
    assert any("deploy" in c for c in result.candidates)


# ── Multi-word channel names (greedy n-gram matching) ────────────────

MW_CHANNELS = [
    *CHANNELS,
    ("!gc:fil", "General Chat"),
    ("!dev:fil", "dev"),
    ("!devops:fil", "dev ops"),
    ("!ops:fil", "ops room"),
]


def test_multiword_channel_exact_show_and_mutation():
    result = parse("/fil-config #General Chat show", channels=MW_CHANNELS)
    assert result == slash.ChannelShow(
        room_id="!gc:fil", channel_name="General Chat"
    )
    result = parse("/fil-config General Chat linear off", channels=MW_CHANNELS)
    assert result == slash.ToolsCommand(
        room_id="!gc:fil",
        channel_name="General Chat",
        target="mcp:linear",
        verb="revoke",
    )


def test_multiword_channel_exact_beats_single_word_prefix():
    # "General Chat" must win over the bare "general" channel that prefixes
    # it — longest exact match first.
    result = parse("/fil-config #General Chat show", channels=MW_CHANNELS)
    assert isinstance(result, slash.ChannelShow)
    assert result.room_id == "!gc:fil"


def test_multiword_channel_typo_resolves_fuzzily():
    result = parse("/fil-config #ops rom show", channels=MW_CHANNELS)
    assert result == slash.ChannelShow(room_id="!ops:fil", channel_name="ops room")


def test_prefixing_channel_names_longest_wins():
    result = parse("/fil-config dev ops post off", channels=MW_CHANNELS)
    assert isinstance(result, slash.ToolsCommand)
    assert result.room_id == "!devops:fil"
    result = parse("/fil-config dev post off", channels=MW_CHANNELS)
    assert isinstance(result, slash.ToolsCommand)
    assert result.room_id == "!dev:fil"


def test_multiword_channel_near_tie_still_asks():
    channels = [("!a:fil", "general chat"), ("!b:fil", "general chats")]
    result = parse("/fil-config general chatz post off", channels=channels)
    assert isinstance(result, slash.Ambiguous)
    assert len(result.candidates) == 2


def test_multiword_channel_guidance_and_example_roundtrip():
    result = parse(
        "/fil-config General Chat guidance be nice", channels=MW_CHANNELS
    )
    assert result == slash.GuidanceCommand(
        room_id="!gc:fil", channel_name="General Chat", text="be nice"
    )
    # Examples emit real names verbatim — and they must parse back.
    example = slash._example_channel([("!gc:fil", "General Chat")])
    assert example == "#General Chat"
    result = parse(
        f"/fil-config {example} show", channels=[("!gc:fil", "General Chat")]
    )
    assert isinstance(result, slash.ChannelShow)


def test_multiword_backchannel_still_gets_the_note():
    result = parse(
        "/fil-config #Laptop agent post off",
        backchannel=("!cc:fil", "Laptop agent"),
    )
    assert isinstance(result, slash.Unparsed)
    assert result.problem == slash.BACKCHANNEL_NOTE


# ── /fil-config: overview / channel / catalog queries ────────────────


def test_bare_config_and_help_route_to_compact_index():
    assert parse("/fil-config") == slash.HelpRequest("config")
    assert parse("/fil-config help") == slash.HelpRequest("config")


def test_config_list_is_the_overview():
    assert parse("/fil-config list") == slash.ChannelsOverview()
    # Fuzzy like any other token.
    assert parse("/fil-config lst") == slash.ChannelsOverview()


def test_channel_show_forms_are_equivalent():
    expected = slash.ChannelShow(room_id="!welcome:fil", channel_name="welcome")
    for body in (
        "/fil-config #welcome",
        "/fil-config welcome",
        "/fil-config welcom",
        "/fil-config #welcome show",
        "/fil-config show #welcome",
        "/fil-config #welcome list",
        "/fil-config #welcome tools",
    ):
        assert parse(body) == expected, body


def test_config_tools_list_is_the_catalog():
    assert parse("/fil-config tools list") == slash.ToolsList()
    assert parse("/fil-config list tools") == slash.ToolsList()


def test_config_tools_and_wake_alone_route_to_contextual_help():
    assert parse("/fil-config tools") == slash.HelpRequest("tools")
    assert parse("/fil-config wake") == slash.HelpRequest("wake")


def test_query_keyword_with_mutation_tokens_never_mutates():
    result = parse("/fil-config #welcome list post off")
    assert isinstance(result, slash.Unparsed)
    assert "`/fil-config #welcome show`" in result.problem
    assert "`/fil-config #welcome <tool> <on|off>`" in result.problem
    result = parse("/fil-config #welcome show wake all")
    assert isinstance(result, slash.Unparsed)
    assert "mixes a query with a change" in result.problem


def test_old_config_show_gets_a_redirect():
    for body in ("/fil-config show", "/fil-config shwo"):
        result = parse(body)
        assert isinstance(result, slash.Redirect), body
        assert "`/fil-config list`" in result.reply


# ── /fil-config <channel> wake ───────────────────────────────────────


def test_wake_basic_and_order_free():
    expected = slash.WakeCommand(
        room_id="!welcome:fil", channel_name="welcome", mode="all"
    )
    assert parse("/fil-config #welcome wake all") == expected
    assert parse("/fil-config wake all welcome") == expected
    assert parse("/fil-config all wake #welcome") == expected


def test_wake_mode_typo_and_off_via_verb():
    result = parse("/fil-config #welcome wake mentoin")
    assert isinstance(result, slash.WakeCommand)
    assert result.mode == "mention"
    # "off" classifies as a revoke verb; with the wake keyword it is the
    # off mode.
    assert parse("/fil-config #welcome wake off").mode == "off"


def test_wake_grant_verb_is_rejected():
    result = parse("/fil-config #welcome wake on")
    assert isinstance(result, slash.Unparsed)
    assert "mention, all, or off" in result.problem


def test_wake_keyword_without_mode_still_asks():
    result = parse("/fil-config #welcome wake")
    assert isinstance(result, slash.Unparsed)
    assert "mention, all, or off" in result.problem


def test_wake_mode_without_keyword_points_at_the_spelling():
    result = parse("/fil-config #welcome all")
    assert isinstance(result, slash.Unparsed)
    assert "`/fil-config #welcome wake all`" in result.problem


def test_wake_unknown_mode_is_unparsed():
    result = parse("/fil-config #welcome wake sometimes")
    assert isinstance(result, slash.Unparsed)
    assert "sometimes" in result.problem


# ── /fil-config <channel> guidance ───────────────────────────────────


def test_guidance_free_text_preserved_verbatim():
    result = parse("/fil-config #welcome guidance Be  brief;   escalate billing.")
    assert isinstance(result, slash.GuidanceCommand)
    assert result.room_id == "!welcome:fil"
    assert result.text == "Be  brief;   escalate billing."


def test_guidance_keyword_first_takes_channel_from_text_head():
    result = parse("/fil-config guidance #welcome answer in haiku")
    assert isinstance(result, slash.GuidanceCommand)
    assert result.room_id == "!welcome:fil"
    assert result.text == "answer in haiku"


def test_guidance_leading_fillers_before_channel():
    result = parse("/fil-config for the #welcome guidance answer in haiku")
    assert isinstance(result, slash.GuidanceCommand)
    assert result.room_id == "!welcome:fil"
    # Fillers before the channel are grammar; everything after the keyword
    # is verbatim.
    assert result.text == "answer in haiku"


def test_guidance_reserved_words_in_text_are_not_commands():
    result = parse("/fil-config #welcome guidance help people turn things off")
    assert isinstance(result, slash.GuidanceCommand)
    assert result.text == "help people turn things off"


def test_guidance_clear_is_case_insensitive():
    for token in ("clear", "Clear", "CLEAR"):
        result = parse(f"/fil-config #welcome guidance {token}")
        assert isinstance(result, slash.GuidanceCommand), token
        assert result.text is None


def test_guidance_channel_without_text_is_a_show_query():
    result = parse("/fil-config #welcome guidance")
    assert result == slash.GuidanceShow(
        room_id="!welcome:fil", channel_name="welcome"
    )


def test_guidance_keyword_alone_routes_to_contextual_help():
    assert parse("/fil-config guidance") == slash.HelpRequest("guidance")


def test_guidance_keyword_typo_still_routes():
    result = parse("/fil-config #welcome guidnce be nice")
    assert isinstance(result, slash.GuidanceCommand)
    assert result.text == "be nice"


def test_guidance_unknown_channel():
    result = parse("/fil-config #nosuch guidance be nice")
    assert isinstance(result, slash.Unparsed)
    assert "nosuch" in result.problem


# ── /fil-config feature ──────────────────────────────────────────────


def test_feature_on_off_and_typo():
    features = dict(reactive.KNOWN_FEATURES)
    result = parse("/fil-config feature advanced_tool_controls on", features=features)
    assert result == slash.FeatureCommand(
        feature="advanced_tool_controls", enabled=True
    )
    result = parse(
        "/fil-config feature advnced_tool_controls off", features=features
    )
    assert result == slash.FeatureCommand(
        feature="advanced_tool_controls", enabled=False
    )


def test_feature_unknown_name_is_unparsed():
    result = parse(
        "/fil-config feature warp_drive on",
        features={"advanced_tool_controls": ""},
    )
    assert isinstance(result, slash.Unparsed)
    assert "warp_drive" in result.problem


def test_feature_bare_and_list_render_the_feature_list():
    features = dict(reactive.KNOWN_FEATURES)
    assert parse("/fil-config feature", features=features) == slash.FeatureList()
    assert (
        parse("/fil-config feature list", features=features)
        == slash.FeatureList()
    )
    # Fuzzy like any other token.
    assert (
        parse("/fil-config feature lst", features=features)
        == slash.FeatureList()
    )


def test_feature_name_without_verb_is_a_show_query():
    features = dict(reactive.KNOWN_FEATURES)
    result = parse("/fil-config feature slash_commands", features=features)
    assert result == slash.FeatureShow(feature="slash_commands")
    # Typos resolve the same way as in the mutation form.
    result = parse(
        "/fil-config feature advnced_tool_controls", features=features
    )
    assert result == slash.FeatureShow(feature="advanced_tool_controls")


def test_feature_state_without_name_still_asks():
    result = parse(
        "/fil-config feature on", features=dict(reactive.KNOWN_FEATURES)
    )
    assert isinstance(result, slash.Unparsed)
    assert "feature name" in result.problem


def test_feature_list_mixed_with_other_tokens_is_unparsed():
    result = parse(
        "/fil-config feature list on", features=dict(reactive.KNOWN_FEATURES)
    )
    assert isinstance(result, slash.Unparsed)
    assert "`/fil-config feature list`" in result.problem


# ── Old-form redirects ───────────────────────────────────────────────


def test_old_tools_mutation_redirects_with_translated_example():
    result = parse("/fil-tools #welcome linear off")
    assert isinstance(result, slash.Redirect)
    assert "moved under `/fil-config`" in result.reply
    assert "`/fil-config #welcome linear off`" in result.reply


def test_old_tools_list_and_bare_redirect():
    result = parse("/fil-tools list")
    assert isinstance(result, slash.Redirect)
    assert "`/fil-config tools list`" in result.reply
    result = parse("/fil-tools")
    assert isinstance(result, slash.Redirect)
    assert "`/fil-config help`" in result.reply


def test_old_wake_redirects_with_translated_example():
    result = parse("/fil-wake #general all")
    assert isinstance(result, slash.Redirect)
    assert "`/fil-config #general wake all`" in result.reply
    # Untranslatable arguments fall back to the generic form.
    result = parse("/fil-wake")
    assert isinstance(result, slash.Redirect)
    assert "wake <mention|all|off>" in result.reply


def test_old_guidance_redirects_with_translated_example():
    result = parse("/fil-guidance #welcome Be brief.")
    assert isinstance(result, slash.Redirect)
    assert "`/fil-config #welcome guidance Be brief.`" in result.reply


def test_old_feature_redirects_with_translated_example():
    result = parse("/fil-feature advanced_tool_controls on")
    assert isinstance(result, slash.Redirect)
    assert "`/fil-config feature advanced_tool_controls on`" in result.reply


def test_old_command_typo_still_redirects():
    result = parse("/fil-tols #welcome linear on")
    assert isinstance(result, slash.Redirect)
    assert "`/fil-config #welcome linear on`" in result.reply


# ── Command routing / errors ─────────────────────────────────────────


def test_command_word_fuzzy():
    assert isinstance(parse("/fil-confg list"), slash.ChannelsOverview)


def test_unknown_command_is_unparsed_with_index_in_reply():
    result = parse("/fil-frobnicate everything")
    assert isinstance(result, slash.Unparsed)
    reply = slash.render_reply(result)
    assert "frobnicate" in reply
    assert "`/fil-config`" in reply  # the index
    assert "/fil-config help" in reply


def test_render_reply_ambiguous_asks_did_you_mean():
    channels = [*CHANNELS, ("!post:fil", "post")]
    result = parse("/fil-config post welcome on", channels=channels)
    reply = slash.render_reply(result)
    assert "did you mean" in reply
    assert "#post" in reply
    assert "Usage:" in reply


def test_render_reply_unparsed_includes_usage_example():
    reply = slash.render_reply(parse("/fil-config #welcome zzz on"))
    assert "`/fil-config <channel> <tool> <on|off>`" in reply
    reply = slash.render_reply(parse("/fil-config #welcome linear"))
    assert "`/fil-config #welcome linear off`" in reply


# ── Help text builders ───────────────────────────────────────────────


def test_help_index_points_at_config():
    text = slash.help_index()
    assert text.startswith("**Commands:**")
    assert "\n- `/fil-config`" in text
    assert "`/fil-config list`" in text
    assert "`/fil-config <channel>`" in text
    assert "`/fil-config tools list`" in text
    assert "`/fil-config help`" in text


def test_help_config_is_the_compact_form_index():
    text = slash.help_config(CHANNELS)
    assert "- `/fil-config list`" in text
    assert "- `/fil-config <channel>`" in text
    assert "- `/fil-config <channel> <tool> <on|off>`" in text
    assert "- `/fil-config <channel> wake <mention|all|off>`" in text
    assert "- `/fil-config <channel> guidance <text…|clear>`" in text
    assert "- `/fil-config tools list`" in text
    assert "- `/fil-config feature <name> <on|off>`" in text
    assert "`/fil-config #welcome linear off`" in text  # real-channel example
    assert "Typos are fine" in text
    for row in slash.ROWS:  # an index, not the catalog
        assert slash.ROW_DESCRIPTIONS[row] not in text


def test_help_tools_is_compact_pointers_only():
    text = slash.help_tools(CHANNELS)
    assert len(text.splitlines()) == 5
    assert "- `/fil-config tools list`" in text
    assert "- `/fil-config <channel>`" in text
    assert "`/fil-config #welcome linear off`" in text
    assert "`bundle:wake on`" in text  # the grammar-collision escape
    assert "Typos are fine" in text


def test_help_wake_and_guidance_use_new_forms():
    text = slash.help_wake(CHANNELS)
    assert "`/fil-config <channel> wake <mention|all|off>`" in text
    assert "`/fil-config #welcome wake all`" in text
    text = slash.help_guidance(CHANNELS)
    assert "`/fil-config #welcome guidance`" in text
    assert "`/fil-config #welcome guidance clear`" in text


def test_help_feature_lists_known_features():
    text = slash.help_feature(reactive.KNOWN_FEATURES)
    assert "advanced_tool_controls" in text
    assert "`/fil-config feature advanced_tool_controls on`" in text


def test_help_for_dispatches():
    assert slash.help_for(None) == slash.help_index()
    assert slash.help_for("config", channels=CHANNELS) == slash.help_config(
        CHANNELS
    )
    assert slash.help_for("tools", channels=CHANNELS) == slash.help_tools(
        CHANNELS
    )


def test_example_channel_placeholder_when_no_shared_channels():
    # With no shared channels (e.g. backchannel-only agent), examples fall
    # back to a generic placeholder, never nothing.
    for text in (
        slash.help_config(()),
        slash.help_tools(()),
        slash.help_wake(()),
        slash.help_guidance(()),
        slash.render_tools_list(()),
    ):
        assert "#your-channel" in text


# ── Backchannel exclusion ────────────────────────────────────────────
# The adapter passes the cc room separately, never in ``channels``:
# per-channel controls are meaningless for the control plane and its name
# must never surface as a help example.

BACKCHANNEL = ("!cc:fil", "backchannel")


def test_backchannel_targeting_gets_the_note():
    for body in (
        "/fil-config #backchannel post off",
        "/fil-config !cc:fil post off",
        "/fil-config #backchannel",
        "/fil-config #backchannel wake all",
    ):
        result = parse(body, backchannel=BACKCHANNEL)
        assert isinstance(result, slash.Unparsed), body
        assert result.problem == slash.BACKCHANNEL_NOTE, body


def test_guidance_targeting_backchannel_gets_the_note():
    result = parse(
        "/fil-config #backchannel guidance be brief", backchannel=BACKCHANNEL
    )
    assert isinstance(result, slash.Unparsed)
    assert result.problem == slash.BACKCHANNEL_NOTE


def test_unknown_channel_without_backchannel_match_keeps_generic_error():
    result = parse("/fil-config #nosuch post off", backchannel=BACKCHANNEL)
    assert isinstance(result, slash.Unparsed)
    assert "nosuch" in result.problem
    assert result.problem != slash.BACKCHANNEL_NOTE


# ── Compiled mutations: tools ────────────────────────────────────────


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
    assert "✓ Enabled **gcal** in **#welcome**" in mutation.reply
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
        "✓ Disabled **linear** in **#welcome** "
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


def test_apply_tools_state_match_with_flag_on_is_a_true_noop():
    flags = {"advanced_tool_controls": True}
    mutation = slash.apply_tools(_tools("post", "grant"), {}, flags)
    assert not mutation.changed
    assert mutation.sections == ()
    assert mutation.capability_policy is None
    assert "already" in mutation.reply
    mutation = slash.apply_tools(_tools("mcp:gcal", "revoke"), {}, flags)
    assert not mutation.changed
    assert mutation.sections == ()
    assert "isn't enabled" in mutation.reply


def test_apply_tools_state_match_with_flag_off_still_enables_enforcement():
    # The principal asked for this state to APPLY: if enforcement is off the
    # "no-op" still turns the gate on in the same write and says so.
    mutation = slash.apply_tools(_tools("post", "grant"), {}, {})
    assert mutation.changed
    assert mutation.capability_policy is None  # the grant list is untouched
    assert mutation.feature_flags == {"advanced_tool_controls": True}
    assert mutation.sections == ("feature_flags",)
    assert "**post** was already enabled" in mutation.reply
    assert "enforcement was off, so I turned it on; grants now apply" in (
        mutation.reply
    )
    mutation = slash.apply_tools(_tools("mcp:gcal", "revoke"), {}, {})
    assert mutation.changed
    assert mutation.feature_flags == {"advanced_tool_controls": True}
    assert mutation.sections == ("feature_flags",)
    assert "**gcal** isn't enabled" in mutation.reply
    assert "enforcement was off" in mutation.reply


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


# ── Compiled mutations: wake, guidance, feature ──────────────────────


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
    assert "✓ Wake mode for **#welcome**: **all**" in mutation.reply


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


# ── /fil-config list rendering ───────────────────────────────────────


def test_render_config_list_one_line_per_channel():
    text = slash.render_config_list(
        capability_policy={
            "per_channel": {"!welcome:fil": ["read_history", "mcp:linear"]}
        },
        wake_policy={
            "reactive_wake": "mention",
            "per_channel": {"!general:fil": {"reactive_wake": "all"}},
        },
        channel_instructions={"!welcome:fil": "Be brief."},
        channels=CHANNELS,
    )
    assert text.startswith("**Channels:**")
    assert (
        "- **#welcome** — tools: read_history, mcp:linear (override); "
        "guidance set" in text
    )
    assert "- **#general** — tools: default; wake: all" in text
    assert text.rstrip().endswith("Details: `/fil-config #welcome show`")


def test_render_config_list_hides_default_matching_wake_pin():
    # "wake mode if non-default": a pin equal to the current default adds
    # noise, not information.
    text = slash.render_config_list(
        capability_policy={},
        wake_policy={
            "reactive_wake": "all",
            "per_channel": {"!welcome:fil": {"reactive_wake": "all"}},
        },
        channel_instructions={},
        channels=CHANNELS,
    )
    assert "- **#welcome** — tools: default" in text
    assert "wake:" not in text


def test_render_config_list_without_channels():
    text = slash.render_config_list(
        capability_policy={},
        wake_policy={},
        channel_instructions={},
        channels=(),
    )
    assert "No shared channels yet" in text


# ── /fil-config <channel> rendering ──────────────────────────────────


def test_render_channel_show_default_grant():
    text = slash.render_channel_show(
        room_id="!welcome:fil",
        channel_name="welcome",
        capability_policy={},
        feature_flags={},
        wake_policy={},
        channel_instructions={},
        mcp_servers={"linear": 62, "gcal": None},
        other_sources={"cli": 3},
    )
    assert text.startswith("**#welcome** configuration:")
    assert "**Tools** (default grant" in text
    assert "no channel override" in text
    for row in slash.ROWS:  # everything default-granted, bold + one-liner
        assert f"- **{row}** — {slash.ROW_DESCRIPTIONS[row]}" in text
    assert "**Off:** mcp:gcal, mcp:linear" in text
    assert "**Runtime plugins on this agent's host**" in text
    assert "cli (3 tools)" in text
    # Gate off → the status says the grants aren't enforced yet.
    assert "`advanced_tool_controls` is off" in text
    assert "`/fil-config` tools change turns it on" in text
    # Wake and guidance lines with defaults.
    assert "**Wake:** mention (default)" in text
    assert "**Guidance:** none" in text
    # Backticked examples for all three subforms, plus the typo note.
    assert "- `/fil-config #welcome gcal off`" in text
    assert "- `/fil-config #welcome wake all`" in text
    assert "- `/fil-config #welcome guidance Be brief.`" in text
    assert "Typos are fine" in text


def test_render_channel_show_override_wake_pin_and_guidance():
    text = slash.render_channel_show(
        room_id="!welcome:fil",
        channel_name="welcome",
        capability_policy={
            "per_channel": {"!welcome:fil": ["read_history", "mcp:linear"]}
        },
        feature_flags={"advanced_tool_controls": True},
        wake_policy={"per_channel": {"!welcome:fil": {"reactive_wake": "all"}}},
        channel_instructions={"!welcome:fil": "Be  brief.\nEscalate billing."},
        mcp_servers={"linear": 62, "gcal": None},
    )
    assert "**Tools** (channel override):" in text
    assert (
        f"- **read_history** — {slash.ROW_DESCRIPTIONS['read_history']}" in text
    )
    assert "- **mcp:linear** — MCP server (62 tools)" in text
    assert "**Off:** post, directory, escalate, mcp:gcal" in text
    # Gate already on → no "not enforced" note.
    assert "is off" not in text
    # A pinned wake mode shows without the default marker.
    assert "**Wake:** all — every message there wakes me" in text
    assert "(default)" not in text
    # Guidance verbatim (interior spacing intact), blockquoted per line.
    assert "**Guidance:** (28 chars)" in text
    assert "> Be  brief.\n> Escalate billing." in text


def test_render_channel_show_empty_override_and_unknown_grants():
    text = slash.render_channel_show(
        room_id="!welcome:fil",
        channel_name="welcome",
        capability_policy={"per_channel": {"!welcome:fil": []}},
        feature_flags={"advanced_tool_controls": True},
        wake_policy={},
        channel_instructions={},
    )
    assert "- none (baseline only)" in text
    text = slash.render_channel_show(
        room_id="!welcome:fil",
        channel_name="welcome",
        capability_policy={
            "per_channel": {"!welcome:fil": ["calendar", "mcp:gone"]}
        },
        feature_flags={"advanced_tool_controls": True},
        wake_policy={},
        channel_instructions={},
    )
    # "calendar" names no custom bundle: fail-closed expansion grants
    # nothing, and the display must say so rather than dress the typo up
    # as a working bundle.
    assert "- **calendar** — unrecognized (grants nothing" in text
    assert "- **calendar** — custom bundle" not in text
    assert "- **mcp:gone** — MCP server — not currently connected" in text


# ── /fil-config tools list rendering ─────────────────────────────────


def test_render_tools_list_shows_full_catalog():
    text = slash.render_tools_list(
        CHANNELS, {"linear": 62, "gcal": None}, {"spotify": 7, "terminal": 1}
    )
    for row in slash.ROWS:
        # Bold row name bullets with the one-line copy.
        assert f"- **{row}** — {slash.ROW_DESCRIPTIONS[row]}" in text
    assert "**Built-in bundles:**" in text
    assert "`mcp:linear` (62 tools)" in text
    assert "mcp:gcal" in text
    # The runtime-plugin section spells out the semantic and the counts
    # (singular/plural per count).
    assert "**Runtime plugins on this agent's host**" in text
    assert (
        "available in the backchannel; blocked in shared channels while "
        "enforcement is on: spotify (7 tools), terminal (1 tool)" in text
    )
    # The change example uses the new form and a real channel.
    assert "`/fil-config #welcome linear off`" in text


def test_render_tools_list_plugin_names_without_counts():
    text = slash.render_tools_list(CHANNELS, {}, ["cli"])
    assert "**Connected MCP servers:** none" in text
    assert "**Runtime plugins on this agent's host**" in text
    assert "cli" in text
    assert "(0 tools)" not in text


def test_render_tools_list_without_plugins():
    text = slash.render_tools_list(CHANNELS, {}, {})
    assert "Runtime plugins" not in text


# ── /fil-config <channel> guidance rendering ─────────────────────────


def test_render_guidance_show_verbatim_blockquote():
    text = slash.render_guidance_show(
        room_id="!welcome:fil",
        channel_name="welcome",
        channel_instructions={"!welcome:fil": "Be  brief.\nEscalate billing."},
    )
    assert text.startswith("**#welcome** guidance (28 chars):")
    # Verbatim (interior spacing intact), quoted line by line.
    assert "> Be  brief.\n> Escalate billing." in text
    assert "`/fil-config #welcome guidance <text…>`" in text
    assert "`/fil-config #welcome guidance clear`" in text


def test_render_guidance_show_when_unset():
    text = slash.render_guidance_show(
        room_id="!welcome:fil",
        channel_name="welcome",
        channel_instructions={},
    )
    assert "No guidance is set for **#welcome**." in text
    assert "`/fil-config #welcome guidance <text…>`" in text


# ── /fil-config feature rendering ────────────────────────────────────


def test_render_feature_list_shows_states_and_change_example():
    text = slash.render_feature_list(
        features=reactive.KNOWN_FEATURES,
        feature_flags={"slash_commands": True},
    )
    assert text.startswith("**Features:**")
    # One bullet per known feature: bold name, one-line summary, state.
    for name, description in reactive.KNOWN_FEATURES.items():
        summary = str(description).split(". ")[0].rstrip(".")
        glyph, state = ("✅", "on") if name == "slash_commands" else ("⬜", "off")
        assert f"- {glyph} **{name}** — {state} — {summary}" in text
    assert "✅ **slash_commands** — on —" in text
    # advanced_tool_controls not set in the flags
    assert "⬜ **advanced_tool_controls** — off —" in text
    assert "`/fil-config feature slash_commands off`" in text
    assert "`/fil-config feature <name> <on|off>`" in text


def test_render_feature_show_state_and_full_description():
    text = slash.render_feature_show(
        feature="slash_commands",
        features=reactive.KNOWN_FEATURES,
        feature_flags={"slash_commands": True},
    )
    assert text.startswith("**slash_commands** is **on**.")
    # The full description, not just the first sentence.
    assert reactive.KNOWN_FEATURES["slash_commands"].strip() in text
    assert "`/fil-config feature slash_commands <on|off>`" in text
    text = slash.render_feature_show(
        feature="slash_commands",
        features=reactive.KNOWN_FEATURES,
        feature_flags={},
    )
    assert text.startswith("**slash_commands** is **off**.")


# ── Markdown injection (member-editable names render inert) ──────────


def test_channel_names_are_markdown_escaped_in_replies():
    mutation = slash.apply_tools(
        _tools("post", "revoke", name="[urgent](https://x)"), {}, {}
    )
    assert "[urgent](https://x)" not in mutation.reply  # no live link
    assert "\\[urgent\\]\\(https://x\\)" in mutation.reply
    text = slash.render_channel_show(
        room_id="!evil:fil",
        channel_name="**bold** _sneaky_",
        capability_policy={},
        feature_flags={"advanced_tool_controls": True},
        wake_policy={},
        channel_instructions={},
    )
    assert "**bold**" not in text.split(" configuration")[0]
    assert "\\*\\*bold\\*\\*" in text
    assert "\\_sneaky\\_" in text


# ── Effective row state (grant expansion) in the tools block ─────────


def test_channel_show_alias_grant_shows_covered_rows_via_alias():
    text = slash.render_channel_show(
        room_id="!welcome:fil",
        channel_name="welcome",
        capability_policy={"per_channel": {"!welcome:fil": ["messaging"]}},
        feature_flags={"advanced_tool_controls": True},
        wake_policy={},
        channel_instructions={},
        mcp_servers={"linear": 62},
    )
    # The deprecated alias's member list is frozen verbatim, so it fully
    # covers only rows that haven't grown past it: directory yes, but
    # read_history (list_reactions) and post (quote, rechat) now carry
    # tools the alias never granted — those rows are OFF, matching what
    # enforcement actually permits.
    assert (
        f"- **directory** — {slash.ROW_DESCRIPTIONS['directory']} "
        "(via messaging)" in text
    )
    # escalate's member tools are not covered by messaging → off.
    assert "**escalate**" not in text
    assert "**Off:** read_history, post, escalate, mcp:linear" in text
    # The alias itself is represented by its rows, not a standalone bullet.
    assert "- **messaging**" not in text


def test_channel_show_custom_bundle_expansion_covers_rows_and_mcp():
    text = slash.render_channel_show(
        room_id="!welcome:fil",
        channel_name="welcome",
        capability_policy={
            "per_channel": {"!welcome:fil": ["combo"]},
            "bundles": {"combo": ["@escalate", "@mcp:linear"]},
        },
        feature_flags={"advanced_tool_controls": True},
        wake_policy={},
        channel_instructions={},
        mcp_servers={"linear": 62, "gcal": None},
    )
    assert (
        f"- **escalate** — {slash.ROW_DESCRIPTIONS['escalate']} (via combo)"
        in text
    )
    assert "- **combo** — custom bundle" in text
    # mcp:linear is covered through the bundle → not in the Off list.
    assert "**Off:** read_history, post, directory, mcp:gcal" in text


# ── Parser reach: names the grammar must not lose ────────────────────


def test_four_word_channel_resolves():
    # The n-gram width follows the longest known name, so no real channel
    # is out of reach.
    channels = [*CHANNELS, ("!csw:fil", "customer success west coast")]
    res = parse(
        "/fil-config customer success west coast show", channels=channels
    )
    assert isinstance(res, slash.ChannelShow)
    assert res.room_id == "!csw:fil"
    res = parse(
        "/fil-config customer success west coast linear off",
        channels=channels,
    )
    assert isinstance(res, slash.ToolsCommand)
    assert res.room_id == "!csw:fil"


def test_channel_named_with_filler_word_resolves():
    # Fillers are skipped lazily, AFTER the channel n-gram pass: a channel
    # whose name contains one ("The Lounge") still resolves.
    channels = [*CHANNELS, ("!lounge:fil", "The Lounge")]
    res = parse("/fil-config The Lounge show", channels=channels)
    assert isinstance(res, slash.ChannelShow)
    assert res.room_id == "!lounge:fil"
    res = parse("/fil-config the lounge linear off", channels=channels)
    assert isinstance(res, slash.ToolsCommand)
    assert res.room_id == "!lounge:fil"
    # Fillers used as fillers still skip.
    res = parse("/fil-config in the welcome linear off", channels=channels)
    assert isinstance(res, slash.ToolsCommand)
    assert res.room_id == "!welcome:fil"


def test_guidance_channel_containing_filler_word():
    channels = [*CHANNELS, ("!lounge:fil", "The Lounge")]
    res = parse(
        "/fil-config The Lounge guidance Be gentle.", channels=channels
    )
    assert isinstance(res, slash.GuidanceCommand)
    assert res.room_id == "!lounge:fil"
    assert res.text == "Be gentle."


def test_wake_two_modes_asks_instead_of_picking():
    # "wake all off" names two modes; the parser must ask, never pick.
    res = parse("/fil-config welcome wake all off")
    assert isinstance(res, slash.Unparsed)
    assert "all" in res.problem and "off" in res.problem
    # An affirming "on" alongside a mode is not a conflict.
    res = parse("/fil-config welcome wake all on")
    assert isinstance(res, slash.WakeCommand)
    assert res.mode == "all"


def test_channel_named_after_command_words_asks():
    channels = [*CHANNELS, ("!wa:fil", "wake all")]
    res = parse("/fil-config wake all welcome", channels=channels)
    assert isinstance(res, slash.Ambiguous)
    assert "wake all" in res.token
    # Both readings stay reachable: # forces the channel…
    res = parse("/fil-config #wake all show", channels=channels)
    assert isinstance(res, slash.ChannelShow)
    assert res.room_id == "!wa:fil"
    # …and channel-first uses the command words.
    res = parse("/fil-config welcome wake all", channels=channels)
    assert isinstance(res, slash.WakeCommand)
    assert res.room_id == "!welcome:fil"


def test_duplicate_channel_names_are_distinguishable_candidates():
    channels = [("!a:fil", "general"), ("!b:fil", "general")]
    res = parse("/fil-config general show", channels=channels)
    assert isinstance(res, slash.Ambiguous)
    assert any("!a:fil" in c for c in res.candidates)
    assert any("!b:fil" in c for c in res.candidates)
    # The room id always selects exactly.
    res = parse("/fil-config !b:fil show", channels=channels)
    assert isinstance(res, slash.ChannelShow)
    assert res.room_id == "!b:fil"


def test_example_channel_is_not_markdown_escaped():
    # Examples render inside code spans, where escaping doesn't apply — a
    # backslash would end up in the command the principal copies back.
    assert (
        slash._example_channel([("!g:fil", "general_chat")])
        == "#general_chat"
    )
    text = slash.help_config([("!g:fil", "general_chat")])
    assert "#general_chat" in text
    assert "\\_" not in text


# ── Display truthfulness: shadowing, split coverage, unknown grants ──


def test_channel_show_custom_bundle_shadowing_a_row_name():
    # A custom bundle named "post" replaces the builtin meaning (custom wins
    # on collision in the enforcers) — the row must not show as on.
    text = slash.render_channel_show(
        room_id="!welcome:fil",
        channel_name="welcome",
        capability_policy={
            "per_channel": {"!welcome:fil": ["post"]},
            "bundles": {"post": ["some_other_tool"]},
        },
        feature_flags={"advanced_tool_controls": True},
        wake_policy={},
        channel_instructions={},
    )
    assert f"- **post** — {slash.ROW_DESCRIPTIONS['post']}" not in text
    assert "- **post** — custom bundle" in text
    assert "post" in text.split("**Off:**")[1]


def test_channel_show_row_covered_by_split_bundles_names_both():
    members = list(slash.BUNDLE_MEMBERS["post"])
    half = len(members) // 2 or 1
    text = slash.render_channel_show(
        room_id="!welcome:fil",
        channel_name="welcome",
        capability_policy={
            "per_channel": {"!welcome:fil": ["a", "b"]},
            "bundles": {"a": members[:half], "b": members[half:]},
        },
        feature_flags={"advanced_tool_controls": True},
        wake_policy={},
        channel_instructions={},
    )
    assert (
        f"- **post** — {slash.ROW_DESCRIPTIONS['post']} (via a, b)" in text
    )


def test_channel_show_alias_named_custom_bundle_is_a_bundle():
    # A custom bundle may shadow a deprecated alias name; it renders as the
    # bundle it is, not silently as the alias's rows.
    text = slash.render_channel_show(
        room_id="!welcome:fil",
        channel_name="welcome",
        capability_policy={
            "per_channel": {"!welcome:fil": ["messaging"]},
            "bundles": {
                "messaging": ["get_user_profile", "search_members"]
            },
        },
        feature_flags={"advanced_tool_controls": True},
        wake_policy={},
        channel_instructions={},
    )
    assert "- **messaging** — custom bundle" in text
    assert (
        f"- **directory** — {slash.ROW_DESCRIPTIONS['directory']} "
        "(via messaging)" in text
    )
    assert "- **post**" not in text
    assert "- **read_history**" not in text


def test_code_span_examples_use_raw_typeable_channel_name():
    # Example commands render inside code spans, where escape backslashes
    # display verbatim — the copied command must carry the RAW name. Prose
    # headings keep the escaped form. Backticks are dropped from the ref so
    # a hostile name can't terminate the span it renders in.
    text = slash.render_channel_show(
        room_id="!r:fil",
        channel_name="my_room",
        capability_policy={},
        feature_flags={"advanced_tool_controls": True},
        wake_policy={},
        channel_instructions={},
    )
    assert "`/fil-config #my_room post off`" in text
    assert "#my\\_room post" not in text
    assert "**#my\\_room** configuration" in text  # prose stays escaped
    text = slash.render_guidance_show(
        room_id="!r:fil",
        channel_name="my_room",
        channel_instructions={},
    )
    assert "`/fil-config #my_room guidance <text…>`" in text
    assert "\\_" not in text.split("Set it with")[1]
    # A backtick in the name can't break out of the code span.
    text = slash.render_channel_show(
        room_id="!r:fil",
        channel_name="x` **live** `y",
        capability_policy={},
        feature_flags={"advanced_tool_controls": True},
        wake_policy={},
        channel_instructions={},
    )
    for line in text.splitlines():
        if line.startswith("- `/fil-config"):
            assert line.count("`") == 2, line


def test_channel_named_guidance_team_takes_guidance():
    # A leading channel name owns its tokens before the keyword scan: the
    # first word of "Guidance Team" must not read as the guidance keyword.
    channels = [*CHANNELS, ("!gt:fil", "Guidance Team")]
    res = parse(
        "/fil-config Guidance Team guidance Be nice.", channels=channels
    )
    assert isinstance(res, slash.GuidanceCommand)
    assert res.room_id == "!gt:fil"
    assert res.text == "Be nice."
    # Keyword-first spelling still works…
    res = parse("/fil-config guidance welcome Be brief.", channels=channels)
    assert isinstance(res, slash.GuidanceCommand)
    assert res.room_id == "!welcome:fil"
    # …and the channel's non-guidance forms are untouched.
    res = parse("/fil-config Guidance Team show", channels=channels)
    assert isinstance(res, slash.ChannelShow)
    assert res.room_id == "!gt:fil"


def test_bundle_prefix_forces_grammar_colliding_bundle():
    # A bundle named "wake" collides with the command word; bare use asks,
    # the bundle: prefix forces the target reading — no dead ends.
    res = parse("/fil-config #welcome wake on", bundles=["wake"])
    assert isinstance(res, slash.Ambiguous)
    res = parse("/fil-config #welcome bundle:wake on", bundles=["wake"])
    assert isinstance(res, slash.ToolsCommand)
    assert res.target == "wake"
    assert res.verb == "grant"
    # The prefix is matching-time syntax only — unknown names still miss.
    res = parse("/fil-config #welcome bundle:nosuch on", bundles=["wake"])
    assert isinstance(res, slash.Unparsed)


def test_mcp_tool_counts_pluralize():
    text = slash.render_tools_list(CHANNELS, {"solo": 1, "many": 3}, {})
    assert "`mcp:solo` (1 tool)" in text
    assert "`mcp:many` (3 tools)" in text
    text = slash.render_channel_show(
        room_id="!welcome:fil",
        channel_name="welcome",
        capability_policy={"per_channel": {"!welcome:fil": ["mcp:solo"]}},
        feature_flags={"advanced_tool_controls": True},
        wake_policy={},
        channel_instructions={},
        mcp_servers={"solo": 1},
    )
    assert "(1 tool)" in text
    assert "(1 tools)" not in text


def test_single_word_channel_named_guidance_asks_not_guesses():
    channels = [*CHANNELS, ("!g:fil", "guidance")]
    # Keyword position + exact channel name = a genuine fork: ask.
    res = parse("/fil-config guidance", channels=channels)
    assert isinstance(res, slash.Ambiguous)
    # Both readings stay reachable: # forces the channel…
    res = parse("/fil-config #guidance", channels=channels)
    assert isinstance(res, slash.ChannelShow)
    assert res.room_id == "!g:fil"
    # …and channel-first keeps the keyword form, unambiguous.
    res = parse("/fil-config welcome guidance Be brief.", channels=channels)
    assert isinstance(res, slash.GuidanceCommand)
    assert res.room_id == "!welcome:fil"


def test_single_word_channel_named_feature_asks_not_guesses():
    channels = [*CHANNELS, ("!f:fil", "feature")]
    res = parse("/fil-config feature list", channels=channels)
    assert isinstance(res, slash.Ambiguous)
    res = parse("/fil-config #feature", channels=channels)
    assert isinstance(res, slash.ChannelShow)
    assert res.room_id == "!f:fil"
    # Without such a channel, feature routes exactly as before.
    res = parse("/fil-config feature list")
    assert isinstance(res, slash.FeatureList)


def test_deep_include_chain_expands_without_recursion_error():
    # The include graph is config-document data: a deep chain must render,
    # not RecursionError, and a diamond of shared includes must not blow up
    # exponentially.
    depth = 5000
    bundles = {f"b{i}": [f"@b{i + 1}", f"t{i}"] for i in range(depth)}
    bundles[f"b{depth}"] = ["leaf_tool"]
    names, tools = slash._expand_grant("b0", bundles)
    assert "leaf_tool" in tools
    assert "t0" in tools and f"t{depth - 1}" in tools
    assert f"b{depth}" in names
    # Diamond: both arms share a base; expansion stays linear and complete.
    diamond = {
        "top": ["@left", "@right"],
        "left": ["@base", "l"],
        "right": ["@base", "r"],
        "base": ["deep"],
    }
    names, tools = slash._expand_grant("top", diamond)
    assert tools == {"l", "r", "deep"}


def test_md_escape_neutralizes_pre_escaped_names():
    # A name that ARRIVES with backslashes must not eat the inserted escape:
    # raw \[urgent\](https://evil) has to render inert, not as a live link.
    escaped = slash._md_escape("\\[urgent\\](https://evil)")
    assert "\\\\" in escaped  # the original backslash is itself escaped
    assert "\\\\\\[" in escaped  # ...so the bracket keeps its own escape
    mutation = slash.apply_tools(
        _tools("post", "revoke", name="\\[urgent\\](https://x)"), {}, {}
    )
    assert "\\[urgent\\](https://x)" not in mutation.reply
