# Snooze user guide

For installation and your first snooze, start with the [README](../README.md#start). This guide covers commands, examples and optional settings.

## Create a snooze

Use `/snooze` followed by the reminder text, a start time or delay, and optional clauses:

```text
/snooze <what> (at <time> | in <duration>) [every <duration>] [until <time>] [daily | weekdays | on <days>] [ok <word> | ok <a>|<b>|…] [then <action>]
```

Choose one of `at` or `in`; parentheses and brackets are notation, not text to type. The reminder text can be in any language; clause keywords are English. For a request in plain language, ask Hermes to create the snooze instead of using `/snooze` syntax.

| Clause | Meaning |
|---|---|
| `at <time>` | First reminder at a time of day. If that time has passed, it means tomorrow. |
| `in <duration>` | First reminder after a delay. Use either `at` or `in`. |
| `every <duration>` | Interval between reminders. Defaults to 10 minutes. |
| `until <time>` | End time: the first such time after the first reminder. Without `until`, `cap_minutes` sets it. See [When a snooze stops](#when-a-snooze-stops). |
| `daily`, `weekdays`, `on <days>` | Start a new occurrence every day, Monday to Friday, or on listed days (`mon,thu` or a range such as `mon-fri`); for example, `/snooze Water plants at 8am on mon,thu`. |
| `ok <word>` | Require a word, emoji, contains rule, or any message. See [Confirmations](#confirmations). |
| `ok <a>\|<b>\|…` | Require one of 2–9 distinct one-word options; for example, `ok cook\|takeaway\|restaurant`. |
| `then <action>` | Ask the agent to do something after confirmation. See [Then-actions](#then-actions). |

Times use the configured time zone, in forms such as `8am` or `16:00`. Durations are digits plus a unit (seconds, minutes or hours), such as `10m`, `1h30m` or `2 hours`; for example, `/snooze Call mom in 1 hour 30 minutes`. Units may be joined or separated by spaces. The minimum is one minute. A recurrence needs a start time (`at <time>` or a trailing time); it cannot be combined with `in <duration>`.

Scheduling and confirmation clauses can appear in any order after the reminder text. Put `then` last: everything after it is the action for the agent. The parser reads the other clauses from the end; text before the clauses is the reminder. For example, `Call mom before 6pm at 4pm` keeps `Call mom before 6pm` as the text. A short form such as `/snooze Wake up 8am 10m` means `at 8am every 10m`; a trailing time alone means `at` that time. A time after `before`, `by`, `after`, `around`, `since`, `from` or `to` stays part of the text. Clause keywords are case-insensitive. Keep extra commentary outside the command: a trailing word such as `please` is refused with an error naming it, and no reminder is created. The same goes for a duration that is not digits plus a unit (`every ten minutes`). A second `at` is accepted: the last `at` is the start and the earlier one stays in the reminder text, so `Meeting at 3pm at 2:30pm` rings at 2:30pm with the text `Meeting at 3pm`. `at` and `in` together (`at 16:00 in 30m`) are refused.

The creation reply summarizes the snooze, its schedule and destination. Every reminder message includes its snooze ID so overlapping reminders can be identified.

### Examples

| Need | Command | What happens after your answer |
|---|---|---|
| Send a quote before the end of the day | `/snooze Send the quote at 16:00 every 15m until 18:00` | `Ok` ends the snooze. |
| A weekday routine | `/snooze Pack my bag at 20:00 weekdays every 10m` | `Ok` ends today's occurrence; the next scheduled weekday stays set. |
| Resume work with the agent when ready | `/snooze Resume this project in 30m ok ready then summarize where we left off and suggest the next step` | `ready` confirms and asks the agent to help you continue in this chat. |
| Choose dinner, then get suggestions | `/snooze Dinner? at 18:30 ok cook\|takeaway\|restaurant then suggest two dinner ideas based on my answer` | Reply with a number or option; the agent uses your choice for its suggestions. |

## Commands

| Command | Purpose |
|---|---|
| `/snooze <what> ...` | Create a snooze. |
| `/snooze list` | Show snoozes that are active or waiting for setup confirmation. |
| `/snooze ok [id]` | Confirm with a command instead of a reply. Without an ID, confirms the running snooze that sent a reminder most recently. |
| `/snooze later [N] [id]` | Delay by `N` minutes (`30`, `30m`, `1h`). Without a delay, uses the snooze's `every` interval. |
| `/snooze cancel <id>` | Stop without confirming. |
| `/snooze yes [id]` | Start a snooze waiting for setup confirmation. Without an ID, selects the newest one. |
| `/snooze test` | Send one test reminder and check confirmation. |

`later` is measured from now, except before the first reminder is sent, when it is measured from the first reminder's time. What it does to the end time is in [When a snooze stops](#when-a-snooze-stops). `/snooze test` sends within about 15 seconds; reply `Ok` within two minutes. It reports immediately if the gateway is not running for the profile. A bare `yes` works in a chat; from the CLI use `/snooze yes <id>`.

If a command name is followed by text that does not fit that command, Snooze treats the input as a new reminder when it parses as one. For example, `/snooze cancel the order at 4pm` creates a reminder.

## When a snooze stops

Each occurrence sends reminders every `every`, from the first one, until it is answered or missed. In the diagrams, `R` is a reminder, `ok` your answer and `X` the occurrence being missed.

```text
every 10m, first reminder at 9:00

                    9:00  9:10  9:20  9:30  9:40
you answer after R2  R1    R2   ok
until 9:40           R1    R2    R3    R4     X
cap_reminders 2      R1    R2     X
```

There are three ways out. Your answer ends the occurrence at once. Otherwise it is missed at the end time (`until`, or the first reminder plus `cap_minutes` without `until`), or one `every` after the last reminder allowed by `cap_reminders`, whichever comes first.

A `later` that lands before the end time only moves the next reminder; the end time stays:

```text
every 10m, until 10:00; you send "later 30" at 9:12

 9:00   9:10   9:12            9:42   9:52   10:00
  R1     R2   later 30 ------>  R3     R4      X
```

A `later` that reaches or passes the end time reopens the window: the new end time is the new reminder time plus `every` for each reminder still allowed, so all of them are sent and you still get one interval to answer the last. Reminders still allowed are `cap_reminders` minus reminders already sent, at least 1.

```text
every 10m, until 9:40, cap_reminders 5; you send "later 1h" at 9:25

 9:00   9:10   9:20   9:25             10:25  10:35  10:45
  R1     R2     R3   later 1h ------->  R4     R5     X
                                        end time 9:40 -> 10:45
                                        (10:25 + 2 left x 10m)
```

`cap_reminders` is the hard limit: `later` never buys more reminders than it. Once that many are sent, `later` is refused, nothing changes, and the reply tells you to answer instead.

On a recurring snooze, each occurrence has its own counters and end time, so these limits apply per occurrence:

```text
weekdays at 20:00, every 10m, cap_reminders 3

Mon   20:00  20:10  20:20  20:30
       R1     R2     R3     X      missed; the next occurrence is set
Tue   20:00  20:10
       R1     R2     ok            answered; Wednesday stays set
```

## Confirmations

By default, reply `Ok` to confirm. The `ok` clause can change the rule:

| Rule | Accepted confirmation |
|---|---|
| omitted or `ok` | `Ok` |
| `ok <word>` | That word |
| `ok <emoji>` | That emoji as a message or reaction |
| `ok contains:<word>` | Any message containing the word |
| `ok any` | Any message |
| `ok <a>\|<b>\|…` | One of 2–9 distinct, non-empty options, by number or option text; the answer is passed to the then-action |

Matching ignores case, extra spaces, punctuation, emoji skin tones and common check-mark variations. `OK!` matches the default rule, and `👍🏽` matches `ok 👍`; `✔️` matches `ok ✅`.

`later`, `later 30`, `later 1h` and `not now` always postpone the snooze, even with `ok any`. For the default, word, emoji and choice rules, extra text can both confirm and continue to the agent: `ok, and book a table for 8`. For `any` and `contains`, the whole message is used as the answer and consumed.

For a choice, every reminder shows numbered options; reply with the number or option text. The selected option word is passed to the then-action. `/snooze ok <id>` cannot answer a choice; reply in the chat instead. On a recurring snooze, `later` delays only today's occurrence; it does not move the next scheduled day.

A reply to a snooze's latest reminder message selects that snooze. Any other message, including a reply to an older message, selects the active snooze that sent a reminder most recently. A reaction counts only on the latest reminder message. Slash commands continue to Hermes and are not treated as answers. A whole-message `yes` starts the newest snooze waiting for setup confirmation.

Only replies from the chat the reminder was sent to count, and from its user when the snooze has one. For a home channel with no known user, any user Hermes authorizes in that chat can answer. Reactions do not carry a user ID, so reaction confirmation is accepted only for emoji rules in private chats and only on the latest reminder message.

## Create from scripts

Use the Hermes CLI to create a snooze without an agent call, then check its state later. The text uses the same grammar as `/snooze` and is sent to the profile's notification target:

```bash
id=$(hermes snooze add "Backup failed in 1m")
hermes snooze status "$id"
```

Add `--wait` to block until the occurrence ends and print its answer on stdout. The summary and wait notice go to stderr, so command substitution captures only the answer:

```bash
if answer=$(hermes snooze add "Backup failed in 1m ok retry|skip" --wait --timeout 600); then
    case "$answer" in
        retry) ./backup.sh ;;
        skip) echo "Backup skipped." ;;
    esac
else
    echo "No decision received; backup was not restarted." >&2
fi
```

Here `./backup.sh` is your existing backup script. Snooze returns `retry` or `skip`; the script restarts the backup only after a successful `retry` answer. A single-word confirmation such as `ok done` confirms the reminder but returns an empty answer. To use a named profile, add `-p <name>` after `hermes` in each command.

Use `--timeout SECONDS` with `--wait` to stop waiting after that many seconds; `0` (the default) waits forever. A timeout leaves the snooze running, so check it later with `status`.

Quote the snooze text because `|` is a shell pipe. On success, `add` prints the ID on stdout and the creation summary on stderr, then exits 0. If the gateway is not running for the profile, the summary warns that the reminder will not be sent. Parse, target and store errors go to stderr and exit 2. `status` prints the state and, when present, the answer; it exits 0 for a known ID and 2 for an invalid or unknown ID.

With `--wait`, exit codes are `0` for answered, `2` for a parse/store error or recurring snooze, `3` for missed, `4` for cancelled, `5` for timeout, and `130` if interrupted with Ctrl-C. An answered snooze prints its answer on stdout (an empty line when there is no answer).

## Then-actions

When a snooze with `then` is confirmed, Snooze reports that it is handing the action to the agent, then asks Hermes to continue in the same chat. A text confirmation can be reused as the agent's message; any extra text is included. A reaction or `/snooze ok` has no text to reuse, so Hermes must allow gateway injection.

To enable that case, set `allow_gateway_injection: true` directly under the plugin entry (not under `settings`) in the active profile's `config.yaml`, then restart the gateway:

```yaml
plugins:
  entries:
    snooze:
      allow_gateway_injection: true
```

From CLI or Desktop, `/snooze ok` confirms the snooze but does not run its then-action. If Hermes refuses the then-action, the snooze remains done and the chat displays the failure. The agent's normal tool approval rules still apply.

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
| `validate_setup` | `false` | When enabled, a new snooze waits up to 10 minutes for `yes` before starting. |
| `cap_minutes` | `120` | Maximum time a snooze keeps sending reminders after its first one, unless `until` is set. |
| `cap_reminders` | `10` | Maximum reminders sent per occurrence, including when `until` is set. |
| `timezone` | `system` | IANA time zone (for example, `Europe/Paris`) or `system` for the machine's time zone. |
| `notification_platform` | `source` | `source` sends to the originating chat; `telegram` or `discord` sends to that platform's Hermes home channel. |

Hermes owns the home channel setting. Configure the bot and select its destination using Hermes' [Telegram setup](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram) or [Discord setup](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/discord). Then set `notification_platform` to the desired platform. This is one destination per profile; reminders created elsewhere, including from the CLI, go to that home channel. Restart the gateway after changing settings.

## Agent tool

The agent can create snoozes with `snooze_create`. It requires `text` and `first_at` (ISO 8601 in local time); `every`, `until`, `recurrence`, `confirm`, and `then` are optional. `recurrence` accepts `daily`, `weekdays`, or day lists and ranges. `confirm` accepts `ok`, a word, emoji, `any`, `contains:<word>`, or a `|`-separated choice. Its argument details live in the tool schema in `commands.py`; its response uses the same summary shown for a chat command.

## Lifecycle and stored data

| State | Meaning |
|---|---|
| `pending` | Waiting for `yes` when setup validation is enabled. If not confirmed within 10 minutes, it becomes `missed`. |
| `active` | Reminders are being sent. |
| `done` | Confirmed. |
| `missed` | Setup was not confirmed in time, or the end time or a cap was reached without an answer. |
| `cancelled` | Stopped with `/snooze cancel`. |

A recurring snooze returns to `active` after each answer or miss. Its stored `answer` is the latest choice word or free-text answer, `days` stores the recurrence days, and `span` stores the daily reminder window; only `/snooze cancel <id>` ends the series.

Snoozes are stored per profile in the plugin data directory. `snoozes.json` contains records with UTC ISO 8601 timestamps and is updated atomically. `snoozes.lock` protects changes to the data file; `sender.lock` ensures only one process sends reminders at a time.

## Troubleshooting and limits

- **No reminders arrive:** keep the profile's gateway running on an awake machine. A sleeping computer cannot send reminders. After a delay, Snooze sends one late reminder rather than a burst.
- **Gateway is paused:** `hermes pause` holds reminders. `/snooze list` shows that state; after `hermes resume`, each snooze that has not reached its end time gets one late reminder.
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
