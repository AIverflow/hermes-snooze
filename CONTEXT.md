# Snooze

A reminder that repeats on the user's phone until they answer, then can hand a follow-up to the agent.

## Language

**Snooze**:
One reminder record: its text, schedule, confirm rule and optional then-action, identified by a number.
_Avoid_: task, alarm, job

**Reminder**:
One message sent for a snooze; a snooze sends reminders until it is answered, or a cap or its end time is reached.
_Avoid_: notification, ping

**Occurrence**:
One run of reminders for a snooze, from its first reminder to its answer or miss. A one-time snooze has one occurrence; a recurring snooze has one per scheduled day.
_Avoid_: instance, cycle

**Recurrence**:
The days on which a recurring snooze starts a new occurrence (`daily`, `weekdays`, or `every <days>`). It is separate from the repeat interval between reminders within one occurrence (`every <duration>`).
_Avoid_: repeat (that means the interval between reminders)

**Confirm rule**:
What the user must reply to answer an occurrence: `Ok`, a word, an emoji, a substring, anything, or one of several choices.
_Avoid_: validation, ack

**Choice**:
A confirm rule listing several accepted options; the user replies with an option or its number.
_Avoid_: poll, vote, menu

**Answer**:
The option or text that confirmed an occurrence, kept on the snooze and given to the then-action and the external creator.
_Avoid_: response, result

**Missed**:
An occurrence that reached its cap or end time without an answer. A one-time snooze ends there; a recurring snooze waits for its next occurrence.
_Avoid_: expired, failed

**Then-action**:
The follow-up the agent is asked to do once an occurrence is answered.
_Avoid_: callback, hook

**External creator**:
A program outside Hermes (script, cron job, home automation) that creates a snooze from the shell and may wait for its answer.
_Avoid_: client, integration, trigger
