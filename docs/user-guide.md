# Snooze user guide

This guide covers how to create and manage reminders.

## Setup and configuration

For installation and gateway startup, see the [README](../README.md).

Plugin settings go under `plugins.entries.snooze.settings` in the active profile's `config.yaml`. The default profile uses `~/.hermes/config.yaml`; a named profile uses `~/.hermes/profiles/<name>/config.yaml`:

```yaml
plugins:
  entries:
    snooze:
      settings:
        validate_setup: false
        cap_minutes: 120
        cap_reminders: 10
        timezone: system
        notification_platform: source
```

| Setting | Default | Purpose |
|---|---:|---|
| `validate_setup` | `false` | When enabled, a new reminder waits up to 10 minutes for `yes` before starting. |
| `cap_minutes` | `120` | Maximum time a reminder repeats after its first reminder, unless `until` is set. |
| `cap_reminders` | `10` | Maximum reminders sent for one snooze. |
| `timezone` | `system` | IANA time zone (for example, `Europe/Paris`) or `system` for the machine's time zone. |
| `notification_platform` | `source` | `source` sends to the originating chat; `telegram` or `discord` sends to that platform's Hermes home channel. |

Hermes owns the home channel setting. Configure the bot and select its destination using Hermes' [Telegram setup](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram) or [Discord setup](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/discord). Then set `notification_platform` to the desired platform. This is one destination per profile; reminders created elsewhere, including from the CLI, go to that home channel. Restart the gateway after changing settings.

## Create a reminder

Use `/snooze` followed by the reminder text and optional timing clauses:

```text
/snooze <what> [at <time> | in <duration>] [every <duration>] [until <time>] [daily | weekdays | on <days>] [ok <word>] [then <action>]
```

| Clause | Meaning |
|---|---|
| `at <time>` | First reminder at a time of day. If that time has passed, it means tomorrow. |
| `in <duration>` | First reminder after a delay. Use either `at` or `in`. |
| `every <duration>` | Repeat interval. Defaults to 10 minutes. |
| `until <time>` | Stop at the first such time after the first reminder. Without it, configured caps apply. |
| `daily`, `weekdays`, `on <days>` | Start a new occurrence every day, Monday to Friday, or on listed days (for example, `on mon,thu` or `on mon-fri`). |
| `ok <word>` | Require a word, emoji, or matching rule to confirm. See [Confirmations](#confirmations). |
| `then <action>` | Ask the agent to do something after confirmation. See [Follow-up actions](#follow-up-actions). |

Times use the configured time zone: `8am`, `8:30am`, `4pm`, or `16:00`. Durations accept forms such as `10m`, `1h`, `90s`, `1h30m`, `10 min`, `1 hour`, `2hours`, `30 minutes`, or `1 h 30 min` (units `s`, `m`, `h`, also `sec`/`secs`/`second`/`seconds`, `min`/`mins`/`minute`/`minutes`, and `hr`/`hrs`/`hour`/`hours`). The minimum is one minute.

Clauses can appear in any order after the reminder text. The parser reads clauses from the end; text before the clauses is the reminder. For example, `Call mom before 6pm at 4pm` has reminder text `Call mom before 6pm`. A short form is also supported: `/snooze Wake up 8am 10m` means `at 8am every 10m`; a trailing time alone means `at` that time. A time after `before`, `by`, `after`, `around`, `since`, `from` or `to` stays part of the text. Clause keywords are case-insensitive.

Examples:

```text
/snooze Wake up at 8am every 10m
/snooze Wake up 8am 10m
/snooze Take my pill in 30m every 5m ok taken
/snooze Send the report at 4pm ok contains:sent
/snooze Stretch in 1h every 20m ok 💪
/snooze Take my pill at 8am daily
```

The creation reply summarizes the reminder, its schedule and destination, and shows how to confirm it, including `/snooze ok <id>` as a fallback. Every reminder message includes its snooze ID so overlapping reminders can be identified.

## Commands

| Command | Purpose |
|---|---|
| `/snooze <what> ...` | Create a reminder. |
| `/snooze list` | Show reminders that are active or waiting for setup confirmation. |
| `/snooze ok [id]` | Confirm from this client. Without an ID, confirms the running snooze that sent a reminder most recently. |
| `/snooze later [N] [id]` | Delay by `N` minutes (`30`, `30m`, `1h`). Without a delay, uses the reminder interval. |
| `/snooze cancel <id>` | Stop without confirming. |
| `/snooze yes [id]` | Start a reminder waiting for setup confirmation. Without an ID, selects the newest one. |
| `/snooze test` | Send one test reminder and check confirmation. |

`later` is measured from now, except before a reminder's first delivery, when it is measured from that delivery time. `/snooze test` sends within about 15 seconds; reply `Ok` within two minutes. It reports immediately if the gateway is not running for the profile. A bare `yes` works in a chat; from the CLI use `/snooze yes <id>`.

If a command name is followed by text that does not fit that command, Snooze treats the input as a new reminder when it parses as one. For example, `/snooze cancel the order at 4pm` creates a reminder.

## Confirmations

By default, reply `Ok` to confirm. The `ok` clause can change the rule:

| Rule | Accepted confirmation |
|---|---|
| omitted or `ok` | `Ok` |
| `ok <word>` | That word |
| `ok <emoji>` | That emoji as a message or reaction |
| `ok contains:<word>` | Any message containing the word |
| `ok any` | Any message |
| `ok <a>|<b>|<c>` | One listed option, by number or option text; the answer is passed to the follow-up action |

Matching ignores case, extra spaces, punctuation, emoji skin tones and common check-mark variations. `OK!`, `ok 👍🏽` and `✔️` match their corresponding rules.

`later`, `later 30`, `later 1h` and `not now` always postpone the reminder, even with `ok any`. For word and emoji rules, extra text can both confirm and continue to the agent: `ok, and book a table for 8`. For `any` and `contains`, the whole message is used as the answer and consumed.

For a choice, every reminder shows numbered options; reply with the number or option text. For example, `/snooze Dinner? at 8pm ok asian|french|italian then order it` can be answered with `2`, and the agent receives `french` with the follow-up action. `/snooze ok <id>` cannot answer a choice; reply in the chat instead.

A reply to a reminder message selects that snooze. Otherwise, Snooze selects the active reminder that sent most recently. Slash commands continue to Hermes and are not treated as answers. A whole-message `yes` starts a reminder waiting for setup confirmation.

Only the user and chat the reminder was sent to can confirm it. Reactions do not carry a user ID, so reaction confirmation is accepted only for emoji rules in private chats and only on the latest reminder message.

## Follow-up actions

When a reminder with `then` is confirmed, Snooze reports that it is handing the action to the agent, then asks Hermes to continue in the same chat. A text confirmation can be reused as the agent's message; any extra text is included. A reaction or `/snooze ok` has no text to reuse, so Hermes must allow gateway injection.

To enable that case, set `allow_gateway_injection: true` directly under the plugin entry (not under `settings`) in the active profile's `config.yaml`, then restart the gateway:

```yaml
plugins:
  entries:
    snooze:
      allow_gateway_injection: true
```

From CLI or Desktop, `/snooze ok` confirms the reminder but does not run its follow-up. If Hermes refuses the follow-up, the reminder remains done and the chat displays the failure. The agent's normal tool approval rules still apply.

## Agent tool

The agent can create reminders with `snooze_create`. It requires `text` and `first_at` (ISO 8601 in local time); `every`, `until`, `confirm`, and `then` are optional. Its argument details live in the tool schema in `commands.py`; its response uses the same summary shown for a chat command.

## Lifecycle and stored data

| State | Meaning |
|---|---|
| `pending` | Waiting for `yes` when setup validation is enabled. If not confirmed within 10 minutes, it becomes `missed`. |
| `active` | Reminders are being sent. |
| `done` | Confirmed. |
| `missed` | Setup was not confirmed in time or a configured cap was reached. |
| `cancelled` | Stopped with `/snooze cancel`. |

A recurring snooze returns to `active` after each answer or miss; only `/snooze cancel <id>` ends the series.

Snoozes are stored per profile in the plugin data directory. `snoozes.json` contains records with UTC ISO 8601 timestamps and is updated atomically. `snoozes.lock` protects changes to the data file; `sender.lock` ensures only one process sends reminders at a time.

## Troubleshooting and limits

- **No reminders arrive:** keep the profile's gateway running on an awake machine. A sleeping computer cannot send notifications. After a delay, Snooze sends one late reminder rather than a burst.
- **Gateway is paused:** `hermes pause` holds reminders. `/snooze list` shows that state; after `hermes resume`, one late reminder is sent for each snooze.
- **No confirmation:** allow the bot's notifications and make sure the chat is not muted. Replies must come from the destination chat and user.
- **No reaction confirmation:** reactions work only on Telegram private chats, only for emoji confirmation rules, and only on the latest reminder message.
- **No voice-note confirmation:** confirmations currently support text, emoji and the eligible Telegram reactions.
- **WhatsApp Cloud API:** unsupported because it cannot send reminders after its 24-hour messaging window.

The gateway timer starts when the plugin loads. If the gateway loop is not ready then, the first incoming message starts it; a reminder due earlier will be sent late. `/snooze test` and the creation summary report whether a gateway is running.

## Development

```bash
python3 -m unittest discover -s tests -t .
ruff check .
ruff format .
```

The repo-level [AGENTS.md](../AGENTS.md) describes code layout and contributor conventions.
