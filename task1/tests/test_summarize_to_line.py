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
