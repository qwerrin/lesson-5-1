"""LINE への送信。**Section 4-3 の課題9で書いたものを共有部品に格上げした。**

なぜ移したか
------------------------------------------------------------------

課題10（連携済みの API に機能を追加）では ``import send_push`` と書いて、
課題9の実装を**1行も変えずに**使った。提出済みのコードに手を入れずに機能を
足せるうえ、課題9側が直れば自動で効く（反映漏れが構造的に起きない）。

Section 5-1 はリポジトリが分かれるので、その import は使えない。
そこで**送信のコアだけ**を ``common/`` に置き、CLI の皮
（引数解析・記録の組み立て・画面表示）は使う課題ごとに書く。
皮は課題ごとに違うが、この4つはどの課題でも同じだからである。

============================== ================================================
ここに置くもの                  なぜ共有できるか
============================== ================================================
``build_payload``              push の本文の形は LINE が決めている
``read_send_result``           応答から材料を取り出す規則も LINE が決めている
``push``                       再送キーの付け方は課題に依らない
``fetch_usage``                通数の読み方は課題に依らない
``mask_destination``           記録に宛先を残す形は public リポジトリの都合
============================== ================================================

読み返せないことは変わらない
------------------------------------------------------------------

LINE には bot が送ったテキストを読み返す API が無い（課題9で公式 OpenAPI 定義に
当たって確認済み）。だから送信側の仕事に「**あとから照合できる材料を残すこと**」
まで含める。材料は3つで、**3つとも「何を送ったか」は言わない**。

============================== ==============================================
材料                            何を言えるか
============================== ==============================================
``sentMessages[].id``           LINE がこの送信に ID を振った
``totalUsage`` の送信前後        **別のエンドポイント**が通数の増加を認めた
``/v2/bot/info`` の ``basicId``  意図したチャネルを叩いた
============================== ==============================================
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from common import line_auth

PUSH_PATH = "/v2/bot/message/push"
CONSUMPTION_PATH = "/v2/bot/message/quota/consumption"

#: 伏せ字にするときに残す前後の文字数。
_MASK_KEEP = 2


class SendError(Exception):
    """送信まわりの失敗。利用者にそのまま見せられる。"""


@dataclass(frozen=True)
class Sent:
    """送信できたことの証跡。

    ``quote_token`` は引用返信に使える値で照合には要らないため、
    **記録には書かない**（public リポジトリに入るため）。
    """

    message_id: str
    quote_token: str
    request_id: str


# ------------------------------------------------------------------ 送る中身


def build_payload(*, to: str, text: str) -> dict:
    """push のリクエスト本文を組む。

    **本文は strip しない。** 空白だけを弾くのと、書いた空白を落とすのは別の話。
    落とすと「送った文字列」と「届いた文字列」が最初からずれる。

    **長さは検査しない。** 上限の数字を実物で確かめていない。確かめていない数字を
    定数に置くと、LINE 側が変えた日に**正しい送信を拒む**側で壊れる。
    """
    if not (text or "").strip():
        raise SendError(
            "本文が空です。送る文字列を指定してください。\n"
            "（空のまま送ると API が 400 を返しますが、手元で止めれば通数を消費しません）"
        )

    # 1リクエストに5件まで載るが1件に固定する。totalUsage の増分は
    # 「送信対象になった人数」なので、件数を増やすと照合の解釈が難しくなる。
    return {"to": to, "messages": [{"type": "text", "text": text}]}


# ------------------------------------------------------------------ 応答を読む


def read_send_result(response) -> Sent:
    """push の応答から message ID を取り出す。

    **HTTP 200 で ``sentMessages`` が空**という形を失敗にする。
    ID が無ければ記録に残す材料が無く、あとから何も言えない。
    「エラーにならない失敗」を成功として通さない。
    """
    payload = line_auth.payload_of(response)
    if not isinstance(payload, dict):
        raise SendError("push の応答を JSON として読めませんでした。")

    sent_messages = payload.get("sentMessages")
    if not isinstance(sent_messages, list) or not sent_messages:
        raise SendError(
            "push の応答に sentMessages がありません。\n"
            "HTTP は成功していますが、送信された証跡が取れないため失敗として扱います。"
        )

    first = sent_messages[0]
    if not isinstance(first, dict):
        raise SendError("push の応答の sentMessages の形が想定と違います。")

    message_id = str(first.get("id") or "").strip()
    if not message_id:
        raise SendError("push の応答に message ID がありません。")

    return Sent(
        message_id=message_id,
        quote_token=str(first.get("quoteToken") or ""),
        request_id=line_auth.request_id_of(response),
    )


# ------------------------------------------------------------------ 通数


def fetch_usage(session, *, base: str = line_auth.API_BASE, secrets: tuple = ()) -> int:
    """今月の送信通数を読む。

    **取れなかったときに 0 を返さない。** 0 は「まだ1通も送っていない」という
    正当な値なので、失敗を 0 に倒すと増分の照合が偽の成功に化ける。
    """
    response = session.get(base + CONSUMPTION_PATH)
    line_auth.raise_for_line_error(response, *secrets)

    payload = line_auth.payload_of(response)
    if not isinstance(payload, dict) or "totalUsage" not in payload:
        raise SendError(f"{CONSUMPTION_PATH} が totalUsage を返しませんでした。")

    value = payload["totalUsage"]
    # bool は int の仲間なので、素朴な型検査を素通りする。通数として通さない。
    if isinstance(value, bool) or not isinstance(value, int):
        raise SendError(
            f"{CONSUMPTION_PATH} の totalUsage が整数ではありません: {type(value).__name__}"
        )

    return value


# ------------------------------------------------------------------ 送る


def push(
    session,
    payload: dict,
    *,
    base: str = line_auth.API_BASE,
    secrets: tuple = (),
    retry_key: str | None = None,
):
    """push を投げる。

    ``X-Line-Retry-Key`` を付ける。通信が切れて再実行したとき、同じキーなら
    **二重送信にならない**。無料プランは月200通なので、事故った再実行で
    通数を溶かさない意味もある。
    """
    headers = {"X-Line-Retry-Key": retry_key or str(uuid.uuid4())}
    response = session.post(base + PUSH_PATH, json=payload, headers=headers)
    line_auth.raise_for_line_error(response, *secrets)
    return response


# ------------------------------------------------------------------ 記録に残す形


def mask_destination(value: str) -> str:
    """記録に残す宛先を伏せる。

    記録は public リポジトリに入る。宛先IDはチャネルに紐づく識別子で単体では
    他人が使えないが、**残す必要が無いものは残さない**。前後を少し残すのは、
    記録どうしを見比べて「同じ宛先か」を人が判断できるようにするため。

    **短い値に例外を作らない。** 例外を作ると、そこだけ丸ごと出る。
    """
    text = value or ""
    if len(text) <= _MASK_KEEP * 2:
        return "…" * 3
    return f"{text[:_MASK_KEEP]}…{text[-_MASK_KEEP:]}"
