# snooze

A reminder for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that **repeats on your phone until you confirm it**, then can hand a follow-up to the agent. Reminders are sent by the plugin itself: zero LLM calls per reminder.

```
/snooze Send email to John Doe at 4pm every 10m until 6pm then close ticket 42
```

## Status

Work in progress. Working today: the `/snooze` command and all its subcommands, the `snooze_create` tool, the parser, the confirm matcher and the store. **Not wired yet:** sending the reminders on a timer, catching `ok` / `later` / `yes` replies from the chat, running the `then` action, and `/snooze test`. Until then, manage snoozes with the `/snooze ok|later|cancel|yes` subcommands.

## Install and first steps

Requires Hermes Agent 0.22.7 or later and Python 3.10+.

1. Install and enable the plugin:

   ```bash
   hermes plugins install AIverflow/hermes-snooze
   hermes plugins enable snooze
   ```

   Or clone this repo into `~/.hermes/plugins/snooze` and run the second command.

2. Restart Hermes (and the gateway, if you use one).
3. Send a first snooze from a chat (Telegram, Discord, ...) or from the CLI:

   ```
   /snooze Take my pill in 30m every 5m
   ```

4. The plugin answers with an echo: what, when (in your time zone), how to stop it, where it goes, how to undo it.
5. By default a new snooze waits for you to reply `yes` (within 10 minutes) before it starts. Reply `yes` or send `/snooze yes`.
6. When the reminders arrive, reply `ok` to stop them, or `later 30` to push them back 30 minutes.

From the CLI (no chat), reminders go to the first home channel set in your profile.

## Writing a snooze

```
/snooze <what> [at <time> | in <duration>] [every <duration>] [until <time>] [ok <word>] [then <action>]
```

| Part | Meaning |
|---|---|
| `<what>` | The reminder text. Required. |
| `at <time>` | First reminder at a time of day. A time that already passed today means tomorrow. |
| `in <duration>` | First reminder after a delay. Use `at` or `in`, not both. |
| `every <duration>` | Repeat interval. Default: 10 minutes. |
| `until <time>` | Stop time: the first such time after the first reminder. Default: see [Configuration](#configuration). |
| `ok <word>` | How you confirm. See [Confirming](#confirming). |
| `then <action>` | A follow-up for the agent after you confirm. Takes everything after the first `then`. |

- **Times:** `8am`, `8:30am`, `4pm`, `16:00`. Read in your configured time zone, so daylight-saving days work.
- **Durations:** `10m`, `1h`, `90s`, `1h30m` (units `s m h`, also `sec min hr`). Minimum 1 minute.
- **Any order:** the clauses can come in any order after the text. They are read from the end and stop at the first thing that is not a clause, so `Call mom before 6pm at 4pm` has the text "Call mom before 6pm".
- **Short form:** `/snooze Wake up 8am 10m` means `at 8am every 10m`. A lone trailing time (`/snooze Wake up 8am`) means `at 8am`. A time right after `before`, `by`, `after`, `around`, `since`, `from` or `to` is part of the text.
- **Keywords** (`at in every until ok then`) are case-insensitive.

Examples:

```
/snooze Wake up at 8am every 10m
/snooze Wake up 8am 10m
/snooze Take my pill in 30m every 5m ok taken
/snooze Send the report at 4pm ok contains:sent
/snooze Stretch in 1h every 20m ok 💪
```

## Commands

| Command | What it does |
|---|---|
| `/snooze <what> ...` | Create a snooze (see above). |
| `/snooze list` | Snoozes that are running or waiting for `yes`. |
| `/snooze ok [id]` | Confirm from this client. Without an id: the running snooze that sent last. |
| `/snooze later [N] [id]` | Push back by `N` minutes (`30`, `30m`, `1h`) from now. Before the first reminder, push from that reminder's time instead. Default: the snooze interval. |
| `/snooze cancel <id>` | Stop without confirming. |
| `/snooze yes [id]` | Start a snooze that is waiting for validation. Without an id: the newest waiting one. |
| `/snooze test` | Not available yet. |

If the first word is a subcommand name but the rest does not fit it, the text is read as a new reminder when it parses as one: `/snooze cancel the order at 4pm` creates a reminder; otherwise you get the usage line.

## Confirming

The `ok` clause sets what stops the reminders:

| You write | You stop it by sending |
|---|---|
| (nothing) or `ok` | `ok` |
| `ok <word>` | that word |
| `ok <emoji>` | that emoji, as a message or as a reaction |
| `ok contains:<word>` | any message containing the word |
| `ok any` | any message |

How replies are read:

- **Only you can answer.** The chat and user must be the ones the reminder went to. Reactions carry no user id, so they are accepted in private chats only, only for an emoji rule, and only on the latest reminder message.
- **Matching is forgiving.** Case, spaces, punctuation, emoji variation selectors, skin tones and check-mark variants are ignored: `OK!`, `ok 👍🏽` and `✔️` match as you expect.
- **`later` always wins.** `later`, `later 30`, `later 1h` and `not now` push the reminder back and are never taken as a confirm, even with `ok any`. Without a number the delay is the snooze interval.
- **More text is not lost.** For the `ok`, word and emoji rules, `ok, and book a table for 8` confirms the snooze and also passes the message on to the agent. For `any` and `contains` the whole message is the answer and is consumed.
- **Which snooze.** A reply to a reminder message picks that snooze. Otherwise it is the running snooze that sent last.
- **Slash commands are never answers.** A message starting with `/` goes to Hermes as usual, whatever the `ok` rule.
- **`yes`** (the whole message) starts a snooze that is waiting for validation.

## Agent tool

The agent can create snoozes with the `snooze_create` tool: `text` and `first_at` (ISO 8601, in your local time) are required; `every`, `until`, `confirm` and `then` are optional and work like the command clauses. The tool returns the same echo as the command, which the agent shows to you as it is. Parameter details are in the tool schema (`TOOL_SCHEMA` in `commands.py`), which is what the agent reads.

## Configuration

Set these under `plugins.entries.snooze.settings` in `~/.hermes/config.yaml`:

```yaml
plugins:
  entries:
    snooze:
      settings:
        validate_setup: true
        cap_minutes: 120
        cap_reminders: 10
        timezone: Europe/Paris
```

| Key | Default | Meaning |
|---|---|---|
| `validate_setup` | `true` | A new snooze waits for your `yes` (10 minutes) before it starts. `false` starts it at once. |
| `cap_minutes` | `120` | Without `until`, a snooze stops this many minutes after its first reminder. |
| `cap_reminders` | `10` | A snooze never sends more than this many reminders. |
| `timezone` | `system` | IANA zone name such as `Europe/Paris`, or `system` for the machine's zone. An unknown name falls back to `system`. |

## Snooze life cycle

| State | Meaning |
|---|---|
| `pending` | Created, waiting for `yes`. If no `yes` comes within 10 minutes it is never started (`missed`). |
| `active` | Reminders are being sent. |
| `done` | You confirmed. |
| `missed` | Not started in time, or the cap was reached without a confirm. |
| `cancelled` | Stopped with `/snooze cancel`. |

Snoozes are stored per profile in the plugin data directory:

| File | Content |
|---|---|
| `snoozes.json` | The records, written atomically. Timestamps are UTC ISO 8601. |
| `snoozes.lock` | Held during every read-modify-write. |
| `sender.lock` | Held by the one process that sends reminders, so several Hermes processes never send twice. |

## Limits

- A Hermes process must be running to send reminders. Ideally run the gateway on an always-on machine; a sleeping laptop sends nothing.
- Do Not Disturb on your phone is your job: allow the bot's notifications, and do not mute the chat.
- Voice-note confirms are not supported.
- WhatsApp Cloud API is not supported (it cannot send 24 hours after your last message).

## Development

```bash
python3 -m unittest discover -s tests -t .   # tests
ruff check .                                 # lint (rules in ruff.toml)
ruff format .                                # format
```

Code layout and conventions: [AGENTS.md](AGENTS.md).
