"""task3/verify_source のテスト。**実装より先に書いた。**

**照合の相手は台本（ソース）で、文字起こしではない。**

`minutes.py` の照合は「議事録が文字起こしに忠実か」しか見ない。
2026-09-12 の実機で、**全ての引用が逐語で、時刻も合い、検査は「問題なし」を
返した議事録**に、文字起こしの誤変換（特集→特許・試算→資産）が
そのまま入っていた。*下流が上流に忠実であることを検査すると、
上流の誤りが「正しい」と証明されてしまう。*

============ ====================================================================
根拠         ここで守ること
============ ====================================================================
課題2の講評   **ソース側を開いて突き合わせる**。出力を2回読むのは照合ではない
5-C          数値・固有名詞の誤変換を、台本との差として出す
5-J          担当が入れ替わっていないか（台本の正解と比べる）
5-K          撤回された側（千二百個・大和商事・木曜）が決定になっていないか
DESIGN 6章   論点が決定へ格上げされていないか
============ ====================================================================

**逐語一致は期待できない。** 引用は TTS → 文字起こし を通っているので、
台本とは必ず少し違う。だから `difflib` で**いちばん近い台本の行**を探し、
*違う文字だけ*を出す。閾値で「見つからない」と「違う」を分ける。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "task3"))
sys.path.insert(0, str(ROOT / "task3" / "tools"))

import build_audio  # noqa: E402  台本の Line を借りる（tools/ にある）
import minutes  # noqa: E402
import verify_source  # noqa: E402

NL = chr(10)

SCRIPT = [
    build_audio.Line("Ichiro", 0.3, "おはようございます。週次定例を始めます。"),
    build_audio.Line("Haruka", 0.4, "では初回は千八百個に変更でお願いします。千二百ではなく千八百です。"),
    build_audio.Line("Ichiro", 0.4, "大和物産に切り替えます。契約書の準備は小林さんでお願いします。"),
    build_audio.Line("Ayumi", 0.4, "値上げのタイミングは特集の前と後、どちらがいいですか。"),
    build_audio.Line("Ichiro", 0.4, "これは持ち帰りにしましょう。"),
]

TRUTH = {
    "decisions": [
        {"id": "D1", "text": "初回発注は1800個", "must_contain": ["1800"],
         "must_not_contain": ["1200"]},
        {"id": "D2", "text": "大和物産へ切り替える", "must_contain": ["大和物産"],
         "must_not_contain": []},
    ],
    "todos": [
        {"id": "A1", "owner": "小林", "text": "大和物産との契約書を用意する", "due": "10月3日"},
    ],
    "open_issues": [
        {"id": "O1", "text": "モイストセラムの価格改定のタイミング", "note": "持ち帰り"},
    ],
    "confusables": [
        {"correct": "大和物産", "wrong": "大和商事", "about": "切り替え先"},
        {"correct": "千八百個", "wrong": "千二百個", "about": "発注数"},
    ],
    "absent_from_audio": [{"text": "10/7", "about": "チャットにのみある"}],
}


def item(kind, text, quote, *, owner="", source="audio"):
    return minutes.Item(kind=kind, text=text, at=0, quote=quote, source=source, owner=owner)


# ------------------------------------------------------------------ 正規化

def test_句読点を落とす():
    """**文字起こしの句読点は当てにならない。** 落とさないと差分が句読点で埋まる。"""
    a = verify_source.normalize_for_match("これは、持ち帰りにしましょう。")
    b = verify_source.normalize_for_match("これは持ち帰りにしましょう")
    assert a == b


def test_語の違いは残す():
    """**そろえすぎない。** 特集と特許が同じになったら、この層は無意味になる。"""
    assert verify_source.normalize_for_match("特集") != verify_source.normalize_for_match("特許")


# ------------------------------------------------------------------ 近さ

def test_完全一致は1():
    assert verify_source.coverage("あいうえお", "あいうえお") == pytest.approx(1.0)


def test_一部が欠けても高い():
    assert verify_source.coverage("あいうえおかきくけこ", "あいうえお") == pytest.approx(1.0)


def test_無関係なら低い():
    assert verify_source.coverage("あいうえお", "たちつてと") < 0.3


def test_引用が空なら0():
    """**空文字はどんな文字列にも含まれる。** 1.0 にすると何でも一致になる。"""
    assert verify_source.coverage("あいうえお", "") == 0.0


def test_読みの置き換えは長いものから():
    """**短い読みが先に当たると、長い読みが割れて別物になる。**

    「二十日」に「十日→10日」を先に当てると「二10日」になる。
    """
    truth = {"numbers": [
        {"spoken": "十日", "value": "10日"},
        {"spoken": "二十日", "value": "20日"},
    ]}
    assert verify_source.apply_readings("二十日", truth) == "20日"


def test_いちばん近い台本の行を選ぶ():
    ratio, span = verify_source.closest("大和物産に切り替えます", SCRIPT)
    assert ratio > 0.9
    assert "大和物産" in span


def test_行をまたぐ引用も見つかる():
    """台本の1行に収まらない引用がある。**隣り合う行も候補にする。**"""
    ratio, _ = verify_source.closest(
        "どちらがいいですかこれは持ち帰りにしましょう", SCRIPT
    )
    assert ratio > 0.9


# ------------------------------------------------------------------ 差分

def test_違う文字だけを出す():
    """**本命。** 2026-09-12 の実機で実際に起きた誤変換。"""
    marks = verify_source.diff_marks("値上げのタイミングは特集の前と後", "値上げのタイミングは特許の前と後")
    # **文脈つきで出す。** `集→許` だけだと、どの語が変わったのか読めない。
    assert marks == "は特[集→許]の前"


def test_複数の違いを並べる():
    marks = verify_source.diff_marks("特集と試算", "特許と資産")
    assert "[集→許]" in marks and "[試算→資産]" in marks
    assert " / " in marks


def test_同じなら空():
    assert verify_source.diff_marks("あいう", "あいう") == ""


# --------------------------------------------------- 雑音を出さないための3つ

def test_台本側は一致した範囲だけを見る():
    """**候補の行まるごとと比べない。**

    隣り合う2行をつないだ候補が選ばれると、引用に対応しない前半が
    「が欠落」として大量に出る。*雑音が多い検査は、本物を隠す。*
    """
    ratio, span = verify_source.closest("大和物産に切り替えます", SCRIPT)
    assert span.startswith("大和物産")
    assert "おはよう" not in span


def test_読みと表記の違いを誤りにしない():
    """台本は「千八百個」、議事録は「1800個」。**どちらも正しい。**

    対応は正解データ（`numbers` の spoken / value）が持っている。
    *持っている情報を使わずに「違う」と言うのは、検査ではなく雑音。*
    """
    truth = dict(TRUTH, numbers=[{"spoken": "千八百個", "value": "1800個", "about": "発注数"}])
    items = [item("decision", "初回発注は1800個", "では初回は1800個に変更でお願いします")]
    findings, checked, matched = verify_source.check_quotes(items, SCRIPT, "", truth)
    assert matched == 1
    assert findings == []


def test_読みの対応が無ければ違いとして出す():
    """**正解データに書いていないものまで見逃さない。**"""
    items = [item("decision", "初回発注は1800個", "では初回は1800個に変更でお願いします")]
    findings, _, matched = verify_source.check_quotes(items, SCRIPT, "", TRUTH)
    assert matched == 0
    assert findings


def test_敬称の違いを担当の食い違いにしない():
    """台本は「小林」、議事録は「小林さん」。**同じ人。**

    決定を空で渡しているので `must_contain` の所見は出る。
    ここで見たいのは**担当の所見が出ないこと**なので、種類で絞る。
    """
    todos = [item("todo", "大和物産との契約書を用意する", "q", owner="小林さん")]
    got = verify_source.check_truth([], todos, [], TRUTH)
    assert [f for f in got if f.kind == "owner"] == []


def test_別人なら敬称があっても見つける():
    todos = [item("todo", "大和物産との契約書を用意する", "q", owner="佐藤さん")]
    got = verify_source.check_truth([], todos, [], TRUTH)
    assert any("佐藤" in f.message for f in got)


def test_担当が不明なら見つける():
    """**「不明」は合っていない。** 台本には担当が書いてある。"""
    todos = [item("todo", "大和物産との契約書を用意する", "q", owner="不明")]
    got = verify_source.check_truth([], todos, [], TRUTH)
    assert any("不明" in f.message for f in got)


# ------------------------------------------------------------------ 引用の照合

def test_台本と一致すれば所見なし():
    items = [item("decision", "大和物産へ切り替える", "大和物産に切り替えます")]
    findings, checked, matched = verify_source.check_quotes(items, SCRIPT, "", TRUTH)
    assert findings == []
    assert (checked, matched) == (1, 1)


def test_台本と1語違えば見つける():
    """**minutes.py はこれを通した。** 文字起こしには逐語で存在していたため。"""
    items = [item("issue", "値上げのタイミング", "値上げのタイミングは特許の前と後どちらがいいですか")]
    findings, checked, matched = verify_source.check_quotes(items, SCRIPT, "", TRUTH)
    assert matched == 0
    assert any("[集→許]" in f.message and "特" in f.message for f in findings)


def test_引用が空なら照合の件数に入れない():
    """**空の引用は「照合した」に数えない。** 数えると分母が水増しされ、
    *N件中N件という数字が、実際より良く見える*。
    """
    items = [item("decision", "根拠なしの決定", "")]
    findings, checked, matched = verify_source.check_quotes(items, SCRIPT, "", TRUTH)
    assert (checked, matched) == (0, 0)
    assert any("引用が空" in f.message for f in findings)


def test_台本に無い引用を見つける():
    items = [item("decision", "架空の決定", "来週から全社でリモート勤務になります")]
    findings, _, matched = verify_source.check_quotes(items, SCRIPT, "", TRUTH)
    assert matched == 0
    assert any("見つかりません" in f.message for f in findings)


def test_チャット由来は台本と照合しない():
    """**チャットは音声に無いのが正しい。** 台本で探すと必ず「無い」になる。"""
    chat = "10:26 田村 次回は10/7（火）10:00〜 でカレンダー入れておきます"
    items = [item("todo", "次回の日程", "次回は10/7（火）10:00〜 でカレンダー入れておきます",
                  source="chat")]
    findings, checked, matched = verify_source.check_quotes(items, SCRIPT, chat, TRUTH)
    assert findings == []
    assert (checked, matched) == (1, 1)


def test_チャットにも無ければ見つける():
    items = [item("todo", "次回の日程", "次回は11/4に決まりました", source="chat")]
    findings, _, matched = verify_source.check_quotes(items, SCRIPT, "10:26 田村 次回は10/7", TRUTH)
    assert matched == 0
    assert findings


# ------------------------------------------------------------------ 正解との照合

def test_決定に必要な語が無ければ見つける():
    got = verify_source.check_truth([item("decision", "初回発注を増やす", "q")], [], [], TRUTH)
    assert any("1800" in f.message for f in got)


def test_撤回された側が決定にあれば見つける():
    """**5-K。** 言い直しの前の値が決定として残る。"""
    got = verify_source.check_truth(
        [item("decision", "初回発注は1200個", "q"), item("decision", "大和物産へ", "q")],
        [], [], TRUTH,
    )
    assert any("1200" in f.message for f in got)


def test_誤った固有名詞が決定にあれば見つける():
    got = verify_source.check_truth(
        [item("decision", "大和商事に切り替える", "q")], [], [], TRUTH
    )
    assert any("大和商事" in f.message for f in got)


def test_論点が決定に格上げされていたら見つける():
    """**T6。** 持ち帰りになったものが決定として並ぶ。"""
    got = verify_source.check_truth(
        [item("decision", "モイストセラムの価格改定のタイミングを決定", "q")], [], [], TRUTH
    )
    assert any("論点" in f.message for f in got)


def test_担当が入れ替わっていたら見つける():
    """**5-J。** 話者ラベルの取り違えは、担当の食い違いとして表に出る。"""
    todos = [item("todo", "大和物産との契約書を用意する", "q", owner="佐藤")]
    got = verify_source.check_truth([], todos, [], TRUTH)
    assert any("小林" in f.message and "佐藤" in f.message for f in got)


def test_担当が合っていれば所見なし():
    todos = [item("todo", "大和物産との契約書を用意する", "q", owner="小林")]
    decisions = [item("decision", "初回発注は1800個", "q"),
                 item("decision", "大和物産へ切り替える", "q")]
    issues = [item("issue", "モイストセラムの価格改定のタイミング", "q")]
    assert verify_source.check_truth(decisions, todos, issues, TRUTH) == []


def test_音声に無いはずの語が音声だけの回に出たら見つける():
    got = verify_source.check_truth(
        [item("decision", "次回は10/7に開催する", "q"),
         item("decision", "初回発注は1800個", "q"),
         item("decision", "大和物産へ切り替える", "q")],
        [], [], TRUTH, chat_used=False,
    )
    assert any("10/7" in f.message for f in got)


def test_チャットを使った回なら出てよい():
    got = verify_source.check_truth(
        [item("decision", "次回は10/7に開催する", "q"),
         item("decision", "初回発注は1800個", "q"),
         item("decision", "大和物産へ切り替える", "q")],
        [], [], TRUTH, chat_used=True,
    )
    assert not any("10/7" in f.message for f in got)


# ------------------------------------------------------------------ まとめ

def test_件数を返す():
    """**N件中N件を出す。** 1件だけ合っていた絵は、何も言っていない（課題2の講評）。"""
    items = [
        item("decision", "大和物産へ切り替える", "大和物産に切り替えます"),
        item("issue", "値上げ", "値上げのタイミングは特許の前と後どちらがいいですか"),
    ]
    report = verify_source.verify(items[:1], [], items[1:], SCRIPT, TRUTH)
    assert report.checked == 2
    assert report.matched == 1
    assert report.findings


def test_所見が無ければ空():
    items = [item("decision", "初回発注は1800個", "では初回は千八百個に変更でお願いします"),
             item("decision", "大和物産へ切り替える", "大和物産に切り替えます")]
    issues = [item("issue", "モイストセラムの価格改定のタイミング", "これは持ち帰りにしましょう")]
    report = verify_source.verify(items, [], issues, SCRIPT, TRUTH)
    assert report.findings == []
    assert report.matched == report.checked == 3
