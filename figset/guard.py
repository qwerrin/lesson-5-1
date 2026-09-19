"""外へ出す前に、ローカルだけで秘匿情報を見る段。

なぜ `classify` より前なのか
--------------------------------------------------------------------------

`classify` は画像を外部 API へ送って中身を判定する。だから
「秘匿情報が写っているか」を**外部 API に聞く**設計にすると、
*トークンが写った画像をトークンごと送ってから「写っていますね」と教わる*
という順番になる。**検査は通るが、守れていない**（`DESIGN.md` 3-3・M10）。

`guard` はそれを避けるために**ローカルだけ**で走る。
標準ライブラリしか使わず、外へは1バイトも出さない。

見る層と、見ていない層
--------------------------------------------------------------------------

=========== ================================== ==================================
層           何を見るか                          見られないとき
=========== ================================== ==================================
`path`      ファイルのパスそのもの               （常に見られる）
`metadata`  PNG の tEXt / zTXt / iTXt          PNG でない・壊れている → 未検査
`pixels`    画面に描かれた文字                  `ocr` を渡さなければ未検査
=========== ================================== ==================================

**全部の層を見て、何も見つからなかったときだけ `OK`。**
1つでも見ていない層があれば `UNKNOWN` で、*これは「安全」ではない*。

既定では `ocr` を渡さないので `OK` は出ない。**不便だが、隠さない。**
「見つからなかった」を「安全」と言い換えた瞬間、この道具は嘘をつく。

報告そのものが漏洩経路になる
--------------------------------------------------------------------------

この出力は**記事の図版になる**（`DESIGN.md` 11章の 04）。
だから `Finding` は**当たったもの**を持たない。持つのは
**規則の名前**と**どの層のどこで当たったか**だけで、しかもその「どこ」も伏せる
——当たった場所の名前が、当たったものでありうる。

教訓 `the-frame-leaks-not-the-content`：漏れるのは本文ではなく枠のほう。
ここでは「検査結果」という枠が、それになりうる。
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: 見る層。**「見た」と「見ていない」を層ごとに持つ。**
PATH = "path"
METADATA = "metadata"
PIXELS = "pixels"
LAYERS = (PATH, METADATA, PIXELS)

#: 判定。**`OK` は全部の層を見たときにしか出ない。**
OK = "ok"
BLOCKED = "blocked"
UNKNOWN = "unknown"

#: 伏せ字。**規則に当たった所だけを置き換える。**
MASK = "***"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class Rule:
    """探すものひとつ。`name` は**人に見せてよい名前**でなければならない。

    報告に出るのはこの名前だけなので、ここに秘匿そのものを書くと
    **伏せた意味が無くなる**。「ホームのパス」「Slack のトークン」のように書く。
    """

    name: str
    pattern: re.Pattern[str]


def literal_rule(name: str, text: str) -> Rule:
    """そのままの文字列を探す規則。**大小は区別しない。**

    Windows のパスは大小を区別しないので、区別すると `C:\\USERS\\...` が
    静かに素通りする。
    """
    return Rule(name, re.compile(re.escape(text), re.IGNORECASE))


@dataclass(frozen=True)
class Policy:
    """探すものの一覧。**空では作れない。**"""

    rules: tuple[Rule, ...]

    def __post_init__(self) -> None:
        if not self.rules:
            raise ValueError(
                "規則が0件のポリシーは、何を通しても「見つかりませんでした」と答える。"
                "検査が無いことには気づけるが、空であることには気づけない"
            )


@dataclass(frozen=True)
class Finding:
    """当たったこと。**当たったもの（秘匿そのもの）は持たない。**"""

    layer: str
    rule: str
    where: str


@dataclass(frozen=True)
class Verdict:
    path: Path
    findings: tuple[Finding, ...]
    checked: tuple[str, ...]
    unchecked: tuple[str, ...]
    label: str

    @property
    def status(self) -> str:
        """`BLOCKED` → `UNKNOWN` → `OK` の順に判定する。

        **危ないと分かったものを保留にしない**ので `BLOCKED` が最優先。
        見つからなくても、**見ていない層があれば安全とは言わない**。
        """
        if self.findings:
            return BLOCKED
        if self.unchecked:
            return UNKNOWN
        return OK

    @property
    def report(self) -> str:
        """人に見せる1件ぶん。**フルパスを載せない**（`label` は伏せ済みの名前）。"""
        lines = [f"{self.label}: {self.status}"]
        for finding in self.findings:
            lines.append(f"  [{finding.layer}] {finding.rule} ← {finding.where}")
        if self.unchecked:
            lines.append(f"  未検査: {', '.join(self.unchecked)}")
        return "\n".join(lines)


def redact(text: str, policy: Policy) -> str:
    """規則に当たった所を伏せる。**当たっていない所は残す。**

    全部伏せると人が場所を追えなくなるので、*当たった範囲だけ*を置き換える。
    """
    out = text
    for rule in policy.rules:
        out = rule.pattern.sub(MASK, out)
    return out


def inspect(
    path: Path,
    policy: Policy,
    *,
    ocr: Callable[[Path], str] | None = None,
) -> Verdict:
    """`path` を**ローカルだけで**見る。

    **`policy` に既定値を置かない**（H7）。環境変数やユーザー名から組み立てると、
    値がずれたときに**黙って検査ゼロ**になる
    （教訓 `detector-inputs-must-not-come-from-env`）。

    `ocr` を渡さなければ画素の層は見ず、**見ていないと申告する**。
    渡した場合、その `ocr` が外部通信するかどうかは**渡した側の責任**になる。
    """
    findings: list[Finding] = []
    checked: list[str] = []
    unchecked: list[str] = []

    # ---- path: 中身でなくても漏れる。送るときに名前を添えれば、名前は外に出る
    checked.append(PATH)
    for rule in _scan(str(path), policy):
        findings.append(Finding(PATH, rule.name, PATH))

    # ---- metadata: 撮影ツールが勝手に書き込む。画面に写っていなくても入る
    texts = _png_texts(path)
    if texts is None:
        unchecked.append(METADATA)
    else:
        checked.append(METADATA)
        for tag, key, value in texts:
            # **場所の名前も伏せる。** 当たった場所の名前が、当たったものでありうる。
            where = f"{tag}:{redact(key, policy)}"
            for rule in _scan(f"{key}\n{value}", policy):
                findings.append(Finding(METADATA, rule.name, where))

    # ---- pixels: 画面に描かれた文字。OCR は差し込み式
    if ocr is None:
        unchecked.append(PIXELS)
    else:
        checked.append(PIXELS)
        for rule in _scan(ocr(path), policy):
            findings.append(Finding(PIXELS, rule.name, PIXELS))

    return Verdict(
        path=path,
        findings=tuple(findings),
        checked=tuple(checked),
        unchecked=tuple(unchecked),
        label=redact(path.name, policy),
    )


def _scan(text: str, policy: Policy) -> Sequence[Rule]:
    """当たった規則を返す。**当たった文字列は返さない。**"""
    return tuple(rule for rule in policy.rules if rule.pattern.search(text))


def _png_texts(path: Path) -> list[tuple[str, str, str]] | None:
    """PNG の文字チャンクを (種別, キー, 値) で返す。**読めなければ `None`。**

    `None` は「文字チャンクが無かった」ではなく「**読めなかった**」である。
    空リストと `None` を混ぜると、*読めなかったファイルが「綺麗だった」に化ける*。

    拡張子では判断しない——**拡張子は中身の証拠にならない**。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(_PNG_SIGNATURE):
        return None

    chunks = _png_chunks(data)
    if chunks is None:
        return None

    found: list[tuple[str, str, str]] = []
    for tag, body in chunks:
        if tag == b"tEXt":
            key, _, value = body.partition(b"\x00")
            found.append(("tEXt", _latin1(key), _latin1(value)))
        elif tag == b"zTXt":
            key, _, rest = body.partition(b"\x00")
            value = _inflate(rest[1:])
            if value is not None:
                found.append(("zTXt", _latin1(key), value))
        elif tag == b"iTXt":
            item = _itxt(body)
            if item is not None:
                found.append(item)
    return found


def _png_chunks(data: bytes) -> list[tuple[bytes, bytes]] | None:
    """チャンクを頭から並べる。**最後まで読めなければ `None`。**

    途中で切れたファイルで「読めたぶん」を返すと、*全部読めていないのに
    読んだことになる*。この道具では、それは「綺麗だった」と同じ意味になってしまう。
    """
    chunks: list[tuple[bytes, bytes]] = []
    pos = len(_PNG_SIGNATURE)
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        tag = data[pos + 4 : pos + 8]
        start = pos + 8
        end = start + length
        if end + 4 > len(data):
            return None  # 途中で切れている
        chunks.append((tag, data[start:end]))
        if tag == b"IEND":
            return chunks
        pos = end + 4  # CRC を飛ばす
    return None  # IEND に辿り着いていない


def _itxt(body: bytes) -> tuple[str, str, str] | None:
    """iTXt は keyword \\0 圧縮フラグ 圧縮方式 言語 \\0 訳語 \\0 本文。"""
    key, sep, rest = body.partition(b"\x00")
    if not sep or len(rest) < 2:
        return None
    compressed = rest[0] == 1
    rest = rest[2:]
    _lang, sep, rest = rest.partition(b"\x00")
    if not sep:
        return None
    _translated, sep, raw = rest.partition(b"\x00")
    if not sep:
        return None
    if compressed:
        text = _inflate(raw)
        if text is None:
            return None
    else:
        text = raw.decode("utf-8", "replace")
    return "iTXt", key.decode("utf-8", "replace"), text


def _inflate(raw: bytes) -> str | None:
    try:
        return zlib.decompress(raw).decode("utf-8", "replace")
    except (zlib.error, ValueError):
        return None


def _latin1(raw: bytes) -> str:
    return raw.decode("latin-1", "replace")
