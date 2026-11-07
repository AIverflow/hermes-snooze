# snooze

A reminder for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that **repeats on your phone until you confirm it**, then can hand a follow-up to the agent. Reminders are sent by the plugin itself: zero LLM calls per reminder.

```
/snooze Send email to John Doe at 4pm every 10m until 6pm then close ticket 42
```

## Status

New (v0.1.0). Everything below is built: command, agent tool, timer, reply hooks, `then`, `/snooze test`. It has had little real-world use so far: expect rough edges. Not supported: see [Limits](#limits).

## Screenshots

None yet.

## Install and first steps

Requires Hermes Agent 0.22.7 or later and Python 3.10+.

1. Install and enable the plugin:

   ```bash
   hermes plugins install AIverflow/hermes-snooze
   hermes plugins enable snooze
   ```

   Or clone this repo into `~/.hermes/plugins/snooze` and run the second command.

2. Restart the gateway (`hermes gateway restart`, or `hermes gateway start` if it is not running). The timer and the reply hooks live in the gateway, so reminders only go out while it runs.
3. Check the setup: send `/snooze test` in the chat you will use. Within 15 seconds you get a test reminder; reply `ok` within 2 minutes and you get "Test passed". If no gateway runs for this profile, it says so and makes no test.
4. Send a first snooze from a chat (Telegram, Discord, ...). From the CLI you can create and manage snoozes, but reminders still need the gateway:

   ```
   /snooze Take my pill in 30m every 5m
   ```

5. The plugin answers with an echo: what, when (in your time zone), how to stop it, where it goes, how to undo it.
6. By default a new snooze waits for you to reply `yes` (within 10 minutes) before it starts. Reply `yes` or send `/snooze yes`.
7. When the reminders arrive, reply `ok` to stop them, or `later 30` to push them back 30 minutes.

A snooze made from the CLI (no chat) goes to the first home channel set in your profile.

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
| `then <action>` | A follow-up for the agent after you confirm. Takes everything after the first `then`. See [The then action](#the-then-action). |

- **Times:** `8am`, `8:30am`, `4pm`, `16:00`. Read in your configured time zone, so daylight-saving days work.
- **Durations:** `10m`, `1h`, `90s`, `1h30m`, or with spaces: `10 min`, `1 h 30 min` (units `s m h`, also `sec min hr`). Minimum 1 minute.
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
| `/snooze test` | Check the setup: sends one test reminder to this chat within 15 s; reply `ok` within 2 min and you get "Test passed". It tells you at once if no gateway runs for this profile (then no test is made). |

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

### The then action

When you confirm a snooze that has a `then`, the plugin first sends `✅ Done: <what>. Now: <action>…`, then the agent gets this message in the same chat: *The user confirmed the reminder '<what>'. Now do this: <action>. Then tell the user the result in one line.* The agent uses its own tools and answers in the chat.

- **Text reply** (`ok`, your word, `ok, and book a table`): your own message becomes that agent turn, so it needs no setup. Extra text you wrote is added after it as "The user also wrote: ...".
- **Reaction or `/snooze ok`** (no message to reuse): the plugin asks Hermes to start the turn. Hermes only allows that if you set `allow_gateway_injection` directly under the plugin entry (not under `settings`) in `~/.hermes/config.yaml`, then restart the gateway:

  ```yaml
  plugins:
    entries:
      snooze:
        allow_gateway_injection: true
  ```

- **From the CLI or Desktop:** `/snooze ok` still confirms the snooze, but the reply says the then-action was not run. It runs only when you confirm from the chat.
- **If it fails** (injection not allowed, no gateway, Hermes refuses), the chat gets `⚠️ then-action failed: <reason>`. The snooze stays `done`.
- The agent's own rules apply: a risky tool asks for approval in the chat as usual.

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

## What it runs and reads

| What | Detail |
|---|---|
| Timer | One task in the gateway, every 15 seconds, started when the gateway loads the plugin (the first inbound message is only a fallback). It sends due reminders through Hermes's own send path (no LLM call). |
| Hooks | `pre_gateway_dispatch` and `gateway_platform_event` see every inbound gateway message and Telegram reaction before the agent. They only act on replies to a running snooze; all other messages pass untouched. |
| Own messages | Reminders, `✅ Done`, `⌛ Missed` and `then` status lines, sent to the chat the snooze was created in. |
| Reads outside its data | The Hermes pause flag, whether a gateway is running, and the profile's home channel (for snoozes made from the CLI). Read-only. |
| Network, credentials, shell | None of its own. No telemetry. |

## Limits

- **The gateway must run.** The timer runs only in the gateway of the profile (`hermes gateway start`), and the gateway is also what reads your replies. A CLI or Desktop session alone sends nothing. Run it on an always-on machine; a sleeping laptop sends nothing, and after wake-up one late reminder follows ("late by N min"). If no gateway runs, a new snooze's echo warns you, and `/snooze test` tells you.
- **The timer starts with the gateway.** It starts when the gateway loads the plugin, with no message needed. If the gateway loop is not ready at that point, it starts with the first message that arrives in the profile instead, and a reminder due before that goes out late, with "late by N min".
- **`hermes pause` holds reminders.** While Hermes is paused, no reminder is sent; `/snooze list` shows "paused". After `hermes resume`, one late reminder follows per snooze, not a burst.
- Do Not Disturb on your phone is your job: allow the bot's notifications, and do not mute the chat. The plugin cannot get through a muted chat.
- Voice-note confirms are not supported: only text, emoji and reactions count.
- Reaction confirms work on Telegram private chats only (a reaction carries no user id, so groups are refused).
- WhatsApp Cloud API is not supported (it cannot send 24 hours after your last message).

## Development

```bash
python3 -m unittest discover -s tests -t .   # tests
ruff check .                                 # lint (rules in ruff.toml)
ruff format .                                # format
```

Code layout and conventions: [AGENTS.md](AGENTS.md).
