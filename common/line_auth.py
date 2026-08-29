"""LINE Messaging API の認証。課題をまたいで使えるように common/ に置く。

common/ の他の5つとは別モジュールにしてある。6つとも「認証」だが手順が違う。

===================== ================================================
何を使うか             どういう相手か
===================== ================================================
google_auth（OAuth）  **本人のデータ**を触る。同意画面 → token.json → リフレッシュ
zoom_auth（S2S）      アカウントの権限で動く。同意画面なし・毎回取り直し
youtube_auth（キー）  **公開データ**を読むだけ。認可する相手がいない
slack_auth（Bot）     **アプリ自身**が動く。インストール時に1回発行、期限なし
discord_auth（Bot）   アプリ自身が動く。**加えて「認証しない経路」が併存する**
line_auth（長期）     アプリ自身が動く。**無期限で、チャネルに1本しか無い**
===================== ================================================

使う側::

    from pathlib import Path
    from common import env_file, line_auth

    ROOT = Path(__file__).resolve().parents[1]
    env = env_file.load(ROOT / env_file.ENV_FILENAME)

    token = line_auth.read_channel_access_token(env)
    session = line_auth.build_session(token)
    info = line_auth.fetch_bot_info(session, secrets=(token,))

LINE に固有の事情が3つある。
------------------------------------------------------------------

**1. 送ったメッセージを読み返す API が無い。**

課題1〜8は全部「送信 → 別経路で読み返して照合」で締めてきたが、LINE には
それに当たるものが無い。``GET /v2/bot/message/{messageId}/content`` は
**ユーザーが送った**画像・動画・音声専用で、bot が送ったテキストは取れない
（2026-08-18 に公式 OpenAPI 定義で確認）。

そこで物差しを ``GET /v2/bot/info`` に置く。**投稿の応答だけを見て「送れた」と
判断すると、同じ応答の中で値を比べるトートロジーになる**（課題4・6・7・8と
同じ形）。別のエンドポイントから取った ``userId`` / ``basicId`` と突き合わせて
初めて閉じる。

なお ``POST /v2/bot/message/broadcast`` の応答は**空オブジェクト**なので、
この経路を選ぶと照合の手段そのものが消える。**push を使う**理由がこれ
（課題8で webhook の 204 を退けたのと同じ判断）。

**2. 紛らわしい識別子が4つある。**

======================== =================================================
名前                      どこにあるか
======================== =================================================
チャネルアクセストークン   「Messaging API設定」タブの**いちばん下**
チャネルシークレット       「チャネル基本設定」タブ（**別物**）
あなたのユーザーID         「チャネル基本設定」タブ（``U`` で始まる）
ボットのベーシックID       「Messaging API設定」タブ（``@`` で始まる）
======================== =================================================

**取り違えても、形の上では「設定されている」ように見える。** 読む側で形を見て、
名指しで違いを言う。課題8の「Bot Token と Webhook URL の取り違え」と同じ対処だが、
LINE のほうが候補が多い。

**3. 応答モードが API から取れる。**

``chatMode`` は ``chat`` か ``bot`` を返す。画面の表示ではなく **API の答え**なので、
「画面ではこうなっているはず」を根拠にしないで済む（課題8で「画面の見え方と、
投稿した主体は別」を踏んだ）。ただし**値を列挙して検査はしない**。LINE 側が
値を増やした日に、動いていたものが止まるため。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

import requests

CHANNEL_ACCESS_TOKEN_ENV = "LINE_CHANNEL_ACCESS_TOKEN"
USER_ID_ENV = "LINE_USER_ID"

# ホストは固定する。組み立てで作ると、いつか別のホストへ資格情報を送る形になる。
API_BASE = "https://api.line.me"

# 何が叩いているかを相手側に残す。既定の requests のままだと分からない。
USER_AGENT = "lesson-4-3-2 (+https://github.com/qwerrin/lesson-4-3-2, 1.0)"

# 伏せたことが分かる印。空文字にすると「元から無かった」と区別がつかない。
REDACTED = "***"

# push の宛先として受け付ける先頭文字。U=ユーザー / C=グループ / R=複数人トーク。
# **ユーザーIDだけに絞らない。** 絞るとグループへ送りたくなったときに形で止まる。
_DESTINATION_PREFIXES = ("U", "C", "R")

_HEX = frozenset("0123456789abcdefABCDEF")

# チャネルシークレットとして疑う形。**長さを仕様として断定はしない**ので、
# 一致したら「では？」と疑うだけにとどめる（弾く理由は形ではなく、
# トークンとして短すぎてどのみち 401 になるから）。
_CHANNEL_SECRET_LENGTH = 32


class LineError(Exception):
    """このモジュールが出す失敗の共通の親。利用者にそのまま見せられる。"""


class AuthError(LineError):
    """資格情報まわりの失敗。"""


class ApiError(LineError):
    """API が 2xx 以外を返した。"""


@dataclass(frozen=True)
class BotInfo:
    """``GET /v2/bot/info`` が答えた「このトークンは誰か」。

    ``user_id`` と ``basic_id`` が照合の物差しになる。前者は bot 自身の ID、
    後者は画面に出ている ``@`` 始まりの ID で、**意図したチャネルを叩いているか**を
    確かめられる。チャネルを2つ作って取り違えたときは、ここでしか気づけない。
    """

    user_id: str
    basic_id: str
    display_name: str
    chat_mode: str
    mark_as_read_mode: str


# ------------------------------------------------------------------ 資格情報を読む


def read_channel_access_token(env: Mapping[str, str]) -> str:
    """チャネルアクセストークン（長期）を読む。

    空文字・空白だけは「未設定」と同じ扱いにする。``LINE_CHANNEL_ACCESS_TOKEN=``
    と書いただけでもキーとしては存在するので、有無だけ見ると素通りして、
    後段の API が 401 を返し、原因がここだと分からなくなる。

    **値そのものは絶対にメッセージへ載せない**（壊れた値であっても）。
    打ち間違いなら本物がそのまま入っている。この文言は公開する
    スクリーンショットに写る。
    """
    value = (env.get(CHANNEL_ACCESS_TOKEN_ENV) or "").strip()

    if not value:
        raise AuthError(
            f"チャネルアクセストークンが設定されていません: {CHANNEL_ACCESS_TOKEN_ENV}\n"
            "LINE Developers Console → 対象のチャネル → 「Messaging API設定」タブの"
            "**いちばん下**にある「チャネルアクセストークン（長期）」の［発行］で"
            "取得し、.env に設定してください。\n"
            "（「チャネル基本設定」タブにあるのはチャネルシークレットで、別物です）"
        )

    if value.lower().startswith("bearer "):
        raise AuthError(
            f"{CHANNEL_ACCESS_TOKEN_ENV} に Authorization ヘッダの形で入っています。\n"
            "先頭の「Bearer 」は送信時にこちらで付けます。トークンだけを設定してください。"
        )

    if len(value) == _CHANNEL_SECRET_LENGTH and all(char in _HEX for char in value):
        raise AuthError(
            f"{CHANNEL_ACCESS_TOKEN_ENV} にチャネルシークレットが入っていませんか。\n"
            "チャネルシークレットは「チャネル基本設定」タブ、"
            "チャネルアクセストークンは「Messaging API設定」タブのいちばん下にあります。\n"
            "API のリクエストに載せるのは後者です。"
        )

    return value


def read_user_id(env: Mapping[str, str]) -> str:
    """push の宛先 ID を読む。

    **この値は秘密ではないので伏せない。** トークンと違い「どこへ送るか」であって
    権限ではない。伏せると、宛先を間違えたときに何を直せばよいか分からなくなる。

    **長さは検査しない。** 実物は ``U`` ＋ 32桁だが、それを仕様として明記した
    ドキュメントを確認していない。確かめていない数字を定数に置くと、
    LINE 側が形を変えた日に**正しい値を拒む**側で壊れる（課題8で
    「``content`` の最大長を自前で持たない」と決めたのと同じ）。
    """
    value = (env.get(USER_ID_ENV) or "").strip()

    if not value:
        raise AuthError(
            f"送信先のユーザーIDが設定されていません: {USER_ID_ENV}\n"
            "LINE Developers Console → 対象のチャネル → 「チャネル基本設定」タブの"
            "「あなたのユーザーID」を .env に設定してください。"
        )

    if value.startswith("@"):
        raise AuthError(
            f"{USER_ID_ENV} にボットのベーシックID（{value}）が入っています。\n"
            "必要なのは「チャネル基本設定」タブの「あなたのユーザーID」（U で始まる値）です。\n"
            "ベーシックIDは友だち追加用の ID で、push の宛先には使えません。"
        )

    if any(char.isspace() for char in value):
        raise AuthError(
            f"{USER_ID_ENV} に空白が含まれています: {value!r}\n"
            "コピーする範囲を確認してください。"
        )

    if not value.startswith(_DESTINATION_PREFIXES):
        raise AuthError(
            f"{USER_ID_ENV} が送信先IDの形ではありません: {value}\n"
            "ユーザーIDは U、グループIDは C、複数人トークIDは R で始まります。"
        )

    return value


# ------------------------------------------------------------------ 伏せ字


def redact(text: str, *secrets: str | None) -> str:
    """文字列から資格情報を伏せる。

    空や None の秘密は素通りさせる。``str.replace("", x)`` は**全部の文字の
    間に x を挿し込む**ので、素通りさせないと文章が壊れる。
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


# ------------------------------------------------------------------ セッション


def build_session(token: str, *, factory: Callable = requests.Session):
    """チャネルアクセストークンを載せた HTTP セッションを組む。

    トークンは ``Authorization`` ヘッダで送る。**URL には載らない**ので、
    課題6（YouTube の API キーが URI に載って例外で漏れる）と同じ漏れ方は
    しない。ただし *その漏れ方が* 起きないだけなので、表に出す文字列は
    redact() を通す方針を変えない。
    """
    value = (token or "").strip()
    if not value:
        # 空のまま組むと、実行時に 401 が返って原因がここだと分からなくなる。
        raise AuthError(
            f"チャネルアクセストークンが空です。{CHANNEL_ACCESS_TOKEN_ENV} を設定してください"
        )

    session = factory()
    session.headers.update(
        {"Authorization": f"Bearer {value}", "User-Agent": USER_AGENT}
    )
    return session


# ------------------------------------------------------------------ エラーの訳し分け


def payload_of(response) -> Any:
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - 本文が JSON でないことは実際に起きる
        return None


def request_id_of(response) -> str:
    """``x-line-request-id`` を取り出す。

    **HTTP ヘッダ名は大文字小文字を区別しない。** requests の Response.headers は
    区別しない辞書だが、テストの偽物や別のクライアントは素の dict のことがある。
    ここで畳んでおかないと「本物では取れるがテストでは取れない」が起きる。
    """
    headers = getattr(response, "headers", None) or {}
    for name, value in headers.items():
        if str(name).lower() == "x-line-request-id":
            return str(value)
    return ""


def raise_for_line_error(response, *secrets: str | None) -> None:
    """2xx でなければ、利用者に見せられる ApiError にして投げる。

    LINE のエラー本文は ``{"message": ..., "details": [{"property", "message"}]}``。
    **``details`` があるときは候補を並べない。** 相手が壊れている場所を
    ``/messages/0/text`` の形で名指ししているので、こちらの推測を足すと
    その情報が埋もれる（課題4・Meet の教訓）。

    本文が JSON でないことは実際に起きる。**中身をそのまま流さない**——
    HTML が画面いっぱいに出ても利用者には読めない。
    """
    status = response.status_code
    if 200 <= status < 300:
        return

    payload = payload_of(response)
    body = payload if isinstance(payload, dict) else {}

    message = f"LINE API がエラーを返しました: HTTP {status}"

    detail = body.get("message")
    if detail:
        message += f"\n{detail}"
    elif not body:
        # JSON ではなかった。中身は載せない。
        message += "\n応答が JSON ではありませんでした。"

    details = body.get("details")
    named = isinstance(details, list) and bool(details)
    if named:
        for item in details:
            if not isinstance(item, dict):
                continue
            where = item.get("property") or "(場所の指定なし)"
            what = item.get("message") or ""
            message += f"\n  {where}: {what}".rstrip()

    if status == 400 and not named:
        # 名指しが無い 400 だけ候補を出す。push が友だちでない相手に失敗する経路は
        # details を伴わないことがあり、ここで何も言わないと英語の本文だけが残る。
        message += (
            "\n次のどれかの可能性があります:\n"
            f"  - 宛先の ID が違う（{USER_ID_ENV} を確認）\n"
            "  - 宛先のユーザーが LINE公式アカウントを友だち追加していない\n"
            "  - 宛先のユーザーが LINE公式アカウントをブロックしている"
        )

    if status == 401:
        message += (
            f"\nチャネルアクセストークンを確認してください（{CHANNEL_ACCESS_TOKEN_ENV}）。"
        )

    if status == 429:
        message += (
            "\nレート制限です。しばらく待ってから再実行してください。\n"
            "（無料のコミュニケーションプランは月200通。通数は"
            "「送信対象になった人数」でカウントされます）"
        )

    request_id = request_id_of(response)
    if request_id:
        # 問い合わせるときに必要な唯一の手掛かり。
        message += f"\nx-line-request-id: {request_id}"

    raise ApiError(redact(message, *secrets))


# ------------------------------------------------------------------ 自分が誰か


def fetch_bot_info(session, *, base: str = API_BASE, secrets: tuple = ()) -> BotInfo:
    """``GET /v2/bot/info`` で「このトークンが誰なのか」を確かめる。

    最初の1回をここに置くと、「トークンが無効」と「宛先が違う」を分けて
    報告できる。**送信してから初めて気づく、という順番にしない。**
    """
    response = session.get(f"{base}/v2/bot/info")

    try:
        raise_for_line_error(response, *secrets)
    except ApiError as error:
        raise AuthError(
            f"{error}\n"
            f"{CHANNEL_ACCESS_TOKEN_ENV} の値が正しいか、"
            "対象のチャネルで Messaging API が有効かを確認してください。"
        ) from error

    payload = payload_of(response)
    if not isinstance(payload, dict):
        raise AuthError("応答を JSON として読めませんでした（/v2/bot/info）。")

    # 照合の物差し。空のまま進むと照合が素通りする（課題8「0 件は不一致」と同じ形）。
    user_id = str(payload.get("userId") or "").strip()
    if not user_id:
        raise AuthError(
            "/v2/bot/info が userId を返しませんでした。"
            "送信者の照合ができないため中断します。"
        )

    basic_id = str(payload.get("basicId") or "").strip()
    if not basic_id:
        raise AuthError(
            "/v2/bot/info が basicId を返しませんでした。"
            "意図したチャネルかどうかを確かめられないため中断します。"
        )

    return BotInfo(
        user_id=user_id,
        basic_id=basic_id,
        display_name=str(payload.get("displayName") or ""),
        # **値は列挙して検査しない。** LINE 側が増やした日に動いていたものが止まる。
        chat_mode=str(payload.get("chatMode") or ""),
        mark_as_read_mode=str(payload.get("markAsReadMode") or ""),
    )


# ------------------------------------------------ 送る前に確かめる（課題10で追加）
#
# 課題9では「送れたことを、どう確かめるか」だけを扱った。LINE には bot が
# 送ったテキストを読み返す API が無い以上、**送る前にしか確かめられないこと**
# があり、それがここから下である。
#
# 課題9の verify_push.py も quota を叩いているが、あちらは送信後の
# 「通数が増えたか」に使っていて、**送信を止める判断には使っていない**。


@dataclass(frozen=True)
class Profile:
    """``GET /v2/bot/profile/{userId}`` が答えた宛先の素性。

    必須なのは ``userId`` と ``displayName`` の2つだけ。``pictureUrl`` /
    ``statusMessage`` / ``language`` は省略されうる（``language`` は
    **利用者がプライバシーポリシーに同意していないと入らない**。
    公式 OpenAPI 定義で確認）。必須でないものを必須として読むと、
    こちらの実装は変えていないのに**相手の同意状況で落ちる**。
    """

    user_id: str
    display_name: str


@dataclass(frozen=True)
class Reachability:
    """その宛先に**届くのか**を、送る前に答えたもの。

    **push は未友だち・ブロックの相手にも 200 を返す。** 友だちかどうかを
    判定する専用の API は無く、``GET /v2/bot/profile/{userId}`` が 404 を
    返すことだけが手がかりになる。

    だから 404 は「異常」ではなく、**正常に取得できた否定の答え**として扱い、
    例外にしない。例外にすると呼ぶ側が try/except で包むことになり、
    「確かめて届かないと分かった」と「確かめられなかった」が混ざる。
    """

    reachable: bool
    reason: str
    profile: Profile | None


@dataclass(frozen=True)
class Quota:
    """今月の送信上限。

    ``limited`` が偽なら**無制限**で、``limit`` は ``None`` になる。
    **0 と混ぜないこと。** 0 は「上限はあるが使い切った」で、無制限とは
    真逆の意味になる。
    """

    limited: bool
    limit: int | None


#: 404 が返ったときに利用者へ見せる説明。
#:
#: **理由を2つに決めつけていた**（2026-08-21 に修正）。公式リファレンスの
#: 404 の説明は「プロフィール情報を取得できませんでした」として、
#: ユーザーIDが存在しない・**プロフィールの取得に同意していない**・
#: 友だち追加していない、を並べている。**どれなのかは区別できない。**
#:
#: とくに2つ目が効く。**友だちであっても 404 になりうる**ので、このガードは
#: 「届くはずの相手を止める」側で間違えることがある。止めた側の誤りは
#: 送ってしまう誤りより安全だが、**そう書いていないと利用者は「ブロックされた」と
#: 読んでしまう**（そして相手に確認しに行くことになる）。
#:
#: **4つ目を実測で足した**（2026-08-24）。``task10/probe/block_probe.py`` で
#: 自分のアカウントを3点測ったところ（ブロック前 200 → ブロック中 404 →
#: 解除後 200、ボットも宛先も同一）、**ブロックされた相手にも 404 が返った**。
#: 公式の一覧にブロックは入っていないので、**書かなければ運用担当者は
#: 検討すらできない**。
#:
#: ただし**原因を1つに決めつけない**ことは変えない。ブロックが 404 を返しても、
#: 404 がブロックだとは限らない。原因が4つに増えても、区別が付かないことは同じ。
UNREACHABLE_REASON = (
    "この宛先はプロフィールを返しませんでした（404）。"
    "公式リファレンスでは、404 は「ユーザーIDが存在しない」"
    "「プロフィール情報の取得に同意していない」"
    "「友だち追加していない」のいずれでも返るとされています。"
    "さらにこの課題で実測したところ、"
    "**ブロックされている相手にも 404 が返りました**（公式の一覧には無い）。"
    "どれなのかはこの応答からは区別できません。"
    "push はこれらの相手にも 200 を返して届かないため、送信前に止めます。"
)

QUOTA_PATH = "/v2/bot/message/quota"
CONSUMPTION_PATH = "/v2/bot/message/quota/consumption"


def fetch_profile(
    session, user_id: str, *, base: str = API_BASE, secrets: tuple = ()
) -> Reachability:
    """宛先に届くかを、送る前に確かめる。

    **404 を例外にしない。** ここだけは「エラー」ではなく答えとして返す。
    理由は Reachability の説明のとおりで、404 が唯一の判定手段だから。

    ただし**404 は1つのことを言っていない。** 何を意味しうるかは
    UNREACHABLE_REASON に書いた。**「ブロックされている」と断定しない。**

    一方 401 や 403 は**資格情報の問題**なので、通常どおり例外にする。
    混ぜると「トークンが切れているのに、相手にブロックされたと報告する」
    ことになり、原因が違うのに同じ直しかたを試すはめになる。
    """
    response = session.get(f"{base}/v2/bot/profile/{user_id}")

    if getattr(response, "status_code", None) == 404:
        return Reachability(
            reachable=False,
            reason=UNREACHABLE_REASON,
            profile=None,
        )

    raise_for_line_error(response, *secrets)

    payload = payload_of(response)
    if not isinstance(payload, dict):
        raise ApiError("応答を JSON として読めませんでした（/v2/bot/profile）。")

    profile_user_id = str(payload.get("userId") or "").strip()
    if not profile_user_id:
        raise ApiError(
            "/v2/bot/profile が userId を返しませんでした。"
            "宛先を確かめられないため中断します。"
        )

    return Reachability(
        reachable=True,
        reason="",
        profile=Profile(
            user_id=profile_user_id,
            display_name=str(payload.get("displayName") or ""),
        ),
    )


def fetch_quota(session, *, base: str = API_BASE, secrets: tuple = ()) -> Quota:
    """今月の送信上限を読む。"""
    response = session.get(base + QUOTA_PATH)
    raise_for_line_error(response, *secrets)

    payload = payload_of(response)
    if not isinstance(payload, dict):
        raise ApiError("応答を JSON として読めませんでした（/v2/bot/message/quota）。")

    limited = str(payload.get("type") or "") == "limited"
    raw_limit = payload.get("value")
    # **value は type が limited のときだけ返る。**
    # 無いものを 0 と読むと「無制限」が「使い切った」に化ける。
    limit = int(raw_limit) if limited and raw_limit is not None else None
    return Quota(limited=limited, limit=limit)


def fetch_consumption(session, *, base: str = API_BASE, secrets: tuple = ()) -> int:
    """今月すでに送った通数を読む。"""
    response = session.get(base + CONSUMPTION_PATH)
    raise_for_line_error(response, *secrets)

    payload = payload_of(response)
    if not isinstance(payload, dict):
        raise ApiError(
            "応答を JSON として読めませんでした（/v2/bot/message/quota/consumption）。"
        )

    raw_usage = payload.get("totalUsage")
    if raw_usage is None:
        raise ApiError(
            "/v2/bot/message/quota/consumption が totalUsage を返しませんでした。"
            "残数を数えられないため中断します。"
        )
    return int(raw_usage)


def remaining_messages(quota: Quota, consumption: int) -> int | None:
    """あと何通送れるか。**無制限なら None。**

    0 を返してはいけない。0 は「上限はあるが使い切った」であり、無制限とは
    真逆である。混ぜると送信前ガードが**無制限のアカウントで送信を止める**。

    使い切ったあとも消費数は増えるので、**負の値は丸めない**。丸めると
    「あと 0 通」と「12 通ぶん超過している」が同じ表示になる。
    """
    if not quota.limited or quota.limit is None:
        return None
    return quota.limit - consumption
