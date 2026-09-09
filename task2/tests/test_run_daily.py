"""task2/run_daily のテスト。**実装より先に書いた。**

LINE にも Google にも楽天にも1回も繋がない。実行も送信も差し替える。

守らせる対象は `task2/DESIGN.md` の **5-V / 5-W / 5-Y / 5-AA / 5-AC**。

============ ====================================================================
DESIGN       ここで守ること
============ ====================================================================
5-V          報告を**必ず残す**。標準出力へ出して終わりにしない
5-W / 5-Y    生存通知が「**何回動いたか**」と「**何行書いたか**」を両方言う
5-AA         通知の送信に失敗したら、**残したうえで終了コードに乗せる**
5-AC         ロックが取れなければ**何もしない**。取れなかったことは残す
============ ====================================================================
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import diff  # noqa: E402
import run_daily  # noqa: E402
import to_sheet  # noqa: E402


JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 9, 21, 0, 0, tzinfo=JST)
CODE_A = "shop-a:1"
ENV = {
    "LINE_CHANNEL_ACCESS_TOKEN": "test-token-0123456789",
    "LINE_USER_ID": "U0123456789abcdef0123456789abcdef",
}


# ============================================================ 偽物


def make_result(**over):
    base = dict(
        requested=3, fetched_ok=3, fetched_failed=0, reasons={},
        rows_built=3, sent=3, written=3, wrote=True, drops=[],
        incomparable={}, name_changed=[], stock_changed=[],
        duplicates=[], header_state="そろっています",
        fetched_at=NOW.isoformat(timespec="seconds"),
    )
    base.update(over)
    return to_sheet.RunResult(**base)


def make_outcome(*, result=None, exit_code=None, lines=None):
    result = make_result() if result is None else result
    return to_sheet.Outcome(
        exit_code=result.exit_code if exit_code is None else exit_code,
        result=result,
        lines=["対象           3 件"] if lines is None else lines,
    )


def drop(code=CODE_A, previous=1280, current=1180):
    return diff.Comparison(
        item_code=code, previous_price=previous, current_price=current,
        delta=current - previous, note="",
    )


class FakeSender:
    """送った本文を覚えるだけの偽物。**宛先を持たない**（テストから外へ出さない）。"""

    def __init__(self, fail_kinds=()):
        self.sent: list[run_daily.Notice] = []
        self._fail = set(fail_kinds)

    def __call__(self, notices):
        errors = []
        for notice in notices:
            if notice.kind in self._fail:
                errors.append(f"{notice.kind}: 送れませんでした")
                continue
            self.sent.append(notice)
        return errors

    def kinds(self):
        return [n.kind for n in self.sent]


# ============================================================ 5-V ログ


class Testログ:
    """**残らなければ、起きなかったのと同じ。**

    タスクスケジューラは標準出力を捨てる。報告は毎回作られて毎回消えるので、
    残す先をこちらで持つ（DESIGN 5-V）。
    """

    def test_親ディレクトリが無くても作る(self, tmp_path):
        path = tmp_path / "logs" / "run.jsonl"
        run_daily.append_record(path, {"event": "run"})
        assert path.exists()

    def test_追記する(self, tmp_path):
        # **上書きしない。** 上書きすると「前に何回動いたか」が毎回消える。
        path = tmp_path / "run.jsonl"
        run_daily.append_record(path, {"event": "run", "n": 1})
        run_daily.append_record(path, {"event": "run", "n": 2})
        assert [r["n"] for r in run_daily.read_records(path)] == [1, 2]

    def test_1行1件で書く(self, tmp_path):
        path = tmp_path / "run.jsonl"
        run_daily.append_record(path, {"event": "run", "report": ["あ", "い"]})
        assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 1

    def test_日本語がそのまま読める(self, tmp_path):
        # エスケープされた JSON は、人が開いたときに読めない。
        path = tmp_path / "run.jsonl"
        run_daily.append_record(path, {"report": ["値下がり"]})
        assert "値下がり" in path.read_text(encoding="utf-8")

    def test_ファイルが無ければ空(self, tmp_path):
        assert run_daily.read_records(tmp_path / "nothing.jsonl") == []

    def test_壊れた行はその1行だけ捨てる(self, tmp_path):
        # **1行の崩れで全部を失わない。** 途中で電源が落ちれば半端な行が残る。
        path = tmp_path / "run.jsonl"
        run_daily.append_record(path, {"event": "run", "n": 1})
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"event": "run", "n"')
            handle.write("\n")
        run_daily.append_record(path, {"event": "run", "n": 3})
        assert [r["n"] for r in run_daily.read_records(path)] == [1, 3]

    def test_辞書でない行も捨てる(self, tmp_path):
        path = tmp_path / "run.jsonl"
        path.write_text('[1, 2]\n{"event": "run", "n": 9}\n', encoding="utf-8")
        assert [r["n"] for r in run_daily.read_records(path)] == [9]


# ============================================================ 5-AC ロック


class Testロック:
    """**重なるのは異常ではない。** 21時の実行が長引いた所へログオンが重なる。

    取れなかったときに例外にしない。「もう走っている」だけである。
    """

    def test_取れる(self, tmp_path):
        with run_daily.lock(tmp_path / "run.lock", now=NOW) as got:
            assert got is True

    def test_抜けたら解放する(self, tmp_path):
        path = tmp_path / "run.lock"
        with run_daily.lock(path, now=NOW):
            pass
        assert not path.exists()

    def test_例外で抜けても解放する(self, tmp_path):
        # **残ったロックは、次の回を丸ごと止める。**
        path = tmp_path / "run.lock"
        with pytest.raises(RuntimeError):
            with run_daily.lock(path, now=NOW):
                raise RuntimeError("こわれた")
        assert not path.exists()

    def test_二重には取れない(self, tmp_path):
        path = tmp_path / "run.lock"
        with run_daily.lock(path, now=NOW):
            with run_daily.lock(path, now=NOW) as got:
                assert got is False

    def test_取れなかったら解放もしない(self, tmp_path):
        # 取れていない側が消すと、**走っている側のロックを外してしまう。**
        path = tmp_path / "run.lock"
        with run_daily.lock(path, now=NOW):
            with run_daily.lock(path, now=NOW):
                pass
            assert path.exists()

    def test_古いロックは奪う(self, tmp_path):
        # 強制終了や停電でロックは残る。**永久に信じると二度と走らない。**
        path = tmp_path / "run.lock"
        path.write_text((NOW - timedelta(hours=2)).isoformat(), encoding="utf-8")
        with run_daily.lock(path, now=NOW) as got:
            assert got is True

    def test_新しいロックは奪わない(self, tmp_path):
        path = tmp_path / "run.lock"
        path.write_text((NOW - timedelta(minutes=1)).isoformat(), encoding="utf-8")
        with run_daily.lock(path, now=NOW) as got:
            assert got is False

    def test_オフセットの無いロックは奪う(self, tmp_path):
        # **物差しを2本にしない**（5-E）。オフセットの無い時刻を受け入れると、
        # 「読めた」ように見えて、そのあとの比較が型違いで落ちる。
        path = tmp_path / "run.lock"
        path.write_text("2026-09-09T20:59:00", encoding="utf-8")
        with run_daily.lock(path, now=NOW) as got:
            assert got is True

    def test_中身が読めないロックは奪う(self, tmp_path):
        # **読めない印を「生きている」と読まない。** 書き途中で落ちた形。
        path = tmp_path / "run.lock"
        path.write_text("こわれた", encoding="utf-8")
        with run_daily.lock(path, now=NOW) as got:
            assert got is True


# ============================================================ 記録の形


class Test記録:
    def test_走った回はrun(self):
        assert run_daily.record_of(make_outcome(), NOW)["event"] == "run"

    def test_スキップした回はskip(self):
        outcome = make_outcome(result=make_result(skipped=True, wrote=False, sent=0, written=0))
        assert run_daily.record_of(outcome, NOW)["event"] == "skip"

    def test_結果が無い回はerror(self):
        # **「走らなかった」を「0件で成功」と書かない。**
        outcome = to_sheet.Outcome(exit_code=1, result=None, lines=["こわれました"])
        assert run_daily.record_of(outcome, NOW)["event"] == "error"

    def test_報告をそのまま残す(self):
        outcome = make_outcome(lines=["対象 3 件", "値下がり 1 件"])
        assert run_daily.record_of(outcome, NOW)["report"] == ["対象 3 件", "値下がり 1 件"]

    def test_数字を残す(self):
        record = run_daily.record_of(make_outcome(result=make_result(written=2)), NOW)
        assert record["written"] == 2
        assert record["requested"] == 3

    def test_値下がりの件数を残す(self):
        outcome = make_outcome(result=make_result(drops=[drop(), drop("shop-b:2")]))
        assert run_daily.record_of(outcome, NOW)["drops"] == 2

    def test_時刻を残す(self):
        assert run_daily.record_of(make_outcome(), NOW)["at"] == NOW.isoformat(timespec="seconds")

    def test_結果が無くても終了コードは残す(self):
        outcome = to_sheet.Outcome(exit_code=1, result=None, lines=["こわれました"])
        assert run_daily.record_of(outcome, NOW)["exit_code"] == 1


# ============================================================ 5-AA 失敗の通知


class Test失敗の通知:
    def test_成功なら鳴らない(self):
        assert run_daily.failure_notice(run_daily.record_of(make_outcome(), NOW)) is None

    def test_書き込みが足りなければ鳴る(self):
        outcome = make_outcome(result=make_result(sent=3, written=2))
        assert run_daily.failure_notice(run_daily.record_of(outcome, NOW)) is not None

    def test_取得の失敗でも鳴る(self):
        outcome = make_outcome(result=make_result(fetched_failed=1, reasons={"不明": 1}))
        assert run_daily.failure_notice(run_daily.record_of(outcome, NOW)) is not None

    def test_スキップは鳴らない(self):
        # スキップは異常ではない。**鳴らしすぎると読まなくなる。**
        outcome = make_outcome(result=make_result(skipped=True, wrote=False, sent=0, written=0))
        assert run_daily.failure_notice(run_daily.record_of(outcome, NOW)) is None

    def test_本文に報告が全部入る(self):
        # **「失敗しました」だけでは、何をすればいいか分からない。**
        outcome = make_outcome(result=make_result(sent=3, written=2),
                               lines=["対象 3 件", "書き込み 送った 3 行 / 入った 2 行"])
        notice = run_daily.failure_notice(run_daily.record_of(outcome, NOW))
        assert "送った 3 行 / 入った 2 行" in notice.text

    def test_本文に終了コードが入る(self):
        outcome = make_outcome(result=make_result(fetched_failed=1))
        notice = run_daily.failure_notice(run_daily.record_of(outcome, NOW))
        # **部分一致にしない。** 日付の "2026" でも通ってしまう。
        assert "終了コード 2" in notice.text

    def test_本文に日付が入る(self):
        outcome = make_outcome(result=make_result(sent=3, written=2))
        notice = run_daily.failure_notice(run_daily.record_of(outcome, NOW))
        assert "2026-09-09" in notice.text


# ============================================================ 値下がりの通知


class Test値下がりの通知:
    def test_値下がりが無ければ鳴らない(self):
        assert run_daily.drops_notice(make_result(), NOW) is None

    def test_1件でも鳴る(self):
        assert run_daily.drops_notice(make_result(drops=[drop()]), NOW) is not None

    def test_結果が無ければ鳴らない(self):
        assert run_daily.drops_notice(None, NOW) is None

    def test_本文に商品コードと前後の価格が入る(self):
        notice = run_daily.drops_notice(make_result(drops=[drop()]), NOW)
        assert CODE_A in notice.text
        assert "1280" in notice.text
        assert "1180" in notice.text

    def test_本文に下げ幅が入る(self):
        notice = run_daily.drops_notice(make_result(drops=[drop()]), NOW)
        assert "-100" in notice.text

    def test_件数を先に言う(self):
        result = make_result(drops=[drop(), drop("shop-b:2", 900, 800)])
        assert "2 件" in run_daily.drops_notice(result, NOW).text

    def test_全部の行が入る(self):
        result = make_result(drops=[drop(), drop("shop-b:2", 900, 800)])
        assert "shop-b:2" in run_daily.drops_notice(result, NOW).text


# ============================================================ 5-Y 生存通知


class Test生存通知:
    """**差分でしか鳴らない通知は、正常と経路の死を同じ無音にする。**

    週1で1本送って、無音の意味を1つに減らす。
    """

    def records(self, *, runs=3, written=9, days_ago_heartbeat=None):
        out = []
        if days_ago_heartbeat is not None:
            out.append({
                "at": (NOW - timedelta(days=days_ago_heartbeat)).isoformat(timespec="seconds"),
                "event": "heartbeat",
            })
        for i in range(runs):
            out.append({
                "at": (NOW - timedelta(days=i)).isoformat(timespec="seconds"),
                "event": "run", "exit_code": 0,
                "written": written // runs if runs else 0,
            })
        return out

    def test_一度も送っていなければ送る(self):
        # 初回に鳴らすことで、**経路が生きていることを最初に確かめられる。**
        assert run_daily.heartbeat_notice(self.records(), NOW) is not None

    def test_7日経っていなければ送らない(self):
        assert run_daily.heartbeat_notice(self.records(days_ago_heartbeat=3), NOW) is None

    def test_7日経っていれば送る(self):
        assert run_daily.heartbeat_notice(self.records(days_ago_heartbeat=7), NOW) is not None

    def test_動いた回数を言う(self):
        notice = run_daily.heartbeat_notice(self.records(runs=3), NOW)
        assert "3 回" in notice.text

    def test_書いた行数も言う(self):
        # **走ったことと、入ったことは別。** 回数だけでは「動いていた」の証拠にならない。
        notice = run_daily.heartbeat_notice(self.records(runs=3, written=9), NOW)
        assert "9 行" in notice.text

    def test_生存通知そのものは回数に数えない(self):
        # **境界に置く。** 8日前だと数える期間の外なので、
        # 数え方を壊しても結果が変わらない＝この検査は何も守っていない。
        notice = run_daily.heartbeat_notice(self.records(runs=2, days_ago_heartbeat=7), NOW)
        assert "2 回" in notice.text

    def test_期間より古い記録は数えない(self):
        old = {"at": (NOW - timedelta(days=30)).isoformat(timespec="seconds"),
               "event": "run", "exit_code": 0, "written": 100}
        notice = run_daily.heartbeat_notice([old, *self.records(runs=2, written=4)], NOW)
        assert "2 回" in notice.text
        assert "100" not in notice.text

    def test_うまくいかなかった回も言う(self):
        records = self.records(runs=2, written=4)
        records.append({"at": NOW.isoformat(timespec="seconds"), "event": "run",
                        "exit_code": 2, "written": 0})
        assert "うまくいかなかった回 1 回" in run_daily.heartbeat_notice(records, NOW).text

    def test_1回も動いていなければ0と言う(self):
        # **0 を隠さない。** ここが 0 のまま届くことが、いちばん知りたい事実。
        notice = run_daily.heartbeat_notice([], NOW)
        assert "0 回" in notice.text

    def test_時刻が読めない記録は数えない(self):
        bad = {"at": "こわれた", "event": "run", "exit_code": 0, "written": 5}
        notice = run_daily.heartbeat_notice([bad, *self.records(runs=1, written=1)], NOW)
        assert "1 回" in notice.text


# ============================================================ 送信


class Test送信:
    def test_送る(self):
        pushed = []

        def push(session, payload, **kwargs):
            pushed.append(payload)
            return object()

        errors = run_daily.send_all(
            [run_daily.Notice("drops", "値下がり")], ENV,
            build_session=lambda token: object(), push=push,
            read_result=lambda response: None,
        )
        assert errors == []
        assert pushed[0]["messages"][0]["text"] == "値下がり"

    def test_失敗しても他の通知は送る(self):
        # **1本の失敗で残りを落とさない。** 失敗したのは経路ではなく1通かもしれない。
        pushed = []

        def push(session, payload, **kwargs):
            if "こわれ" in payload["messages"][0]["text"]:
                raise RuntimeError("送れません")
            pushed.append(payload)
            return object()

        errors = run_daily.send_all(
            [run_daily.Notice("failure", "こわれ"), run_daily.Notice("drops", "値下がり")],
            ENV, build_session=lambda token: object(), push=push,
            read_result=lambda response: None,
        )
        assert len(errors) == 1
        assert len(pushed) == 1

    def test_失敗の説明に秘密を出さない(self):
        # 記録は public リポジトリに入る。**例外の本文にトークンが載ることがある。**
        token = ENV["LINE_CHANNEL_ACCESS_TOKEN"]

        def push(session, payload, **kwargs):
            raise RuntimeError("401 Unauthorized: Bearer " + token)

        errors = run_daily.send_all(
            [run_daily.Notice("drops", "値下がり")], ENV,
            build_session=lambda t: object(), push=push, read_result=lambda r: None,
        )
        assert token not in errors[0]

    def test_通知が無ければ繋ぎもしない(self):
        # **送るものが無いのに認証しない。** 失敗する場所を増やさない。
        called = []
        errors = run_daily.send_all(
            [], ENV, build_session=lambda t: called.append(t),
            push=lambda *a, **k: None, read_result=lambda r: None,
        )
        assert errors == []
        assert called == []

    def test_資格情報が足りなければ失敗として返す(self):
        # **例外で落とさない。** 落とすと、本体の成功まで巻き添えになる。
        errors = run_daily.send_all(
            [run_daily.Notice("drops", "値下がり")], {},
            build_session=lambda t: object(), push=lambda *a, **k: None,
            read_result=lambda r: None,
        )
        assert len(errors) == 1


# ============================================================ 配線


class Test配線:
    def _run(self, tmp_path, outcome, *, sender=None, now=NOW, records=()):
        log = tmp_path / "run.jsonl"
        for record in records:
            run_daily.append_record(log, record)
        sender = FakeSender() if sender is None else sender
        code = run_daily.run(
            log_path=log, now=now, execute=lambda argv: outcome, argv=[], send=sender
        )
        return code, run_daily.read_records(log), sender

    def test_記録が残る(self, tmp_path):
        _, records, _ = self._run(tmp_path, make_outcome())
        assert [r["event"] for r in records if r["event"] != "heartbeat"] == ["run"]

    def test_終了コードを返す(self, tmp_path):
        code, _, _ = self._run(tmp_path, make_outcome(result=make_result(sent=3, written=2)))
        assert code == 1

    def test_成功なら0(self, tmp_path):
        code, _, _ = self._run(tmp_path, make_outcome())
        assert code == 0

    def test_失敗したら通知が飛ぶ(self, tmp_path):
        _, _, sender = self._run(tmp_path, make_outcome(result=make_result(sent=3, written=2)),
                                 records=[{"at": NOW.isoformat(), "event": "heartbeat"}])
        assert sender.kinds() == ["failure"]

    def test_値下がりも飛ぶ(self, tmp_path):
        _, _, sender = self._run(tmp_path, make_outcome(result=make_result(drops=[drop()])),
                                 records=[{"at": NOW.isoformat(), "event": "heartbeat"}])
        assert sender.kinds() == ["drops"]

    def test_失敗と値下がりは別々に飛ぶ(self, tmp_path):
        result = make_result(sent=3, written=2, drops=[drop()])
        _, _, sender = self._run(tmp_path, make_outcome(result=result),
                                 records=[{"at": NOW.isoformat(), "event": "heartbeat"}])
        assert sorted(sender.kinds()) == ["drops", "failure"]

    def test_生存通知も飛ぶ(self, tmp_path):
        _, _, sender = self._run(tmp_path, make_outcome())
        assert "heartbeat" in sender.kinds()

    def test_生存通知を送ったら記録に残す(self, tmp_path):
        # 残さないと**毎回**送ることになる。次にいつ送るかを決める根拠がここ。
        _, records, _ = self._run(tmp_path, make_outcome())
        assert [r["event"] for r in records if r["event"] == "heartbeat"] == ["heartbeat"]

    def test_送れなかった生存通知は記録しない(self, tmp_path):
        # **送ったことにすると、次の7日も黙る。** 届いていないのに。
        _, records, _ = self._run(tmp_path, make_outcome(), sender=FakeSender(["heartbeat"]))
        assert [r for r in records if r["event"] == "heartbeat"] == []

    def test_送信に失敗したら終了コード3(self, tmp_path):
        # **本体の成功より、通知の死のほうが重い。** 気づけなくなるのはこちら。
        code, _, _ = self._run(tmp_path, make_outcome(), sender=FakeSender(["heartbeat"]))
        assert code == 3

    def test_送信の失敗も記録に残す(self, tmp_path):
        _, records, _ = self._run(tmp_path, make_outcome(), sender=FakeSender(["heartbeat"]))
        assert any(r["event"] == "notify_failed" for r in records)

    def test_本体が1で通知も失敗したら3(self, tmp_path):
        code, _, _ = self._run(tmp_path, make_outcome(result=make_result(sent=3, written=2)),
                               sender=FakeSender(["failure", "heartbeat"]))
        assert code == 3

    def test_スキップした回は通知を出さない(self, tmp_path):
        outcome = make_outcome(result=make_result(skipped=True, wrote=False, sent=0, written=0))
        _, _, sender = self._run(tmp_path, outcome,
                                 records=[{"at": NOW.isoformat(), "event": "heartbeat"}])
        assert sender.kinds() == []

    def test_スキップも記録には残す(self, tmp_path):
        # **何もしなかったことを、何も残さないで表さない。**
        outcome = make_outcome(result=make_result(skipped=True, wrote=False, sent=0, written=0))
        _, records, _ = self._run(tmp_path, outcome,
                                  records=[{"at": NOW.isoformat(), "event": "heartbeat"}])
        assert [r["event"] for r in records] == ["heartbeat", "skip"]

    def test_本体が例外でも記録を残す(self, tmp_path):
        # **落ちたことがどこにも残らないのが、いちばん困る。**
        def boom(argv):
            raise RuntimeError("想定外")

        log = tmp_path / "run.jsonl"
        code = run_daily.run(log_path=log, now=NOW, execute=boom, argv=[], send=FakeSender())
        assert code == 4
        assert any(r["event"] == "crash" for r in run_daily.read_records(log))

    def test_例外の回も通知が飛ぶ(self, tmp_path):
        def boom(argv):
            raise RuntimeError("想定外")

        sender = FakeSender()
        run_daily.run(log_path=tmp_path / "run.jsonl", now=NOW, execute=boom,
                      argv=[], send=sender)
        assert "failure" in sender.kinds()


# ============================================================ 入口


class TestCLI:
    """`main` は**外の世界を組み立てるだけ**。判断は `run` が持つ。

    それでも入口にしか無い分岐が2つある——**同じ日に2回走らせない指定**と、
    **ロックが取れなかったときに何もしないこと**。ここを検査しないと、
    判定が正しくても「そこを一度も通っていない」まま緑になる。
    """

    def _patch(self, monkeypatch, seen, *, outcome=None, env=None, errors=()):
        outcome = make_outcome() if outcome is None else outcome

        def execute(argv):
            seen.append(list(argv))
            return outcome

        monkeypatch.setattr(run_daily.to_sheet, "execute", execute)
        monkeypatch.setattr(run_daily, "send_all", lambda notices, e, **kw: list(errors))
        monkeypatch.setattr(
            run_daily.env_file, "load", lambda path: dict(ENV if env is None else env)
        )

    def test_既定では同じ日に2回書かない(self, tmp_path, monkeypatch):
        seen: list[list[str]] = []
        self._patch(monkeypatch, seen)
        run_daily.main(["--log-dir", str(tmp_path)])
        assert "--once-a-day" in seen[0]

    def test_明示すれば同じ日でも取りに行く(self, tmp_path, monkeypatch):
        # 手で回して確かめたいときに、逃げ道が無いと困る。
        seen: list[list[str]] = []
        self._patch(monkeypatch, seen)
        run_daily.main(["--log-dir", str(tmp_path), "--allow-same-day"])
        assert "--once-a-day" not in seen[0]

    def test_下げ幅をそのまま渡す(self, tmp_path, monkeypatch):
        seen: list[list[str]] = []
        self._patch(monkeypatch, seen)
        run_daily.main(["--log-dir", str(tmp_path), "--threshold", "500"])
        assert "500" in seen[0]

    def test_ロックが取れなければ1回も走らない(self, tmp_path, monkeypatch):
        # **重なるのは異常ではない。** 21時の実行が長引いた所へログオンが重なる。
        seen: list[list[str]] = []
        self._patch(monkeypatch, seen)
        held = datetime.now().astimezone().isoformat()
        (tmp_path / run_daily.LOCK_NAME).write_text(held, encoding="utf-8")

        code = run_daily.main(["--log-dir", str(tmp_path)])
        records = run_daily.read_records(tmp_path / run_daily.LOG_NAME)
        assert code == 0
        assert seen == []
        assert [r["event"] for r in records] == ["locked"]

    def test_走っている側のロックを外さない(self, tmp_path, monkeypatch):
        seen: list[list[str]] = []
        self._patch(monkeypatch, seen)
        path = tmp_path / run_daily.LOCK_NAME
        path.write_text(datetime.now().astimezone().isoformat(), encoding="utf-8")
        run_daily.main(["--log-dir", str(tmp_path)])
        assert path.exists()

    def test_走り終えたらロックは残らない(self, tmp_path, monkeypatch):
        seen: list[list[str]] = []
        self._patch(monkeypatch, seen)
        run_daily.main(["--log-dir", str(tmp_path)])
        assert not (tmp_path / run_daily.LOCK_NAME).exists()

    def test_env_が読めなくても本体は走る(self, tmp_path, monkeypatch):
        # **通知の準備ができないことは、本体を止める理由にならない。**
        # 止めると、シートへの記録という本来の仕事まで落ちる。
        seen: list[list[str]] = []

        def broken(path):
            raise RuntimeError("読めません")

        self._patch(monkeypatch, seen)
        monkeypatch.setattr(run_daily.env_file, "load", broken)
        run_daily.main(["--log-dir", str(tmp_path)])
        assert seen != []

    def test_記録の置き場を指定できる(self, tmp_path, monkeypatch):
        seen: list[list[str]] = []
        self._patch(monkeypatch, seen)
        run_daily.main(["--log-dir", str(tmp_path)])
        assert (tmp_path / run_daily.LOG_NAME).exists()

    def test_生存通知の間隔を渡せる(self):
        assert run_daily.build_parser().parse_args(["--heartbeat-days", "3"]).heartbeat_days == 3

    def test_同じ日の説明が何を見るか言う(self):
        # **「1日1回」ではない。** 見ているのは「その日に取得できた行があるか」。
        help_text = run_daily.build_parser().format_help()
        index = help_text.rindex("--allow-same-day")
        assert "取得できた行" in help_text[index:index + 300]

    def test_タブ名を渡せる(self, tmp_path, monkeypatch):
        # 本番の履歴を汚さずに値下がりを再現するのに要る（別タブで試す）。
        seen: list[list[str]] = []
        self._patch(monkeypatch, seen)
        run_daily.main(["--log-dir", str(tmp_path), "--sheet-name", "検証"])
        assert "--sheet-name" in seen[0]
        assert "検証" in seen[0]

    def test_既定ではタブ名を渡さない(self, tmp_path, monkeypatch):
        # **既定値を2箇所に持たない。** 渡さなければ to_sheet.py の既定が効く。
        # ここに書き写すと、向こうを変えた日にこちらが古い値で上書きする。
        seen: list[list[str]] = []
        self._patch(monkeypatch, seen)
        run_daily.main(["--log-dir", str(tmp_path)])
        assert "--sheet-name" not in seen[0]
