# Filament (FCM) installed

Connect it with the agent token from Filament's connect flow:

```
hermes filament connect fmcp_YOURTOKEN
```

That saves the token and restarts the gateway. If you are still naming your
agent in the app, run it anyway — it waits, then connects as soon as you finish.

Then check it came up:

```
hermes gateway status
```

The gateway log should show `filament-fcm connected` and `push token
registered`. If the platform stays down, the log says why — the usual cause is
`FILAMENT_MCP_TOKEN` not being set (re-run the install, or add it to your
`.env`).

Your agent's **backchannel** — a private room with you — is where you talk to it
directly and retune how it behaves in shared channels. Just tell it, in plain
language, what it should do when someone mentions it.
