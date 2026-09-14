"""task3/tools/shots のテスト。

道具にもテストを付ける。**撮影の直前に落ちるのがいちばん困る**
——実行画面を撮る段になって道具が壊れていると、
*画面を消して整えた状態がやり直しになる*。

============ ====================================================================
守ること     なぜ
============ ====================================================================
折り返し     全角を2で数える。数えないと帯が枠からはみ出て、絵が崩れる
記号落とし   `**` を残すと画面に markdown が出る。ここは端末であって記事ではない
実在の確認   コマンドが**実在するファイル**を指していること。名前を変えたら気づく
番号の一意   同じ番号が2つあると、片方が永久に呼ばれない
============ ====================================================================
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "task3" / "tools"))

import shots  # noqa: E402


def test_全角は2で数える():
    assert shots.width("あい") == 4
    assert shots.width("ab") == 2
    assert shots.width("あb") == 3


def test_幅で折り返す():
    lines = shots.wrap("あいうえお", 4)
    assert lines == ["あい", "うえ", "お"]


def test_強調の記号を落とす():
    """**端末に markdown を出さない。** ここは記事ではない。"""
    assert shots.wrap("**強調**", 40) == ["強調"]


def test_折り返しても文字は落ちない():
    """**幅で折るのは見た目の都合。** 中身が減ったら説明が変わる。"""
    text = "台本と正解データが食い違っていないこと、被り2箇所が両側とも声であること"
    assert "".join(shots.wrap(text, 20)) == text


def test_番号は重複しない():
    numbers = [s[0] for s in shots.STEPS]
    assert len(numbers) == len(set(numbers))


def test_主張が空の手順は無い():
    """**帯に主張が無い絵は、貼っても何も言っていない**（課題2の講評）。"""
    for number, label, claim, _, _ in shots.STEPS:
        assert claim.strip(), number
        assert label.strip(), number


def test_コマンドが実在するファイルを指す():
    """**名前を変えたら気づく。** 撮影の場で「そんなファイルは無い」と出ないように。"""
    for number, _, _, cmd, _ in shots.STEPS:
        if cmd is None:
            continue  # 06 はドキュメントIDが要るので実行時に組む
        for arg in cmd[1:]:
            if arg.endswith(".py") or arg.endswith(".wav") or arg.endswith(".txt") \
               or arg.endswith(".json"):
                assert (ROOT / arg).exists(), "{}: {} が無い".format(number, arg)


def test_06だけは実行時に組む():
    """台帳から引くので、手で貼らせない（貼り間違いで別の文書と比べうる）。"""
    assert [s for s in shots.STEPS if s[3] is None][0][0] == "06"
