"""scout/verify_source のテスト。**実装より先に書いた。**

`verify_source` は、要約の文に出る**数字と英語の語**が、**記事の本文に実在するか**を照合する段。
課題2（5-1-1）の講評で3回続いた指摘——*出力を読み返すのは照合ではない。ソースを開け*——への答え。

何を照合するか（2026-09-25 の実測・`DESIGN.md` U12・U13）
--------------------------------------------------------------------------

============ ====================================================================
**U13**      **引用は証拠にしない。** 要約器が自分で選ぶので、実在しても一貫性の検査。
             さらに `'IF'`（2字）のような短い引用はどの本文にもある。
             → 照合するのは**要約の文そのもの**に出る数字・英語の語
**U13**      **部分一致で比べない。** `2倍速い` が本文の `200倍` の `2` に当たって素通りした。
             → **境界つき**（数字の前後に数字が無い・語の前後に英数字が無い）
**U12**      ずれは2種類だけ——強調 `**` と、リンク `[文字](URL)` を描画後の見た目で読んだもの。
             → 本文の**書式記号だけ**を落として比べる。**意味を持つ字は落とさない**
============ ====================================================================

判定を安全側に倒す（`DESIGN.md` 6-1）
--------------------------------------------------------------------------

- **照合0件を「一致」にしない**（M9）。主張が1つも無い要約は「確認できない」
- **一致しなかった主張を消さない。印を付けて残す**（6-2）
- **件数を必ず出す**。「N個中M個が本文に実在」

見ていないもの（承知で残す）
--------------------------------------------------------------------------

**日本語の固有名詞・言い回しは照合しない。** 「〜を廃止した」と本文に無いことを書かれても
ここでは捕まらない。*数字と英語の語だけを見ている*ことを、出力の文言で隠さない。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scout"))

import dedupe  # noqa: E402
import fetch  # noqa: E402
import rank  # noqa: E402
import summarize  # noqa: E402
import verify_source  # noqa: E402

WHEN = datetime(2026, 9, 22, 10, 0, 0)


def _summary(
    text: str,
    body: str | None,
    *,
    url: str = "https://q.com/x",
    quotes: tuple[str, ...] = (),
) -> summarize.Summary:
    article = fetch.Article(
        source="qiita",
        url=url,
        title="記事",
        body=body,
        published_at=WHEN,
        updated_at=None,
        author="someone",
        tags=("misc",),
        metrics={},
    )
    scored = rank.Scored(kept=dedupe.Kept(article=article, key=dedupe.normalize(url)), score=0, hits=())
    return summarize.Summary(
        scored=scored, text=text, quotes=quotes, prompt_tokens=None, output_tokens=None
    )


def _one(text: str, body: str | None, **kwargs: object) -> verify_source.Check:
    return verify_source.verify([_summary(text, body, **kwargs)]).checks[0]  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 何を主張として抜くか
# --------------------------------------------------------------------------


def test_数字と英語の語を抜く() -> None:
    got = verify_source.claims("Jev は LLM より 200 倍速く、400倍安い。")

    assert got == ("Jev", "LLM", "200", "400")


def test_同じ主張は1つにまとめる() -> None:
    assert verify_source.claims("LLM と LLM と 3 と 3") == ("LLM", "3")


def test_小数とカンマ区切りを1つの数として抜く() -> None:
    assert verify_source.claims("2.5倍で1,000件") == ("2.5", "1000")


def test_全角の数字と英字を半角にしてから抜く() -> None:
    """要約器は全角で書くことがある。**表記が違うだけで「本文に無い」にしない。**"""
    assert verify_source.claims("ＬＬＭを２００回") == ("LLM", "200")


def test_語に付く記号を抜く() -> None:
    """`C++`・`C#`・`Node.js` は**記号まで含めて1つの名前**。"""
    assert verify_source.claims("C++ と C# と Node.js") == ("C++", "C#", "Node.js")


def test_文末の句点を語に含めない() -> None:
    assert verify_source.claims("使うのは Python.") == ("Python",)


def test_日本語だけの要約からは何も抜かない() -> None:
    """**日本語の言い回しは照合しない**（承知で残す）。抜けるものが無いだけで、異常ではない。"""
    assert verify_source.claims("仕組みを分かりやすく説明している。") == ()


# --------------------------------------------------------------------------
# 本文に実在するか（境界つき）
# --------------------------------------------------------------------------


def test_本文にある数字を見つける() -> None:
    assert verify_source.present("200", "LLMより200倍速い")


def test_数字の一部には当てない() -> None:
    """**★ U13 の罠。** 本文の `200` の中の `2` を、`2倍` の根拠にしない。"""
    assert not verify_source.present("2", "LLMより200倍速い")


def test_小数の一部には当てない() -> None:
    assert not verify_source.present("2", "2.5倍速い")
    assert not verify_source.present("5", "2.5倍速い")


def test_小数そのものは見つける() -> None:
    assert verify_source.present("2.5", "2.5倍速い")


def test_カンマ区切りと区切りなしを同じ数として見る() -> None:
    assert verify_source.present("1000", "1,000件を処理した")
    assert verify_source.present("1000", "1000件を処理した")


def test_語の一部には当てない() -> None:
    """`Java` は `JavaScript` ではない。**語の前後に英数字があれば別の語。**"""
    assert not verify_source.present("Java", "JavaScript で書いた")


def test_日本語に挟まれた語は見つける() -> None:
    """日本語の本文では、語の前後に空白が無いのが普通。**空白を境界にしない。**"""
    assert verify_source.present("LLM", "これはLLMの話")


def test_英語の大小を問わない() -> None:
    assert verify_source.present("python", "Python で書いた")


def test_記号つきの語を見つける() -> None:
    assert verify_source.present("C++", "C++ で書いた")
    assert verify_source.present("Node.js", "Node.js を使う")


def test_記号つきの語は記号まで一致させる() -> None:
    """`C` に `C++` で当てない。**記号の違いは別の言語。**"""
    assert not verify_source.present("C", "C++ で書いた")


def test_強調を落として比べる() -> None:
    """**U12：要約器は `**` を描画後の見た目で読む。**"""
    assert verify_source.present("200", "LLMより**200倍速く**")


def test_リンクは文字だけにして比べる() -> None:
    """**U12：`[個人ブログ](https://…)` を「個人ブログ」として読む。**"""
    assert verify_source.present("Qiita", "[Qiita](https://qiita.com) に書いた")


def test_リンクのURLの中身を根拠にしない() -> None:
    """**URL の中にある語は、本文が主張していることではない。**"""
    assert not verify_source.present("example", "[記事](https://example.com/a) を読んだ")


def test_コードの印を落として比べる() -> None:
    assert verify_source.present("pytest", "`pytest` を使う")


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------


def test_主張が全部本文にあれば照合済み() -> None:
    got = _one("LLM より 200 倍速い。", "Jev は LLMより200倍速い")

    assert got.verdict == verify_source.CONFIRMED
    assert got.missing == ()
    assert got.found == ("LLM", "200")


def test_本文に無い主張があれば印を付ける() -> None:
    """**一致しなかった主張を消さない**（6-2）。*消すと、なぜ落としたかが消える。*"""
    got = _one("Rust で書かれ、2 倍速い。", "Go で書かれ、200倍速い")

    assert got.verdict == verify_source.MISMATCH
    assert got.missing == ("Rust", "2")


def test_1つでも無ければ照合済みにしない() -> None:
    got = _one("LLM と Rust", "LLM の話")

    assert got.verdict == verify_source.MISMATCH
    assert (got.found, got.missing) == (("LLM",), ("Rust",))


def test_主張が無ければ確認できないにする() -> None:
    """**★ M9：照合0件を「一致」にしない。** 何も比べていないのに「問題なし」と言わない。"""
    got = _one("仕組みを分かりやすく説明している。", "本文")

    assert got.verdict == verify_source.UNVERIFIABLE


def test_本文が無ければ確認できないにする() -> None:
    """**M8：読めなかったことと、問題なかったことを分ける。**"""
    got = _one("LLM の話。", None)

    assert got.verdict == verify_source.UNVERIFIABLE


def test_引用は判定に使わない() -> None:
    """**U13：引用が本文にあっても、要約の主張の証拠にならない。**"""
    got = _one("Rust で書かれた。", "本文の一節", quotes=("本文の一節",))

    assert got.verdict == verify_source.MISMATCH


def test_本文に無い引用を記録する() -> None:
    """判定には使わないが、**要約器が本文に無い引用を作った**ことは読む人に見せる。"""
    got = _one(
        "LLM の話。",
        "LLM は**速い**。",
        quotes=("LLM は速い。", "本文に無い一節"),
    )

    assert got.verdict == verify_source.CONFIRMED
    assert got.quotes_missing == ("本文に無い一節",)


# --------------------------------------------------------------------------
# 件数
# --------------------------------------------------------------------------


def test_要約ごとに結果を返す() -> None:
    summaries = [
        _summary("LLM", "LLM", url="https://q.com/a"),
        _summary("Rust", "Go", url="https://q.com/b"),
        _summary("説明している", "本文", url="https://q.com/c"),
    ]

    got = verify_source.verify(summaries)

    assert [c.summary.scored.kept.article.url for c in got.checks] == [
        "https://q.com/a",
        "https://q.com/b",
        "https://q.com/c",
    ]
    assert got.counts == {
        verify_source.CONFIRMED: 1,
        verify_source.MISMATCH: 1,
        verify_source.UNVERIFIABLE: 1,
    }


def test_全部照合済みのときだけ問題なし() -> None:
    assert verify_source.verify([_summary("LLM", "LLM")]).ok is True
    assert verify_source.verify([_summary("Rust", "Go")]).ok is False
    assert verify_source.verify([_summary("説明", "本文")]).ok is False


def test_件数と主張の個数を出す() -> None:
    """**「N個中M個が本文に実在」の形にする**（6-1）。"""
    got = verify_source.verify(
        [_summary("LLM と 200", "LLM 200", url="https://q.com/a"), _summary("Rust", "Go", url="https://q.com/b")]
    )

    assert "2 件中 1 件を照合できた" in got.summary
    assert "主張 3 個中 2 個が本文に実在" in got.summary


def test_見ていないものを出力に書く() -> None:
    """**数字と英語の語しか見ていないことを、文言で隠さない。**"""
    got = verify_source.verify([_summary("LLM", "LLM")])

    assert "数字と英語の語" in got.summary


def test_要約が0件でも落ちない() -> None:
    got = verify_source.verify([])

    assert got.checks == ()
    assert "0 件中 0 件" in got.summary
