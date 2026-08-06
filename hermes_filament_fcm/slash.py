"""Deterministic slash-command layer for the principal's backchannel.

The commands live in the ``/fil-`` namespace (``/fil-help``, ``/fil-tools``,
…). A backchannel message whose stripped body starts with ``/fil-``
(case-insensitive prefix, see ``is_fil_command``) is a command for the
*plugin*, not the model: the adapter intercepts it before any LLM dispatch
and answers from this module alone. A ``/fil-`` message must never reach
inference — when parsing fails, the reply is help text or a clarifying
question, not a model turn. That makes the command surface exact, auditable,
and free of prompt-shaped surprises. Any *other* leading-``/`` message is not
ours: other slash namespaces belong to other software, so the adapter lets
those fall through to the normal control-plane LLM path.

Replies are markdown (Filament renders it): command names and examples in
backticks, section headings bold, lists as ``-`` bullets, resolved names in
confirmation echoes bold. Formatting, not padding — replies stay compact.

Parsing is token classification, not positional grammar: after the command
word, each remaining token is matched against closed vocabularies — a channel
(``#name`` / ``name`` / room id, resolved against the server-attributed
channel list the caller passes in), a target (capability rows, bundle names,
deprecated aliases, and ``mcp:<server>`` auto-bundles matchable with or
without the prefix), and a verb (grant/revoke synonyms). Token order is free;
filler words are skipped. Matching is fuzzy (``difflib``, cutoff
``FUZZY_CUTOFF``), but never a silent guess: a near-tie or a cross-vocabulary
collision comes back as an ``Ambiguous`` result carrying the candidates, and
the adapter asks — "did you mean …?".

Trust: the only untrusted strings this module interpolates into replies are
channel names (server data, but display-editable) and the principal's own
tokens. Channel names are sanitized on ingestion (``_sanitize``, mirroring the
adapter's ``_sanitize_meta``); unresolved tokens are quoted plainly inside the
error reply, never wrapped in trusted framing.

Stdlib-only, side-effect-free: parsing returns structured results, the
``apply_*`` compilers turn them into new store documents plus a confirmation
echo, and the adapter owns all I/O (store writes, ``write_back``, sending the
reply). That keeps the whole product surface unit-testable without Hermes.
"""

import difflib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

# ── Vocabularies ─────────────────────────────────────────────────────
#
# ROWS / DEPRECATED_ALIASES / MCP_PREFIX / FEATURE_ADVANCED_TOOL_CONTROLS
# mirror reactive.py's DEFAULT_CAPABILITIES, the deprecated alias bundles in
# BUILTIN_BUNDLES, MCP_BUNDLE_PREFIX, and FEATURE_ADVANCED_TOOL_CONTROLS.
# Deliberately restated (not imported) so this module stays loadable
# standalone; tests pin the two modules together.

ROWS: tuple[str, ...] = ("read_history", "post", "directory", "escalate")
DEPRECATED_ALIASES: tuple[str, ...] = ("messaging", "readonly")
MCP_PREFIX = "mcp:"
FEATURE_ADVANCED_TOOL_CONTROLS = "advanced_tool_controls"

# One-line row copy, reused wherever a row is displayed (help, echoes). Keep
# in sync with the member lists in reactive.BUILTIN_BUNDLES.
ROW_DESCRIPTIONS: dict[str, str] = {
    "read_history": (
        "read the channel: history, threads, search, mentions, attachments"
    ),
    "post": "write into the channel: post, reply in threads, react/unreact",
    "directory": "look up who people are",
    "escalate": "reach your principal (message_principal)",
}

# The command namespace. Only bodies carrying this prefix are ours; other
# slash namespaces belong to other software and must not be swallowed.
PREFIX = "/fil-"

GRANT_WORDS: tuple[str, ...] = ("enable", "on", "grant", "allow")
REVOKE_WORDS: tuple[str, ...] = ("disable", "off", "revoke", "block", "deny")
WAKE_MODES: tuple[str, ...] = ("mention", "all", "off")
FILLER_WORDS: frozenset[str] = frozenset({"in", "for", "the"})
COMMANDS: tuple[str, ...] = (
    "help",
    "config",
    "tools",
    "wake",
    "guidance",
    "feature",
)

# The tools command also accepts a bare "list" (the full tool catalog);
# it is matched exactly like any other token (exact + fuzzy).
LIST_WORD = "list"

# Per-channel controls are meaningless for the control plane, so the
# backchannel is excluded from the channel vocabulary. A token that would
# have resolved to it gets this note instead of the generic unknown-channel
# error.
BACKCHANNEL_NOTE = (
    "The backchannel isn't a shared channel — these controls apply to "
    "shared channels only."
)

FUZZY_CUTOFF = 0.75
# Two candidates whose match scores are within this margin are a near-tie:
# the parser reports both instead of picking one. A wrong silent pick would
# mutate policy the principal didn't ask for; a clarifying question costs one
# message.
AMBIGUITY_MARGIN = 0.08

USAGE: dict[str, str] = {
    "config": "`/fil-config show`",
    "tools": "`/fil-tools <channel> <tool-or-bundle> <on|off>` "
    "(e.g. `/fil-tools #welcome linear off`), or `/fil-tools <channel>` "
    "for that channel's current tool status",
    "wake": "`/fil-wake <channel> <mention|all|off>` "
    "(e.g. `/fil-wake #general all`)",
    "guidance": "`/fil-guidance <channel> <text…|clear>` "
    "(e.g. `/fil-guidance #welcome Be brief.`), or "
    "`/fil-guidance <channel>` to show the current guidance",
    "feature": "`/fil-feature <name> <on|off>` "
    "(e.g. `/fil-feature advanced_tool_controls on`)",
}


def is_fil_command(body: str | None) -> bool:
    """True when a message body enters the deterministic slash layer: its
    stripped form starts with the ``/fil-`` namespace prefix
    (case-insensitive). Any other leading-``/`` message belongs to some other
    software's slash namespace and must fall through to the normal LLM path —
    the adapter's intercept boundary is exactly this predicate."""
    return (body or "").strip().lower().startswith(PREFIX)


def _sanitize(value: str, limit: int = 80) -> str:
    """Flatten a string for safe single-line interpolation into a reply:
    collapse whitespace, drop non-printables, truncate. Mirrors the adapter's
    ``_sanitize_meta`` (restated so this module stays standalone-loadable)."""
    if not value:
        return ""
    flat = re.sub(r"\s+", " ", str(value)).strip()
    flat = "".join(ch for ch in flat if ch.isprintable())
    return flat[:limit]


# ── Parse results ────────────────────────────────────────────────────


@dataclass(frozen=True)
class HelpRequest:
    """``/help``, ``/<command> help``, or a bare command with nothing usable —
    the adapter answers with the matching help text (see ``help_for``)."""

    command: str | None  # None → the index


@dataclass(frozen=True)
class ShowConfig:
    """``/config`` / ``/config show`` — the adapter renders the current
    document with ``render_config_show``."""


@dataclass(frozen=True)
class ToolsCommand:
    room_id: str
    channel_name: str  # sanitized; "" when the channel is unnamed
    target: str  # canonical grant name (row/alias/bundle or "mcp:<server>")
    verb: str  # "grant" | "revoke"


@dataclass(frozen=True)
class ToolsStatus:
    """``/fil-tools <channel>`` with no target/verb — the adapter answers
    with the channel's current tool status (``render_tools_status``)."""

    room_id: str
    channel_name: str  # sanitized; "" when the channel is unnamed


@dataclass(frozen=True)
class ToolsList:
    """``/fil-tools list`` — the adapter answers with the full tool catalog
    (``render_tools_list``)."""


@dataclass(frozen=True)
class WakeCommand:
    room_id: str
    channel_name: str
    mode: str  # "mention" | "all" | "off"


@dataclass(frozen=True)
class GuidanceCommand:
    room_id: str
    channel_name: str
    text: str | None  # None → clear


@dataclass(frozen=True)
class GuidanceShow:
    """``/fil-guidance <channel>`` with no text — the adapter answers with
    the current guidance (``render_guidance_show``)."""

    room_id: str
    channel_name: str


@dataclass(frozen=True)
class FeatureCommand:
    feature: str
    enabled: bool


@dataclass(frozen=True)
class Ambiguous:
    """A token matched more than one candidate too closely to call. The
    candidates ride along so the adapter can ask instead of guessing."""

    command: str | None
    token: str  # sanitized
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class Unparsed:
    """The message was a slash command but didn't resolve to an action.
    ``problem`` is a plain-language explanation; ``render_reply`` appends the
    command's usage line (or the full index when no command was recognized)."""

    command: str | None
    problem: str


# ── Token classification ─────────────────────────────────────────────


@dataclass(frozen=True)
class _Entry:
    """One matchable vocabulary item: ``key`` is the lowercase string a token
    is compared against, ``canonical`` the resolved value, ``display`` what a
    clarifying reply shows. ``exact_only`` marks keys (room ids) that make no
    sense to fuzzy-match."""

    slot: str
    key: str
    canonical: object
    display: str
    exact_only: bool = False
    # Plain form for prose ("I understood channel #welcome …"); ``display``
    # may carry a disambiguating suffix for candidate lists.
    label: str = ""

    @property
    def prose(self) -> str:
        return self.label or self.display


def _mcp_names(mcp_servers: object) -> list[str]:
    """Server names from either a plain sequence or a name→tool-count
    mapping (the adapter passes counts so help text can show them)."""
    if isinstance(mcp_servers, Mapping):
        return [str(k) for k in mcp_servers]
    if isinstance(mcp_servers, Iterable) and not isinstance(mcp_servers, str):
        return [str(s) for s in mcp_servers]
    return []


def _channel_entries(
    channels: Sequence[tuple[str, str]],
) -> list[_Entry]:
    entries: list[_Entry] = []
    for room_id, name in channels:
        clean = _sanitize(name or "")
        label = f"#{clean}" if clean else str(room_id)
        display = f"{label} (channel)" if clean else label
        canonical = (str(room_id), clean)
        if clean:
            entries.append(
                _Entry("channel", clean.lower(), canonical, display, label=label)
            )
        entries.append(
            _Entry(
                "channel", str(room_id).lower(), canonical, display, True, label
            )
        )
    return entries


def _target_entries(
    mcp_servers: object, bundles: Iterable[str]
) -> list[_Entry]:
    entries = [
        _Entry("target", name, name, name)
        for name in ROWS + DEPRECATED_ALIASES
    ]
    for bundle in bundles or ():
        text = str(bundle)
        # mcp:-prefixed names can't name custom bundles (reserved); rows and
        # aliases are already present.
        low = text.lower()
        if text and not low.startswith(MCP_PREFIX) and low not in {
            e.key for e in entries
        }:
            entries.append(_Entry("target", low, text, text))
    for server in _mcp_names(mcp_servers):
        canonical = f"{MCP_PREFIX}{server}"
        entries.append(_Entry("target", canonical.lower(), canonical, canonical))
        # The bare server name matches too: "gcal" → "mcp:gcal".
        entries.append(_Entry("target", str(server).lower(), canonical, canonical))
    return entries


def _verb_entries() -> list[_Entry]:
    return [_Entry("verb", w, "grant", w) for w in GRANT_WORDS] + [
        _Entry("verb", w, "revoke", w) for w in REVOKE_WORDS
    ]


def _dedupe(entries: Iterable[_Entry]) -> list[_Entry]:
    seen: set[tuple[str, object]] = set()
    out: list[_Entry] = []
    for e in entries:
        k = (e.slot, e.canonical)
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def _score(
    token: str, entries: Sequence[_Entry]
) -> tuple[_Entry | None, tuple[str, ...]]:
    """Classify one token against the still-open vocabulary entries.

    Returns ``(entry, ())`` on a confident unique match; ``(None,
    candidates)`` when the best matches are too close to call (within
    ``AMBIGUITY_MARGIN``, or several exact hits across vocabularies); ``(None,
    ())`` when nothing clears ``FUZZY_CUTOFF``. Shape-forced tokens
    (``#…``/``!…`` → channel, ``mcp:…`` → target) only ever match their own
    vocabulary, so ``#post`` is a channel even where a bundle ``post`` exists.
    """
    forced: str | None = None
    if token.startswith(("#", "!")):
        forced = "channel"
    elif token.lower().startswith(MCP_PREFIX):
        forced = "target"
    if forced is not None:
        entries = [e for e in entries if e.slot == forced]
    norm = (token[1:] if token.startswith("#") else token).lower()

    exact = _dedupe(e for e in entries if e.key == norm)
    if len(exact) == 1:
        return exact[0], ()
    if len(exact) > 1:
        return None, tuple(e.display for e in exact)

    best_by: dict[tuple[str, object], tuple[float, _Entry]] = {}
    for e in entries:
        if e.exact_only:
            continue
        ratio = difflib.SequenceMatcher(None, norm, e.key).ratio()
        if ratio < FUZZY_CUTOFF:
            continue
        k = (e.slot, e.canonical)
        if k not in best_by or ratio > best_by[k][0]:
            best_by[k] = (ratio, e)
    if not best_by:
        return None, ()
    ranked = sorted(best_by.values(), key=lambda p: p[0], reverse=True)
    best = ranked[0][0]
    tied = [e for r, e in ranked if best - r < AMBIGUITY_MARGIN]
    if len(tied) == 1:
        return tied[0], ()
    return None, tuple(e.display for e in tied)


def _match_word(
    word: str, vocab: Sequence[str]
) -> tuple[str | None, tuple[str, ...]]:
    """``_score`` for a flat word list (command words, ``config show``)."""
    entry, candidates = _score(
        word, [_Entry("word", v.lower(), v, v) for v in vocab]
    )
    return (str(entry.canonical) if entry else None), candidates


_TOKEN_RE = re.compile(r"\S+")


def _tokenize(rest: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(rest)]


def _fill_slots(
    tokens: Sequence[tuple[str, int, int]],
    entries: Sequence[_Entry],
    command: str,
    expects: str,
    labels: Mapping[str, str],
    required: Sequence[str],
    complete: Sequence[Sequence[str]] = (),
    backchannel_entries: Sequence[_Entry] = (),
):
    """Order-free classification: assign each non-filler token to an open
    slot. Returns ``(slots, None)`` on success — including a possibly
    *incomplete* ``slots`` dict, which the caller turns into help (nothing
    resolved) or a "still need …" reply — or ``(None, result)`` when a token
    was ambiguous or unplaceable. A slot set exactly matching one of
    ``complete`` is also a success even though ``required`` slots are
    missing (e.g. a bare channel means "show status", not a mutation).
    ``backchannel_entries`` is the excluded backchannel's own vocabulary: an
    unmatched token that would have resolved there answers with
    ``BACKCHANNEL_NOTE`` instead of the generic unknown-token error."""
    slots: dict[str, _Entry] = {}
    for text, _start, _end in tokens:
        if text.lower() in FILLER_WORDS:
            continue
        open_entries = [e for e in entries if e.slot not in slots]
        entry, candidates = _score(text, open_entries)
        if candidates:
            return None, Ambiguous(
                command=command, token=_sanitize(text), candidates=candidates
            )
        if entry is None:
            if backchannel_entries:
                bc_entry, bc_candidates = _score(text, backchannel_entries)
                if bc_entry is not None or bc_candidates:
                    return None, Unparsed(
                        command=command, problem=BACKCHANNEL_NOTE
                    )
            return None, Unparsed(
                command=command,
                problem=f'I couldn\'t match "{_sanitize(text)}" to {expects}.',
            )
        slots[entry.slot] = entry
    if not slots:
        return slots, HelpRequest(command)
    if any(set(slots) == set(combo) for combo in complete):
        return slots, None
    missing = [s for s in required if s not in slots]
    if missing:
        got = ", ".join(
            f"{labels[s]} {slots[s].prose}" for s in required if s in slots
        )
        need = " and ".join(labels[s] for s in missing)
        return None, Unparsed(
            command=command,
            problem=f"I understood {got}, but still need {need}.",
        )
    return slots, None


# ── Parsing ──────────────────────────────────────────────────────────


def parse(
    body: str,
    *,
    channels: Sequence[tuple[str, str]],
    mcp_servers: object = (),
    bundles: Iterable[str] = (),
    features: Mapping[str, str] | None = None,
    backchannel: tuple[str, str] | None = None,
):
    """Parse one backchannel ``/fil-`` message into a structured result.

    ``channels`` is the server-attributed ``[(room_id, name), …]`` list (from
    ``list_channels``, with the backchannel already excluded — per-channel
    controls are meaningless for the control plane); ``mcp_servers`` the
    known MCP server names (sequence, or name→tool-count mapping);
    ``bundles`` any extra grantable bundle names (the policy's custom
    bundles); ``features`` the known feature flags (name → description);
    ``backchannel`` the excluded cc room's ``(room_id, name)``, used only to
    answer a command that explicitly targets it with ``BACKCHANNEL_NOTE``.
    Returns one of the result dataclasses above — never raises, never
    returns anything that should reach an LLM.
    """
    features = dict(features or {})
    text = (body or "").strip()
    match = re.match(re.escape(PREFIX) + r"(\S*)", text, re.IGNORECASE)
    if not match:
        return Unparsed(
            command=None, problem=f"That isn't a {PREFIX} command."
        )
    word = match.group(1)
    rest = text[match.end() :]
    if not word:
        return HelpRequest(None)
    command, candidates = _match_word(word, COMMANDS)
    if command is None:
        if candidates:
            return Ambiguous(
                command=None,
                token=_sanitize(word),
                candidates=tuple(f"{PREFIX}{c}" for c in candidates),
            )
        return Unparsed(
            command=None,
            problem=f'Unknown command "{PREFIX}{_sanitize(word)}".',
        )
    if command == "help":
        return HelpRequest(None)
    tokens = _tokenize(rest)
    if tokens and tokens[0][0].lower() == "help":
        return HelpRequest(command)
    bc_entries = _channel_entries([backchannel]) if backchannel else []
    if command == "config":
        return _parse_config(tokens)
    if command == "tools":
        return _parse_tools(tokens, channels, mcp_servers, bundles, bc_entries)
    if command == "wake":
        return _parse_wake(tokens, channels, bc_entries)
    if command == "guidance":
        return _parse_guidance(rest, tokens, channels, bc_entries)
    return _parse_feature(tokens, features)


def _parse_config(tokens: Sequence[tuple[str, int, int]]):
    if not tokens:
        return ShowConfig()
    sub, _candidates = _match_word(tokens[0][0], ("show",))
    if sub == "show":
        return ShowConfig()
    return Unparsed(
        command="config",
        problem=f'"{PREFIX}config {_sanitize(tokens[0][0])}" '
        "isn't something I know.",
    )


def _parse_tools(
    tokens: Sequence[tuple[str, int, int]],
    channels: Sequence[tuple[str, str]],
    mcp_servers: object,
    bundles: Iterable[str],
    backchannel_entries: Sequence[_Entry] = (),
):
    entries = (
        _channel_entries(channels)
        + _target_entries(mcp_servers, bundles)
        + _verb_entries()
        + [_Entry("list", LIST_WORD, LIST_WORD, LIST_WORD)]
    )
    slots, result = _fill_slots(
        tokens,
        entries,
        "tools",
        "a channel, a tool bundle, on/off, or list",
        {"channel": "channel", "target": "bundle", "verb": "on/off"},
        ("channel", "target", "verb"),
        # A bare channel is a complete question, not a truncated mutation:
        # "/fil-tools #welcome" asks what's enabled there; a bare "list"
        # asks for the full catalog.
        complete=(("channel",), ("list",)),
        backchannel_entries=backchannel_entries,
    )
    if result is not None or slots is None:
        return result
    if set(slots) == {"list"}:
        return ToolsList()
    if "list" in slots:
        return Unparsed(
            command="tools",
            problem='"list" stands alone — say `/fil-tools list`.',
        )
    room_id, channel_name = slots["channel"].canonical
    if "target" not in slots:
        return ToolsStatus(room_id=room_id, channel_name=channel_name)
    return ToolsCommand(
        room_id=room_id,
        channel_name=channel_name,
        target=str(slots["target"].canonical),
        verb=str(slots["verb"].canonical),
    )


def _parse_wake(
    tokens: Sequence[tuple[str, int, int]],
    channels: Sequence[tuple[str, str]],
    backchannel_entries: Sequence[_Entry] = (),
):
    entries = _channel_entries(channels) + [
        _Entry("mode", m, m, m) for m in WAKE_MODES
    ]
    slots, result = _fill_slots(
        tokens,
        entries,
        "wake",
        "a channel or a wake mode (mention/all/off)",
        {"channel": "channel", "mode": "wake mode"},
        ("channel", "mode"),
        backchannel_entries=backchannel_entries,
    )
    if result is not None or slots is None:
        return result
    room_id, channel_name = slots["channel"].canonical
    return WakeCommand(
        room_id=room_id,
        channel_name=channel_name,
        mode=str(slots["mode"].canonical),
    )


def _parse_guidance(
    rest: str,
    tokens: Sequence[tuple[str, int, int]],
    channels: Sequence[tuple[str, str]],
    backchannel_entries: Sequence[_Entry] = (),
):
    index = 0
    while index < len(tokens) and tokens[index][0].lower() in FILLER_WORDS:
        index += 1
    if index >= len(tokens):
        return HelpRequest("guidance")
    token, _start, end = tokens[index]
    entry, candidates = _score(token, _channel_entries(channels))
    if candidates:
        return Ambiguous(
            command="guidance", token=_sanitize(token), candidates=candidates
        )
    if entry is None:
        if backchannel_entries:
            bc_entry, bc_candidates = _score(token, backchannel_entries)
            if bc_entry is not None or bc_candidates:
                return Unparsed(command="guidance", problem=BACKCHANNEL_NOTE)
        return Unparsed(
            command="guidance",
            problem=f'I couldn\'t find a channel matching "{_sanitize(token)}".',
        )
    room_id, channel_name = entry.canonical
    # Everything after the channel token is the guidance, verbatim (interior
    # whitespace preserved) — free text is never token-classified, so words
    # like "help" or "off" in it mean nothing special.
    text = rest[end:].strip()
    if not text:
        # A bare channel is a question: show the current guidance there.
        return GuidanceShow(room_id=room_id, channel_name=channel_name)
    if text.lower() == "clear":
        return GuidanceCommand(
            room_id=room_id, channel_name=channel_name, text=None
        )
    return GuidanceCommand(
        room_id=room_id, channel_name=channel_name, text=text
    )


def _parse_feature(
    tokens: Sequence[tuple[str, int, int]],
    features: Mapping[str, str],
):
    entries = [
        _Entry("feature", str(name).lower(), str(name), str(name))
        for name in features
    ] + [
        _Entry("state", w, True, w) for w in GRANT_WORDS
    ] + [
        _Entry("state", w, False, w) for w in REVOKE_WORDS
    ]
    slots, result = _fill_slots(
        tokens,
        entries,
        "feature",
        "a feature name or on/off",
        {"feature": "feature", "state": "on/off"},
        ("feature", "state"),
    )
    if result is not None or slots is None:
        return result
    return FeatureCommand(
        feature=str(slots["feature"].canonical),
        enabled=bool(slots["state"].canonical),
    )


# ── Error / clarification rendering ──────────────────────────────────


def render_reply(result) -> str:
    """The reply text for an ``Ambiguous`` or ``Unparsed`` result. Quotes the
    principal's token plainly (already sanitized at parse time) and shows the
    command's usage — parse failures answer with help, never a model turn."""
    if isinstance(result, Ambiguous):
        options = " or ".join(result.candidates)
        text = f'"{result.token}" is ambiguous — did you mean {options}?'
        usage = USAGE.get(result.command or "")
        if usage:
            text += f"\nUsage: {usage}"
        return text
    if isinstance(result, Unparsed):
        usage = USAGE.get(result.command or "")
        if usage:
            return f"{result.problem}\nUsage: {usage}"
        return f"{result.problem}\n\n{help_index()}"
    raise TypeError(f"no reply rendering for {type(result).__name__}")


# ── Help text ────────────────────────────────────────────────────────


def _example_channel(channels: Sequence[tuple[str, str]]) -> str:
    """A real shared channel for examples (the backchannel never appears —
    the adapter excludes it from ``channels``); a generic placeholder when
    no shared channel is known yet."""
    for _room_id, name in channels:
        clean = _sanitize(name or "")
        if clean:
            return f"#{clean}"
    return "#your-channel"


def _channels_line(channels: Sequence[tuple[str, str]], limit: int = 12) -> str:
    names = []
    for _room_id, name in channels:
        clean = _sanitize(name or "")
        if clean:
            names.append(f"#{clean}")
    if not names:
        return "**Channels:** (none known yet)"
    shown = ", ".join(names[:limit])
    extra = len(names) - limit
    if extra > 0:
        shown += f" (+{extra} more)"
    return f"**Channels:** {shown}"


def help_index() -> str:
    return "\n".join(
        [
            "**Commands:**",
            "- `/fil-config show` — summary of my current configuration",
            "- `/fil-tools <channel> <tool> <on|off>` — grant/revoke tool "
            "bundles per channel (e.g. `/fil-tools #welcome linear off`)",
            "- `/fil-tools <channel>` — what's enabled in that channel "
            "right now",
            "- `/fil-wake <channel> <mention|all|off>` — when that channel "
            "wakes me (e.g. `/fil-wake #general all`)",
            "- `/fil-guidance <channel> <text|clear>` — my standing guidance "
            "there (e.g. `/fil-guidance #welcome Be brief.`)",
            "- `/fil-feature <name> <on|off>` — toggle a feature "
            "(e.g. `/fil-feature advanced_tool_controls on`)",
            "Say `/fil-<command> help` for details (e.g. `/fil-tools help`).",
        ]
    )


def help_config() -> str:
    return (
        "`/fil-config show` — a summary of the current configuration: "
        "per-channel tool grants, wake modes, guidance, and feature flags."
    )


def help_tools(channels: Sequence[tuple[str, str]] = ()) -> str:
    """The compact `/fil-tools` usage (also the `/fil-tools help` reply):
    one-line purpose plus pointers. The full catalog lives under
    `/fil-tools list` (``render_tools_list``)."""
    example = _example_channel(channels)
    return "\n".join(
        [
            "`/fil-tools` — control which tools I may use per shared "
            "channel.",
            "- `/fil-tools list` — all tools & sources",
            "- `/fil-tools <channel>` — what's enabled there",
            "- `/fil-tools <channel> <tool> <on|off>` — change it "
            f"(e.g. `/fil-tools {example} linear off`)",
            "Typos are fine — I'll confirm what I understood.",
        ]
    )


def render_tools_list(
    channels: Sequence[tuple[str, str]] = (),
    mcp_servers: object = (),
    other_sources: Sequence[str] = (),
) -> str:
    """The `/fil-tools list` reply: the full tool catalog — built-in rows
    with their one-liners, connected MCP servers with tool counts, and the
    non-switchable sources."""
    example = _example_channel(channels)
    lines = [
        "**Tool catalog** — grantable per shared channel:",
        "",
        "**Built-in bundles:**",
    ]
    lines += [
        f"- **{name}** — {ROW_DESCRIPTIONS[name]}" for name in ROWS
    ]
    lines.append("")
    names = _mcp_names(mcp_servers)
    if names:
        lines.append(
            "**Connected MCP servers** (grant with or without the `mcp:` "
            "prefix):"
        )
        counts = mcp_servers if isinstance(mcp_servers, Mapping) else {}
        for server in sorted(names):
            count = counts.get(server)
            suffix = f" ({count} tools)" if isinstance(count, int) else ""
            lines.append(f"- `{MCP_PREFIX}{server}`{suffix}")
    else:
        lines.append("**Connected MCP servers:** none")
    clean_sources = [_sanitize(str(s)) for s in other_sources if str(s)]
    if clean_sources:
        lines.append("")
        lines.append(
            "Other tool sources (not per-channel switchable yet): "
            + ", ".join(sorted(clean_sources))
        )
    lines += [
        "",
        f"Change with `/fil-tools <channel> <tool> <on|off>` (e.g. "
        f"`/fil-tools {example} linear off` — verbs: on/off, "
        "enable/disable, grant/revoke, allow/deny).",
        "",
        _channels_line(channels),
    ]
    return "\n".join(lines)


def help_wake(channels: Sequence[tuple[str, str]] = ()) -> str:
    example = _example_channel(channels)
    return "\n".join(
        [
            "`/fil-wake <channel> <mention|all|off>` — when a shared "
            "channel wakes me:",
            "- **mention** — only @-mentions (and engaged threads) wake me "
            "(the default)",
            "- **all** — every message wakes me",
            "- **off** — messages there never wake me",
            f"Example: `/fil-wake {example} all`",
            _channels_line(channels),
        ]
    )


def help_guidance(channels: Sequence[tuple[str, str]] = ()) -> str:
    example = _example_channel(channels)
    return "\n".join(
        [
            "`/fil-guidance <channel> <text…>` — set my standing guidance "
            "for one channel (it frames every wake there). Everything after "
            "the channel is kept verbatim.",
            f"- `/fil-guidance {example}` — show the current guidance",
            f"- `/fil-guidance {example} clear` — remove it",
            f"Example: `/fil-guidance {example} Keep replies short; "
            "escalate billing questions to me.`",
            _channels_line(channels),
        ]
    )


def help_feature(features: Mapping[str, str] | None = None) -> str:
    lines = ["`/fil-feature <name> <on|off>` — toggle a runtime feature."]
    features = dict(features or {})
    if features:
        lines.append("**Known features:**")
        for name in sorted(features):
            # First sentence only — the full description lives in
            # get_features.
            summary = str(features[name]).split(". ")[0].rstrip(".")
            lines.append(f"- **{name}** — {summary}.")
    lines.append(
        f"Example: `/fil-feature {FEATURE_ADVANCED_TOOL_CONTROLS} on`"
    )
    return "\n".join(lines)


def help_for(
    command: str | None,
    *,
    channels: Sequence[tuple[str, str]] = (),
    mcp_servers: object = (),
    other_sources: Sequence[str] = (),
    features: Mapping[str, str] | None = None,
) -> str:
    """The help text for a ``HelpRequest`` — the index, or one command's
    contextual help built from the same live vocabularies the parser uses."""
    if command == "config":
        return help_config()
    if command == "tools":
        # Same compact usage as bare `/fil-tools`; the full catalog is
        # `/fil-tools list` (mcp_servers/other_sources feed only that).
        return help_tools(channels)
    if command == "wake":
        return help_wake(channels)
    if command == "guidance":
        return help_guidance(channels)
    if command == "feature":
        return help_feature(features)
    return help_index()


# ── Mutation compilation ─────────────────────────────────────────────


@dataclass(frozen=True)
class Mutation:
    """A compiled slash mutation: the new store documents to write (only the
    fields that changed are set), the sections to ``write_back``, and the
    confirmation echo. ``changed`` False means reply-only (no write)."""

    changed: bool
    reply: str
    capability_policy: dict | None = None
    wake_policy: dict | None = None
    channel_instructions: dict | None = None
    feature_flags: dict | None = None
    sections: tuple[str, ...] = ()


def _channel_label(room_id: str, channel_name: str) -> str:
    clean = _sanitize(channel_name or "")
    return f"#{clean}" if clean else str(room_id)


def _friendly_target(target: str) -> str:
    return target[len(MCP_PREFIX) :] if target.startswith(MCP_PREFIX) else target


def apply_tools(
    command: ToolsCommand, capability_policy: dict, feature_flags: dict
) -> Mutation:
    """Grant or revoke one bundle in a channel's ``per_channel`` entry.

    v1 override semantics, same as the app: a channel's first edit
    materializes its entry from the default list, and from then on the entry
    is the channel's whole grant (replace, not union). A change also opts
    into ``advanced_tool_controls`` in the same write — granting per-channel
    tools without the gate on would silently do nothing.
    """
    label = _channel_label(command.room_id, command.channel_name)
    friendly = _friendly_target(command.target)
    policy = dict(capability_policy)
    raw_per = policy.get("per_channel")
    per = dict(raw_per) if isinstance(raw_per, dict) else {}
    current = per.get(command.room_id)
    if isinstance(current, list):
        grants = [str(g) for g in current]
    else:
        default = policy.get("default_capabilities")
        grants = (
            [str(g) for g in default]
            if isinstance(default, list)
            else list(ROWS)
        )
    listed = ", ".join(grants) or "none (baseline only)"
    if command.verb == "grant":
        if command.target in grants:
            return Mutation(
                changed=False,
                reply=f"**{friendly}** is already enabled in **{label}** "
                f"(tools: {listed}).",
            )
        grants.append(command.target)
        verbed = "Enabled"
    else:
        if command.target not in grants:
            return Mutation(
                changed=False,
                reply=f"**{friendly}** isn't enabled in **{label}** "
                f"(tools: {listed}).",
            )
        grants = [g for g in grants if g != command.target]
        verbed = "Disabled"
    per[command.room_id] = grants
    policy["per_channel"] = per
    new_flags: dict | None = None
    flag_note = ""
    if not feature_flags.get(FEATURE_ADVANCED_TOOL_CONTROLS):
        new_flags = dict(feature_flags)
        new_flags[FEATURE_ADVANCED_TOOL_CONTROLS] = True
        flag_note = " — advanced tool controls are now on"
    now = ", ".join(grants) or "none (baseline only)"
    sections = ("capability_policy",) + (
        ("feature_flags",) if new_flags is not None else ()
    )
    return Mutation(
        changed=True,
        reply=f"✓ {verbed} **{friendly}** in **{label}** (tools now: {now})"
        f"{flag_note}",
        capability_policy=policy,
        feature_flags=new_flags,
        sections=sections,
    )


_WAKE_GLOSS = {
    "mention": "I wake there only when mentioned (or in an engaged thread)",
    "all": "every message there wakes me",
    "off": "messages there never wake me",
}


def apply_wake(command: WakeCommand, wake_policy: dict) -> Mutation:
    label = _channel_label(command.room_id, command.channel_name)
    policy = dict(wake_policy)
    raw_per = policy.get("per_channel")
    per = dict(raw_per) if isinstance(raw_per, dict) else {}
    raw_entry = per.get(command.room_id)
    entry = dict(raw_entry) if isinstance(raw_entry, dict) else {}
    gloss = _WAKE_GLOSS[command.mode]
    if entry.get("reactive_wake") == command.mode:
        return Mutation(
            changed=False,
            reply=f"Wake mode for **{label}** is already "
            f"**{command.mode}** — {gloss}.",
        )
    entry["reactive_wake"] = command.mode
    per[command.room_id] = entry
    policy["per_channel"] = per
    return Mutation(
        changed=True,
        reply=f"✓ Wake mode for **{label}**: **{command.mode}** — {gloss}.",
        wake_policy=policy,
        sections=("wake_policy",),
    )


def apply_guidance(
    command: GuidanceCommand, channel_instructions: dict
) -> Mutation:
    label = _channel_label(command.room_id, command.channel_name)
    mapping = dict(channel_instructions)
    if command.text is None:
        if command.room_id not in mapping:
            return Mutation(
                changed=False, reply=f"No guidance is set for **{label}**."
            )
        del mapping[command.room_id]
        return Mutation(
            changed=True,
            reply=f"✓ Cleared guidance for **{label}**.",
            channel_instructions=mapping,
            sections=("channel_instructions",),
        )
    mapping[command.room_id] = command.text
    # The guidance is the principal's own text; echo enough of it to confirm
    # what was saved without flooding the backchannel.
    shown = command.text if len(command.text) <= 120 else command.text[:117] + "…"
    return Mutation(
        changed=True,
        reply=f"✓ Guidance for **{label}** set ({len(command.text)} chars): "
        f"{shown}",
        channel_instructions=mapping,
        sections=("channel_instructions",),
    )


def apply_feature(command: FeatureCommand, feature_flags: dict) -> Mutation:
    state = "on" if command.enabled else "off"
    if bool(feature_flags.get(command.feature, False)) == command.enabled:
        return Mutation(
            changed=False,
            reply=f"Feature **{command.feature}** is already **{state}**.",
        )
    flags = dict(feature_flags)
    flags[command.feature] = command.enabled
    return Mutation(
        changed=True,
        reply=f"✓ Feature **{command.feature}**: **{state}**",
        feature_flags=flags,
        sections=("feature_flags",),
    )


# ── /config show rendering ───────────────────────────────────────────


def render_config_show(
    *,
    capability_policy: dict,
    wake_policy: dict,
    channel_instructions: dict,
    feature_flags: dict,
    channels: Sequence[tuple[str, str]],
) -> str:
    """A readable summary of the local config document, with room ids
    resolved to channel names wherever the channel list knows them."""
    names = {
        str(room_id): _sanitize(name or "") for room_id, name in channels
    }

    def label(room_id: str) -> str:
        clean = names.get(str(room_id), "")
        return f"#{clean}" if clean else str(room_id)

    lines = ["**Current configuration:**", ""]
    atc = bool(feature_flags.get(FEATURE_ADVANCED_TOOL_CONTROLS, False))
    lines.append(
        f"**Tools** (advanced_tool_controls: {'on' if atc else 'off'}):"
    )
    default = capability_policy.get("default_capabilities")
    default_names = (
        [str(g) for g in default] if isinstance(default, list) else list(ROWS)
    )
    lines.append(f"- default: {', '.join(default_names) or 'none'}")
    per = capability_policy.get("per_channel")
    if isinstance(per, dict):
        for room_id in sorted(per, key=label):
            grants = per[room_id]
            if isinstance(grants, list):
                shown = ", ".join(str(g) for g in grants)
                lines.append(
                    f"- **{label(room_id)}**: "
                    f"{shown or 'none (baseline only)'}"
                )
    lines += ["", "**Wake:**"]
    lines.append(f"- default: {wake_policy.get('reactive_wake', 'mention')}")
    wake_per = wake_policy.get("per_channel")
    if isinstance(wake_per, dict):
        for room_id in sorted(wake_per, key=label):
            entry = wake_per[room_id]
            if isinstance(entry, dict) and "reactive_wake" in entry:
                lines.append(
                    f"- **{label(room_id)}**: {entry['reactive_wake']}"
                )
    lines += ["", "**Guidance:**"]
    any_guidance = False
    for room_id in sorted(channel_instructions or {}, key=label):
        text = channel_instructions[room_id]
        if isinstance(text, str) and text:
            lines.append(f"- **{label(room_id)}**: set ({len(text)} chars)")
            any_guidance = True
    if not any_guidance:
        lines.append("- none")
    lines += ["", "**Features:**"]
    for name in sorted(set(feature_flags) | {FEATURE_ADVANCED_TOOL_CONTROLS}):
        state = "on" if feature_flags.get(name) else "off"
        lines.append(f"- {name}: {state}")
    return "\n".join(lines)


# ── Channel status rendering ─────────────────────────────────────────


def render_tools_status(
    *,
    room_id: str,
    channel_name: str,
    capability_policy: dict,
    feature_flags: dict,
    mcp_servers: object = (),
    other_sources: Sequence[str] = (),
) -> str:
    """The ``/fil-tools <channel>`` status reply: what's enabled in that
    channel right now, whether that grant is the default or a channel
    override, what's off, and how to change it. Read-only — same resolution
    the mutation path starts from (per-channel override replaces the default
    list; absent both, the built-in rows)."""
    label = _channel_label(room_id, channel_name)
    raw_per = capability_policy.get("per_channel")
    per = raw_per if isinstance(raw_per, dict) else {}
    entry = per.get(room_id)
    if isinstance(entry, list):
        grants = [str(g) for g in entry]
        origin = "channel override"
    else:
        default = capability_policy.get("default_capabilities")
        grants = (
            [str(g) for g in default]
            if isinstance(default, list)
            else list(ROWS)
        )
        origin = "default grant — no channel override"
    servers = _mcp_names(mcp_servers)
    counts = mcp_servers if isinstance(mcp_servers, Mapping) else {}
    lines = [f"**{label}** tools ({origin}):"]
    for grant in grants:
        if grant in ROW_DESCRIPTIONS:
            lines.append(f"- **{grant}** — {ROW_DESCRIPTIONS[grant]}")
        elif grant.startswith(MCP_PREFIX):
            server = grant[len(MCP_PREFIX) :]
            count = counts.get(server)
            suffix = f" ({count} tools)" if isinstance(count, int) else ""
            note = "" if server in servers else " — not currently connected"
            lines.append(f"- **{grant}** — MCP server{suffix}{note}")
        elif grant in DEPRECATED_ALIASES:
            lines.append(f"- **{grant}** — deprecated alias bundle")
        else:
            lines.append(f"- **{grant}** — custom bundle")
    if not grants:
        lines.append("- none (baseline only)")
    off = [row for row in ROWS if row not in grants] + [
        f"{MCP_PREFIX}{server}"
        for server in sorted(servers)
        if f"{MCP_PREFIX}{server}" not in grants
    ]
    lines.append("")
    lines.append(f"**Off:** {', '.join(off) if off else 'nothing'}")
    clean_sources = [_sanitize(str(s)) for s in other_sources if str(s)]
    if clean_sources:
        lines.append("")
        lines.append(
            "Other tool sources (not per-channel switchable yet): "
            + ", ".join(sorted(clean_sources))
        )
    if not feature_flags.get(FEATURE_ADVANCED_TOOL_CONTROLS):
        lines.append("")
        lines.append(
            "Note: `advanced_tool_controls` is off, so this isn't enforced "
            "yet — any `/fil-tools` change turns it on."
        )
    example_target = sorted(servers)[0] if servers else "post"
    lines += [
        "",
        "**Examples:**",
        f"- `/fil-tools {label} {example_target} off`",
        f"- `/fil-tools {label} read_history on`",
        "Typos are fine — I'll confirm what I understood.",
    ]
    return "\n".join(lines)


def render_guidance_show(
    *,
    room_id: str,
    channel_name: str,
    channel_instructions: dict,
) -> str:
    """The ``/fil-guidance <channel>`` status reply: the current guidance
    verbatim (blockquoted — it's the principal's own text), plus how to set
    or clear it."""
    label = _channel_label(room_id, channel_name)
    text = (channel_instructions or {}).get(room_id)
    if isinstance(text, str) and text:
        lines = [f"**{label}** guidance ({len(text)} chars):"]
        lines += [f"> {line}" for line in text.splitlines()]
    else:
        lines = [f"No guidance is set for **{label}**."]
    lines += [
        "",
        f"Set it with `/fil-guidance {label} <text…>`; clear it with "
        f"`/fil-guidance {label} clear`.",
    ]
    return "\n".join(lines)
