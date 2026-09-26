"""scout/summarize のテスト。**実装より先に書いた。**

`summarize` は `split` が要約へ回した記事を、1件ずつ Gemini に要約させる段。

この段で守ること（2026-09-25 の実測から）
--------------------------------------------------------------------------

============ ====================================================================
根拠         ここで守ること
============ ====================================================================
**U13**      **引用は証拠にしない。** 要約器が自分で選ぶので、実在しても一貫性の
             検査でしかない。受け取るが、ここでは**照合済みにしない**（`verify_source` の仕事）
U4・U7       **本文は切らずに渡す。** p95（22017字）でも入力 7133 トークンだった
M8           **要約できなかった記事を「問題なし」に倒さない。** 失敗を記事ごとに残す
M2           1件の失敗で残りを止めない。ただし**全体を成功にしない**
課金         **静かに何度も呼ばない。** 1件につき1回。上限を超える件数は、呼ぶ前に止める
============ ====================================================================

HTTP と同じく**呼び出しは差し込み式**（`fetch` の `Fetcher` と同じ）。
本物の Gemini に触らずに、打ち切り・壊れた JSON・API の失敗を全部通せる。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scout"))

import dedupe  # noqa: E402
import fetch  # noqa: E402
import rank  # noqa: E402
import summarize  # noqa: E402
from common import gemini_client  # noqa: E402

WHEN = datetime(2026, 9, 22, 10, 0, 0)
BODY = "本文の一行目。\n" + "本文" * 300 + "\n本文の最後の行。"


def _scored(url: str, *, title: str = "記事", body: str = BODY) -> rank.Scored:
    article = fetch.Article(
        source="qiita",
        url=url,
        title=title,
        body=body,
        published_at=WHEN,
        updated_at=None,
        author="someone",
        tags=("misc",),
        metrics={},
    )
    return rank.Scored(kept=dedupe.Kept(article=article, key=dedupe.normalize(url)), score=0, hits=())


def _reply(
    summary: object = "要約です。",
    quotes: object = ("本文の一行目。",),
    *,
    finish: str | None = "STOP",
    tokens: tuple[int | None, int | None] = (100, 20),
    text: str | None = None,
) -> gemini_client.Reply:
    payload = text if text is not None else json.dumps(
        {"summary": summary, "quotes": list(quotes) if isinstance(quotes, tuple) else quotes},
        ensure_ascii=False,
    )
    return gemini_client.Reply(
        text=payload, finish_reason=finish, prompt_tokens=tokens[0], output_tokens=tokens[1]
    )


class Recorder:
    """呼ばれた回数とプロンプトを数える。**課金は呼んだ回数で決まる。**"""

    def __init__(self, *replies: object) -> None:
        self.prompts: list[str] = []
        self._replies = list(replies) or [_reply()]

    def __call__(self, prompt: str) -> gemini_client.Reply:
        self.prompts.append(prompt)
        reply = self._replies[min(len(self.prompts), len(self._replies)) - 1]
        if isinstance(reply, BaseException):
            raise reply
        return reply  # type: ignore[return-value]


def _urls(items: object) -> list[str]:
    return [item.scored.kept.article.url for item in items]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 要約できたもの
# --------------------------------------------------------------------------


def test_要約を受け取る() -> None:
    got = summarize.summarize([_scored("https://q.com/x")], call=Recorder(_reply("短くまとめた。")))

    assert got.done[0].text == "短くまとめた。"
    assert got.failed == ()


def test_引用も受け取るが照合済みにはしない() -> None:
    """**★ U13：引用が実在しても、要約の正しさの証拠にならない。**

    読む人が本文を開いて確かめる手がかりにはなるので受け取る。
    *照合済みかどうかを、この段は知らない*——`verify_source` が決める。
    """
    got = summarize.summarize(
        [_scored("https://q.com/x")], call=Recorder(_reply(quotes=("本文の一行目。", "本文")))
    )

    assert got.done[0].quotes == ("本文の一行目。", "本文")
    assert not hasattr(got.done[0], "verified")


def test_要約の前後の空白を落とす() -> None:
    got = summarize.summarize([_scored("https://q.com/x")], call=Recorder(_reply("\n 要約。 \n")))

    assert got.done[0].text == "要約。"


def test_トークン数を記事ごとに残す() -> None:
    """**U7：課金は呼び出しごとに実際の値で追う。** 見積もりは見積もりでしかない。"""
    got = summarize.summarize(
        [_scored("https://q.com/x")], call=Recorder(_reply(tokens=(2435, 240)))
    )

    assert (got.done[0].prompt_tokens, got.done[0].output_tokens) == (2435, 240)


# --------------------------------------------------------------------------
# 何を渡すか
# --------------------------------------------------------------------------


def test_本文を切らずに渡す() -> None:
    """**U4：p95 の記事でも入力 7133 トークン。** 切ると、*切った先の主張が照合できない*。"""
    long_body = "最初の段落。\n" + "あ" * 30000 + "\n最後の段落。"
    recorder = Recorder()

    summarize.summarize([_scored("https://q.com/x", body=long_body)], call=recorder)

    assert long_body in recorder.prompts[0]


def test_タイトルを渡す() -> None:
    recorder = Recorder()

    summarize.summarize([_scored("https://q.com/x", title="BGP の AS Path Prepend")], call=recorder)

    assert "BGP の AS Path Prepend" in recorder.prompts[0]


def test_本文からそのまま抜くよう指示する() -> None:
    """**言い換えた引用は、照合の手がかりにもならない。**"""
    recorder = Recorder()

    summarize.summarize([_scored("https://q.com/x")], call=recorder)

    assert "そのまま" in recorder.prompts[0]


# --------------------------------------------------------------------------
# 呼び出しの回数（課金）
# --------------------------------------------------------------------------


def test_1件につき1回だけ呼ぶ() -> None:
    """**静かに何度も呼ぶ経路を作らない**（`common/gemini_client` の方針）。"""
    recorder = Recorder()

    summarize.summarize([_scored(f"https://q.com/{n}") for n in range(3)], call=recorder)

    assert len(recorder.prompts) == 3


def test_失敗しても投げ直さない() -> None:
    """**投げ直しは呼び出す側の判断にする。** ここで重ねると、*1回の実行の課金が読めない*。"""
    recorder = Recorder(gemini_client.ApiError("HTTP 503"), _reply())

    summarize.summarize([_scored("https://q.com/x")], call=recorder)

    assert len(recorder.prompts) == 1


def test_上限を超える件数は呼ぶ前に止める() -> None:
    """**呼んでから気づいても、課金は戻らない。** `rank` の上限を素通りした入力を、ここで止める。"""
    recorder = Recorder()

    with pytest.raises(ValueError):
        summarize.summarize(
            [_scored(f"https://q.com/{n}") for n in range(3)], call=recorder, max_calls=2
        )

    assert recorder.prompts == []


def test_既定の上限はrankの上限と同じ10件() -> None:
    assert summarize.MAX_CALLS == 10


def test_記事が0件なら呼ばない() -> None:
    """**0件は異常ではない。** 呼ばなければ課金もされない。"""
    recorder = Recorder()

    got = summarize.summarize([], call=recorder)

    assert recorder.prompts == []
    assert (got.done, got.failed) == ((), ())


# --------------------------------------------------------------------------
# 要約できなかったもの（M8）
# --------------------------------------------------------------------------


def test_APIの失敗を記事ごとに残す() -> None:
    got = summarize.summarize(
        [_scored("https://q.com/x")], call=Recorder(gemini_client.ApiError("HTTP 503 だった"))
    )

    assert got.done == ()
    assert [f.reason for f in got.failed] == [summarize.CALL_FAILED]
    assert "HTTP 503" in got.failed[0].detail


def test_1件の失敗で残りを止めない() -> None:
    """**M2：1つ死んでも他は取る。**"""
    recorder = Recorder(gemini_client.ApiError("HTTP 503"), _reply("2件目の要約。"))

    got = summarize.summarize(
        [_scored("https://q.com/a"), _scored("https://q.com/b")], call=recorder
    )

    assert _urls(got.failed) == ["https://q.com/a"]
    assert _urls(got.done) == ["https://q.com/b"]


def test_想定外の例外は握らない() -> None:
    """**握ってよいのは `GeminiError` だけ。** こちらのバグを「API の失敗」に化けさせない。"""
    with pytest.raises(KeyError):
        summarize.summarize([_scored("https://q.com/x")], call=Recorder(KeyError("バグ")))


def test_打ち切られた要約を使わない() -> None:
    """**打ち切られても本文は返る**（`gemini_client.Reply` の注記）。途中で切れた要約を通さない。"""
    got = summarize.summarize(
        [_scored("https://q.com/x")], call=Recorder(_reply(finish="MAX_TOKENS"))
    )

    assert got.done == ()
    assert [f.reason for f in got.failed] == [summarize.NOT_FINISHED]
    assert "MAX_TOKENS" in got.failed[0].detail


def test_終わり方が分からない要約を使わない() -> None:
    """**「STOP だった」と「見られなかった」を混同させない。**"""
    got = summarize.summarize([_scored("https://q.com/x")], call=Recorder(_reply(finish=None)))

    assert [f.reason for f in got.failed] == [summarize.NOT_FINISHED]


@pytest.mark.parametrize(
    "reply",
    [
        _reply(text="これはJSONではない"),
        _reply(text="[]"),
        _reply(text='{"quotes": []}'),
        _reply(summary=""),
        _reply(summary="   "),
        _reply(summary=123),
        _reply(quotes="文字列"),
        _reply(quotes=[1, 2]),
    ],
    ids=["JSONでない", "配列", "要約が無い", "要約が空", "要約が空白", "要約が文字列でない", "引用が配列でない", "引用が文字列でない"],
)
def test_壊れた答えを要約にしない(reply: gemini_client.Reply) -> None:
    """**解釈できないものを、空の要約として先へ流さない。**"""
    got = summarize.summarize([_scored("https://q.com/x")], call=Recorder(reply))

    assert got.done == ()
    assert [f.reason for f in got.failed] == [summarize.BROKEN_REPLY]


def test_壊れた答えの原文を残す() -> None:
    """**解釈して落ちると、何が返ったか分からないまま課金だけが乗る**（`generate_json` の注記）。"""
    got = summarize.summarize(
        [_scored("https://q.com/x")], call=Recorder(_reply(text="これはJSONではない"))
    )

    assert "これはJSONではない" in got.failed[0].detail


def test_引用が無くても要約は受け取る() -> None:
    """引用は**証拠ではない**ので、無いことは失敗にしない（U13）。"""
    got = summarize.summarize([_scored("https://q.com/x")], call=Recorder(_reply(quotes=())))

    assert got.done[0].quotes == ()


# --------------------------------------------------------------------------
# 件数
# --------------------------------------------------------------------------


def test_渡された順のまま() -> None:
    urls = ["https://q.com/c", "https://q.com/a", "https://q.com/b"]

    got = summarize.summarize([_scored(u) for u in urls], call=Recorder())

    assert _urls(got.done) == urls


def test_どの記事も必ずどちらか1つに入る() -> None:
    recorder = Recorder(_reply(), gemini_client.ApiError("HTTP 503"), _reply(finish="MAX_TOKENS"))

    got = summarize.summarize([_scored(f"https://q.com/{n}") for n in range(3)], call=recorder)

    assert got.total == 3
    assert (len(got.done), len(got.failed)) == (1, 2)


def test_1件でも失敗したら全体を成功にしない() -> None:
    """**M2：他が生きていれば全体は成功に見える**のを止める。"""
    recorder = Recorder(_reply(), gemini_client.ApiError("HTTP 503"))

    got = summarize.summarize([_scored("https://q.com/a"), _scored("https://q.com/b")], call=recorder)

    assert got.ok is False


def test_全部できたら成功() -> None:
    got = summarize.summarize([_scored("https://q.com/a")], call=Recorder())

    assert got.ok is True


def test_0件は成功() -> None:
    """**呼ぶものが無かったことは失敗ではない。** 失敗は「呼んで、できなかった」こと。"""
    assert summarize.summarize([], call=Recorder()).ok is True


def test_トークンの合計を出す() -> None:
    recorder = Recorder(_reply(tokens=(100, 20)), _reply(tokens=(300, 40)))

    got = summarize.summarize([_scored("https://q.com/a"), _scored("https://q.com/b")], call=recorder)

    assert (got.prompt_tokens, got.output_tokens) == (400, 60)


def test_トークン数が1件でも分からなければ合計を出さない() -> None:
    """**分からないことを 0 と混ぜない。** 混ぜると*実際より安く見える*。"""
    recorder = Recorder(_reply(tokens=(100, 20)), _reply(tokens=(None, 40)))

    got = summarize.summarize([_scored("https://q.com/a"), _scored("https://q.com/b")], call=recorder)

    assert got.prompt_tokens is None
    assert got.output_tokens == 60


def test_件数と理由ごとの内訳を出す() -> None:
    recorder = Recorder(_reply(), gemini_client.ApiError("HTTP 503"), _reply(finish="MAX_TOKENS"))

    got = summarize.summarize([_scored(f"https://q.com/{n}") for n in range(3)], call=recorder)

    assert got.reasons == {summarize.CALL_FAILED: 1, summarize.NOT_FINISHED: 1}
    assert "3 件中 1 件を要約した" in got.summary
    assert "できなかった 2 件" in got.summary
    assert summarize.CALL_FAILED in got.summary
