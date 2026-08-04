# filament-hermes

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) gateway plugin
that connects your agent to [Filament](https://filament.dm). It receives
messages as Firebase Cloud Messaging (FCM) push notifications and sends replies
through Filament's MCP-compatible tools.

## Setup

```
hermes plugins install filament-dm/filament-hermes --enable
hermes gateway restart
```

The install prompts for the agent token from Filament's connect flow (it starts
with `fmcp_`) and saves it to your `~/.hermes/.env`. Start the connect flow in
the Filament app to get one; the app also offers a one-line command that does
all of this for you.

Nothing else to install. The plugin's Python dependencies ship inside it (see
`vendor/`, rebuilt by `scripts/vendor-deps.sh`), because `hermes plugins
install` clones a directory and never runs pip — and on the Docker and cloud
images the venv the gateway imports from is not writable by the gateway anyway.

To update:

```
hermes plugins update filament-fcm && hermes gateway restart
```
