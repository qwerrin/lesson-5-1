"""task2/transform のテスト。**実装より先に書いた。**

守らせる対象は `task2/DESIGN.md` の穴分析から引いている。
「動くこと」ではなく「**欠落しないこと**」を確かめる。

============ ====================================================================
DESIGN の項目 ここで守ること
============ ====================================================================
5-F          商品名の改行・タブでセルが壊れない
4-③          ポイント倍率から実質価格が出る／**生の値も必ず残る**
5-B          取得できなかったものも1行として残る（価格空欄＋理由）
5-C          失敗行も列数が同じ。ズレると追記でシートの列が崩れる
5-E          取得時刻の物差しが1本（オフセット付き ISO 8601）
4-④/4-⑥     availability・postageFlag・taxFlag は**解釈せず生値のまま**
============ ====================================================================
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import transform  # noqa: E402


AT = "2026-09-06T21:30:00+09:00"


def item(**overrides):
    """楽天 API が実際に返す形（2026-09-06 の疎通確認で確認した41フィールドの一部）。"""
    base = {
        "itemCode": "shop:10001542",
        "itemName": "ドリップバッグコーヒー 6種 120袋",
        "itemPrice": 12000,
        "pointRate": 1,
        "postageFlag": 0,
        "taxFlag": 0,
        "availability": 1,
        "shopName": "テスト商店",
        "reviewAverage": 4.68,
        "reviewCount": 532,
        "itemUrl": "https://item.rakuten.co.jp/shop/10001542/",
    }
    base.update(overrides)
    return base


# ============================================================ 列の形


class Test列:
    def test_列名が15本ある(self):
        # シートの見出し行と行の長さは必ず一致する。
        assert len(transform.COLUMNS) == 15

    def test_取得行の長さが列名と一致する(self):
        row = transform.to_row(item(), AT)
        assert len(row) == len(transform.COLUMNS)

    def test_失敗行の長さも列名と一致する(self):
        # **ここがズレると追記でシートの列が崩れる**（DESIGN 5-C）。
        # 成功行だけ長さを合わせても、落ちた日に崩れる。
        row = transform.failure_row("shop:10001542", AT, "IP拒否")
        assert len(row) == len(transform.COLUMNS)

    def test_取得行と失敗行の長さが等しい(self):
        assert len(transform.to_row(item(), AT)) == len(
            transform.failure_row("x:1", AT, "不明")
        )


# ============================================================ 5-F 壊れる文字


class Test商品名の正規化:
    @pytest.mark.parametrize("raw", ["改行\nあり", "改行\r\nあり", "タブ\tあり"])
    def test_改行とタブは空白になる(self, raw):
        row = transform.to_row(item(itemName=raw), AT)
        name = row[transform.COLUMNS.index("商品名")]
        assert "\n" not in name and "\r" not in name and "\t" not in name

    def test_前後の空白は落ちる(self):
        row = transform.to_row(item(itemName="  コーヒー  "), AT)
        assert row[transform.COLUMNS.index("商品名")] == "コーヒー"

    def test_改行は空白になり語は繋がらない(self):
        """正規化は壊れる文字を**空白に置き換える**。消してはいけない。

        **最初は「両方の語が残っている」で書いていて、それでは弱かった。**
        改行を空文字で消す実装でも ``コーヒー120袋`` になり、
        ``"コーヒー" in name and "120袋" in name`` は**両方とも通る**。
        2026-09-06 のミューテーションで実際に素通りした（14件中これ1件だけ）。

        部分一致は「消えていないこと」を確かめられない。期待値を丸ごと固定する。
        """
        row = transform.to_row(item(itemName="コーヒー\n120袋"), AT)
        assert row[transform.COLUMNS.index("商品名")] == "コーヒー 120袋"


# ============================================================ 4-③ ポイントと実質価格


class Test実質価格:
    def test_生の本体価格が残る(self):
        # **計算結果しか残さないと、式を変えた日に過去の行と比較できなくなる。**
        row = transform.to_row(item(itemPrice=12000), AT)
        assert row[transform.COLUMNS.index("本体価格")] == 12000

    def test_生のポイント倍率が残る(self):
        row = transform.to_row(item(pointRate=5), AT)
        assert row[transform.COLUMNS.index("ポイント倍率")] == 5

    def test_倍率1なら実質は1パーセント引き(self):
        row = transform.to_row(item(itemPrice=12000, pointRate=1), AT)
        assert row[transform.COLUMNS.index("実質価格")] == 11880

    def test_倍率5なら実質は5パーセント引き(self):
        row = transform.to_row(item(itemPrice=12000, pointRate=5), AT)
        assert row[transform.COLUMNS.index("実質価格")] == 11400

    def test_倍率が無いときは本体価格と同じ(self):
        # **0 とみなす。** 「不明だから空」にすると実質価格の列に穴が空き、
        # 値下がり判定が「比較できない行」を挟んで途切れる。
        no_rate = item()
        del no_rate["pointRate"]
        row = transform.to_row(no_rate, AT)
        assert row[transform.COLUMNS.index("実質価格")] == no_rate["itemPrice"]

    def test_端数は切り捨てて整数になる(self):
        # 価格は整数で持つ。小数が混ざるとシート上で桁が揃わず、比較もぶれる。
        row = transform.to_row(item(itemPrice=1055, pointRate=1), AT)
        value = row[transform.COLUMNS.index("実質価格")]
        assert isinstance(value, int)
        assert value == 1045  # 1055 - (1055//100)*1

    def test_100円未満は切り捨ててから倍率をかける(self):
        """**式を1つに固定する。** 候補が2つあり、上の例では区別が付かなかった。

        - ``floor(価格 × 倍率 ÷ 100)`` … 価格の N%
        - ``floor(価格 ÷ 100) × 倍率`` … **100円ごとに N ポイント**（採用）

        1055円・倍率3 で初めて割れる: 前者は 31、後者は 30。
        楽天は「100円ごとに1ポイント」を基本にしているので後者を採る。
        **ただし公式で確認していない**（DESIGN 3.2 の未確認に入れてある）。
        確認して違っていたらこのテストごと直す——**式を変えたことが1件の失敗で分かる**
        状態にしておくのが目的で、それが無いと静かに全行の意味が変わる。
        """
        row = transform.to_row(item(itemPrice=1055, pointRate=3), AT)
        assert row[transform.COLUMNS.index("実質価格")] == 1025


# ============================================================ 4-④/4-⑥ 解釈しない


class Test生値のまま入れる:
    @pytest.mark.parametrize("field,column", [
        ("availability", "在庫"),
        ("postageFlag", "送料フラグ"),
        ("taxFlag", "税フラグ"),
    ])
    @pytest.mark.parametrize("value", [0, 1])
    def test_フラグは解釈せずそのまま(self, field, column, value):
        # **0 が「送料込み」なのか「別」なのかを確認していない**（DESIGN 3.5）。
        # 確かめる前に解釈して入れると、逆だったとき履歴が全部嘘になる。
        row = transform.to_row(item(**{field: value}), AT)
        assert row[transform.COLUMNS.index(column)] == value


# ============================================================ 5-B 失敗も1行


class Test失敗行:
    def test_状態が失敗になる(self):
        row = transform.failure_row("shop:1", AT, "429")
        assert row[transform.COLUMNS.index("状態")] == "失敗"

    def test_理由が入る(self):
        row = transform.failure_row("shop:1", AT, "IP拒否")
        assert row[transform.COLUMNS.index("理由")] == "IP拒否"

    def test_価格の欄は空になる(self):
        # **0 を入れない。** 0 円は「値下がりした」に見えてしまう。
        row = transform.failure_row("shop:1", AT, "見つからない")
        for column in ("本体価格", "ポイント倍率", "実質価格"):
            assert row[transform.COLUMNS.index(column)] == ""

    def test_商品コードは残る(self):
        # どの商品が取れなかったのかが分からないと、C の突き合わせができない。
        row = transform.failure_row("shop:10001542", AT, "不明")
        assert row[transform.COLUMNS.index("itemCode")] == "shop:10001542"

    def test_取得時刻は入る(self):
        row = transform.failure_row("shop:1", AT, "不明")
        assert row[transform.COLUMNS.index("取得時刻")] == AT

    def test_理由が空なら不明になる(self):
        # 区別できないことを、区別できないと書く（DESIGN 4-⑥）。
        row = transform.failure_row("shop:1", AT, "")
        assert row[transform.COLUMNS.index("理由")] == "不明"


class Test取得行の状態:
    def test_状態は取得(self):
        row = transform.to_row(item(), AT)
        assert row[transform.COLUMNS.index("状態")] == "取得"

    def test_理由は空(self):
        row = transform.to_row(item(), AT)
        assert row[transform.COLUMNS.index("理由")] == ""


# ============================================================ 5-E 時刻の物差し


class Test取得時刻:
    def test_渡した値がそのまま入る(self):
        row = transform.to_row(item(), AT)
        assert row[transform.COLUMNS.index("取得時刻")] == AT

    def test_現在時刻はオフセット付きで返る(self):
        # **物差しを2本置かない。** 値そのものにオフセットを持たせれば、
        # 読む側の解釈に依存しない。
        now = transform.now_iso()
        assert now.endswith(("+09:00", "-09:00")) or "+" in now[10:] or "-" in now[10:]

    def test_現在時刻は秒まで(self):
        assert len(transform.now_iso()) >= len("2026-09-06T21:30:00+09:00")


# ============================================================ 欠損に強い


class Test欠損:
    def test_フィールドが無くても落ちない(self):
        row = transform.to_row({"itemCode": "shop:1"}, AT)
        assert len(row) == len(transform.COLUMNS)

    def test_無い文字列の欄は空文字になる(self):
        row = transform.to_row({"itemCode": "shop:1"}, AT)
        assert row[transform.COLUMNS.index("商品名")] == ""

    def test_価格が無ければ空になる(self):
        # **0 にしない**（失敗行と同じ理由）。
        row = transform.to_row({"itemCode": "shop:1"}, AT)
        assert row[transform.COLUMNS.index("本体価格")] == ""
        assert row[transform.COLUMNS.index("実質価格")] == ""
