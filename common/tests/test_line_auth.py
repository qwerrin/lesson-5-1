"""common/line_auth.py のテスト。

LINE の Channel Access Token は、これまでに書いた5つの認証のどれとも違う。

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

LINE に固有の事情が3つある。
------------------------------------------------------------------

**1. 送ったメッセージを読み返す API が無い。**

課題1〜8は全部「送信 → 別経路で読み返して照合」で締めてきた。LINE には
それに当たるものが無い（``GET /v2/bot/message/{messageId}/content`` は
**ユーザーが送った**画像・動画・音声専用で、bot が送ったテキストは取れない。
2026-08-18 に公式 OpenAPI 定義で確認）。

代わりの物差しが ``GET /v2/bot/info`` である。**投稿の応答だけを見て
「送れた」と判断すると、同じ応答の中で値を比べるトートロジーになる**
（課題4・課題6・課題7・課題8と同じ形）。別のエンドポイントから取った
``userId`` / ``basicId`` と突き合わせて初めて閉じる。

**2. 紛らわしい識別子が3つある。**

======================== ==================================================
名前                      どこにあるか
======================== ==================================================
チャネルアクセストークン   「Messaging API設定」タブの**いちばん下**
チャネルシークレット       「チャネル基本設定」タブ（**別物**）
あなたのユーザーID         「チャネル基本設定」タブ（``U`` で始まる）
ボットのベーシックID       「Messaging API設定」タブ（``@`` で始まる）
======================== ==================================================

**取り違えても、形の上では「設定されている」ように見える。** だから読む側で
形を見て、名指しで違いを言う。課題8の「Bot Token と Webhook URL の取り違え」と
同じ対処だが、LINE のほうが候補が多い。

**3. 応答モードが API から取れる。**

``GET /v2/bot/info`` の ``chatMode`` は ``chat`` か ``bot`` を返す。
画面の表示ではなく **API の答え**なので、「画面ではこうなっているはず」を
根拠にしないで済む。課題8で「画面の見え方と、投稿した主体は別」を踏んだので、
取れるものは取っておく。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import line_auth  # noqa: E402


# 本物に似た形の偽物。**実在の値は絶対に書かない**（このファイルは public リポジトリに入る）。
TOKEN = "FAKEtoken0000000000000000000000000000000000/FAKE+aaaa="
USER_ID = "U" + "0" * 32
BASIC_ID = "@fake0000"


class FakeResponse:
    """requests.Response のうち、このモジュールが触る部分だけを持つ器。

    ``json()`` が例外を投げる場合を作れるようにしてある。**本文が JSON でない
    応答は実際に来る**ので、そこで素の例外が出ると利用者に読めないものが出る。
    """

    def __init__(self, *, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers if headers is not None else {}

    def json(self):
        if self._payload is None:
            raise ValueError("応答が JSON ではありません")
        return self._payload


class FakeSession:
    """呼ばれた URL を覚える偽セッション。"""

    def __init__(self, response=None):
        self.headers = {}
        self.response = response or FakeResponse()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.response


def bot_info_payload(**overrides):
    payload = {
        "userId": "U" + "1" * 32,
        "basicId": BASIC_ID,
        "displayName": "開発テスト",
        "chatMode": "bot",
        "markAsReadMode": "auto",
    }
    payload.update(overrides)
    return payload


# ================================================================== トークンを読む


def test_reads_token_from_mapping():
    env = {line_auth.CHANNEL_ACCESS_TOKEN_ENV: TOKEN}

    assert line_auth.read_channel_access_token(env) == TOKEN


def test_token_is_stripped():
    """.env 経由では両端の空白はライブラリが落とすが、**呼び出し元は .env とは限らない**。

    common/ の他の5モジュールは os.environ から読む。同じ関数がどちらからも
    呼ばれうるので、ここでも落とす。
    """
    env = {line_auth.CHANNEL_ACCESS_TOKEN_ENV: f"  {TOKEN}  "}

    assert line_auth.read_channel_access_token(env) == TOKEN


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_blank_token_is_treated_as_missing(value):
    """空文字・空白だけは「未設定」と同じ扱いにする。

    ``LINE_CHANNEL_ACCESS_TOKEN=`` と書いただけでもキーとしては存在するので、
    有無だけ見ると素通りして、後段の API が 401 を返し、原因がここだと
    分からなくなる（課題4・課題8で踏んだ形）。
    """
    with pytest.raises(line_auth.AuthError):
        line_auth.read_channel_access_token({line_auth.CHANNEL_ACCESS_TOKEN_ENV: value})


def test_missing_key_is_treated_as_missing():
    with pytest.raises(line_auth.AuthError):
        line_auth.read_channel_access_token({})


def test_missing_token_message_tells_where_to_get_it():
    """**「いちばん下」まで書く。**

    実際にここで詰まった（2026-08-19）。「Messaging API設定タブにある」だけだと
    見つからない。ページ最下部までスクロールしないと現れないため。
    """
    with pytest.raises(line_auth.AuthError) as error:
        line_auth.read_channel_access_token({})

    message = str(error.value)
    assert line_auth.CHANNEL_ACCESS_TOKEN_ENV in message
    assert "Messaging API設定" in message
    assert "いちばん下" in message


def test_token_with_bearer_prefix_is_rejected():
    """``Bearer `` を付けた値を弾く。先頭は送信時にこちらで付ける。

    二重に付くと ``Authorization: Bearer Bearer xxx`` になり、401 の理由が
    「トークンが無効」に見える。
    """
    env = {line_auth.CHANNEL_ACCESS_TOKEN_ENV: f"Bearer {TOKEN}"}

    with pytest.raises(line_auth.AuthError) as error:
        line_auth.read_channel_access_token(env)

    assert "Bearer" in str(error.value)


def test_bearer_prefix_check_is_case_insensitive():
    env = {line_auth.CHANNEL_ACCESS_TOKEN_ENV: f"bearer {TOKEN}"}

    with pytest.raises(line_auth.AuthError):
        line_auth.read_channel_access_token(env)


def test_channel_secret_shaped_value_is_questioned():
    """32桁の16進数だけなら「チャネルシークレットでは？」と疑う。

    2つは**別のタブに並んでいて、どちらも「秘密の文字列」に見える**。
    取り違えたまま実行すると 401 が返るだけで、どちらを間違えたか分からない。

    **断定はしない。** チャネルシークレットが常にこの形だと公式が明記して
    いるのを確認していないので、「では？」と疑う文にとどめる
    （課題4の「相手が名指しで答えているものに候補を並べない」の裏返しで、
    こちらは相手が何も言っていない段階なので候補を出す価値がある）。
    """
    env = {line_auth.CHANNEL_ACCESS_TOKEN_ENV: "0123456789abcdef" * 2}

    with pytest.raises(line_auth.AuthError) as error:
        line_auth.read_channel_access_token(env)

    assert "チャネルシークレット" in str(error.value)


def test_a_real_looking_token_is_not_questioned():
    """本物の形は通す。**疑いが誤爆すると、正しい値で止まる。**"""
    assert line_auth.read_channel_access_token(
        {line_auth.CHANNEL_ACCESS_TOKEN_ENV: TOKEN}
    ) == TOKEN


def test_token_value_never_appears_in_error_messages():
    """**壊れた値でもメッセージに載せない。**

    打ち間違いなら本物がそのまま入っている。この文言は公開するスクリーンショットに写る。
    """
    broken = f"Bearer {TOKEN}"

    with pytest.raises(line_auth.AuthError) as error:
        line_auth.read_channel_access_token(
            {line_auth.CHANNEL_ACCESS_TOKEN_ENV: broken}
        )

    assert TOKEN not in str(error.value)


# ================================================================== 宛先IDを読む


def test_reads_user_id():
    env = {line_auth.USER_ID_ENV: USER_ID}

    assert line_auth.read_user_id(env) == USER_ID


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_user_id_is_treated_as_missing(value):
    """**「未設定」と名指しできていることまで見る。**

    ここを ``AuthError が出た`` だけで済ませると、空の検査を消しても
    下の「送信先IDの形ではありません」に肩代わりされて素通りする
    （2026-08-19・ミューテーションで検出）。空文字は先頭文字の検査にも引っかかるため。
    """
    with pytest.raises(line_auth.AuthError) as error:
        line_auth.read_user_id({line_auth.USER_ID_ENV: value})

    assert "設定されていません" in str(error.value)


def test_basic_id_in_user_id_slot_is_named_explicitly():
    """``@`` 始まりは「ボットのベーシックIDでは？」と名指しする。

    **これが LINE でいちばん踏みやすい取り違え。** 両方とも「ID」で、
    片方は Messaging API設定タブ、もう片方はチャネル基本設定タブにある。
    ``@687jseqd`` を宛先に入れても「IDっぽい」ので気づけない。
    """
    with pytest.raises(line_auth.AuthError) as error:
        line_auth.read_user_id({line_auth.USER_ID_ENV: BASIC_ID})

    message = str(error.value)
    assert "ベーシックID" in message
    assert "あなたのユーザーID" in message
    # **入れてしまった値そのものを出す。** ここを見ないと、名指しの一文を消しても
    # 後続の説明文に「ベーシックID」が残っていて素通りする（2026-08-19 に検出）。
    assert BASIC_ID in message


@pytest.mark.parametrize("prefix", ["U", "C", "R"])
def test_user_group_and_room_ids_are_all_accepted(prefix):
    """``U``（ユーザー）``C``（グループ）``R``（複数人トーク）を通す。

    push の ``to`` はこの3種を受ける。**ユーザーIDだけに絞らない。**
    絞ると、グループへ送りたくなったときに「形が違う」で止まる。
    """
    value = prefix + "0" * 32

    assert line_auth.read_user_id({line_auth.USER_ID_ENV: value}) == value


def test_obviously_wrong_prefix_is_rejected():
    with pytest.raises(line_auth.AuthError):
        line_auth.read_user_id({line_auth.USER_ID_ENV: "xyz123"})


def test_user_id_with_whitespace_inside_is_rejected():
    """内側に空白がある値を弾く。コピペで隣の文字を拾った形。"""
    with pytest.raises(line_auth.AuthError):
        line_auth.read_user_id({line_auth.USER_ID_ENV: "U000 111"})


def test_user_id_is_not_secret_so_it_may_appear_in_messages():
    """**ユーザーIDは伏せない。**

    トークンと違い、これは「どこへ送るか」であって権限ではない。伏せると
    「宛先を間違えた」ときに何を直せばよいか分からなくなる。
    ただしスクリーンショットには写るので、記事に載せる範囲は README で決める。
    """
    with pytest.raises(line_auth.AuthError) as error:
        line_auth.read_user_id({line_auth.USER_ID_ENV: "xyz123"})

    assert "xyz123" in str(error.value)


# ================================================================== 伏せ字


def test_redact_replaces_every_secret():
    text = f"token={TOKEN} again={TOKEN}"

    assert TOKEN not in line_auth.redact(text, TOKEN)


def test_redact_skips_empty_secrets():
    """空や None の秘密は素通りさせる。

    ``str.replace("", x)`` は**全部の文字の間に x を挿し込む**ので、
    素通りさせないと文章が壊れる。
    """
    assert line_auth.redact("そのまま", "", None) == "そのまま"


def test_redact_leaves_a_visible_marker():
    """伏せたことが分かる印を残す。空文字にすると「元から無かった」と区別がつかない。

    **``REDACTED in text`` だけでは守れない。** ``REDACTED = ""`` にすると
    「空文字はどんな文字列にも含まれる」ので必ず真になり、印を消しても素通りする
    （2026-08-19・ミューテーションで検出）。印が**空でないこと**を先に見る。
    """
    assert line_auth.REDACTED
    assert line_auth.REDACTED in line_auth.redact(f"x={TOKEN}", TOKEN)


# ================================================================== セッション


def test_build_session_sets_bearer_authorization():
    session = line_auth.build_session(TOKEN, factory=FakeSession)

    assert session.headers["Authorization"] == f"Bearer {TOKEN}"


def test_build_session_sets_user_agent():
    """User-Agent を明示する。既定のままだと、何が叩いているのか相手側で分からない。"""
    session = line_auth.build_session(TOKEN, factory=FakeSession)

    assert session.headers["User-Agent"] == line_auth.USER_AGENT


def test_build_session_rejects_blank_token():
    """空のまま組ませない。組めてしまうと 401 になって原因が遠くなる。"""
    with pytest.raises(line_auth.AuthError):
        line_auth.build_session("   ", factory=FakeSession)


# ================================================================== 自分が誰か


def test_fetch_bot_info_calls_the_info_endpoint():
    session = FakeSession(FakeResponse(payload=bot_info_payload()))

    line_auth.fetch_bot_info(session)

    method, url, _ = session.calls[0]
    assert method == "GET"
    assert url.endswith("/v2/bot/info")


def test_fetch_bot_info_returns_every_field():
    session = FakeSession(FakeResponse(payload=bot_info_payload()))

    info = line_auth.fetch_bot_info(session)

    assert info.user_id == "U" + "1" * 32
    assert info.basic_id == BASIC_ID
    assert info.display_name == "開発テスト"
    assert info.chat_mode == "bot"
    assert info.mark_as_read_mode == "auto"


def test_fetch_bot_info_keeps_unknown_chat_mode():
    """知らない ``chatMode`` が来ても落とさない。

    enum を列挙して検査すると、LINE 側が値を増やした日に**動いていたものが止まる**。
    ここは「何が返ったか」を持ち帰るだけの役目にして、判断は呼び出し側に置く。
    """
    session = FakeSession(FakeResponse(payload=bot_info_payload(chatMode="未知の値")))

    assert line_auth.fetch_bot_info(session).chat_mode == "未知の値"


def test_fetch_bot_info_without_user_id_fails():
    """``userId`` が空なら止める。**これが照合の物差しなので、空のまま進むと照合が素通りする。**

    課題8の「0 件を不一致として捕まえる」と同じ形。物差しが無い状態を
    「一致した」に倒さない。
    """
    session = FakeSession(FakeResponse(payload=bot_info_payload(userId="")))

    with pytest.raises(line_auth.AuthError):
        line_auth.fetch_bot_info(session)


def test_fetch_bot_info_without_basic_id_fails():
    """``basicId`` も物差しに使うので必須にする。

    画面に出ている ``@687jseqd`` と突き合わせて「意図したチャネルか」を確かめる。
    チャネルを2つ作って取り違えたときに、ここでしか気づけない。
    """
    session = FakeSession(FakeResponse(payload=bot_info_payload(basicId="")))

    with pytest.raises(line_auth.AuthError):
        line_auth.fetch_bot_info(session)


def test_fetch_bot_info_with_non_json_body_fails_readably():
    """本文が JSON でないときに素の例外を出さない。利用者に読めないものを見せない。

    **「JSON として読めなかった」と名指しできていることまで見る。**
    ここを ``AuthError が出た`` だけにすると、空の辞書に倒す実装に変えても
    userId が空で落ちるので素通りする（2026-08-19・ミューテーションで検出）。
    """
    session = FakeSession(FakeResponse(status_code=200, payload=None, text="<html>"))

    with pytest.raises(line_auth.AuthError) as error:
        line_auth.fetch_bot_info(session)

    assert "JSON" in str(error.value)


def test_fetch_bot_info_with_list_body_fails():
    """JSON ではあるが辞書でない場合。``payload.get`` が AttributeError になる経路を塞ぐ。"""
    session = FakeSession(FakeResponse(payload=[1, 2]))

    with pytest.raises(line_auth.AuthError):
        line_auth.fetch_bot_info(session)


def test_fetch_bot_info_wraps_api_error_as_auth_error():
    """401 は「権限が足りない」ではなく「トークンが違う」として案内する。

    最初の1回をここに置くと、**送信してから初めて気づく**という順番にしないで済む。
    """
    session = FakeSession(
        FakeResponse(status_code=401, payload={"message": "Authentication failed"})
    )

    with pytest.raises(line_auth.AuthError) as error:
        line_auth.fetch_bot_info(session)

    assert line_auth.CHANNEL_ACCESS_TOKEN_ENV in str(error.value)


def test_fetch_bot_info_redacts_secrets_in_errors():
    session = FakeSession(
        FakeResponse(status_code=400, payload={"message": f"bad {TOKEN}"})
    )

    with pytest.raises(line_auth.LineError) as error:
        line_auth.fetch_bot_info(session, secrets=(TOKEN,))

    assert TOKEN not in str(error.value)


# ================================================================== エラーの訳し分け


def test_success_status_raises_nothing():
    line_auth.raise_for_line_error(FakeResponse(status_code=200, payload={}))


def test_error_includes_status_code():
    with pytest.raises(line_auth.ApiError) as error:
        line_auth.raise_for_line_error(
            FakeResponse(status_code=400, payload={"message": "bad request"})
        )

    assert "400" in str(error.value)
    assert "bad request" in str(error.value)


def test_error_includes_property_paths_from_details():
    """``details`` の ``property`` を出す。**LINE は壊れている場所を名指ししてくる。**

    ``/messages/0/text`` のような指し方をするので、そのまま見せるのが一番早い。
    """
    payload = {
        "message": "The request body has 1 error(s)",
        "details": [{"message": "May not be empty", "property": "/messages/0/text"}],
    }

    with pytest.raises(line_auth.ApiError) as error:
        line_auth.raise_for_line_error(FakeResponse(status_code=400, payload=payload))

    text = str(error.value)
    assert "/messages/0/text" in text
    assert "May not be empty" in text


def test_no_guesses_when_the_api_names_the_problem():
    """``details`` があるときは候補を並べない。

    課題4（Meet）の教訓：**相手が名指しで答えているものに候補を並べない。**
    「友だち追加していますか」を毎回足すと、名指しの情報が埋もれる。
    """
    payload = {
        "message": "The request body has 1 error(s)",
        "details": [{"message": "May not be empty", "property": "/messages/0/text"}],
    }

    with pytest.raises(line_auth.ApiError) as error:
        line_auth.raise_for_line_error(FakeResponse(status_code=400, payload=payload))

    assert "友だち" not in str(error.value)


def test_bare_400_offers_the_likely_causes():
    """``details`` が無い 400 のときだけ候補を出す。

    push が友だちでない相手に対して失敗する経路は、**details を伴わない**ことがある。
    ここで何も言わないと、利用者は本文の英語だけを見て途方に暮れる。
    """
    with pytest.raises(line_auth.ApiError) as error:
        line_auth.raise_for_line_error(
            FakeResponse(status_code=400, payload={"message": "Invalid to"})
        )

    assert "友だち" in str(error.value)


def test_401_points_at_the_token():
    """401 はトークンの問題だと名指しする。

    「認証に失敗しました」だけだと、利用者は宛先やチャネル設定を疑い始める。
    どの環境変数を直せばよいかまで書く。
    """
    with pytest.raises(line_auth.ApiError) as error:
        line_auth.raise_for_line_error(
            FakeResponse(status_code=401, payload={"message": "Authentication failed"})
        )

    assert line_auth.CHANNEL_ACCESS_TOKEN_ENV in str(error.value)


def test_429_explains_the_rate_limit():
    with pytest.raises(line_auth.ApiError) as error:
        line_auth.raise_for_line_error(
            FakeResponse(status_code=429, payload={"message": "Too Many Requests"})
        )

    assert "レート制限" in str(error.value)


def test_request_id_header_is_surfaced():
    """``x-line-request-id`` を出す。問い合わせるときに必要な唯一の手掛かり。"""
    response = FakeResponse(
        status_code=500,
        payload={"message": "Internal Server Error"},
        headers={"x-line-request-id": "abcdef-123"},
    )

    with pytest.raises(line_auth.ApiError) as error:
        line_auth.raise_for_line_error(response)

    assert "abcdef-123" in str(error.value)


def test_request_id_lookup_is_case_insensitive():
    """HTTP ヘッダ名は大文字小文字を区別しない。

    requests の Response.headers は区別しないが、**テストの偽物は素の dict** なので、
    ここを固定しておかないと「本物では動くがテストでは落ちる」の逆が起きる。
    """
    response = FakeResponse(
        status_code=500,
        payload={"message": "x"},
        headers={"X-Line-Request-Id": "UPPER-1"},
    )

    with pytest.raises(line_auth.ApiError) as error:
        line_auth.raise_for_line_error(response)

    assert "UPPER-1" in str(error.value)


def test_non_json_error_body_is_reported_without_dumping_html():
    """本文が HTML のとき、中身をそのまま流さない。読めないものを見せない。"""
    response = FakeResponse(status_code=502, payload=None, text="<html>Bad Gateway</html>")

    with pytest.raises(line_auth.ApiError) as error:
        line_auth.raise_for_line_error(response)

    text = str(error.value)
    assert "502" in text
    assert "<html>" not in text


def test_error_message_redacts_secrets():
    response = FakeResponse(status_code=400, payload={"message": f"bad {TOKEN}"})

    with pytest.raises(line_auth.ApiError) as error:
        line_auth.raise_for_line_error(response, TOKEN)

    assert TOKEN not in str(error.value)


# ================================================================== 定数


def test_api_base_is_pinned_to_the_documented_host():
    """ホストを固定する。組み立てで作ると、いつか別のホストへ送る形になる。"""
    assert line_auth.API_BASE == "https://api.line.me"


def test_env_names_match_the_example_file():
    """``.env.example`` に書いた名前と一致させる。

    見本と実装がずれると、**見本どおりに書いたのに動かない**という最悪の形になる。
    ここは文字列で固定し、README の記述は check_docs.py で別途突き合わせる。
    """
    assert line_auth.CHANNEL_ACCESS_TOKEN_ENV == "LINE_CHANNEL_ACCESS_TOKEN"
    assert line_auth.USER_ID_ENV == "LINE_USER_ID"


# ============================================ 課題10：送る前に確かめる（宛先）
#
# 課題9では「送れたことをどう確かめるか」だけを扱った。読み返す API が無い以上、
# **送る前にしか確かめられないこと**があり、それがここから下である。


def profile_payload(**overrides):
    payload = {"userId": USER_ID, "displayName": "ダミー太郎"}
    payload.update(overrides)
    return payload


def test_profile_of_a_friend_is_reachable():
    session = FakeSession(FakeResponse(payload=profile_payload()))

    result = line_auth.fetch_profile(session, USER_ID)

    assert result.reachable is True
    assert result.profile is not None
    assert result.profile.display_name == "ダミー太郎"


def test_profile_404_means_not_reachable_and_is_not_an_error():
    """未友だち・ブロックは 404。**例外にしない。**

    push はこの相手に対しても **HTTP 200 を返す**。友だちかどうかを判定する
    専用の API は無く、この 404 が唯一の手がかりである。つまり 404 は
    「異常」ではなく、**送る前に分かる正常な状態**として扱う。

    例外にすると呼ぶ側が try/except で包むことになり、
    「確かめて届かないと分かった」と「確かめられなかった」が混ざる。
    """
    session = FakeSession(
        FakeResponse(status_code=404, payload={"message": "Not found"})
    )

    result = line_auth.fetch_profile(session, USER_ID)

    assert result.reachable is False
    assert result.profile is None
    assert result.reason != ""


def test_profile_401_is_an_error_not_unreachable():
    """401 は資格情報の問題。「友だちではない」と混ぜない。

    混ぜると、**トークンが切れているのに「相手にブロックされた」と報告する**。
    原因が違えば直しかたも違う。
    """
    session = FakeSession(
        FakeResponse(status_code=401, payload={"message": "Authentication failed"})
    )

    with pytest.raises(line_auth.LineError):
        line_auth.fetch_profile(session, USER_ID)


def test_profile_url_carries_the_user_id():
    session = FakeSession(FakeResponse(payload=profile_payload()))

    line_auth.fetch_profile(session, USER_ID)

    assert session.calls[0][1].endswith(f"/v2/bot/profile/{USER_ID}")


def test_profile_without_optional_fields_still_works():
    """``pictureUrl`` / ``statusMessage`` / ``language`` は省略されうる。

    ``language`` は**利用者がプライバシーポリシーに同意していないと入らない**
    （公式 OpenAPI 定義で確認）。必須でないものを必須として読むと、
    同意状況によって落ちる。
    """
    session = FakeSession(
        FakeResponse(payload={"userId": USER_ID, "displayName": "ダミー太郎"})
    )

    result = line_auth.fetch_profile(session, USER_ID)

    assert result.reachable is True


# ============================================ 課題10：送る前に確かめる（通数）


def test_quota_limited_carries_the_limit():
    session = FakeSession(FakeResponse(payload={"type": "limited", "value": 200}))

    quota = line_auth.fetch_quota(session)

    assert quota.limited is True
    assert quota.limit == 200


def test_quota_none_has_no_limit_and_is_not_zero():
    """``type: "none"`` は**無制限**で、``value`` は省略される。

    ここを 0 と読むと「枠が無い＝送れない」と**真逆に判定する**。
    公式 OpenAPI 定義は value を
    「``type`` が ``limited`` のときに返る」と明記している。
    """
    session = FakeSession(FakeResponse(payload={"type": "none"}))

    quota = line_auth.fetch_quota(session)

    assert quota.limited is False
    assert quota.limit is None


def test_consumption_reads_total_usage():
    session = FakeSession(FakeResponse(payload={"totalUsage": 4}))

    assert line_auth.fetch_consumption(session) == 4


def test_remaining_is_limit_minus_usage():
    quota = line_auth.Quota(limited=True, limit=200)

    assert line_auth.remaining_messages(quota, 4) == 196


def test_remaining_is_none_when_unlimited_not_zero():
    """無制限のときの残数は **None**。0 と混ぜない。

    0 を返すと「使い切った」と同じ値になり、送信前ガードが
    **無制限のアカウントで送信を止める**。
    """
    quota = line_auth.Quota(limited=False, limit=None)

    assert line_auth.remaining_messages(quota, 4) is None


def test_remaining_can_go_negative():
    """使い切った後も消費数は増える。負の値を 0 に丸めない。

    丸めると「あと 0 通」と「12 通ぶん超過している」が同じ表示になる。
    """
    quota = line_auth.Quota(limited=True, limit=200)

    assert line_auth.remaining_messages(quota, 212) == -12


# ==================== 課題10：上のテストが守れていなかったところ（実測で発覚）
#
# 2026-08-20 のミューテーションで、下の4つは**壊しても誰も気づかなかった**。
# どれも「例外が出た」「空でない」しか見ていなかったのが原因で、
# 課題9で踏んだ「肩代わり」と同じ形をしている。


def test_unreachable_reason_names_the_cause():
    """理由に「なぜ止めたか」が入る。**空でないだけでは足りない。**

    reason は複数の文を連結して作るので、一部を空にしても「空でない」は
    満たされてしまう。何が書かれているべきかを名指しで確かめる。
    """
    session = FakeSession(
        FakeResponse(status_code=404, payload={"message": "Not found"})
    )

    result = line_auth.fetch_profile(session, USER_ID)

    assert "404" in result.reason
    assert "友だち" in result.reason
    # push が 200 を返すこと自体が、止める理由の中核なので必ず触れる。
    assert "200" in result.reason


def test_unreachable_reason_includes_the_measured_block_case():
    """404 の原因に**ブロック**を入れる（2026-08-24 に実測した）。

    公式リファレンスが並べているのは3つで、ブロックは入っていない。
    だが ``task10/probe/block_probe.py`` で3点測ったところ
    （ブロック前 200 → ブロック中 404 → 解除後 200）、
    **ブロックされた相手にも 404 が返った**。

    ここを書かないと、運用担当者は一覧に無い原因を検討できない。
    「安全に倒すことと、正しく説明することは別」——この課題の主題そのもの。
    """
    session = FakeSession(
        FakeResponse(status_code=404, payload={"message": "Not found"})
    )

    result = line_auth.fetch_profile(session, USER_ID)

    assert "ブロック" in result.reason


def test_unreachable_reason_does_not_decide_which_cause_it_is():
    """**原因を1つに決めつけない。**

    ブロックが 404 を返すと分かっても、404 がブロックだとは限らない。
    「ブロックされています」と書けば、運用担当者は何もしていない相手に
    確認の連絡をする。原因が4つに増えても、区別が付かないことは変わらない。
    """
    session = FakeSession(
        FakeResponse(status_code=404, payload={"message": "Not found"})
    )

    result = line_auth.fetch_profile(session, USER_ID)

    assert "区別できません" in result.reason
    assert "ブロックされています" not in result.reason


def test_profile_401_is_rejected_even_when_the_body_looks_normal():
    """本文が**正常な形**でも、401 は例外にする。

    ひとつ上の 401 のテストは本文をエラー形にしていたので、
    ``raise_for_line_error`` を消しても「userId が無い」で別の例外が出て、
    **テストが通ってしまった**（2026-08-20 のミューテーションで素通り）。
    本文を正常な形にすると、防壁がこの1つだけになる。
    """
    session = FakeSession(FakeResponse(status_code=401, payload=profile_payload()))

    with pytest.raises(line_auth.LineError):
        line_auth.fetch_profile(session, USER_ID)


def test_profile_without_user_id_is_rejected():
    """200 でも ``userId`` が無ければ中断する。

    宛先を確かめられないまま「届く」と答えると、**確かめていないことを
    確かめたことにしてしまう**。
    """
    session = FakeSession(FakeResponse(payload={"displayName": "ダミー太郎"}))

    with pytest.raises(line_auth.LineError):
        line_auth.fetch_profile(session, USER_ID)


def test_consumption_without_total_usage_is_rejected():
    """``totalUsage`` が無ければ中断する。

    0 とみなすと**残数が水増しされる**。「まだ 200 通ある」と答えてから
    送信に失敗するのが、いちばん困る壊れかたである。
    """
    session = FakeSession(FakeResponse(payload={}))

    with pytest.raises(line_auth.LineError):
        line_auth.fetch_consumption(session)
