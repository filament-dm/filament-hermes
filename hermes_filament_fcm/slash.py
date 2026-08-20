"""Deterministic slash-command layer for the principal's backchannel.

The surface is two commands in the ``/fil-`` namespace: ``/fil-help`` (the
index) and ``/fil-config``, which carries everything as subforms —
``/fil-config list`` (all channels at a glance), ``/fil-config <channel>``
(one channel's full config), ``/fil-config <channel> [tools] <target>
<on|off>``, ``/fil-config <channel> wake <mention|all|off>``, ``/fil-config
<channel> guidance <text…|clear>`` (bare = show), ``/fil-config tools list``
(the tool catalog), and ``/fil-config feature <name> <on|off>``. The retired
top-level commands (``/fil-tools``, ``/fil-wake``, ``/fil-guidance``,
``/fil-feature``, the old whole-document ``/fil-config show``) answer with a
one-line redirect to the new spelling, arguments translated where that is
mechanical.

A backchannel message whose stripped body starts with ``/fil-``
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
confirmation echoes bold, blank lines between sections so the app renders
them as distinct paragraphs. Formatting, not padding — replies stay compact.

Parsing is token classification, not positional grammar: after the command
word, each remaining token is matched against closed vocabularies — a channel
(``#name`` / ``name`` / room id, resolved against the server-attributed
channel list the caller passes in), a subform keyword (show/list/tools/wake),
a target (capability rows, bundle names, deprecated aliases, and
``mcp:<server>`` auto-bundles matchable with or without the prefix), a verb
(grant/revoke synonyms), and a wake mode. Token order is free; filler words
are skipped. The two exceptions are positional by necessity: ``feature``
(global, so it must lead) and ``guidance`` (it introduces verbatim free text,
so the keyword must sit first or right after the channel). Matching is fuzzy
(``difflib``, cutoff ``FUZZY_CUTOFF``), but never a silent guess: a near-tie
or a cross-vocabulary collision comes back as an ``Ambiguous`` result
carrying the candidates, and the adapter asks — "did you mean …?".

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
# The other reserved auto-bundle spelling: the host's own toolsets.
TOOLSET_PREFIX = "toolset:"
# Forces target classification for a bundle whose name collides with a
# command word ("wake", "on", "list", …): `bundle:wake` is the bundle,
# bare `wake` is the grammar. Matching-time syntax only — never stored.
BUNDLE_FORCE_PREFIX = "bundle:"
FEATURE_ADVANCED_TOOL_CONTROLS = "advanced_tool_controls"

# Member tools of the builtin bundles (rows + deprecated aliases), restated
# verbatim from reactive.BUILTIN_BUNDLES (tests pin the two identical) so the
# status renderer can compute *effective* row state — a row is on when its
# name is granted or its member tools are covered by the channel's expanded
# grants (e.g. the deprecated "messaging" alias covers read_history, post,
# and directory without naming them).
BUNDLE_MEMBERS: dict[str, tuple[str, ...]] = {
    "read_history": (
        "get_recent_messages",
        "get_thread",
        "search_messages",
        "list_mentions",
        "download_media",
        "list_reactions",
    ),
    "post": (
        "post_message",
        "reply_in_thread",
        "react",
        "unreact",
        "quote",
        "rechat",
    ),
    "directory": ("get_user_profile", "search_members"),
    "escalate": ("message_principal",),
    "messaging": (
        "get_self",
        "get_recent_messages",
        "get_thread",
        "get_user_profile",
        "search_messages",
        "search_members",
        "list_mentions",
        "react",
        "unreact",
        "mark_read",
        "post_message",
        "reply_in_thread",
        "download_media",
    ),
    "readonly": (
        "get_self",
        "get_recent_messages",
        "get_thread",
        "get_user_profile",
        "search_messages",
        "list_mentions",
    ),
}

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

# The live command surface, plus the retired pre-consolidation commands —
# still recognized (exact + fuzzy) so an old-form invocation gets a one-line
# redirect to the `/fil-config` spelling instead of "unknown command".
COMMANDS: tuple[str, ...] = ("help", "config")
OLD_COMMANDS: tuple[str, ...] = ("tools", "wake", "guidance", "feature")

LIST_WORD = "list"
# Subform keywords classified inside `/fil-config` args (each is its own
# slot, so "tools list" fills two). `guidance` and `feature` are routed
# positionally instead — see the module docstring.
_CONFIG_SUBS: tuple[str, ...] = ("show", "list", "tools", "wake")

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
    "config": "`/fil-config list` · `/fil-config <channel>` · "
    "`/fil-config <channel> <tool> <on|off>` — `/fil-config help` "
    "for every form",
    "tools": "`/fil-config <channel> <tool-or-bundle> <on|off>` "
    "(e.g. `/fil-config #welcome linear off`); catalog: "
    "`/fil-config tools list`",
    "wake": "`/fil-config <channel> wake <mention|all|off>` "
    "(e.g. `/fil-config #general wake all`)",
    "guidance": "`/fil-config <channel> guidance <text…|clear>` "
    "(e.g. `/fil-config #welcome guidance Be brief.`); leave the text off "
    "to show the current guidance",
    "feature": "`/fil-config feature <name> <on|off>` "
    "(e.g. `/fil-config feature advanced_tool_controls on`)",
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


_MD_ESCAPE_RE = re.compile(r"([\\`*_~\[\]()])")


def _md_escape(value: str) -> str:
    """Backslash-escape markdown-active characters (the backslash itself
    first among them, then backticks, brackets, parens, asterisks,
    underscores, tildes) so member-editable metadata that reaches replies —
    channel names above all — renders inert: a channel named
    ``[urgent](https://x)`` must never become a live link or smuggle
    formatting into the trusted reply text. The backslash is in the set
    because a name that ARRIVES as ``\\[`` would otherwise eat the inserted
    escape and leave the bracket active."""
    return _MD_ESCAPE_RE.sub(r"\\\1", value)


# ── Parse results ────────────────────────────────────────────────────


@dataclass(frozen=True)
class HelpRequest:
    """``/fil-help``, ``/fil-config help``, or a bare subform with nothing
    usable — the adapter answers with the matching help text (``help_for``)."""

    command: str | None  # None → the index


@dataclass(frozen=True)
class ChannelsOverview:
    """``/fil-config list`` — the adapter renders one summary line per
    shared channel with ``render_config_list``."""


@dataclass(frozen=True)
class ChannelShow:
    """``/fil-config <channel>`` (also ``… show`` / ``… list``) — the
    adapter renders the channel's full config with ``render_channel_show``."""

    room_id: str
    channel_name: str  # sanitized; "" when the channel is unnamed


@dataclass(frozen=True)
class ToolsCommand:
    room_id: str
    channel_name: str  # sanitized; "" when the channel is unnamed
    target: str  # canonical grant name (row/alias/bundle or "mcp:<server>")
    verb: str  # "grant" | "revoke"


@dataclass(frozen=True)
class ToolsList:
    """``/fil-config tools list`` — the adapter answers with the full tool
    catalog (``render_tools_list``)."""


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
    """``/fil-config <channel> guidance`` with no text — the adapter answers
    with the current guidance (``render_guidance_show``)."""

    room_id: str
    channel_name: str


@dataclass(frozen=True)
class FeatureCommand:
    feature: str
    enabled: bool


@dataclass(frozen=True)
class FeatureList:
    """``/fil-config feature`` / ``/fil-config feature list`` — the adapter
    renders every known feature with its current state
    (``render_feature_list``)."""


@dataclass(frozen=True)
class FeatureShow:
    """``/fil-config feature <name>`` with no on/off — the adapter renders
    that feature's state and description (``render_feature_show``)."""

    feature: str


@dataclass(frozen=True)
class Redirect:
    """A retired pre-consolidation invocation (``/fil-tools`` & co, old
    ``/fil-config show``): the reply is a one-line pointer to the new
    ``/fil-config`` spelling, with the caller's own arguments translated
    where that is mechanical. Reply-only — never a mutation."""

    reply: str


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
    subform's usage line (or the full index when nothing was recognized)."""

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
    # Two channels can share a display name; their candidate entries must
    # still be tellable apart (and selectable — the room id always matches
    # exactly), so duplicates carry the room id in the display.
    counts: dict[str, int] = {}
    for _room_id, name in channels:
        clean = _sanitize(name or "").lower()
        if clean:
            counts[clean] = counts.get(clean, 0) + 1
    for room_id, name in channels:
        clean = _sanitize(name or "")
        # display/label are reply text → markdown-escaped; the match key and
        # the canonical name stay raw (escaping is a rendering concern).
        label = f"#{_md_escape(clean)}" if clean else str(room_id)
        if clean and counts.get(clean.lower(), 0) > 1:
            display = f"{label} (channel `{room_id}`)"
        else:
            display = f"{label} (channel)" if clean else label
        canonical = (str(room_id), clean)
        if clean:
            entries.append(
                _Entry("channel", clean.lower(), canonical, display, label=label)
            )
        entries.append(
            _Entry("channel", str(room_id).lower(), canonical, display, True, label)
        )
    return entries


def _target_entries(
    mcp_servers: object, bundles: Iterable[str], other_sources: object = ()
) -> list[_Entry]:
    entries = [_Entry("target", name, name, name) for name in ROWS + DEPRECATED_ALIASES]
    for bundle in bundles or ():
        text = str(bundle)
        # mcp:-prefixed names can't name custom bundles (reserved); rows and
        # aliases are already present.
        low = text.lower()
        if (
            text
            and not low.startswith(MCP_PREFIX)
            and low not in {e.key for e in entries}
        ):
            entries.append(_Entry("target", low, text, text))
    for server in _mcp_names(mcp_servers):
        canonical = f"{MCP_PREFIX}{server}"
        entries.append(_Entry("target", canonical.lower(), canonical, canonical))
        # The bare server name matches too: "gcal" → "mcp:gcal".
        entries.append(_Entry("target", str(server).lower(), canonical, canonical))
    # The host's own toolsets — Hermes' bundled plugins and core tools. Just as
    # grantable as a remote MCP server (`toolset:<name>`), and named the same
    # two ways: canonical, and bare ("spotify" → "toolset:spotify").
    known = {e.key for e in entries}
    for source in _mcp_names(other_sources):
        canonical = f"{TOOLSET_PREFIX}{source}"
        for key in (canonical.lower(), str(source).lower()):
            if key not in known:
                entries.append(_Entry("target", key, canonical, canonical))
                known.add(key)
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
    (``#…``/``!…`` → channel, ``mcp:…``/``bundle:…`` → target) only ever
    match their own vocabulary, so ``#post`` is a channel even where a
    bundle ``post`` exists, and ``bundle:wake`` is the bundle named "wake"
    even though bare ``wake`` reads as the command word — the escape for
    bundle names that collide with the grammar.
    """
    forced: str | None = None
    if token.startswith(("#", "!")):
        forced = "channel"
    elif token.lower().startswith(MCP_PREFIX):
        forced = "target"
    elif token.lower().startswith(BUNDLE_FORCE_PREFIX):
        forced = "target"
        token = token[len(BUNDLE_FORCE_PREFIX) :]
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
    # <= so a gap of exactly the margin is still "within" it, as documented.
    tied = [e for r, e in ranked if best - r <= AMBIGUITY_MARGIN]
    if len(tied) == 1:
        return tied[0], ()
    return None, tuple(e.display for e in tied)


def _match_word(word: str, vocab: Sequence[str]) -> tuple[str | None, tuple[str, ...]]:
    """``_score`` for a flat word list (command and keyword words)."""
    entry, candidates = _score(word, [_Entry("word", v.lower(), v, v) for v in vocab])
    return (str(entry.canonical) if entry else None), candidates


_TOKEN_RE = re.compile(r"\S+")


def _tokenize(rest: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(rest)]


# Channel names may be multi-word ("General Chat"), but tokens split on
# whitespace — so channel classification is greedy n-gram matching: up to
# ``_ngram_max`` consecutive tokens are joined (the ``#`` allowed on the
# first) and tried against the channel vocabulary before single-token
# fallback. The width follows the longest known channel name so no real
# channel is ever out of reach, floored at this and capped hard.
_CHANNEL_NGRAM_MIN = 3
_CHANNEL_NGRAM_CAP = 8


def _ngram_max(entries: Sequence[_Entry]) -> int:
    words = max(
        (len(e.key.split()) for e in entries if e.slot == "channel"),
        default=1,
    )
    return min(max(words, _CHANNEL_NGRAM_MIN), _CHANNEL_NGRAM_CAP)


def _exact_channel_join(
    joined: str, channel_entries: Sequence[_Entry]
) -> tuple[_Entry | None, tuple[str, ...]]:
    """Exact-key match of one (possibly multi-token) string against channel
    entries; multiple exact hits are a candidates tuple, like ``_score``."""
    norm = (joined[1:] if joined.startswith("#") else joined).lower()
    hits = _dedupe(e for e in channel_entries if e.key == norm)
    if len(hits) == 1:
        return hits[0], ()
    if len(hits) > 1:
        return None, tuple(e.display for e in hits)
    return None, ()


def _exact_channel_ngram(
    tokens: Sequence[tuple[str, int, int]],
    index: int,
    entries: Sequence[_Entry],
) -> tuple[_Entry | None, int, tuple[str, ...]]:
    """Longest exact multi-word channel match at ``index`` (n-grams of
    ``_ngram_max``..2 joined with single spaces). Single tokens are
    the ordinary classifier's job. Returns (entry, tokens_consumed,
    candidates)."""
    channel_entries = [e for e in entries if e.slot == "channel"]
    if not channel_entries:
        return None, 0, ()
    for n in range(_ngram_max(channel_entries), 1, -1):
        if index + n > len(tokens):
            continue
        joined = " ".join(t[0] for t in tokens[index : index + n])
        entry, candidates = _exact_channel_join(joined, channel_entries)
        if candidates:
            return None, n, candidates
        if entry is not None:
            return entry, n, ()
    return None, 0, ()


def _fuzzy_channel_ngram(
    tokens: Sequence[tuple[str, int, int]],
    index: int,
    entries: Sequence[_Entry],
    min_n: int = 2,
) -> tuple[_Entry | None, int, tuple[str, ...]]:
    """Fuzzy multi-word channel match at ``index``: every n-gram width
    competes and the best ratio wins (a typo'd two-word name must not lose to
    a three-word join that happens to clear the cutoff, nor swallow trailing
    tokens a shorter join matches better without), with the usual near-tie
    ambiguity instead of a guess. ``min_n`` is 2 in slot classification
    (single tokens are the ordinary classifier's job) and 1 where the caller
    has no single-token pass of its own."""
    channel_entries = [e for e in entries if e.slot == "channel" and not e.exact_only]
    if not channel_entries:
        return None, 0, ()
    best_by: dict[object, tuple[float, _Entry, int]] = {}
    for n in range(_ngram_max(channel_entries), min_n - 1, -1):
        if index + n > len(tokens):
            continue
        joined = " ".join(t[0] for t in tokens[index : index + n])
        norm = (joined[1:] if joined.startswith("#") else joined).lower()
        for e in channel_entries:
            ratio = difflib.SequenceMatcher(None, norm, e.key).ratio()
            if ratio < FUZZY_CUTOFF:
                continue
            k = e.canonical
            if k not in best_by or ratio > best_by[k][0]:
                best_by[k] = (ratio, e, n)
    if not best_by:
        return None, 0, ()
    ranked = sorted(best_by.values(), key=lambda p: p[0], reverse=True)
    best = ranked[0][0]
    tied = [(e, n) for r, e, n in ranked if best - r <= AMBIGUITY_MARGIN]
    if len(tied) == 1:
        entry, n = tied[0]
        return entry, n, ()
    return None, 0, tuple(e.display for e, _n in tied)


def _fill_slots(
    tokens: Sequence[tuple[str, int, int]],
    entries: Sequence[_Entry],
    command: str,
    expects: str,
    labels: Mapping[str, str],
    required: Sequence[str],
    backchannel_entries: Sequence[_Entry] = (),
):
    """Order-free classification: assign each non-filler token to an open
    slot. Returns ``(slots, None)`` on success — including a possibly
    *incomplete* ``slots`` dict when ``required`` is empty, which the caller
    routes itself — or ``(None, result)`` when a token was ambiguous or
    unplaceable, or ``(slots, HelpRequest)`` when nothing resolved at all.
    ``backchannel_entries`` is the excluded backchannel's own vocabulary: an
    unmatched token (or joined span) that would have resolved there answers
    with ``BACKCHANNEL_NOTE`` instead of the generic unknown-token error.

    Channel matching is greedy n-gram (multi-word names): longest exact
    join first, then ordinary single-token classification (exact hits and
    cross-vocabulary ambiguity included), then fuzzy joins competing on
    ratio — never a silent guess at any stage. Filler words are skipped
    lazily, after the channel n-gram pass, so a channel whose *name*
    contains a filler ("The Lounge") still resolves."""
    slots: dict[str, _Entry] = {}
    toks = list(tokens)
    i = 0
    while i < len(toks):
        text = toks[i][0]
        open_entries = [e for e in entries if e.slot not in slots]
        entry, consumed, candidates = _exact_channel_ngram(toks, i, open_entries)
        if candidates:
            joined = " ".join(t[0] for t in toks[i : i + consumed])
            return None, Ambiguous(
                command=command, token=_sanitize(joined), candidates=candidates
            )
        if entry is not None:
            # A name spelled entirely out of command words ("wake all") must
            # not silently swallow the keyword reading — ask, unless the
            # ``#``/``!`` shape already forced the channel.
            span = [t[0] for t in toks[i : i + consumed]]
            if not span[0].startswith(("#", "!")):
                taken = set(slots)
                alt: list[_Entry] = []
                for word in span:
                    hits = _dedupe(
                        e
                        for e in open_entries
                        if e.slot != "channel"
                        and e.slot not in taken
                        and e.key == word.lower()
                    )
                    if len(hits) != 1:
                        alt = []
                        break
                    taken.add(hits[0].slot)
                    alt.append(hits[0])
                if alt:
                    joined = " ".join(span)
                    return None, Ambiguous(
                        command=command,
                        token=_sanitize(joined),
                        candidates=(
                            f"{entry.display} — say `#{_sanitize(joined)}`",
                            f"the command words `{_sanitize(joined).lower()}`"
                            " — put the channel first",
                        ),
                    )
            slots[entry.slot] = entry
            i += consumed
            continue
        if text.lower() in FILLER_WORDS:
            i += 1
            continue
        entry, candidates = _score(text, open_entries)
        if candidates:
            return None, Ambiguous(
                command=command, token=_sanitize(text), candidates=candidates
            )
        if entry is not None:
            slots[entry.slot] = entry
            i += 1
            continue
        entry, consumed, candidates = _fuzzy_channel_ngram(toks, i, open_entries)
        if candidates:
            return None, Ambiguous(
                command=command, token=_sanitize(text), candidates=candidates
            )
        if entry is not None:
            slots[entry.slot] = entry
            i += consumed
            continue
        if backchannel_entries:
            for n in range(1, _ngram_max(backchannel_entries) + 1):
                if i + n > len(toks):
                    break
                joined = " ".join(t[0] for t in toks[i : i + n])
                bc_entry, bc_candidates = _score(joined, backchannel_entries)
                if bc_entry is not None or bc_candidates:
                    return None, Unparsed(command=command, problem=BACKCHANNEL_NOTE)
        return None, Unparsed(
            command=command,
            problem=f'I couldn\'t match "{_sanitize(text)}" to {expects}.',
        )
    if not slots:
        return slots, HelpRequest(command)
    missing = [s for s in required if s not in slots]
    if missing:
        got = ", ".join(f"{labels[s]} {slots[s].prose}" for s in required if s in slots)
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
    other_sources: object = (),
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
        return Unparsed(command=None, problem=f"That isn't a {PREFIX} command.")
    word = match.group(1)
    rest = text[match.end() :]
    if not word:
        return HelpRequest(None)
    command, candidates = _match_word(word, COMMANDS + OLD_COMMANDS)
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
    if command in OLD_COMMANDS:
        return _redirect_old(command, rest.strip(), channels)
    bc_entries = _channel_entries([backchannel]) if backchannel else []
    return _parse_config(
        rest, channels, mcp_servers, bundles, features, bc_entries, other_sources
    )


def _unknown_channel(command: str, token: str, backchannel_entries: Sequence[_Entry]):
    if backchannel_entries:
        bc_entry, bc_candidates = _score(token, backchannel_entries)
        if bc_entry is not None or bc_candidates:
            return Unparsed(command=command, problem=BACKCHANNEL_NOTE)
    return Unparsed(
        command=command,
        problem=f'I couldn\'t find a channel matching "{_sanitize(token)}".',
    )


def _parse_config(
    rest: str,
    channels: Sequence[tuple[str, str]],
    mcp_servers: object,
    bundles: Iterable[str],
    features: Mapping[str, str],
    backchannel_entries: Sequence[_Entry],
    other_sources: object = (),
):
    toks = _tokenize(rest)
    meaningful = [t for t in toks if t[0].lower() not in FILLER_WORDS]
    if not meaningful:
        return HelpRequest("config")
    first = meaningful[0][0]
    if first.lower() == "help":
        return HelpRequest("config")
    # Positional routes first. `feature` is global (never takes a channel),
    # so it must lead; `guidance` introduces verbatim free text that must not
    # be token-classified, so its keyword sits first or right after the
    # channel. Shape-forced tokens (#…/!…/mcp:…/bundle:…) can't be either
    # keyword.
    first_forced = first.startswith(("#", "!")) or first.lower().startswith(
        (MCP_PREFIX, BUNDLE_FORCE_PREFIX)
    )
    chan_entries = _channel_entries(channels)

    def _keyword_channel_clash(token: str, keyword: str):
        """A leading token that reads as *keyword* but also exactly names a
        channel is a genuine fork — ask, never silently pick the keyword.
        Both readings stay reachable: `#name` forces the channel, and the
        channel-first spelling keeps the keyword form."""
        hit, cands = _exact_channel_join(token, chan_entries)
        if hit is None and not cands:
            return None
        display = hit.display if hit is not None else " / ".join(cands)
        clean = _sanitize(token)
        return Ambiguous(
            command="config",
            token=clean,
            candidates=(
                f"{display} — say `#{clean}`",
                f"the `{keyword}` keyword — put the channel first",
            ),
        )

    if not first_forced and _match_word(first, ("feature",))[0]:
        clash = _keyword_channel_clash(first, "feature")
        if clash is not None:
            return clash
        return _parse_feature(meaningful[1:], features)
    # The keyword may sit first or right after the channel — which can be a
    # multi-word name spanning up to the n-gram width. The scan runs over the
    # raw token stream (channel names may contain filler words), but fillers
    # don't count against the reach.
    reach = _ngram_max(chan_entries) + 1
    # A leading multi-word channel name owns its tokens BEFORE the keyword
    # scan looks at them — a channel named "Guidance Team" must not have
    # its first word read as the guidance keyword. (Exact matches only:
    # a fuzzy leading span must not steal the keyword.)
    lead_entry, lead_consumed, _lead_cands = _exact_channel_ngram(toks, 0, chan_entries)
    skip_before = lead_consumed if lead_entry is not None else 0
    scan = [
        idx
        for idx, t in enumerate(toks)
        if idx >= skip_before and t[0].lower() not in FILLER_WORDS
    ][:reach]
    for pos in scan:
        tok = toks[pos][0]
        if tok.startswith(("#", "!")) or tok.lower().startswith(
            (MCP_PREFIX, BUNDLE_FORCE_PREFIX)
        ):
            continue
        if _match_word(tok, ("guidance",))[0]:
            if pos == scan[0]:
                # Keyword in channel position: a single-word channel named
                # "guidance" would silently lose to the keyword reading —
                # ask instead. (After a channel, the keyword is unambiguous;
                # multi-word names are owned by the lead guard above.)
                clash = _keyword_channel_clash(tok, "guidance")
                if clash is not None:
                    return clash
            return _config_guidance(rest, toks, pos, channels, backchannel_entries)
    entries = (
        chan_entries
        + [_Entry(sub, sub, sub, sub) for sub in _CONFIG_SUBS]
        + _target_entries(mcp_servers, bundles, other_sources)
        + _verb_entries()
        + [_Entry("mode", m, m, m) for m in ("mention", "all")]
    )
    slots, result = _fill_slots(
        toks,
        entries,
        "config",
        "a channel, a tool or bundle, on/off, or one of "
        "list/show/tools/wake/guidance/feature",
        {},
        (),
        backchannel_entries=backchannel_entries,
    )
    if result is not None and not isinstance(result, HelpRequest):
        return result
    if not slots:
        return HelpRequest("config")
    return _route_config_slots(slots)


def _route_config_slots(slots: Mapping[str, _Entry]):
    """Turn one filled slot set into a result. Valid shapes: ``{list}`` the
    overview, ``{tools, list}`` the catalog, a channel plus nothing/show/
    list/tools the channel view, channel+wake+mode a wake mutation,
    channel+target+verb a tools mutation. Everything else answers with what
    is still missing — never a guess."""
    keys = set(slots)
    if keys == {"list"}:
        return ChannelsOverview()
    if keys == {"show"}:
        # The retired whole-document form.
        return Redirect(f"`{PREFIX}config show` is now `{PREFIX}config list`.")
    if keys == {"tools", "list"}:
        return ToolsList()
    if keys == {"tools"}:
        return HelpRequest("tools")
    if keys == {"wake"}:
        return HelpRequest("wake")
    if "channel" not in keys:
        if "target" in keys or "verb" in keys:
            got = [
                f"{what} {slots[slot].prose}"
                for slot, what in (("target", "bundle"), ("verb", "on/off"))
                if slot in keys
            ]
            return Unparsed(
                command="tools",
                problem=f"I understood {', '.join(got)}, but still need the channel.",
            )
        if "mode" in keys:
            return Unparsed(
                command="wake",
                problem="I understood the wake mode, but still need the channel.",
            )
        return Unparsed(
            command="config", problem="I couldn't work out which form that is."
        )
    room_id, channel_name = slots["channel"].canonical
    # Escaped label for prose; raw ref for example commands in code spans
    # (escape backslashes render verbatim there and break the copied form).
    label = _channel_label(room_id, channel_name)
    ref = _code_ref(room_id, channel_name)
    # A query keyword mixed with mutation tokens must never mutate — ask
    # which of the two was meant instead of guessing either way.
    if keys & {"show", "list"} and keys & {"wake", "mode", "target", "verb"}:
        return Unparsed(
            command="config",
            problem="That mixes a query with a change — did you mean "
            f"`{PREFIX}config {ref} show` (see it) or "
            f"`{PREFIX}config {ref} <tool> <on|off>` (change it)?",
        )
    if "wake" in keys:
        if keys - {"channel", "wake", "mode", "verb"}:
            return Unparsed(
                command="wake",
                problem="Wake takes just a channel and a mode.",
            )
        if "mode" in keys and "verb" in keys and slots["verb"].canonical == "revoke":
            # "wake all off" names two modes ("off" is the off mode in a
            # wake context) — ask, never pick one.
            mode_word = str(slots["mode"].canonical)
            return Unparsed(
                command="wake",
                problem=f'That says both "{mode_word}" and "off" — '
                f"`{PREFIX}config {ref} wake {mode_word}` or "
                f"`{PREFIX}config {ref} wake off`?",
            )
        if "mode" in keys:
            mode = str(slots["mode"].canonical)
        elif "verb" in keys and slots["verb"].canonical == "revoke":
            # "off" classifies as a revoke verb; in a wake context it is the
            # off mode.
            mode = "off"
        elif "verb" in keys:
            return Unparsed(
                command="wake", problem="Wake mode is mention, all, or off."
            )
        else:
            return Unparsed(
                command="wake",
                problem=f"I understood channel {label}, but still need "
                "mention, all, or off.",
            )
        return WakeCommand(room_id=room_id, channel_name=channel_name, mode=mode)
    if "mode" in keys:
        # A wake mode without the wake keyword: point at the exact spelling
        # rather than mutating on a guess.
        mode = str(slots["mode"].canonical)
        return Unparsed(
            command="wake",
            problem=f"To set the wake mode, say `{PREFIX}config {ref} wake {mode}`.",
        )
    if "target" in keys and "verb" in keys:
        return ToolsCommand(
            room_id=room_id,
            channel_name=channel_name,
            target=str(slots["target"].canonical),
            verb=str(slots["verb"].canonical),
        )
    if "target" in keys:
        return Unparsed(
            command="tools",
            problem=f"I understood channel {label} and bundle "
            f"{slots['target'].prose}, but still need on/off.",
        )
    if "verb" in keys:
        return Unparsed(
            command="tools",
            problem=f"I understood channel {label} and on/off, but still "
            "need a tool or bundle.",
        )
    # A bare channel (possibly with show/list/tools riding along): the
    # channel's full config.
    return ChannelShow(room_id=room_id, channel_name=channel_name)


def _config_guidance(
    rest: str,
    meaningful: Sequence[tuple[str, int, int]],
    kw_pos: int,
    channels: Sequence[tuple[str, str]],
    backchannel_entries: Sequence[_Entry],
):
    """``/fil-config <channel> guidance <text…|clear>`` (and the keyword-first
    spelling with the channel at the head of the text). Everything after the
    channel+keyword is the guidance, verbatim (interior whitespace preserved)
    — free text is never token-classified, so words like "help" or "off" in
    it mean nothing special."""
    entries = _channel_entries(channels)
    channel_entry: _Entry | None = None
    if kw_pos > 0:
        # Everything before the keyword must be the channel — one name,
        # possibly multi-word: join it and resolve exact-then-fuzzy. Two
        # joins compete: the raw span (channel names may *contain* filler
        # words — "The Lounge") and the filler-stripped span (the principal
        # may phrase it naturally — "in the lounge guidance …").
        span = [t[0] for t in meaningful[:kw_pos]]
        stripped = [w for w in span if w.lower() not in FILLER_WORDS]
        joins = list(dict.fromkeys(" ".join(s) for s in (span, stripped) if s))
        joined = joins[0]
        entry = candidates = None
        for attempt in joins:
            entry, candidates = _exact_channel_join(attempt, entries)
            if entry is not None or candidates:
                joined = attempt
                break
        if entry is None and not candidates:
            for attempt in joins:
                entry, candidates = _score(attempt, entries)
                if entry is not None or candidates:
                    joined = attempt
                    break
        if candidates:
            return Ambiguous(
                command="guidance",
                token=_sanitize(joined),
                candidates=candidates,
            )
        if entry is None:
            return _unknown_channel("guidance", joined, backchannel_entries)
        channel_entry = entry
    text = rest[meaningful[kw_pos][2] :].strip()
    if channel_entry is None:
        head = _tokenize(text)
        if not head:
            return HelpRequest("guidance")
        # Keyword-first spelling: the channel sits at the head of the text —
        # longest exact prefix wins, then the fuzzy joins compete on ratio,
        # so multi-word names work here without swallowing guidance text.
        consumed_end = 0
        for n in range(min(_ngram_max(entries), len(head)), 0, -1):
            joined = " ".join(t[0] for t in head[:n])
            entry, candidates = _exact_channel_join(joined, entries)
            if candidates:
                return Ambiguous(
                    command="guidance",
                    token=_sanitize(joined),
                    candidates=candidates,
                )
            if entry is not None:
                channel_entry, consumed_end = entry, head[n - 1][2]
                break
        if channel_entry is None:
            entry, n, candidates = _fuzzy_channel_ngram(head, 0, entries, min_n=1)
            if candidates:
                return Ambiguous(
                    command="guidance",
                    token=_sanitize(head[0][0]),
                    candidates=candidates,
                )
            if entry is not None:
                channel_entry, consumed_end = entry, head[n - 1][2]
        if channel_entry is None:
            return _unknown_channel("guidance", head[0][0], backchannel_entries)
        text = text[consumed_end:].strip()
    room_id, channel_name = channel_entry.canonical
    if not text:
        # No text is a question: show the current guidance there.
        return GuidanceShow(room_id=room_id, channel_name=channel_name)
    if text.lower() == "clear":
        return GuidanceCommand(room_id=room_id, channel_name=channel_name, text=None)
    return GuidanceCommand(room_id=room_id, channel_name=channel_name, text=text)


def _parse_feature(
    tokens: Sequence[tuple[str, int, int]],
    features: Mapping[str, str],
):
    entries = (
        [
            _Entry("feature", str(name).lower(), str(name), str(name))
            for name in features
        ]
        + [_Entry("state", w, True, w) for w in GRANT_WORDS]
        + [_Entry("state", w, False, w) for w in REVOKE_WORDS]
        + [_Entry("list", LIST_WORD, LIST_WORD, LIST_WORD)]
    )
    slots, result = _fill_slots(
        tokens,
        entries,
        "feature",
        "a feature name, on/off, or list",
        {},
        (),
    )
    if result is not None and not isinstance(result, HelpRequest):
        return result
    keys = set(slots or {})
    # Bare `feature` and `feature list` are the same question: every known
    # feature with its current state.
    if not keys or keys == {"list"}:
        return FeatureList()
    if "list" in keys:
        return Unparsed(
            command="feature",
            problem=f'"list" stands alone — say `{PREFIX}config feature list`.',
        )
    if keys == {"feature"}:
        # A feature with no on/off is a question: show its state.
        return FeatureShow(feature=str(slots["feature"].canonical))
    if keys == {"state"}:
        return Unparsed(
            command="feature",
            problem=f"I understood on/off {slots['state'].prose}, but still "
            "need the feature name.",
        )
    return FeatureCommand(
        feature=str(slots["feature"].canonical),
        enabled=bool(slots["state"].canonical),
    )


# ── Old-form redirects ───────────────────────────────────────────────


def _redirect_old(
    command: str, rest: str, channels: Sequence[tuple[str, str]]
) -> Redirect:
    """One-line pointer from a retired top-level command to its
    ``/fil-config`` spelling, translating the caller's own arguments where
    that is mechanical — cheap goodwill during the rename, deterministic."""
    bare = not rest or rest.lower() == "help"
    if command == "feature":
        new = (
            f"{PREFIX}config feature {_sanitize(rest)}"
            if not bare
            else f"{PREFIX}config feature <name> <on|off>"
        )
    elif command == "tools":
        if bare:
            new = f"{PREFIX}config help"
        elif len(rest.split()) == 1 and _match_word(rest, (LIST_WORD,))[0]:
            new = f"{PREFIX}config tools list"
        else:
            # Mutations and channel queries translate literally: the tools
            # keyword is optional in the new grammar.
            new = f"{PREFIX}config {_sanitize(rest)}"
    elif command == "wake":
        label = mode = None
        for tok, _start, _end in _tokenize(rest):
            low = tok.lower()
            if low in FILLER_WORDS or low == "help":
                continue
            if label is None:
                entry, _cands = _score(tok, _channel_entries(channels))
                if entry is not None:
                    room_id, name = entry.canonical
                    # The translated command renders in a code span → raw ref.
                    label = _code_ref(room_id, name)
                    continue
            if mode is None:
                word, _cands = _match_word(tok, WAKE_MODES)
                if word:
                    mode = word
        new = (
            f"{PREFIX}config {label or '<channel>'} wake {mode or '<mention|all|off>'}"
        )
    else:  # guidance
        label = None
        text = ""
        tokens = [t for t in _tokenize(rest) if t[0].lower() not in FILLER_WORDS]
        if tokens and tokens[0][0].lower() != "help":
            entry, _cands = _score(tokens[0][0], _channel_entries(channels))
            if entry is not None:
                room_id, name = entry.canonical
                label = _channel_label(room_id, name)
                text = rest[tokens[0][2] :].strip()
        tail = _sanitize(text, 60) if text and len(text) <= 60 else "<text…|clear>"
        new = f"{PREFIX}config {label or '<channel>'} guidance {tail}"
    return Redirect(
        reply=f"`{PREFIX}{command}` moved under `{PREFIX}config` — say `{new}`."
    )


# ── Error / clarification rendering ──────────────────────────────────


def render_reply(result) -> str:
    """The reply text for an ``Ambiguous`` or ``Unparsed`` result. Quotes the
    principal's token plainly (already sanitized at parse time) and shows the
    subform's usage — parse failures answer with help, never a model turn."""
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
    no shared channel is known yet. NOT markdown-escaped: examples render
    inside code spans, where escaping doesn't apply — a backslash here
    would end up in the command the principal copies back."""
    for _room_id, name in channels:
        clean = _sanitize(name or "").replace("`", "")
        if clean:
            return f"#{clean}"
    return "#your-channel"


def _channels_line(channels: Sequence[tuple[str, str]], limit: int = 12) -> str:
    names = []
    for _room_id, name in channels:
        clean = _sanitize(name or "")
        if clean:
            names.append(f"#{_md_escape(clean)}")
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
            "- `/fil-config` — my settings: per-channel tools, wake, and "
            "guidance, plus global features",
            "- `/fil-config list` — all channels at a glance",
            "- `/fil-config <channel>` — one channel's full config",
            "- `/fil-config tools list` — the full tool catalog",
            "Say `/fil-config help` for every form.",
        ]
    )


def help_config(channels: Sequence[tuple[str, str]] = ()) -> str:
    """The compact index of `/fil-config` forms — the reply to bare
    `/fil-config` and `/fil-config help`."""
    example = _example_channel(channels)
    return "\n".join(
        [
            "`/fil-config` — my per-channel and global settings.",
            "- `/fil-config list` — all channels at a glance",
            "- `/fil-config <channel>` — that channel's full config",
            "- `/fil-config <channel> <tool> <on|off>` — grant/revoke tools "
            f"there (e.g. `/fil-config {example} linear off`)",
            "- `/fil-config <channel> wake <mention|all|off>` — when it wakes me",
            "- `/fil-config <channel> guidance <text…|clear>` — my standing "
            "guidance there (leave the text off to show it)",
            "- `/fil-config tools list` — the full tool catalog",
            "- `/fil-config feature <name> <on|off>` — toggle a feature",
            "Typos are fine — I'll confirm what I understood.",
        ]
    )


def help_tools(channels: Sequence[tuple[str, str]] = ()) -> str:
    """The `/fil-config tools` reply: compact pointers. The full catalog
    lives under `/fil-config tools list` (``render_tools_list``)."""
    example = _example_channel(channels)
    return "\n".join(
        [
            "`/fil-config <channel> <tool> <on|off>` — control which tools "
            "I may use per shared channel "
            f"(e.g. `/fil-config {example} linear off`).",
            "- `/fil-config tools list` — the full tool catalog",
            "- `/fil-config <channel>` — what's enabled there",
            "A bundle named like a command word? Prefix it: `bundle:wake on`.",
            "Typos are fine — I'll confirm what I understood.",
        ]
    )


def help_wake(channels: Sequence[tuple[str, str]] = ()) -> str:
    example = _example_channel(channels)
    return "\n".join(
        [
            "`/fil-config <channel> wake <mention|all|off>` — when a shared "
            "channel wakes me:",
            "- **mention** — only @-mentions (and engaged threads) wake me "
            "(the default)",
            "- **all** — every message wakes me",
            "- **off** — messages there never wake me",
            f"Example: `/fil-config {example} wake all`",
            _channels_line(channels),
        ]
    )


def help_guidance(channels: Sequence[tuple[str, str]] = ()) -> str:
    example = _example_channel(channels)
    return "\n".join(
        [
            "`/fil-config <channel> guidance <text…>` — set my standing "
            "guidance for one channel (it frames every wake there). "
            "Everything after `guidance` is kept verbatim.",
            f"- `/fil-config {example} guidance` — show the current guidance",
            f"- `/fil-config {example} guidance clear` — remove it",
            f"Example: `/fil-config {example} guidance Keep replies short; "
            "escalate billing questions to me.`",
            _channels_line(channels),
        ]
    )


def _feature_summary(description: str) -> str:
    """First sentence only — the full description lives in get_features and
    the single-feature show."""
    return str(description).split(". ")[0].rstrip(".")


def help_feature(features: Mapping[str, str] | None = None) -> str:
    lines = ["`/fil-config feature <name> <on|off>` — toggle a runtime feature."]
    features = dict(features or {})
    if features:
        lines.append("**Known features:**")
        for name in sorted(features):
            lines.append(f"- **{name}** — {_feature_summary(features[name])}.")
    lines.append(f"Example: `/fil-config feature {FEATURE_ADVANCED_TOOL_CONTROLS} on`")
    return "\n".join(lines)


def help_for(
    command: str | None,
    *,
    channels: Sequence[tuple[str, str]] = (),
    mcp_servers: object = (),
    other_sources: object = (),
    features: Mapping[str, str] | None = None,
) -> str:
    """The help text for a ``HelpRequest`` — the index, or one subform's
    contextual help built from the same live vocabularies the parser uses."""
    if command == "config":
        return help_config(channels)
    if command == "tools":
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
    # Reply-side interpolation funnel: the (member-editable) name is
    # markdown-escaped here so it renders inert everywhere it appears.
    return f"#{_md_escape(clean)}" if clean else str(room_id)


def _code_ref(room_id: str, channel_name: str) -> str:
    """The channel as the principal should TYPE it — for example commands
    inside code spans. Raw, not markdown-escaped: escape backslashes render
    verbatim inside a code span, so an escaped name would put backslashes
    in the command the principal copies back. Backticks are dropped so a
    hostile name can't terminate the span it renders in (they'd break the
    command's parse anyway)."""
    clean = _sanitize(channel_name or "").replace("`", "")
    return f"#{clean}" if clean else str(room_id)


def _friendly_target(target: str) -> str:
    return target[len(MCP_PREFIX) :] if target.startswith(MCP_PREFIX) else target


def _count_suffix(count: object) -> str:
    """`` (N tools)`` / `` (1 tool)``, or nothing when the count is unknown."""
    if not isinstance(count, int):
        return ""
    unit = "tool" if count == 1 else "tools"
    return f" ({count} {unit})"


def _tools_state_matches(
    friendly: str,
    label: str,
    listed: str,
    state_phrase: str,
    feature_flags: dict,
) -> Mutation:
    """A tools mutation whose grant is already in the desired state. Only a
    true no-op (state matches AND enforcement already on) is reply-only: the
    principal asked for this state to *apply*, so if ``advanced_tool_controls``
    is still off the same write turns it on and the reply says so — otherwise
    the confirmation would describe a policy nothing enforces."""
    if feature_flags.get(FEATURE_ADVANCED_TOOL_CONTROLS):
        return Mutation(
            changed=False,
            reply=f"**{friendly}** {state_phrase} in **{label}** (tools: {listed}).",
        )
    flags = dict(feature_flags)
    flags[FEATURE_ADVANCED_TOOL_CONTROLS] = True
    return Mutation(
        changed=True,
        reply=f"**{friendly}** {state_phrase} in **{label}** — enforcement "
        f"was off, so I turned it on; grants now apply (tools: {listed}).",
        feature_flags=flags,
        sections=("feature_flags",),
    )


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
        grants = [str(g) for g in default] if isinstance(default, list) else list(ROWS)
    listed = ", ".join(grants) or "none (baseline only)"
    if command.verb == "grant":
        if command.target in grants:
            return _tools_state_matches(
                friendly, label, listed, "was already enabled", feature_flags
            )
        grants.append(command.target)
        verbed = "Enabled"
    else:
        if command.target not in grants:
            return _tools_state_matches(
                friendly, label, listed, "isn't enabled", feature_flags
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
        reply=f"✓ {verbed} **{friendly}** in **{label}** (tools now: {now}){flag_note}",
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
            reply=f"Wake mode for **{label}** is already **{command.mode}** — {gloss}.",
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


def apply_guidance(command: GuidanceCommand, channel_instructions: dict) -> Mutation:
    label = _channel_label(command.room_id, command.channel_name)
    mapping = dict(channel_instructions)
    if command.text is None:
        if command.room_id not in mapping:
            return Mutation(changed=False, reply=f"No guidance is set for **{label}**.")
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
        reply=f"✓ Guidance for **{label}** set ({len(command.text)} chars): {shown}",
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


# ── Overview / channel / catalog rendering ───────────────────────────


def _runtime_plugins_line(other_sources: object) -> str | None:
    """The host's own toolsets, grantable per channel as ``toolset:<name>``.
    ``other_sources`` is a name→tool-count mapping (or a plain name sequence,
    shown without counts)."""
    counts = other_sources if isinstance(other_sources, Mapping) else {}
    shown = []
    for name in sorted(_mcp_names(other_sources)):
        clean = _sanitize(str(name))
        if not clean:
            continue
        count = counts.get(name)
        if isinstance(count, int):
            unit = "tool" if count == 1 else "tools"
            shown.append(f"{clean} ({count} {unit})")
        else:
            shown.append(clean)
    if not shown:
        return None
    return (
        "**Runtime plugins on this agent's host** — always available in the "
        "backchannel; grant per channel by name (e.g. `#channel spotify on`): "
        + ", ".join(shown)
    )


def render_config_list(
    *,
    capability_policy: dict,
    wake_policy: dict,
    channel_instructions: dict,
    channels: Sequence[tuple[str, str]],
) -> str:
    """The ``/fil-config list`` reply: one summary line per shared channel —
    override or default grant, wake mode when it differs from the default,
    and a guidance marker — ending with the pointer to the per-channel
    detail form."""
    if not channels:
        return (
            "No shared channels yet — once I'm in one, "
            "`/fil-config list` will summarize it here."
        )
    raw_per = capability_policy.get("per_channel")
    per = raw_per if isinstance(raw_per, dict) else {}
    default_mode = str(wake_policy.get("reactive_wake", "mention"))
    raw_wake_per = wake_policy.get("per_channel")
    wake_per = raw_wake_per if isinstance(raw_wake_per, dict) else {}
    guidance = channel_instructions if isinstance(channel_instructions, dict) else {}
    lines = ["**Channels:**"]
    for room_id, name in channels:
        label = _channel_label(room_id, name)
        entry = per.get(room_id)
        if isinstance(entry, list):
            grants = ", ".join(str(g) for g in entry) or "none (baseline only)"
            parts = [f"tools: {grants} (override)"]
        else:
            parts = ["tools: default"]
        wake_entry = wake_per.get(room_id)
        pinned = (
            wake_entry.get("reactive_wake") if isinstance(wake_entry, dict) else None
        )
        if pinned and pinned != default_mode:
            parts.append(f"wake: {pinned}")
        text = guidance.get(room_id)
        if isinstance(text, str) and text:
            parts.append("guidance set")
        lines.append(f"- **{label}** — " + "; ".join(parts))
    lines += [
        "",
        f"Details: `{PREFIX}config {_example_channel(channels)} show`",
    ]
    return "\n".join(lines)


_EXHAUSTED = object()


def _expand_grant(name: str, custom: Mapping[str, object]) -> tuple[set[str], set[str]]:
    """One grant's ``(names, tools)`` expansion, mirroring
    ``reactive.expand_bundle``: builtin bundles by member list, custom
    bundles by ``@name`` includes (cycle-guarded, custom wins on name
    collision), plain members as tool names, ``mcp:`` names kept as names
    (their live tools are resolved server-side). Fail-closed: unknown
    names expand to themselves with no tools.

    Iterative, not recursive: the include graph comes from the config
    document (data), so neither a deep chain (stack depth) nor a diamond
    of shared includes (exponential re-expansion) may decide whether the
    status view renders. Expansion is context-free, so each name is
    expanded at most once — cycles and diamonds alike just skip."""
    names: set[str] = set()
    tools: set[str] = set()
    stack: list[tuple[str, object]] = []
    expanded: set[str] = set()

    def _enter(nm: str) -> None:
        names.add(nm)
        if nm.startswith(MCP_PREFIX) or nm in expanded:
            return
        expanded.add(nm)
        members = custom.get(nm)
        if isinstance(members, list):
            stack.append((nm, iter(members)))
        elif nm in BUNDLE_MEMBERS:
            tools.update(BUNDLE_MEMBERS[nm])

    _enter(str(name))
    while stack:
        _nm, it = stack[-1]
        member = next(it, _EXHAUSTED)  # type: ignore[call-overload]
        if member is _EXHAUSTED:
            stack.pop()
            continue
        m = str(member)
        if m.startswith("@"):
            _enter(m[1:])
        elif m:
            tools.add(m)
    return names, tools


def _tools_status_lines(
    room_id: str,
    channel_name: str,
    capability_policy: dict,
    feature_flags: dict,
    mcp_servers: object,
    other_sources: object,
) -> list[str]:
    """The tools block of a channel view: the *effective* row state right
    now, whether that grant is the default or a channel override, what's
    off, and the runtime-plugin note. Effective means grants are expanded
    (builtin bundles, deprecated aliases, custom bundles, ``mcp:`` names):
    a row is on iff its name is granted or its member tools are covered —
    rows on only via an alias/bundle carry a "(via …)" suffix."""
    raw_per = capability_policy.get("per_channel")
    per = raw_per if isinstance(raw_per, dict) else {}
    entry = per.get(room_id)
    if isinstance(entry, list):
        grants = [str(g) for g in entry]
        origin = "channel override"
    else:
        default = capability_policy.get("default_capabilities")
        grants = [str(g) for g in default] if isinstance(default, list) else list(ROWS)
        origin = "default grant — no channel override"
    servers = _mcp_names(mcp_servers)
    counts = mcp_servers if isinstance(mcp_servers, Mapping) else {}
    raw_custom = capability_policy.get("bundles")
    custom = (
        {str(k): v for k, v in raw_custom.items()}
        if isinstance(raw_custom, dict)
        else {}
    )
    per_grant = [(grant, *_expand_grant(grant, custom)) for grant in grants]
    granted_names: set[str] = set()
    granted_tools: set[str] = set()
    for _grant, names, tools in per_grant:
        granted_names |= names
        granted_tools |= tools

    # A custom bundle wins any name collision (mirroring the enforcers), so
    # a row/alias *name* only carries its builtin meaning when no custom
    # bundle shadows it. Only a well-formed (list) definition shadows —
    # ``_expand_grant`` falls back to the builtin for anything else.
    shadowed = {k for k, v in custom.items() if isinstance(v, list)}

    def _row_covered(row: str) -> bool:
        if row in granted_names and row not in shadowed:
            return True
        return set(BUNDLE_MEMBERS[row]) <= granted_tools

    def _row_via(row: str) -> list[str]:
        # Every grant that contributes to the row's coverage — by naming it
        # (un-shadowed) or by supplying member tools. Coverage may be split
        # across several bundles; all of them are named.
        members = set(BUNDLE_MEMBERS[row])
        return [
            grant
            for grant, names, tools in per_grant
            if grant != row
            and ((row in names and row not in shadowed) or tools & members)
        ]

    lines = [f"**Tools** ({origin}):"]
    for row in ROWS:
        if not _row_covered(row):
            continue
        suffix = ""
        if row not in grants or row in shadowed:
            sources = _row_via(row)
            if sources:
                suffix = f" (via {', '.join(sources)})"
        lines.append(f"- **{row}** — {ROW_DESCRIPTIONS[row]}{suffix}")
    for grant in grants:
        if (
            grant in ROW_DESCRIPTIONS or grant in DEPRECATED_ALIASES
        ) and grant not in shadowed:
            # Rows render above; an alias's coverage shows as "(via …)" on
            # the rows it grants, not as its own bullet. A custom bundle
            # that *shadows* a row/alias name is a bundle, not that row —
            # it gets its own bullet below.
            continue
        if grant.startswith(MCP_PREFIX):
            server = grant[len(MCP_PREFIX) :]
            suffix = _count_suffix(counts.get(server))
            note = "" if server in servers else " — not currently connected"
            lines.append(f"- **{grant}** — MCP server{suffix}{note}")
        elif grant in shadowed:
            lines.append(f"- **{grant}** — custom bundle")
        else:
            # Fail-closed expansion grants nothing for unknown names; say
            # so instead of dressing a typo up as a working bundle.
            lines.append(
                f"- **{grant}** — unrecognized (grants nothing — check the spelling)"
            )
    if not grants:
        lines.append("- none (baseline only)")
    off = [row for row in ROWS if not _row_covered(row)] + [
        f"{MCP_PREFIX}{server}"
        for server in sorted(servers)
        if f"{MCP_PREFIX}{server}" not in granted_names
    ]
    lines.append("")
    lines.append(f"**Off:** {', '.join(off) if off else 'nothing'}")
    plugins = _runtime_plugins_line(other_sources)
    if plugins:
        lines.append("")
        lines.append(plugins)
    if not feature_flags.get(FEATURE_ADVANCED_TOOL_CONTROLS):
        lines.append("")
        lines.append(
            "Note: `advanced_tool_controls` is off, so this isn't enforced "
            f"yet — any `{PREFIX}config` tools change turns it on."
        )
    return lines


def render_channel_show(
    *,
    room_id: str,
    channel_name: str,
    capability_policy: dict,
    feature_flags: dict,
    wake_policy: dict,
    channel_instructions: dict,
    mcp_servers: object = (),
    other_sources: object = (),
) -> str:
    """The ``/fil-config <channel>`` reply: the channel's full config — the
    tools status block, the wake mode, and the guidance verbatim (or
    "none"). Read-only."""
    label = _channel_label(room_id, channel_name)
    lines = [f"**{label}** configuration:", ""]
    lines += _tools_status_lines(
        room_id,
        channel_name,
        capability_policy,
        feature_flags,
        mcp_servers,
        other_sources,
    )
    default_mode = str(wake_policy.get("reactive_wake", "mention"))
    raw_wake_per = wake_policy.get("per_channel")
    wake_per = raw_wake_per if isinstance(raw_wake_per, dict) else {}
    wake_entry = wake_per.get(room_id)
    pinned = wake_entry.get("reactive_wake") if isinstance(wake_entry, dict) else None
    mode = str(pinned) if pinned else default_mode
    gloss = _WAKE_GLOSS.get(mode)
    suffix = "" if pinned else " (default)"
    lines += [
        "",
        f"**Wake:** {mode}{suffix}" + (f" — {gloss}" if gloss else ""),
    ]
    text = (channel_instructions or {}).get(room_id)
    if isinstance(text, str) and text:
        lines += ["", f"**Guidance:** ({len(text)} chars)"]
        lines += [f"> {line}" for line in text.splitlines()]
    else:
        lines += ["", "**Guidance:** none"]
    servers = _mcp_names(mcp_servers)
    example_target = sorted(servers)[0] if servers else "post"
    ref = _code_ref(room_id, channel_name)
    lines += [
        "",
        "**Examples:**",
        f"- `{PREFIX}config {ref} {example_target} off`",
        f"- `{PREFIX}config {ref} wake all`",
        f"- `{PREFIX}config {ref} guidance Be brief.`",
        "Typos are fine — I'll confirm what I understood.",
    ]
    return "\n".join(lines)


def render_tools_list(
    channels: Sequence[tuple[str, str]] = (),
    mcp_servers: object = (),
    other_sources: object = (),
) -> str:
    """The ``/fil-config tools list`` reply: the full tool catalog —
    built-in rows with their one-liners, connected MCP servers with tool
    counts, and the runtime plugins on the agent's host."""
    example = _example_channel(channels)
    lines = [
        "**Tool catalog** — grantable per shared channel:",
        "",
        "**Built-in bundles:**",
    ]
    lines += [f"- **{name}** — {ROW_DESCRIPTIONS[name]}" for name in ROWS]
    lines.append("")
    names = _mcp_names(mcp_servers)
    if names:
        lines.append(
            "**Connected MCP servers** (grant with or without the `mcp:` prefix):"
        )
        counts = mcp_servers if isinstance(mcp_servers, Mapping) else {}
        for server in sorted(names):
            suffix = _count_suffix(counts.get(server))
            lines.append(f"- `{MCP_PREFIX}{server}`{suffix}")
    else:
        lines.append("**Connected MCP servers:** none")
    plugins = _runtime_plugins_line(other_sources)
    if plugins:
        lines.append("")
        lines.append(plugins)
    lines += [
        "",
        f"Change with `{PREFIX}config <channel> <tool> <on|off>` (e.g. "
        f"`{PREFIX}config {example} linear off` — verbs: on/off, "
        "enable/disable, grant/revoke, allow/deny).",
        "",
        _channels_line(channels),
    ]
    return "\n".join(lines)


def render_feature_list(
    *,
    features: Mapping[str, str],
    feature_flags: dict,
) -> str:
    """The ``/fil-config feature`` / ``/fil-config feature list`` reply:
    every known feature as one bullet — bold name, one-line summary, current
    state — plus the change example."""
    lines = ["**Features:**"]
    for name in sorted(features):
        state = "on" if feature_flags.get(name) else "off"
        glyph = "✅" if state == "on" else "⬜"
        lines.append(
            f"- {glyph} **{name}** — {state} — {_feature_summary(features[name])}"
        )
    lines += [
        "",
        f"Change with `{PREFIX}config feature <name> <on|off>` "
        f"(e.g. `{PREFIX}config feature slash_commands off`); "
        f"`{PREFIX}config feature <name>` shows one feature in full.",
    ]
    return "\n".join(lines)


def render_feature_show(
    *,
    feature: str,
    features: Mapping[str, str],
    feature_flags: dict,
) -> str:
    """The ``/fil-config feature <name>`` reply: that feature's current
    state plus its full description."""
    state = "on" if feature_flags.get(feature) else "off"
    lines = [f"**{feature}** is **{state}**."]
    description = str(features.get(feature, "")).strip()
    if description:
        lines += ["", description]
    lines += [
        "",
        f"Change with `{PREFIX}config feature {feature} <on|off>`.",
    ]
    return "\n".join(lines)


def render_guidance_show(
    *,
    room_id: str,
    channel_name: str,
    channel_instructions: dict,
) -> str:
    """The ``/fil-config <channel> guidance`` reply: the current guidance
    verbatim (blockquoted — it's the principal's own text), plus how to set
    or clear it."""
    label = _channel_label(room_id, channel_name)
    text = (channel_instructions or {}).get(room_id)
    if isinstance(text, str) and text:
        lines = [f"**{label}** guidance ({len(text)} chars):"]
        lines += [f"> {line}" for line in text.splitlines()]
    else:
        lines = [f"No guidance is set for **{label}**."]
    ref = _code_ref(room_id, channel_name)
    lines += [
        "",
        f"Set it with `{PREFIX}config {ref} guidance <text…>`; clear it "
        f"with `{PREFIX}config {ref} guidance clear`.",
    ]
    return "\n".join(lines)
