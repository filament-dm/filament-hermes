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

To update:

```
hermes plugins update filament && hermes gateway restart
```

## Choosing a model

By default your agent speaks only when someone mentions it. Almost any model can
do that.

You can also let it watch a whole channel and speak only when it has something to
add. That job is harder, because the right answer is usually to say nothing. A
model that isn't up to it will answer messages meant for other people, repeat an
answer it already gave, or post a line explaining why it has nothing to say.

We tested fifteen models on that job in July 2026. These made no mistakes:

| | |
|---|---|
| Claude | Opus 5, Sonnet 5, Haiku 4.5 |
| OpenAI | GPT-5.4 mini, GPT-5.4 nano, GPT-OSS 120B |
| Google | Gemini 3.6 Flash, Gemini 3.1 Flash Lite |
| Other | GLM 5.2, Kimi K2.6, Qwen3.6 Plus, DeepSeek V4 Flash |

These made mistakes, and we recommend picking from the table instead: Mistral
Medium 3.1, GPT-OSS 20B, Qwen3.6 35B-A3B.

These results depend on the instructions your agent follows, which this plugin
sets up for you. If you rewrite them, your model may behave differently.
