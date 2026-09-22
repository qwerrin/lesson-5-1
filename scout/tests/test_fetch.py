"""scout/fetch のテスト。**実装より先に書いた。**

`fetch` は取得元から記事の一覧を取る段。**ここで守るのは「取れなかったことを、
無かったことにしない」**の1点に尽きる。

============ ====================================================================
根拠         ここで守ること
============ ====================================================================
H2           **0件と「取れなかった」を同じ顔にしない**（HTTP 200 ＋ 空配列）
H3           公開日と取得日は別物。公開日は取得元が言うほうを使う
H6           **日付の物差しは1本**。Qiita は ISO 8601、Zenn は RFC822 の GMT
H11          Qiita の `created:>=` は**日付までで時刻が無い**。同日の再取得は必ず起きる
H13          Qiita は `page` ≤100 × `per_page` ≤100
M1           レート制限で切れたのに、取れたぶんだけで「完了」にしない
M2           **取得元1つが死んでも、全体を成功にしない**
M3           認証切れが 401 ではなく**空配列**で返る提供元がある
M9           取得元が空のまま「うまくいった」と言わない
============ ====================================================================

取ってよい根拠は `sources.yaml`（`DESIGN.md` 2-1）にあり、**ここでは扱わない**。
`fetch` は渡された取得元を、渡された上限の範囲で取るだけ。

HTTP は差し込み式
--------------------------------------------------------------------------

`guard` の `ocr`・`verify_figs` の `reader` と同じ。
**本物のネットワークに触らずに、取得元ごとの壊れ方を全部通せる。**
本物を1回叩く経路は `--probe` として別に持つ（`DESIGN.md` 8-1）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scout"))

import fetch  # noqa: E402


def _qiita(source: str = "qiita", query: str = "tag:ClaudeCode", limit: int = 20) -> fetch.Source:
    return fetch.Source(name=source, kind=fetch.QIITA, query=query, limit=limit)


def _zenn(source: str = "zenn", query: str = "claudecode", limit: int = 20) -> fetch.Source:
    return fetch.Source(name=source, kind=fetch.ZENN, query=query, limit=limit)


QIITA_ITEM = {
    "title": "Claude Code で図版を作る",
    "url": "https://qiita.com/someone/items/abc123",
    "body": "# 見出し\n本文がここに入る",
    "created_at": "2026-09-20T12:34:56+09:00",
    "updated_at": "2026-09-21T00:00:00+09:00",
    "tags": [{"name": "ClaudeCode"}, {"name": "Python"}],
    "likes_count": 12,
    "stocks_count": 3,
    "user": {"id": "someone"},
}

ZENN_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Claude Code にrm -rfを打たせない</title>
    <description>冒頭の300文字だけがここに入る</description>
    <link>https://zenn.dev/someone/articles/xyz</link>
    <guid>https://zenn.dev/someone/articles/xyz</guid>
    <pubDate>Sat, 19 Sep 2026 08:26:20 GMT</pubDate>
    <dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">someone</dc:creator>
  </item>
</channel></rss>
"""


def _reply(text: str, status: int = 200, headers: dict[str, str] | None = None) -> fetch.Response:
    return fetch.Response(status=status, text=text, headers=headers or {})


def _get(mapping: dict[str, fetch.Response]) -> fetch.Fetcher:
    """URL の**中身では分岐しない**。呼ばれた URL をそのまま鍵にする。"""

    def getter(url: str) -> fetch.Response:
        for key, reply in mapping.items():
            if key in url:
                return reply
        raise AssertionError(f"知らない URL を叩いた: {url}")

    return getter


def _both(qiita_items: list[dict] | None = None, zenn: str = ZENN_FEED) -> fetch.Fetcher:
    items = QIITA_ITEM if qiita_items is None else qiita_items
    payload = json.dumps([items] if isinstance(items, dict) else items, ensure_ascii=False)
    return _get({"qiita.com": _reply(payload), "zenn.dev": _reply(zenn)})


# --------------------------------------------------------------------------
# 取得元の指定（M9）
# --------------------------------------------------------------------------


def test_取得元を1つも渡さなければ作れない() -> None:
    """**空で回すと、何も取らずに「異常なし」と答える。**"""
    with pytest.raises(ValueError):
        fetch.harvest((), _both())


def test_取得元を渡さずには呼べない() -> None:
    with pytest.raises(TypeError):
        fetch.harvest()  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# Qiita
# --------------------------------------------------------------------------


def test_Qiitaの記事を読む() -> None:
    got = fetch.harvest((_qiita(),), _both())
    article = got.articles[0]

    assert article.source == "qiita"
    assert article.title == "Claude Code で図版を作る"
    assert article.url == "https://qiita.com/someone/items/abc123"
    assert article.author == "someone"
    assert article.tags == ("ClaudeCode", "Python")
    assert article.metrics["likes"] == 12


def test_Qiitaは本文を持つ() -> None:
    """**本文があるかどうかが、後段の分かれ目**（`split` が見る）。"""
    got = fetch.harvest((_qiita(),), _both())

    assert got.articles[0].body == "# 見出し\n本文がここに入る"
    assert got.articles[0].has_body is True


def test_本文が空文字なら本文なし扱い() -> None:
    """**空を本文として通さない。** `None` と空文字を分けても、後段には同じ害になる。

    `split` は「本文がある側」を要約へ送る。空文字を通すと、
    *中身の無い本文で要約を作り、ソース側の照合が0件で「一致」になる*。
    """
    empty = {**QIITA_ITEM, "body": ""}
    got = fetch.harvest((_qiita(),), _get({"qiita.com": _reply(json.dumps([empty]))}))

    assert got.articles[0].has_body is False


def test_配列でない応答を0件にしない() -> None:
    """Qiita はエラーを `{"message": ..., "type": ...}` で返す。

    **形が違うものを、黙って0件として通さない。**

    **`status` だけ見ると弱い。** 形を見ずに回しても、辞書のキーを記事として
    読もうとして `TypeError` で落ち、結局 `FAILED` にはなる
    ——*同じ結論に、読めない理由で辿り着く*。
    だから **`detail` が何が起きたかを言っていること**まで見る。
    """
    payload = json.dumps({"message": "Not found", "type": "not_found"})
    got = fetch.harvest((_qiita(),), _get({"qiita.com": _reply(payload)}))

    assert got.results[0].status == fetch.FAILED
    assert "配列" in got.results[0].detail


def test_Qiitaの日時をローカルの素の日時に揃える() -> None:
    """**物差しを1本にする**（教訓 `one-date-basis-per-output`）。"""
    got = fetch.harvest((_qiita(),), _both())
    expected = datetime(2026, 9, 20, 12, 34, 56, tzinfo=timezone(timedelta(hours=9)))

    assert got.articles[0].published_at == expected.astimezone().replace(tzinfo=None)
    assert got.articles[0].published_at.tzinfo is None


def test_Qiitaの公開日と更新日を別に持つ() -> None:
    """**H3：公開日と更新日は別物。** 古い記事が更新で「新着」に化ける。"""
    got = fetch.harvest((_qiita(),), _both())
    article = got.articles[0]

    assert article.updated_at is not None
    assert article.updated_at > article.published_at


def test_検索の期間は日付までしか指定できない() -> None:
    """**H11：`created:>=` に時刻が無い。** 同日の再取得は必ず起きるので、

    *`dedupe` は任意の最適化ではなく必須の部品*になる。
    """
    seen: list[str] = []

    def watcher(url: str) -> fetch.Response:
        seen.append(url)
        return _reply(json.dumps([QIITA_ITEM], ensure_ascii=False))

    fetch.harvest((_qiita(),), watcher, since=datetime(2026, 9, 20, 23, 59, 59))
    query = parse_qs(urlparse(seen[0]).query)["query"][0]

    assert "created:>=2026-09-20" in query
    assert "23:59" not in query


def test_1回あたりの件数は上限100を超えない() -> None:
    """**H13：`per_page` の上限は 100。** 超える値を送っても意味が無い。"""
    seen: list[str] = []

    def watcher(url: str) -> fetch.Response:
        seen.append(url)
        return _reply("[]")

    fetch.harvest((_qiita(limit=500),), watcher)
    per_page = parse_qs(urlparse(seen[0]).query)["per_page"][0]

    assert per_page == "100"


def test_まだ先があることを記録する() -> None:
    """**H1：検索は見つかったものしか返さない。** 打ち切りを黙らない。"""
    got = fetch.harvest(
        (_qiita(limit=1),),
        _get({"qiita.com": _reply(json.dumps([QIITA_ITEM]), headers={"Total-Count": "57"})}),
    )

    assert got.results[0].more is True
    assert got.results[0].total == 57


def test_全部取れていれば先は無い() -> None:
    got = fetch.harvest(
        (_qiita(limit=20),),
        _get({"qiita.com": _reply(json.dumps([QIITA_ITEM]), headers={"Total-Count": "1"})}),
    )

    assert got.results[0].more is False


def test_件数の総数が分からないことを隠さない() -> None:
    """`Total-Count` が返らない取得元もある。**0 と混ぜない。**"""
    got = fetch.harvest((_qiita(),), _get({"qiita.com": _reply(json.dumps([QIITA_ITEM]))}))

    assert got.results[0].total is None


# --------------------------------------------------------------------------
# Zenn
# --------------------------------------------------------------------------


def test_Zennの記事を読む() -> None:
    got = fetch.harvest((_zenn(),), _both())
    article = got.articles[0]

    assert article.source == "zenn"
    assert article.title == "Claude Code にrm -rfを打たせない"
    assert article.url == "https://zenn.dev/someone/articles/xyz"
    assert article.author == "someone"


def test_Zennは本文を持たない() -> None:
    """**フィードの `description` は 300字で打ち切られる**（2026-09-19 実測）。

    *一部だけの本文を「本文」と呼ぶと、要約の物差しにしてしまう。*
    だから `None` にする——**短い本文と、切られた本文を、長さでは区別できない。**
    """
    got = fetch.harvest((_zenn(),), _both())

    assert got.articles[0].body is None
    assert got.articles[0].has_body is False


def test_ZennのGMTをローカルの素の日時に揃える() -> None:
    got = fetch.harvest((_zenn(),), _both())
    expected = datetime(2026, 9, 19, 8, 26, 20, tzinfo=timezone.utc)

    assert got.articles[0].published_at == expected.astimezone().replace(tzinfo=None)
    assert got.articles[0].published_at.tzinfo is None


def test_Zennはトピックのフィードを叩く() -> None:
    seen: list[str] = []

    def watcher(url: str) -> fetch.Response:
        seen.append(url)
        return _reply(ZENN_FEED)

    fetch.harvest((_zenn(query="claudecode"),), watcher)

    assert seen == ["https://zenn.dev/topics/claudecode/feed"]


# --------------------------------------------------------------------------
# 取れなかったこと（H2・M2・M3）
# --------------------------------------------------------------------------


def test_0件は取れなかったことではない() -> None:
    """**★ H2：HTTP 200 ＋ 空配列。** 提供側は「無い」を成功として返す。"""
    got = fetch.harvest((_qiita(),), _get({"qiita.com": _reply("[]")}))

    assert got.results[0].status == fetch.EMPTY
    assert got.results[0].articles == ()


def test_取れなかったことを0件にしない() -> None:
    got = fetch.harvest((_qiita(),), _get({"qiita.com": _reply("", status=500)}))

    assert got.results[0].status == fetch.FAILED
    assert "500" in got.results[0].detail


def test_レート制限を失敗として扱う() -> None:
    """**M1：429 で切れたのに、取れたぶんで「完了」にしない。**"""
    got = fetch.harvest((_qiita(),), _get({"qiita.com": _reply("", status=429)}))

    assert got.results[0].status == fetch.FAILED
    assert "429" in got.results[0].detail


def test_壊れたJSONを0件にしない() -> None:
    got = fetch.harvest((_qiita(),), _get({"qiita.com": _reply("{壊れている")}))

    assert got.results[0].status == fetch.FAILED


def test_壊れたXMLを0件にしない() -> None:
    got = fetch.harvest((_zenn(),), _get({"zenn.dev": _reply("<rss><これは壊れている")}))

    assert got.results[0].status == fetch.FAILED


def test_取得の途中で落ちても他の取得元は取る() -> None:
    """**取得元ごとに成否を持つ。** 片方が死んでももう片方は使える。"""
    getter = _get(
        {"qiita.com": _reply("", status=500), "zenn.dev": _reply(ZENN_FEED)}
    )
    got = fetch.harvest((_qiita(), _zenn()), getter)

    statuses = {r.source: r.status for r in got.results}
    assert statuses == {"qiita": fetch.FAILED, "zenn": fetch.OK}
    assert len(got.articles) == 1


def test_1つでも死んだら全体を成功にしない() -> None:
    """**★ M2：他が生きていれば全体は成功に見える。**"""
    getter = _get(
        {"qiita.com": _reply("", status=500), "zenn.dev": _reply(ZENN_FEED)}
    )
    got = fetch.harvest((_qiita(), _zenn()), getter)

    assert got.status == fetch.FAILED


def test_全部空なら全体も空() -> None:
    getter = _get({"qiita.com": _reply("[]"), "zenn.dev": _reply("<rss><channel></channel></rss>")})
    got = fetch.harvest((_qiita(), _zenn()), getter)

    assert got.status == fetch.EMPTY


def test_1件でも取れれば全体は成功() -> None:
    getter = _get({"qiita.com": _reply("[]"), "zenn.dev": _reply(ZENN_FEED)})
    got = fetch.harvest((_qiita(), _zenn()), getter)

    assert got.status == fetch.OK


def test_例外で落ちても取得元の失敗として残す() -> None:
    """**繋がらなかったことを、この段で握りつぶさない。**

    握れば「0件」になり、握らなければ*他の取得元まで道連れ*になる。
    取得元ごとの失敗として記録して、**続ける**。
    """

    def broken(url: str) -> fetch.Response:
        if "qiita.com" in url:
            raise OSError("名前解決に失敗しました")
        return _reply(ZENN_FEED)

    got = fetch.harvest((_qiita(), _zenn()), broken)

    assert got.results[0].status == fetch.FAILED
    assert "名前解決" in got.results[0].detail
    assert got.results[1].status == fetch.OK


def test_取得元の順に結果を並べる() -> None:
    """**渡した順で返す。** 並び替えると、どれが落ちたかを追いにくくなる。"""
    got = fetch.harvest((_zenn(), _qiita()), _both())

    assert [r.source for r in got.results] == ["zenn", "qiita"]
