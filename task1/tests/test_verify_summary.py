"""task1/verify_summary.py のテスト。

**この照合には、原理的に届かない場所がある。**

LINE には bot が送ったテキストを読み返す API が無い（課題9で公式 OpenAPI 定義に
当たって確認）。だから「送った本文が届いたか」は最後まで機械では言えない。

照合が扱うのは**言えることだけ**で、言えないことは「確認できない」と
**必ず表示する**。合格したときこそ表示する——全部 OK の画面に
「本文の到達は確認していません」が出ていないと、読んだ人は
「全部確かめた」と受け取る。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import verify_summary as verify  # noqa: E402
from common import line_auth  # noqa: E402

CHANNEL = "C0BQFU7STLM"
BASIC_ID = "@687jseqd"
BOT_USER_ID = "U" + "1" * 32


def record(**overrides):
    payload = {
        "channel": CHANNEL,
        "oldest": "250",
        "to_masked": "U8…88",
        "basic_id": BASIC_ID,
        "bot_user_id": BOT_USER_ID,
        "usage_before": 40,
        "usage_after": 41,
        "read_count": 2,
        "skipped": {"channel_join": 1},
        "pages": 1,
        "truncated": False,
        "latest_ts": "300",
        "summarized": True,
        "body_chars": 120,
        "message_id": "629447891827818550",
        "request_id": "req-1",
    }
    payload.update(overrides)
    return payload


def bot_info(basic_id=BASIC_ID, user_id=BOT_USER_ID):
    return line_auth.BotInfo(
        user_id=user_id,
        basic_id=basic_id,
        display_name="開発テスト",
        chat_mode="bot",
        mark_as_read_mode="auto",
    )


class FakeSlack:
    def __init__(self, messages, *, replies=None):
        self._messages = messages
        self._replies = {k: list(v) for k, v in (replies or {}).items()}
        self.calls = []
        self.reply_calls = []

    def conversations_history(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "ok": True,
            "messages": list(self._messages),
            "response_metadata": {"next_cursor": ""},
        }

    def conversations_replies(self, **kwargs):
        """**返信は history に出さない。** 本物がそうだから（2026-09-04 実測）。"""
        self.reply_calls.append(kwargs)
        ts = kwargs.get("ts")
        parent = {"ts": ts, "text": "親", "user": "U1", "thread_ts": ts}
        return {
            "ok": True,
            "messages": [parent] + list(self._replies.get(ts, [])),
            "response_metadata": {"next_cursor": ""},
        }


def slack_message(ts, text="本文", user="U1", **extra):
    payload = {"ts": ts, "text": text, "user": user}
    payload.update(extra)
    return payload


class LocalChecks(unittest.TestCase):
    """記録の中だけで言えること。**ここだけで合格にしない。**"""

    def test_usage_delta_of_one_passes(self):
        checks = verify.build_local_checks(record())

        self.assertTrue(verify.all_ok(checks))

    def test_usage_delta_of_zero_fails(self):
        """増分0は「送信対象として数えられなかった」ことを意味する。"""
        checks = verify.build_local_checks(record(usage_after=40))

        self.assertFalse(verify.all_ok(checks))

    def test_unreadable_usage_after_is_not_a_pass(self):
        """**``None`` を「一致」に倒さない。**

        読めなかったことは、増えたことの証拠にならない。
        """
        checks = verify.build_local_checks(record(usage_after=None))

        self.assertFalse(verify.all_ok(checks))

    def test_a_record_missing_the_key_is_named_as_old_not_as_unreadable(self):
        """**「キーが無い」と「読めなかった」は別の事実。**

        2026-08-29 に実際に踏んだ。``usage_after`` を実装する前に書かれた
        記録を新しい照合器にかけたところ、``.get()`` が ``None`` を返し、
        「送信後の通数を読めなかった」と報告された。**送信時にはそんな
        失敗は起きていない**——単に記録が古かっただけである。

        同じ NG にすると、直しかたが正反対のものを1つの表示に畳む
        （前者は撮り直し、後者は API の調査）。
        """
        payload = record()
        del payload["usage_after"]
        del payload["usage_before"]

        checks = verify.build_local_checks(payload)
        text = "\n".join(verify.format_checks(checks))

        self.assertFalse(verify.all_ok(checks))
        self.assertIn("古い", text)

    def test_present_but_null_is_still_unreadable(self):
        """キーがあって ``None`` なら、こちらは本当に「読めなかった」。"""
        checks = verify.build_local_checks(record(usage_after=None))
        text = "\n".join(verify.format_checks(checks))

        self.assertFalse(verify.all_ok(checks))
        self.assertIn("読めなかった", text)
        self.assertNotIn("古い", text)

    def test_missing_message_id_fails(self):
        checks = verify.build_local_checks(record(message_id=""))

        self.assertFalse(verify.all_ok(checks))

    def test_masked_destination_must_stay_masked(self):
        """記録に生の宛先が混ざっていないことを、**照合側でも見る**。

        書く側だけで守ると、書き方を変えた日に気づけない。
        """
        checks = verify.build_local_checks(record(to_masked="U" + "8" * 32))

        self.assertFalse(verify.all_ok(checks))


class RemoteChecks(unittest.TestCase):
    """**別のエンドポイント**から取った値と突き合わせる。"""

    def test_basic_id_must_match(self):
        checks = verify.build_remote_checks(record(), bot_info(), slack_count=2)

        self.assertTrue(verify.all_ok(checks))

    def test_different_basic_id_fails(self):
        """チャネルを取り違えていたら、ここでしか気づけない。"""
        checks = verify.build_remote_checks(record(), bot_info(basic_id="@other"), slack_count=2)

        self.assertFalse(verify.all_ok(checks))

    def test_slack_count_must_match(self):
        checks = verify.build_remote_checks(record(), bot_info(), slack_count=5)

        self.assertFalse(verify.all_ok(checks))

    def test_unknown_slack_count_is_not_a_pass(self):
        """**数え直せなかったことを「一致」にしない。**（課題8「0 件は不一致」と同じ）"""
        checks = verify.build_remote_checks(record(), bot_info(), slack_count=None)

        self.assertFalse(verify.all_ok(checks))


class RecountFromSlack(unittest.TestCase):
    def test_a_record_without_oldest_is_refused_instead_of_recounting_everything(self):
        """**``oldest`` の無い記録で数え直さない。**

        キーが無いのを「初回（範囲指定なし）」と読むと、チャンネルの全件を
        数えて記録の件数と比べることになる。実際にそうなり、
        「期待 1 / 実際 10」という**照合器のほうが間違っている NG** が出た
        （2026-08-29）。数え直せないなら、数え直せないと言う。
        """
        payload = record()
        del payload["oldest"]
        client = FakeSlack([slack_message("300")])

        self.assertIsNone(verify.recount(client, payload))
        self.assertEqual(client.calls, [])

    def test_recount_uses_the_recorded_oldest(self):
        """**記録した ``oldest`` から数え直す。**

        現在の状態ファイルを使うと、位置は次の実行のために進んでいるので
        再現できない。物差しは**記録の側**から取る。
        """
        client = FakeSlack([slack_message("300"), slack_message("260")])

        verify.recount(client, record())

        self.assertEqual(client.calls[0]["oldest"], "250")

    def test_recount_excludes_the_same_subtypes(self):
        """**読み取りと同じ規則で数える。**

        別の規則で数えると、正しい実装でも件数が食い違う。
        """
        client = FakeSlack(
            [slack_message("300"), slack_message("260", subtype="channel_join")]
        )

        self.assertEqual(verify.recount(client, record()), 1)


class Reporting(unittest.TestCase):
    def test_unverifiable_is_always_reported(self):
        """**合格したときこそ出す。**

        全部 OK の画面に「本文の到達は確認していません」が無いと、
        読んだ人は「全部確かめた」と受け取る。
        """
        lines = verify.format_unverifiable()

        self.assertTrue(lines)
        self.assertTrue(any("本文" in line for line in lines))

    def test_format_checks_shows_both_sides(self):
        """食い違ったとき、**期待と実際の両方**を出す。片方では直せない。"""
        checks = verify.build_remote_checks(record(), bot_info(basic_id="@other"), slack_count=2)

        text = "\n".join(verify.format_checks(checks))

        self.assertIn(BASIC_ID, text)
        self.assertIn("@other", text)


class LoadResults(unittest.TestCase):
    def test_missing_file_is_a_readable_error(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(verify.VerifyError):
                verify.load_results(Path(directory) / "nope.json")

    def test_broken_json_is_a_readable_error(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            path.write_text("{壊れている", encoding="utf-8")

            with self.assertRaises(verify.VerifyError):
                verify.load_results(path)

    def test_roundtrip(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            path.write_text(json.dumps(record()), encoding="utf-8")

            self.assertEqual(verify.load_results(path)["channel"], CHANNEL)


if __name__ == "__main__":
    unittest.main()


class ReplyRecount(unittest.TestCase):
    """返信の件数も**別経路で数え直す**。

    数え直せるのは、記録に ``reply_watch``（どの親をどこから読んだか）を
    残してあるからである。**物差しは記録の側から取る**——いまの状態ファイルは
    次の実行のために先へ進んでいるので、使うと別の答えになる。

    **「返信を読んでいない実行」と「返信が0件だった実行」を混ぜない。**
    前者は照合の対象外で、後者は 0 と一致すべき値である。同じ 0 に畳むと、
    ``--include-replies`` を付け忘れた実行が「返信0件で一致」として通る。
    """

    def test_missing_watch_means_not_measured(self):
        self.assertIsNone(verify.recount_replies(object(), {"channel": "C1"}))

    def test_empty_watch_is_zero_not_unmeasured(self):
        """空の見張りは**測れている**。0 件という答えが出る。"""
        client = FakeSlack([])

        self.assertEqual(
            verify.recount_replies(client, {"channel": "C1", "reply_watch": {}}), 0
        )

    def test_counts_replies_from_the_recorded_positions(self):
        client = FakeSlack([], replies={"100": [{"ts": "101", "text": "返信", "user": "U1",
                                                 "thread_ts": "100"}]})

        count = verify.recount_replies(
            client, {"channel": "C1", "reply_watch": {"100": ""}}
        )

        self.assertEqual(count, 1)

    def test_unmeasured_is_not_reported_as_a_match(self):
        """数え直せなかったことを「一致」にしない（読んだ件数と同じ判断）。"""
        checks = verify.build_reply_checks(
            {"reply_read_count": 3, "reply_watch": {"100": ""}}, None
        )

        self.assertTrue(checks)
        self.assertFalse(all(check.ok for check in checks))

    def test_missing_both_sides_is_not_a_match(self):
        """**両方欠けている記録を「一致」にしない。**

        件数のキーが無い古い記録では期待値も ``None`` になる。数え直しも
        ``None`` なら、素朴に比べると ``None == None`` で**一致してしまう**——
        1件も確かめていないのに緑になる。

        上の test だけでは足りなかった（期待 3 と実際 ``None`` がたまたま
        食い違うので、比較に落としても不一致のままだった）。
        **「数え直せなかった」を一致にする改変が素通りした。**
        """
        checks = verify.build_reply_checks({"reply_watch": {"100": ""}}, None)

        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0].ok)
        self.assertIn("数え直せなかった", str(checks[0].actual))

    def test_no_reply_checks_when_the_run_did_not_read_replies(self):
        """返信を読んでいない実行に、返信の照合項目を作らない。"""
        self.assertEqual(verify.build_reply_checks({}, None), [])
