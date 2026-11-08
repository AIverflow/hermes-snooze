# Snooze

Snooze is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for reminders that can repeat daily, ask you to choose an answer, and be created from scripts. The gateway sends reminders without an LLM call.

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

For [setup](docs/user-guide.md#setup-and-configuration), [commands](docs/user-guide.md#commands), [confirmations](docs/user-guide.md#confirmations), [recurrence](docs/user-guide.md#create-a-reminder), and [script use](docs/user-guide.md#create-from-scripts), see the user guide.

## Limits

- The Hermes gateway must be running on an awake machine for reminders and replies to work.
- Snooze does not support voice-note confirmations or WhatsApp Cloud API.
- Reaction confirmations are supported only in Telegram private chats.
- Discord and Telegram destinations must be configured in Hermes.
