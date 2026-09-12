"""Gemini API の呼び出し。要約に使う LLM をここに閉じ込める。

課題1（Slack → 要約 → LINE）で**唯一新しく増える相手**がこれ。Slack と LINE は
Section 4-3 で作った認証をそのまま使えるが、要約だけは新規になる。

他の認証モジュールとの違い
------------------------------------------------------------------

===================== ================================================
何を使うか             どういう相手か
===================== ================================================
slack_auth（Bot）     **アプリ自身**が動く。インストール時に1回発行、期限なし
line_auth（長期）     アプリ自身が動く。無期限で、チャネルに1本しか無い
gemini_client（キー） **認可する相手がいない**。キー1本がそのまま権限
===================== ================================================

``youtube_auth``（課題6）と同じ「キーだけ」の形だが、**課金が乗っている**点が違う。
YouTube のキーは無料枠の読み取りだったのに対し、こちらは呼ぶたびに金額が動く。
だから「静かに何度も呼ぶ」経路を作らないことが、この課題の安全側になる。

このモジュールに固有の事情が4つある
------------------------------------------------------------------

**1. クライアントを一時オブジェクトにしてはいけない。**

``genai.Client(...).models.generate_content(...)`` と書くと、**リクエストが飛ぶ前に**
``RuntimeError: Cannot send a request, as the client has been closed.`` になる。
参照が消えた時点で内部の httpx が閉じるため。``build_client()`` が返した値を
必ず変数で受けて使い回す。

**2. 一時エラーの判定に本文の文字列を使わない。**

``str(error)`` に ``429`` が含まれるかで判定すると、
``token count 429 exceeds limit``（**恒久エラー**）を投げ直し続ける。
判定に使ってよいのは ``APIError.code``（整数）だけである。

**3. 接続断は ``OSError`` だけでは拾えない。**

``httpx.TransportError`` の継承は ``RequestError → HTTPError → Exception`` で、
**``OSError`` の仲間ではない**（2026-08-29 に実測）。「接続断＝OSError」で
済ませると、転送タイムアウトが恒久エラーに化けて1回で諦める。

**4. 一覧に載ることは、呼べることの証拠にならない。**

2026-08-29 の実測で、``models.list()`` に載っている ``gemini-2.5-flash-lite`` が
404 を返した。本文は「no longer available to new users」で後継を名指ししていた。
**モデル名を一覧から選ぶ実装にしない**——既定を1つ決め、変えるときは呼んで確かめる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

#: キーを渡す環境変数名。
API_KEY_ENV = "GEMINI_API_KEY"

#: 既定のモデル。**2026-08-29 に実際に呼んで通ることを確認した値。**
#: 一覧から動的に選ばない（上の事情4）。変えるときは呼んで確かめてから変える。
DEFAULT_MODEL = "gemini-3.5-flash-lite"

#: 伏せたことが分かる印。空文字にすると「元から無かった」と区別がつかない。
REDACTED = "***"

#: 投げ直してよい HTTP ステータス。**本文ではなくこれで判定する**（上の事情2）。
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

#: 1リクエストに直接載せられる大きさの上限（課題3・音声）。
#: 公式の Audio understanding に「総リクエスト 20MB 以下」とある。
#: 超えるぶんは Files API に回すが、**この課題では実装しない**——
#: 使わない経路を書くと、動かしたことのないコードが残る。
INLINE_LIMIT_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class Reply:
    """生成の答え。**本文だけでは足りない。**

    ``finish_reason`` を持ち回るのは、**打ち切られても本文は返る**ためである。
    テキストだけ見ていると、途中で切れた文字起こしが「短い会議」として通る
    ——`task3/DESIGN.md` の 5-G。

    トークン数も持つ。課題3 では ``count_tokens`` と ``usage_metadata`` が
    101 ズレる件が未決（DESIGN 3.5-#6）なので、**実際に課金された側**を
    呼び出しごとに残せるようにしておく。
    """

    text: str
    finish_reason: str | None
    prompt_tokens: int | None
    output_tokens: int | None


class GeminiError(Exception):
    """このモジュールが出す失敗の共通の親。利用者にそのまま見せられる。"""


class AuthError(GeminiError):
    """資格情報まわりの失敗。"""


class ApiError(GeminiError):
    """API が失敗を返した、または答えが使えなかった。"""


# ------------------------------------------------------------------ 資格情報


def read_api_key(env: Mapping[str, str]) -> str:
    """API キーを読む。

    空文字・空白だけは「未設定」と同じ扱いにする。``GEMINI_API_KEY=`` と
    書いただけでもキーとしては存在するので、有無だけ見ると素通りして、
    後段の API が 400 を返し、原因がここだと分からなくなる。

    **形は検査しない。** 現行のキーは ``AQ.`` で始まる（2026-08-29 実測）が、
    それを仕様として明記した文書は確認していない。確かめていない形を検査に
    使うと、提供側が形を変えた日に**正しい値を拒む**側で壊れる
    （``line_auth.read_user_id`` で長さを検査しないと決めたのと同じ）。

    **値そのものは絶対にメッセージへ載せない**（壊れた値であっても）。
    打ち間違いなら本物がそのまま入っているし、この文言は公開する
    スクリーンショットに写る。
    """
    value = (env.get(API_KEY_ENV) or "").strip()

    if not value:
        raise AuthError(
            f"Gemini の API キーが設定されていません: {API_KEY_ENV}\n"
            "Google AI Studio (https://aistudio.google.com/apikey) で発行し、"
            "環境変数か .env に設定してください。"
        )

    return value


def redact(text: str, *secrets: str | None) -> str:
    """文字列から資格情報を伏せる。

    空や None の秘密は素通りさせる。``str.replace("", x)`` は**全部の文字の
    間に x を挿し込む**ので、素通りさせないと文章が壊れる。
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def build_client(api_key: str, *, factory: Callable = genai.Client):
    """クライアントを組む。**返り値は必ず変数で受けて使い回す**（上の事情1）。"""
    value = (api_key or "").strip()
    if not value:
        raise AuthError(f"API キーが空です。{API_KEY_ENV} を設定してください")
    return factory(api_key=value)


# ------------------------------------------------------------------ 呼び出しの設定


def build_config() -> types.GenerateContentConfig:
    """生成の設定を組む。

    **自動関数呼び出し（AFC）を切る。** ツールを1つも渡していないのに、
    SDK が stderr へ「Chat.send_message を使え」という勧告を出す
    （2026-08-29 実測）。要約にツールは使わないので、意味の無い行を
    実行画面に残さない。**実行画面は記事のスクリーンショットになる。**
    """
    return types.GenerateContentConfig(
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
    )


# ------------------------------------------------------------------ エラーの扱い


def is_transient(error: BaseException) -> bool:
    """投げ直してよい失敗かを答える。

    判定に使うのは **``code``（整数）と例外の型だけ**。本文の文字列は見ない
    （上の事情2）。``httpx.TransportError`` は ``OSError`` の仲間ではないので
    別に並べる（上の事情3）。
    """
    if isinstance(error, genai_errors.APIError):
        return error.code in TRANSIENT_STATUS
    return isinstance(error, (OSError, httpx.TransportError))


def translate_error(error: BaseException, api_key: str | None = None) -> ApiError:
    """SDK の例外を、利用者に見せられる文言に置き換える。

    **エラーコードは必ず出す。** 日本語の説明だけにすると公式資料を引けない。
    本文もそのまま載せる——404 のときに SDK が後継モデルを名指しすることがあり
    （2026-08-29 実測）、**こちらで要約するとその情報が消える**。

    ただし必ず ``redact()`` を通す。確認用に書いた短いコードほど素の例外を
    画面に出すので（課題6で実際に鍵を出した）、本番の経路の側で伏せる。
    """
    if isinstance(error, genai_errors.APIError):
        head = f"Gemini API がエラーを返しました: HTTP {error.code}"
    else:
        head = f"Gemini API の呼び出しに失敗しました: {type(error).__name__}"

    message = f"{head}\n詳細: {error}"

    if is_transient(error):
        message += "\n（一時的な失敗です。時間をおいて実行し直してください）"

    return ApiError(redact(message, api_key))


# ------------------------------------------------------------------ 生成


def generate(
    client,
    *,
    prompt: str,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
) -> str:
    """本文を生成して返す。

    **入力が空なら呼ばない。** 空のまま投げても相手は何かを返すが、それは
    材料の無いところから作った文章＝発明である。呼ぶ前に止めれば、
    課金も発生しない。

    **空の答えを「できた」にしない。** 安全フィルタや打ち切りで本文が空に
    なることがある。空文字を返すと、呼ぶ側は「要約が空だった」ではなく
    「空という要約」を送ってしまう（課題6で踏んだ「空が正常値の欄は
    バグが静かな側に倒れる」と同じ形）。
    """
    if not (prompt or "").strip():
        raise ValueError("prompt が空です。要約する材料がありません")

    try:
        response = client.models.generate_content(
            model=model, contents=prompt, config=build_config()
        )
    except Exception as error:  # noqa: BLE001 - 訳して投げ直す
        raise translate_error(error, api_key) from error

    return require_text(response)


def generate_with_audio(
    client,
    *,
    prompt: str,
    audio_bytes: bytes,
    mime_type: str,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    limit_bytes: int = INLINE_LIMIT_BYTES,
) -> Reply:
    """音声を添えて生成し、**本文だけでなく打ち切りの有無も返す**。

    ``generate()`` と分けてあるのは、返す型が違うからである。既存の呼び手
    （課題1の要約）は本文しか要らず、**そちらは1文字も変えない**。

    **上限を超えたら送らない。** 20MB は inline data の制限で、
    超えたぶんは Files API に回す必要がある。黙って送ると相手が拒否するが、
    *その拒否は課金や再試行と混ざって、原因が音声の大きさだと分かりにくい*。

    **空の答えを「できた」にしない**のは ``generate()`` と同じ。
    ただし**打ち切りは失敗にしない**——途中まででも文字起こしは高いので、
    捨てるかどうかは呼び手に決めさせる（``finish_reason`` を見て判断する）。
    """
    if not (prompt or "").strip():
        raise ValueError("prompt が空です")
    if not audio_bytes:
        raise ValueError("音声が空です")
    if len(audio_bytes) > limit_bytes:
        raise ValueError(
            "音声が {:,} バイトで、1リクエストの上限 {:,} バイトを超えています。"
            "Files API に回すか、短く区切ってください".format(len(audio_bytes), limit_bytes)
        )

    part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
    try:
        response = client.models.generate_content(
            model=model, contents=[prompt, part], config=build_config()
        )
    except Exception as error:  # noqa: BLE001 - 訳して投げ直す
        raise translate_error(error, api_key) from error

    return Reply(
        text=require_text(response),
        finish_reason=_finish_reason_of(response),
        prompt_tokens=_usage_of(response, "prompt_token_count"),
        output_tokens=_usage_of(response, "candidates_token_count"),
    )


def require_text(response: Any) -> str:
    """本文を取り出し、**空を「できた」にしない**。

    ``generate()`` と ``generate_with_audio()`` の両方が通る。同じ6行を
    2箇所に置いていたのを1つに寄せた（2026-09-12）——**重複した検査は、
    片方だけ壊しても気づけない**。ミューテーションで1箇所ずつ壊す作りなので、
    同じコードが2箇所にあると「置換先が2件」で検査そのものが素通りする。

    空になるのは安全フィルタか打ち切り。空文字を返すと、呼ぶ側は
    「本文が空だった」ではなく「空という本文」を先へ流してしまう。
    """
    text = _text_of(response)
    if not text:
        raise ApiError(
            "Gemini が本文を返しませんでした。"
            "安全フィルタで止まったか、出力が打ち切られた可能性があります。"
        )
    return text


def _text_of(response: Any) -> str:
    """応答から本文を取り出す。None も空白だけも「無し」に寄せる。"""
    return (getattr(response, "text", None) or "").strip()


def _finish_reason_of(response: Any) -> str | None:
    """打ち切りの理由を文字列で取り出す。

    **取れなくても落とさない。** ここで例外にすると、本文は返っているのに
    全体が失敗になる。取れなかったことは ``None`` として上へ伝え、
    *「STOP だった」と「見られなかった」を混同させない*。
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    reason = getattr(candidates[0], "finish_reason", None)
    if reason is None:
        return None
    return getattr(reason, "name", None) or str(reason)


def _usage_of(response: Any, field: str) -> int | None:
    usage = getattr(response, "usage_metadata", None)
    value = getattr(usage, field, None) if usage is not None else None
    return int(value) if isinstance(value, int) else None
