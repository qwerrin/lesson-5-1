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

import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from common.gemini_client import GeminiError, Reply
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
    #: **答えが返ったなら課金されている。** 要約に使わなくても数は残す。
    #: 例外で終わった呼び出し（`CALL_FAILED`）は答えが無いので `None`。
    prompt_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class Digest:
    done: tuple[Summary, ...]
    failed: tuple[Failure, ...]

    @property
    def total(self) -> int:
        return len(self.done) + len(self.failed)

    @property
    def ok(self) -> bool:
        """**1件でもできなかったら、全体は成功にしない**（M2）。0件は失敗ではない。"""
        return not self.failed

    @property
    def reasons(self) -> Mapping[str, int]:
        return dict(Counter(f.reason for f in self.failed))

    @property
    def prompt_tokens(self) -> int | None:
        """**答えが返った呼び出し**のトークン。要約に使えなかったものも含める。

        例外で終わった呼び出しは数を持たないので入らない——*件数は `call_failed` に出る*。
        """
        return _sum(item.prompt_tokens for item in self._answered())

    @property
    def output_tokens(self) -> int | None:
        return _sum(item.output_tokens for item in self._answered())

    def _answered(self) -> list[Summary | Failure]:
        return [*self.done, *(f for f in self.failed if f.reason != CALL_FAILED)]

    @property
    def summary(self) -> str:
        """**件数を必ず出す。** できなかったぶんの理由も数える。"""
        breakdown = "・".join(f"{r} {n}" for r, n in sorted(self.reasons.items()))
        return (
            f"{self.total} 件中 {len(self.done)} 件を要約した"
            f"／できなかった {len(self.failed)} 件（{breakdown or 'なし'}）"
        )


#: 壊れた答えの原文を、どこまで記録に残すか。**全部は残さない**——記録が読めなくなる。
DETAIL_LIMIT = 500


def summarize(
    items: Sequence[Scored],
    *,
    call: Caller,
    max_calls: int = MAX_CALLS,
) -> Digest:
    """**1件につき1回だけ呼ぶ。** できなかったものは理由つきで残す。"""
    if len(items) > max_calls:
        # **呼ぶ前に止める。** 呼んでから気づいても、課金は戻らない。
        raise ValueError(
            f"要約する記事が {len(items)} 件で、1回の上限 {max_calls} 件を超えている。"
            "`rank` の上限を素通りした入力の可能性がある"
        )

    done: list[Summary] = []
    failed: list[Failure] = []

    for scored in items:
        try:
            reply = call(_prompt(scored))
        except GeminiError as error:
            # **握るのは GeminiError だけ。** こちらのバグを API の失敗に化けさせない。
            # 文言は `gemini_client` が伏せ字を通したもの。
            failed.append(Failure(scored=scored, reason=CALL_FAILED, detail=str(error)))
            continue

        if reply.finish_reason != "STOP":
            # **打ち切られても本文は返る。** 「見られなかった」も STOP と混ぜない。
            failed.append(
                Failure(
                    scored=scored,
                    reason=NOT_FINISHED,
                    detail=f"finish_reason={reply.finish_reason}",
                    prompt_tokens=reply.prompt_tokens,
                    output_tokens=reply.output_tokens,
                )
            )
            continue

        try:
            text, quotes = _parse(reply.text)
        except ValueError as error:
            failed.append(
                Failure(
                    scored=scored,
                    reason=BROKEN_REPLY,
                    detail=f"{error}／原文: {reply.text[:DETAIL_LIMIT]}",
                    prompt_tokens=reply.prompt_tokens,
                    output_tokens=reply.output_tokens,
                )
            )
            continue

        done.append(
            Summary(
                scored=scored,
                text=text,
                quotes=quotes,
                prompt_tokens=reply.prompt_tokens,
                output_tokens=reply.output_tokens,
            )
        )

    return Digest(done=tuple(done), failed=tuple(failed))


def _prompt(scored: Scored) -> str:
    """**本文は切らずに渡す**（U4：p95 の 22017字でも入力 7133 トークン）。

    切ると、*切った先に書かれていた主張が照合できなくなる*。
    """
    article = scored.kept.article
    return (
        "次の技術記事を日本語で3〜5文に要約してください。\n"
        "quotes には、要約の根拠にした本文の一節を3〜5個、**本文からそのまま**抜き出してください。"
        "言い換え・省略・記号の変更をしないこと。\n\n"
        f"# {article.title}\n\n{article.body}"
    )


def _parse(raw: str) -> tuple[str, tuple[str, ...]]:
    """**解釈できないものを、空の要約として先へ流さない。**"""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON として読めない（{error.msg}）") from error
    if not isinstance(payload, dict):
        raise ValueError("オブジェクトではない")

    text = payload.get("summary")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("要約が無い・空・文字列でない")

    quotes = payload.get("quotes", [])
    # 引用は**証拠ではない**ので、無いことは失敗にしない（U13）。形が違うことは失敗にする。
    if not isinstance(quotes, list) or not all(isinstance(q, str) for q in quotes):
        raise ValueError("引用が文字列の配列でない")

    return text.strip(), tuple(quotes)


def _sum(values: Iterable[int | None]) -> int | None:
    """**分からないことを 0 と混ぜない。** 混ぜると、*実際より安く見える*。"""
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total
