"""task2/diff のテスト。**実装より先に書いた。**

守らせる対象は `task2/DESIGN.md` の **5-G / 5-H / 5-I / 5-J**——
`diff.py` を書く前に出した「比較する層の穴」である。

============ ====================================================================
DESIGN の項目 ここで守ること
============ ====================================================================
5-G          前回が失敗行なら「比較できない」と言う。**「変化なし」にしない**
5-H          商品名が変わったら注意を出す（同じ itemCode で中身が入れ替わる）
5-I          オフセット付きの時刻だけを採る。読めなかった行は**数えて返す**
5-J          価格が同じでも在庫が変わったことが分かる
============ ====================================================================

**行は `transform` で作る。** 手で組むと列の順序を写し間違え、
実装と同じ誤りをテストにも埋め込む（課題10で実際に踏んだ形）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import diff  # noqa: E402
import transform  # noqa: E402


CODE = "f282260-awaji:10001542"
T1 = "2026-09-06T21:00:00+09:00"
T2 = "2026-09-07T21:00:00+09:00"
T3 = "2026-09-08T21:00:00+09:00"


def row(at, *, code=CODE, price=12000, rate=1, name="テスト商品", stock=1):
    """取得できた行。**`transform` に作らせる**ので列の順序が1本になる。"""
    return transform.to_row(
        {
            "itemCode": code,
            "itemName": name,
            "itemPrice": price,
            "pointRate": rate,
            "availability": stock,
        },
        at,
    )


def failed(at, *, code=CODE, reason="見つからない"):
    return transform.failure_row(code, at, reason)


def no_price(at, *, code=CODE):
    """**`状態=取得` なのに価格が空**の行。

    `transform.to_row` は `itemPrice` が読めないとき価格欄を空にする——
    0 を入れると「値下がりした」に見えるため。**取得できた行なのに比べられない**
    という組み合わせが実際に作れる。
    """
    return transform.to_row({"itemCode": code, "itemName": "テスト商品", "availability": 1}, at)


def price_of(r):
    return r[transform.COLUMNS.index("実質価格")]


# ============================================================ 5-I 時刻


class Test時刻の読み取り:
    def test_オフセット付きは読める(self):
        assert diff.parse_time(T1) is not None

    def test_オフセット無しは読めない(self):
        """**物差しを2本にしない。**

        `transform.now_iso` は必ずオフセットを付けるが、
        シートは人も編集できる。読む側が受け入れた瞬間に 5-E が崩れる。
        """
        assert diff.parse_time("2026-09-06T21:00:00") is None

    @pytest.mark.parametrize("bad", ["", None, "きのう", "2026/09/06 21:00", 12345])
    def test_読めない値はNoneになる(self, bad):
        assert diff.parse_time(bad) is None


# ============================================================ 前回の選び方


class Test前回の行:
    def test_同じ商品の直前の行を選ぶ(self):
        history = [row(T1, price=13000), row(T2, price=12500)]
        found, _ = diff.previous_row(history, CODE, before=diff.parse_time(T3))
        assert price_of(found) == price_of(row(T2, price=12500))

    def test_別の商品は選ばない(self):
        history = [row(T2, code="other:1", price=100)]
        found, _ = diff.previous_row(history, CODE, before=diff.parse_time(T3))
        assert found is None

    def test_失敗行は前回に選ばない(self):
        """**5-G の芯。** 価格が空の行を前回にすると、空を数値に落とす経路ができる。"""
        history = [row(T1, price=13000), failed(T2)]
        found, _ = diff.previous_row(history, CODE, before=diff.parse_time(T3))
        assert price_of(found) == price_of(row(T1, price=13000))

    def test_今回と同時刻の行は選ばない(self):
        # シートを追記してから読み直すと、**今回の行が履歴に入っている**。
        # 自分自身と比べると、何が起きても「変化なし」になる。
        history = [row(T3, price=12000)]
        found, _ = diff.previous_row(history, CODE, before=diff.parse_time(T3))
        assert found is None

    def test_今回より後の行は選ばない(self):
        history = [row(T3, price=1)]
        found, _ = diff.previous_row(history, CODE, before=diff.parse_time(T2))
        assert found is None

    def test_並びが時刻順でなくても最新を選ぶ(self):
        """**末尾に依存しない。**

        `append` は末尾に足すが、シートは人が並べ替えられる。
        「いちばん下が最新」を前提にすると、並べ替えた日から静かに間違える。
        """
        history = [row(T2, price=12500), row(T1, price=13000)]
        found, _ = diff.previous_row(history, CODE, before=diff.parse_time(T3))
        assert price_of(found) == price_of(row(T2, price=12500))

    def test_時刻が読めない行は数えて飛ばす(self):
        # **黙って飛ばさない**（5-I）。0件になった理由が言えなくなる。
        history = [row("2026-09-07 21:00", price=1), row(T1, price=13000)]
        found, skipped = diff.previous_row(history, CODE, before=diff.parse_time(T3))
        assert skipped == 1
        assert price_of(found) == price_of(row(T1, price=13000))

    def test_見出し行を前回にしない(self):
        history = [list(transform.COLUMNS), row(T1, price=13000)]
        found, _ = diff.previous_row(history, CODE, before=diff.parse_time(T3))
        assert price_of(found) == price_of(row(T1, price=13000))

    def test_列が足りない行で落ちない(self):
        # 人が1列消すことがある。**ここで例外を出すと全商品の判定が止まる。**
        history = [["壊れた行"], row(T1, price=13000)]
        found, _ = diff.previous_row(history, CODE, before=diff.parse_time(T3))
        assert found is not None


# ============================================================ 比較


class Test比較:
    def test_値下がりはdeltaが負(self):
        comparison = diff.compare_row(row(T2, price=11000), [row(T1, price=12000)])
        assert comparison.delta < 0
        assert comparison.dropped is True

    def test_値上がりはdeltaが正(self):
        comparison = diff.compare_row(row(T2, price=13000), [row(T1, price=12000)])
        assert comparison.delta > 0
        assert comparison.dropped is False

    def test_同額はdeltaがゼロ(self):
        # **0 と「比較できない」を区別する。** 0 は「比べて、動かなかった」。
        comparison = diff.compare_row(row(T2), [row(T1)])
        assert comparison.delta == 0
        assert comparison.comparable is True

    def test_実質価格で比べる(self):
        """本体価格が同じでも、**ポイント倍率が下がれば実質は上がる**（DESIGN 4-③）。

        本体価格だけを見ていると「変化なし」になる。
        """
        comparison = diff.compare_row(
            row(T2, price=12000, rate=1), [row(T1, price=12000, rate=5)]
        )
        assert comparison.delta > 0

    def test_生の本体価格も残る(self):
        # 式を変えた日に、過去の判定を計算し直せるようにする。
        comparison = diff.compare_row(row(T2, price=11000), [row(T1, price=12000)])
        assert comparison.previous_item_price == 12000
        assert comparison.current_item_price == 11000


class Test比較できないとき:
    def test_初回は比較できない(self):
        comparison = diff.compare_row(row(T1), [])
        assert comparison.comparable is False
        assert comparison.note == "初回"
        assert comparison.delta is None

    def test_初回を値下がりにしない(self):
        # ここを 0 と比べると、**追加した日に必ず「値下がり」が飛ぶ**。
        assert diff.compare_row(row(T1), []).dropped is False

    def test_前回が失敗だけなら理由が変わる(self):
        """**5-G。** 「初回」と「前回が失敗」は別のこと。

        同じ「比較できない」でも、前者は待てば直り、後者は**取れていない**。
        """
        comparison = diff.compare_row(row(T2), [failed(T1)])
        assert comparison.comparable is False
        assert comparison.note == "前回が失敗"

    def test_今回が失敗なら比較しない(self):
        comparison = diff.compare_row(failed(T2), [row(T1, price=12000)])
        assert comparison.comparable is False
        assert comparison.note == "今回が失敗"

    def test_今回が失敗でも商品コードは残る(self):
        # どれが比べられなかったのかが分からないと、突き合わせができない。
        assert diff.compare_row(failed(T2), []).item_code == CODE

    def test_時刻が読めない行があれば数が残る(self):
        comparison = diff.compare_row(row(T2), [row("きのう", price=1), row(T1)])
        assert comparison.skipped_rows == 1

    def test_前回が取得でも価格が空なら比較しない(self):
        # **状態だけ見て価格を見ないと、空を数値に落とす経路が残る。**
        comparison = diff.compare_row(row(T2), [no_price(T1)])
        assert comparison.comparable is False
        assert comparison.note == "前回の価格が無い"

    def test_今回の価格が空なら比較しない(self):
        comparison = diff.compare_row(no_price(T2), [row(T1)])
        assert comparison.comparable is False
        assert comparison.note == "今回の価格が無い"

    def test_価格が空でも値下がりにしない(self):
        assert diff.compare_row(no_price(T2), [row(T1)]).dropped is False

    def test_今回の時刻が読めなければ比較しない(self):
        comparison = diff.compare_row(row("きのう"), [row(T1)])
        assert comparison.comparable is False
        assert comparison.note == "取得時刻が読めない"


# ============================================================ 5-H 中身の差し替え


class Test商品名の変化:
    def test_名前が変わったら注意が立つ(self):
        """同じ `itemCode` のまま中身が入れ替わることがある。**API は正常終了する。**"""
        comparison = diff.compare_row(
            row(T2, name="別の商品 500g"), [row(T1, name="テスト商品")]
        )
        assert comparison.name_changed is True

    def test_名前が変わっても判定は続く(self):
        # 止めると、表記ゆれ1つで価格の追跡が切れる。**注意は出すが、比較はする。**
        comparison = diff.compare_row(
            row(T2, price=11000, name="別の商品"), [row(T1, price=12000)]
        )
        # **比べるのは実質価格**（本体価格の差 -1000 ではない）。
        # 12000 - 120 = 11880 ／ 11000 - 110 = 10890 ／ 差は -990。
        assert comparison.delta == (11000 - 110) - (12000 - 120)

    def test_同じ名前なら立たない(self):
        assert diff.compare_row(row(T2), [row(T1)]).name_changed is False

    def test_前回の名前が空なら立たない(self):
        # 空を「変わった」と数えると、古い行のせいで毎回鳴る。
        old = row(T1)
        old[transform.COLUMNS.index("商品名")] = ""
        assert diff.compare_row(row(T2), [old]).name_changed is False


# ============================================================ 5-J 在庫


class Test在庫の変化:
    def test_在庫が変わったら分かる(self):
        comparison = diff.compare_row(row(T2, stock=0), [row(T1, stock=1)])
        assert comparison.stock_changed is True

    def test_価格が同じでも在庫の変化は出る(self):
        """**「値段が動いていない」は正しいが、買えない。**

        価格だけ見ていると「変化なし」として記録される。
        """
        comparison = diff.compare_row(row(T2, stock=0), [row(T1, stock=1)])
        assert comparison.delta == 0
        assert comparison.stock_changed is True

    def test_変わらなければ立たない(self):
        assert diff.compare_row(row(T2), [row(T1)]).stock_changed is False


# ============================================================ 判定と通知を分ける


class Test値下がりの抽出:
    def test_値下がりだけ返る(self):
        comparisons = [
            diff.compare_row(row(T2, price=11000), [row(T1, price=12000)]),
            diff.compare_row(row(T2, price=13000), [row(T1, price=12000)]),
        ]
        assert len(diff.drops(comparisons)) == 1

    def test_閾値未満は通知しないがdeltaは残る(self):
        """**判定と通知を分ける。**

        閾値未満を「変化なし」として記録すると、後から
        「動かなかった日」に化ける。落とすのは通知だけ。
        """
        comparison = diff.compare_row(row(T2, price=11700), [row(T1, price=12000)])
        assert diff.drops([comparison], threshold=500) == []
        assert comparison.delta is not None and comparison.delta < 0

    def test_閾値ちょうどは通知する(self):
        comparison = diff.compare_row(row(T2, price=11500), [row(T1, price=12000)])
        assert len(diff.drops([comparison], threshold=495)) == 1

    def test_比較できない行は通知に混ざらない(self):
        assert diff.drops([diff.compare_row(row(T1), [])]) == []

    def test_値上がりは通知しない(self):
        comparison = diff.compare_row(row(T2, price=13000), [row(T1, price=12000)])
        assert diff.drops([comparison]) == []


# ============================================================ 件数


class Test全件:
    def test_今回の行数と結果の数が一致する(self):
        current = [row(T2, code="a:1"), row(T2, code="b:2"), failed(T2, code="c:3")]
        assert len(diff.compare_all(current, [])) == 3

    def test_順序が変わらない(self):
        current = [row(T2, code="a:1"), row(T2, code="b:2")]
        assert [c.item_code for c in diff.compare_all(current, [])] == ["a:1", "b:2"]

    def test_商品ごとに履歴を引く(self):
        history = [row(T1, code="a:1", price=12000), row(T1, code="b:2", price=500)]
        current = [row(T2, code="a:1", price=11000), row(T2, code="b:2", price=500)]
        deltas = [c.delta for c in diff.compare_all(current, history)]
        assert deltas == [(11000 - 110) - (12000 - 120), 0]

    def test_履歴が空でも落ちない(self):
        assert diff.compare_all([row(T1)], []) != []
