import dataclasses
import unittest

from snooze import confirm
from snooze.confirm import Decision, Sender, match
from snooze.parse import ConfirmRule, parse_ok_arg
from snooze.store import Schedule, Snooze, Target


def rule_of(text):
    """The stored confirm rule for the word after `ok` in /snooze (a plain "ok" is the default rule)."""
    rule = ConfirmRule() if text == "ok" else parse_ok_arg(text)
    return {"kind": rule.kind, "value": rule.value}


def snooze(
    id=1,
    confirm_rule="ok",
    state="active",
    chat="100",
    user="42",
    platform="telegram",
    last_sent_at="2026-11-07T16:00:00",
    last_message_id="m1",
    every=10,
):
    """A stored snooze. `confirm_rule` is what the user typed after `ok`, or a ready dict."""
    return Snooze(
        id=id,
        text="x",
        created="2026-11-07T15:00:00",
        target=Target(platform=platform, chat_id=chat, user_id=user),
        schedule=Schedule(first_at="2026-11-07T16:00:00", every=every),
        confirm=confirm_rule if isinstance(confirm_rule, dict) else rule_of(confirm_rule),
        state=state,
        last_sent_at=last_sent_at,
        last_message_id=last_message_id,
    )


ME = Sender(platform="telegram", chat_id="100", user_id="42")


def run(text, snoozes=None, sender=ME, reply_to=None, **kw):
    return match(text, snoozes if snoozes is not None else [snooze()], sender, reply_to, **kw)


class DefaultOk(unittest.TestCase):
    def test_plain_ok(self):
        d = run("ok")
        self.assertEqual(d, Decision("confirm", 1, None, False))

    def test_case_spaces_punctuation(self):
        for text in ["OK", "Ok", " ok ", "ok!", "OK!!!", "ok.", "¡ok!", "(ok)"]:
            with self.subTest(text=text):
                self.assertEqual(run(text).action, "confirm")
                self.assertFalse(run(text).pass_through)

    def test_not_ok(self):
        for text in ["okay", "nok", "not ok", "hello", "", "   ", "!!!"]:
            with self.subTest(text=text):
                self.assertEqual(run(text).action, "none")

    def test_ok_with_emoji_is_still_pure(self):
        # #15: "ok 👍🏽" is a pure confirm (emoji is not extra text)
        for text in ["ok 👍🏽", "ok👍", "OK! 👍"]:
            with self.subTest(text=text):
                d = run(text)
                self.assertEqual(d.action, "confirm")
                self.assertFalse(d.pass_through)

    def test_no_snoozes(self):
        self.assertEqual(run("ok", snoozes=[]).action, "none")


class WordRule(unittest.TestCase):
    def test_word_replaces_ok(self):
        s = [snooze(confirm_rule="taken")]
        self.assertEqual(run("taken", s).action, "confirm")
        self.assertEqual(run("Taken!", s).action, "confirm")
        self.assertEqual(run("ok", s).action, "none")
        self.assertEqual(run("I have taken it", s).action, "none")

    def test_word_dict_rule(self):
        s = [snooze(confirm_rule={"kind": "word", "value": "taken"})]
        self.assertEqual(run("taken", s).action, "confirm")

    def test_multi_word(self):
        s = [snooze(confirm_rule="all done")]
        self.assertEqual(run("All done!", s).action, "confirm")
        self.assertEqual(run("done", s).action, "none")

    def test_word_with_more_text_passes_through(self):
        s = [snooze(confirm_rule="taken")]
        d = run("taken, and order more", s)
        self.assertEqual((d.action, d.pass_through), ("confirm", True))


class ContainsRule(unittest.TestCase):
    def test_contains(self):
        s = [snooze(confirm_rule="contains:sent")]
        d = run("ok I sent it", s)
        self.assertEqual((d.action, d.pass_through), ("confirm", False))
        self.assertEqual(run("SENT", s).action, "confirm")
        self.assertEqual(run("I did it", s).action, "none")
        self.assertEqual(run("consent", s).action, "none")  # whole word only

    def test_contains_dict(self):
        s = [snooze(confirm_rule={"kind": "contains", "value": "sent"})]
        self.assertEqual(run("it was sent yesterday", s).action, "confirm")


class AnyRule(unittest.TestCase):
    def test_any_message(self):
        s = [snooze(confirm_rule="any")]
        for text in ["hello", "what time is it?", "👍", "x"]:
            with self.subTest(text=text):
                d = run(text, s)
                self.assertEqual((d.action, d.pass_through), ("confirm", False))

    def test_any_empty_message(self):
        self.assertEqual(run("   ", [snooze(confirm_rule="any")]).action, "none")


class EmojiRule(unittest.TestCase):
    def test_emoji_message(self):
        s = [snooze(confirm_rule="👍")]
        self.assertEqual(run("👍", s), Decision("confirm", 1, None, False))

    def test_skin_tones_and_variants(self):
        # #15
        s = [snooze(confirm_rule="👍")]
        for text in ["👍🏽", "👍🏿", "👍🏻", "👍️", "👍👍"]:
            with self.subTest(text=text):
                self.assertEqual(run(text, s).action, "confirm")

    def test_rule_with_skin_tone_matches_plain(self):
        s = [snooze(confirm_rule="👍🏽")]
        self.assertEqual(run("👍", s).action, "confirm")

    def test_check_mark_variants(self):
        # #15: ✔️ vs ✅
        for rule in ["✅", "✔️", "✔", "☑️"]:
            s = [snooze(confirm_rule=rule)]
            for text in ["✅", "✔️", "✔", "☑️"]:
                with self.subTest(rule=rule, text=text):
                    self.assertEqual(run(text, s).action, "confirm")

    def test_other_emoji_does_not_match(self):
        s = [snooze(confirm_rule="👍")]
        self.assertEqual(run("👎", s).action, "none")
        self.assertEqual(run("ok", s).action, "none")

    def test_emoji_with_text_passes_through(self):
        s = [snooze(confirm_rule="👍")]
        d = run("👍 and book a table", s)
        self.assertEqual((d.action, d.pass_through), ("confirm", True))

    def test_emoji_dict_rule(self):
        s = [snooze(confirm_rule={"kind": "emoji", "value": "👍"})]
        self.assertEqual(run("👍🏽", s).action, "confirm")


class Later(unittest.TestCase):
    def test_later_default_is_interval(self):
        d = run("later", [snooze(every=15)])
        self.assertEqual(d, Decision("later", 1, 15, False))

    def test_later_default_without_interval(self):
        d = run("later", [snooze(every=None)])
        self.assertEqual(d.later_minutes, confirm.DEFAULT_LATER_MINUTES)

    def test_later_minutes(self):
        for text, minutes in [
            ("later 30", 30),
            ("Later 30!", 30),
            ("later 30m", 30),
            ("later 30 min", 30),
            ("later 1h", 60),
            ("later 2 hours", 120),
            ("not now 20", 20),
        ]:
            with self.subTest(text=text):
                d = run(text)
                self.assertEqual((d.action, d.later_minutes, d.pass_through), ("later", minutes, False))

    def test_not_now(self):
        self.assertEqual(run("not now").action, "later")
        self.assertEqual(run("Not now!").action, "later")

    def test_later_zero_uses_default(self):
        self.assertEqual(run("later 0", [snooze(every=7)]).later_minutes, 7)

    def test_later_wins_over_any(self):
        # #17: confirm = any message, "not now" must not count as done
        s = [snooze(confirm_rule="any")]
        for text in ["later", "later 30", "not now"]:
            with self.subTest(text=text):
                self.assertEqual(run(text, s).action, "later")

    def test_later_wins_over_word_and_contains(self):
        self.assertEqual(run("later", [snooze(confirm_rule="later")]).action, "later")
        self.assertEqual(run("not now", [snooze(confirm_rule="contains:now")]).action, "later")

    def test_later_with_trailing_text_is_consumed(self):
        d = run("later, I'm busy")
        self.assertEqual((d.action, d.pass_through), ("later", False))

    def test_later_in_middle_is_not_later(self):
        self.assertEqual(run("I will do it later").action, "none")

    def test_later_ignored_for_pending(self):
        self.assertEqual(run("later", [snooze(state="pending")]).action, "none")


class PassThrough(unittest.TestCase):
    def test_confirm_with_more_text(self):
        # #18
        d = run("ok, and book a table for 8")
        self.assertEqual(d, Decision("confirm", 1, None, True))

    def test_pure_confirm_is_dropped(self):
        self.assertFalse(run("ok").pass_through)

    def test_unrelated_message_passes(self):
        d = run("what's the weather?")
        self.assertEqual((d.action, d.pass_through), ("none", True))


class WhichSnooze(unittest.TestCase):
    def three(self):
        return [
            snooze(id=1, last_sent_at="2026-11-07T16:00:00", last_message_id="m1"),
            snooze(id=2, last_sent_at="2026-11-07T16:20:00", last_message_id="m2"),
            snooze(id=3, last_sent_at="2026-11-07T16:10:00", last_message_id="m3"),
        ]

    def test_last_sent_wins(self):
        # #19
        self.assertEqual(run("ok", self.three()).snooze_id, 2)

    def test_last_sent_wins_whatever_the_order(self):
        self.assertEqual(run("ok", list(reversed(self.three()))).snooze_id, 2)

    def test_reply_to_picks_that_one(self):
        # #19
        self.assertEqual(run("ok", self.three(), reply_to="m1").snooze_id, 1)
        self.assertEqual(run("ok", self.three(), reply_to="m3").snooze_id, 3)

    def test_reply_to_id_types(self):
        s = [snooze(id=1, last_message_id=55), snooze(id=2, last_message_id=56, last_sent_at="2026-11-07T15:00:00")]
        self.assertEqual(run("ok", s, reply_to="56").snooze_id, 2)
        self.assertEqual(run("ok", s, reply_to=56).snooze_id, 2)

    def test_reply_to_other_message_falls_back_to_last_sent(self):
        self.assertEqual(run("ok", self.three(), reply_to="zzz").snooze_id, 2)

    def test_later_follows_same_rule(self):
        d = run("later 5", self.three(), reply_to="m1")
        self.assertEqual((d.snooze_id, d.later_minutes), (1, 5))

    def test_reply_to_uses_that_snooze_rule(self):
        s = [
            snooze(id=1, confirm_rule="taken", last_message_id="m1"),
            snooze(id=2, last_sent_at="2026-11-07T17:00:00", last_message_id="m2"),
        ]
        self.assertEqual(run("taken", s, reply_to="m1").snooze_id, 1)
        self.assertEqual(run("taken", s).action, "none")  # last sent is #2, rule ok

    def test_never_sent_is_not_confirmable(self):
        s = [snooze(id=1, last_sent_at=None, last_message_id=None)]
        self.assertEqual(run("ok", s).action, "none")

    def test_finished_states_ignored(self):
        for state in ["done", "missed", "cancelled"]:
            with self.subTest(state=state):
                self.assertEqual(run("ok", [snooze(state=state)]).action, "none")


class WhoCanAnswer(unittest.TestCase):
    def test_stranger_in_other_chat(self):
        # #20
        stranger = Sender(platform="telegram", chat_id="999", user_id="999")
        self.assertEqual(run("ok", sender=stranger).action, "none")

    def test_right_user_wrong_chat(self):
        # #20 / #23: thread or channel with another id
        other = Sender(platform="telegram", chat_id="101", user_id="42")
        self.assertEqual(run("ok", sender=other).action, "none")

    def test_right_chat_wrong_user(self):
        # #21: group chat, someone else replies
        group = [snooze(chat="-500")]
        other = Sender(platform="telegram", chat_id="-500", user_id="7")
        mine = Sender(platform="telegram", chat_id="-500", user_id="42")
        self.assertEqual(run("ok", group, sender=other).action, "none")
        self.assertEqual(run("ok", group, sender=mine).action, "confirm")

    def test_wrong_platform(self):
        other = Sender(platform="discord", chat_id="100", user_id="42")
        self.assertEqual(run("ok", sender=other).action, "none")

    def test_platform_case_insensitive_and_int_ids(self):
        sender = Sender(platform="Telegram", chat_id=100, user_id=42)
        self.assertEqual(run("ok", sender=sender).action, "confirm")

    def test_missing_user_id_never_matches(self):
        sender = Sender(platform="telegram", chat_id="100", user_id=None)
        self.assertEqual(run("ok", sender=sender).action, "none")
        self.assertEqual(run("ok", [snooze(user=None)], sender=sender).action, "none")

    def test_stranger_cannot_use_reply_to(self):
        stranger = Sender(platform="telegram", chat_id="999", user_id="999")
        self.assertEqual(run("later", sender=stranger, reply_to="m1").action, "none")

    def test_other_users_snooze_is_skipped(self):
        # two users, each with their own snooze: the sender only sees theirs
        s = [
            snooze(id=1, user="42", last_sent_at="2026-11-07T10:00:00"),
            snooze(id=2, user="7", chat="200", last_sent_at="2026-11-07T11:00:00"),
        ]
        self.assertEqual(run("ok", s).snooze_id, 1)


class Reaction(unittest.TestCase):
    DM = Sender(platform="telegram", chat_id="100", user_id=None, chat_type="private")

    def test_reaction_in_private_chat(self):
        s = [snooze(confirm_rule="👍")]
        d = run("👍", s, sender=self.DM, is_reaction=True)
        self.assertEqual(d, Decision("confirm", 1, None, False))

    def test_reaction_skin_tone_list(self):
        s = [snooze(confirm_rule="👍")]
        self.assertEqual(run(["👍🏽"], s, sender=self.DM, is_reaction=True).action, "confirm")

    def test_reaction_in_group_ignored(self):
        # #21
        s = [snooze(confirm_rule="👍", chat="-500")]
        group = Sender(platform="telegram", chat_id="-500", user_id=None, chat_type="group")
        self.assertEqual(run("👍", s, sender=group, is_reaction=True).action, "none")
        no_type = Sender(platform="telegram", chat_id="-500", user_id=None)
        self.assertEqual(run("👍", s, sender=no_type, is_reaction=True).action, "none")

    def test_reaction_wrong_chat(self):
        s = [snooze(confirm_rule="👍")]
        other = dataclasses.replace(self.DM, chat_id="555")
        self.assertEqual(run("👍", s, sender=other, is_reaction=True).action, "none")

    def test_reaction_wrong_user_when_known(self):
        s = [snooze(confirm_rule="👍")]
        other = dataclasses.replace(self.DM, user_id="7")
        self.assertEqual(run("👍", s, sender=other, is_reaction=True).action, "none")

    def test_reaction_needs_emoji_rule(self):
        for rule in ["ok", "any", "taken", "contains:sent"]:
            with self.subTest(rule=rule):
                s = [snooze(confirm_rule=rule)]
                self.assertEqual(run("👍", s, sender=self.DM, is_reaction=True).action, "none")

    def test_reaction_on_reminder_message(self):
        s = [
            snooze(id=1, confirm_rule="👍", last_message_id="m1"),
            snooze(id=2, confirm_rule="👍", last_message_id="m2", last_sent_at="2026-11-07T17:00:00"),
        ]
        d = run("👍", s, sender=self.DM, reply_to="m1", is_reaction=True)
        self.assertEqual(d.snooze_id, 1)

    def test_wrong_emoji_reaction(self):
        s = [snooze(confirm_rule="👍")]
        self.assertEqual(run("👎", s, sender=self.DM, is_reaction=True).action, "none")


class Yes(unittest.TestCase):
    def test_yes_validates_pending(self):
        s = [snooze(state="pending", last_sent_at=None, last_message_id=None)]
        self.assertEqual(run("yes", s), Decision("yes", 1, None, False))
        self.assertEqual(run("Yes!", s).action, "yes")

    def test_yes_only_for_pending(self):
        self.assertEqual(run("yes").action, "none")

    def test_yes_needs_whole_message(self):
        s = [snooze(state="pending", last_sent_at=None)]
        self.assertEqual(run("yes please tell me a joke", s).action, "none")

    def test_ok_does_not_validate_pending(self):
        s = [snooze(state="pending", last_sent_at=None)]
        self.assertEqual(run("ok", s).action, "none")

    def test_yes_from_stranger(self):
        s = [snooze(state="pending", last_sent_at=None)]
        stranger = Sender(platform="telegram", chat_id="999", user_id="999")
        self.assertEqual(run("yes", s, sender=stranger).action, "none")

    def test_yes_with_active_and_pending(self):
        s = [snooze(id=1), snooze(id=2, state="pending", last_sent_at=None, last_message_id=None)]
        self.assertEqual(run("yes", s).snooze_id, 2)
        self.assertEqual(run("ok", s).snooze_id, 1)

    def test_yes_with_confirm_any_rule_prefers_confirm(self):
        # an active "any" snooze confirms; a pending one stays pending
        s = [snooze(id=1, confirm_rule="any"), snooze(id=2, state="pending", last_sent_at=None)]
        self.assertEqual(run("yes", s).action, "confirm")

    def test_reply_to_pending_echo(self):
        s = [
            snooze(id=1, state="pending", last_sent_at=None, last_message_id="e1"),
            snooze(id=2, state="pending", last_sent_at=None, last_message_id="e2"),
        ]
        self.assertEqual(run("yes", s, reply_to="e1").snooze_id, 1)
        self.assertEqual(run("yes", s).snooze_id, 2)  # else the most recent in the list


class Normalize(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(confirm.normalize("  OK!!  "), "ok")
        self.assertEqual(confirm.normalize("👍🏽"), "👍")
        self.assertEqual(confirm.normalize("✔️"), "✅")
        self.assertEqual(confirm.normalize(None), "")


if __name__ == "__main__":
    unittest.main()
