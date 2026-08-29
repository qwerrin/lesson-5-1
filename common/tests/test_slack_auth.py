"""common/slack_auth.py のテスト。

Slack の Bot Token は、これまでに書いた3つの認証のどれとも違う。

===================== ================================================
何を使うか             どういう相手か
===================== ================================================
google_auth（OAuth）  **本人のデータ**を触る。同意画面 → token.json → リフレッシュ
zoom_auth（S2S）      アカウントの権限で動く。同意画面なし・毎回取り直し
youtube_auth（キー）  **公開データ**を読むだけ。認可する相手がいない
slack_auth（Bot）     **アプリ自身**が動く。インストール時に1回発行、期限なし
===================== ================================================

Slack で固有なのは次の2つである。

1. **トークンは Authorization ヘッダで送る。URL には載らない。**
   youtube_auth の「API キーが URI に載るので str(error) で漏れる」問題は
   ここでは起きない。ただし漏れ方が違うだけで、漏れないわけではないので
   redact() は同じように用意する。

2. **付与されたスコープが応答本文に入らない。**
   zoom_auth は access_token と一緒に scope が返るので require_scopes() で
   確定できた。Slack は HTTP ヘッダ（x-oauth-scopes）に入るとされるが、
   **公式のメソッドリファレンスには記載がない**（2026-08-16 に確認）。
   つまり「取れるかどうか自体が不確実」なので、**取れなかったことを
   『足りている』に倒さない**設計にする。ここがこのモジュールの山場。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import slack_auth  # noqa: E402


# **本物の形を真似ない。** 本物の Bot Token は `xoxb-` のあとが数字とハイフンの
# 並びになる。リポジトリ全体を「トークンらしき文字列」で検査するので、
# 本物そっくりの偽物を置くと検査が鳴り続け、除外リストで通す羽目になる。
# 除外で通すようにすると、**本物を置いたときも同じ言い訳で通ってしまう**。
# プレフィックスだけ本物なのは、プレフィックス検査そのものを確かめるため。
TOKEN = "xoxb-DUMMY-TOKEN-FOR-TESTS-not-a-real-credential"


def fake_auth_test(*, ok=True, headers=None, **fields):
    """auth.test の応答を模した最小の器。

    slack_sdk の SlackResponse は data（本文）と headers（HTTPヘッダ）を持つ。
    照合したいのはその2つだけなので、本物を組み立てずに同じ形を作る。
    """

    class _Response:
        def __init__(self):
            self.data = {"ok": ok, **fields}
            self.headers = headers if headers is not None else {}

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    class _Client:
        def __init__(self):
            self.calls = []

        def auth_test(self, **kwargs):
            self.calls.append(kwargs)
            return _Response()

    return _Client()


class TestReadBotToken:
    def test_環境変数から読む(self):
        assert slack_auth.read_bot_token({slack_auth.BOT_TOKEN_ENV: TOKEN}) == TOKEN

    def test_前後の空白を落とす(self):
        env = {slack_auth.BOT_TOKEN_ENV: f"  {TOKEN}\n"}
        assert slack_auth.read_bot_token(env) == TOKEN

    def test_未設定なら落ちる(self):
        with pytest.raises(slack_auth.AuthError):
            slack_auth.read_bot_token({})

    def test_空文字は未設定と同じ扱い(self):
        # `$env:SLACK_BOT_TOKEN = ""` は変数としては存在する。
        # 有無だけ見ると素通りして、後段が invalid_auth を返し原因が遠くなる。
        with pytest.raises(slack_auth.AuthError):
            slack_auth.read_bot_token({slack_auth.BOT_TOKEN_ENV: ""})

    def test_空白だけも未設定と同じ扱い(self):
        with pytest.raises(slack_auth.AuthError):
            slack_auth.read_bot_token({slack_auth.BOT_TOKEN_ENV: "   "})

    def test_エラーに環境変数名が出る(self):
        with pytest.raises(slack_auth.AuthError) as caught:
            slack_auth.read_bot_token({})
        assert slack_auth.BOT_TOKEN_ENV in str(caught.value)

    def test_ユーザートークンを弾く(self):
        # xoxp- は User Token。**投稿自体は通ってしまう**ので、弾かないと
        # 「アプリが投稿した」つもりで本人名義の投稿ができあがる。
        # 課題の要件は Bot なので、ここで気づけるようにする。
        with pytest.raises(slack_auth.AuthError):
            slack_auth.read_bot_token({slack_auth.BOT_TOKEN_ENV: "xoxp-DUMMY-user-token"})

    def test_全く別の値も弾く(self):
        # Client Secret や Signing Secret を貼り間違えた場合。
        with pytest.raises(slack_auth.AuthError):
            slack_auth.read_bot_token({slack_auth.BOT_TOKEN_ENV: "0123456789abcdef"})

    def test_エラーにトークンの値を載せない(self):
        # 実行画面のスクリーンショットが public リポジトリに入る。
        # 「形式が違う」と伝えるために値を出す必要はない。
        wrong = "xoxp-SECRET-VALUE-should-not-appear"
        with pytest.raises(slack_auth.AuthError) as caught:
            slack_auth.read_bot_token({slack_auth.BOT_TOKEN_ENV: wrong})
        assert wrong not in str(caught.value)

    def test_エラーに正しいプレフィックスを書く(self):
        # 何が期待されているか分からないと直せない。値は出さないが形式は出す。
        with pytest.raises(slack_auth.AuthError) as caught:
            slack_auth.read_bot_token({slack_auth.BOT_TOKEN_ENV: "xoxp-DUMMY-user-token"})
        assert slack_auth.BOT_TOKEN_PREFIX in str(caught.value)

    def test_未設定と形式違いで理由を分ける(self):
        # **「例外が出ること」と「正しい理由で落ちること」は別物。**
        # 未設定のときは値が "" になり、そのまま形式の検査にも引っかかる。
        # 型だけ見ていると、未設定の検査を丸ごと消しても緑のままになる
        # （2026-08-16 のミューテーションで実際に素通りした）。
        with pytest.raises(slack_auth.AuthError) as unset:
            slack_auth.read_bot_token({})
        with pytest.raises(slack_auth.AuthError) as wrong:
            slack_auth.read_bot_token({slack_auth.BOT_TOKEN_ENV: "xoxp-DUMMY-user-token"})
        assert str(unset.value) != str(wrong.value)

    def test_未設定のエラーは設定を促す(self):
        with pytest.raises(slack_auth.AuthError) as caught:
            slack_auth.read_bot_token({})
        assert "設定されていません" in str(caught.value)


class TestRedact:
    def test_トークンを伏せる(self):
        text = f"Authorization: Bearer {TOKEN}"
        assert TOKEN not in slack_auth.redact(text, TOKEN)

    def test_伏せ字に置き換わる(self):
        assert slack_auth.REDACTED in slack_auth.redact(f"token={TOKEN}", TOKEN)

    def test_伏せ字は空でない(self):
        # 空文字にすると「伏せた」と「元から無かった」の区別がつかない。
        # さらに `REDACTED in hidden` が常に真になり、上の検査が死ぬ。
        assert slack_auth.REDACTED != ""

    def test_複数回出てきても全部伏せる(self):
        assert TOKEN not in slack_auth.redact(f"{TOKEN} と {TOKEN}", TOKEN)

    def test_トークン以外はそのまま残す(self):
        text = f"not_in_channel ({TOKEN})"
        hidden = slack_auth.redact(text, TOKEN)
        assert "not_in_channel" in hidden

    def test_トークンが空なら何もしない(self):
        # str.replace("", x) は全部の文字の間に x を挿し込む。素通りさせると文章が壊れる。
        assert slack_auth.redact("そのまま", "") == "そのまま"

    def test_トークンがNoneでも落ちない(self):
        assert slack_auth.redact("そのまま", None) == "そのまま"


class TestBuildClient:
    def test_トークンを渡す(self):
        calls = []

        def fake_factory(**kwargs):
            calls.append(kwargs)
            return "client"

        slack_auth.build_client(TOKEN, factory=fake_factory)
        assert calls[0]["token"] == TOKEN

    def test_組み立てたクライアントを返す(self):
        assert slack_auth.build_client(TOKEN, factory=lambda **k: "client") == "client"

    def test_空のトークンでは組み立てない(self):
        with pytest.raises(slack_auth.AuthError):
            slack_auth.build_client("", factory=lambda **k: "client")


class TestFetchIdentity:
    def test_authtestを呼ぶ(self):
        client = fake_auth_test(
            team="TeamName", team_id="T111", user_id="U222", bot_id="B333"
        )
        slack_auth.fetch_identity(client)
        assert len(client.calls) == 1

    def test_ワークスペースとBotの情報を取り出す(self):
        client = fake_auth_test(
            team="TeamName", team_id="T111", user_id="U222", bot_id="B333"
        )
        identity = slack_auth.fetch_identity(client)
        # 期待値はリテラルで書く。定数と比べるとどちらを書き換えても通ってしまう。
        assert identity.team == "TeamName"
        assert identity.team_id == "T111"
        assert identity.user_id == "U222"
        assert identity.bot_id == "B333"

    def test_botidが無ければ落ちる(self):
        # User Token を渡すと auth.test は成功するが bot_id を返さない。
        # **プレフィックス検査は形式しか見ていない。** ここが性質の検査になる。
        client = fake_auth_test(team="TeamName", team_id="T111", user_id="U222")
        with pytest.raises(slack_auth.AuthError):
            slack_auth.fetch_identity(client)

    def test_botidが空文字でも落ちる(self):
        client = fake_auth_test(
            team="TeamName", team_id="T111", user_id="U222", bot_id=""
        )
        with pytest.raises(slack_auth.AuthError):
            slack_auth.fetch_identity(client)

    def test_useridが無ければ落ちる(self):
        # user_id は「投稿者が自分か」を確かめる物差しになる。
        # 空のまま進むと照合が素通りする。
        client = fake_auth_test(team="TeamName", team_id="T111", bot_id="B333")
        with pytest.raises(slack_auth.AuthError):
            slack_auth.fetch_identity(client)

    def test_okがfalseなら落ちる(self):
        client = fake_auth_test(ok=False, error="invalid_auth")
        with pytest.raises(slack_auth.AuthError):
            slack_auth.fetch_identity(client)

    def test_ヘッダからスコープを読む(self):
        client = fake_auth_test(
            team="TeamName",
            team_id="T111",
            user_id="U222",
            bot_id="B333",
            headers={slack_auth.SCOPE_HEADER: "chat:write,channels:history"},
        )
        identity = slack_auth.fetch_identity(client)
        assert identity.scopes == ("chat:write", "channels:history")

    def test_スコープの空白を落とす(self):
        client = fake_auth_test(
            team="TeamName",
            team_id="T111",
            user_id="U222",
            bot_id="B333",
            headers={slack_auth.SCOPE_HEADER: "chat:write, channels:history"},
        )
        identity = slack_auth.fetch_identity(client)
        assert identity.scopes == ("chat:write", "channels:history")

    def test_ヘッダの大文字小文字を問わない(self):
        # HTTP ヘッダ名は大小を区別しない。実際に返る綴りが確認できていないので、
        # どちらで来ても読めるようにする。
        client = fake_auth_test(
            team="TeamName",
            team_id="T111",
            user_id="U222",
            bot_id="B333",
            headers={"X-OAuth-Scopes": "chat:write"},
        )
        identity = slack_auth.fetch_identity(client)
        assert identity.scopes == ("chat:write",)

    def test_ヘッダが無ければスコープは不明(self):
        # **空タプルにしない。** 「0個だった」と「読めなかった」は別のこと。
        # 空タプルにすると、この後の照合で「足りないものは無い」に化ける。
        client = fake_auth_test(
            team="TeamName", team_id="T111", user_id="U222", bot_id="B333"
        )
        identity = slack_auth.fetch_identity(client)
        assert identity.scopes is None

    def test_別のヘッダしか無くてもスコープは不明(self):
        # 空の headers だけで試すと、**ヘッダを1件ずつ見る経路を一度も通らない**。
        # 実際の応答にはヘッダが必ず何かしら入っているので、こちらが本番に近い。
        client = fake_auth_test(
            team="TeamName",
            team_id="T111",
            user_id="U222",
            bot_id="B333",
            headers={"content-type": "application/json"},
        )
        identity = slack_auth.fetch_identity(client)
        assert identity.scopes is None

    def test_okがfalseなら他が揃っていても落ちる(self):
        # ok=False の応答から bot_id を抜いた入力で試すと、bot_id の検査のほうで
        # 落ちてしまい「ok を見ているか」を確かめられない。
        # **他の条件を全部満たしたうえで、狙った1点だけ違う入力**にする。
        client = fake_auth_test(
            ok=False,
            error="invalid_auth",
            team="TeamName",
            team_id="T111",
            user_id="U222",
            bot_id="B333",
        )
        with pytest.raises(slack_auth.AuthError):
            slack_auth.fetch_identity(client)

    def test_認証エラーの理由をそのまま出す(self):
        # 相手が invalid_auth と名指ししているので、こちらで候補を並べ直さない。
        client = fake_auth_test(
            ok=False,
            error="invalid_auth",
            team="TeamName",
            team_id="T111",
            user_id="U222",
            bot_id="B333",
        )
        with pytest.raises(slack_auth.AuthError) as caught:
            slack_auth.fetch_identity(client)
        assert "invalid_auth" in str(caught.value)


class TestCheckScopes:
    def _identity(self, scopes):
        return slack_auth.Identity(
            team="TeamName", team_id="T111", user_id="U222", bot_id="B333", scopes=scopes
        )

    def test_全部あれば足りない物は無い(self):
        check = slack_auth.check_scopes(
            self._identity(("chat:write", "channels:history")),
            ("chat:write", "channels:history"),
        )
        assert check.missing == ()
        assert check.known is True

    def test_足りないものを名指しする(self):
        check = slack_auth.check_scopes(
            self._identity(("chat:write",)), ("chat:write", "channels:history")
        )
        assert check.missing == ("channels:history",)

    def test_前方一致で通さない(self):
        # Slack には chat:write と **chat:write.public** の両方が実在する。
        # 前方一致で見ると、chat:write.public しか無いのに chat:write が
        # 「足りている」と報告され、実際の呼び出しで missing_scope になる。
        # zoom_auth の meeting:read:meeting と同じ形。
        check = slack_auth.check_scopes(
            self._identity(("chat:write.public",)), ("chat:write",)
        )
        assert check.missing == ("chat:write",)

    def test_余分なスコープは問題にしない(self):
        check = slack_auth.check_scopes(
            self._identity(("chat:write", "channels:history", "im:history")),
            ("chat:write",),
        )
        assert check.missing == ()

    def test_スコープが不明ならknownはfalse(self):
        check = slack_auth.check_scopes(self._identity(None), ("chat:write",))
        assert check.known is False

    def test_スコープが不明なら足りない扱いにする(self):
        # **ここがこのモジュールで一番大事な1件。**
        # 読めなかったときに missing を空にすると、呼び出し側が
        # `if check.missing:` と書いた瞬間、「確認できていない」が
        # 「足りている」に化ける。安全側（＝全部足りない）に倒す。
        check = slack_auth.check_scopes(
            self._identity(None), ("chat:write", "channels:history")
        )
        assert check.missing == ("chat:write", "channels:history")

    def test_確認できた場合とできない場合を区別できる(self):
        unknown = slack_auth.check_scopes(self._identity(None), ("chat:write",))
        missing = slack_auth.check_scopes(self._identity(()), ("chat:write",))
        # missing の中身は同じでも、known で理由を分けられること。
        # 分けられないと、利用者に出す直しかたが書けない。
        assert unknown.missing == missing.missing
        assert unknown.known != missing.known

    def test_確認する対象が空なら落ちる(self):
        # 何も指定せずに呼ぶと「全部足りている」が返る。呼び出し側の書き忘れを通さない。
        with pytest.raises(slack_auth.AuthError):
            slack_auth.check_scopes(self._identity(("chat:write",)), ())

    def test_付与済みスコープを持ち歩く(self):
        check = slack_auth.check_scopes(
            self._identity(("chat:write", "channels:history")), ("chat:write",)
        )
        # 何が付いていたかを出力に書けること（不明なら None のまま）。
        assert check.granted == ("chat:write", "channels:history")

    def test_不明なら付与済みもNone(self):
        check = slack_auth.check_scopes(self._identity(None), ("chat:write",))
        assert check.granted is None
