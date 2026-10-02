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

    assert got == ("Jev", "LLM", "200倍", "400倍")


def test_同じ主張は1つにまとめる() -> None:
    assert verify_source.claims("LLM と LLM と 3 と 3") == ("LLM", "3")


def test_小数とカンマ区切りを1つの数として抜く() -> None:
    assert verify_source.claims("2.5倍で1,000件") == ("2.5倍", "1000件")


def test_全角の数字と英字を半角にしてから抜く() -> None:
    """要約器は全角で書くことがある。**表記が違うだけで「本文に無い」にしない。**"""
    assert verify_source.claims("ＬＬＭを２００回") == ("LLM", "200回")


# --------------------------------------------------------------------------
# 数は単位ごと抜く（2026-10-02 に実データで漏れた・A）
# --------------------------------------------------------------------------
#
# 境界つきにしても、`2倍速い` は本文の `System 1/2` の `2` で裏付けられてしまった。
# **数だけでは、何の数かが分からない。** 単位が付いているなら、単位ごと照合する。


def test_数のすぐ後ろの単位を一緒に抜く() -> None:
    assert verify_source.claims("200倍速い") == ("200倍",)


def test_数と単位の間の空白を詰めて抜く() -> None:
    """要約器は `200 倍` と空白を入れて書く（2026-09-25 の実物）。"""
    assert verify_source.claims("200 倍速い") == ("200倍",)


def test_英字の単位も一緒に抜く() -> None:
    assert verify_source.claims("100 MB に収まる") == ("100MB",)


def test_英字の単位の後ろに英字が続けば単位にしない() -> None:
    """`5msec` の `ms` は単位ではない。**語の途中を単位として切り取らない。**"""
    assert verify_source.claims("5msec で返る") == ("5", "msec")


def test_単位でない字は付けない() -> None:
    """`3つの命令` の `つ` は単位として扱わない。**知らない字を単位にすると、照合が厳しすぎて外れる。**"""
    assert verify_source.claims("3つの命令") == ("3",)


def test_単位つきの数は単位ごと照合する() -> None:
    """**★ 2026-10-02 に実データで漏れた形そのもの。**

    本文に `2倍` は無いが、`System 1/2` に裸の `2` がある。*数だけで照合すると裏付けに化ける。*
    """
    body = "System 1/2 を使い分ける。LLMより200倍速い"

    assert not verify_source.present("2倍", body)


def test_単位つきの数を本文から見つける() -> None:
    assert verify_source.present("200倍", "LLMより200倍速い")


def test_本文の数と単位の間に空白があっても見つける() -> None:
    assert verify_source.present("200倍", "LLMより 200 倍速い")
    assert verify_source.present("100MB", "100 MB に収まる")


def test_単位が違えば別の主張() -> None:
    assert not verify_source.present("200件", "200倍速い")


def test_単位つきでも数の一部には当てない() -> None:
    assert not verify_source.present("2倍", "LLMより200倍速い")
    assert not verify_source.present("50件", "150件を処理した")


def test_英字の単位の後ろに英字が続けば別の単位() -> None:
    """`5ms` は `5msec` の一部ではない……のではなく、**`5m` を `5ms` で裏付けない。**"""
    assert not verify_source.present("5m", "5ms で返る")


# --------------------------------------------------------------------------
# レビューで出た「本文が裏付けていないのに照合済み」（2026-10-02）
# --------------------------------------------------------------------------
#
# **照合済みの誤りがいちばん重い。** 「本文に無い」の誤りは読む人が開いて確かめるが、
# 「照合済み」と出たものは誰も開かない。


def test_数と単位の間で行をまたがない() -> None:
    """見出しや箇条書きの番号が行末に来て、次の行の頭の字と組んでしまう。"""
    assert not verify_source.present("5秒", "Step 5\n\n秒速で動く")
    assert not verify_source.present("2日", "バージョン 2\n日本語版")


def test_上付き文字を数にしない() -> None:
    """NFKC は `10²` を `102` にする。**本文に無い数が生まれる。**"""
    assert not verify_source.present("102", "計算量は 10² で済む")


def test_分数を数にしない() -> None:
    """NFKC は `1½` を `11⁄2` にする。**`2倍` が分母の `2` で裏付けられる。**"""
    assert not verify_source.present("2倍", "1½倍に伸びた")


def test_要約の上付き文字も数にしない() -> None:
    assert verify_source.claims("10² 回") == ("10",)


def test_短い語は大小を区別する() -> None:
    """`Go` を英文の `go` で裏付けない。**3字以下の語は、大小が違えば別の語として扱う。**"""
    assert not verify_source.present("Go", "Let's go to the store")
    assert not verify_source.present("AI", "Thai food")  # 境界で弾かれるが、大小でも弾く
    assert not verify_source.present("IF", "if (x) {}")


def test_3字の語も大小を区別する() -> None:
    assert not verify_source.present("API", "api の話")


def test_長い語は大小を問わない() -> None:
    assert verify_source.present("Python", "python で書いた")
    assert verify_source.present("Rust", "rust で書いた")


def test_下付き文字も数にしない() -> None:
    """`CO₂` を `CO2` にしない。**語に本文に無い数字が混ざる。**"""
    assert verify_source.claims("CO₂ を 30% 削減") == ("CO", "30%")


def test_3桁ずつでない区切りをつなげない() -> None:
    assert not verify_source.present("12345", "値は 1,2345")
    assert not verify_source.present("1234567", "値は 1234,567")


def test_空の主張は本文にあることにしない() -> None:
    """**空文字はどこにでもある。** `claims()` は空を出さないが、公開した関数として閉じる。"""
    assert not verify_source.present("", "本文")


def test_桁区切りでないカンマは落とさない() -> None:
    """`1,2,3` は**3つの数**。カンマを全部落とすと `123` になる。"""
    assert not verify_source.present("123", "手順は 1,2,3 の順")
    assert not verify_source.present("123", "値は 1,23 だった")


def test_桁区切りでないカンマで数をつなげて抜かない() -> None:
    assert verify_source.claims("手順 1,2,3") == ("1", "2", "3")


def test_桁区切りのカンマは何組でも落とす() -> None:
    assert verify_source.claims("12,345,678件") == ("12345678件",)
    assert verify_source.present("12345678", "12,345,678 件")


def test_英数字以外の数字で落ちない() -> None:
    """`str.isdigit()` はアラビア数字の `٣` も数字と答える。**照合で例外を出さない。**"""
    assert not verify_source.present("٣", "本文")


# --------------------------------------------------------------------------
# 1桁の裸の数は弱い（B）
# --------------------------------------------------------------------------
#
# 単位の無い1桁の数は、本文の見出し番号・箇条書き・`System 1` に必ずある。
# **見つかっても何も証明しないので、照合に使わない。** ただし数は出す（隠さない）。


def test_単位の無い1桁の数は弱い() -> None:
    assert verify_source.weak("3")
    assert verify_source.weak("２")


def test_単位つき・2桁以上・小数は弱くない() -> None:
    assert not verify_source.weak("2倍")
    assert not verify_source.weak("12")
    assert not verify_source.weak("2.5")


def test_語は弱くない() -> None:
    """`IF` のような短い語も弱いが、**語は境界つきで比べるので数ほどには当たらない。** 今回は数だけ。"""
    assert not verify_source.weak("IF")


def test_語に付く記号を抜く() -> None:
    """`C++`・`C#`・`Node.js` は**記号まで含めて1つの名前**。"""
    assert verify_source.claims("C++ と C# と Node.js") == ("C++", "C#", "Node.js")


def test_文末の句点を語に含めない() -> None:
    assert verify_source.claims("使うのは Python.") == ("Python",)


def test_語の後ろのハイフンを含めない() -> None:
    """`API-とは` の `-` は語ではない。含めると、本文の `API` と別物になる。"""
    assert verify_source.claims("API-とは") == ("API",)


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


def test_前に数字が続く数にも当てない() -> None:
    """後ろだけでなく前も見る。`50件` を本文の `150件` で裏付けない。"""
    assert not verify_source.present("50", "150件を処理した")


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


def test_語の後ろに続きがあれば別の語() -> None:
    """`Node` は `Node.js` ではない。**記号の後ろに英数字が続けば、まだ同じ語の中。**"""
    assert not verify_source.present("Node", "Node.js を使う")


def test_語の前に英字があれば別の語() -> None:
    assert not verify_source.present("Script", "JavaScript で書いた")


def test_語の後ろの句点は境界にする() -> None:
    assert verify_source.present("Python", "使うのは Python.")


def test_全角で書かれた本文でも見つける() -> None:
    assert verify_source.present("LLM", "ＬＬＭの話")
    assert verify_source.present("200", "２００倍")


def test_全角で書かれた主張でも見つける() -> None:
    assert verify_source.present("２００", "200倍")


def test_カンマ区切りの主張でも見つける() -> None:
    assert verify_source.present("1,000", "1000件")


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


def test_下線の強調も落として比べる() -> None:
    """**語で確かめる。** 数の境界は数字しか見ないので、`__200__` は落とさなくても当たる
    ——2026-10-02 のミューテーションで素通りした。語は `_` を語の一部とみなすので、効く。
    """
    assert verify_source.present("Python", "__Python__ で書いた")


def test_リンクは文字だけにして比べる() -> None:
    """**U12：`[個人ブログ](https://…)` を「個人ブログ」として読む。**"""
    assert verify_source.present("Qiita", "[Qiita](https://qiita.com) に書いた")


def test_リンクのURLの中身を根拠にしない() -> None:
    """**URL の中にある語は、本文が主張していることではない。**"""
    assert not verify_source.present("example", "[記事](https://example.com/a) を読んだ")


def test_むき出しのURLの中身も根拠にしない() -> None:
    """リンクの形になっていない URL も同じ。**URL のホスト名は、本文の主張ではない。**"""
    # `example.com` で書くと「後ろに `.` ＋英字が続けば別の語」の規則で先に弾かれ、
    # **URL を落とす規則を1度も通らない**（2026-10-02 のミューテーションで素通りした）。
    # だから `.` の続かないパスの語で確かめる。
    assert not verify_source.present("items", "参考: https://qiita.com/ak33/items/x を読んだ")


def test_コードの印を落として比べる() -> None:
    assert verify_source.present("pytest", "`pytest` を使う")


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------


def test_主張が全部本文にあれば照合済み() -> None:
    got = _one("LLM より 200 倍速い。", "Jev は LLMより200倍速い")

    assert got.verdict == verify_source.CONFIRMED
    assert got.missing == ()
    assert got.found == ("LLM", "200倍")


def test_本文に無い主張があれば印を付ける() -> None:
    """**一致しなかった主張を消さない**（6-2）。*消すと、なぜ落としたかが消える。*"""
    got = _one("Rust で書かれ、2 倍速い。", "Go で書かれ、200倍速い")

    assert got.verdict == verify_source.MISMATCH
    assert got.missing == ("Rust", "2倍")


def test_実データで漏れた罠を捕まえる() -> None:
    """**★ 2026-10-02：中央値の記事に「2倍速い」を仕込んだら、照合済みになった。**"""
    got = _one(
        "Jev は LLM より 2 倍速い。",
        "System 1/2 を使い分ける。Jev は LLMより**200倍速く**",
    )

    assert got.verdict == verify_source.MISMATCH
    assert got.missing == ("2倍",)


def test_弱い数は見つかっても見つからなくても数えない() -> None:
    got = _one("COBOL は 3 つの命令で読める。", "COBOL の話")

    assert got.verdict == verify_source.CONFIRMED
    assert (got.found, got.missing, got.weak) == (("COBOL",), (), ("3",))


def test_弱い数しか無ければ確認できない() -> None:
    """**弱い主張だけで「照合済み」と言わない**（M9 と同じ形）。"""
    got = _one("手順は 3 つ。", "手順 1 2 3")

    assert got.verdict == verify_source.UNVERIFIABLE
    assert got.weak == ("3",)


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


def test_リンクを含む引用を本文に無いことにしない() -> None:
    """**U12 で実測した形そのもの。** 原文 `私の[個人ブログ](https://…)に` を、
    要約器は `私の個人ブログに` と書いた。
    """
    got = _one(
        "LLM の話。",
        "LLM の話。私の[個人ブログ](https://www.example.jp/diary/)に書き溜めた",
        quotes=("私の個人ブログに書き溜めた",),
    )

    assert got.quotes_missing == ()


def test_全角で書かれた引用を本文に無いことにしない() -> None:
    got = _one("LLM の話。", "LLM は速い。", quotes=("ＬＬＭ は速い。",))

    assert got.quotes_missing == ()


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


def test_弱くて使わなかった数を出す() -> None:
    """**使わなかったことを隠さない。** 見えないと、照合が全部を見たように読める。"""
    got = verify_source.verify([_summary("COBOL と 3 つ", "COBOL")])

    assert "弱くて照合に使わなかった数 1 個" in got.summary


def test_見ていないものを出力に書く() -> None:
    """**数字と英語の語しか見ていないことを、文言で隠さない。**"""
    got = verify_source.verify([_summary("LLM", "LLM")])

    assert "数字と英語の語" in got.summary


def test_要約が0件でも落ちない() -> None:
    got = verify_source.verify([])

    assert got.checks == ()
    assert "0 件中 0 件" in got.summary
