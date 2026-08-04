# Filament (FCM) installed

Two things left, and the first one is probably already done:

1. **Finish the connect flow in the Filament app** — naming your agent is what
   creates it. Until then the gateway will report the agent as reserved and keep
   retrying, which is harmless.

2. **Restart the gateway** so it loads the plugin:

   ```
   hermes gateway restart
   ```

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
