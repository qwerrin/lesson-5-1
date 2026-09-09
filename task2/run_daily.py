"""定期実行の皮。`to_sheet` を1回まわし、**結果を残し**、必要なら LINE へ知らせる。

なぜ皮が要るのか
------------------------------------------------------------------

`to_sheet.py` は「何が起きたか」を数字の形で作る。しかし**タスクスケジューラは
標準出力を捨てる**（DESIGN 5-V）。定期実行では、その報告は毎回作られて毎回消え、
残るのは ``LastTaskResult`` の数字1つ——中身を持たない数字だけになる。

さらに、値下がりの通知だけを付けると**沈黙が2つの意味を持つ**（DESIGN 5-Y）。
「値下がりが無かった」と「そもそも動いていない」が、同じ無音で届く。

だからここがやるのは3つだけである::

    1. 残す      すべての回を1行の記録にして追記する（5-V）
    2. 知らせる  失敗したとき／値下がったときに送る（5-AA）
    3. 黙らない  週に1度、動いた回数と**書いた行数**を送る（5-Y）

**報告の文字列は読まない。** `to_sheet.execute` が返す `RunResult` をそのまま見る。
文字列から件数を取り出すと、「値下がり 0 件」と「値下がり 10 件」を部分一致で
見分けることになる——照合器で1度踏んだ形である（`README.md` 参照）。

終了コード
------------------------------------------------------------------

::

    0   正常（スキップと「もう走っている」も含む）
    1   書き込みが足りない／読み直しが合わない（`to_sheet` から）
    2   取れなかった商品がある（`to_sheet` から）
    3   **通知を送れなかった**
    4   想定していない失敗で止まった

**3 が 1・2 より強いのは、通知の死のほうが重いから。** 本体が失敗しても通知が
届けば人が気づける。通知が死ぬと、以後の失敗が**全部**無音になる（5-AA）。

**例外を投げ直さない。** ここで投げると、落ちたことがどこにも残らない。
記録してから 4 を返す。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import to_sheet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import env_file, line_auth, line_send  # noqa: E402

#: 記録とロックの置き場。**`.gitignore` に入れてある**（実行の記録は成果物ではない）。
DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_NAME = "run.jsonl"
LOCK_NAME = "run.lock"
#: 生存通知の間隔。**差分でしか鳴らない通知の隣に、必ず鳴る1本を置く**（5-Y）。
HEARTBEAT_DAYS = 7
#: これより古いロックは**置き去り**と見なして奪う。1回の実行は数十秒で終わる。
LOCK_STALE_MINUTES = 30

#: 記録に残る出来事。**「何もしなかった」も残す。**
EVENT_RUN = "run"
EVENT_SKIP = "skip"
EVENT_ERROR = "error"
EVENT_CRASH = "crash"
EVENT_LOCKED = "locked"
EVENT_HEARTBEAT = "heartbeat"
EVENT_NOTIFY_FAILED = "notify_failed"

#: 「動いた」と数える出来事。生存通知そのものは含めない。
_COUNTED = (EVENT_RUN, EVENT_SKIP, EVENT_ERROR, EVENT_CRASH)


@dataclass(frozen=True)
class Notice:
    """送るべき知らせ1本。**宛先は持たない**（送る側が決める）。"""

    kind: str
    text: str


# ------------------------------------------------------------------ 5-V 残す


def append_record(path: str | Path, record: Mapping[str, Any]) -> None:
    """1件を1行として**追記する**。

    上書きにすると「前に何回動いたか」が毎回消える。生存通知（5-Y）は
    この積み重ねだけを根拠にしているので、消えると数えられなくなる。

    ``ensure_ascii=False`` は見た目の好みではない。**人が開いて読めること**が、
    通知が死んだときの最後の手段になる。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False))
        handle.write("\n")


def read_records(path: str | Path) -> list[dict]:
    """読める行だけ返す。**1行の崩れで全部を失わない。**

    書いている途中で電源が落ちれば半端な行が残る。そこで全部を諦めると、
    ロックや生存通知の判断材料まで一緒に消える。
    """
    path = Path(path)
    if not path.exists():
        return []

    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _parse(value: Any) -> datetime | None:
    """記録の時刻。**オフセットが無ければ読まない**（物差しを2本にしない・5-E）。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# ------------------------------------------------------------------ 5-AC ロック


def _stale(path: Path, now: datetime, minutes: int) -> bool:
    """置き去りのロックか。**読めない印を「生きている」と読まない。**

    強制終了・停電・書き途中で落ちた形は、どれもここに来る。
    永久に信じると、**二度と走らなくなる**。
    """
    try:
        written = _parse(path.read_text(encoding="utf-8").strip())
    except OSError:
        return True
    if written is None:
        return True
    return now - written > timedelta(minutes=minutes)


@contextmanager
def lock(
    path: str | Path, *, now: datetime, stale_minutes: int = LOCK_STALE_MINUTES
) -> Iterator[bool]:
    """取れたら ``True``、取れなければ ``False`` を渡す。

    **取れなかったことを例外にしない。** 21時の実行が長引いた所へログオンの実行が
    重なるのは異常ではなく、「もう走っている」だけである（DESIGN 5-AC）。

    **取れなかった側は解放もしない。** 消してしまうと、走っている側のロックを
    外すことになる。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    acquired = False
    try:
        try:
            handle = os.open(path, flags)
        except FileExistsError:
            if not _stale(path, now, stale_minutes):
                yield False
                return
            path.unlink(missing_ok=True)
            try:
                handle = os.open(path, flags)
            except FileExistsError:
                # 奪おうとした隙に別のプロセスが取った。**譲る。**
                yield False
                return
        acquired = True
        with os.fdopen(handle, "w", encoding="utf-8") as opened:
            opened.write(now.isoformat())
        yield True
    finally:
        if acquired:
            path.unlink(missing_ok=True)


# ------------------------------------------------------------------ 記録の形


def record_of(outcome: to_sheet.Outcome, now: datetime) -> dict:
    """1回ぶんを記録の形にする。

    **「走らなかった」を「0件で成功」と書かない。** 結果が無い回は `error` として
    残す。数字が 0 で並ぶ記録と、数字がそもそも無い記録は、別のことを言っている。
    """
    record: dict[str, Any] = {
        "at": now.isoformat(timespec="seconds"),
        "event": EVENT_RUN,
        "exit_code": outcome.exit_code,
        "report": list(outcome.lines),
    }

    result = outcome.result
    if result is None:
        record["event"] = EVENT_ERROR
        return record

    if result.skipped:
        record["event"] = EVENT_SKIP
    record.update(
        {
            "requested": result.requested,
            "fetched_ok": result.fetched_ok,
            "fetched_failed": result.fetched_failed,
            "sent": result.sent,
            "written": result.written,
            "drops": len(result.drops),
        }
    )
    return record


def _when(record: Mapping[str, Any]) -> str:
    """記録の時刻を、**人が読む形**で短く。秒とオフセットは通知には要らない。"""
    return str(record.get("at", ""))[:16].replace("T", " ")


# ------------------------------------------------------------------ 5-AA 通知


def failure_notice(record: Mapping[str, Any]) -> Notice | None:
    """うまくいかなかった回だけ鳴らす。**スキップは鳴らさない。**

    本文に報告を丸ごと入れるのは、「失敗しました」だけでは**何をすればいいか
    分からない**ため。403 の案内（共有先のアドレス）もここに乗って届く。
    """
    # **スキップは失敗ではない。** スキップした回の終了コードは 0 なので、
    # ここで自然に止まる。出来事の名前でも見張ると二重になるが、
    # **その二重をわざと壊しても誰も気づけない**（通る記録が1つも無い）ので置かない。
    code = record.get("exit_code") or 0
    if code == 0:
        return None

    lines = [
        f"⚠ 価格ウォッチャー 終了コード {code}",
        _when(record),
        *[str(line) for line in record.get("report", [])],
    ]
    return Notice("failure", "\n".join(lines))


def drops_notice(result: to_sheet.RunResult | None, now: datetime) -> Notice | None:
    """値下がりを知らせる。**結果そのものから組み立てる**（報告の文字列を読まない）。"""
    if result is None or not result.drops:
        return None

    lines = [f"↓ 値下がり {len(result.drops)} 件（{now.date().isoformat()}）"]
    for comparison in result.drops:
        lines.append(
            f"{comparison.item_code}  "
            f"{comparison.previous_price} → {comparison.current_price} 円"
            f"（{comparison.delta:+} 円・実質）"
        )
    return Notice("drops", "\n".join(lines))


def _last_heartbeat(records: Sequence[Mapping[str, Any]]) -> datetime | None:
    best: datetime | None = None
    for record in records:
        if record.get("event") != EVENT_HEARTBEAT:
            continue
        at = _parse(record.get("at"))
        if at is None:
            continue
        if best is None or at > best:
            best = at
    return best


def heartbeat_notice(
    records: Sequence[Mapping[str, Any]], now: datetime, *, days: int = HEARTBEAT_DAYS
) -> Notice | None:
    """週に1度の生存通知。**無音の意味を1つに減らす。**

    値下がりの通知は差分でしか鳴らない。それだけだと「値下がりが無かった」と
    「経路が死んでいる」が同じ無音になる（DESIGN 5-Y）。

    **回数と行数を両方言う。** 走ったことと、シートに入ったことは別である。
    ``0 回`` のまま届くことが、いちばん知りたい事実になる——**0 を隠さない。**
    """
    last = _last_heartbeat(records)
    if last is not None and now - last < timedelta(days=days):
        return None

    since = now - timedelta(days=days)
    counted = []
    for record in records:
        if record.get("event") not in _COUNTED:
            continue
        at = _parse(record.get("at"))
        if at is None or not (since <= at <= now):
            continue
        counted.append(record)

    written = sum(int(record.get("written") or 0) for record in counted)
    failed = sum(1 for record in counted if (record.get("exit_code") or 0) != 0)
    latest = max((str(record.get("at", "")) for record in counted), default="")

    lines = [
        "価格ウォッチャー 生存確認",
        f"この {days} 日で {len(counted)} 回動いて、{written} 行書きました",
        f"うまくいかなかった回 {failed} 回",
    ]
    if latest:
        lines.append(f"最後に動いたのは {_when({'at': latest})}")
    return Notice("heartbeat", "\n".join(lines))


# ------------------------------------------------------------------ 送る


def send_all(
    notices: Sequence[Notice],
    env: Mapping[str, str],
    *,
    build_session: Callable[[str], Any] = line_auth.build_session,
    push: Callable[..., Any] = line_send.push,
    read_result: Callable[[Any], Any] = line_send.read_send_result,
) -> list[str]:
    """送れなかったものの説明を返す。**例外で落とさない。**

    落とすと、本体の成功まで巻き添えになる。通知が死んでいることは
    **記録して終了コードに乗せる**（DESIGN 5-AA）。

    説明に秘密を入れない。記録は public リポジトリに置くリポジトリの中を通るし、
    **例外の本文にトークンが載ることが実際にある**（401 の応答本文など）。
    """
    notices = list(notices)
    if not notices:
        # **送るものが無いのに認証しない。** 失敗する場所を増やさない。
        return []

    secrets = tuple(str(value) for value in env.values() if value)
    try:
        token = line_auth.read_channel_access_token(env)
        to = line_auth.read_user_id(env)
        session = build_session(token)
    except Exception as error:  # noqa: BLE001
        return [f"通知の準備ができませんでした: {line_auth.redact(str(error), *secrets)}"]

    errors: list[str] = []
    for notice in notices:
        try:
            response = push(
                session, line_send.build_payload(to=to, text=notice.text), secrets=(token,)
            )
            read_result(response)
        except Exception as error:  # noqa: BLE001
            # **1本の失敗で残りを落とさない。** 死んだのは経路ではなく1通かもしれない。
            errors.append(f"{notice.kind}: {line_auth.redact(str(error), *secrets)}")
    return errors


# ------------------------------------------------------------------ 配線


def run(
    *,
    log_path: str | Path,
    now: datetime,
    execute: Callable[[Sequence[str]], to_sheet.Outcome],
    argv: Sequence[str],
    send: Callable[[Sequence[Notice]], list[str]],
    days: int = HEARTBEAT_DAYS,
) -> int:
    """1回ぶん。**残す → 知らせる → 黙らない** の順。

    生存通知を**別に送る**のは、送れたかどうかを1本ずつ確かめるため。
    まとめて送ると「どれが失敗したか」を説明文から読み取ることになり、
    また部分一致に頼る羽目になる。
    """
    try:
        outcome = execute(list(argv))
    except Exception as error:  # noqa: BLE001
        # **投げ直さない。** ここで投げると、落ちたことがどこにも残らない。
        record: dict[str, Any] = {
            "at": now.isoformat(timespec="seconds"),
            "event": EVENT_CRASH,
            "exit_code": 4,
            "report": ["想定していない失敗で止まりました", repr(error)],
        }
        result = None
        code = 4
    else:
        record = record_of(outcome, now)
        result = outcome.result
        code = outcome.exit_code

    append_record(log_path, record)

    notices = [
        notice
        for notice in (failure_notice(record), drops_notice(result, now))
        if notice is not None
    ]
    errors = list(send(notices))

    heartbeat = heartbeat_notice(read_records(log_path), now, days=days)
    if heartbeat is not None:
        heartbeat_errors = list(send([heartbeat]))
        if heartbeat_errors:
            # **送ったことにしない。** 届いていないのに、次の7日も黙ることになる。
            errors.extend(heartbeat_errors)
        else:
            append_record(
                log_path, {"at": now.isoformat(timespec="seconds"), "event": EVENT_HEARTBEAT}
            )

    if errors:
        append_record(
            log_path,
            {
                "at": now.isoformat(timespec="seconds"),
                "event": EVENT_NOTIFY_FAILED,
                "detail": errors,
            },
        )
        return 3
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_daily.py",
        description=(
            "定期実行の皮。to_sheet を1回まわし、結果を残し、"
            "必要なら LINE へ知らせます。"
        ),
    )
    parser.add_argument(
        "--log-dir",
        default=str(DEFAULT_LOG_DIR),
        help=f"記録とロックの置き場（既定: {DEFAULT_LOG_DIR}）",
    )
    parser.add_argument("--env", default=".env", help="資格情報を読む .env の場所")
    parser.add_argument(
        "--sheet-name",
        default=None,
        help=(
            "書き込み先のタブ名。渡さなければ to_sheet.py の既定が効きます。既定値をここに書き写さないのは、2箇所に持つと片方が古くなるためです。"
        ),
    )
    parser.add_argument(
        "--threshold", type=int, default=0,
        help="値下がりを知らせる下げ幅（円）。既定 0 は1円でも知らせる",
    )
    parser.add_argument(
        "--heartbeat-days", type=int, default=HEARTBEAT_DAYS,
        help=f"生存通知の間隔（日・既定 {HEARTBEAT_DAYS}）",
    )
    parser.add_argument(
        "--allow-same-day",
        action="store_true",
        help=(
            "同じ日にすでに取得できた行があっても、もう一度取りに行きます。"
            "既定では何もせずに終わります（同じ日に2行入れると、"
            "次の回の前回値が同じ日の行になるため）。"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = datetime.now().astimezone()
    log_dir = Path(args.log_dir)
    log_path = log_dir / LOG_NAME

    try:
        env = env_file.load(args.env)
    except Exception:  # noqa: BLE001
        # **握りつぶしではない。** 資格情報が読めないことは `send_all` が
        # 「通知の準備ができませんでした」として返し、記録にも終了コードにも残る。
        env = {}

    inner = ["--env", args.env, "--threshold", str(args.threshold)]
    if args.sheet_name:
        inner += ["--sheet-name", args.sheet_name]
    if not args.allow_same_day:
        inner.append("--once-a-day")

    with lock(log_dir / LOCK_NAME, now=now) as acquired:
        if not acquired:
            # **何もしなかったことを、何も残さないで表さない。**
            append_record(
                log_path, {"at": now.isoformat(timespec="seconds"), "event": EVENT_LOCKED}
            )
            return 0
        return run(
            log_path=log_path,
            now=now,
            execute=to_sheet.execute,
            argv=inner,
            send=lambda notices: send_all(notices, env),
            days=args.heartbeat_days,
        )


if __name__ == "__main__":
    raise SystemExit(main())
