"""common/google_auth のテスト。

**lesson-4-3-2 から、期待値を1つも書き換えずに移した。**
実装（``common/google_auth.py``）と対で移植している。

書き直さなかった理由は ``common/line_send.py`` と同じ。
新しい実装を見ながら期待値を作ると「実装がこうなっているから、こう期待する」になり、
**実機で1度通った知識が失われる**。ここは Section 4-3 の課題3・5・6・10 が
実際に通した期待値そのものである。

フィクスチャのスコープ文字列（``meetings.space.created`` / ``documents``）も
そのまま残してある。**課題2で使う Sheets のスコープに置き換えていない**——
このテストが確かめているのは「**要求と保存済みの権限が食い違えば取り直す**」という
振る舞いであって、特定のスコープ名ではないため。値を今の課題に寄せると、
移植で何も変えていないという事実が読み取れなくなる。

課題1・課題2では認証まわりを課題ごとにコピーしていたので、テストも課題ごとにあった。
共有した以上、ここが落ちれば全部の課題が落ちる。**共有モジュールに自前のテストが
無いと、壊しても誰も気づかない**ので、切り出しと同時に書いた。

本物の Google には繋がない。flow と refresher は差し替えられるようにしてある。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import google_auth  # noqa: E402


SCOPES = ("https://www.googleapis.com/auth/meetings.space.created",)
OTHER_SCOPES = ("https://www.googleapis.com/auth/documents",)


class FakeCredentials:
    def __init__(self, *, valid=True, expired=False, refresh_token="R", scopes=None) -> None:
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self._scopes = list(scopes or SCOPES)
        self.refreshed = False

    def has_scopes(self, wanted) -> bool:
        return set(wanted).issubset(set(self._scopes))

    def to_json(self) -> str:
        return json.dumps({"token": "T", "scopes": self._scopes})


class FakeFlow:
    def __init__(self) -> None:
        self.ports: list[int] = []

    def run_local_server(self, port=0):
        self.ports.append(port)
        return FakeCredentials(valid=True)


# ================================================================ トークンの読み書き


class TestReadToken:
    def test_ファイルが無ければNone(self, tmp_path: Path):
        assert google_auth.read_token(tmp_path / "token.json") is None

    def test_壊れたJSONならNone(self, tmp_path: Path):
        token = tmp_path / "token.json"
        token.write_text("{ not json", encoding="utf-8")
        assert google_auth.read_token(token) is None

    def test_保存された権限をそのまま読む(self, tmp_path: Path):
        # from_authorized_user_file に scopes を渡さないことの証拠。
        # 渡すと、ファイルに書かれた実際の権限が引数で上書きされ、
        # 「権限が足りているか」の判定が常に真になる。
        token = tmp_path / "token.json"
        token.write_text(
            json.dumps(
                {
                    "type": "authorized_user",
                    "client_id": "CID",
                    "client_secret": "CSECRET",
                    "refresh_token": "RT",
                    "scopes": list(OTHER_SCOPES),
                }
            ),
            encoding="utf-8",
        )
        credentials = google_auth.read_token(token)
        assert credentials is not None
        assert not credentials.has_scopes(list(SCOPES))


class TestSaveToken:
    def test_保存する(self, tmp_path: Path):
        token = tmp_path / "token.json"
        google_auth.save_token(token, FakeCredentials())
        assert token.exists()

    def test_親ディレクトリが無くても作る(self, tmp_path: Path):
        token = tmp_path / "nested" / "token.json"
        google_auth.save_token(token, FakeCredentials())
        assert token.exists()


# ================================================================ load_credentials


class TestLoadCredentials:
    @pytest.fixture(autouse=True)
    def _patch_reader(self, monkeypatch):
        """token.json の読み込みだけ差し替える。google-auth の実物は通さない。"""
        self.stored: dict[str, FakeCredentials] = {}

        def fake_read(token_path):
            return self.stored.get(str(token_path))

        monkeypatch.setattr(google_auth, "read_token", fake_read)

    def _with_credentials_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "credentials.json"
        path.write_text("{}", encoding="utf-8")
        return path

    def test_有効なトークンならブラウザを開かない(self, tmp_path: Path):
        token = tmp_path / "token.json"
        self.stored[str(token)] = FakeCredentials(valid=True)
        called: list[int] = []

        google_auth.load_credentials(
            tmp_path / "credentials.json",
            token,
            SCOPES,
            flow_factory=lambda *a, **k: called.append(1),
        )
        assert called == []

    def test_期限切れならリフレッシュする(self, tmp_path: Path):
        token = tmp_path / "token.json"
        credentials = FakeCredentials(valid=False, expired=True, refresh_token="R")
        self.stored[str(token)] = credentials

        def refresher(c):
            c.refreshed = True
            c.valid = True

        google_auth.load_credentials(
            tmp_path / "credentials.json", token, SCOPES, refresher=refresher
        )
        assert credentials.refreshed

    def test_リフレッシュしたトークンを保存し直す(self, tmp_path: Path):
        token = tmp_path / "token.json"
        self.stored[str(token)] = FakeCredentials(valid=False, expired=True, refresh_token="R")

        google_auth.load_credentials(
            tmp_path / "credentials.json", token, SCOPES, refresher=lambda c: None
        )
        assert token.exists()

    def test_権限が足りなければ同意を取り直す(self, tmp_path: Path):
        # 課題2の token.json は documents しか持っていない。
        # meetings.space.created を要求したら必ず取り直しになる。
        token = tmp_path / "token.json"
        self.stored[str(token)] = FakeCredentials(valid=True, scopes=OTHER_SCOPES)
        flow = FakeFlow()

        google_auth.load_credentials(
            self._with_credentials_file(tmp_path), token, SCOPES, flow_factory=lambda *a, **k: flow
        )
        assert flow.ports

    def test_要求するスコープをflowに渡す(self, tmp_path: Path):
        passed: list[list[str]] = []

        def factory(path, scopes):
            passed.append(list(scopes))
            return FakeFlow()

        google_auth.load_credentials(
            self._with_credentials_file(tmp_path),
            tmp_path / "token.json",
            SCOPES,
            flow_factory=factory,
        )
        assert passed == [list(SCOPES)]

    def test_取り直したトークンを保存する(self, tmp_path: Path):
        token = tmp_path / "token.json"
        google_auth.load_credentials(
            self._with_credentials_file(tmp_path),
            token,
            SCOPES,
            flow_factory=lambda *a, **k: FakeFlow(),
        )
        assert token.exists()

    def test_credentialsが無ければ案内つきで失敗する(self, tmp_path: Path):
        with pytest.raises(google_auth.AuthError) as caught:
            google_auth.load_credentials(
                tmp_path / "credentials.json", tmp_path / "token.json", SCOPES
            )
        assert "credentials.json" in str(caught.value)

    def test_スコープが空なら失敗する(self, tmp_path: Path):
        # 既定値を持たせていないので、呼ぶ側が渡し忘れると空で来る。
        # 空のまま進むと「権限を要求していないのに動いているように見える」形になる。
        with pytest.raises(google_auth.AuthError):
            google_auth.load_credentials(
                self._with_credentials_file(tmp_path), tmp_path / "token.json", []
            )

    def test_スコープが空なら同意画面を開かない(self, tmp_path: Path):
        called: list[int] = []
        with pytest.raises(google_auth.AuthError):
            google_auth.load_credentials(
                self._with_credentials_file(tmp_path),
                tmp_path / "token.json",
                [],
                flow_factory=lambda *a, **k: called.append(1),
            )
        assert called == []
