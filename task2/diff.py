"""履歴シートの行と今回の行を比べて、**言えることだけを言う**。

**情報源はシート1本。** 前回値も履歴から読む（DESIGN 2）。`state.json` を持つと
「シートには入ったが state は進んでいない」というズレが構造的に生まれる。

この層で守ること
------------------------------------------------------------------

**「変化なし」と「比較できない」を混ぜない。**
どちらも「値下がりの通知が飛ばない」という同じ見え方になるが、
前者は比べた結果で、後者は**比べていない**。混ぜると、
取得が壊れている期間が「価格が安定している期間」として履歴に残る。

`DESIGN.md` の 5-G 〜 5-J を引き受ける。**4つとも同じ形**——
「言えないことを、言えないと書く」。

============ ====================================================================
DESIGN       ここでの扱い
============ ====================================================================
5-G          前回として採るのは ``状態=取得`` の行だけ。無ければ理由を返す
             （**「初回」と「前回が失敗」を区別する**——前者は待てば埋まり、
             後者は取れていない）
5-H          商品名が変わったら注意を立てる。**判定は止めない**
5-I          オフセット付きの時刻だけ採り、読めなかった行数を返す
5-J          在庫の変化は価格とは**別の欄**で返す
============ ====================================================================

**閾値はここに持たない。** 「いくら動いたか」は常に記録し、
通知するかどうかだけ ``drops`` が決める。閾値未満を「変化なし」として
記録すると、後から見たときに「動かなかった日」に化ける。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

import transform

_AT = transform.COLUMNS.index("取得時刻")
_CODE = transform.COLUMNS.index("itemCode")
_NAME = transform.COLUMNS.index("商品名")
_ITEM_PRICE = transform.COLUMNS.index("本体価格")
_EFFECTIVE = transform.COLUMNS.index("実質価格")
_STOCK = transform.COLUMNS.index("在庫")
_STATUS = transform.COLUMNS.index("状態")

#: 比較できなかった理由。**空にしない**（DESIGN 4-⑥ と同じ方針）。
NOTE_FIRST = "初回"
NOTE_PREVIOUS_FAILED = "前回が失敗"
NOTE_CURRENT_FAILED = "今回が失敗"
NOTE_BAD_TIME = "取得時刻が読めない"
NOTE_NO_PREVIOUS_PRICE = "前回の価格が無い"
NOTE_NO_CURRENT_PRICE = "今回の価格が無い"


@dataclass(frozen=True)
class Comparison:
    """商品1件ぶんの比較。**比べられなくても1つ返る。**"""

    item_code: str
    #: 実質価格（ポイント込み）。比較の主役。
    previous_price: int | None
    current_price: int | None
    #: ``current - previous``。負なら値下がり。**比べていなければ None**。
    delta: int | None
    #: 比べられなかった理由。比べたときは空。
    note: str
    #: 生の本体価格。式を変えた日に、過去の判定を計算し直せるようにする。
    previous_item_price: int | None = None
    current_item_price: int | None = None
    #: 同じ ``itemCode`` のまま中身が入れ替わった疑い（DESIGN 5-H）。
    name_changed: bool = False
    #: 価格が動かなくても、買えなくなっていることがある（DESIGN 5-J）。
    stock_changed: bool = False
    #: 取得時刻が読めずに候補から外した履歴の行数（DESIGN 5-I）。
    skipped_rows: int = 0

    @property
    def comparable(self) -> bool:
        return self.delta is not None

    @property
    def dropped(self) -> bool:
        return self.delta is not None and self.delta < 0


def parse_time(value: Any) -> datetime | None:
    """**オフセット付きの ISO 8601 だけ**を受け取る。読めなければ ``None``。

    オフセットの無い時刻を受け入れると、書く側で1本にした物差しが
    読む側で2本に戻る（DESIGN 5-E / 5-I）。シートは人も編集できるので、
    「書いたのは自分だから大丈夫」は成り立たない。
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _cell(row: Sequence[Any], index: int) -> Any:
    """列が足りない行でも落ちない。

    **ここで例外を出すと、1行の崩れで全商品の判定が止まる。**
    シートは人が編集できる＝1列消えることは実際に起こる。
    """
    return row[index] if len(row) > index else ""


def _as_price(value: Any) -> int | None:
    """価格として使える値だけ通す。**空を 0 にしない。**

    0 円は「値下がりした」に見える。取れなかったことと、
    0 円だったことを同じ数字にしない。
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def previous_row(
    history: Sequence[Sequence[Any]], item_code: str, *, before: datetime | None
) -> tuple[Sequence[Any] | None, int]:
    """前回の「取得できた行」と、時刻が読めずに外した行数を返す。

    **末尾に依存しない。** ``append`` は末尾に足すが、シートは人が並べ替えられる。
    「いちばん下が最新」を前提にすると、並べ替えた日から静かに間違える。

    ``before`` より**厳密に前**の行だけを見る。シートへ追記してから読み直すと
    **今回の行が履歴に入っている**ので、そのままだと自分自身と比べて
    何が起きても「変化なし」になる。
    """
    skipped = 0
    best: Sequence[Any] | None = None
    best_at: datetime | None = None

    for row in history:
        if not isinstance(row, Sequence) or isinstance(row, str):
            continue
        if _cell(row, _CODE) != item_code:
            continue

        at = parse_time(_cell(row, _AT))
        if at is None:
            # **黙って飛ばさない。** 0件になった理由が言えなくなる。
            skipped += 1
            continue
        if before is not None and at >= before:
            continue
        if _cell(row, _STATUS) != transform.STATUS_OK:
            # 失敗行を前回にすると、空の価格を数値に落とす経路ができる（5-G）。
            continue
        if best_at is None or at > best_at:
            best, best_at = row, at

    return best, skipped


def written_on(history: Sequence[Sequence[Any]], moment: str) -> bool:
    """``moment`` と同じ日に、**取得できた行**がすでにあるか（DESIGN 5-X）。

    **失敗行しか無い日は「まだ」と答える。** ネットワークが上がる前に走った回
    （5-AB）を、次の起動で拾い直せるようにするため。

    日は **``moment`` のオフセットに揃えて**見る。実行環境のタイムゾーンで
    答えが変わると、書く側で1本にした物差しが読む側で2本に戻る（5-E）。
    """
    now = parse_time(moment)
    if now is None:
        # **読めない時刻を「今日ではない」と決めない**のではなく、
        # ここでは「まだ書いていない」に倒す。止めるより走らせるほうが、
        # 欠測（行がただ無い日）を作らない。
        return False

    for row in history:
        if not isinstance(row, Sequence) or isinstance(row, str):
            continue
        if _cell(row, _STATUS) != transform.STATUS_OK:
            continue
        at = parse_time(_cell(row, _AT))
        if at is None:
            continue
        if at.astimezone(now.tzinfo).date() == now.date():
            return True
    return False


def _has_earlier_row(
    history: Sequence[Sequence[Any]], item_code: str, before: datetime | None
) -> bool:
    """その商品の行が**そもそも履歴にあるか**（状態は問わない）。

    「初回」と「前回が失敗」を分けるために要る。同じ「比較できない」でも、
    前者は明日には埋まり、後者は**取れていない**。
    """
    for row in history:
        if not isinstance(row, Sequence) or isinstance(row, str):
            continue
        if _cell(row, _CODE) != item_code:
            continue
        at = parse_time(_cell(row, _AT))
        if at is None:
            continue
        if before is None or at < before:
            return True
    return False


def compare_row(
    current: Sequence[Any], history: Sequence[Sequence[Any]]
) -> Comparison:
    """今回の1行を履歴と比べる。**比べられなくても必ず1つ返す。**"""
    item_code = _cell(current, _CODE)
    current_at = parse_time(_cell(current, _AT))

    def incomparable(note: str, *, skipped: int = 0) -> Comparison:
        return Comparison(
            item_code=item_code,
            previous_price=None,
            current_price=_as_price(_cell(current, _EFFECTIVE)),
            delta=None,
            note=note,
            current_item_price=_as_price(_cell(current, _ITEM_PRICE)),
            skipped_rows=skipped,
        )

    if _cell(current, _STATUS) != transform.STATUS_OK:
        return incomparable(NOTE_CURRENT_FAILED)
    if current_at is None:
        return incomparable(NOTE_BAD_TIME)

    previous, skipped = previous_row(history, item_code, before=current_at)
    if previous is None:
        note = (
            NOTE_PREVIOUS_FAILED
            if _has_earlier_row(history, item_code, current_at)
            else NOTE_FIRST
        )
        return incomparable(note, skipped=skipped)

    previous_price = _as_price(_cell(previous, _EFFECTIVE))
    current_price = _as_price(_cell(current, _EFFECTIVE))
    if previous_price is None:
        return incomparable(NOTE_NO_PREVIOUS_PRICE, skipped=skipped)
    if current_price is None:
        return incomparable(NOTE_NO_CURRENT_PRICE, skipped=skipped)

    previous_name = str(_cell(previous, _NAME))
    current_name = str(_cell(current, _NAME))
    previous_stock = _cell(previous, _STOCK)
    current_stock = _cell(current, _STOCK)

    return Comparison(
        item_code=item_code,
        previous_price=previous_price,
        current_price=current_price,
        delta=current_price - previous_price,
        note="",
        previous_item_price=_as_price(_cell(previous, _ITEM_PRICE)),
        current_item_price=_as_price(_cell(current, _ITEM_PRICE)),
        # 片方が空のときに「変わった」と数えると、古い行のせいで毎回鳴る。
        name_changed=bool(previous_name and current_name and previous_name != current_name),
        stock_changed=(
            previous_stock != "" and current_stock != "" and previous_stock != current_stock
        ),
        skipped_rows=skipped,
    )


def compare_all(
    current_rows: Sequence[Sequence[Any]], history: Sequence[Sequence[Any]]
) -> list[Comparison]:
    """今回の全行を比べる。**入れた数だけ返る**（DESIGN 5-C と同じ約束）。"""
    return [compare_row(row, history) for row in current_rows]


def drops(comparisons: Sequence[Comparison], *, threshold: int = 0) -> list[Comparison]:
    """通知したい値下がりだけを選ぶ。**判定はここでやり直さない。**

    ``threshold`` は「いくら下がったら知らせるか」。既定は 0 ＝ 1円でも知らせる。
    ここで落とすのは**通知だけ**で、``delta`` は全件に残っている。
    """
    return [c for c in comparisons if c.dropped and -(c.delta or 0) >= threshold]
