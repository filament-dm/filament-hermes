# filament-hermes

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) gateway plugin
that connects your agent to [Filament](https://filament.dm). It receives
messages as Firebase Cloud Messaging (FCM) push notifications and sends replies
through Filament's MCP-compatible tools.

## Setup

You don't install this by hand. The Filament app gives you a one-line connect
command from the agent connect flow — copy it and paste it into your terminal
on the machine running your Hermes Agent. It installs this plugin, prompts for
anything it needs, and connects your agent.
