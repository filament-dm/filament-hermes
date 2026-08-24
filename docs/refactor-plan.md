# Turn path refactor

## Overview

This document plans a refactor of the inbound turn path in `adapter.py`.
The goal is a fixed sequence of six named phases, so that reading one function
tells you the whole order of operations for any inbound event.

`adapter.py` is 2440 lines and holds five unrelated jobs: the staged connect,
administrative traffic, the turn path, `send()`, and the status lifecycle.
This plan covers the turn path only.
Steps 1 and 2 are complete; see [Refactor progress](#refactor-progress).

Terms used throughout:

Term | Meaning
--- | ---
Control plane | The principal's private backchannel. Messages there are commands.
Data plane | Every shared channel. An event is a wake-up signal, and its content is data, not instructions.
Turn | One dispatch of an event to the Hermes agent loop.
Phase | One numbered step of the turn path, with a declared input, output, and I/O budget.

## Problems in the current code

**Four dispatch sites implement the same handoff separately.**
`_handle_control_message`, `_wake`, `_maybe_greet`, and the reaction path each
set four ContextVars, apply session keying, open a status turn, and call
`handle_message`.
They do not agree.
`_maybe_greet` sets no ContextVars at all, so a backchannel greeting runs with
the trust zone at its fail-closed data default and no session keying.

**The cheap rejection checks are duplicated and the copies have drifted.**
Event deduplication and the own-sender check appear in both the message path
and the reaction path.
The check that suppresses Filament's system notices appears only in the message
path, so a system reaction can wake the agent where the equivalent message
cannot.
Nine separate log sites carry hand-written skip reasons.

**A sentinel stands in for normalization.**
`_wake` distinguishes a message from a reaction by testing whether its `data`
argument is `None`, which requires a comment explaining why an empty string is
not the same as `None`.

**Ordering rules exist only in comments.**
Three examples: drop an event before doing any paid work; check the local
engagement record before the API call that classifies a sender; apply session
keying with no `await` between it and the handoff.
Each rule is correct and each is invisible in the structure of the code.

## The refactored pipeline

Every inbound event becomes one `InboundEvent` at the FCM boundary and runs
through the same six phases.
Administrative traffic branches off before phase 1 and never enters the
pipeline.

```python
async def run_turn(self, event: InboundEvent) -> None:
    # 1. Rules no policy can override.
    if drop := unconditional_filter(event, self._seen, self._user_id):
        return self._log_drop(event, drop)
    # Every fresh read below sees the server-held config.
    await self._server_config.sync()
    # 2. Which plane, mode, and session does the event belong to?
    route = self.route(event)
    if route.handler is Handler.SLASH:
        return await self._slash.run(event, route)  # never reaches inference
    # 3. Does the principal's wake policy want a turn spent here?
    if drop := await self.wake_policy(event, route):
        return self._log_drop(event, drop)
    grant = self.grant(route)  # 4. which tools apply?
    prompt = await self.assemble(event, route, grant)  # 5. build context
    await self.dispatch(  # 6. hand off to Hermes
        TurnPlan(event, route, grant, prompt)
    )
```

Each phase name resolves to one module.

Phase | Function | I/O budget | Returns
--- | --- | --- | ---
1 | `unconditional_filter` | None | `Drop` or `None`
2 | `route` | File reads | `Route(plane, mode, session_scope, reply_target, handler)`
3 | `wake_policy` | 1 API call at most | `Drop` or `None`
4 | `grant` | File reads | `Grant(tools, hint)`
5 | `assemble` | 2 API calls at most, run concurrently | `Prompt(text, breadcrumb)`
6 | `dispatch` | Hands off to Hermes | Nothing

The phases run cheapest first, so what a dropped event costs depends on how
far it got:

-   Dropped by `unconditional_filter`: nothing. No configuration is read and no
    request is made.
-   Dropped by `wake_policy`: the config sync and the policy files needed to
    reach that decision, plus at most one sender-classification call. No
    message content is fetched and no turn is spent.

Today that ordering is a property of where statements sit inside a 150-line
method rather than of the structure.

### `unconditional_filter` vs `wake_policy` filter

Both phases can drop an event.
The split is whether configuration has a say:

-   **Phase 1, `unconditional_filter`.** Rules the principal cannot turn off.
    The event was already handled, the agent sent it, or Filament's system
    user sent it. No configuration is consulted, which is also why the phase
    is free.
-   **Phase 3, `wake_policy`.** The principal's own per-channel choice,
    retuned conversationally from the backchannel with no restart.

Order matters, not just grouping. A principal may configure 👀 as a wake
trigger, and the adapter adds 👀 to every message it handles, so if that guard
sat in phase 3 the policy would admit it and the agent would answer its own
marker in a loop.

The config sync between the two phases is what forces them apart. Merging them
would mean either syncing on every duplicate push, or reading policy the sync
has not yet refreshed.

Both functions return a `Drop` to mean dropped and `None` to mean continue, so
the two call sites read the same way despite the names reading differently.

## Drop reasons

Drops become a closed set rather than nine string literals spread across two
methods.
The enum value is the string written to the structured log, and the existing
values are kept verbatim so log queries keep working.

```python
class DropReason(StrEnum):
    # Phase 1, the unconditional filter: rules no policy can override.
    DUPLICATE = "event_id_seen"
    OWN_MESSAGE = "own_message"
    OWN_REACTION = "own_reaction"
    PROCESSING_MARKER = "processing_reaction"
    SYSTEM_NOTICE = "system_notice"

    # Phase 2, routing: the event has no turn to spend in its plane.
    BACKCHANNEL_REACTION = "backchannel_reaction"

    # Phase 3, wake policy: the principal's per-channel choice.
    WAKE_POLICY = "wake_policy"
```

Grouping the reasons by phase means a log line identifies which phase stopped
the event, which the current flat strings do not.

The middle group has one member and is the reason the taxonomy has three
groups rather than two.
A reaction in the backchannel is not a wake signal, but it is neither an
unconditional rule nor a policy decision: the control plane simply has nothing
to do with it.
That makes it a routing outcome, and phase 2 is where it belongs.

`WAKE_POLICY` currently covers five distinct rules: the channel is muted, the
agent was not mentioned, the emoji is not a trigger, thread waking is off, and
the sender is another agent.
The current code separates them only through ad hoc extra log fields.
Add a `detail` field on `Drop` for the specific rule rather than new top-level
values, so existing queries continue to match.

## Example traces

### Trace A: a mention in a shared channel

Input: `@agent what's the deploy status?` from `@alice:filament.dm` in `#eng`,
event `$evt1`, top level, with the server's mention flag set.

Phase | Decision
--- | ---
1 filter | `$evt1` is unseen, the sender is not the agent, and the sender is not Filament's system user. Continue.
— | Sync the server-held config, so every read below sees current policy.
2 route | The room is not the backchannel, so the plane is data. No thread and the channel's default reply style give mode `CHANNEL`, session scope `("channel", "!eng:filament.dm")`, and a reply threaded off `$evt1`.
3 wake policy | The mention flag admits the event. Continue, and record `$evt1` as a thread the agent is engaged in.
4 grant | The `advanced_tool_controls` flag is off, which is the default, so the turn is ungated and gets no capability hint.
5 assemble | Strip the mention, look up attachments, read the standing instructions and the channel's guidance, and count unseen history. Build the envelope.
6 dispatch | Activate the turn context, apply session keying, open the status turn, and call `handle_message`.

Variant: with `advanced_tool_controls` on, phase 4 instead resolves the
channel's granted tool bundles into an explicit set of tool names and builds
the matching capability hint.
Phase 6 pins that set, and the `pre_tool_call` gate denies anything outside it.
An unlisted channel resolves to the minimal default profile, never to an
unrestricted turn.

### Trace B: an @everyone broadcast, dropped at phase 3

Input: `@everyone standup in 5` in `#eng`.

Phase | Decision
--- | ---
1 filter | Passes.
2 route | Plane is data, mode is `CHANNEL`.
3 wake policy | An `@everyone` mention is not a mention of the agent, because one broadcast must not wake every agent in a channel at once. The channel's default policy needs a mention, so the event drops with `WAKE_POLICY`.

The cost is the config sync and the channel's wake policy. No attachment
lookup, no history read, no standing instructions read, and no turn.

### Trace C: the agent's own processing marker, dropped at phase 1

The adapter adds a 👀 reaction to `$evt1` while handling trace A, and that
reaction arrives back as a push.

Phase | Decision
--- | ---
1 filter | The reaction is a new event, so deduplication passes, but the sender is the agent itself. Drops with `OWN_REACTION`.

Two independent guards catch this case, and both are in phase 1: the
own-sender check, and the list of reactions the adapter itself adds.
The second guard covers a different user adding 👀 to a channel where the
principal configured 👀 as a wake trigger.
If either guard were missing, the agent would wake itself in a loop.

This trace also shows the drift described above.
The reaction path has its own copy of the deduplication and own-sender checks
but no system-notice check, so a system reaction reaches phase 3 where the
equivalent message would already have dropped.

### Trace D: a slash command, diverted at phase 2

Input: `/fil-config #eng wake all` in the backchannel.

Phase | Decision
--- | ---
1 filter | Passes.
2 route | The room is the backchannel, so the plane is control. The body starts with `/fil-` and the `slash_commands` flag is on, so the handler is `SLASH`.
— | The pipeline hands off to the slash runtime and returns. Phases 3 through 6 do not run.

Slash commands must never reach the model, in success or in failure.
In the pipeline that rule is one line in `run_turn`, rather than an early
return part way through the control-plane handler.

### Trace E: an emoji reaction that wakes the agent

Input: 🔥 added by `@bob:filament.dm` to `$evt1` in `#eng`, in a channel where
the principal listed 🔥 as a wake trigger.

Phase | Decision
--- | ---
1 filter | The reaction is unseen, the sender is not the agent, and 🔥 is not one of the markers the adapter adds. Continue.
2 route | Plane is data. A reaction always anchors its reply to the message reacted to, so the reply threads off `$evt1`.
3 wake policy | The channel lists 🔥 as a trigger. Continue.
4 grant | As trace A.
5 assemble | A reaction carries no body, so the event-data block is a stand-in that names the emoji and tells the agent to read `$evt1` and its thread. There is no mention to strip.
6 dispatch | As trace A, except that the status turn is keyed on the reaction's own event id. Several reactions can share one target message, and keying on the target would let them collide.

This trace is where the `data is None` sentinel disappears.
The normalized event states its kind, so phase 5 selects the reaction data
block by kind rather than by inferring it from a missing argument.

### Trace F: a backchannel command

Input: `remind me to check the deploy at 5` from the principal in the
backchannel.

Phase | Decision
--- | ---
1 filter | Passes. The same guards apply in both planes.
2 route | The room is the backchannel, so the plane is control and the mode is `BACKCHANNEL`. The body does not start with `/fil-`, so the handler is the model. The reply goes on the main timeline unless the principal wrote inside a thread.
3 wake policy | Returns `None`. The wake policy governs shared channels only, so a control-plane command is always admitted.
4 grant | The control plane keeps full capability, so the turn is ungated.
5 assemble | Name the speaker in the framing, recognizing the principal by server-attributed id rather than display name. Do not wrap the body as untrusted data. Look up attachments and count unseen history as in trace A.
6 dispatch | Activate the control turn context, which carries full capability, no read-cursor authority, and no reply anchor as one value.

This trace is the case that today has its own 120-line method duplicating
attachment lookup, history counting, framing, and dispatch.
Routing it through the same phases is what makes that duplicate deletable.

### Trace G: a thread reply with no mention

Input: `@carol:filament.dm` replies inside thread `$evt1`, the thread trace A
created, without mentioning the agent.

Phase | Decision
--- | ---
1 filter | Passes.
2 route | Plane is data, mode is `THREAD`, session scope is `("thread", "$evt1")`. A thread turn joins the thread's conversation, not the channel's.
3 wake policy | Not a mention. The agent's engagement record lists `$evt1` and the channel's thread waking is set to `engaged`, so classify the sender with one API call. `@carol` is not an agent, so the event is admitted.
4–6 | As trace A.

This is the only API call any drop phase makes, and it is last in the
condition on purpose: the two cheap local checks short-circuit it, so the call
happens only for a thread the agent is already engaged in.

The negative case matters as much.
If the sender were another agent, or if the classification call failed, the
check fails closed and the event drops with `WAKE_POLICY` and detail
`sender_is_agent`.
Agents must not wake each other without an explicit mention, or two subscribed
agents will answer each other indefinitely.

## Module layout

Use flat modules rather than a `turn/` package.
The test loaders build a stand-in `hermes_filament_fcm` module and register
submodules by dotted name, so a subpackage would need extra registration in
every test file.
Flat modules also match the existing `slash.py`, `timeline.py`, and
`reactive.py`.

Module | Lines | Contents
--- | --- | ---
`adapter.py` | ~450 | Connect stages, `send()`, and the FCM callback bridge
`turn.py` | ~80 | `run_turn` and drop logging
`events.py` | ~140 | `InboundEvent`, `Drop`, `DropReason`, `Route`, `Grant`, `TurnPlan`
`filters.py` | ~90 | Phase 1, `unconditional_filter`
`route.py` | ~140 | Phase 2
`admission.py` | ~160 | Phase 3, `wake_policy`
`grant.py` | ~90 | Phase 4
`assemble.py` | ~220 | Phase 5
`dispatch.py` | ~110 | Phase 6
`framing.py` | 216 | Every string the model sees. Complete.
`turn_context.py` | 137 | The per-turn authority value. Complete.
`admin.py` | ~350 | Ping, invite, vouch, heartbeat, update check, greeting
`slash_runtime.py` | ~180 | The slash result dispatch table

`reactive.py` is 1414 lines and needs the same treatment afterwards, split into
`stores.py` and `capabilities.py`.

## Design changes

1.  **One turn context behind one ContextVar.**
    Complete.
    A dispatch site can no longer configure part of a turn's authority.

2.  **A drop is a value, not an early return.**
    Each phase returns `Drop(reason)` and `run_turn` logs it in one place.
    See [Drop reasons](#drop-reasons).

3.  **One shared unconditional filter.**
    Deduplication, the own-sender check, the system-notice check, and the
    processing-marker guard all run against the normalized event, so the
    message and reaction paths cannot drift apart again.

4.  **The control plane runs through the pipeline.**
    Control becomes `Route(plane=CONTROL, mode=BACKCHANNEL)`, and `assemble`
    selects framing by plane.
    This deletes a parallel implementation of attachment lookup, history
    counting, framing, and dispatch.
    It also lets `assemble` run its two lookups concurrently, which the
    current code cannot do because they sit in different methods.

5.  **`dispatch` owns the handoff.**
    One function activates the turn context, applies session keying, opens the
    status turn, and calls `handle_message`, in that order and with no `await`
    between the keying and the handoff.

## Testing

Adapter-level tests currently carry about 40 lines of stub setup each, in five
separate files, because `adapter.py` cannot be imported without Hermes
installed.

Phases 1, 2, and 4, and the framing part of phase 5, are pure functions.
They test with no stubs at all, the way `slash.py` and `timeline.py` already
do.
`tests/test_framing.py` and `tests/test_turn_context.py` are the first two
examples: 30 tests that each load one stdlib-only module in three lines.

The seven traces above are the acceptance tests for the finished pipeline.
Each asserts one `TurnPlan` or one `Drop`, which needs no Hermes and no
network.

Caution: a module loaded standalone must be registered in `sys.modules` before
`exec_module` if it defines a dataclass, because `dataclasses` resolves
annotations through `sys.modules[cls.__module__]`.
See the loader in `tests/test_turn_context.py`.

## Refactor sequencing

Steps fall into three kinds, and the kind determines how each one is verified.

Kind | Meaning | Verification
--- | --- | ---
A | No behavior change | Differential test against the replaced code
B | Call sites change, behavior does not | Existing suite
C | Deliberate behavior change | Ships alone with a new test

Step | Description | Kind | Status
--- | --- | --- | ---
1 | `framing.py`: extract every model-facing string | A | Complete
2 | `turn_context.py`: collapse the four ContextVars | B | Complete
3 | `turn.py` and `events.py`: the driver, `InboundEvent`, and `DropReason`, with each phase a shim delegating to existing code | B | Next
4 | Move the shim bodies into the phase modules, one at a time | B | |
5 | Route the control plane through the pipeline and delete `_handle_control_message` | B | |
6 | Add the system-notice check to the reaction path | C | |
7 | Make the greeting a control-plane turn with session keying applied | C | |
8 | `admin.py`, then split `reactive.py` | B | |

Step 3 introduces the pipeline before moving any logic into it.
`InboundEvent` cannot be built earlier: its fields are determined by what the
six phases consume, so defining it first means guessing the shape and then
writing a layer inside each of the three current handlers to unpack it again.
Both would be deleted in step 4.

Steps 6 and 7 fix the two bugs this plan identifies.
They are deferred so that each arrives in a commit that describes it, rather
than inside a large mechanical change.

## Refactor progress

Steps 1 and 2 are complete.

-   `framing.py` holds the wake envelope, the control-plane sender line,
    metadata sanitization, and the attachment note.
    A differential test against the replaced code covered 3,984 input
    combinations with no difference in output.
-   `turn_context.py` replaces four ContextVars with one frozen value.
    Two `activate()` calls replace eight scattered assignments, and ten
    repeated zone guards in `__init__.py` now call one predicate.
-   The suite grew from 544 to 575 tests with no new stub setup.

Two known items are unchanged on purpose.
The greeting still activates no turn context, which is step 7.
`test_plugin_manifest_version_matches_pyproject` fails on `main` because
`plugin.yaml` and `pyproject.toml` disagree on the version, which is unrelated
to this work.
