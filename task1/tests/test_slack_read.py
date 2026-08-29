"""task1/slack_read.py のテスト。

**この層が守るのは「静かに減らないこと」だけ。** 要約も送信もしない。

課題の目的は「重要な情報を LINE で受け取り、**見逃しを防ぐ**」。
見逃しは例外を出さない——件数が減るだけなので、**減ったことに気づく仕掛け**を
実装側に持たせ、それをここで固定する。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import slack_read  # noqa: E402

CHANNEL = "C0BQFU7STLM"


def message(ts, text="本文", user="U1", **extra):
    payload = {"ts": ts, "text": text, "user": user}
    payload.update(extra)
    return payload


class FakeClient:
    """``conversations_history`` だけを持つ最小の相手。

    応答は素の ``dict`` を返す。**実物の ``SlackResponse`` も Mapping** なので、
    実装が ``response.get(...)`` で読む限り、本物と偽物で読み方が変わらない。
    ``.messages`` のような属性アクセスを実装が使い始めたら、
    ここが本物と食い違う——だから属性を生やさない。
    """

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def conversations_history(self, **kwargs):
        self.calls.append(kwargs)
        if not self.pages:
            return {"ok": True, "messages": []}
        return self.pages.pop(0)


def page(messages, *, next_cursor="", has_more=None):
    body = {
        "ok": True,
        "messages": messages,
        "response_metadata": {"next_cursor": next_cursor},
    }
    if has_more is not None:
        body["has_more"] = has_more
    return body


class Ordering(unittest.TestCase):
    def test_messages_are_returned_oldest_first(self):
        """**並び順を信用しない。**

        Slack は新しい順で返す。要約に渡すのは読み下せる順＝古い順なので、
        こちらで ``ts`` で並べ替える。「返ってきた順」に依存すると、
        相手が順序を変えた日に要約の意味が静かに変わる
        （課題10 Discord の「並び順を信用しない」と同じ判断）。
        """
        client = FakeClient([page([message("300"), message("100"), message("200")])])

        got = slack_read.fetch_since(client, channel=CHANNEL)

        self.assertEqual([m.ts for m in got.messages], ["100", "200", "300"])

    def test_timestamps_stay_strings(self):
        """``ts`` は識別子であって時刻ではない。

        ``1503435956.000247`` を float にすると倍精度で表しきれず、
        末尾が変わって**別のメッセージを指す**（課題7の実測）。
        """
        client = FakeClient([page([message("1503435956.000247")])])

        got = slack_read.fetch_since(client, channel=CHANNEL)

        self.assertIsInstance(got.messages[0].ts, str)
        self.assertEqual(got.messages[0].ts, "1503435956.000247")


class Pagination(unittest.TestCase):
    def test_follows_the_cursor_until_it_is_empty(self):
        client = FakeClient(
            [
                page([message("300")], next_cursor="c1"),
                page([message("200")], next_cursor="c2"),
                page([message("100")], next_cursor=""),
            ]
        )

        got = slack_read.fetch_since(client, channel=CHANNEL)

        self.assertEqual([m.ts for m in got.messages], ["100", "200", "300"])
        self.assertEqual(got.pages, 3)
        self.assertEqual(client.calls[1]["cursor"], "c1")
        self.assertEqual(client.calls[2]["cursor"], "c2")

    def test_a_short_page_does_not_stop_pagination(self):
        """**これが公式仕様の罠を殺すテスト。**

        「Fewer than the requested number of items may be returned, **even if
        the end of the conversation history hasn't been reached**」
        （2026-08-29 に公式リファレンスで確認）。

        ``len(messages) < limit`` を「終わり」と読む実装は、ここで静かに
        取りこぼす。**終わりを決めてよいのは ``next_cursor`` だけ。**
        """
        client = FakeClient(
            [
                page([message("300")], next_cursor="c1"),  # limit より遥かに少ない
                page([message("200"), message("100")], next_cursor=""),
            ]
        )

        got = slack_read.fetch_since(client, channel=CHANNEL, page_limit=200)

        self.assertEqual(len(got.messages), 3)

    def test_empty_page_with_a_cursor_still_continues(self):
        """0件のページが返っても、cursor があるなら続きがある。"""
        client = FakeClient(
            [
                page([], next_cursor="c1"),
                page([message("100")], next_cursor=""),
            ]
        )

        got = slack_read.fetch_since(client, channel=CHANNEL)

        self.assertEqual([m.ts for m in got.messages], ["100"])

    def test_repeated_cursor_stops_instead_of_looping_forever(self):
        """同じ cursor が返り続けたら止める。

        相手の不具合や想定外の応答で無限に回り続けると、**課金や
        レート制限を静かに焼く**。止めたことは truncated で外に出す。
        """
        client = FakeClient([page([message("100")], next_cursor="same")] * 5)

        got = slack_read.fetch_since(client, channel=CHANNEL, max_pages=3)

        self.assertTrue(got.truncated)
        self.assertLessEqual(got.pages, 3)

    def test_truncation_is_reported_not_silent(self):
        """**黙って切らない。**

        打ち切った結果を「全部読んだ」と同じ形で返すと、下流の要約は
        欠けた材料を完全な材料として扱う。
        """
        client = FakeClient([page([message(str(i))], next_cursor=f"c{i}") for i in range(9)])

        got = slack_read.fetch_since(client, channel=CHANNEL, max_pages=2)

        self.assertTrue(got.truncated)

    def test_not_truncated_when_the_cursor_runs_out(self):
        client = FakeClient([page([message("100")], next_cursor="")])

        self.assertFalse(slack_read.fetch_since(client, channel=CHANNEL).truncated)


class Subtypes(unittest.TestCase):
    def test_join_and_leave_are_excluded(self):
        """参加・退出は本文ではない。要約の材料にすると中身が薄まる。"""
        client = FakeClient(
            [
                page(
                    [
                        message("300", text="実装の話"),
                        message("200", text="…が参加しました", subtype="channel_join"),
                        message("100", text="…が退出しました", subtype="channel_leave"),
                    ]
                )
            ]
        )

        got = slack_read.fetch_since(client, channel=CHANNEL)

        self.assertEqual([m.ts for m in got.messages], ["300"])

    def test_excluded_counts_are_recorded_by_subtype(self):
        """**捨てたものを数えて残す。**

        件数が合わないときに「読めなかった」のか「捨てた」のかを
        区別できるようにする。数えずに捨てると、後から追えない。
        """
        client = FakeClient(
            [
                page(
                    [
                        message("300", subtype="channel_join"),
                        message("200", subtype="channel_join"),
                        message("100", subtype="channel_leave"),
                    ]
                )
            ]
        )

        got = slack_read.fetch_since(client, channel=CHANNEL)

        self.assertEqual(got.skipped, {"channel_join": 2, "channel_leave": 1})

    def test_unknown_subtypes_are_kept(self):
        """**知らない subtype は残す側に倒す。**

        「subtype があるものは全部捨てる」と書くと、``bot_message`` や
        将来増える種類まで落ちる。この課題の目的は**見逃しを防ぐこと**なので、
        知らないものを黙って捨てる向きは目的そのものに反する。
        """
        client = FakeClient(
            [page([message("300", subtype="bot_message"), message("200", subtype="なにか新種")])]
        )

        got = slack_read.fetch_since(client, channel=CHANNEL)

        self.assertEqual([m.ts for m in got.messages], ["200", "300"])
        self.assertEqual(got.skipped, {})


class Since(unittest.TestCase):
    def test_oldest_is_passed_through_when_given(self):
        client = FakeClient([page([message("300")])])

        slack_read.fetch_since(client, channel=CHANNEL, oldest="200.5")

        self.assertEqual(client.calls[0]["oldest"], "200.5")

    def test_oldest_is_omitted_when_absent(self):
        """初回は範囲を絞らない。空文字を渡すと相手の解釈に委ねることになる。"""
        client = FakeClient([page([message("300")])])

        slack_read.fetch_since(client, channel=CHANNEL)

        self.assertNotIn("oldest", client.calls[0])

    def test_blank_oldest_is_omitted_too(self):
        """**空文字も「指定なし」に寄せる。**

        `is not None` で判定すると空文字が渡り、相手の解釈に委ねることになる。
        None だけを見るテストではこの違いが出ない（2026-08-29・ミューテーションで検出）。
        """
        client = FakeClient([page([message("300")])])

        slack_read.fetch_since(client, channel=CHANNEL, oldest="")

        self.assertNotIn("oldest", client.calls[0])

    def test_inclusive_is_never_sent(self):
        """``inclusive`` は使わない。

        既定（排他）で取りこぼしは起きない——前回の最新 ``ts`` **より後**を
        取るので、その ts のメッセージは既に読んでいる。包含にすると
        **毎回1件重複**して要約にノイズが乗る。取りこぼし対策は別の場所
        （位置の更新を送信成功の後にする）で作る。
        """
        client = FakeClient([page([message("300")])])

        slack_read.fetch_since(client, channel=CHANNEL, oldest="200")

        self.assertNotIn("inclusive", client.calls[0])

    def test_latest_ts_is_reported_for_the_next_run(self):
        client = FakeClient([page([message("300"), message("100")])])

        got = slack_read.fetch_since(client, channel=CHANNEL)

        self.assertEqual(got.latest_ts, "300")

    def test_latest_ts_is_none_when_nothing_was_read(self):
        """**0件のときに位置を進めない。**

        進めると、次回はそこから後だけを読む。0件は「まだ何も無い」であって
        「ここまで読んだ」ではない。
        """
        client = FakeClient([page([])])

        got = slack_read.fetch_since(client, channel=CHANNEL)

        self.assertIsNone(got.latest_ts)

    def test_latest_ts_counts_excluded_messages_too(self):
        """**除外したメッセージも位置には数える。**

        ``channel_join`` しか無かった回で位置を進めないと、次回も同じ
        ``channel_join`` を読み直す。要約には使わないが、**読んだことは事実**。
        """
        client = FakeClient([page([message("300", subtype="channel_join")])])

        got = slack_read.fetch_since(client, channel=CHANNEL)

        self.assertEqual(got.latest_ts, "300")
        self.assertEqual(got.messages, [])


class EmptyIsNotAFailure(unittest.TestCase):
    def test_zero_messages_is_a_normal_result(self):
        """0件は正常値。**例外にしない。**

        「投稿が無かった」ことは伝えるべき情報で、失敗ではない。
        """
        got = slack_read.fetch_since(FakeClient([page([])]), channel=CHANNEL)

        self.assertEqual(got.messages, [])
        self.assertEqual(got.skipped, {})


class FakePermalinkClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat_getPermalink(self, **kwargs):
        self.calls.append(kwargs)
        return self.payload


class Permalink(unittest.TestCase):
    """要約の下に置く「原文への入口」。

    **要約は入力に無いことを言い出す**（2026-08-29 に実機で確認。
    「Drive & Notion」が「Google DriveとNotion」になり、別人の発言が
    1人に畳まれた）。目的は「見逃しを防ぐ」ことであって
    「要約を信じさせる」ことではないので、**原文へ辿れる道を残す**。

    ``chat.getPermalink`` は**スコープを要求しない**（課題7で公式リファレンスに
    当たって確認済み）。だから権限を増やさずに足せる。
    """

    def test_returns_the_permalink(self):
        client = FakePermalinkClient({"ok": True, "permalink": "https://example.slack.com/archives/C1/p1"})

        got = slack_read.fetch_permalink(client, channel=CHANNEL, ts="100")

        self.assertEqual(got, "https://example.slack.com/archives/C1/p1")

    def test_passes_channel_and_ts(self):
        client = FakePermalinkClient({"ok": True, "permalink": "x"})

        slack_read.fetch_permalink(client, channel=CHANNEL, ts="100.5")

        self.assertEqual(client.calls[0]["channel"], CHANNEL)
        self.assertEqual(client.calls[0]["message_ts"], "100.5")

    def test_failure_returns_empty_instead_of_raising(self):
        """**リンクが取れないことで送信を止めない。**

        リンクは「あると良いもの」で、無くても通知の目的は果たせる。
        ただし**静かにしない**——空を返したことは呼ぶ側が画面に出す。
        """
        client = FakePermalinkClient({"ok": False, "error": "message_not_found"})

        self.assertEqual(slack_read.fetch_permalink(client, channel=CHANNEL, ts="100"), "")

    def test_blank_permalink_is_empty(self):
        client = FakePermalinkClient({"ok": True, "permalink": "   "})

        self.assertEqual(slack_read.fetch_permalink(client, channel=CHANNEL, ts="100"), "")

    def test_api_exception_returns_empty(self):
        class Boom:
            def chat_getPermalink(self, **kwargs):
                raise RuntimeError("落ちた")

        self.assertEqual(slack_read.fetch_permalink(Boom(), channel=CHANNEL, ts="100"), "")


class Request(unittest.TestCase):
    def test_channel_is_passed(self):
        client = FakeClient([page([message("100")])])

        slack_read.fetch_since(client, channel=CHANNEL)

        self.assertEqual(client.calls[0]["channel"], CHANNEL)

    def test_page_limit_is_passed(self):
        client = FakeClient([page([message("100")])])

        slack_read.fetch_since(client, channel=CHANNEL, page_limit=50)

        self.assertEqual(client.calls[0]["limit"], 50)


if __name__ == "__main__":
    unittest.main()
