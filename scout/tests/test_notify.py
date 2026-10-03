"""scout/notify のテスト。**実装より先に書いた。**

`notify` は1回の実行の結果を LINE に**1通**送る段。**0件でも、途中で失敗しても送る**
——*無音は「今日は何も無かった」と「収集が死んだ」を区別できない*（M7）。

決めたこと（2026-10-03）
--------------------------------------------------------------------------

============ ====================================================================
A            状態は**各段の結果から自分で判定する**。呼び出し側から「正常」を受け取らない
B            `emit` が書けなかった回も送る（その文言を載せる）
C            記事のタイトルは載せない。通知は入口で、中身は Inbox にある
D            `X-Line-Retry-Key` は **run-id と本文から決まる UUID**。公式（Retry failed API
             requests）が「同じキーで中身を変えるな」と書いているので、本文も混ぜる
E            **409 は `x-line-accepted-request-id` があるときだけ**「送ってあった」として
             成功にする。公式によると push の 409 は最初と同じ `sentMessages.id` を返す
F            通数が読めなくても**送る**。課題3 と違い、ここで守りたいのは証拠より無音でないこと
G            `session` は差し込み。**このテストは `.env` を読まず、ネットワークにも出ない**
             （教訓 `isolate-outbound-destinations-in-tests`）
============ ====================================================================
"""

from __future__ import annotations

import socket
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scout"))

import dedupe  # noqa: E402
import emit  # noqa: E402
import fetch  # noqa: E402
import notify  # noqa: E402
import rank  # noqa: E402
import summarize  # noqa: E402
import verify_source  # noqa: E402
from common import line_auth, line_send  # noqa: E402

AT = datetime(2026, 9, 22, 21, 5, 0)
TO = "U0123456789abcdef0123456789abcdef"
TOKEN = "tok-secret-123"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """G：**このファイルのどのテストも外へ接続できない。** 偽の session を渡し忘れても、ここで止まる。

    ソースを grep するだけでは、`requests.Session()` の直接生成や別経路の接続に気づけない。
    """

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("テストからネットワークへ接続しようとした")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------


def _source(name: str = "qiita", status: str = fetch.OK, *, more: bool = False, detail: str = "") -> fetch.SourceResult:
    return fetch.SourceResult(source=name, status=status, articles=(), detail=detail, total=None, more=more)


def _harvest(*results: fetch.SourceResult) -> fetch.Harvest:
    return fetch.Harvest(results=results if results else (_source(),))


def _scored(url: str) -> rank.Scored:
    article = fetch.Article(
        source="qiita", url=url, title="記事", body="本文" * 300,
        published_at=AT, updated_at=None, author="a", tags=(), metrics={},
    )
    return rank.Scored(kept=dedupe.Kept(article=article, key=url), score=1, hits=())


def _summary(url: str = "https://qiita.com/x/items/1") -> summarize.Summary:
    return summarize.Summary(scored=_scored(url), text="要約", quotes=(), prompt_tokens=1, output_tokens=1)


def _digest(*, failed: int = 0, audit: verify_source.Audit | None = None) -> summarize.Digest:
    """**照合した要約と同じものを `done` に置く。** ずれた組は、それ自体が異常になる。"""
    failures = tuple(
        summarize.Failure(scored=_scored(f"https://qiita.com/f/items/{i}"), reason=summarize.NOT_FINISHED, detail="")
        for i in range(failed)
    )
    done = tuple(c.summary for c in audit.checks) if audit else ()
    return summarize.Digest(done=done, failed=failures)


def _audit(*verdicts: str) -> verify_source.Audit:
    return verify_source.Audit(
        checks=tuple(
            verify_source.Check(summary=_summary(f"https://qiita.com/a/items/{i}"), verdict=v, found=(), missing=(), quotes_missing=())
            for i, v in enumerate(verdicts)
        )
    )


def _emitted(*problems: str) -> emit.Emitted:
    return emit.Emitted(path=Path("2026-09-22-scout.md"), created=True, entries=3, problems=problems)


def _judge(**kwargs: object) -> notify.Health:
    args: dict[str, object] = {
        "harvest": _harvest(),
        "digest": _digest(),
        "audit": _audit(),
        "emitted": _emitted(),
        "emit_error": None,
    }
    args.update(kwargs)
    if "audit" in kwargs and "digest" not in kwargs:
        args["digest"] = _digest(audit=kwargs["audit"])  # type: ignore[arg-type]
    return notify.judge(**args)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# A・B：判定
# ---------------------------------------------------------------------------


def test_zero_articles_with_every_source_answering_is_normal() -> None:
    health = _judge(harvest=_harvest(_source(status=fetch.EMPTY)))
    assert health.level == notify.NORMAL
    assert health.reasons == ()


def test_a_failed_source_is_abnormal_even_if_others_answered() -> None:
    health = _judge(harvest=_harvest(_source("qiita"), _source("zenn", fetch.FAILED, detail="HTTP 503")))
    assert health.level == notify.ABNORMAL
    assert any("zenn" in r and "HTTP 503" in r for r in health.reasons)


def test_no_sources_at_all_is_abnormal() -> None:
    """**1件も処理しなかったのに成功**にしない（M9）。"""
    health = _judge(harvest=fetch.Harvest(results=()))
    assert health.level == notify.ABNORMAL
    assert health.reasons


def test_summary_failures_are_abnormal() -> None:
    health = _judge(digest=_digest(failed=2))
    assert health.level == notify.ABNORMAL
    assert any("2 件" in r for r in health.reasons)


def test_emit_error_is_abnormal() -> None:
    health = _judge(emitted=None, emit_error="Inbox が無い")
    assert health.level == notify.ABNORMAL
    assert any("書けなかった" in r for r in health.reasons)


def test_readback_problems_are_abnormal() -> None:
    health = _judge(emitted=_emitted("記事が 2 件（期待は 3 件）"))
    assert health.level == notify.ABNORMAL
    assert any("読み戻し" in r and "1 件" in r for r in health.reasons)


@pytest.mark.parametrize("verdict", [verify_source.MISMATCH, verify_source.UNVERIFIABLE])
def test_unconfirmed_summaries_need_attention(verdict: str) -> None:
    health = _judge(audit=_audit(verify_source.CONFIRMED, verdict))
    assert health.level == notify.ATTENTION
    assert any("1 件" in r for r in health.reasons)


def test_truncated_source_needs_attention() -> None:
    """**まだ先がある**＝取りこぼし（H1・M1）。"""
    health = _judge(harvest=_harvest(_source("qiita", more=True)))
    assert health.level == notify.ATTENTION
    assert any("qiita" in r for r in health.reasons)


def test_abnormal_wins_over_attention_and_both_reasons_are_kept() -> None:
    audit = _audit(verify_source.MISMATCH)
    health = _judge(digest=_digest(failed=1, audit=audit), audit=audit)
    assert health.level == notify.ABNORMAL
    assert len(health.reasons) == 2


def test_unknown_source_status_is_abnormal_not_normal() -> None:
    """`fetch` が将来別の状態を足した日に、**知らないものを正常に倒さない。**"""
    health = _judge(harvest=_harvest(_source("qiita", "throttled")))
    assert health.level == notify.ABNORMAL
    assert any("qiita" in r and "throttled" in r for r in health.reasons)


def test_audit_that_does_not_cover_every_summary_is_abnormal() -> None:
    """要約が1件あるのに照合が0件——**照合が走っていない**。0件の不一致は正常に見える。"""
    digest = summarize.Digest(done=(_summary(),), failed=())
    health = _judge(digest=digest, audit=_audit())
    assert health.level == notify.ABNORMAL
    assert any("照合" in r for r in health.reasons)


@pytest.mark.parametrize(
    ("emitted", "emit_error"),
    [(None, None), (_emitted(), "x")],
)
def test_emitted_and_emit_error_must_be_exactly_one(emitted: object, emit_error: object) -> None:
    with pytest.raises(ValueError):
        _judge(emitted=emitted, emit_error=emit_error)


# ---------------------------------------------------------------------------
# C：本文
# ---------------------------------------------------------------------------


def _compose(**kwargs: object) -> str:
    args: dict[str, object] = {
        "at": AT,
        "run_id": "r1",
        "health": notify.Health(level=notify.NORMAL, reasons=()),
        "stages": ("fetch: qiita ok", "dedupe: 3 件中 3 件"),
        "emitted": _emitted(),
        "emit_error": None,
    }
    args.update(kwargs)
    return notify.compose(**args)  # type: ignore[arg-type]


def test_first_line_says_level_and_time() -> None:
    body = _compose()
    assert body.split("\n", 1)[0] == "【scout】正常｜2026-09-22 21:05"


def test_body_has_run_id_stages_and_emit_result() -> None:
    body = _compose()
    assert "r1" in body
    assert "fetch: qiita ok" in body
    assert "dedupe: 3 件中 3 件" in body
    assert _emitted().summary in body


def test_reasons_are_listed_when_not_normal() -> None:
    health = notify.Health(level=notify.ABNORMAL, reasons=("取得元 zenn が取れなかった", "要約できなかった 1 件"))
    body = _compose(health=health)
    assert body.startswith("【scout】異常｜")
    assert "取得元 zenn が取れなかった" in body
    assert "要約できなかった 1 件" in body


def test_attention_label() -> None:
    body = _compose(health=notify.Health(level=notify.ATTENTION, reasons=("照合できなかった 1 件",)))
    assert body.startswith("【scout】注意｜")


def test_emit_error_is_shown_on_one_line() -> None:
    body = _compose(emitted=None, emit_error="Inbox が無い:\nC:/x")
    assert "Inbox に書けなかった: Inbox が無い: C:/x" in body


def test_stage_lines_are_flattened() -> None:
    body = _compose(stages=("fetch: 1行目\n2行目",))
    assert "fetch: 1行目 2行目" in body


# ---------------------------------------------------------------------------
# D：再送キー
# ---------------------------------------------------------------------------


def test_retry_key_is_a_stable_uuid_of_run_and_body() -> None:
    key = notify.retry_key("r1", "本文")
    assert str(uuid.UUID(key)) == key
    assert notify.retry_key("r1", "本文") == key


def test_retry_key_changes_with_body_and_with_run() -> None:
    """**同じキーで中身を変えるな**（公式）。本文が変われば別のキーにする。"""
    key = notify.retry_key("r1", "本文")
    assert notify.retry_key("r1", "本文2") != key
    assert notify.retry_key("r2", "本文") != key


def test_retry_key_does_not_confuse_run_and_body_boundary() -> None:
    assert notify.retry_key("r1", "2本文") != notify.retry_key("r12", "本文")


def test_retry_key_does_not_collide_when_run_id_has_a_newline() -> None:
    """**`emit` が run-id を弾いた回も送る**ので、ここには検証前の run-id が来る。"""
    assert notify.retry_key("a\nb", "c") != notify.retry_key("a", "b\nc")


# ---------------------------------------------------------------------------
# E・F・G：送る（偽の session だけ）
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status: int, payload: object = None, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Session:
    """**本物の宛先を持たない。** 呼ばれた順と中身を覚えるだけ。"""

    def __init__(self, post: _Response, usage: list[_Response] | None = None) -> None:
        self._post = post
        self._usage = list(usage) if usage is not None else [_usage(10), _usage(11)]
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append(("GET", url, kwargs))
        return self._usage.pop(0)

    def post(self, url: str, **kwargs: object) -> _Response:
        self.calls.append(("POST", url, kwargs))
        return self._post


def _usage(n: int) -> _Response:
    return _Response(200, {"totalUsage": n})


def _ok(message_id: str = "m-1") -> _Response:
    return _Response(200, {"sentMessages": [{"id": message_id, "quoteToken": "q"}]}, {"x-line-request-id": "req-1"})


def _send(session: _Session, body: str = "本文") -> notify.Delivered:
    return notify.send(session, to=TO, body=body, run_id="r1", secrets=(TOKEN,))


def _posts(session: _Session) -> list[dict[str, object]]:
    return [kwargs for method, _, kwargs in session.calls if method == "POST"]


def test_send_posts_once_with_retry_key_from_run_and_body() -> None:
    session = _Session(_ok())
    delivered = _send(session)
    posts = _posts(session)
    assert len(posts) == 1
    assert posts[0]["json"] == {"to": TO, "messages": [{"type": "text", "text": "本文"}]}
    assert posts[0]["headers"] == {"X-Line-Retry-Key": notify.retry_key("r1", "本文")}
    assert [url for _, url, _ in session.calls] == [
        line_auth.API_BASE + line_send.CONSUMPTION_PATH,
        line_auth.API_BASE + line_send.PUSH_PATH,
        line_auth.API_BASE + line_send.CONSUMPTION_PATH,
    ]
    assert delivered.message_id == "m-1"
    assert delivered.request_id == "req-1"
    assert delivered.duplicate is False
    assert delivered.usage_before == 10
    assert delivered.usage_after == 11
    assert delivered.to_masked == line_send.mask_destination(TO)
    assert TO not in delivered.summary


def test_409_with_accepted_request_id_is_already_sent_not_a_failure() -> None:
    conflict = _Response(
        409,
        {"message": "The retry key is already accepted", "sentMessages": [{"id": "m-first", "quoteToken": "q"}]},
        {"x-line-request-id": "req-2", "x-line-accepted-request-id": "req-1"},
    )
    delivered = _send(_Session(conflict, [_usage(11), _usage(11)]))
    assert delivered.duplicate is True
    assert delivered.message_id == "m-first"
    assert delivered.accepted_request_id == "req-1"
    assert "送ってあった" in delivered.summary


def test_409_without_accepted_request_id_is_a_failure() -> None:
    conflict = _Response(409, {"message": "conflict"}, {"x-line-request-id": "req-2"})
    with pytest.raises(line_auth.ApiError):
        _send(_Session(conflict))


def test_409_with_accepted_id_but_without_sent_messages_is_a_failure() -> None:
    """送ってあったと言うなら、**何を送ってあったか**の ID が要る。"""
    conflict = _Response(409, {"message": "x"}, {"x-line-accepted-request-id": "req-1"})
    with pytest.raises(line_send.SendError):
        _send(_Session(conflict))


def test_server_error_is_raised_and_secret_is_not_in_the_message() -> None:
    error = _Response(500, {"message": f"bad token {TOKEN}"}, {"x-line-request-id": "req-3"})
    with pytest.raises(line_auth.ApiError) as raised:
        _send(_Session(error))
    assert TOKEN not in str(raised.value)


def test_200_without_sent_messages_is_a_failure() -> None:
    with pytest.raises(line_send.SendError):
        _send(_Session(_Response(200, {})))


def test_empty_body_is_refused_before_anything_is_sent() -> None:
    session = _Session(_ok())
    with pytest.raises(line_send.SendError):
        _send(session, body="  \n")
    assert _posts(session) == []


def test_usage_after_failing_still_reports_the_send() -> None:
    """**送ったものは取り消せない。** 後で数えられなくても、送ったことは返す。"""
    session = _Session(_ok(), [_usage(10), _Response(500, {"message": "x"})])
    delivered = _send(session)
    assert delivered.message_id == "m-1"
    assert delivered.usage_after is None
    assert "読めず" in delivered.summary


def test_usage_before_failing_does_not_stop_the_send() -> None:
    """F：**無音にしない**ほうを取る。数えられないことは結果に残す。"""
    session = _Session(_ok(), [_Response(500, {"message": "x"}), _usage(11)])
    delivered = _send(session)
    assert len(_posts(session)) == 1
    assert delivered.usage_before is None
    assert delivered.usage_after == 11


class _Broken(_Session):
    """通信そのものが失敗する session。**HTTP の応答が返らない**経路。"""

    def __init__(self, *, get_fails: tuple[bool, bool] = (False, False), post_fails: bool = False) -> None:
        super().__init__(_ok())
        self._get_fails = list(get_fails)
        self._post_fails = post_fails

    def get(self, url: str, **kwargs: object) -> _Response:
        if self._get_fails.pop(0):
            raise requests.ConnectionError("connection refused")
        return super().get(url, **kwargs)

    def post(self, url: str, **kwargs: object) -> _Response:
        if self._post_fails:
            raise requests.ConnectionError(f"failed with {TOKEN}")
        return super().post(url, **kwargs)


def test_usage_before_connection_error_does_not_stop_the_send() -> None:
    """**HTTP 500 だけ真似ても足りない。** 応答が返らない通信の例外も握る（2026-10-03 のレビュー）。"""
    session = _Broken(get_fails=(True, False))
    delivered = _send(session)
    assert len(_posts(session)) == 1
    assert delivered.usage_before is None


def test_usage_after_connection_error_still_reports_the_send() -> None:
    """**送ったのに失敗扱い**にしない。呼び出し側が送り直しに行く。"""
    delivered = _send(_Broken(get_fails=(False, True)))
    assert delivered.message_id == "m-1"
    assert delivered.usage_after is None


def test_push_connection_error_becomes_a_line_error_without_the_secret() -> None:
    with pytest.raises(line_auth.LineError) as raised:
        _send(_Broken(post_fails=True))
    assert TOKEN not in str(raised.value)


def test_secret_in_the_body_is_masked_before_sending() -> None:
    """本文は各段の文言の寄せ集め。**例外の文字列に鍵が混ざっても、送る直前で伏せる。**"""
    session = _Session(_ok())
    _send(session, body=f"Inbox に書けなかった: key={TOKEN}")
    sent = _posts(session)[0]["json"]["messages"][0]["text"]  # type: ignore[index]
    assert TOKEN not in sent


def test_accepted_request_id_header_is_case_insensitive() -> None:
    conflict = _Response(
        409,
        {"message": "x", "sentMessages": [{"id": "m-first", "quoteToken": "q"}]},
        {"X-Line-Accepted-Request-Id": "req-1"},
    )
    delivered = _send(_Session(conflict))
    assert delivered.duplicate is True
    assert delivered.accepted_request_id == "req-1"


def test_summary_shows_message_id_and_usage() -> None:
    delivered = _send(_Session(_ok()))
    assert "m-1" in delivered.summary
    assert "10 -> 11" in delivered.summary


def test_module_import_does_not_read_env_or_open_a_session() -> None:
    """G：**読み込んだだけで本物に触れない。** 接続は `cli` が組み立てる。"""
    source = (ROOT / "scout" / "notify.py").read_text(encoding="utf-8")
    assert "env_file" not in source
    assert "build_session" not in source
