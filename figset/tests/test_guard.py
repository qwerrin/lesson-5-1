"""figset/guard のテスト。**実装より先に書いた。**

`guard` は **外へ出す前**に走る段。`classify` は画像を外部 API へ送るので、
**順番が逆になった瞬間に、この道具は「秘匿情報を送ってから、送ってはいけない
ものでしたと教える道具」になる**（`DESIGN.md` 3-3・M10）。

============ ====================================================================
根拠         ここで守ること
============ ====================================================================
M5           ホームのパス・利用者名・トークンの形が写っていないか
M10          **検査は送る前にローカルで走る**。外部 API に聞かない
H7           見るものは**引数で渡す**。環境変数やユーザー名から組み立てない
M8           **見ていない層があることを、見つからなかったことに混ぜない**
M9           見るものが空のポリシーを成功にしない（＝黙って検査ゼロにしない）
============ ====================================================================

物差しに、実在のユーザー名を使わない
--------------------------------------------------------------------------

最初この定数を**実在のホームのパス**で書いて、**書き直した**。

（**ここに実物を書かない。** このリポジトリは公開されている——
*架空の名前に差し替えた理由を説明する文が、実物を載せていた*。
`guard` の M5「報告そのものが漏洩経路になる」を、この docstring で踏んだ。）

`--basetemp .pytest_tmp` を使うと `tmp_path` は**リポジトリの下**、つまり
*実在のホームディレクトリの下*に作られる。すると:

- 「パスが綺麗なら見つけない」テストは、**何を置いても必ず当たって**落ちる
- 「パスに秘匿文字列があれば止める」テストは、**フィクスチャを作らなくても通る**
  ——通る理由が、テストが用意したものではなくなる

*検査対象が、検査の物差しを含んでいた。* 架空の利用者名を使う。

報告そのものが漏洩経路になる
--------------------------------------------------------------------------

この検査の出力は**記事の図版になる**（`DESIGN.md` 11章の 04）。
だから報告に秘匿文字列の断片を1文字も持たせない。
見つけたことを言うのに、見つけたものを見せる必要はない
——**規則の名前と、どの層で当たったか**があれば人は動ける。

教訓 `the-frame-leaks-not-the-content`：出力を grep したら3つとも綺麗だったのに、
スクショにホームのパスが写った。*漏れるのは、自分が書いた本文ではなく枠のほう*。
ここでは「検査結果」という枠が、それになりうる。

「安全」と言える条件
--------------------------------------------------------------------------

**全部の層を見て、何も見つからなかったときだけ** `OK`。
1つでも見ていない層があれば `UNKNOWN` で、*これは「安全」ではない*。

既定では画素（描かれた文字）を見ないので、**既定の呼び出しでは `OK` は出ない**。
OCR を差し込めば出る。**出ないことを隠さないのが、この設計の要点**であって、
不便を承知で選んでいる。
"""

from __future__ import annotations

import re
import socket
import struct
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "figset"))

import guard  # noqa: E402

#: **架空**のホーム。実在のものを使うと、tmp_path 自身が当たってしまう（上の節）。
SECRET = r"C:\Users\dummyuser"
USER = "dummyuser"
TOKEN = "xoxb-DUMMY-TOKEN-FOR-TESTS-not-a-real-credential"


def _policy() -> guard.Policy:
    return guard.Policy(
        (
            guard.literal_rule("ホームのパス", SECRET),
            guard.Rule("Slack のトークン", re.compile(r"xoxb-[A-Za-z0-9-]+")),
        )
    )


def _user_policy() -> guard.Policy:
    return guard.Policy((guard.literal_rule("利用者名", USER),))


def _chunk(tag: bytes, data: bytes) -> bytes:
    """PNG のチャンク1つ。長さ・種別・中身・CRC。"""
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def _png(
    path: Path,
    *,
    text: tuple[str, str] | None = None,
    ztxt: tuple[str, str] | None = None,
    itxt: tuple[str, str] | None = None,
    truncate: int = 0,
) -> Path:
    """最小の PNG を書く。文字チャンクは3種類とも作れる。

    Pillow に依存させない——**依存を足すと、この検査自体が環境の都合で落ちる**。

    `truncate` を渡すと**末尾をそのバイト数だけ削る**。途中で切れたファイルを
    「読めたぶんだけ読んだ」で済ませていないかを見るために使う。
    """

    chunk = _chunk
    w = h = 2
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * w for _ in range(h))
    blob = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    if text is not None:
        key, value = text
        blob += chunk(b"tEXt", key.encode("latin-1") + b"\x00" + value.encode("latin-1"))
    if ztxt is not None:
        key, value = ztxt
        payload = key.encode("latin-1") + b"\x00\x00" + zlib.compress(value.encode("utf-8"))
        blob += chunk(b"zTXt", payload)
    if itxt is not None:
        key, value = itxt
        # keyword \0 圧縮フラグ 圧縮方式 言語 \0 訳語 \0 本文（非圧縮）
        payload = key.encode("utf-8") + b"\x00\x00\x00" + b"\x00" + b"\x00" + value.encode("utf-8")
        blob += chunk(b"iTXt", payload)
    blob += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    if truncate:
        blob = blob[:-truncate]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


# --------------------------------------------------------------------------
# ポリシー（H7・M9）
# --------------------------------------------------------------------------


def test_見るものが無いポリシーは作れない() -> None:
    """**空のポリシーを成功にしない。**

    規則が0件の検査は、何を通しても「見つかりませんでした」と答える。
    *検査があるのに検査していない*状態は、検査が無いより悪い
    ——**無いことには気づけるが、空であることには気づけない。**
    """
    with pytest.raises(ValueError):
        guard.Policy(())


def test_ポリシーを渡さずには呼べない(tmp_path: Path) -> None:
    """**見るものに既定値を置かない**（教訓 `detector-inputs-must-not-come-from-env`）。"""
    png = _png(tmp_path / "a.png")
    with pytest.raises(TypeError):
        guard.inspect(png)  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# path 層
# --------------------------------------------------------------------------


def test_パスに秘匿文字列があれば止める(tmp_path: Path) -> None:
    """**画像の中身でなくても漏れる。** 送るときに名前を添えれば、名前は外に出る。"""
    png = _png(tmp_path / "Users" / USER / "a.png")

    got = guard.inspect(png, _user_policy())

    assert got.status == guard.BLOCKED
    assert [f.layer for f in got.findings] == [guard.PATH]
    assert [f.rule for f in got.findings] == ["利用者名"]


def test_パスの大小が違っても見つける(tmp_path: Path) -> None:
    """Windows のパスは大小を区別しない。**区別すると静かに素通りする。**"""
    png = _png(tmp_path / "USERS" / USER.upper() / "a.png")

    got = guard.inspect(png, _user_policy())

    assert got.status == guard.BLOCKED


def test_パスが綺麗なら見つけない(tmp_path: Path) -> None:
    png = _png(tmp_path / "01-verify-source.png")

    got = guard.inspect(png, _policy())

    assert got.findings == ()


def test_パスは常に見たことにする(tmp_path: Path) -> None:
    """**見た層を申告しないと、見ていない層との区別が付かない。**"""
    png = _png(tmp_path / "a.png")

    got = guard.inspect(png, _policy())

    assert guard.PATH in got.checked
    assert guard.PATH not in got.unchecked


# --------------------------------------------------------------------------
# metadata 層（PNG の tEXt）
# --------------------------------------------------------------------------


def test_PNGのテキストチャンクを見る(tmp_path: Path) -> None:
    """**撮影ツールが勝手に書き込む。** 画面に写っていなくてもファイルには入る。"""
    png = _png(tmp_path / "a.png", text=("Software", f"ShareX from {SECRET}"))

    got = guard.inspect(png, _policy())

    assert got.status == guard.BLOCKED
    assert [f.layer for f in got.findings] == [guard.METADATA]
    assert got.findings[0].where == "tEXt:Software"


def test_PNGのテキストチャンクが綺麗なら見つけない(tmp_path: Path) -> None:
    png = _png(tmp_path / "a.png", text=("Software", "ShareX"))

    got = guard.inspect(png, _policy())

    assert got.findings == ()


def test_PNG以外はメタデータを見ていないことにする(tmp_path: Path) -> None:
    """**読めない形式を「綺麗だった」にしない。** 読めないことは、無いことではない。"""
    jpg = tmp_path / "a.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 32)

    got = guard.inspect(jpg, _policy())

    assert guard.METADATA in got.unchecked
    assert guard.METADATA not in got.checked


def test_壊れたPNGはメタデータを見ていないことにする(tmp_path: Path) -> None:
    """**拡張子は中身の証拠にならない。** 読めなかったなら、そう言う。"""
    broken = tmp_path / "a.png"
    broken.write_bytes(b"not a png at all")

    got = guard.inspect(broken, _policy())

    assert guard.METADATA in got.unchecked


def test_途中で切れたPNGは読めなかったことにする(tmp_path: Path) -> None:
    """**読めたぶんだけ読んで「見た」と言わない。**

    途中で切れたファイルは、切れた先に何が入っていたか分からない。
    それを「綺麗だった」と同じ扱いにすると、*欠けたぶんが安全側に化ける*。
    """
    png = _png(tmp_path / "a.png", text=("Software", "ShareX"), truncate=12)

    got = guard.inspect(png, _policy())

    assert guard.METADATA in got.unchecked


def test_署名が無ければチャンクを読まない(tmp_path: Path) -> None:
    """**PNG だと名乗っていないものを、PNG として読まない。**

    中身がチャンクとして読めてしまう形をしていても、署名が無いなら PNG ではない。
    ここを飛ばすと、*PNG でないファイルを読んで「見た」と申告する*ことになる。

    このファイルは署名だけが違い、**中に秘匿文字列を入れてある**。
    署名を見ていれば「未検査」で、見ていなければ「見つけた」になる
    ——**どちらに転んだかで、署名を見たかどうかが分かる。**
    """
    blob = b"NOT-APNG"  # 署名と同じ8バイトぶんの別物
    blob += _chunk(b"tEXt", b"Software\x00" + SECRET.encode("latin-1"))
    blob += _chunk(b"IEND", b"")
    target = tmp_path / "a.png"
    target.write_bytes(blob)

    got = guard.inspect(target, _policy())

    assert guard.METADATA in got.unchecked
    assert got.findings == ()


def test_CRCまで読めないチャンクは読めなかったことにする(tmp_path: Path) -> None:
    """**チャンクは長さ・種別・中身・CRC で1つ。** CRC が無いなら読み切っていない。

    末尾の 4 バイト（IEND の CRC）だけを削る。読み切ったことにすると、
    *手前の tEXt に入れた秘匿文字列を「見た上で見つけた」と報告してしまう*
    ——**見たことにしてはいけない。**
    """
    png = _png(tmp_path / "a.png", text=("Software", SECRET), truncate=4)

    got = guard.inspect(png, _policy())

    assert guard.METADATA in got.unchecked
    assert guard.METADATA not in got.checked


def test_文字チャンクが無いPNGは見たことにする(tmp_path: Path) -> None:
    """**「読んだが空だった」と「読めなかった」を混ぜない。**

    どちらも見つかった件数は0だが、意味はまったく違う。
    """
    png = _png(tmp_path / "a.png")

    got = guard.inspect(png, _policy())

    assert guard.METADATA in got.checked
    assert guard.METADATA not in got.unchecked


def test_zTXtチャンクを見る(tmp_path: Path) -> None:
    """**圧縮された文字チャンクも中身は文字。** 読まなければ素通りする。"""
    png = _png(tmp_path / "a.png", ztxt=("Comment", f"path={SECRET}"))

    got = guard.inspect(png, _policy())

    assert got.status == guard.BLOCKED
    assert got.findings[0].where == "zTXt:Comment"


def test_iTXtチャンクを見る(tmp_path: Path) -> None:
    """日本語が入るのはこちら（UTF-8）。**`tEXt` だけ見ていると落ちる。**"""
    png = _png(tmp_path / "a.png", itxt=("説明", f"保存先は {SECRET} です"))

    got = guard.inspect(png, _policy())

    assert got.status == guard.BLOCKED
    assert got.findings[0].where == "iTXt:説明"


def test_トークンの形に一致すれば止める(tmp_path: Path) -> None:
    png = _png(tmp_path / "a.png", text=("Comment", f"token={TOKEN}"))

    got = guard.inspect(png, _policy())

    assert got.status == guard.BLOCKED
    assert [f.rule for f in got.findings] == ["Slack のトークン"]


# --------------------------------------------------------------------------
# pixels 層（OCR は差し込み式）
# --------------------------------------------------------------------------


def test_OCRを渡さなければ画素は見ていないことにする(tmp_path: Path) -> None:
    png = _png(tmp_path / "a.png")

    got = guard.inspect(png, _policy())

    assert guard.PIXELS in got.unchecked


def test_OCRを渡せば画素も見る(tmp_path: Path) -> None:
    """**差し込んだ OCR の結果に、規則を実際に当てていること。**

    ここで偽物を渡すのは手抜きではなく、*画素の層が規則へ繋がっているか*を
    見るためである。繋がっていなければ、本物の OCR を入れても何も止まらない
    （教訓 `the-fake-is-the-evidence` の裏面——**偽物で何を証明したかを書く**）。
    """
    png = _png(tmp_path / "a.png")

    got = guard.inspect(png, _policy(), ocr=lambda _p: f"PS {SECRET}\\lesson-5-1>")

    assert got.status == guard.BLOCKED
    assert [f.layer for f in got.findings] == [guard.PIXELS]


def test_全部見て何も無ければ安全と言える(tmp_path: Path) -> None:
    png = _png(tmp_path / "01-verify-source.png", text=("Software", "ShareX"))

    got = guard.inspect(png, _policy(), ocr=lambda _p: "引用照合: 8 件中 2 件が台本と一致")

    assert got.unchecked == ()
    assert got.status == guard.OK


# --------------------------------------------------------------------------
# 判定（M8）
# --------------------------------------------------------------------------


def test_見ていない層があるなら安全と言わない(tmp_path: Path) -> None:
    """**見つからなかったことと、見ていないことを混ぜない。**

    `verify_doc.py` の「値が返らなかった項目を OK にしない」と同じ規律。
    """
    png = _png(tmp_path / "01-verify-source.png", text=("Software", "ShareX"))

    got = guard.inspect(png, _policy())

    assert got.findings == ()
    assert got.status == guard.UNKNOWN


def test_見つかれば見ていない層があっても止める(tmp_path: Path) -> None:
    """**BLOCKED が UNKNOWN より強い。** 危ないと分かったものを保留にしない。"""
    png = _png(tmp_path / "a.png", text=("Software", SECRET))

    got = guard.inspect(png, _policy())

    assert guard.PIXELS in got.unchecked
    assert got.status == guard.BLOCKED


# --------------------------------------------------------------------------
# 報告そのものが漏らさない
# --------------------------------------------------------------------------


def test_報告に秘匿文字列そのものを持たない(tmp_path: Path) -> None:
    """**この出力は記事の図版になる。** 断片も残さない。"""
    png = _png(tmp_path / "a.png", text=("Software", f"{SECRET} / {TOKEN}"))

    got = guard.inspect(png, _policy())

    assert SECRET not in got.report
    assert TOKEN not in got.report
    # 実在のホームごと載せない。**フルパスを報告に入れた瞬間、これが落ちる。**
    assert str(png.parent) not in got.report


def test_報告は伏せた上で規則の名前と層を出す(tmp_path: Path) -> None:
    """伏せるのと、何も言わないのは別。**人が動ける情報は残す。**"""
    png = _png(tmp_path / "a.png", text=("Software", SECRET))

    got = guard.inspect(png, _policy())

    assert "ホームのパス" in got.report
    assert "tEXt:Software" in got.report


def test_秘匿文字列を含む名前は伏せて出す(tmp_path: Path) -> None:
    """**ファイル名そのものが秘匿のことがある。** 報告に素で載せない。"""
    png = _png(tmp_path / f"{USER}-の画面.png")

    got = guard.inspect(png, _user_policy())

    assert USER not in got.report
    assert "の画面.png" in got.report


def test_チャンクの名前が秘匿でも伏せて出す(tmp_path: Path) -> None:
    """**当たった場所の名前も、当たったものでありうる。**

    値は ASCII にする——`tEXt` は仕様上 latin-1 しか入らない
    （日本語を入れるなら `iTXt`）。**ここで見たいのはキーのほう。**
    """
    png = _png(tmp_path / "a.png", text=(USER, "nothing special"))

    got = guard.inspect(png, _user_policy())

    # **キーも走査の対象。** 値だけ見ていると、ここで当たらない。
    assert got.status == guard.BLOCKED
    assert [f.layer for f in got.findings] == [guard.METADATA]
    assert USER not in got.report


def test_伏せ字は規則に当たった所だけ() -> None:
    assert guard.redact(f"C:/Users/{USER}/Documents", _user_policy()) == "C:/Users/***/Documents"


# --------------------------------------------------------------------------
# 外部へ出さないこと（M10・U6）
# --------------------------------------------------------------------------


def test_外部通信の口を塞いでも通る(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**「呼んでいない」は主張であって証拠ではない。**

    ソケットを作れない状態で検査を通す。これで示せるのは
    *この呼び出しの中で `socket.socket` を作っていない*ことだけで、
    「外部通信が絶対に無い」の証明ではない——が、主張よりははるかに強い。

    OCR を差し込んだ場合は**差し込んだ側の責任**になる。
    ここで塞いでいるのは `guard` 自身の経路だけである。
    """

    def _no_socket(*args: object, **kwargs: object) -> None:
        raise RuntimeError("guard は外部通信してはいけない")

    monkeypatch.setattr(socket, "socket", _no_socket)

    png = _png(tmp_path / "a.png", text=("Software", SECRET))
    got = guard.inspect(png, _policy())

    assert got.status == guard.BLOCKED
