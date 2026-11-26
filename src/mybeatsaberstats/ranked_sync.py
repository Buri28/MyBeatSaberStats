"""ランク譜面キャッシュの「全件取り直し」をいつ行うかの判定（ScoreSaber・BeatLeader 共通）。

日付の窓（前回取得の 60 日前まで）だけの差分取得では、窓の外で起きた変化（★・メタ情報の変更、
総数が同じままの入れ替わり）を拾えない。期間を決めた定期の全件取り直しは行わず（1 回に SS 441 ページ＋
BL 40 ページ・約 200 秒かかる一方、窓の外の★がまとめて変わるのは年に数回のため）、次のときだけ全件取り直す。
- 初回：全件取得を一度も最後まで終えていない（{service}_complete が無い）。★のずれの比較の基準日時に使う。
- ★のずれ：最新のスコア取得で見えた★がキャッシュと違う（flag_star_drift で {service}_stars_drift を付ける）。
- 総数の食い違い：キャッシュと API の総数が合わない（mismatch_traversal_allowed）。
PP のずれによる取り直しは collector.py の PP reconcile が別に行う。
全件取得が失敗した直後は ATTEMPT_BACKOFF_HOURS 時間待ってから取り直す（毎回の連打を避ける）。
全件取得しても総数が合わなかった（API の総数が列挙数と合わない等）ときは、同じ総数のうちは繰り返さない。
1 回目は MISMATCH_RETRY_HOURS 時間（一時的な 404 などの可能性）、同じ総数で 2 回続いたら MISMATCH_STUCK_DAYS 日待つ。
記録は ranked_full_traversal.json に残す（service = "scoresaber" / "beatleader"）。
SS（メインスレッド）と BL（裏スレッド）が同じファイルを書くので、読み書きはロックで 1 回にまとめる。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .fileio import atomic_write_text

#: 全件取得が失敗した直後に、取り直しを繰り返さない時間。
ATTEMPT_BACKOFF_HOURS = 1
#: 総数が食い違ったまま終わった全件取得の後（1 回目）、同じ総数でもう一度全件取りしない時間。
MISMATCH_RETRY_HOURS = 24
#: 同じ総数の食い違いが 2 回続いた後、全件取り直しを待つ日数（API 側の総数がずれたままの状態で連打しない）。
MISMATCH_STUCK_DAYS = 14

_LOCK = threading.Lock()


def _read(path: Path | str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _age_hours(data: dict, key: str) -> Optional[float]:
    value = data.get(key)
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600.0


def _blocked_hours(data: dict, service: str) -> float:
    """直前の全件取得の結果から、次の全件取得まで待つ時間（0 = 待たない）。"""
    if data.get(f"{service}_unresolved_total") is not None:
        return MISMATCH_STUCK_DAYS * 24 if int(data.get(f"{service}_unresolved_count") or 1) >= 2 else MISMATCH_RETRY_HOURS
    if data.get(f"{service}_last_ok") is False:
        return ATTEMPT_BACKOFF_HOURS
    return 0


def full_traversal_due(path: Path | str, service: str) -> bool:
    """差分取得の代わりに全件を取り直すか（初回か★のずれがあるとき。期間による定期の取り直しはしない）。"""
    data = _read(path)
    # 全件取得を最後まで終えていて★のずれも無ければ、何日経っても差分取得のまま。
    if data.get(f"{service}_complete") and not data.get(f"{service}_stars_drift"):
        return False
    # 初回・★のずれ：失敗直後などの待ち時間だけは守る。
    attempt = _age_hours(data, service)
    return not (attempt is not None and attempt < _blocked_hours(data, service))


STAR_EPSILON = 0.005


def stars_differ(a, b) -> bool:
    """★が（丸め誤差を超えて）違うか。どちらかが数値でない・0 以下なら比較しない（False）。"""
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return fa > 0 and fb > 0 and abs(fa - fb) > STAR_EPSILON


def flag_star_drift(path: Path | str, service: str) -> None:
    """取得済みの最新の★とランク譜面キャッシュの★が食い違っていた。次の Snapshot で全件を取り直す。"""
    with _LOCK:
        data = _read(path)
        if data.get(f"{service}_stars_drift"):
            return
        data[f"{service}_stars_drift"] = True
        try:
            atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        except Exception:  # noqa: BLE001
            pass


def flag_star_drift_if_any(path: Path | str, service: str, entries) -> bool:
    """entries = (スコア側の★, ランク譜面キャッシュ側の★, プレイ日時 aware datetime or None) の列。

    前回の全件取得より後にプレイしたスコアだけを比べる。そのスコアの★はプレイ時点（最近）のもので、
    ランク譜面キャッシュが古ければ食い違う。全件取得より前のスコアは、スコア側の★が古いだけなので比べない。
    食い違いがあれば flag_star_drift して True を返す。
    """
    complete = _read(path).get(f"{service}_complete")
    try:
        since = datetime.fromisoformat(str(complete).replace("Z", "+00:00")) if complete else None
    except ValueError:
        since = None
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    for score_stars, ranked_stars, played in entries:
        if played is None or (since is not None and played <= since):
            continue
        if stars_differ(score_stars, ranked_stars):
            flag_star_drift(path, service)
            return True
    return False


def mismatch_traversal_allowed(path: Path | str, service: str, api_total: int) -> bool:
    """キャッシュと API の総数が食い違うとき、全件取り直してよいか。"""
    data = _read(path)
    attempt = _age_hours(data, service)
    if attempt is None or attempt >= _blocked_hours(data, service):
        return True
    unresolved = data.get(f"{service}_unresolved_total")
    # 直前が失敗（unresolved なし）なら待つ。食い違いのまま終わった場合は、総数が変わっていれば直ちに取り直す。
    return unresolved is not None and unresolved != api_total


#: ★同期（sync_changed_ranked_scores）で、検索しても目当ての譜面が返らなかったときの印の区切り。
#: 印は「食い違いの理由|not_found:回数@日時」。取り直して食い違いが残った（確認済み）印は理由だけ。
_NOT_FOUND_SEP = "|not_found:"


def _parse_not_found(mark: Optional[str], why: str) -> tuple[int, Optional[datetime]]:
    """mark が why の「見つからなかった」印なら (回数, 日時)。違えば (0, None)。"""
    prefix = why + _NOT_FOUND_SEP
    if not isinstance(mark, str) or not mark.startswith(prefix):
        return 0, None
    count_text, _, when_text = mark[len(prefix):].partition("@")
    try:
        count = max(1, int(count_text))
    except ValueError:
        count = 1
    try:
        when = datetime.fromisoformat(when_text.replace("Z", "+00:00")) if when_text else None
    except ValueError:
        when = None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return count, when


def star_sync_skip(mark: Optional[str], why: str, now: Optional[datetime] = None) -> bool:
    """★同期で、食い違い why の譜面を今回は取り直さないか（印 mark を見る）。

    - 取り直しても同じ食い違いが残った（mark == why）：取り直さない。
    - 検索しても返らなかった（not_found の印）：全件取り直しの総数の食い違いと同じ待ち時間だけ探し直さない。
      1 回目は MISMATCH_RETRY_HOURS 時間、続けて 2 回以上なら MISMATCH_STUCK_DAYS 日。過ぎたらもう一度探す。
    - 食い違いが変わった（why が違う）・印が無い：取り直す。
    """
    if mark is None:
        return False
    if mark == why:
        return True
    count, when = _parse_not_found(mark, why)
    if count == 0 or when is None:
        return False
    wait_hours = MISMATCH_STUCK_DAYS * 24 if count >= 2 else MISMATCH_RETRY_HOURS
    age = ((now or datetime.now(timezone.utc)) - when).total_seconds() / 3600.0
    return 0 <= age < wait_hours


def star_sync_not_found_mark(previous: Optional[str], why: str, now: Optional[datetime] = None) -> str:
    """★同期で検索しても返らなかった譜面の印（star_sync_skip が読む）。続けて返らなければ回数を増やす。"""
    count, _ = _parse_not_found(previous, why)
    when = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{why}{_NOT_FOUND_SEP}{count + 1}@{when}"


def load_newly_ranked_checked(path: Path | str, service: str, player_id: str) -> dict[str, str]:
    """「新しくランクになった譜面」の検知で、取り直してもランクのスコアにならなかった譜面の印を読む。

    戻り値は {leaderboard id: ランク譜面キャッシュ側の印（ランクになった日時）}。印が同じ譜面は再検知しない
    （ランクから外れた譜面がランク譜面キャッシュに残っていると、プレイし直すたびに取り直してしまうため）。
    スコアの譜面情報は書き換えない（ランクでない譜面のスコアを ranked にしてしまうため）。
    """
    data = _read(path)
    entry = (data.get(service) or {}).get(str(player_id)) if isinstance(data.get(service), dict) else None
    if not isinstance(entry, dict):
        return {}
    return {str(k): str(v) for k, v in entry.items()}


def save_newly_ranked_checked(path: Path | str, service: str, player_id: str, marks: dict[str, str]) -> None:
    """load_newly_ranked_checked の印を書く（同じファイルを SS・BL が書くのでロックする）。"""
    with _LOCK:
        data = _read(path)
        per_service = data.get(service) if isinstance(data.get(service), dict) else {}
        per_service[str(player_id)] = {str(k): str(v) for k, v in marks.items()}
        data[service] = per_service
        try:
            atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        except Exception:  # noqa: BLE001
            pass


def record_full_traversal(path: Path | str, service: str, complete: bool, unresolved_total: Optional[int]) -> None:
    """全件取得を試みたことを記録する。

    complete = 最後まで取れて総数も合った。unresolved_total = 一覧は読めたのに総数が合わなかったときの API の総数。
    どちらでもない（失敗）ときは last_ok = False を残して、1 時間後に取り直す。
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _LOCK:
        data = _read(path)
        previous = data.get(f"{service}_unresolved_total")
        data[service] = now
        if complete:
            data[f"{service}_complete"] = now
        data[f"{service}_unresolved_total"] = unresolved_total
        data[f"{service}_unresolved_count"] = (
            (int(data.get(f"{service}_unresolved_count") or 0) + 1) if unresolved_total is not None and previous == unresolved_total
            else (1 if unresolved_total is not None else 0)
        )
        data[f"{service}_last_ok"] = bool(complete or unresolved_total is not None)
        if data[f"{service}_last_ok"]:
            data.pop(f"{service}_stars_drift", None)  # 一覧を最後まで読めたので★は最新になった
        try:
            atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        except Exception:  # noqa: BLE001
            pass
