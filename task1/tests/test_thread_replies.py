"""task1/slack_read.py の「スレッドの返信を読む」層のテスト。

なぜこの層が要るのか（2026-09-04 に実測して確定）
------------------------------------------------------------------

``conversations.history`` は**スレッドの返信を1件も返さない**。
公式リファレンスはこれを明言しておらず、「返信は ``conversations.replies``
を使え」と書いてあるだけである。だから実測した。

============================================ ==========
測ったこと                                     結果
============================================ ==========
Slack に投稿した件数（親1・返信3）                 4 件
``conversations.history`` の件数の増分            **+1 件**
``conversations.replies`` が返した件数           4 件（先頭は親）
返信のうち ``history`` にも出た件数                **0 件**
============================================ ==========

**返信は例外を出さずに落ちる。** 件数が減るだけなので、受け取る側からは
「議論が無かった」と見分けが付かない。これは課題の目的語（見逃し）そのものである。

この層が守ること
------------------------------------------------------------------

* **親を返さない**——``conversations.replies`` は親を先頭に含めて返す
  （2026-09-04 実測）。そのまま足すとチャンネル本文と重複する
* **前回より後の返信だけ**を返す。位置は親ごとに持つ
* **終わりを決めてよいのは ``next_cursor`` が空になったときだけ**。
  ``limit`` は best-effort（非 Marketplace アプリでは最大 15 に下がる）
* **1本のスレッドが失敗しても、他のスレッドを止めない。**
  ただし**静かにしない**——失敗した親を数えて返す
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import slack_read  # noqa: E402

CHANNEL = "C0BQFU7STLM"
PARENT = "1788495546.216329"


def reply(ts, text="返信", user="U1", **extra):
    payload = {"ts": ts, "text": text, "user": user, "thread_ts": PARENT}
    payload.update(extra)
    return payload


def parent_message(ts=PARENT, text="親", **extra):
    payload = {"ts": ts, "text": text, "user": "U1", "thread_ts": ts}
    payload.update(extra)
    return payload


def page(messages, *, next_cursor=""):
    return {
        "ok": True,
        "messages": messages,
        "response_metadata": {"next_cursor": next_cursor},
    }


class FakeClient:
    """``conversations_replies`` だけを持つ最小の相手。

    応答は素の ``dict``。実物の ``SlackResponse`` も Mapping なので、
    実装が ``response.get(...)`` で読む限り本物と経路が変わらない。
    """

    def __init__(self, pages_by_parent=None, *, raises=()):
        self.pages_by_parent = {k: list(v) for k, v in (pages_by_parent or {}).items()}
        self.raises = set(raises)
        self.calls = []

    def conversations_replies(self, **kwargs):
        self.calls.append(kwargs)
        ts = kwargs.get("ts")
        if ts in self.raises:
            raise RuntimeError("boom")
        pages = self.pages_by_parent.get(ts)
        if not pages:
            return page([parent_message(ts)])
        return pages.pop(0)


class ParentIsNotAReply(unittest.TestCase):
    def test_parent_is_excluded(self):
        """**親を足さない。**

        ``conversations.replies`` は親を先頭に含めて返す（2026-09-04 実測）。
        足すとチャンネル本文と重複し、要約に同じ発言が2回混ざる。
        """
        client = FakeClient({PARENT: [page([parent_message(), reply("2"), reply("3")])]})

        result = slack_read.fetch_replies(client, channel=CHANNEL, watch={PARENT: ""})

        self.assertEqual([m.ts for m in result.messages], ["2", "3"])

    def test_parent_only_thread_returns_nothing(self):
        """返信がまだ無い親は0件。**進める位置も作らない。**"""
        client = FakeClient({PARENT: [page([parent_message()])]})

        result = slack_read.fetch_replies(client, channel=CHANNEL, watch={PARENT: ""})

        self.assertEqual(result.messages, [])
        self.assertNotIn(PARENT, result.latest_by_parent)


class Position(unittest.TestCase):
    def test_only_replies_after_the_remembered_one(self):
        """前回の最後の返信より後だけを返す。**排他**（``oldest`` と同じ向き）。"""
        client = FakeClient(
            {PARENT: [page([parent_message(), reply("2"), reply("3"), reply("4")])]}
        )

        result = slack_read.fetch_replies(client, channel=CHANNEL, watch={PARENT: "3"})

        self.assertEqual([m.ts for m in result.messages], ["4"])

    def test_latest_by_parent_is_the_last_reply(self):
        client = FakeClient({PARENT: [page([parent_message(), reply("2"), reply("9")])]})

        result = slack_read.fetch_replies(client, channel=CHANNEL, watch={PARENT: ""})

        self.assertEqual(result.latest_by_parent, {PARENT: "9"})

    def test_replies_are_sorted_oldest_first(self):
        """**並び順を信用しない。** 相手が今そう返すというだけ。"""
        client = FakeClient(
            {PARENT: [page([reply("30"), parent_message(), reply("10"), reply("20")])]}
        )

        result = slack_read.fetch_replies(client, channel=CHANNEL, watch={PARENT: ""})

        self.assertEqual([m.ts for m in result.messages], ["10", "20", "30"])


class Paging(unittest.TestCase):
    def test_follows_next_cursor(self):
        client = FakeClient(
            {
                PARENT: [
                    page([parent_message(), reply("2")], next_cursor="c1"),
                    page([reply("3")]),
                ]
            }
        )

        result = slack_read.fetch_replies(client, channel=CHANNEL, watch={PARENT: ""})

        self.assertEqual([m.ts for m in result.messages], ["2", "3"])
        self.assertEqual(len(client.calls), 2)

    def test_count_does_not_end_the_walk(self):
        """**件数で終わりを決めない。**

        ``limit`` は best-effort で、非 Marketplace アプリでは最大値も既定値も
        15 に下がる（公式リファレンス）。``len(messages) < limit`` を
        「終わりまで読んだ」と読む実装は、相手が少なく返した回だけ静かに落とす。
        """
        client = FakeClient(
            {
                PARENT: [
                    page([parent_message(), reply("2")], next_cursor="c1"),
                    page([reply("3")]),
                ]
            }
        )

        result = slack_read.fetch_replies(
            client, channel=CHANNEL, watch={PARENT: ""}, page_limit=200
        )

        self.assertEqual(len(result.messages), 2)
        self.assertFalse(result.truncated)

    def test_repeating_cursor_stops_and_says_so(self):
        """同じ cursor が返り続ける形。回り続けるとレート制限を静かに焼く。"""
        client = FakeClient(
            {
                PARENT: [
                    page([parent_message(), reply("2")], next_cursor="same"),
                    page([reply("3")], next_cursor="same"),
                ]
            }
        )

        result = slack_read.fetch_replies(client, channel=CHANNEL, watch={PARENT: ""})

        self.assertTrue(result.truncated)

    def test_max_pages_stops_and_says_so(self):
        client = FakeClient(
            {
                PARENT: [
                    page([reply("2")], next_cursor="c1"),
                    page([reply("3")], next_cursor="c2"),
                    page([reply("4")], next_cursor="c3"),
                ]
            }
        )

        result = slack_read.fetch_replies(
            client, channel=CHANNEL, watch={PARENT: ""}, max_pages=2
        )

        self.assertTrue(result.truncated)


class Subtypes(unittest.TestCase):
    def test_excluded_subtypes_are_counted_not_silently_dropped(self):
        """捨てたものは種類別に数える。「読めなかった」と区別できるように。"""
        client = FakeClient(
            {
                PARENT: [
                    page(
                        [
                            parent_message(),
                            reply("2", subtype="channel_join"),
                            reply("3"),
                        ]
                    )
                ]
            }
        )

        result = slack_read.fetch_replies(client, channel=CHANNEL, watch={PARENT: ""})

        self.assertEqual([m.ts for m in result.messages], ["3"])
        self.assertEqual(result.skipped, {"channel_join": 1})

    def test_position_advances_past_excluded_replies(self):
        """除外したものでも**読んだことは事実**。位置は進める。

        ``channel_join`` しか無かった回に位置を止めると、次回も同じものを読む。
        """
        client = FakeClient(
            {PARENT: [page([parent_message(), reply("5", subtype="channel_join")])]}
        )

        result = slack_read.fetch_replies(client, channel=CHANNEL, watch={PARENT: ""})

        self.assertEqual(result.messages, [])
        self.assertEqual(result.latest_by_parent, {PARENT: "5"})


class Failures(unittest.TestCase):
    def test_one_bad_thread_does_not_stop_the_others(self):
        other = "1788495600.000100"
        client = FakeClient(
            {other: [page([parent_message(other), reply("7")])]},
            raises=(PARENT,),
        )

        result = slack_read.fetch_replies(
            client, channel=CHANNEL, watch={PARENT: "", other: ""}
        )

        self.assertEqual([m.ts for m in result.messages], ["7"])

    def test_failed_parents_are_reported(self):
        """**静かにしない。** 失敗した親は呼ぶ側が画面に出す。"""
        client = FakeClient({}, raises=(PARENT,))

        result = slack_read.fetch_replies(client, channel=CHANNEL, watch={PARENT: ""})

        self.assertEqual(result.failed, (PARENT,))

    def test_failed_parent_does_not_advance(self):
        """取れなかった親の位置を進めない。進めると二度と読まない。"""
        client = FakeClient({}, raises=(PARENT,))

        result = slack_read.fetch_replies(client, channel=CHANNEL, watch={PARENT: "3"})

        self.assertNotIn(PARENT, result.latest_by_parent)


class Marking(unittest.TestCase):
    def test_replies_carry_their_parent(self):
        """返信であることを落とさない。要約の材料で親と並べるときに要る。"""
        client = FakeClient({PARENT: [page([parent_message(), reply("2")])]})

        result = slack_read.fetch_replies(client, channel=CHANNEL, watch={PARENT: ""})

        self.assertEqual(result.messages[0].thread_ts, PARENT)


class NoWork(unittest.TestCase):
    def test_empty_watch_calls_nothing(self):
        """見張る親が無いなら**1回も呼ばない**。レート制限を無駄に使わない。"""
        client = FakeClient({})

        result = slack_read.fetch_replies(client, channel=CHANNEL, watch={})

        self.assertEqual(client.calls, [])
        self.assertEqual(result.messages, [])


class BrokenTimestamps(unittest.TestCase):
    """``ts`` が読めない返信は**残す側**に倒す。

    ``subtype`` と同じ判断である。「知らないものを黙って捨てる」向きは
    課題の目的（見逃しを防ぐ）に真っ向から反する。読めない値を
    「前回より前」に丸めると、**その返信は二度と読まれない**。
    """

    def test_unparsable_ts_is_kept(self):
        client = FakeClient({PARENT: [page([parent_message(), reply("これは ts ではない")])]})

        result = slack_read.fetch_replies(client, channel=CHANNEL, watch={PARENT: "3"})

        self.assertEqual([m.ts for m in result.messages], ["これは ts ではない"])

    def test_ts_is_compared_as_a_number_not_as_text(self):
        """桁数が違うと文字列比較は逆転する。``"9" > "10"`` は真になる。

        秒の桁が増える日（あるいは相手が 0 埋めを変えた日）に、
        **境界の1件が静かに落ちる**。数として比べる。
        """
        client = FakeClient({PARENT: [page([parent_message(), reply("10.000001")])]})

        result = slack_read.fetch_replies(
            client, channel=CHANNEL, watch={PARENT: "9.000001"}
        )

        self.assertEqual([m.ts for m in result.messages], ["10.000001"])


class Channel(unittest.TestCase):
    def test_channel_is_passed_through(self):
        client = FakeClient({PARENT: [page([parent_message(), reply("2")])]})

        slack_read.fetch_replies(client, channel=CHANNEL, watch={PARENT: ""})

        self.assertEqual(client.calls[0]["channel"], CHANNEL)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
