"""common/line_send.py のテスト。

**Section 4-3 の課題9で書いた送信を、共有部品に格上げしたもの。**
課題10 では ``import send_push`` で課題9の実装をそのまま使い、
「提出済みに手を入れず、直れば自動で効く」形にしていた。
リポジトリが分かれるとその import は使えないので、**送信のコアだけ**を
``common/`` に移す。CLI の皮（``parse_args`` / 記録の組み立て / 画面表示）は
使う課題ごとに違うので持ってこない。

テストも課題9から移した。**期待値を新しい実装から作り直さない**——
作り直すと「実装がこうなっているから、こう期待する」になり、
実機で1度通った知識が失われる。
"""

from __future__ import annotations

import unittest

from common import line_send

USER_ID = "U" + "8" * 32
MESSAGE_ID = "627984934547751122"
QUOTE_TOKEN = "QUOTEtoken" + "z" * 40


class FakeResponse:
    def __init__(self, *, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = ""
        self.headers = headers if headers is not None else {}

    def json(self):
        if self._payload is None:
            raise ValueError("応答が JSON ではありません")
        return self._payload


def push_payload(**overrides):
    sent = {"id": MESSAGE_ID, "quoteToken": QUOTE_TOKEN}
    sent.update(overrides)
    return {"sentMessages": [sent]}


class BuildPayload(unittest.TestCase):
    def test_payload_has_the_documented_shape(self):
        payload = line_send.build_payload(to=USER_ID, text="やっほー")
        self.assertEqual(
            payload,
            {"to": USER_ID, "messages": [{"type": "text", "text": "やっほー"}]},
        )

    def test_blank_text_is_rejected_before_the_request(self):
        """空文字・空白だけは手元で止める。

        LINE も 400 を返すが、**手元で止めれば1通も消費しない**。
        無料プランは月200通で、失敗した送信も試行のたびに時間を食う。
        """
        for text in ("", "   ", "\n\t"):
            with self.subTest(text=text):
                with self.assertRaises(line_send.SendError):
                    line_send.build_payload(to=USER_ID, text=text)

    def test_text_is_sent_as_typed_including_surrounding_spaces(self):
        """**本文は strip しない。**

        空白だけを弾くのと、書いた空白を落とすのは別の話。落とすと
        「送った文字列」と「届いた文字列」が最初からずれ、照合の意味が消える。
        """
        payload = line_send.build_payload(to=USER_ID, text="  端に空白  ")
        self.assertEqual(payload["messages"][0]["text"], "  端に空白  ")

    def test_no_length_limit_is_enforced_locally(self):
        """**長さ検査を自前で持たない。**

        確かめていない数字を定数に置くと、LINE 側が変えた日に
        **正しい送信を拒む**側で壊れる。長すぎるときは API のエラーを訳して見せる。
        """
        payload = line_send.build_payload(to=USER_ID, text="あ" * 6000)
        self.assertEqual(len(payload["messages"][0]["text"]), 6000)

    def test_exactly_one_message_object_is_sent(self):
        """1リクエストに5件まで載るが、**1件に固定する**。

        複数件にすると ``totalUsage`` の増分が「送信対象になった人数」であって
        メッセージ件数ではないことと噛み合わず、照合の解釈が難しくなる。
        """
        payload = line_send.build_payload(to=USER_ID, text="x")
        self.assertEqual(len(payload["messages"]), 1)


class ReadSendResult(unittest.TestCase):
    def test_reads_message_id_from_response(self):
        sent = line_send.read_send_result(FakeResponse(payload=push_payload()))
        self.assertEqual(sent.message_id, MESSAGE_ID)

    def test_reads_request_id_from_headers(self):
        """``x-line-request-id`` を残す。問い合わせるときの唯一の手掛かり。"""
        response = FakeResponse(
            payload=push_payload(), headers={"x-line-request-id": "req-1"}
        )
        self.assertEqual(line_send.read_send_result(response).request_id, "req-1")

    def test_missing_request_id_is_empty_not_an_error(self):
        """ヘッダが無くても送信自体は成功している。ここで落とさない。"""
        sent = line_send.read_send_result(FakeResponse(payload=push_payload()))
        self.assertEqual(sent.request_id, "")

    def test_empty_sent_messages_is_a_failure(self):
        """``sentMessages`` が空なら失敗にする。

        **HTTP 200 で空**という形は「エラーにならない失敗」そのもの。
        ID が無ければ記録に残す材料が無く、あとから何も言えない。
        """
        with self.assertRaises(line_send.SendError):
            line_send.read_send_result(FakeResponse(payload={"sentMessages": []}))

    def test_missing_sent_messages_key_is_a_failure(self):
        with self.assertRaises(line_send.SendError):
            line_send.read_send_result(FakeResponse(payload={}))

    def test_blank_message_id_is_a_failure(self):
        with self.assertRaises(line_send.SendError):
            line_send.read_send_result(FakeResponse(payload=push_payload(id="")))

    def test_non_json_response_names_json_in_the_message(self):
        """**「JSON として読めなかった」と名指しできていることまで見る。**

        ここを「SendError が出た」だけにすると、空の辞書に倒す実装に変えても
        「sentMessages がありません」で落ちるので素通りする
        （2026-08-19・課題9のミューテーションで実際に検出された）。
        """
        with self.assertRaises(line_send.SendError) as caught:
            line_send.read_send_result(FakeResponse(payload=None))
        self.assertIn("JSON", str(caught.exception))

    def test_list_body_is_a_failure(self):
        with self.assertRaises(line_send.SendError):
            line_send.read_send_result(FakeResponse(payload=[1, 2]))


class RecordingSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(payload=push_payload())


class Push(unittest.TestCase):
    def test_push_posts_to_the_documented_path(self):
        session = RecordingSession()
        line_send.push(session, {"to": USER_ID, "messages": []})
        self.assertTrue(session.calls[0][0].endswith("/v2/bot/message/push"))

    def test_push_sends_the_payload_as_json(self):
        session = RecordingSession()
        payload = {"to": USER_ID, "messages": [{"type": "text", "text": "x"}]}
        line_send.push(session, payload)
        self.assertEqual(session.calls[0][1]["json"], payload)

    def test_push_sets_a_retry_key(self):
        """``X-Line-Retry-Key`` を必ず付ける。

        通信が切れて再実行したとき、同じキーなら**二重送信にならない**。
        **付け忘れても手元のテストは通ってしまう**ので、ヘッダの有無を固定する。
        """
        session = RecordingSession()
        line_send.push(session, {"to": USER_ID, "messages": []})
        self.assertTrue(session.calls[0][1]["headers"]["X-Line-Retry-Key"])

    def test_push_uses_the_given_retry_key(self):
        session = RecordingSession()
        line_send.push(session, {"to": USER_ID, "messages": []}, retry_key="fixed-key")
        self.assertEqual(
            session.calls[0][1]["headers"]["X-Line-Retry-Key"], "fixed-key"
        )


class MaskDestination(unittest.TestCase):
    def test_destination_is_masked_for_the_record(self):
        """記録に残す宛先は伏せた形にする。**public リポジトリに入る。**

        先頭と末尾だけ残すのは、記録どうしを見比べたときに
        「同じ宛先か」を人が判断できるようにするため。
        """
        masked = line_send.mask_destination(USER_ID)
        self.assertTrue(masked.startswith("U8"))
        self.assertTrue(masked.endswith("88"))
        self.assertNotIn(USER_ID, masked)
        self.assertIn("…", masked)

    def test_mask_keeps_short_values_unreadable_too(self):
        """短い値でも中身を丸ごと出さない。**例外を作ると、そこだけ漏れる。**

        課題9では最初 ``!= "U123"`` とだけ書いていたが弱すぎた。短い値の分岐を
        消すと ``U1…23`` が返り、**元の文字は全部読めるのに ``!= "U123"`` は真**に
        なる（2026-08-19・ミューテーションで検出）。「違う文字列になった」ことと
        「読めなくなった」ことは別。**後者を書く。**
        """
        masked = line_send.mask_destination("U123")
        self.assertFalse(any(char in masked for char in "123"))


class FetchUsage(unittest.TestCase):
    def session_returning(self, payload):
        outer = self

        class Session:
            headers = {}

            def get(self, url, **kwargs):
                outer.assertTrue(url.endswith("/v2/bot/message/quota/consumption"))
                return FakeResponse(payload=payload)

        return Session()

    def test_reads_total_usage(self):
        self.assertEqual(line_send.fetch_usage(self.session_returning({"totalUsage": 7})), 7)

    def test_rejects_missing_field(self):
        """``totalUsage`` が無いのに 0 として続行しない。

        **0 を返すと「送信前は0通だった」と区別が付かず、増分の照合が
        偽の成功に化ける。** 取れなかったことを取れなかったと言う。
        """
        with self.assertRaises(line_send.SendError):
            line_send.fetch_usage(self.session_returning({}))

    def test_rejects_non_integer(self):
        with self.assertRaises(line_send.SendError):
            line_send.fetch_usage(self.session_returning({"totalUsage": "たくさん"}))

    def test_rejects_boolean(self):
        """``True`` は Python では int の仲間。**通数として通してはいけない。**"""
        with self.assertRaises(line_send.SendError):
            line_send.fetch_usage(self.session_returning({"totalUsage": True}))


if __name__ == "__main__":
    unittest.main()
