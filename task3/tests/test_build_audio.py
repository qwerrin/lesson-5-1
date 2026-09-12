"""task3/tools/build_audio のテスト。**実装より先に書いた。**

守らせる対象は `task3/DESIGN.md` と `task3/meeting/script.md`。
音声素材は**成果物ではなく物差し**なので、ここが狂うと
`verify_source.py` の照合が丸ごと意味を失う。

============ ====================================================================
守ること     なぜ
============ ====================================================================
台本の抽出   フェンスが無いのに0行を返したら、**無音の wav を作って成功する**
3つ組の分割  本文に `|` が入っても壊れない。区切りは最初の2つだけ
正解との照合 台本を直して正解 JSON を直し忘れると、**照合器が嘘をつき続ける**
不在の照合   `absent_from_audio` が台本に**在ったら** 4-② の実験が成立しない
時間割       負の間隔で**実際に重なる**。重なりすぎたら順序が壊れるので止める
重ね合わせ   重なった区間で**両方の振幅が残る**。上書きでは T4 を仕込めていない
飽和         int16 を超えたら**折り返さずに張り付く**。折り返すと轟音になる
============ ====================================================================

**「重ねたつもりで上書き」は音を聞いても気づけない。** 片方の声しか聞こえない結果は、
「被りは拾えなかった」という実験結果と**まったく同じ見た目**になる
——*仕込めていない罠は、突破された罠と区別が付かない*（DESIGN 3.4 で実際に踏んだ形）。

**numpy は使わない。** 素材生成は成果物ではないので、採点者に増やさせる
`pip install` を1つ増やす価値がない。`array` と `wave` で足りる。
"""

from __future__ import annotations

import sys
from array import array
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import build_audio  # noqa: E402


REPO = Path(__file__).resolve().parents[1]
SCRIPT_MD = REPO / "meeting" / "script.md"

FENCE = "```"

MINIMAL = (
    "# 見出し\n\n"
    + FENCE + "text\n"
    "Ichiro|0.3|おはようございます。\n"
    "Haruka|0.4|小林です。\n"
    "Ayumi|-0.8|あの、すみません。\n"
    + FENCE + "\n\n"
    + FENCE + "json\n"
    '{"meeting": {"speakers": {"Ichiro": "田村", "Haruka": "小林", "Ayumi": "佐藤"}},\n'
    ' "confusables": [], "numbers": [], "absent_from_audio": []}\n'
    + FENCE + "\n"
)


def pcm(value, count):
    """一定の振幅が続くだけの 16bit PCM。**足し算の結果を目で確かめられる形にする。**"""
    return array("h", [value] * count)


# ---------------------------------------------------------------- 台本の抽出

def test_台本のフェンスから行が取れる():
    lines = build_audio.parse_script(MINIMAL)
    assert [l.voice for l in lines] == ["Ichiro", "Haruka", "Ayumi"]
    assert lines[0].gap == 0.3
    assert lines[2].gap == -0.8
    assert lines[1].text == "小林です。"


def test_本文に区切り文字が入っても壊れない():
    md = MINIMAL.replace("Haruka|0.4|小林です。", "Haruka|0.4|税抜3980|4380のどちらかです。")
    assert build_audio.parse_script(md)[1].text == "税抜3980|4380のどちらかです。"


def test_台本のフェンスが無ければ例外():
    """**0行を静かに返さない。** 返すと無音の wav を作って成功してしまう。"""
    with pytest.raises(ValueError, match="フェンスが見つからない"):
        build_audio.parse_script("# 見出しだけ\n\n本文\n")


def test_台本のフェンスが空でも例外():
    with pytest.raises(ValueError, match="0行"):
        build_audio.parse_script("# x\n\n" + FENCE + "text\n" + FENCE + "\n")


def test_間隔が数でなければ例外():
    md = MINIMAL.replace("Haruka|0.4|", "Haruka|あとで|")
    with pytest.raises(ValueError):
        build_audio.parse_script(md)


# ------------------------------------------------------------------ 正解の抽出

def test_正解のJSONが読める():
    assert build_audio.parse_truth(MINIMAL)["meeting"]["speakers"]["Ichiro"] == "田村"


def test_正解のフェンスが無ければ例外():
    """JSON の構文エラーも ValueError なので、**理由で区別しないと素通りする。**"""
    with pytest.raises(ValueError, match="フェンスが見つからない"):
        build_audio.parse_truth("# 見出しだけ\n")


# -------------------------------------------------- 台本と正解が食い違わないか

def test_知らない声名を見つける():
    """**TTS を44回叩いた後で落ちるのでは遅い。** 読んだ時点で全部見る。"""
    md = MINIMAL.replace("Ayumi|-0.8|", "Sayaka|-0.8|")
    problems = build_audio.audit(build_audio.parse_script(md), build_audio.parse_truth(md))
    assert any("Sayaka" in p for p in problems)


def test_正解にあるのに台本に無い語を見つける():
    md = MINIMAL.replace(
        '"confusables": []',
        '"confusables": [{"correct": "大和物産", "wrong": "大和商事", "about": "x"}]',
    )
    problems = build_audio.audit(build_audio.parse_script(md), build_audio.parse_truth(md))
    assert any("大和物産" in p for p in problems)


def test_正解の数値が台本に無ければ見つける():
    md = MINIMAL.replace(
        '"numbers": []',
        '"numbers": [{"spoken": "千八百個", "value": "1800", "about": "x"}]',
    )
    problems = build_audio.audit(build_audio.parse_script(md), build_audio.parse_truth(md))
    assert any("千八百個" in p for p in problems)


def test_音声に無いはずの語が台本にあれば見つける():
    """`absent_from_audio` は**逆向きの検査**。在ったら 4-② の実験が成立しない。"""
    md = MINIMAL.replace(
        '"absent_from_audio": []',
        '"absent_from_audio": [{"text": "小林です", "about": "x"}]',
    )
    problems = build_audio.audit(build_audio.parse_script(md), build_audio.parse_truth(md))
    assert any("小林です" in p for p in problems)


def test_食い違いが無ければ空を返す():
    assert build_audio.audit(
        build_audio.parse_script(MINIMAL), build_audio.parse_truth(MINIMAL)
    ) == []


# ------------------------------------------------------------------ 時間割

def test_間隔どおりに並ぶ():
    assert build_audio.plan_starts([1.0, 2.0], [0.5, 0.25]) == pytest.approx([0.5, 1.75])


def test_負の間隔で実際に重なる():
    """開始が**前の終了より前**に来ていなければ、被りは作れていない。"""
    starts = build_audio.plan_starts([1.0, 2.0], [0.0, -0.8])
    assert starts[1] < 1.0
    assert starts[1] == pytest.approx(0.2)


def test_重なりすぎたら例外():
    """直前より前から始まると**順序が入れ替わる**。無言で並べ替えない。"""
    with pytest.raises(ValueError, match="開始が負"):
        build_audio.plan_starts([1.0, 2.0], [0.0, -1.5])


def test_順序が壊れる重なりは負でなくても例外():
    """**0以上でも、直前より前なら順序が壊れる。**

    上のケースは `s < 0` のガードに先に当たっていて、順序のガードを
    一度も通っていなかった（ミューテーションで素通りして分かった）。
    """
    with pytest.raises(ValueError, match="重ねすぎ"):
        build_audio.plan_starts([1.0, 2.0, 1.0], [1.0, 0.0, -3.0])


def test_先頭が負なら例外():
    with pytest.raises(ValueError):
        build_audio.plan_starts([1.0], [-0.1])


# ---------------------------------------------------------------- 重ね合わせ

def test_重なった区間で両方の振幅が残る():
    """**上書きだと片方が消える。** 消えた結果は「拾えなかった」と見分けが付かない。"""
    out = build_audio.mix([pcm(1000, 100), pcm(300, 100)], [0, 60])
    assert out.typecode == "h"
    assert len(out) == 160
    assert out[0] == 1000      # a だけ
    assert out[70] == 1300     # 重なり＝両方
    assert out[150] == 300     # b だけ


def test_出力の長さは最後の終了位置():
    assert len(build_audio.mix([pcm(1, 10), pcm(1, 10)], [0, 100])) == 110


def test_隙間は無音になる():
    out = build_audio.mix([pcm(1, 10), pcm(1, 10)], [0, 100])
    assert set(out[10:100]) == {0}


def test_飽和しても折り返さない():
    """int16 を超えたら張り付く。**折り返すと大音量が負に化けて轟音になる。**"""
    out = build_audio.mix([pcm(20000, 10), pcm(20000, 10)], [0, 0])
    assert max(out) == 32767
    assert min(out) > 0


def test_負の側も飽和する():
    out = build_audio.mix([pcm(-20000, 10), pcm(-20000, 10)], [0, 0])
    assert min(out) == -32768
    assert max(out) < 0


# -------------------------------------------------- 実物の台本を物差しにする

def test_実物の台本が読めて正解と食い違わない():
    md = SCRIPT_MD.read_text(encoding="utf-8")
    lines = build_audio.parse_script(md)
    assert len(lines) >= 40
    assert build_audio.audit(lines, build_audio.parse_truth(md)) == []


def test_実物の台本に被りが1つ以上ある():
    """T4 が**台本の側から消えていない**ことを見る。"""
    lines = build_audio.parse_script(SCRIPT_MD.read_text(encoding="utf-8"))
    assert any(l.gap < 0 for l in lines)


# ------------------------------------------------- 無音の切り落としと被りの実効

def test_前後の無音を落とす():
    s = array("h", [0] * 100 + [8000] * 50 + [0] * 200)
    out = build_audio.trim_edges(s, rate=1000, threshold=256, keep=0.0)
    assert len(out) == 50
    assert set(out) == {8000}


def test_余白を指定した秒数だけ残す():
    """**声の立ち上がりを削らない。** ぴったり切ると子音の頭が消える。"""
    s = array("h", [0] * 100 + [8000] * 50 + [0] * 200)
    out = build_audio.trim_edges(s, rate=1000, threshold=256, keep=0.02)
    assert len(out) == 50 + 20 + 20


def test_余白は元の長さを超えない():
    s = array("h", [0] * 5 + [8000] * 10 + [0] * 5)
    out = build_audio.trim_edges(s, rate=1000, threshold=256, keep=1.0)
    assert len(out) == 20


def test_全部無音なら例外():
    """**空の発話は静かに通さない。** 通すと台本の1行が音として消える。"""
    with pytest.raises(ValueError):
        build_audio.trim_edges(array("h", [0] * 100), rate=1000)


def test_閾値より大きい音は削らない():
    s = array("h", [8000] * 30)
    assert len(build_audio.trim_edges(s, rate=1000, threshold=256, keep=0.0)) == 30


def test_実効値を出す():
    assert build_audio.rms(array("h", [3, -4])) == pytest.approx(3.5355, rel=1e-3)
    assert build_audio.rms(array("h", [])) == 0.0


def test_被りの両側の実効値を返す():
    """**片側が無音なら、被りは仕込めていない。**

    2026-09-12 に実際に踏んだ形。混ぜ方は正しかったが、重なったのは
    TTS が発話の末尾に付ける 0.8 秒の無音だった——*足し算は合っているのに、
    声としては1ミリも被っていない*。
    """
    loud = array("h", [8000] * 100)
    quiet = array("h", [5] * 100)
    # 0番の末尾50と、1番の冒頭50が重なる
    got = build_audio.overlap_report([loud, quiet], [0, 50], [0.0, -0.5], rate=100)
    assert len(got) == 1
    o = got[0]
    assert o["index"] == 1
    assert o["seconds"] == pytest.approx(0.5)
    assert o["rms_prev"] > 1000
    assert o["rms_next"] < 100


def test_被りが無ければ空を返す():
    loud = array("h", [8000] * 100)
    assert build_audio.overlap_report([loud, loud], [0, 100], [0.0, 0.0], rate=100) == []


def test_正の間隔は重なって見えても被りに数えない():
    """**数えるのは「わざと重ねたもの」だけ。**

    開始位置は `round(秒 * 標本化周波数)` で出すので、正の間隔でも
    1サンプルだけ重なることがある。それを被りとして数えると、
    *仕込んだ罠の件数に、丸め誤差が混ざる*。
    """
    loud = array("h", [8000] * 100)
    assert build_audio.overlap_report([loud, loud], [0, 50], [0.0, 0.5], rate=100) == []
