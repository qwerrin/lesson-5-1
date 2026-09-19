"""figset/collect のテスト。**実装より先に書いた。**

`collect` は ShareX が吐いた原本を集めて台帳にする段。**ここで守るのは「数え方」**で、
中身の判定（`classify`）にも、番号の割付（`layout`）にも踏み込まない。

============ ====================================================================
根拠         ここで守ること
============ ====================================================================
H1           ファイル名はタイムスタンプだけ。**名前を索引として信用しない**
H2           撮影順を**明示的に持つ**。並び順を後段で推測させない
H4           撮り直したほぼ同じ絵を**ハッシュで**区別する
H6           ShareX は月フォルダで分断する。**またいで集める**
H7           保存先は**引数で受け取る**。環境変数やユーザー名から組み立てない
M6           同名で上書きされたものを見分けられるよう、中身のハッシュを持つ
M7           原本が無いとき「0件」と言わない。**確認不能**に倒す
M9           対象0件を**成功にしない**
============ ====================================================================

**日時の物差しは1本。** `collect` が使うのはファイルシステムの mtime だけで、
すべてローカルの naive な `datetime` として扱う（教訓 `one-date-basis-per-output`）。

ファイル名にも日時が入っているが、**それは物差しではなく second opinion** である。
mtime と食い違ったら記録するだけで、どちらが正しいかは `collect` は決めない
——*copy したら mtime は変わりうるし、リネームしたら名前は変わりうる*。
**両方壊れる可能性があるものは、突き合わせて食い違いを出すのが精一杯。**

そして **「名前から日時が読めなかった」と「読めたが食い違った」は別の状態**にする。
読めなかったものを食い違い扱いにすると、*読めなかったことが異常として鳴り続ける*。
これは `verify_doc.py` の「値が返らなかった項目を OK にしない」の裏面で、
**判定できなかったことを、判定の結果に混ぜない**という同じ規律である。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "figset"))

import collect  # noqa: E402

# ShareX の既定の名前は YYYY-MM-DD-HH-mm-ss-fff
SHAREX = "%Y-%m-%d-%H-%M-%S-000"

PNG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 64
PNG_B = b"\x89PNG\r\n\x1a\n" + b"B" * 64


def _put(root: Path, name: str, body: bytes, when: datetime) -> Path:
    """`root` に1枚置いて mtime を `when` に合わせる。"""
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.write_bytes(body)
    ts = when.timestamp()
    os.utime(p, (ts, ts))
    return p


def _shot(when: datetime, body: bytes = PNG_A) -> tuple[str, bytes, datetime]:
    """ShareX 流の名前で1枚ぶんの材料を作る（名前と mtime が揃った状態）。"""
    return (when.strftime(SHAREX) + ".png", body, when)


# --------------------------------------------------------------------------
# 集める範囲（H6・H7）
# --------------------------------------------------------------------------


def test_月フォルダをまたいで集める(tmp_path: Path) -> None:
    """ShareX は月ごとにフォルダを分ける。**片方だけ見ると静かに取りこぼす。**"""
    aug = datetime(2026, 8, 22, 7, 18, 20)
    sep = datetime(2026, 9, 14, 23, 11, 1)
    _put(tmp_path / "2026-08", *_shot(aug, PNG_A))
    _put(tmp_path / "2026-09", *_shot(sep, PNG_B))

    got = collect.collect([tmp_path / "2026-08", tmp_path / "2026-09"])

    assert [s.captured_at for s in got.shots] == [aug, sep]


def test_ルートを渡さずには呼べない() -> None:
    """**保存先を既定値で持たない。** 環境変数やユーザー名から組み立てると、

    `USERNAME` が実際のユーザー名と違うときに**黙って検査ゼロ**になる
    （教訓 `detector-inputs-must-not-come-from-env`）。
    """
    with pytest.raises(TypeError):
        collect.collect()  # type: ignore[call-arg]


def test_画像だけ拾う(tmp_path: Path) -> None:
    """図版は画像。`.pdf` は Gemini が読めるが**スクリーンショットではない**。"""
    when = datetime(2026, 9, 14, 23, 11, 1)
    for name in ("a.png", "b.jpg", "c.jpeg", "d.webp"):
        _put(tmp_path, name, PNG_A + name.encode(), when)
    for name in ("e.pdf", "f.txt", "g.mp4", "h"):
        _put(tmp_path, name, b"x", when)

    got = collect.collect([tmp_path])

    assert sorted(s.path.name for s in got.shots) == ["a.png", "b.jpg", "c.jpeg", "d.webp"]


def test_拡張子の大小を問わない(tmp_path: Path) -> None:
    """`.PNG` で保存されることがある。**大文字だと1枚も拾わない**のは静かな事故。"""
    when = datetime(2026, 9, 14, 23, 11, 1)
    _put(tmp_path, "SHOT.PNG", PNG_A, when)

    got = collect.collect([tmp_path])

    assert [s.path.name for s in got.shots] == ["SHOT.PNG"]


def test_画像の名前のフォルダは拾わない(tmp_path: Path) -> None:
    """**フォルダは画像ではない。** 拾うと枚数が増え、中身を読む段で初めて落ちる。"""
    (tmp_path / "まぎらわしい.png").mkdir()

    got = collect.collect([tmp_path])

    assert got.shots == ()


# --------------------------------------------------------------------------
# 順序（H2）
# --------------------------------------------------------------------------


def test_撮影時刻の昇順に並ぶ(tmp_path: Path) -> None:
    """**撮影順を台帳が持つ。** 後段（`layout`）に順序を推測させない。

    実測（2026-09-14）では撮影順と記事の番号が一致しなかった
    （1→01, 2→04, 3→06, 6→02, 8→03）。*並び順を意味に使わない*ためには、
    まず**本当の撮影順**を持っている必要がある。
    """
    base = datetime(2026, 9, 14, 23, 0, 0)
    late = _shot(base + timedelta(minutes=30), PNG_A)
    early = _shot(base, PNG_B)
    _put(tmp_path, *late)
    _put(tmp_path, *early)

    got = collect.collect([tmp_path])

    assert [s.path.name for s in got.shots] == [early[0], late[0]]


def test_名前の順と撮影順が逆でも撮影順に並ぶ(tmp_path: Path) -> None:
    """**上のテストだけでは、並べ替えを検査できていなかった。**

    `_shot()` は時刻からファイル名を作るので、早い順＝名前順になる。
    Windows の `iterdir()` は名前順に返すため、*並べ替えを消しても通ってしまう*
    ——2026-09-19 のミューテーションで実際に素通りした。

    **名前順と撮影順を食い違わせて初めて、並べ替えを検査したことになる。**
    """
    early = datetime(2026, 9, 14, 23, 0, 0)
    late = datetime(2026, 9, 14, 23, 30, 0)
    _put(tmp_path, "01-late.png", PNG_A, late)
    _put(tmp_path, "99-early.png", PNG_B, early)

    got = collect.collect([tmp_path])

    assert [s.path.name for s in got.shots] == ["99-early.png", "01-late.png"]


def test_同時刻は名前順で決まる(tmp_path: Path) -> None:
    """撮影時刻が同じとき、**並びが実行のたびに入れ替わらない**こと。

    ルートを2つ渡し、**後ろのルートに名前が若いほう**を置く。
    並べ替えが時刻だけを見ていると、`sort` が安定なぶん
    *渡した順（= b が先）* が残る。名前まで見ていれば a が先に来る。
    """
    when = datetime(2026, 9, 14, 23, 11, 1)
    _put(tmp_path / "root1", "b.png", PNG_A, when)
    _put(tmp_path / "root2", "a.png", PNG_B, when)

    got = collect.collect([tmp_path / "root1", tmp_path / "root2"])

    assert [s.path.name for s in got.shots] == ["a.png", "b.png"]


# --------------------------------------------------------------------------
# ハッシュ（H4・M6）
# --------------------------------------------------------------------------


def test_ハッシュは中身から出る(tmp_path: Path) -> None:
    import hashlib

    when = datetime(2026, 9, 14, 23, 11, 1)
    _put(tmp_path, *_shot(when, PNG_A))

    got = collect.collect([tmp_path])

    assert got.shots[0].sha256 == hashlib.sha256(PNG_A).hexdigest()


def test_同じ中身の2枚を同一ハッシュとして挙げる(tmp_path: Path) -> None:
    """撮り直すと**ほぼ同じ絵**が2枚できる。完全に同じなら中身で分かる。"""
    a = datetime(2026, 9, 14, 23, 11, 1)
    b = datetime(2026, 9, 14, 23, 12, 1)
    p1 = _put(tmp_path, *_shot(a, PNG_A))
    p2 = _put(tmp_path, *_shot(b, PNG_A))

    got = collect.collect([tmp_path])

    assert list(got.duplicate_hashes.values()) == [(p1, p2)]


def test_中身が違えば同一ハッシュに挙げない(tmp_path: Path) -> None:
    a = datetime(2026, 9, 14, 23, 11, 1)
    b = datetime(2026, 9, 14, 23, 12, 1)
    _put(tmp_path, *_shot(a, PNG_A))
    _put(tmp_path, *_shot(b, PNG_B))

    got = collect.collect([tmp_path])

    assert got.duplicate_hashes == {}


# --------------------------------------------------------------------------
# 期間（--since / --until）
# --------------------------------------------------------------------------


def test_期間の両端を含む(tmp_path: Path) -> None:
    """**両端を含む。** 「いつからいつまで」と言われて端が落ちるのは事故になる。"""
    a = datetime(2026, 9, 14, 23, 0, 0)
    b = datetime(2026, 9, 15, 23, 0, 0)
    _put(tmp_path, *_shot(a, PNG_A))
    _put(tmp_path, *_shot(b, PNG_B))

    got = collect.collect([tmp_path], since=a, until=b)

    assert [s.captured_at for s in got.shots] == [a, b]


def test_期間の外は落ちる(tmp_path: Path) -> None:
    early = datetime(2026, 9, 13, 23, 0, 0)
    inside = datetime(2026, 9, 14, 23, 0, 0)
    late = datetime(2026, 9, 15, 23, 0, 0)
    for w, body in ((early, PNG_A), (inside, PNG_B), (late, PNG_A + b"z")):
        _put(tmp_path, *_shot(w, body))

    got = collect.collect([tmp_path], since=inside, until=inside)

    assert [s.captured_at for s in got.shots] == [inside]


# --------------------------------------------------------------------------
# 数えられなかったこと（M7・M9）
# --------------------------------------------------------------------------


def test_1枚も無いときを成功にしない(tmp_path: Path) -> None:
    """**対象0件は「うまくいった」ではない。**

    `all_ok` の「空を真にしない」と同じ形。*0件を成功にすると、
    設定を間違えて1枚も見ていない状態が、順調と見分けられなくなる。*
    """
    (tmp_path / "2026-09").mkdir()

    got = collect.collect([tmp_path / "2026-09"])

    assert got.shots == ()
    assert got.status == collect.EMPTY


def test_存在しないルートは確認不能になる(tmp_path: Path) -> None:
    """**原本が無いことと、原本に何も無いことは別。**

    フォルダごと消えている（掃除した・パスを間違えた）とき、「0件でした」と
    答えるのは嘘に近い。*数えきれていないなら、件数を言わない。*
    """
    got = collect.collect([tmp_path / "存在しない"])

    assert got.status == collect.UNKNOWN
    assert got.missing_roots == (tmp_path / "存在しない",)


def test_確認不能でも見えたぶんは返る(tmp_path: Path) -> None:
    """**確認不能は「何も分からない」ではない。** 見えたものは捨てない。

    捨てると、呼び出し側が「確認不能を承知で進める」判断をできなくなる。
    """
    when = datetime(2026, 9, 14, 23, 11, 1)
    _put(tmp_path / "2026-09", *_shot(when, PNG_A))

    got = collect.collect([tmp_path / "2026-09", tmp_path / "2026-08"])

    assert got.status == collect.UNKNOWN
    assert [s.captured_at for s in got.shots] == [when]
    assert got.scanned_roots == (tmp_path / "2026-09",)


def test_1枚でもあれば成功(tmp_path: Path) -> None:
    when = datetime(2026, 9, 14, 23, 11, 1)
    _put(tmp_path, *_shot(when, PNG_A))

    got = collect.collect([tmp_path])

    assert got.status == collect.OK


# --------------------------------------------------------------------------
# 名前と mtime の食い違い（H1）
# --------------------------------------------------------------------------


def test_名前とmtimeが揃っていれば食い違いにしない(tmp_path: Path) -> None:
    when = datetime(2026, 9, 14, 23, 11, 1)
    _put(tmp_path, *_shot(when, PNG_A))

    got = collect.collect([tmp_path])

    assert got.shots[0].name_at == when
    assert got.shots[0].time_mismatch is False


def test_名前とmtimeの食い違いを記録する(tmp_path: Path) -> None:
    """**名前で索引しない**（H1）。名前は mtime と突き合わせる相手でしかない。"""
    named = datetime(2020, 1, 1, 0, 0, 0)
    actual = datetime(2026, 9, 14, 23, 11, 1)
    _put(tmp_path, named.strftime(SHAREX) + ".png", PNG_A, actual)

    got = collect.collect([tmp_path])

    assert got.shots[0].captured_at == actual
    assert got.shots[0].name_at == named
    assert got.shots[0].time_mismatch is True


def test_名前から日時が読めないことを食い違いにしない(tmp_path: Path) -> None:
    """**読めなかったこと と 食い違ったこと を分ける。**

    リネーム済みのファイルを毎回「食い違い」として鳴らすと、
    *本物の食い違いが出ても同じ見た目*になって、検査が読まれなくなる。
    """
    when = datetime(2026, 9, 14, 23, 11, 1)
    _put(tmp_path, "01-verify-source.png", PNG_A, when)

    got = collect.collect([tmp_path])

    assert got.shots[0].name_at is None
    assert got.shots[0].time_mismatch is False
