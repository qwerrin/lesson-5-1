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
生成物の除外 追跡していないものの**存在を要求しない**。他人の手元では必ず落ちる
番号の一意   同じ番号が2つあると、片方が永久に呼ばれない
============ ====================================================================
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "task3" / "tools"))

import shots  # noqa: E402

#: 追跡していない生成物の拡張子。**存在を要求してはいけない。**
#:
#: `.gitignore` で外してあるので、**クローンした人の手元にも、
#: ミューテーションの写し（`*.wav` を除外している）にも無い**。
#: 存在を求めると *他人の環境では必ず落ちるテスト* になる
#: ——2026-09-14 に実際そうなり、ミューテーションが
#: 「壊す前からテストが落ちています」で止まった。
#:
#: **同じ日に2回踏んだ形。** `posted.json`（台帳）を追跡すると
#: クローンした人が一度も動かせない、というのと**同じ間違い**である
#: ——*生成物を前提にすると、自分の手元でしか通らない*。
GENERATED = (".wav",)

CHECKED = (".py", ".txt", ".json") + GENERATED


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


def check_paths(steps) -> None:
    """手順のコマンドが指す先を確かめる。

    生成物だけは**置き場**を見る。ファイルそのものは手元にしか無いが、
    *フォルダの名前を変えたら気づきたい*ので、親までは確かめる。
    """
    for number, _, _, cmd, _ in steps:
        if cmd is None:
            continue  # 06 はドキュメントIDが要るので実行時に組む
        for arg in cmd[1:]:
            if not arg.endswith(CHECKED):
                continue
            target = ROOT / arg
            if arg.endswith(GENERATED):
                assert target.parent.is_dir(), "{}: {} の置き場が無い".format(number, arg)
            else:
                assert target.exists(), "{}: {} が無い".format(number, arg)


def test_コマンドが実在するファイルを指す():
    """**名前を変えたら気づく。** 撮影の場で「そんなファイルは無い」と出ないように。"""
    check_paths(shots.STEPS)


def test_生成物は存在を要求しない():
    """**この判定そのものを検査する。** 手元にあると、区別できているか分からない。

    実在しない `.wav` を混ぜた偽の手順で、**無いファイルでも通ること**を確かめる。
    *手元にある状態でだけ試すと、生成物の分岐を1度も通らない。*
    """
    check_paths([("99", "偽", "偽", ["py", "task3/meeting/ありえない.wav"], False)])


def test_生成物でもフォルダが無ければ落ちる():
    """置き場まで見なくなったら、フォルダの改名に気づけない。"""
    with pytest.raises(AssertionError):
        check_paths([("99", "偽", "偽", ["py", "task3/そんなフォルダは無い/x.wav"], False)])


def test_追跡しているファイルは存在を要求する():
    """生成物の扱いを広げすぎると、**改名を1つも捕まえなくなる**。"""
    with pytest.raises(AssertionError):
        check_paths([("99", "偽", "偽", ["py", "task3/ありえない.py"], False)])


def test_06だけは実行時に組む():
    """台帳から引くので、手で貼らせない（貼り間違いで別の文書と比べうる）。"""
    assert [s for s in shots.STEPS if s[3] is None][0][0] == "06"
