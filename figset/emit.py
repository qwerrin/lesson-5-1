"""`docs/` へ図版・対応表・貼り付け用 HTML・台帳を書き出す段。

出すもの
--------------------------------------------------------------------------

=================== ==========================================================
`NN-slug.ext`       図版（**原本のコピー**。移動も削除もしない）
`README.md`         対応表（**2系統の番号を両方持つ**）
`figs.html`         記事に貼る `<img>` 断片
`figset.json`       台帳（採用も没も載せる）
=================== ==========================================================

なぜ `emit` も伏せるのか
--------------------------------------------------------------------------

`guard` は「**送る**前に見る」段で、`emit` は「**貼る**前に書く」段である。
`docs/README.md` は記事にそのまま引用されるので、
**原本のフルパス（＝ホームのパス）を書いた瞬間に漏れる**。

だから `emit` も `guard.Policy` を受け取り、**書くものを全部 `redact` に通す**。
*出口を1つに絞る*——説明文にも、原本の名前にも、秘匿は入りうる。

書けたことは、読み戻せることの証拠にならない
--------------------------------------------------------------------------

教訓 `write-is-not-readback`。コピーしたら**読み戻してハッシュを照合する**。
例外が出なかったことは、*中身が同じであること*を言わない。

同名を黙って上書きしない
--------------------------------------------------------------------------

`created` / `replaced` / `unchanged` を分ける（M6）。
「書いた」だけだと、*前に何があったか*が消える。
"""

from __future__ import annotations

import hashlib
import html
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from guard import Policy, redact
from layout import COMPLETE, Layout

#: 書き込みの結果。**同名を黙って上書きしない**ために3つに分ける（M6）。
CREATED = "created"
REPLACED = "replaced"
UNCHANGED = "unchanged"

README = "README.md"
HTML = "figs.html"
LEDGER = "figset.json"

#: 台帳と対応表で原本を指すときの、ハッシュの頭の長さ。
DIGEST_HEAD = 12


@dataclass(frozen=True)
class Written:
    path: Path
    action: str


@dataclass(frozen=True)
class Emission:
    docs: Path
    files: tuple[Written, ...]
    #: 書いたあと**読み戻してハッシュが一致した**枚数。
    verified: int
    status: str

    def action_of(self, name: str) -> str:
        for written in self.files:
            if written.path.name == name:
                return written.action
        raise KeyError(f"書き出していない: {name}")


def emit(plan: Layout, docs: Path, policy: Policy) -> Emission:
    """`docs/` へ4種類を書き出す。

    **`policy` に既定値を置かない。** `docs/` は記事に貼る場所なので、
    書くものは全部 `redact` に通す。
    """
    docs.mkdir(parents=True, exist_ok=True)
    files: list[Written] = []
    verified = 0

    for placement in plan.placements:
        target = docs / placement.filename
        body = placement.shot.path.read_bytes()
        files.append(Written(target, _put(target, body)))
        # **読み戻す。** 書けたことは、同じものが入っていることを言わない。
        if hashlib.sha256(target.read_bytes()).hexdigest() == placement.shot.sha256:
            verified += 1

    for name, text in (
        (README, _readme(plan, policy)),
        (HTML, _html(plan, policy)),
        (LEDGER, _ledger(plan, policy)),
    ):
        target = docs / name
        files.append(Written(target, _put(target, text.encode("utf-8"))))

    return Emission(docs=docs, files=tuple(files), verified=verified, status=plan.status)


def _put(path: Path, body: bytes) -> str:
    """**前に何があったかを潰さない。** 同じなら触らない。"""
    if path.exists():
        if path.read_bytes() == body:
            return UNCHANGED
        path.write_bytes(body)
        return REPLACED
    path.write_bytes(body)
    return CREATED


# --------------------------------------------------------------------------
# 対応表
# --------------------------------------------------------------------------


def _readme(plan: Layout, policy: Policy) -> str:
    def safe(text: str) -> str:
        return redact(text, policy)

    lines = [
        "# 図版",
        "",
        "記事に貼るスクリーンショット。**番号は記事に並べる順**で、",
        "`shots.py` の番号（**実行する順**）とは別である。",
        "",
        "**番号が2系統ある。** 混ぜると必ず取り違えるので、対応を書いておく。",
        "",
        f"状態: **{plan.status}**",
        "",
        "| 図版 | `shots.py` | 何を写しているか | この絵が証明すること |",
        "|---|---|---|---|",
    ]
    for placement in plan.placements:
        figure = placement.figure
        shots_no = "（手動）" if figure.shots_no is None else f"{figure.shots_no:02d}"
        lines.append(
            f"| `{safe(placement.filename)}` | {shots_no} "
            f"| {safe(figure.caption)} | {safe(figure.claim)} |"
        )

    lines += _section(
        "絵が無い図版（撮り忘れ）",
        [f"- `{safe(f.key)}`（記事 {f.article_no:02d}・{safe(f.caption)}）" for f in plan.missing],
        "**定義に行があるのに絵が無い。** 撮ってから出し直す。",
    )
    lines += _section(
        "定義に無い割付（貼り忘れ）",
        [f"- `{safe(key)}`" for key in plan.orphans],
        "**絵はあるが、図版の定義に無い。** 定義を足すか、割付を外す。",
    )
    lines += _section(
        "1つの図版に2枚（食い違い）",
        [
            f"- `{safe(key)}`: " + " / ".join(f"`{s.sha256[:DIGEST_HEAD]}`" for s in shots)
            for key, shots in plan.conflicts.items()
        ],
        "**どちらを使うか決まっていない。** 勝手に選ばないので、割付で決める。",
    )
    lines += _section(
        f"撮ったが使わなかったもの（{len(plan.rejected)}枚）",
        [
            f"- `{shot.sha256[:DIGEST_HEAD]}` "
            f"{shot.captured_at.isoformat(sep=' ')}（`{safe(shot.path.name)}`）"
            for shot in plan.rejected
        ],
        "**消していない。** 消すと「撮ったが使わなかった」と「撮っていない」が混ざる。",
    )
    return "\n".join(lines) + "\n"


def _section(title: str, body: Sequence[str], note: str) -> list[str]:
    """**空でも見出しを出す。** 節ごと消すと、*見ていないのか、無いのか*が分からない。"""
    lines = ["", f"## {title}", "", note, ""]
    lines += list(body) if body else ["（なし）"]
    return lines


# --------------------------------------------------------------------------
# 貼り付け用の HTML
# --------------------------------------------------------------------------


def _html(plan: Layout, policy: Policy) -> str:
    def safe(text: str) -> str:
        # **伏せてから逃がす。** 逆にすると、伏せ字が実体参照を壊す。
        return html.escape(redact(text, policy), quote=True)

    blocks = []
    for placement in plan.placements:
        figure = placement.figure
        blocks.append(
            "<figure>\n"
            f'  <img src="{safe(placement.filename)}" alt="{safe(figure.caption)}">\n'
            f"  <figcaption>{safe(figure.claim)}</figcaption>\n"
            "</figure>"
        )
    return "\n".join(blocks) + ("\n" if blocks else "")


# --------------------------------------------------------------------------
# 台帳
# --------------------------------------------------------------------------


def _ledger(plan: Layout, policy: Policy) -> str:
    """**原本はハッシュで指す。** 名前は採用時に変わるし、パスは秘匿を含む。"""

    def safe(text: str) -> str:
        return redact(text, policy)

    payload = {
        "status": plan.status,
        "complete": plan.status == COMPLETE,
        "placements": [
            {
                "key": safe(p.figure.key),
                "article_no": p.figure.article_no,
                "shots_no": p.figure.shots_no,
                "file": safe(p.filename),
                "sha256": p.shot.sha256,
                "source_name": safe(p.shot.path.name),
                "captured_at": p.shot.captured_at.isoformat(),
            }
            for p in plan.placements
        ],
        "rejected": [
            {
                "sha256": s.sha256,
                "source_name": safe(s.path.name),
                "captured_at": s.captured_at.isoformat(),
            }
            for s in plan.rejected
        ],
        "missing": [safe(f.key) for f in plan.missing],
        "orphans": [safe(key) for key in plan.orphans],
        "conflicts": {
            safe(key): [s.sha256 for s in shots] for key, shots in plan.conflicts.items()
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
