"""task1/summarize_to_line.py のテスト。

**この課題の at-least-once は、順番でできている。**

読む → 要約 → 送る → **成功したら位置を進める**。最後の1手が最後にある
ことだけが「取りこぼさない」を支えているので、そこを一番厚く固定する。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state  # noqa: E402
import summarize_to_line as tool  # noqa: E402
from common import line_auth  # noqa: E402

CHANNEL = "C0BQFU7STLM"
USER_ID = "U" + "8" * 32
MESSAGE_ID = "627984934547751122"


# ------------------------------------------------------------------ 偽物


class FakeResponse:
    def __init__(self, *, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = ""
        self.headers = headers if headers is not None else {}

    def json(self):
        if self._payload is None:
            raise ValueError("JSON ではありません")
        return self._payload


class FakeSlack:
    def __init__(self, messages, *, permalink="https://example.slack.com/archives/C1/p1"):
        self._messages = messages
        self._permalink = permalink
        self.calls = []
        self.permalink_calls = []

    def conversations_history(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "ok": True,
            "messages": list(self._messages),
            "response_metadata": {"next_cursor": ""},
        }

    def chat_getPermalink(self, **kwargs):
        self.permalink_calls.append(kwargs)
        return {"ok": bool(self._permalink), "permalink": self._permalink}


class FakeLineSession:
    """push と通数の読み取りを持つ。

    通数は**送信のたびに増える**ようにしてある。増分を照合の材料に使うので、
    固定値にすると「増えたことを確かめた」というテストが常に通ってしまう。
    """

    def __init__(self, *, push_fails=False, usage=40, fail_usage_on=None):
        self.pushes = []
        self.push_fails = push_fails
        self.usage = usage
        #: 何回目の通数読み取りで失敗させるか。1=送信前 / 2=送信後。
        #: **どちらで落ちるかで正しい振る舞いが違う**ので、区別できる形にする。
        self.fail_usage_on = fail_usage_on
        self.usage_calls = 0
        self.headers = {}

    def post(self, url, **kwargs):
        self.pushes.append((url, kwargs))
        if self.push_fails:
            return FakeResponse(status_code=500, payload={"message": "boom"})
        self.usage += 1
        return FakeResponse(
            payload={"sentMessages": [{"id": MESSAGE_ID, "quoteToken": "q"}]},
            headers={"x-line-request-id": "req-1"},
        )

    def get(self, url, **kwargs):
        self.usage_calls += 1
        if self.fail_usage_on == self.usage_calls:
            return FakeResponse(status_code=500, payload={"message": "boom"})
        return FakeResponse(payload={"totalUsage": self.usage})


class FakeGemini:
    def __init__(self, text="要約された内容", fails=False):
        self.calls = []
        self._text = text
        self._fails = fails

    class _Models:
        def __init__(self, outer):
            self.outer = outer

        def generate_content(self, *, model, contents, config=None):
            self.outer.calls.append(contents)
            if self.outer._fails:
                raise RuntimeError("gemini が落ちた")
            return type("R", (), {"text": self.outer._text})()

    @property
    def models(self):
        return self._Models(self)


def slack_message(ts, text="本文", user="U1", **extra):
    payload = {"ts": ts, "text": text, "user": user}
    payload.update(extra)
    return payload


def bot_info():
    return line_auth.BotInfo(
        user_id="U" + "1" * 32,
        basic_id="@fake0000",
        display_name="開発テスト",
        chat_mode="bot",
        mark_as_read_mode="auto",
    )


def reachable():
    return line_auth.Reachability(
        reachable=True,
        reason="",
        profile=line_auth.Profile(user_id=USER_ID, display_name="なな"),
    )


def unreachable():
    return line_auth.Reachability(
        reachable=False, reason=line_auth.UNREACHABLE_REASON, profile=None
    )


class JudgeSend(unittest.TestCase):
    """送る前の判定。**通信をしない純粋な関数**なので、ここで全部の枝を踏む。"""

    def test_reachable_and_enough_quota_passes(self):
        gate = tool.judge_send(reachable(), 100)

        self.assertTrue(gate.ok)
        self.assertEqual(gate.blocks, ())

    def test_unreachable_blocks(self):
        gate = tool.judge_send(unreachable(), 100)

        self.assertFalse(gate.ok)
        self.assertTrue(gate.blocks)

    def test_all_reasons_are_collected(self):
        """**理由を1つ返して止めない。**

        1つずつ返すと、直して再実行したら次の理由で止まる、を繰り返させる。
        """
        gate = tool.judge_send(unreachable(), 0)

        self.assertEqual(len(gate.blocks), 2)

    def test_none_remaining_is_unlimited_not_zero(self):
        """``None`` は**無制限**。0 は「上限はあるが使い切った」で真逆。

        混ぜると、ガードが**無制限のアカウントで送信を止める**。
        """
        gate = tool.judge_send(reachable(), None)

        self.assertTrue(gate.ok)

    def test_zero_remaining_blocks(self):
        self.assertFalse(tool.judge_send(reachable(), 0).ok)

    def test_negative_remaining_is_shown_as_is(self):
        """負の値を丸めない。

        丸めると「あと 0 通」と「12 通ぶん超過」が同じ文面になり、
        直しかたの見当が付かなくなる。
        """
        gate = tool.judge_send(reachable(), -12)

        self.assertIn("-12", " ".join(gate.blocks))

    def test_low_remaining_is_a_note_not_a_block(self):
        gate = tool.judge_send(reachable(), tool.LOW_REMAINING)

        self.assertTrue(gate.ok)
        self.assertTrue(gate.notes)


class GateEvidence(unittest.TestCase):
    """**確認したことは、確認した値で言う。**

    「送信前の確認: 通過」だけを出すと、何を見て通過にしたのかが残らない。
    実行画面はこの課題の証拠になるので、**見た値そのもの**を出す。
    """

    def test_checked_values_are_reported(self):
        gate = tool.judge_send(reachable(), 137)

        joined = " ".join(gate.checked)

        self.assertIn("137", joined)

    def test_checked_names_the_destination_state(self):
        gate = tool.judge_send(reachable(), 100)

        self.assertTrue(any("届" in line for line in gate.checked))

    def test_checked_is_present_even_when_blocked(self):
        """止めたときこそ、何を見たのかが要る。"""
        gate = tool.judge_send(unreachable(), 0)

        self.assertTrue(gate.checked)

    def test_format_gate_includes_the_checked_values(self):
        lines = tool.format_gate(tool.judge_send(reachable(), 137))

        self.assertTrue(any("137" in line for line in lines))


class RunCase(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.state_path = Path(self._dir.name) / "state.json"
        self.results_path = Path(self._dir.name) / "results.json"

    def tearDown(self):
        self._dir.cleanup()

    def run_tool(
        self,
        messages,
        *,
        gemini=None,
        session=None,
        reachability=None,
        remaining=100,
        dry_run=False,
    ):
        self.session = session or FakeLineSession()
        self.gemini = gemini if gemini is not None else FakeGemini()
        self.slack = FakeSlack(messages)
        return tool.run(
            slack_client=self.slack,
            line_session=self.session,
            bot_info=bot_info(),
            reachability=reachability or reachable(),
            remaining=remaining,
            gemini=self.gemini,
            channel=CHANNEL,
            channel_label="#general",
            to=USER_ID,
            state_path=self.state_path,
            results_path=self.results_path,
            dry_run=dry_run,
        )


# ================================================== 位置を進める順番


class CursorOrdering(RunCase):
    def test_cursor_advances_after_a_successful_send(self):
        outcome = self.run_tool([slack_message("300")])

        self.assertTrue(outcome.sent)
        self.assertEqual(state.cursor_for(state.load(self.state_path), CHANNEL), "300")

    def test_cursor_does_not_advance_when_the_send_fails(self):
        """**at-least-once の要。**

        送信に失敗した回で位置を進めると、その範囲は二度と読まれない＝
        取りこぼす。重複して送るほうが、落とすよりましである。
        """
        outcome = self.run_tool(
            [slack_message("300")], session=FakeLineSession(push_fails=True)
        )

        self.assertFalse(outcome.sent)
        self.assertIsNone(state.cursor_for(state.load(self.state_path), CHANNEL))

    def test_cursor_does_not_advance_on_dry_run(self):
        outcome = self.run_tool([slack_message("300")], dry_run=True)

        self.assertFalse(outcome.sent)
        self.assertIsNone(state.cursor_for(state.load(self.state_path), CHANNEL))

    def test_cursor_does_not_advance_when_the_gate_blocks(self):
        outcome = self.run_tool([slack_message("300")], reachability=unreachable())

        self.assertFalse(outcome.gate.ok)
        self.assertIsNone(state.cursor_for(state.load(self.state_path), CHANNEL))

    def test_previous_cursor_is_used_as_oldest(self):
        state.save(self.state_path, state.advanced(state.empty(), CHANNEL, "250"))

        self.run_tool([slack_message("300")])

        self.assertEqual(self.slack.calls[0]["oldest"], "250")

    def test_cursor_advances_even_when_only_excluded_messages_arrived(self):
        """``channel_join`` しか無くても位置は進む。**読んだことは事実。**

        進めないと、次回も同じ ``channel_join`` を読み直す。
        """
        outcome = self.run_tool([slack_message("300", subtype="channel_join")])

        self.assertTrue(outcome.sent)
        self.assertEqual(state.cursor_for(state.load(self.state_path), CHANNEL), "300")


# ================================================== 送るかどうか


class Sending(RunCase):
    def test_dry_run_does_not_push(self):
        self.run_tool([slack_message("300")], dry_run=True)

        self.assertEqual(self.session.pushes, [])

    def test_dry_run_still_builds_the_body(self):
        """**``--dry-run`` は「何もしない」ではない。**

        何を送るはずだったかを必ず作って見せる。見せないと、
        本番で初めて中身を見ることになる。
        """
        outcome = self.run_tool([slack_message("300", text="リリースの件")], dry_run=True)

        self.assertIn("リリースの件", outcome.body)

    def test_blocked_gate_does_not_push(self):
        self.run_tool([slack_message("300")], reachability=unreachable())

        self.assertEqual(self.session.pushes, [])

    def test_zero_messages_are_still_sent(self):
        """**0件でも送る。**

        送らないと、通知が来ない日が「投稿が無かった」のか
        「動いていない」のかを受け取る側から区別できない。
        """
        outcome = self.run_tool([])

        self.assertTrue(outcome.sent)
        self.assertIn("0", outcome.body)

    def test_zero_messages_do_not_advance_the_cursor(self):
        """0件は「ここまで読んだ」ではない。位置は据え置く。"""
        self.run_tool([])

        self.assertIsNone(state.cursor_for(state.load(self.state_path), CHANNEL))


# ================================================== 要約するかどうか


class Summarizing(RunCase):
    def test_short_input_does_not_call_gemini(self):
        """**要約が要らないときは呼ばない。** 呼べば課金される。"""
        outcome = self.run_tool([slack_message("300", text="19時から会議です")])

        self.assertEqual(self.gemini.calls, [])
        self.assertFalse(outcome.summarized)

    def test_long_input_calls_gemini(self):
        messages = [slack_message(str(i), text="議論" * 10) for i in range(300, 310)]

        outcome = self.run_tool(messages)

        self.assertEqual(len(self.gemini.calls), 1)
        self.assertTrue(outcome.summarized)
        self.assertIn("要約された内容", outcome.body)

    def test_summary_failure_stops_the_send(self):
        """**要約に失敗したら送らない。**

        巨大な原文を代わりに送ると、読めないうえ月200通の枠を消費する。
        送らなければ位置も進まないので、次回そのまま拾える（at-least-once）。
        """
        messages = [slack_message(str(i), text="議論" * 10) for i in range(300, 310)]

        outcome = self.run_tool(messages, gemini=FakeGemini(fails=True))

        self.assertFalse(outcome.sent)
        self.assertEqual(self.session.pushes, [])
        self.assertIsNone(state.cursor_for(state.load(self.state_path), CHANNEL))

    def test_summary_carries_the_source_link(self):
        """要約したときは**原文への入口**が本文に入る。"""
        messages = [slack_message(str(i), text="議論" * 10) for i in range(300, 310)]

        outcome = self.run_tool(messages)

        self.assertIn("https://example.slack.com/archives/C1/p1", outcome.body)

    def test_permalink_is_not_fetched_when_no_summary(self):
        """**要約しないなら API も呼ばない。** 使わない値を取りに行かない。"""
        self.run_tool([slack_message("300", text="19時から会議です")])

        self.assertEqual(self.slack.permalink_calls, [])

    def test_missing_permalink_does_not_stop_the_send(self):
        """リンクが取れなくても送る。**リンクは「あると良いもの」。**"""
        messages = [slack_message(str(i), text="議論" * 10) for i in range(300, 310)]
        self.session = FakeLineSession()
        self.gemini = FakeGemini()
        self.slack = FakeSlack(messages, permalink="")
        outcome = tool.run(
            slack_client=self.slack,
            line_session=self.session,
            bot_info=bot_info(),
            reachability=reachable(),
            remaining=100,
            gemini=self.gemini,
            channel=CHANNEL,
            channel_label="#general",
            to=USER_ID,
            state_path=self.state_path,
            results_path=self.results_path,
        )

        self.assertTrue(outcome.sent)
        self.assertNotIn("▼ 原文", outcome.body)

    def test_summary_failure_is_reported(self):
        messages = [slack_message(str(i), text="議論" * 10) for i in range(300, 310)]

        outcome = self.run_tool(messages, gemini=FakeGemini(fails=True))

        self.assertTrue(outcome.error)


# ================================================== 記録


class Record(RunCase):
    def test_record_has_what_verification_needs(self):
        self.run_tool([slack_message("300", text="やあ")])

        payload = json.loads(self.results_path.read_text(encoding="utf-8"))

        for key in ("channel", "message_id", "basic_id", "read_count", "latest_ts"):
            with self.subTest(key=key):
                self.assertIn(key, payload)

    def test_record_never_contains_the_raw_destination(self):
        """記録は public リポジトリに入る。"""
        self.run_tool([slack_message("300")])

        body = self.results_path.read_text(encoding="utf-8")

        self.assertNotIn(USER_ID, body)

    def test_record_carries_the_usage_delta(self):
        """**別のエンドポイントが送信を認めた**という材料を残す。

        LINE には bot が送ったテキストを読み返す API が無い。
        ``totalUsage`` の増分は「送信対象として1通数えた」ことだけを言う——
        **何を送ったかは言わない**が、押した経路とは別の口から取れる。
        """
        self.run_tool([slack_message("300")])

        payload = json.loads(self.results_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["usage_after"] - payload["usage_before"], 1)

    def test_record_carries_the_oldest_used(self):
        """**どこから読んだかを残す。**

        残さないと、あとから ``read_count`` を再現できない
        （位置は次の実行のために先へ進んでしまう）。
        """
        state.save(self.state_path, state.advanced(state.empty(), CHANNEL, "250"))

        self.run_tool([slack_message("300")])

        payload = json.loads(self.results_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["oldest"], "250")

    def test_usage_after_is_null_when_it_cannot_be_read(self):
        """**取れなかったことを 0 や「前と同じ」に倒さない。**

        送信自体は成功しているので失敗にはしない。だが増分を 0 と書くと
        「送ったのに数えられていない」という**別の事実**に化ける。
        """
        outcome = self.run_tool(
            [slack_message("300")], session=FakeLineSession(fail_usage_on=2)
        )

        self.assertTrue(outcome.sent)
        self.assertIsNone(outcome.record["usage_after"])

    def test_failing_to_read_usage_before_stops_the_send(self):
        """**送る前に読めないなら送らない。**

        送信前の値が無いと増分を出せず、送った証拠が1つ減る。
        まだ送っていないのだから、止まるほうが安い。
        """
        session = FakeLineSession(fail_usage_on=1)

        outcome = self.run_tool([slack_message("300")], session=session)

        self.assertFalse(outcome.sent)
        self.assertEqual(session.pushes, [])
        self.assertIsNone(state.cursor_for(state.load(self.state_path), CHANNEL))

    def test_record_is_not_written_when_nothing_was_sent(self):
        """**送っていない回の記録を残さない。**

        残すと、あとから results.json を見た人が「送った」と読む。
        """
        self.run_tool([slack_message("300")], dry_run=True)

        self.assertFalse(self.results_path.exists())


if __name__ == "__main__":
    unittest.main()


# ================================================== スレッドの返信（発展）


class FakeSlackWithThreads(FakeSlack):
    """``conversations_replies`` も持つ相手。

    **返信は ``conversations_history`` に出さない。** 2026-09-04 に実測した
    本物の振る舞いがこれで、偽物がここを間違えると「history にも出るから
    足さなくていい」という誤った実装を通してしまう。
    """

    def __init__(self, messages, *, replies=None, **kwargs):
        super().__init__(messages, **kwargs)
        self.replies = {k: list(v) for k, v in (replies or {}).items()}
        self.reply_calls = []

    def conversations_replies(self, **kwargs):
        self.reply_calls.append(kwargs)
        ts = kwargs.get("ts")
        parent = {"ts": ts, "text": "親", "user": "U1", "thread_ts": ts}
        return {
            "ok": True,
            "messages": [parent] + list(self.replies.get(ts, [])),
            "response_metadata": {"next_cursor": ""},
        }


def thread_reply(ts, parent, text="返信", user="U2"):
    return {"ts": ts, "text": text, "user": user, "thread_ts": parent}


class ThreadRunCase(RunCase):
    def run_tool(self, messages, *, replies=None, include_replies=True, **kwargs):
        self.session = kwargs.pop("session", None) or FakeLineSession()
        self.gemini = kwargs.pop("gemini", None) or FakeGemini()
        self.slack = FakeSlackWithThreads(messages, replies=replies)
        return tool.run(
            slack_client=self.slack,
            line_session=self.session,
            bot_info=bot_info(),
            reachability=kwargs.pop("reachability", None) or reachable(),
            remaining=kwargs.pop("remaining", 100),
            gemini=self.gemini,
            channel=CHANNEL,
            channel_label="#general",
            to=USER_ID,
            state_path=self.state_path,
            results_path=self.results_path,
            include_replies=include_replies,
            **kwargs,
        )


class RepliesAreOptional(ThreadRunCase):
    def test_disabled_calls_conversations_replies_zero_times(self):
        """既定では1回も呼ばない。**足した機能が黙って課金・通信を増やさない。**"""
        self.run_tool([slack_message("300")], include_replies=False)

        self.assertEqual(self.slack.reply_calls, [])


class RepliesAreIncluded(ThreadRunCase):
    def test_replies_reach_the_body(self):
        """**この発展の本題。**

        ``conversations.history`` は返信を返さない（2026-09-04 実測：親1・返信3を
        投稿して history の増分は +1）。親だけ届くと「金曜でいけそう？」しか
        読めず、そこで決まった変更が丸ごと落ちる。
        """
        outcome = self.run_tool(
            [slack_message("300", text="金曜でいけそう？")],
            replies={"300": [thread_reply("301", "300", text="月曜に倒したい")]},
        )

        self.assertIn("月曜に倒したい", outcome.body)

    def test_reply_count_is_shown_separately(self):
        """**返信の件数を別に出す。**

        受け取った人が Slack を開いても、返信はチャンネル本文に出ていない。
        合計だけ出すと「3件と書いてあるのに1件しか見えない」になる。

        **見出しの文字列そのものを見る。** 最初は ``"返信" in body`` と
        ``"2" in body`` で書いていたが、どちらも**別の場所で満たされていた**——
        「返信」は偽の本文に、「2」は返信の ts（``302``）に入っている。
        件数の表示を丸ごと消すミューテーションが素通りした（2026-09-04）。
        課題9・課題1で踏んだのと同じ形が、また別の場所で開いた。
        """
        outcome = self.run_tool(
            [slack_message("300")],
            replies={"300": [thread_reply("301", "300"), thread_reply("302", "300")]},
        )

        self.assertIn("（うちスレッド返信 2 件）", outcome.body.splitlines()[0])

    def test_headline_says_nothing_about_threads_when_there_are_none(self):
        """返信が0件のときに内訳を出さない。**無い内訳を書くと嘘になる。**"""
        outcome = self.run_tool([slack_message("300")])

        self.assertNotIn("スレッド返信", outcome.body)

    def test_replies_are_merged_in_time_order(self):
        outcome = self.run_tool(
            [slack_message("300", text="さき"), slack_message("400", text="あと")],
            replies={"300": [thread_reply("350", "300", text="あいだ")]},
        )

        body = outcome.body
        self.assertLess(body.index("さき"), body.index("あいだ"))
        self.assertLess(body.index("あいだ"), body.index("あと"))


class WatchingLaterReplies(ThreadRunCase):
    def test_every_read_message_is_watched_not_only_current_parents(self):
        """**返信は後から付く。**

        読んだ時点で ``reply_count`` が 0 でも、あとで議論が始まる。
        「いま返信を持っている親」だけを見張ると、**いちばん多い形を取りこぼす**。
        """
        self.run_tool([slack_message("300")])

        self.assertEqual(
            list(state.watch_for(state.load(self.state_path), CHANNEL)), ["300"]
        )

    def test_watched_parent_is_polled_after_the_cursor_moved_past_it(self):
        """位置が親を追い越した後も返信を拾える。**これができないと発展の意味が無い。**"""
        self.run_tool([slack_message("300")])

        second = self.run_tool([], replies={"300": [thread_reply("301", "300")]})

        self.assertIn("301", [m.ts for m in second.replies.messages])

    def test_thread_position_does_not_advance_when_the_send_fails(self):
        """``cursors`` と同じ順番を守る。送信が失敗した回で進めると取りこぼす。"""
        self.run_tool([slack_message("300")])

        self.run_tool(
            [],
            replies={"300": [thread_reply("301", "300")]},
            session=FakeLineSession(push_fails=True),
        )

        self.assertEqual(
            state.watch_for(state.load(self.state_path), CHANNEL), {"300": ""}
        )

    def test_thread_position_advances_after_a_successful_send(self):
        self.run_tool([slack_message("300")])

        self.run_tool([], replies={"300": [thread_reply("301", "300")]})

        self.assertEqual(
            state.watch_for(state.load(self.state_path), CHANNEL), {"300": "301"}
        )

    def test_a_reply_is_not_sent_twice(self):
        """位置が効いていることを、**2回目の本文が空になること**で確かめる。"""
        self.run_tool([slack_message("300")])
        self.run_tool([], replies={"300": [thread_reply("301", "300", text="ただ1回")]})

        third = self.run_tool([], replies={"300": [thread_reply("301", "300", text="ただ1回")]})

        self.assertNotIn("ただ1回", third.body)


class WindowIsNotSilent(ThreadRunCase):
    def test_dropped_parents_are_reported(self):
        """窓から落ちた親は**画面に出す**。

        落ちた親に後から付く返信は二度と読まれない。黙って落とすと、
        この発展が塞いだはずの穴が、別の形で開き直す。
        """
        many = [slack_message(f"{i}.0") for i in range(1, state.WATCH_LIMIT + 3)]

        outcome = self.run_tool(many)

        self.assertEqual(outcome.dropped_threads, ("1.0", "2.0"))

    def test_failed_threads_are_reported(self):
        class Failing(FakeSlackWithThreads):
            def conversations_replies(self, **kwargs):
                raise RuntimeError("boom")

        self.run_tool([slack_message("300")])
        self.slack = Failing([], replies={})
        outcome = tool.run(
            slack_client=self.slack,
            line_session=FakeLineSession(),
            bot_info=bot_info(),
            reachability=reachable(),
            remaining=100,
            gemini=FakeGemini(),
            channel=CHANNEL,
            channel_label="#general",
            to=USER_ID,
            state_path=self.state_path,
            results_path=self.results_path,
            include_replies=True,
        )

        self.assertEqual(outcome.replies.failed, ("300",))


class CommandLine(RunCase):
    """CLI の層。**フラグは、足すだけでは効かない。**

    ``parse_args`` に生えていても ``main`` が ``run`` へ渡し忘れれば、
    利用者から見て**黙って無効**になる。エラーも出ないし、
    「返信 0 件」と「返信を読んでいない」が同じ画面になる。
    """

    def factory(self, messages, replies=None):
        session = FakeLineSession()
        slack = FakeSlackWithThreads(messages, replies=replies)
        self.slack = slack

        def build(channel):
            return {
                "slack_client": slack,
                "slack_identity": None,
                "line_session": session,
                "bot_info": bot_info(),
                "reachability": reachable(),
                "remaining": 100,
                "gemini": FakeGemini(),
                "to": USER_ID,
            }

        return build

    def base_argv(self):
        return ["--channel", CHANNEL, "--state", str(self.state_path), "--dry-run"]

    def test_flag_defaults_to_off(self):
        self.assertFalse(tool.parse_args(["--channel", CHANNEL]).include_replies)

    def test_flag_is_parsed(self):
        args = tool.parse_args(["--channel", CHANNEL, "--include-replies"])

        self.assertTrue(args.include_replies)

    def test_main_reaches_conversations_replies_when_asked(self):
        """**渡し忘れをここで殺す。** 呼ばれたことを本物の経路で確かめる。"""
        build = self.factory([slack_message("300")])

        code = tool.main(self.base_argv() + ["--include-replies"], factory=build)

        self.assertEqual(code, 0)
        self.assertEqual([call["ts"] for call in self.slack.reply_calls], ["300"])

    def test_main_does_not_touch_threads_by_default(self):
        build = self.factory([slack_message("300")])

        code = tool.main(self.base_argv(), factory=build)

        self.assertEqual(code, 0)
        self.assertEqual(self.slack.reply_calls, [])


class WatchCountIsAlwaysKnown(ThreadRunCase):
    """見張っている本数は**記録から読まない**。

    最初は ``outcome.record["reply_watch"]`` から読んでいた。記録は送信に
    成功したときしか作られないので、``--dry-run`` の画面には
    「見張っているスレッド **?** 本」と出た。

    **値は手元にある。** 出せるものを「分からない」と表示すると、
    読んだ人は「数えられない事情がある」と受け取る。実行画面は提出物なので、
    ここは埋まっていなければならない。
    """

    def test_dry_run_still_knows_how_many_threads_are_watched(self):
        outcome = self.run_tool([slack_message("300")], dry_run=True)

        self.assertEqual(outcome.watching, 1)

    def test_blocked_send_still_knows(self):
        outcome = self.run_tool([slack_message("300")], remaining=0)

        self.assertFalse(outcome.sent)
        self.assertEqual(outcome.watching, 1)

    def test_zero_when_replies_are_off(self):
        """読んでいないときは 0。**「0本を見張った」ではなく「見張っていない」。**"""
        outcome = self.run_tool([slack_message("300")], include_replies=False)

        self.assertEqual(outcome.watching, 0)
