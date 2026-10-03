"""scout/cli のテスト。**実装より先に書いた。**

`cli` は8段をつないで1回分の実行をする入口。接続（Qiita・Gemini・LINE）はここで組み立て、
各段には差し込みで渡す。

決めたこと（2026-10-03）
--------------------------------------------------------------------------

============ ====================================================================
A            設定は `scout/config.toml`（`tomllib`）。venv に PyYAML が無いので YAML をやめた
B            設定は**外へ1回でも出る前に全部確かめる**。知らない鍵も誤りにする（書き間違いを黙って捨てない）
C            台帳 `state/seen.json` は **`emit` の読み戻しが問題なしのときだけ**更新する。
             書けなかった記事を「見た」にすると二度と来ない（M4）。**取れなかった取得元がある回は
             前回の日付を進めない**——進めると、その取得元のその日を飛ばす
D            初回は `--since` が必須。2回目からは台帳の前回の日付
E            時刻は**起動時に1回だけ**取る。run-id にも本文にも同じ値を使う（送り直しで2通にしない）
F            取得の失敗も `emit` の失敗も、**それでも送る**。設定の誤りだけは外へ出る前に止める
G            終了コード: 0＝正常・注意／1＝異常か送れなかった／2＝設定・台帳の誤り
H            `build_session` に timeout が無いので、get/post に timeout を足す薄い包みに入れる
I            `--dry-run` は Gemini も LINE も書き込みもしない。`--no-notify` は本文を出して、送らなかったと書く
J            接続ごと差し込む。**このファイルのどのテストも外へ接続できない**
============ ====================================================================
"""

from __future__ import annotations

import json
import socket
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote_plus

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scout"))

import cli  # noqa: E402
import fetch  # noqa: E402
import notify  # noqa: E402
import summarize  # noqa: E402
from common.gemini_client import Reply  # noqa: E402

#: **実行日と違う日**にしておく。
AT = datetime(2026, 9, 22, 21, 5, 0)
RUN_ID = "20260922-210500"
NOTE = "2026-09-22-scout.md"
TOKEN = "line-token-secret"
GEMINI_KEY = "gemini-key-secret"
TO = "U0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """J：**偽の接続を渡し忘れても、ここで止まる。**"""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("テストからネットワークへ接続しようとした")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

CONFIG = """
inbox = "inbox"
model = "gemini-test"
summarizable = ["qiita"]

[profile]
tags = ["Python"]
keywords = []
mute_tags = []
mute_keywords = []
cap = 10

[[sources]]
name = "qiita"
kind = "qiita"
query = "tag:Python"
limit = 20

[[sources]]
name = "zenn"
kind = "zenn"
query = "python"
limit = 20
"""


def _config_file(tmp_path: Path, text: str = CONFIG) -> Path:
    (tmp_path / "inbox").mkdir(exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_config_loads_and_resolves_inbox_from_the_config_file_folder(tmp_path: Path) -> None:
    config = cli.load_config(_config_file(tmp_path))
    assert config.inbox == tmp_path / "inbox"
    assert [s.name for s in config.sources] == ["qiita", "zenn"]
    assert config.sources[0] == fetch.Source(name="qiita", kind=fetch.QIITA, query="tag:Python", limit=20)
    assert config.summarizable == frozenset({"qiita"})
    assert config.profile.tags == frozenset({"Python"})
    assert config.profile.cap == 10
    assert config.model == "gemini-test"


def test_inbox_override_replaces_the_configured_one(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    config = cli.load_config(_config_file(tmp_path), inbox=other)
    assert config.inbox == other


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('model = "gemini-test"', 'model = "gemini-test"\nmodle = "typo"'),  # 知らない鍵
        ('summarizable = ["qiita"]', 'summarizable = ["qiita", "qita"]'),  # 取得元に無い名前
        ('summarizable = ["qiita"]', "summarizable = []"),
        ('name = "zenn"', 'name = "qiita"'),  # 取得元の名前が重複
        ('kind = "zenn"', 'kind = "hatena"'),  # 知らない種類
        ("limit = 20\n\n[[sources]]", "limit = 0\n\n[[sources]]"),
        ("limit = 20\n\n[[sources]]", "limit = true\n\n[[sources]]"),  # bool は int の仲間
        ('query = "tag:Python"', 'query = ""'),
        ("cap = 10", "cap = 0"),
        ('tags = ["Python"]', 'tags = "Python"'),  # 一覧でない
        ('tags = ["Python"]', "tags = [1]"),
        ('inbox = "inbox"', 'inbox = "nope"'),  # 実在しない
        ('model = "gemini-test"', 'model = ""'),
        ("[profile]", "[profle]"),  # 表の名前の書き間違い
    ],
)
def test_bad_config_is_refused(tmp_path: Path, old: str, new: str) -> None:
    assert CONFIG.count(old) == 1, old
    with pytest.raises(cli.ConfigError):
        cli.load_config(_config_file(tmp_path, CONFIG.replace(old, new)))


def test_config_without_sources_is_refused(tmp_path: Path) -> None:
    text = CONFIG.split("[[sources]]")[0]
    with pytest.raises(cli.ConfigError):
        cli.load_config(_config_file(tmp_path, text))


def test_broken_toml_is_a_config_error_not_a_raw_exception(tmp_path: Path) -> None:
    with pytest.raises(cli.ConfigError):
        cli.load_config(_config_file(tmp_path, "inbox = \n"))


def test_missing_config_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(cli.ConfigError):
        cli.load_config(tmp_path / "none.toml")


def test_shipped_config_is_valid_apart_from_the_inbox(tmp_path: Path) -> None:
    """**リポジトリに入れた設定は、クローンでも形として正しい。** Inbox だけは手元にしか無い
    （教訓 `do-not-depend-on-local-only-artifacts`）ので差し替えて確かめる。"""
    config = cli.load_config(ROOT / "scout" / "config.toml", inbox=tmp_path)
    assert config.sources
    assert config.summarizable <= {s.name for s in config.sources}


# ---------------------------------------------------------------------------
# 台帳
# ---------------------------------------------------------------------------


def test_missing_state_is_empty(tmp_path: Path) -> None:
    state = cli.load_state(tmp_path / "state" / "seen.json")
    assert state.seen == frozenset()
    assert state.last_run is None


def test_state_round_trips_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "state" / "seen.json"
    cli.save_state(path, cli.State(seen=frozenset({"a", "b"}), last_run=date(2026, 9, 21)))
    assert cli.load_state(path) == cli.State(seen=frozenset({"a", "b"}), last_run=date(2026, 9, 21))
    assert [p.name for p in path.parent.iterdir()] == ["seen.json"]


@pytest.mark.parametrize("text", ["{", "[]", '{"seen": "a"}', '{"seen": [], "last_run": "昨日"}'])
def test_broken_state_is_an_error_not_an_empty_ledger(tmp_path: Path, text: str) -> None:
    """**読めない台帳を空として続けない。** 空にすると、見た記事を全部もう一度送る。"""
    path = tmp_path / "seen.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(cli.StateError):
        cli.load_state(path)


# ---------------------------------------------------------------------------
# 偽の接続
# ---------------------------------------------------------------------------

LONG = "Python " + "本文" * 300


def _qiita_items(*urls: str) -> list[dict[str, object]]:
    return [
        {
            "title": f"記事 {i}",
            "url": url,
            "body": LONG,
            "created_at": "2026-09-22T12:00:00+09:00",
            "updated_at": None,
            "tags": [{"name": "Python"}],
            "likes_count": 0,
            "stocks_count": 0,
            "user": {"id": "someone"},
        }
        for i, url in enumerate(urls)
    ]


ZENN_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Zenn の記事</title>
    <description>冒頭だけ</description>
    <link>https://zenn.dev/someone/articles/xyz</link>
    <guid>https://zenn.dev/someone/articles/xyz</guid>
    <pubDate>Tue, 22 Sep 2026 03:00:00 GMT</pubDate>
    <dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">someone</dc:creator>
  </item>
</channel></rss>
"""


class _Resp:
    def __init__(self, status: int, payload: object = None, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Line:
    """**本物の宛先を持たない** LINE。送った本文を覚えるだけ。"""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.bodies: list[str] = []
        self.keys: list[str] = []

    def get(self, url: str, **kwargs: object) -> _Resp:
        return _Resp(200, {"totalUsage": 5})

    def post(self, url: str, **kwargs: object) -> _Resp:
        self.bodies.append(kwargs["json"]["messages"][0]["text"])  # type: ignore[index]
        self.keys.append(kwargs["headers"]["X-Line-Retry-Key"])  # type: ignore[index]
        if self.status != 200:
            return _Resp(self.status, {"message": f"boom {TOKEN}"})
        return _Resp(200, {"sentMessages": [{"id": f"m-{len(self.bodies)}", "quoteToken": "q"}]}, {"x-line-request-id": "r"})


class _World:
    """外の世界の偽物。呼ばれた回数と中身を覚える。"""

    def __init__(
        self,
        *,
        qiita: tuple[str, ...] = ("https://qiita.com/a/items/1",),
        zenn_status: int = 200,
        summary: str = "Python の記事。",
        finish: str = "STOP",
        line_status: int = 200,
    ) -> None:
        self.qiita = qiita
        self.zenn_status = zenn_status
        self.summary = summary
        self.finish = finish
        self.line = _Line(line_status)
        self.urls: list[str] = []
        self.prompts: list[str] = []
        self.connects: list[bool] = []

    def get(self, url: str) -> fetch.Response:
        self.urls.append(url)
        if "qiita.com" in url:
            payload = json.dumps(_qiita_items(*self.qiita), ensure_ascii=False)
            return fetch.Response(status=200, text=payload, headers={"Total-Count": str(len(self.qiita))})
        if "zenn.dev" in url:
            return fetch.Response(status=self.zenn_status, text=ZENN_FEED, headers={})
        raise AssertionError(url)

    def call(self, prompt: str) -> Reply:
        self.prompts.append(prompt)
        text = json.dumps({"summary": self.summary, "quotes": []}, ensure_ascii=False)
        return Reply(text=text, finish_reason=self.finish, prompt_tokens=10, output_tokens=5)

    def connect(self, config: cli.Config, *, remote: bool) -> cli.Connections:
        self.connects.append(remote)
        if not remote:
            return cli.Connections(get=self.get, call=None, session=None, to="", secrets=())
        return cli.Connections(get=self.get, call=self.call, session=self.line, to=TO, secrets=(TOKEN, GEMINI_KEY))


class _Clock:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return AT


def _main(
    tmp_path: Path, world: _World, *args: str, clock: _Clock | None = None, since: str | None = "2026-09-21"
) -> int:
    config = tmp_path / "config.toml"
    if not config.exists():
        _config_file(tmp_path)
    argv = ["--config", str(config), "--state", str(tmp_path / "state" / "seen.json"), *args]
    if since is not None:
        argv += ["--since", since]
    return cli.main(argv, connect=world.connect, now=clock or _Clock())


def _note(tmp_path: Path) -> str:
    return (tmp_path / "inbox" / NOTE).read_bytes().decode("utf-8")


def _state(tmp_path: Path) -> cli.State:
    return cli.load_state(tmp_path / "state" / "seen.json")


# ---------------------------------------------------------------------------
# 通しで動く
# ---------------------------------------------------------------------------


def test_full_run_writes_the_note_sends_once_and_saves_the_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world = _World()
    code = _main(tmp_path, world)
    out = capsys.readouterr().out
    assert code == 0
    assert f"実行 `{RUN_ID}`" in _note(tmp_path)
    assert "](https://qiita.com/a/items/1)" in _note(tmp_path)
    assert "](https://zenn.dev/someone/articles/xyz)" in _note(tmp_path)
    assert len(world.line.bodies) == 1
    assert world.line.bodies[0].startswith("【scout】正常｜2026-09-22 21:05")
    assert len(world.prompts) == 1  # Qiita の1件だけ要約する（Zenn は本文が無い）
    state = _state(tmp_path)
    assert len(state.seen) == 2
    assert state.last_run == date(2026, 9, 22)
    assert "m-1" in out


def test_second_run_drops_what_was_seen_and_still_notifies(tmp_path: Path) -> None:
    world = _World()
    _main(tmp_path, world)
    later_at = datetime(2026, 9, 22, 23, 0, 0)
    code = cli.main(
        ["--config", str(tmp_path / "config.toml"), "--state", str(tmp_path / "state" / "seen.json")],
        connect=world.connect,
        now=lambda: later_at,
    )
    assert code == 0
    assert len(world.prompts) == 1  # 2回目は要約しない
    assert len(world.line.bodies) == 2  # **0件でも送る**
    assert "記事 0 件" in _note(tmp_path)


def test_clock_is_read_once_and_the_same_time_is_used_everywhere(tmp_path: Path) -> None:
    clock = _Clock()
    world = _World()
    _main(tmp_path, world, clock=clock)
    assert clock.calls == 1
    assert "## 21:05 実行" in _note(tmp_path)
    assert "21:05" in world.line.bodies[0]
    assert RUN_ID in world.line.bodies[0]


def test_since_comes_from_the_ledger_after_the_first_run(tmp_path: Path) -> None:
    cli.save_state(tmp_path / "state" / "seen.json", cli.State(seen=frozenset(), last_run=date(2026, 9, 20)))
    world = _World()
    _main(tmp_path, world, since=None)
    qiita = [unquote_plus(u) for u in world.urls if "qiita.com" in u]
    assert "created:>=2026-09-20" in qiita[0]


def test_since_option_overrides_the_ledger(tmp_path: Path) -> None:
    cli.save_state(tmp_path / "state" / "seen.json", cli.State(seen=frozenset(), last_run=date(2026, 9, 20)))
    world = _World()
    _main(tmp_path, world, since="2026-09-01")
    qiita = [unquote_plus(u) for u in world.urls if "qiita.com" in u]
    assert "created:>=2026-09-01" in qiita[0]


def test_inbox_option_writes_elsewhere(tmp_path: Path) -> None:
    other = tmp_path / "scratch"
    other.mkdir()
    _main(tmp_path, _World(), "--inbox", str(other))
    assert (other / NOTE).exists()
    assert not (tmp_path / "inbox" / NOTE).exists()


# ---------------------------------------------------------------------------
# 外へ出る前に止める
# ---------------------------------------------------------------------------


def test_first_run_without_since_stops_before_connecting(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world = _World()
    code = _main(tmp_path, world, since=None)
    assert code == 2
    assert world.connects == []
    assert not (tmp_path / "inbox" / NOTE).exists()
    assert "--since" in capsys.readouterr().err


def test_bad_since_is_a_usage_error(tmp_path: Path) -> None:
    world = _World()
    assert _main(tmp_path, world, since="2026/09/21") == 2
    assert world.connects == []


def test_bad_config_stops_before_connecting(tmp_path: Path) -> None:
    _config_file(tmp_path, CONFIG.replace("cap = 10", "cap = 0"))
    world = _World()
    assert _main(tmp_path, world) == 2
    assert world.connects == []


def test_broken_ledger_stops_before_connecting(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "seen.json").write_text("{", encoding="utf-8")
    world = _World()
    assert _main(tmp_path, world) == 2
    assert world.connects == []


def test_missing_inbox_override_is_a_config_error(tmp_path: Path) -> None:
    world = _World()
    assert _main(tmp_path, world, "--inbox", str(tmp_path / "nope")) == 2
    assert world.connects == []


# ---------------------------------------------------------------------------
# I：試し方
# ---------------------------------------------------------------------------


def test_dry_run_calls_no_gemini_no_line_and_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world = _World()
    code = _main(tmp_path, world, "--dry-run")
    assert code == 0
    assert world.connects == [False]
    assert world.prompts == []
    assert world.line.bodies == []
    assert not (tmp_path / "inbox" / NOTE).exists()
    assert not (tmp_path / "state" / "seen.json").exists()
    assert "dry-run" in capsys.readouterr().out


def test_no_notify_prints_the_body_and_says_it_was_not_sent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world = _World()
    code = _main(tmp_path, world, "--no-notify")
    out = capsys.readouterr().out
    assert code == 0
    assert world.line.bodies == []
    assert "【scout】正常｜2026-09-22 21:05" in out
    assert "送っていない" in out
    assert (tmp_path / "inbox" / NOTE).exists()


# ---------------------------------------------------------------------------
# F・G：失敗しても送る・終了コード
# ---------------------------------------------------------------------------


def test_failed_source_still_writes_and_sends_but_does_not_advance_the_date(tmp_path: Path) -> None:
    cli.save_state(tmp_path / "state" / "seen.json", cli.State(seen=frozenset(), last_run=date(2026, 9, 21)))
    world = _World(zenn_status=503)
    code = _main(tmp_path, world, since=None)
    assert code == 1
    assert world.line.bodies[0].startswith("【scout】異常｜")
    state = _state(tmp_path)
    assert state.last_run == date(2026, 9, 21)  # 進めない
    assert "https://qiita.com/a/items/1" in state.seen  # 書いた記事は「見た」にする


def test_emit_error_still_sends_and_leaves_the_ledger(tmp_path: Path) -> None:
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / NOTE).write_text(
        f"---\ntags:\n  - scout\ndate: 2026-09-22\n---\n\n## 21:05 実行 `{RUN_ID}`\n", encoding="utf-8"
    )
    world = _World()
    code = _main(tmp_path, world)
    assert code == 1
    assert "Inbox に書けなかった" in world.line.bodies[0]
    assert not (tmp_path / "state" / "seen.json").exists()


def test_readback_problem_sends_abnormal_and_leaves_the_ledger(tmp_path: Path) -> None:
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / NOTE).write_text("# 手で作った\n", encoding="utf-8")
    world = _World()
    code = _main(tmp_path, world)
    assert code == 1
    assert world.line.bodies[0].startswith("【scout】異常｜")
    assert not (tmp_path / "state" / "seen.json").exists()


def test_summary_failure_is_abnormal_and_still_sends(tmp_path: Path) -> None:
    world = _World(finish="MAX_TOKENS")
    assert _main(tmp_path, world) == 1
    assert len(world.line.bodies) == 1


def test_attention_exits_zero(tmp_path: Path) -> None:
    """**注意で毎日赤くしない。** 本物の異常に慣れてしまう。"""
    world = _World(summary="短い要約。")  # 数字も英語の語も無い＝確認できない
    assert _main(tmp_path, world) == 0
    assert world.line.bodies[0].startswith("【scout】注意｜")


def test_line_failure_exits_one_but_the_ledger_is_saved(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """**書けた記事は「見た」にする。** 送れなかったのは通知の問題で、記事は Inbox にある。"""
    world = _World(line_status=500)
    code = _main(tmp_path, world)
    captured = capsys.readouterr()
    assert code == 1
    assert _state(tmp_path).seen
    assert TOKEN not in captured.out + captured.err
    assert "送れなかった" in captured.err


def test_ledger_save_failure_exits_one_and_is_in_the_notification(tmp_path: Path) -> None:
    (tmp_path / "state" / "seen.json").mkdir(parents=True)  # ファイルの場所がフォルダ
    world = _World()
    code = _main(tmp_path, world)
    assert code == 1
    assert "台帳" in world.line.bodies[0]


def test_secrets_never_reach_the_screen(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _main(tmp_path, _World(), "--no-notify")
    captured = capsys.readouterr()
    for secret in (TOKEN, GEMINI_KEY):
        assert secret not in captured.out + captured.err


# ---------------------------------------------------------------------------
# H：本物の接続の部品（外へは出ない）
# ---------------------------------------------------------------------------


class _Inner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> str:
        self.calls.append(("GET", kwargs))
        return "g"

    def post(self, url: str, **kwargs: object) -> str:
        self.calls.append(("POST", kwargs))
        return "p"


def test_timeout_session_adds_a_timeout_to_get_and_post() -> None:
    inner = _Inner()
    session = cli.TimeoutSession(inner, timeout=7)
    assert session.get("u") == "g"
    assert session.post("u", json={}) == "p"
    assert inner.calls == [("GET", {"timeout": 7}), ("POST", {"json": {}, "timeout": 7})]


def test_timeout_session_keeps_an_explicit_timeout() -> None:
    inner = _Inner()
    cli.TimeoutSession(inner, timeout=7).get("u", timeout=1)
    assert inner.calls == [("GET", {"timeout": 1})]


def test_http_get_passes_a_timeout_and_decodes_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class _R:
        status_code = 200
        content = "日本語".encode("utf-8")
        headers = {"Total-Count": "3"}

    def fake_get(url: str, **kwargs: object) -> _R:
        seen.update(kwargs, url=url)
        return _R()

    monkeypatch.setattr(cli.requests, "get", fake_get)
    reply = cli.http_get("https://qiita.com/api/v2/items")
    assert reply == fetch.Response(status=200, text="日本語", headers={"Total-Count": "3"})
    assert seen["timeout"] == cli.TIMEOUT
    assert "User-Agent" in seen["headers"]  # type: ignore[operator]


def test_gemini_caller_asks_for_the_summary_schema_with_the_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: dict[str, object] = {}
    reply = Reply(text="{}", finish_reason="STOP", prompt_tokens=1, output_tokens=1)

    def fake_generate_json(client: object, **kwargs: object) -> Reply:
        asked.update(kwargs, client=client)
        return reply

    monkeypatch.setattr(cli.gemini_client, "generate_json", fake_generate_json)
    client = object()
    call = cli.gemini_caller(client, model="gemini-test", api_key=GEMINI_KEY)
    assert call("要約して") is reply
    assert asked == {
        "client": client,
        "prompt": "要約して",
        "schema": summarize.SCHEMA,
        "model": "gemini-test",
        "api_key": GEMINI_KEY,  # 例外の文言からキーを伏せるのに使う
    }
