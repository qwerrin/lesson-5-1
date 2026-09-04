"""task1/summarize.py のテスト。

要件の原文は「**必要に応じて**要約し」である。**常に要約する実装は要件と違う。**
要約は情報を落とす操作なので、落とす必要が無いときに落とさない仕掛けをここで固定する。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import summarize  # noqa: E402
from slack_read import SlackMessage  # noqa: E402


def msg(ts, text, user="U1"):
    return SlackMessage(ts=ts, user=user, text=text)


class Unescape(unittest.TestCase):
    """**課題7の逆問題。**

    課題7では「送る前に Slack の変換を再現する」（``&`` → ``&amp;``）ことで
    読み返しの照合を成立させた。今回は読む側なので**逆をやる**。
    やらないと、要約する相手に ``&amp;`` という文字列を見せることになる。
    """

    def test_restores_the_three_control_characters(self):
        self.assertEqual(summarize.unescape_slack("A &amp; B"), "A & B")
        self.assertEqual(summarize.unescape_slack("&lt;tag&gt;"), "<tag>")

    def test_amp_is_restored_last(self):
        """**``&amp;`` を最後に戻す。**

        先に戻すと、``&amp;lt;``（本文として書かれた ``&lt;``）が
        ``&lt;`` → ``<`` まで進んでしまい、**書いていない文字**が現れる。
        課題7の escape が ``&`` を最初に置換したのと、ちょうど裏返しの理由。
        """
        self.assertEqual(summarize.unescape_slack("&amp;lt;"), "&lt;")

    def test_leaves_other_entities_alone(self):
        """Slack が変換するのはこの3つだけ。**他を勝手に戻さない。**"""
        self.assertEqual(summarize.unescape_slack("&quot;x&quot;"), "&quot;x&quot;")


class RenderTranscript(unittest.TestCase):
    def test_lines_are_ordered_and_labelled(self):
        text = summarize.render_transcript([msg("100", "おはよう"), msg("200", "リリースの件")])

        self.assertIn("おはよう", text)
        self.assertIn("リリースの件", text)
        self.assertLess(text.index("おはよう"), text.index("リリースの件"))

    def test_entities_are_unescaped_in_the_transcript(self):
        text = summarize.render_transcript([msg("100", "A &amp; B")])

        self.assertIn("A & B", text)
        self.assertNotIn("&amp;", text)

    def test_user_ids_are_kept_as_is(self):
        """**名前に解決しない。**

        表示名を引くには ``users:read`` が要る。このアプリのスコープは
        ``chat:write`` と ``channels:history`` だけ（2026-08-29 実測）なので
        引けない。**引けないものを引けたように見せる代わりに、ID のまま出す。**
        名前らしきものを捏造するより、ID のほうが正しい。
        """
        text = summarize.render_transcript([msg("100", "やあ", user="U0BQ48E7E3H")])

        self.assertIn("U0BQ48E7E3H", text)

    def test_empty_messages_render_to_empty_string(self):
        self.assertEqual(summarize.render_transcript([]), "")


class NeedsSummary(unittest.TestCase):
    def test_no_messages_needs_no_summary(self):
        """0件を要約に回さない。材料の無い要約は発明になる。"""
        self.assertFalse(summarize.needs_summary([]))

    def test_a_single_short_message_is_passed_through(self):
        """1件の短文を要約しても情報が落ちるだけ。**そのまま送る。**"""
        self.assertFalse(summarize.needs_summary([msg("100", "19時から会議です")]))

    def test_many_messages_need_a_summary(self):
        messages = [msg(str(i), "話題" * 5) for i in range(summarize.MIN_MESSAGES_TO_SUMMARIZE)]

        self.assertTrue(summarize.needs_summary(messages))

    def test_long_text_needs_a_summary_even_with_few_messages(self):
        """件数が少なくても、長ければ要約する。**LINE で読むのは電話の画面。**"""
        long_one = msg("100", "あ" * (summarize.SUMMARIZE_THRESHOLD_CHARS + 1))

        self.assertTrue(summarize.needs_summary([long_one]))

    def test_the_threshold_is_ours_not_a_platform_limit(self):
        """閾値は**こちらの都合**の数字であって、LINE の仕様ではない。

        LINE のテキスト上限を定数に置くと、相手が変えた日に
        **正しい送信を拒む**側で壊れる（``line_send`` で長さ検査を
        持たないと決めたのと同じ）。ここは「読みやすさ」の線引きなので、
        こちらで決めてよい——**そのことを名前と文書で言う**。
        """
        self.assertLess(summarize.SUMMARIZE_THRESHOLD_CHARS, 5000)


class BuildPrompt(unittest.TestCase):
    def test_prompt_contains_the_transcript(self):
        prompt = summarize.build_prompt("[10:00] U1: リリースの話")

        self.assertIn("リリースの話", prompt)

    def test_prompt_forbids_adding_facts(self):
        """**書かれていないことを足すなと明示する。**

        要約は入力に無いことを言い出す失敗をする。プロンプトで禁じても
        完全には防げないが、書かないよりは効く。
        """
        prompt = summarize.build_prompt("なにか")

        self.assertIn("書かれていない", prompt)

    def test_empty_transcript_is_refused(self):
        with self.assertRaises(ValueError):
            summarize.build_prompt("   ")


class BuildMessage(unittest.TestCase):
    """LINE へ送る本文。**0件でも送る。**"""

    def test_zero_messages_still_produces_a_message(self):
        """送らないと「投稿が無い」のか「動いていない」のか区別できない。

        課題10 の ``notify_schedule`` と同じ判断。
        """
        body = summarize.build_message(
            summary=None, messages=[], channel_label="#general"
        )

        self.assertTrue(body.strip())
        self.assertIn("#general", body)

    def test_the_count_is_always_shown(self):
        body = summarize.build_message(
            summary=None, messages=[msg("100", "やあ")], channel_label="#general"
        )

        self.assertIn("1", body)

    def test_summary_is_marked_as_a_summary(self):
        """**要約であることを本文に書く。**

        原文と要約が同じ見た目で届くと、受け取る側は「これで全部」と読む。
        情報を落としたことは、落とした側が言う。
        """
        body = summarize.build_message(
            summary="3行の要約", messages=[msg("100", "x")], channel_label="#general"
        )

        # **見出しそのものを見る。** 「要約」だけで探すと、原文リンクの行に
        # 入っている「要約は入口です」で満たされてしまい、見出しを消しても
        # テストが通る（2026-08-29・ミューテーションで検出）。
        # 課題9で踏んだ「2つの assert が両方とも無関係な場所の文字列で
        # 満たされていた」と同じ形。
        self.assertIn("―― 要約 ――", body)
        self.assertIn("3行の要約", body)

    def test_passthrough_is_not_labelled_as_a_summary(self):
        body = summarize.build_message(
            summary=None, messages=[msg("100", "19時から会議")], channel_label="#general"
        )

        self.assertIn("19時から会議", body)
        self.assertNotIn("―― 要約 ――", body)

    def test_skipped_counts_are_reported(self):
        """**捨てた件数を本文に出す。**

        「3件」と書いてあるのに Slack を見たら5件ある、という食い違いを
        受け取る側が自力で解けるようにする。
        """
        body = summarize.build_message(
            summary=None,
            messages=[msg("100", "x")],
            channel_label="#general",
            skipped={"channel_join": 2},
        )

        self.assertIn("2", body)

    def test_summary_carries_a_link_to_the_source(self):
        """**要約したときは、落とした先への入口を必ず置く。**

        2026-08-29 の実機で、要約が原文に無い語（「Google」）を足し、
        別人の発言を1人に畳んだ。プロンプトで禁じても起きる。
        目的は「見逃しを防ぐ」ことなので、**原文へ辿れる道**を残す。
        """
        body = summarize.build_message(
            summary="要約",
            messages=[msg("100", "x")],
            channel_label="#general",
            permalink="https://example.slack.com/archives/C1/p1",
        )

        self.assertIn("https://example.slack.com/archives/C1/p1", body)

    def test_passthrough_does_not_carry_a_link(self):
        """**原文をそのまま送るときは付けない。**

        落としていないものへの「原文はこちら」は冗長で、
        リンクの意味（落とした先への入口）を薄める。
        """
        body = summarize.build_message(
            summary=None,
            messages=[msg("100", "19時から会議")],
            channel_label="#general",
            permalink="https://example.slack.com/archives/C1/p1",
        )

        self.assertNotIn("https://", body)

    def test_missing_link_does_not_break_the_body(self):
        body = summarize.build_message(
            summary="要約", messages=[msg("100", "x")], channel_label="#general", permalink=""
        )

        self.assertIn("要約", body)

    def test_truncation_is_reported(self):
        """**打ち切ったことを受け取る側に伝える。**

        黙って切ると、欠けた要約が完全な要約として届く。
        """
        body = summarize.build_message(
            summary="要約",
            messages=[msg("100", "x")],
            channel_label="#general",
            truncated=True,
        )

        self.assertIn("打ち切", body)


if __name__ == "__main__":
    unittest.main()


def reply_msg(ts, parent, text, user="U2"):
    return SlackMessage(ts=ts, user=user, text=text, thread_ts=parent)


def parent_msg(ts, text, user="U1"):
    """スレッドの**親**。``thread_ts`` は自分の ``ts`` と等しい。"""
    return SlackMessage(ts=ts, user=user, text=text, thread_ts=ts)


class IsReply(unittest.TestCase):
    """返信かどうかの見分け。**親を返信と数えない。**

    公式の見分け方は「``thread_ts`` と ``ts`` が等しければ親、違えば返信」。
    ``thread_ts`` の有無だけで決めると、**親まで返信として数える**。
    親はチャンネル本文にも出ているので、印を付けると
    「Slack で見えているのに返信と書いてある」という食い違いになる。
    """

    def test_plain_message_is_not_a_reply(self):
        self.assertFalse(summarize.is_reply(msg("100", "本文")))

    def test_thread_parent_is_not_a_reply(self):
        self.assertFalse(summarize.is_reply(parent_msg("100", "親")))

    def test_thread_reply_is_a_reply(self):
        self.assertTrue(summarize.is_reply(reply_msg("101", "100", "返信")))


class TranscriptMarks(unittest.TestCase):
    def test_reply_is_marked(self):
        text = summarize.render_transcript([reply_msg("101", "100", "あとから")])

        self.assertTrue(text.startswith("↳ "))

    def test_parent_is_not_marked(self):
        """**親に印を付けない。** 付けると内訳が狂う。"""
        text = summarize.render_transcript([parent_msg("100", "はじめ")])

        self.assertFalse(text.startswith("↳"))

    def test_plain_message_is_not_marked(self):
        text = summarize.render_transcript([msg("100", "ふつう")])

        self.assertFalse(text.startswith("↳"))
