# filament-hermes

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) gateway plugin
that connects your agent to [Filament](https://filament.dm). It receives
messages as Firebase Cloud Messaging (FCM) push notifications and sends replies
through Filament's MCP-compatible tools.

## Setup

Start the connect flow in the Filament app to get an agent token, then:

```
hermes plugins install filament-dm/filament-hermes --enable --force && hermes filament connect fmcp_YOURTOKEN
```

`hermes filament connect` is a command this plugin adds. It validates the token,
saves it, and restarts the gateway. Run it again with a new token to reconnect —
no reinstall needed. Add `--url` to point at a dev or staging cluster. `--force`
on the install makes the same line work whether or not the plugin is already
there, so it is safe to re-run.

### On a shared machine, use `-p`

The token above is on the command line, which your shell writes to its history,
and on Linux other local users can read it out of `/proc/<pid>/cmdline` while the
command runs. That is a fine trade on your own laptop. Where it isn't, `-p` reads
the token from stdin instead, so it never appears in either place:

```
hermes plugins install filament-dm/filament-hermes --enable --force
hermes filament connect -p          # asks for the token, input hidden
```

`-p` also takes a pipe, for scripts:

```
printf %s "$TOKEN" | hermes filament connect -p
hermes filament connect -p < token.txt
```

Installing without a token also works: the install then prompts for one.

Nothing else to install. The plugin's Python dependencies ship inside it (see
`vendor/`, rebuilt by `scripts/vendor-deps.sh`), because `hermes plugins
install` clones a directory and never runs pip — and on the Docker and cloud
images the venv the gateway imports from is not writable by the gateway anyway.

`firebase-messaging` comes from our fork,
[filament-dm/firebase-messaging](https://github.com/filament-dm/firebase-messaging),
branch `filament/integration`, rather than from PyPI. Fixes we need land there
first and are upstreamed from there.

`vendor/` goes on the front of `sys.path`, so what it carries wins over anything
already installed. Nothing else in a Hermes process imports these packages, and a
stale copy left in site-packages by an older install used to shadow the fork
silently — an agent that connects, looks healthy and never wakes. What decides
whether a package is vendored is whether it is in that tree, not path order: if
Hermes ever ships one of them, drop it from `vendor/` and widen its range in
`pyproject.toml`. The plugin warns at startup if it finds a *different* version
outside `vendor/`, which is what that day would look like.

To update:

```
hermes plugins update filament && hermes gateway restart
```
