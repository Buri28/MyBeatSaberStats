"""ランク譜面キャッシュの「全件取り直し」をいつ行うかの判定（ScoreSaber・BeatLeader 共通）。

日付の窓（前回取得の 60 日前まで）だけの差分取得では、窓の外で起きた変化（★・メタ情報の変更、
総数が同じままの入れ替わり）を拾えない。そこで次の 3 つで全件取り直す。
- 定期：前回「最後まで取れた」全件取得から PERIODIC_FULL_DAYS 日経った。
- 総数の食い違い：キャッシュと API の総数が合わない。直前の全件取得が同じ総数で食い違ったまま終わったときだけ、
  MISMATCH_RETRY_HOURS 時間は繰り返さない（API の総数が実際の列挙数と合わないとき、毎回全件になるのを防ぐ）。
- 失敗後：全件取得が途中で失敗したときは ATTEMPT_BACKOFF_HOURS 時間後に取り直す（毎回の連打を避ける）。
記録は ranked_full_traversal.json に残す（service = "scoresaber" / "beatleader"）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .fileio import atomic_write_text, mark_now, recently_marked

#: 最後まで取れた全件取得から、これだけ経ったら定期の全件取り直し（★・メタ情報の変更と入れ替わりを拾う）。
PERIODIC_FULL_DAYS = 14
#: 全件取得を試みた（失敗も含む）直後に、定期の全件取り直しを繰り返さない時間。
ATTEMPT_BACKOFF_HOURS = 1
#: 総数が食い違ったまま終わった全件取得の後、同じ総数でもう一度全件取りしない時間。
MISMATCH_RETRY_HOURS = 24


def periodic_full_due(path: Path | str, service: str) -> bool:
    """定期の全件取り直しの時期か。"""
    if recently_marked(path, f"{service}_complete", PERIODIC_FULL_DAYS * 24):
        return False
    return not recently_marked(path, service, ATTEMPT_BACKOFF_HOURS)


def mismatch_traversal_allowed(path: Path | str, service: str, api_total: int) -> bool:
    """キャッシュと API の総数が食い違うとき、全件取り直してよいか。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:  # noqa: BLE001
        data = {}
    if data.get(f"{service}_last_ok") is False and recently_marked(path, service, ATTEMPT_BACKOFF_HOURS):
        return False  # 直前の全件取得が失敗した。連打しない
    if not recently_marked(path, service, MISMATCH_RETRY_HOURS):
        return True
    return data.get(f"{service}_unresolved_total") != api_total


def record_full_traversal(path: Path | str, service: str, complete: bool, unresolved_total: Optional[int]) -> None:
    """全件取得を試みたことを記録する。complete = 最後まで取れた。unresolved_total = 取れたのに総数が合わなかったときの API の総数。"""
    mark_now(path, service)
    if complete:
        mark_now(path, f"{service}_complete")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:  # noqa: BLE001
        data = {}
    data[f"{service}_unresolved_total"] = unresolved_total
    data[f"{service}_last_ok"] = bool(complete)
    try:
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:  # noqa: BLE001
        pass
