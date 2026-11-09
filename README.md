# hermes-snooze

hermes-snooze is a plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It repeats an LLM-free reminder in your chat until you answer it or a configured limit is reached.

Add a follow-up and your agent acts after you answer. Add choices and it acts on the option you pick. The command is `/snooze`.

## Before you start

hermes-snooze sends its reminders through a messaging platform, so **Hermes needs at least one platform configured** (for example Telegram or Discord). Without one there is no chat to remind you in. Requires Hermes Agent 0.22.7 or later and Python 3.10+.

| Platform | Status |
|---|---|
| Telegram | ✅ Tested (private chat) |
| Discord | 🔧 In progress |
| Other Hermes platforms (Slack, Signal, …) | 🙋 Testers wanted |

WhatsApp Cloud API is not supported.

## What you can do

Every example repeats its reminder every 10 to 15 minutes until you answer `Ok`, or until a limit is reached.

### 1. Simple

```text
/snooze Polish Zeus's thunderbolts in 20m every 10m
/snooze Drink your ambrosia at 8am daily every 10m
/snooze Deliver the messages from Olympus at 16:00 every 15m until 18:00
/snooze Polish my winged sandals at 20:00 weekdays every 10m
```

### 2. With a follow-up action (`then`)

Reply `Ok`: the agent gets the `then` text and does it, with the tools you gave it: write files, send email, edit Google Docs, run scripts, run skills…

```text
/snooze Steal Apollo's cattle at 23:00 every 10m then write my alibi to alibi.md
/snooze Invent the lyre in 1h every 10m then create a Google Doc called "Lyre v1" with a short ode to it
/snooze Send Zeus's love letter in 30m every 10m then email hera@olympus.example that I was delivering messages all night
/snooze Review the release notes at 17:00 every 10m then push main, tag the release and run my social-media skill to announce it
/snooze Check last night's backup at 08:30 weekdays every 15m then run ./verify-backup.sh and show me the result
```

### 3. With choices (`ok a|b|c`)

Each reminder lists the options. Reply with an option or its number: the agent gets your choice.

```text
/snooze Whom do I bless today? in 10m every 10m ok merchant|traveler|thief then add my choice to blessings.md
/snooze Dinner on Olympus at 18:30 every 10m ok ambrosia|nectar|feast then add my choice to the Google Doc "Olympus menu"
/snooze Zeus is calling. Pick up? in 5m every 10m ok answer|hide then email zeus@olympus.example my choice
/snooze Nightly backup failed. Retry? in 5m every 10m ok retry|skip then if I chose retry, run ./backup.sh and show me the log
/snooze Ship to production? at 17:30 every 10m ok ship|hold then if I chose ship, run ./deploy.sh prod and announce it with my release-notes skill
```

For every clause and rule, see the [user guide](docs/user-guide.md#create-a-snooze).

## Install

```bash
hermes plugins install AIverflow/hermes-snooze
hermes plugins enable snooze
hermes gateway restart
```

Or clone this repository into `~/.hermes/plugins/snooze`, enable the plugin, and restart the gateway.

## Start

In a Telegram private chat or Discord chat already connected to Hermes, check delivery first:

```text
/snooze test
```

Reply `Ok` to the test reminder. Once the test passes, try your first snooze:

```text
/snooze Wake Zeus up in 1m every 1m
```

About a minute later you receive a message like this, and a new one every minute until you answer:

```text
🔔 Snooze #12: Wake Zeus up  (reply "Ok" / later)
```

Reply `Ok` when done: Snooze answers `✅ Done: Wake Zeus up` and stops. Reply `later 15` to postpone by 15 minutes. To cancel instead, use `/snooze cancel 12` with the ID in your reminder.

You can also ask Hermes in plain language: "Remind me to update Hermes in 1 hour, then every 10 minutes until I confirm."

For [setup](docs/user-guide.md#setup-and-configuration), [commands](docs/user-guide.md#commands), [confirmations](docs/user-guide.md#confirmations), [recurrence](docs/user-guide.md#create-a-snooze), and [script use](docs/user-guide.md#create-from-scripts), see the user guide.

## Limits

- The Hermes gateway must be running on an awake machine for reminders and replies to work.
- Snooze does not support voice-note confirmations or WhatsApp Cloud API.
- Reaction confirmations are supported only in Telegram private chats.
- At least one of Telegram or Discord must be configured in Hermes.
