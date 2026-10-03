"""scout の入口。8段をつないで1回分の実行をする。

接続（Qiita・Gemini・LINE）は**ここでだけ**組み立て、各段には差し込みで渡す。
各段は本物に触らないので、壊れ方は全部テストで通せる。

使い方::

    .venv\\Scripts\\python.exe scout\\cli.py --since 2026-10-01 --inbox <試しの場所> --no-notify
    .venv\\Scripts\\python.exe scout\\cli.py

外へ出る前に止める
--------------------------------------------------------------------------

設定（`config.toml`）と台帳（`state/seen.json`）は、**接続を1つも開く前に**全部確かめる。
ここで止まる回は通知も送れない（宛先が設定の先にある）ので、終了コード 2 で知らせる。
それ以外の失敗——取得元が落ちた・要約できなかった・Inbox に書けなかった——は
**それでも送る**（M7：無音にしない）。

台帳を進める条件
--------------------------------------------------------------------------

- 記事を「見た」にするのは、**`emit` の読み戻しが問題なしのときだけ**。
  書けなかった記事を「見た」にすると二度と来ない（M4）
- 前回の日付は、**取れなかった取得元がある回は進めない**。Qiita の期間指定は日付までなので、
  進めるとその取得元の、その日のぶんを飛ばす（H11）

終了コード
--------------------------------------------------------------------------

====  ===================================================================
0     正常・注意（**注意で毎日赤くしない**。本物の異常に慣れてしまう）
1     異常・LINE に送れなかった・台帳を更新できなかった
2     設定・台帳・資格情報の誤り（外へ出る前に止めた）
====  ===================================================================

見ていないもの
--------------------------------------------------------------------------

- **vault にもうある記事**（`dedupe` の `known`）は空で渡す。H9 は承知で残す
- 途中の段が**想定外の例外**で落ちると、通知まで届かない。タスクスケジューラの終了コードで拾う
- Gemini の呼び出しに timeout を足していない（SDK の設定を確かめていない）
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import requests

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
for _path in (_REPO_ROOT, _HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import dedupe  # noqa: E402
import emit  # noqa: E402
import fetch  # noqa: E402
import notify  # noqa: E402
import rank  # noqa: E402
import split  # noqa: E402
import summarize  # noqa: E402
import verify_source  # noqa: E402
from common import env_file, gemini_client, line_auth, line_send  # noqa: E402

DEFAULT_CONFIG = _HERE / "config.toml"
#: **git では追跡しない**（`.gitignore`）。追跡すると、クローンした人が「他人がもう見た」から始まる。
DEFAULT_STATE = _HERE / "state" / "seen.json"

#: 1回の HTTP 呼び出しの待ち時間（秒）。**`build_session` は timeout を持たない**ので、ここで足す。
TIMEOUT = 30
USER_AGENT = "scout (+https://github.com/qwerrin/lesson-5-1)"

EXIT_OK = 0
EXIT_ABNORMAL = 1
EXIT_CONFIG = 2

_TOP_KEYS = {"inbox", "model", "summarizable", "profile", "sources", "min_body", "max_calls"}
_PROFILE_KEYS = {"tags", "keywords", "mute_tags", "mute_keywords", "cap"}
_SOURCE_KEYS = {"name", "kind", "query", "limit"}
_KINDS = {fetch.QIITA, fetch.ZENN}


class ConfigError(Exception):
    """設定の誤り。**外へ出る前に**止める。"""


class StateError(Exception):
    """台帳が読めない。**空として続けない**——見た記事を全部もう一度送ることになる。"""


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    sources: tuple[fetch.Source, ...]
    summarizable: frozenset[str]
    profile: rank.Profile
    inbox: Path
    model: str
    min_body: int
    max_calls: int


def load_config(path: Path, *, inbox: Path | None = None) -> Config:
    """設定を読んで**全部**確かめる。知らない鍵も誤りにする——書き間違いを黙って捨てない。"""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"設定が無い: {path}") from e
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"設定を読めない（{path}）: {e}") from e

    _only(data, _TOP_KEYS, "設定")
    sources = _sources(data.get("sources"))
    names = {s.name for s in sources}
    summarizable = frozenset(_strings(data.get("summarizable"), "summarizable"))
    if not summarizable:
        raise ConfigError("summarizable が空。1件も要約せずに「異常なし」と答える")
    if not summarizable <= names:
        raise ConfigError(f"summarizable に取得元に無い名前がある: {sorted(summarizable - names)}")

    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError("model が空")

    where = inbox if inbox is not None else _relative(path, data.get("inbox"))
    if not where.is_dir():
        # **作らない。** 作ると、パスの書き間違いが「新しいフォルダに書けた」になる。
        raise ConfigError(f"Inbox が無い: {where}")

    profile = _profile(data.get("profile"))
    max_calls = _positive(data.get("max_calls", summarize.MAX_CALLS), "max_calls")
    if max_calls < profile.cap:
        # `summarize` は上限を超える件数を**呼ぶ前に**例外で止める。実行の途中で落ちると通知まで届かない。
        raise ConfigError(f"max_calls（{max_calls}）が profile.cap（{profile.cap}）より小さい")

    return Config(
        sources=sources,
        summarizable=summarizable,
        profile=profile,
        inbox=where,
        model=model,
        min_body=_positive(data.get("min_body", split.MIN_BODY), "min_body"),
        max_calls=max_calls,
    )


def _sources(value: Any) -> tuple[fetch.Source, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("[[sources]] が1つも無い。何も取らずに「異常なし」と答える")
    sources: list[fetch.Source] = []
    for i, item in enumerate(value):
        where = f"sources[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} が表でない")
        _only(item, _SOURCE_KEYS, where)
        name, kind, query = (_text(item.get(k), f"{where}.{k}") for k in ("name", "kind", "query"))
        if kind not in _KINDS:
            raise ConfigError(f"{where}.kind が知らない種類: {kind}（{sorted(_KINDS)}）")
        sources.append(fetch.Source(name=name, kind=kind, query=query, limit=_positive(item.get("limit"), f"{where}.limit")))
    names = [s.name for s in sources]
    if len(set(names)) != len(names):
        raise ConfigError(f"取得元の名前が重複している: {names}")
    return tuple(sources)


def _profile(value: Any) -> rank.Profile:
    if not isinstance(value, dict):
        raise ConfigError("[profile] が無い")
    _only(value, _PROFILE_KEYS, "profile")
    return rank.Profile(
        tags=frozenset(_strings(value.get("tags"), "profile.tags")),
        keywords=tuple(_strings(value.get("keywords"), "profile.keywords")),
        mute_tags=frozenset(_strings(value.get("mute_tags"), "profile.mute_tags")),
        mute_keywords=tuple(_strings(value.get("mute_keywords"), "profile.mute_keywords")),
        cap=_positive(value.get("cap"), "profile.cap"),
    )


def _only(table: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"{where} に知らない鍵がある: {sorted(unknown)}（書き間違い？）")


def _strings(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} が文字列の一覧でない")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} が空")
    return value


def _positive(value: Any, where: str) -> int:
    # bool は int の仲間なので、素朴な型検査を素通りする。
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where} が正の整数でない: {value!r}")
    return value


def _relative(config_path: Path, value: Any) -> Path:
    """**設定ファイルの場所から**解決する。実行した場所で意味が変わらないように。"""
    raw = Path(_text(value, "inbox"))
    return raw if raw.is_absolute() else (config_path.parent / raw).resolve()


# ---------------------------------------------------------------------------
# 台帳
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class State:
    #: Inbox に**書いて読み戻せた**記事のキー（`dedupe.normalize` の値）。
    seen: frozenset[str]
    #: 次の回の期間の始まり。**取れなかった取得元がある回は進めない。**
    last_run: date | None


def load_state(path: Path) -> State:
    if not path.exists():
        return State(seen=frozenset(), last_run=None)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise StateError(f"台帳を読めない（{path}）: {e}") from e
    if not isinstance(data, dict):
        raise StateError(f"台帳の形が違う（{path}）: 表でない")
    seen = data.get("seen")
    if not isinstance(seen, list) or not all(isinstance(k, str) for k in seen):
        raise StateError(f"台帳の seen が文字列の一覧でない（{path}）")
    raw = data.get("last_run")
    try:
        last_run = None if raw is None else date.fromisoformat(raw)
    except (TypeError, ValueError) as e:
        raise StateError(f"台帳の last_run が日付でない（{path}）: {raw!r}") from e
    return State(seen=frozenset(seen), last_run=last_run)


def save_state(path: Path, state: State) -> None:
    """**一時ファイルに書いてから置き換える。** 途中で落ちても、前の台帳は壊れない。"""
    data = {
        "last_run": None if state.last_run is None else state.last_run.isoformat(),
        # 並べて書く。差分を人が読めるように。
        "seen": sorted(state.seen),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


# ---------------------------------------------------------------------------
# 接続
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Connections:
    get: fetch.Fetcher
    #: `--dry-run` では作らない（鍵を読まない）。
    call: summarize.Caller | None
    session: Any
    to: str
    secrets: tuple[str, ...]


class TimeoutSession:
    """get/post に **timeout を足すだけ**の包み。共有部品の `build_session` は書き換えない。"""

    def __init__(self, inner: Any, *, timeout: float) -> None:
        self._inner = inner
        self._timeout = timeout

    def get(self, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self._timeout)
        return self._inner.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self._timeout)
        return self._inner.post(url, **kwargs)


def http_get(url: str) -> fetch.Response:
    """取得元への GET。**UTF-8 で読む**——`requests` の推測に任せると、文字コードを取り違える。"""
    reply = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    return fetch.Response(
        status=reply.status_code,
        text=reply.content.decode("utf-8", errors="replace"),
        # **`dict` にしない。** `requests` のヘッダは大小を区別しない表で、
        # HTTP/2 では `total-count` と小文字で届く。
        headers=reply.headers,
    )


def gemini_caller(client: Any, *, model: str, api_key: str) -> summarize.Caller:
    return functools.partial(
        _call_gemini, client, model=model, api_key=api_key
    )


def _call_gemini(client: Any, prompt: str, *, model: str, api_key: str) -> gemini_client.Reply:
    # `api_key` は例外の文言からキーを伏せるのに使う。
    return gemini_client.generate_json(
        client, prompt=prompt, schema=summarize.SCHEMA, model=model, api_key=api_key
    )


def connect_real(config: Config, *, remote: bool, env_path: Path = _REPO_ROOT / env_file.ENV_FILENAME) -> Connections:
    """本物の接続。`remote=False`（`--dry-run`）では**鍵を読まない**。"""
    if not remote:
        return Connections(get=http_get, call=None, session=None, to="", secrets=())
    env = env_file.load(env_path)
    api_key = gemini_client.read_api_key(env)
    token = line_auth.read_channel_access_token(env)
    to = line_auth.read_user_id(env)
    return Connections(
        get=http_get,
        call=gemini_caller(gemini_client.build_client(api_key), model=config.model, api_key=api_key),
        session=TimeoutSession(line_auth.build_session(token), timeout=TIMEOUT),
        to=to,
        secrets=(token, api_key),
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qiita・Zenn の記事を集めて Inbox に書き、LINE で知らせる")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="設定（既定: scout/config.toml）")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="台帳（既定: scout/state/seen.json）")
    parser.add_argument("--since", default=None, help="この日以降の記事（YYYY-MM-DD）。**初回は必須**")
    parser.add_argument("--inbox", type=Path, default=None, help="書き込み先を差し替える（試すとき用）")
    parser.add_argument(
        "--dry-run", action="store_true", help="取得から振り分けまでで止める。**Gemini も LINE も書き込みもしない**"
    )
    parser.add_argument("--no-notify", action="store_true", help="本文を表示するだけで LINE に送らない")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    connect: Callable[..., Connections] | None = None,
    now: Callable[[], datetime] = datetime.now,
) -> int:
    args = build_parser().parse_args(argv)

    # ---- 外へ出る前に止める
    try:
        config = load_config(args.config, inbox=args.inbox)
        state = load_state(args.state)
    except (ConfigError, StateError) as e:
        print(e, file=sys.stderr)
        return EXIT_CONFIG
    try:
        since = date.fromisoformat(args.since) if args.since is not None else state.last_run
    except ValueError:
        print(f"--since は YYYY-MM-DD で: {args.since}", file=sys.stderr)
        return EXIT_CONFIG
    if since is None:
        print("初回は --since YYYY-MM-DD が要る（台帳に前回の日付が無い）", file=sys.stderr)
        return EXIT_CONFIG

    # **時刻は1回だけ取る。** run-id にも本文にも同じ値を使う（送り直しで2通にしない）。
    at = now()
    run_id = f"{at:%Y%m%d-%H%M%S}"
    try:
        conn = (connect or connect_real)(config, remote=not args.dry_run)
    except (gemini_client.GeminiError, line_auth.LineError, OSError) as e:
        print(f"接続を組み立てられない: {e}", file=sys.stderr)
        return EXIT_CONFIG

    # ---- 取得から振り分けまで
    harvest = fetch.harvest(config.sources, conn.get, since=datetime.combine(since, time()))
    sifted = dedupe.sift(harvest.articles, seen=state.seen)
    ranking = rank.rank(sifted.kept, config.profile)
    parts = split.split(ranking, summarizable=config.summarizable, min_body=config.min_body)
    stages = [
        f"fetch: {_fetch_line(harvest)}",
        f"dedupe: {sifted.summary}",
        f"rank: {ranking.summary}",
        f"split: {parts.summary}",
    ]
    if args.dry_run:
        print("\n".join(stages))
        print("--dry-run なので、要約・書き込み・台帳・通知はしていない")
        return EXIT_OK

    # ---- 要約・照合・書き込み
    if conn.call is None:
        raise RuntimeError("要約の接続が無い（remote=True で組み立てたはず）")
    digest = summarize.summarize(parts.summarize, call=conn.call, max_calls=config.max_calls)
    audit = verify_source.verify(digest.done)
    stages += [f"summarize: {digest.summary}", f"verify: {audit.summary}"]

    emitted: emit.Emitted | None = None
    emit_error: str | None = None
    try:
        emitted = emit.emit(
            config.inbox, at=at, run_id=run_id, stages=tuple(stages), split=parts, digest=digest, audit=audit
        )
    except (ValueError, OSError) as e:
        # **書けなかった回も送る。** 例外のまま抜けると、通知まで届かない。
        emit_error = f"{type(e).__name__}: {e}"

    ledger_failed = False
    if emitted is not None and emitted.ok:
        written = {s.kept.key for s in parts.summarize} | {h.kept.key for h in parts.headline}
        # **取れなかった取得元がある回は、前回の日付を進めない。**
        advance = harvest.status != fetch.FAILED
        new_state = State(seen=state.seen | written, last_run=at.date() if advance else state.last_run)
        try:
            save_state(args.state, new_state)
            stages.append(f"台帳: {len(written)} 件を足した（前回の日付は {new_state.last_run}）")
        except OSError as e:
            ledger_failed = True
            stages.append(f"台帳: 更新できなかった（{type(e).__name__}: {e}）")
    else:
        stages.append("台帳: 更新しなかった（Inbox に書けていないので、記事を「見た」にしない）")

    # ---- 通知
    health = notify.judge(harvest=harvest, digest=digest, audit=audit, emitted=emitted, emit_error=emit_error)
    body = notify.compose(
        at=at, run_id=run_id, health=health, stages=stages, emitted=emitted, emit_error=emit_error
    )
    print(line_auth.redact(body, *conn.secrets))
    print("-" * 60)

    sent = True
    if args.no_notify:
        print("--no-notify なので、LINE には送っていない")
    else:
        try:
            delivered = notify.send(conn.session, to=conn.to, body=body, run_id=run_id, secrets=conn.secrets)
            print(delivered.summary)
        except (line_auth.LineError, line_send.SendError) as e:
            sent = False
            # 秘密は `notify.send` が投げる時点で伏せてある（push の失敗も通信の失敗も）。
            print(f"LINE へ送れなかった: {e}", file=sys.stderr)

    if health.level == notify.ABNORMAL or not sent or ledger_failed:
        return EXIT_ABNORMAL
    return EXIT_OK


def _fetch_line(harvest: fetch.Harvest) -> str:
    parts = []
    for result in harvest.results:
        text = f"{result.source} {result.status} {len(result.articles)} 件"
        if result.detail:
            text += f"（{result.detail}）"
        if result.more:
            text += "（まだ先がある）"
        parts.append(text)
    return "・".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
