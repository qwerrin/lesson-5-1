"""1回の実行の結果を `01_Inbox/YYYY-MM-DD-scout.md` へ**追記**し、**読み戻す**段（M6）。

追記だけで済む形にする
--------------------------------------------------------------------------

frontmatter は**ファイルを作るときだけ**書き、中身は固定にする。件数を入れると
2回目の実行でファイル全体を書き戻すことになり、*読んだ後に他所が書いたぶんを消す*
（教訓 `read-modify-write-drops-concurrent-edits`）。件数は実行ごとの節に書く。

1回の実行は**1回の書き込み**で末尾に足し、読み戻しは**自分の run-id** で探す
——「最後の節」で探すと、同じ日の別の実行と取り違える。

読み戻しは入力と突き合わせる
--------------------------------------------------------------------------

書いた文字列と比べるだけだと、描画が記事を1件落としても「書いたとおり」と答える。
**件数と URL は入力から**取った期待値で数える。

読み戻しの失敗は**例外にしない**。例外だと `notify` まで届かず、失敗が無音になる。
直しも消しもしない——*手で書かれたものを機械が直すと、何を失ったかが分からなくなる*。

外から来た文字列
--------------------------------------------------------------------------

タイトル・要約は Qiita と Gemini から来る。**節の構造を壊せない形**にしてから書く:
要約は各行を `> ` で引用にし、`[` `]` `<` を逃がす。`[[` が残ると vault のリンク検査に
*偽のリンク切れ*が出る。URL は http(s) だけ通す（`javascript:` を vault に置かない）。

見ていないもの（2026-10-03 のレビュー・**承知で残す**）
--------------------------------------------------------------------------

**Obsidian 固有の記法**——`%%`（コメント・以降を閲覧画面で隠す）と `#語`（タグになる）。
節の構造は壊さないが、要約が隠れたりタグ一覧が汚れたりする。Obsidian でどう逃がせば
効くかを**公式で確かめていない**ので、推測で逃がしを書かない。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from split import Headline, Split
from summarize import Digest, Failure
from verify_source import CONFIRMED, MISMATCH, UNVERIFIABLE, Audit, Check

#: run-id に使える字。**節の見出しと HTML コメントの中に入る**ので、どちらも壊さない字だけ。
RUN_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z_.-]*")
TAGS = ("scout", "inbox")

VERDICTS = {
    CONFIRMED: "照合できた",
    MISMATCH: "本文に無い主張あり",
    UNVERIFIABLE: "確認できない",
}

#: URL の中で、Markdown のリンクを閉じてしまう字（`\)` は `)` の逃がしになる）・
#: Obsidian のリンクになる字（`[[`）・タイトル側と組んで code span になる字。
_URL_ESCAPES = {
    " ": "%20", "(": "%28", ")": "%29", "<": "%3C", ">": "%3E",
    "\\": "%5C", "[": "%5B", "]": "%5D", "`": "%60",
}
#: 見出しの行から URL を取り出す。**欲張りに取る**ので、タイトルに仕込んだ `](URL)` ではなく
#: 行末の本物に当たる（タイトル側の `]` は逃がしてある）。
_HEADING_URL = re.compile(r"^### \[.*\]\((\S*)\)$")


@dataclass(frozen=True)
class Emitted:
    path: Path
    created: bool
    entries: int
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def summary(self) -> str:
        """**件数と、読み戻せたかを必ず出す。**"""
        how = "新規" if self.created else "追記"
        result = "問題なし" if self.ok else f"問題 {len(self.problems)} 件: " + "／".join(self.problems)
        return f"{self.path.name} に {self.entries} 件を書いた（{how}）／読み戻し: {result}"


def emit(
    inbox: Path,
    *,
    at: datetime,
    run_id: str,
    stages: Sequence[str],
    split: Split,
    digest: Digest,
    audit: Audit,
) -> Emitted:
    """**書く前に全部確かめ、1回で書き、読み戻す。**"""
    if not inbox.is_dir():
        # **作らない。** 作ると、パスの書き間違いが「新しいフォルダに書けた」になる。
        raise ValueError(f"Inbox が無い: {inbox}")
    block, urls = render(at=at, run_id=run_id, stages=stages, split=split, digest=digest, audit=audit)

    path = inbox / f"{at:%Y-%m-%d}-scout.md"
    created = not path.exists()
    if created:
        # **開く前にバイト列にする。** 開いてから符号化で落ちると、空のファイルが残る。
        _write(path, "xb", _encode(_head(at) + block))
    else:
        data = _encode(block)
        old = _decode(path.read_bytes())
        if _section_start(old, run_id) >= 0:
            raise ValueError(f"run-id `{run_id}` は {path.name} で使用済み。同じ節が2つあると読み戻しで区別できない")
        # 節は `\n` で始まるので、手で書かれた最終行に改行が無くても**見出しは行頭に来る**。
        _write(path, "ab", data)

    text = _decode(path.read_bytes())
    problems = readback(
        text, block=block, run_id=run_id, urls=urls, entries=split.total, day=f"{at:%Y-%m-%d}"
    )
    return Emitted(path=path, created=created, entries=split.total, problems=problems)


def render(
    *,
    at: datetime,
    run_id: str,
    stages: Sequence[str],
    split: Split,
    digest: Digest,
    audit: Audit,
) -> tuple[str, tuple[str, ...]]:
    """1回の実行の節と、そこに載る URL を返す。**段どうしが合わないなら作らない**（E）。"""
    if at.tzinfo is not None:
        # **日付の物差しは1本。** fetch はローカルの素の日時にそろえている。
        raise ValueError(f"時差つきの日時は受け取らない: {at.isoformat()}")
    if not RUN_ID.fullmatch(run_id) or "--" in run_id:
        raise ValueError(f"run-id に使えない形: {run_id!r}")
    _same("split の要約対象", split.summarize, "digest の結果", [*digest.done, *digest.failed])
    if tuple(c.summary for c in audit.checks) != digest.done:
        # **キーではなく中身まで。** 照合した要約と書く要約がずれると、判定が別の文に付く。
        raise ValueError("digest の要約と audit の照合が、順番か中身で一致しない")
    twice = [k for k, n in Counter(map(_key, [*split.summarize, *split.headline])).items() if n > 1]
    if twice:
        raise ValueError(f"同じ記事が2回出る（要約と見出しの両方など）: {sorted(twice)}")

    entries: list[str] = []
    urls: list[str] = []
    for check in audit.checks:
        scored = check.summary.scored
        url = _url(scored.kept.article.url)
        urls.append(url)
        entries.append(
            _heading(scored.kept.article.title, url)
            + f"- {_inline(scored.kept.article.source)}・点 {scored.score}・照合: {_verdict(check)}\n"
            + _quote(check.summary.text)
        )
    for failure in digest.failed:
        url = _url(failure.scored.kept.article.url)
        urls.append(url)
        entries.append(_failure(failure, url))
    for headline in split.headline:
        url = _url(headline.kept.article.url)
        urls.append(url)
        entries.append(_headline(headline, url))

    lines = [f"- {_inline(stage)}" for stage in stages]
    lines.append(
        f"- 記事 {split.total} 件（要約 {len(digest.done)}"
        f"・要約できなかった {len(digest.failed)}・見出しだけ {len(split.headline)}）"
    )
    block = (
        f"\n## {at:%H:%M} {_label(run_id)}\n\n"
        + "\n".join(lines)
        + "\n"
        + "".join(f"\n{entry}" for entry in entries)
        + f"\n{_end(run_id)}\n"
    )
    return block, tuple(urls)


def readback(
    text: str, *, block: str, run_id: str, urls: Iterable[str], entries: int, day: str
) -> tuple[str, ...]:
    """読み戻した全文を見て、**問題を全部**返す。空なら問題なし。

    **CRLF は改ざんとして数えない。** vault は `core.autocrlf=true` なので、
    git が触ったファイルは CRLF で戻ってくる。
    """
    text = _normalize(text)
    problems: list[str] = []
    problems.extend(_check_frontmatter(text, day))

    times = text.count(block)
    if times == 0:
        problems.append(f"run {run_id} の節が書いたとおりに読み戻せない")
    elif times > 1:
        problems.append(f"run {run_id} の節が {times} 回ある")

    start = _section_start(text, run_id)
    end = text.find(_end(run_id), start) if start >= 0 else -1
    if start < 0 or end < 0:
        problems.append(f"run {run_id} の節の始まりか終わりが見つからない")
        return tuple(problems)
    section = text[start:end]
    headings = [line for line in section.split("\n") if line.startswith("### ")]
    if len(headings) != entries:
        problems.append(f"記事が {len(headings)} 件（期待は {entries} 件）")
    # **見出しの行ごとに数える。** 節のどこかに `](URL)` があるかで見ると、
    # タイトルに仕込んだ文字列で通り、同じ URL が2件あっても1件で通る。
    linked = Counter(m[1] for m in map(_HEADING_URL.fullmatch, headings) if m)
    problems.extend(f"URL が無い: {url}" for url in sorted((Counter(urls) - linked).elements()))
    return tuple(problems)


# ---------------------------------------------------------------------------
# 書き込み
# ---------------------------------------------------------------------------


def _write(path: Path, mode: str, data: bytes) -> None:
    # **バイト列で書く。** テキストモードだと Windows の既定で改行が CRLF に化ける。
    with path.open(mode) as f:
        f.write(data)


def _encode(text: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as e:
        # 孤立サロゲートは `json.loads` を通ってくる。
        raise ValueError(f"UTF-8 にできない字がある: {e}") from e


def _decode(raw: bytes) -> str:
    # 手で開いたエディタが BOM を付けることがある。**付いていても同じ中身として読む。**
    return raw.decode("utf-8-sig")


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n")


def _head(at: datetime) -> str:
    tags = "".join(f"  - {t}\n" for t in TAGS)
    return f"---\ntags:\n{tags}date: {at:%Y-%m-%d}\n---\n\n# {at:%Y-%m-%d} scout\n"


def _label(run_id: str) -> str:
    # 前後をバッククォートで閉じるので、`r1` が `r10` の頭に当たらない。
    return f"実行 `{run_id}`"


def _section_start(text: str, run_id: str) -> int:
    """**節の見出しの行**で探す。部分一致だと、前の実行の記事タイトルに当たる。"""
    pattern = rf"^## [0-9]{{2}}:[0-9]{{2}} {re.escape(_label(run_id))}$"
    match = re.search(pattern, _normalize(text), flags=re.MULTILINE)
    return match.start() if match else -1


def _end(run_id: str) -> str:
    return f"<!-- scout:end {run_id} -->"


def _check_frontmatter(text: str, day: str) -> list[str]:
    """**自分が書く形と、Obsidian が書き直しうる形**だけを読む。YAML 全体は読まない。"""
    if not text.startswith("---\n"):
        return ["frontmatter が先頭に無い"]
    close = text.find("\n---\n", 3)
    if close < 0:
        return ["frontmatter が閉じていない"]
    fields: dict[str, list[str] | str] = {}
    key = ""
    for line in text[4:close].split("\n"):
        if line.startswith("  - ") and isinstance(fields.get(key), list):
            fields[key].append(_unquote(line[4:]))  # type: ignore[union-attr]
        elif ":" in line:
            key, _, value = line.partition(":")
            value = value.strip()
            if value.startswith("[") and value.endswith("]"):
                # `tags: [scout, inbox]`——プロパティ画面や手で直すとこの形になる。
                fields[key] = [_unquote(v) for v in value[1:-1].split(",") if v.strip()]
            else:
                fields[key] = _unquote(value) if value else []
    problems: list[str] = []
    tags = fields.get("tags")
    if not isinstance(tags, list) or "scout" not in tags:
        problems.append("frontmatter の tags に scout が無い")
    date = fields.get("date")
    # **形の検査は別に置かない。** `day` は必ず YYYY-MM-DD なので、形が崩れていれば一致しない。
    if date != day:
        problems.append(f"frontmatter の date が {date!r}（この節は {day}）")
    return problems


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


# ---------------------------------------------------------------------------
# 1件分
# ---------------------------------------------------------------------------


def _heading(title: str, url: str) -> str:
    return f"### [{_inline(title) or '（無題）'}]({url})\n"


def _verdict(check: Check) -> str:
    label = VERDICTS.get(check.verdict, _inline(check.verdict))
    # **消さずに印を付ける**（6-2）。主張は要約器が書いた語＝外から来た文字列なので、
    # code span には入れない（中では逃がしが効かず、空の `` は span にならない）。
    marked = "・".join(f"「{_inline(c)}」" for c in check.missing if _line(c))
    if not marked:
        return label
    return f"{label}（{marked}）"


def _failure(failure: Failure, url: str) -> str:
    article = failure.scored.kept.article
    return (
        _heading(article.title, url)
        + f"- {_inline(article.source)}・点 {failure.scored.score}・要約できなかった（{_inline(failure.reason)}）\n"
    )


def _headline(headline: Headline, url: str) -> str:
    article = headline.kept.article
    # **0 と「点なし」を混ぜない。**
    score = "なし" if headline.score is None else str(headline.score)
    return (
        _heading(article.title, url)
        + f"- {_inline(article.source)}・点 {score}・見出しだけ（{_inline(headline.reason)}）\n"
    )


def _quote(text: str) -> str:
    """要約は**全行を引用**にする。中に `## ` や `---` があっても節を作らない。"""
    return "".join(f"> {_escape(line)}\n" for line in text.splitlines())


# ---------------------------------------------------------------------------
# 文字列
# ---------------------------------------------------------------------------


def _same(left_name: str, left: Iterable[object], right_name: str, right: Iterable[object]) -> None:
    """**件数だけでなく、どの記事か**まで合わせる。数が同じで中身が違うのも止める。"""
    a = Counter(_key(x) for x in left)
    b = Counter(_key(x) for x in right)
    if a != b:
        raise ValueError(f"{left_name}と{right_name}が合わない: 片方だけにある {sorted((a - b) + (b - a))}")


def _key(item: object) -> str:
    scored = getattr(item, "scored", item)
    return scored.kept.key  # type: ignore[attr-defined]


def _url(url: str) -> str:
    # **`urlsplit` より先に見る。** 前後の空白や制御文字を黙って落とすので、
    # 検査したものと書くものが食い違う（` https://…` が `%20https://…`＝相対リンクになる）。
    if url != url.strip() or any(
        unicodedata.category(ch) == "Cc" or (ch.isspace() and ch != " ") for ch in url
    ):
        raise ValueError(f"URL の前後に空白があるか、制御文字・改行を含む: {url!r}")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"http(s) でない URL は書かない: {url!r}")
    return "".join(_URL_ESCAPES.get(ch, ch) for ch in url)


def _line(text: str) -> str:
    return " ".join(text.split())


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;")


def _inline(text: str) -> str:
    return _escape(_line(text))
