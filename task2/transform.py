"""楽天 API のレスポンス1件を、スプレッドシートの1行に変換する。

**外部に繋がない。** ここは「取れたデータをどう残すか」だけを引き受ける。
取りに行く責任（`fetch_items.py`）と書き込む責任（`to_sheet.py`）から切ってあるのは、
**壊れる入力を作りやすくするため**である。改行入りの商品名も、価格の無い商品も、
ここでは辞書を1つ組むだけで再現できる。

設計は `DESIGN.md`。この実装が引き受けている穴は5つ:

============ ====================================================================
DESIGN       ここでの扱い
============ ====================================================================
4-③          ポイント倍率から実質価格を出す。**生の値も必ず併記する**
4-④/4-⑥     在庫・送料・税のフラグは**解釈せず生値のまま**入れる
5-B          取れなかったものも1行にする（`failure_row`）
5-C          失敗行も**列数を揃える**。ズレると追記でシートが崩れる
5-E          時刻は**オフセット付き ISO 8601**。物差しを2本置かない
5-F          商品名の改行・タブを空白にする
============ ====================================================================
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

#: シートの見出し行。**行の長さはこれと必ず一致させる。**
#:
#: 生の値（本体価格・ポイント倍率）と計算列（実質価格）を**両方**持つ。
#: 計算結果しか残さないと、式を変えた日に過去の行と比較できなくなる
#: ——履歴の意味が遡って変わってしまう。
#:
#: 送料・税・在庫は **``postageFlag`` などの生値のまま**入れる。
#: 0 が「送料込み」なのか「別」なのかを公式で確認していないので、
#: 解釈した名前（「送料込み」など）を列に付けない。逆だったとき履歴が全部嘘になる。
COLUMNS: tuple[str, ...] = (
    "取得時刻",
    "itemCode",
    "商品名",
    "本体価格",
    "ポイント倍率",
    "実質価格",
    "送料フラグ",
    "税フラグ",
    "在庫",
    "ショップ",
    "レビュー平均",
    "レビュー数",
    "URL",
    "状態",
    "理由",
)

#: 取得できた行に入れる状態。
STATUS_OK = "取得"
#: 取れなかった行に入れる状態。**行そのものは残す**（DESIGN 5-B）。
STATUS_FAILED = "失敗"
#: 理由が言えないときに入れる語。**空欄にしない**（DESIGN 4-⑥）。
#: 「区別できない」ことを、区別できないと書く。
REASON_UNKNOWN = "不明"

#: セルを壊す文字。**消さずに空白へ置き換える。** 消すと語が繋がって別語になる。
#: ``\r\n`` を先に見るのは、後回しにすると空白が2つ入るため。
_BREAKING = ("\r\n", "\r", "\n", "\t")


def now_iso() -> str:
    """いまの時刻を**オフセット付き** ISO 8601 で返す。

    **値そのものにタイムゾーンを持たせる。** 列名で「JST」と名乗る方式にすると、
    読む側の解釈に依存し、書く側と読む側で物差しが2本になる（DESIGN 5-E）。
    オフセットが値に入っていれば、後から誰が読んでも1本に定まる。
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_text(value: Any) -> str:
    """改行・タブを空白に置き換え、前後を削る。

    商品名には改行が入る。**API は正常終了する**ので、
    そのまま書くとセルが壊れたことに誰も気づかない（DESIGN 5-F）。
    """
    if value is None:
        return ""
    text = str(value)
    for char in _BREAKING:
        text = text.replace(char, " ")
    return text.strip()


def effective_price(item_price: int, point_rate: int) -> int:
    """ポイントを引いた実質価格。

    **式は「100円ごとに ``point_rate`` ポイント」を採る**——
    ``(item_price // 100) * point_rate`` を引く。

    もう1つの候補（``item_price * point_rate // 100``＝価格の N%）とは、
    100円未満の端数があるときだけ答えが割れる。
    例: 1055円・倍率3 で 30 と 31。楽天は「100円ごとに1ポイント」を基本に
    しているのでこちらを採るが、**公式で確認していない**（DESIGN 3.2）。

    式を固定してテストで留めてあるので、**変えれば1件の失敗として現れる**。
    留めていないと、式を変えた瞬間に過去の行の意味まで静かに変わる。
    """
    return item_price - (item_price // 100) * point_rate


def to_row(item: dict[str, Any], fetched_at: str) -> list[Any]:
    """取得できた商品1件を行にする。

    **欠けているフィールドで落ちない。** 実物は41フィールド返すが、
    それが常に全部揃う保証は無い。落ちると**その回の全商品**が書けなくなり、
    1件の欠損が全件の欠落になる。

    価格が無いときは **0 ではなく空**にする。0 円は「値下がりした」に見え、
    値下がり判定を誤らせる。
    """
    price = item.get("itemPrice")
    rate = item.get("pointRate", 0)

    if isinstance(price, int):
        rate_value: Any = rate if isinstance(rate, int) else 0
        effective: Any = effective_price(price, rate_value)
    else:
        # 価格が読めなければ、倍率も実質価格も言えない。**推測で埋めない。**
        price = ""
        rate_value = ""
        effective = ""

    return [
        fetched_at,
        normalize_text(item.get("itemCode")),
        normalize_text(item.get("itemName")),
        price,
        rate_value,
        effective,
        item.get("postageFlag", ""),
        item.get("taxFlag", ""),
        item.get("availability", ""),
        normalize_text(item.get("shopName")),
        item.get("reviewAverage", ""),
        item.get("reviewCount", ""),
        normalize_text(item.get("itemUrl")),
        STATUS_OK,
        "",
    ]


def failure_row(item_code: str, fetched_at: str, reason: str) -> list[Any]:
    """取れなかった商品も1行にする（DESIGN 5-B）。

    **行を書かないと「0件」と「取れなかった」が同じ無行になる。**
    課題1で「投稿が0件でも通知する」が評価されたのと同じ形で、
    ここでは「取れなかったことを記録する」に置き換えている。

    ``item_code`` を残すのは、**どれが落ちたかが分からないと
    watchlist との突き合わせ（DESIGN 5-C）ができない**ため。
    """
    row: list[Any] = [""] * len(COLUMNS)
    row[COLUMNS.index("取得時刻")] = fetched_at
    row[COLUMNS.index("itemCode")] = normalize_text(item_code)
    row[COLUMNS.index("状態")] = STATUS_FAILED
    row[COLUMNS.index("理由")] = normalize_text(reason) or REASON_UNKNOWN
    return row
