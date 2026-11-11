# Snooze user guide

For installation and your first snooze, start with the [README](../README.md#start). This guide covers commands, examples and optional settings.

## Create a snooze

Use `/snooze` followed by the reminder text, a start time or delay, and optional clauses:

```text
/snooze <what> (at <time> | in <duration>) [every <duration>] [until <time>] [daily | everyday | weekdays | every <days> | on <day>] [via <platform>] [ok <word> | ok <emoji> | ok <a>|<b>|…] [then <action>]
```

Choose one of `at` or `in`; parentheses and brackets are notation, not text to type. The reminder text can be in any language; clause keywords are English. For a request in plain language, ask Hermes to create the snooze instead of using `/snooze` syntax.

| Clause | Meaning |
|---|---|
| `at <time>` | First reminder at a time of day. If that time has passed, it means tomorrow. |
| `in <duration>` | First reminder after a delay. |
| `every <duration>` | Interval between reminders. Defaults to 10 minutes. |
| `until <time>` | End time: the first such time after the first reminder. Without `until`, `cap_minutes` sets it. See [When a snooze stops](#when-a-snooze-stops). |
| `daily` / `everyday` | Start a new occurrence every day (the two words are the same). |
| `weekdays` | Start a new occurrence Monday to Friday. |
| `every <days>` | Start a new occurrence each week on listed days (`monday,thursday`, or a range such as `monday-friday`; `mon,thu` and `mon-fri` work too); for example, `/snooze Water plants at 8am every monday,thursday`. It can be combined with `every <duration>`: `every monday every 10m`. |
| `on <day>` | One-time snooze on the next such day at the `at` time (today if that time is still ahead); one day only, and `on monday,thursday` is refused (use `every`); for example, `/snooze Call the dentist at 9am on monday`. |
| `via <platform>` | Send this snooze to that platform's home channel (`telegram` or `discord`) instead of the current chat; for example, `/snooze Water plants at 8am daily via discord`. Refused, with nothing set, if the platform has no home channel: run `/sethome` in the wanted chat there. Any other word after `via` stays in the text (`Go to Lyon via Dijon`). |
| `ok <word>`, `ok <emoji>` | Require that word or emoji as the whole reply. See [Confirmations](#confirmations). |
| `ok <a>\|<b>\|…` | Require one of 2–9 distinct one-word options; for example, `ok cook\|takeaway\|restaurant`. |
| `then <action>` | Ask the agent to do something after confirmation. See [Then-actions](#then-actions). |

Times use the configured time zone, in forms such as `8am` or `16:00`. Durations are digits plus a unit (seconds, minutes or hours), such as `10m`, `1h30m` or `2 hours`; for example, `/snooze Call mom in 1 hour 30 minutes`. Units may be joined or separated by spaces. The minimum is one minute. A recurrence or an `on <day>` needs a start time (`at <time>` or a trailing time); it cannot be combined with `in <duration>`.

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
| `/snooze platform [telegram\|discord\|source]` | Show or set the default platform ([Setup and configuration](#setup-and-configuration)). Also `hermes snooze platform [name]`. |

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

By default, reply `Ok` to confirm. The `ok` clause can change the rule. The whole message must be the answer, nothing more:

| Rule | Valid | Not valid |
|---|---|---|
| omitted or `ok` | `ok`, `Ok!` | `ok thanks`, `okkkk` |
| `ok <word>` | that word (`done`) | `done, deploying now` |
| `ok <emoji>` | that emoji, as a message or a reaction | `great 👍` |
| `ok <a>\|<b>\|…` (2–9 distinct, non-empty options) | an option or its number (`a`, `2`); the answer is passed to the then-action | `a please`, `1 then stop` |
| `yes` (a snooze waiting for setup confirmation) | `yes` | |

Words to reply in the chat:

| Reply | Effect |
|---|---|
| `Ok` (or your `ok` rule) | Confirm. |
| `later`, `later N`, `later Nm`, `later N min`, `later 1h` | Postpone by `N` minutes; without `N`, by the `every` interval. `later please` is not valid. |
| `not now [N]` | Same as `later`. |
| `yes` | Start a snooze waiting for setup confirmation. |

Matching ignores case, extra spaces, punctuation, emoji skin tones and common check-mark variations. `OK!` matches the default rule, and `👍🏽` matches `ok 👍`; `✔️` matches `ok ✅`.

Snooze never passes an unclear reply to the agent. For a running snooze in its chat, from a user allowed to answer:

1. A valid answer always counts, whether or not it replies to a reminder.
2. A message that replies to the snooze's latest reminder is always Snooze's: if it is not valid, it gets the list below.
3. The first message after a reminder is taken as the answer to it: if it is not valid, Snooze sends the list and the message does not reach the agent. Each new reminder arms this again.
4. Any other message goes to the agent as usual.

The list names the valid answers and the commands that always work:

```text
"okkkk" is not a valid answer for #31 (drink water).
Reply: Ok · later · later 15     Or type: /snooze ok 31 · /snooze cancel 31
```

For a choice it lists the options and their numbers (`merge · review · 1 · 2`) and offers only `/snooze cancel 31`. On Discord, it says `Reply (to a reminder):`. On Discord, answer in a DM with the bot as you would on Telegram. In a server channel, even with only you and the bot, Hermes delivers only messages that mention or reply to the bot: reply to a reminder, or add the channel to `DISCORD_FREE_RESPONSE_CHANNELS` to answer without replying.

For a choice, every reminder shows numbered options; reply with the number or option text. The selected option word is passed to the then-action. `/snooze ok <id>` cannot answer a choice; reply in the chat instead. On a recurring snooze, `later` delays only today's occurrence; it does not move the next scheduled day. A `later` that goes past the next scheduled time replaces that next occurrence: `later 25h` on a daily 08:00 snooze means no reminder at 08:00 tomorrow.

A reply to a snooze's latest reminder message selects that snooze. Any other message, including a reply to an older message, selects the active snooze that sent a reminder most recently. Slash commands continue to Hermes and are not treated as answers. A whole-message `yes` starts the newest snooze waiting for setup confirmation.

Only replies from the chat the reminder was sent to count, and from its user when the snooze has one. For a home channel with no known user, any user Hermes authorizes in that chat can answer. Reactions do not carry a user ID, so reaction confirmation is accepted only for emoji rules in private chats and only on the latest reminder message.

## Create from scripts

Use the Hermes CLI to create a snooze without an agent call, then check its state later. The text uses the same grammar as `/snooze` and is sent to the profile's home channel (see [Setup and configuration](#setup-and-configuration)):

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

With `--wait`, exit codes are `0` for answered, `2` for an error or a recurring snooze, `3` for missed, `4` for cancelled, `5` for timeout, and `130` if interrupted with Ctrl-C.

## Then-actions

When a snooze with `then` is confirmed, Snooze reports that it is handing the action to the agent, then asks Hermes to continue in the same chat. A text confirmation is reused as the agent's message. A reaction or `/snooze ok` has no text to reuse, so Hermes must allow gateway injection.

To enable that case, set `allow_gateway_injection: true` directly under the plugin entry (not under `settings`) in the active profile's `config.yaml`, then restart the gateway:

```yaml
plugins:
  entries:
    snooze:
      allow_gateway_injection: true
```

From CLI or Desktop, `/snooze ok` confirms the snooze but does not run its then-action. If Hermes refuses the then-action, the snooze remains done and the chat displays the failure. The agent's normal tool approval rules still apply.

## Setup and configuration

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
| `notification_platform` | `source` | The default platform: `source` (the table below), `telegram` or `discord`. |

Hermes owns the home channel setting. Configure the bot and select its destination using Hermes' [Telegram setup](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram) or [Discord setup](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/discord) (`/sethome` in the wanted chat). Change `notification_platform` with `/snooze platform telegram|discord|source` or `hermes snooze platform ...`; it refuses a platform with no home channel. A change applies at once, with no restart.

Where a new snooze goes, first match wins:

| # | Case | Destination |
|---|---|---|
| 1 | The text has `via <platform>` | That platform's home channel. |
| 2 | `notification_platform` is `telegram` or `discord` | That platform's home channel. |
| 3 | A chat command, or the agent tool in a chat | That chat. |
| 4 | The CLI or no chat | The only home channel. If several platforms have one, Snooze refuses, nothing is set, and the reply shows how to pick one: `/snooze platform telegram` (or `hermes snooze platform telegram` in a terminal), or add `via <platform>`. |

When several platforms have a home channel, `/snooze` alone adds a line saying where a reminder typed from here goes (or that there is no default yet), and `/snooze list` names each snooze's platform.

## Agent tool

The agent can create snoozes with `snooze_create`. It requires `text` and `first_at` (ISO 8601 in local time); `every`, `until`, `recurrence`, `platform` (like `via`), `confirm`, and `then` are optional. `recurrence` accepts `daily` (or `everyday`), `weekdays`, or day lists and ranges. `confirm` accepts `ok`, a word, an emoji, or a `|`-separated choice. Its argument details live in the tool schema in `commands.py`; its response uses the same summary shown for a chat command.

## Lifecycle and stored data

| State | Meaning |
|---|---|
| `pending` | Waiting for `yes` when setup validation is enabled. If not confirmed within 10 minutes, it becomes `missed`. |
| `active` | Reminders are being sent. |
| `done` | Confirmed. |
| `missed` | Setup was not confirmed in time, or the end time or a cap was reached without an answer. |
| `cancelled` | Stopped with `/snooze cancel`. |

A recurring snooze returns to `active` after each answer or miss. Its stored `answer` is the latest choice word or free-text answer, `days` stores the recurrence days, and `span` stores the length of each occurrence's reminder window; only `/snooze cancel <id>` ends the series.

Snoozes are stored per profile in the plugin data directory. `snoozes.json` holds records with UTC ISO 8601 timestamps and is updated atomically. `snoozes.lock` protects changes to the data file; `sender.lock` ensures only one process sends reminders at a time.

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
