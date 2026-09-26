"""`split` が要約へ回した記事を、1件ずつ Gemini に要約させる段。

引用は証拠にしない（U13）
--------------------------------------------------------------------------

要約器は「本文からそのまま」と言われた引用を返すが、**自分で選んだもの**である。
実在しても*一貫性の検査*でしかない。受け取って残すが、**照合済みにはしない**
——照合するのは要約の文そのもので、それは `verify_source` の仕事。

静かに何度も呼ばない
--------------------------------------------------------------------------

**1件につき1回。投げ直さない。** 上限を超える件数は、**呼ぶ前に**止める
——呼んでから気づいても課金は戻らない。

呼び出しは差し込み式
--------------------------------------------------------------------------

`fetch` の `Fetcher` と同じ。プロンプト → `gemini_client.Reply`。
SDK の事情（クライアントの寿命・エラーの訳・キーの伏せ字）は
`common/gemini_client` に閉じ込めたまま、ここには持ち込まない。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from common.gemini_client import Reply
from rank import Scored

#: 要約できなかった理由。**1件につき1つに決める。**
CALL_FAILED = "call_failed"
NOT_FINISHED = "not_finished"
BROKEN_REPLY = "broken_reply"

#: 1回の実行で呼んでよい回数。**`rank` の上限と同じ値**にして、素通りを止める。
MAX_CALLS = 10

#: プロンプト → 答え。本物は `gemini_client.generate_json` を包んだもの。
Caller = Callable[[str], Reply]

#: 答えの型。**自由文にしないのは、空だったことを型で見分けるため**（課題3 と同じ）。
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "quotes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "quotes"],
}


@dataclass(frozen=True)
class Summary:
    scored: Scored
    text: str
    #: **証拠ではない**（U13）。読む人が本文を開いて確かめる手がかり。
    quotes: tuple[str, ...]
    prompt_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class Failure:
    scored: Scored
    reason: str
    detail: str


@dataclass(frozen=True)
class Digest:
    done: tuple[Summary, ...]
    failed: tuple[Failure, ...]

    @property
    def total(self) -> int:
        raise NotImplementedError

    @property
    def ok(self) -> bool:
        raise NotImplementedError

    @property
    def reasons(self) -> Mapping[str, int]:
        raise NotImplementedError

    @property
    def prompt_tokens(self) -> int | None:
        raise NotImplementedError

    @property
    def output_tokens(self) -> int | None:
        raise NotImplementedError

    @property
    def summary(self) -> str:
        raise NotImplementedError


def summarize(
    items: Sequence[Scored],
    *,
    call: Caller,
    max_calls: int = MAX_CALLS,
) -> Digest:
    """**1件につき1回だけ呼ぶ。** できなかったものは理由つきで残す。"""
    raise NotImplementedError
