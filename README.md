# Snooze

Snooze is a reminder plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It repeats a reminder until you confirm it, then can ask the agent to do a follow-up. Reminders are sent by the gateway without an LLM call.

## Install

Requires Hermes Agent 0.22.7 or later and Python 3.10+.

```bash
hermes plugins install AIverflow/hermes-snooze
hermes plugins enable snooze
hermes gateway restart
```

Or clone this repository into `~/.hermes/plugins/snooze`, enable the plugin, and restart the gateway.

## Start

From a supported chat, create a reminder:

```text
/snooze Take my pill in 30m every 5m
```

Reply `Ok` to stop it, or use `/snooze list` to see active reminders. Run `/snooze test` to check delivery and confirmation.

The plugin can use the chat where a reminder was created, or one configured Telegram or Discord home channel. See [setup and configuration](docs/user-guide.md#setup-and-configuration) for details.

## Limits

- The Hermes gateway must be running on an awake machine for reminders and replies to work.
- Snooze does not support voice-note confirmations or WhatsApp Cloud API.
- Reaction confirmations are supported only in Telegram private chats.
- Discord and Telegram destinations must be configured in Hermes.

See the [user guide](docs/user-guide.md) for commands, confirmation rules, configuration, troubleshooting, and development.
