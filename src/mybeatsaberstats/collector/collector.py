
from __future__ import annotations
def _is_steam_id(value: str | None) -> bool:
    return isinstance(value, str) and value.isdigit() and len(value) == 17

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import json
import threading
from pathlib import Path
from typing import Optional, Dict, TypedDict, Callable

import math
from collections import defaultdict

import requests

from ..snapshot import BASE_DIR, SNAPSHOT_DIR, StarClearStat, Snapshot
from ..scoresaber import ScoreSaberPlayer
from .scoresaber import _collect_star_stats_from_scoresaber
from .scoresaber import _get_scoresaber_player_scores
from .scoresaber import _get_scoresaber_leaderboards_ranked
from .scoresaber import _get_scoresaber_player_stats
from .scoresaber import _fetch_scoresaber_player_basic
from .scoresaber import _load_cached_pages, _save_cached_pages
from .beatleader import (
    _get_beatleader_player_scores as _bl_get_beatleader_player_scores,
    _get_beatleader_leaderboards_ranked as _bl_get_beatleader_leaderboards_ranked,
    collect_beatleader_star_stats as _bl_collect_beatleader_star_stats,
    collect_beatleader_star_stats_from_cache as _bl_collect_beatleader_star_stats_from_cache,
)
from ..beatleader import BeatLeaderPlayer, fetch_player as fetch_bl_player
from .map_store import MapStore

from ..accsaber_reloaded import fetch_player_all_categories as _fetch_accsaber_reloaded
from ..accsaber_reloaded import fetch_player_xp as _fetch_accsaber_reloaded_xp
from ..accsaber_reloaded import fetch_player_milestone_counts as _fetch_accsaber_reloaded_milestones
from ..accsaber_reloaded import fetch_reloaded_map_counts as _fetch_reloaded_map_counts
from ..accsaber_reloaded import fetch_and_save_all_maps_cache as _fetch_and_save_rl_maps
from ..accsaber_reloaded import fetch_and_save_player_scores_cache as _fetch_and_save_rl_player_scores
from ..accsaber_reloaded import compute_effective_played_counts_from_cache as _compute_rl_effective_played_counts

# キャッシュディレクトリ(app.py と同じ BASE_DIR / "cache" を利用)
CACHE_DIR = BASE_DIR / "cache"
_SCORE_RECONCILE_STATE_PATH = CACHE_DIR / "score_reconcile_state.json"
_SCORE_BACKFILL_DAYS = 60
_PP_RECONCILE_THRESHOLD = 1.0


@dataclass
class SnapshotOptions:
    """スナップショット取得時に各データソースの取得可否を制御するオプション。

    デフォルトはすべて True（全データを取得）。
    False にすると対応するステップをスキップし、既存キャッシュのデータをそのまま使用する。

    ss_fetch_until / bl_fetch_until を指定すると、それより古い日時のスコアまで遡って取得する。
    None の場合はキャッシュの最新 timeSet に達した時点で差分取得を終了する（通常動作）。
    """
    fetch_ss_ranked_maps: bool = True    # ScoreSaber Ranked Maps
    fetch_bl_ranked_maps: bool = True    # BeatLeader Ranked Maps
    fetch_scoresaber: bool = True        # ScoreSaber プレイヤー情報・スコア・統計
    fetch_beatleader: bool = True        # BeatLeader プレイヤー情報・スコア・統計
    fetch_accsaber_reloaded: bool = True # AccSaber (Reloaded) ランク情報
    fetch_ss_star_stats: bool = True     # ScoreSaber ★別クリア統計
    fetch_bl_star_stats: bool = True     # BeatLeader ★別クリア統計
    ss_fetch_until: Optional[datetime] = None  # ScoreSaber スコア取得の遡り期限 (None=自動)
    bl_fetch_until: Optional[datetime] = None  # BeatLeader スコア取得の遡り期限 (None=自動)
    ss_ranked_until: Optional[datetime] = None  # ScoreSaber Ranked Maps 取得の遡り期限 (None=自動)
    bl_ranked_until: Optional[datetime] = None  # BeatLeader Ranked Maps 取得の遡り期限 (None=自動)
    ss_fetch_all: bool = False  # ScoreSaber: 全スコアを最初から再取得 (キャッシュ差分を無視)
    bl_fetch_all: bool = False  # BeatLeader: 全スコアを最初から再取得 (キャッシュ差分を無視)


# SCORESABER_LEADERBOARDS_URL = "https://scoresaber.com/api/leaderboards"
# SCORESABER_PLAYER_SCORES_URL = "https://scoresaber.com/api/player/{player_id}/scores"
# SCORESABER_PLAYER_FULL_URL = "https://scoresaber.com/api/player/{player_id}/full"

BEATLEADER_LEADERBOARDS_URL = "https://api.beatleader.xyz/leaderboards"
BL_BASE_URL = "https://api.beatleader.xyz"


def _read_cache_fetched_at(path: Path) -> Optional[datetime]:
    """キャッシュ JSON の fetched_at フィールドを UTC datetime として返す。

    ファイルが存在しない・読めない・フィールドがない場合は None を返す。
    旧形式の plain list ファイルはファイルの修正時刻を UTC datetime として返す。
    """
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            fa = raw.get("fetched_at")
            if isinstance(fa, str) and fa:
                return datetime.fromisoformat(fa.rstrip("Z"))
        elif isinstance(raw, list) and raw:
            # 旧形式（plain list）: ファイルの修正時刻を返す
            return datetime.utcfromtimestamp(path.stat().st_mtime)
    except Exception:  # noqa: BLE001
        pass
    return None


def _dt_to_utc_z(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat() + "Z"


def _parse_utc_z(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z"))
    except ValueError:
        return None


def _load_score_reconcile_state() -> dict:
    if not _SCORE_RECONCILE_STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(_SCORE_RECONCILE_STATE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_score_reconcile_state(state: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _SCORE_RECONCILE_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _get_service_reconcile_state(service: str, steam_id: str) -> dict:
    state = _load_score_reconcile_state()
    service_state = state.get(service)
    if not isinstance(service_state, dict):
        return {}
    player_state = service_state.get(steam_id)
    return player_state if isinstance(player_state, dict) else {}


def _set_service_reconcile_state(service: str, steam_id: str, **updates: str) -> None:
    state = _load_score_reconcile_state()
    service_state = state.get(service)
    if not isinstance(service_state, dict):
        service_state = {}
        state[service] = service_state
    player_state = service_state.get(steam_id)
    if not isinstance(player_state, dict):
        player_state = {}
        service_state[steam_id] = player_state
    for key, value in updates.items():
        if value:
            player_state[key] = value
    _save_score_reconcile_state(state)


def _load_scoresaber_ranked_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        leaderboards = raw.get("leaderboards") if isinstance(raw, dict) else None
        if isinstance(leaderboards, dict):
            return {str(key) for key in leaderboards.keys()}
    except Exception:
        pass
    return set()


def _load_beatleader_ranked_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        pages = raw.get("pages") if isinstance(raw, dict) else None
        if not isinstance(pages, list):
            return set()
        for page in pages:
            if not isinstance(page, dict):
                continue
            data = page.get("data") or {}
            items = data.get("data") or data.get("leaderboards") or []
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id")
                if item_id is not None:
                    ids.add(str(item_id))
    except Exception:
        pass
    return ids


def _oldest_snapshot_taken_at(steam_id: str) -> Optional[datetime]:
    if not steam_id or not SNAPSHOT_DIR.exists():
        return None

    oldest: Optional[datetime] = None
    for path in SNAPSHOT_DIR.glob(f"{steam_id}_*.json"):
        stem = path.stem
        _, _, stamp = stem.partition("_")
        if not stamp:
            continue
        try:
            taken_at = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if oldest is None or taken_at < oldest:
            oldest = taken_at
    return oldest


def _sum_pp_contribution(stats: list[StarClearStat]) -> float:
    return sum(float(item.pp_contribution or 0.0) for item in stats)


def _resolve_reconcile_fetch_until(service: str, steam_id: str) -> tuple[Optional[datetime], Optional[str], dict[str, str]]:
    service_state = _get_service_reconcile_state(service, steam_id)

    full_scan_at = _parse_utc_z(service_state.get("full_scan_at"))
    if full_scan_at is not None:
        return (
            full_scan_at - timedelta(days=_SCORE_BACKFILL_DAYS),
            f"{service} reconcile from full_scan_at {full_scan_at.strftime('%Y-%m-%d %H:%M:%S')} UTC",
            {},
        )

    oldest_snapshot_scan_at = _parse_utc_z(service_state.get("oldest_snapshot_scan_at"))
    if oldest_snapshot_scan_at is not None:
        return (
            oldest_snapshot_scan_at - timedelta(days=_SCORE_BACKFILL_DAYS),
            f"{service} reconcile from oldest_snapshot_scan_at {oldest_snapshot_scan_at.strftime('%Y-%m-%d %H:%M:%S')} UTC",
            {},
        )

    oldest_snapshot = _oldest_snapshot_taken_at(steam_id)
    if oldest_snapshot is None:
        return None, None, {}

    return (
        oldest_snapshot - timedelta(days=_SCORE_BACKFILL_DAYS),
        f"{service} reconcile from oldest snapshot {oldest_snapshot.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        {"oldest_snapshot_scan_at": _dt_to_utc_z(oldest_snapshot)},
    )


def _load_player_index() -> Dict[str, Dict[str, object]]:
    """players_index.json を読み込んで辞書形式で返す。壊れていれば空 dict。"""

    path = CACHE_DIR / "players_index.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        # 新形式: {"fetched_at": ..., "rows": [...]}
        if isinstance(raw, dict):
            raw = raw.get("rows") or []
        index: Dict[str, Dict[str, object]] = {}
        if isinstance(raw, list):
            for row in raw:
                if not isinstance(row, dict):
                    continue
                sid = str(row.get("steam_id") or "")
                if not sid:
                    continue
                entry: Dict[str, object] = {}
                ss = row.get("scoresaber")
                bl = row.get("beatleader")
                if isinstance(ss, dict):
                    try:
                        entry["scoresaber"] = ScoreSaberPlayer(**ss)
                    except TypeError:
                        pass
                if isinstance(bl, dict):
                    try:
                        entry["beatleader"] = BeatLeaderPlayer(**bl)
                    except TypeError:
                        pass
                if entry:
                    index[sid] = entry
        return index
    except Exception:  # noqa: BLE001
        return {}


def _save_player_index(
    index: Dict[str, Dict[str, object]],
    update_fetched_at: bool = True,
) -> None:
    """players_index.json を保存する。

    update_fetched_at=False の場合は既存ファイルの fetched_at を維持する。
    """

    path = CACHE_DIR / "players_index.json"
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, object]] = []
        for steam_id, entry in index.items():
            row: dict[str, object] = {"steam_id": steam_id}
            ss = entry.get("scoresaber")
            bl = entry.get("beatleader")
            if isinstance(ss, ScoreSaberPlayer):
                row["scoresaber"] = asdict(ss)
            if isinstance(bl, BeatLeaderPlayer):
                row["beatleader"] = asdict(bl)
            rows.append(row)

        # 既存の fetched_at を維持する場合はファイルから読み取る
        if not update_fetched_at:
            existing_fa = _read_cache_fetched_at(path)
            fetched_at_str = (existing_fa.isoformat() + "Z") if existing_fa is not None else (datetime.utcnow().isoformat() + "Z")
        else:
            fetched_at_str = datetime.utcnow().isoformat() + "Z"

        payload = {
            "fetched_at": fetched_at_str,
            "rows": rows,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        return


def _save_cached_pages(path: Path, pages: list[dict]) -> None:
    payload = {
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "pages": pages,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_beatleader_leaderboards_ranked(
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    fetch_until: Optional[datetime] = None,
) -> list[dict]:
    """BeatLeader の Ranked leaderboards をキャッシュ付きで全件取得する。

    beatleader.py 内の同名関数に委譲する。
    """
    return _bl_get_beatleader_leaderboards_ranked(session, progress=progress, fetch_until=fetch_until)




def _get_beatleader_player_scores(
    beatleader_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    fetch_until: Optional[datetime] = None,
    retry_failed_pages_only: bool = False,
    warning_callback: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """BeatLeader のプレイヤースコア取得を beatleader.py 側の実装に委譲するラッパー。

    実際の API 呼び出しとキャッシュ処理は
    src/mybeatsaberstats/collector/beatleader.py 内の
    _get_beatleader_player_scores に集約する。
    """

    if not beatleader_id:
        return []

    return _bl_get_beatleader_player_scores(
        beatleader_id,
        session,
        progress,
        fetch_until=fetch_until,
        retry_failed_pages_only=retry_failed_pages_only,
        warning_callback=warning_callback,
    )


# def _extract_scoresaber_accuracy(score_info: dict) -> Optional[float]:
#     """ScoreSaber のスコア情報から精度(%)を推定して返す。

#     - accuracy / acc フィールドがあればそれを優先
#     - 0.0-1.0 とみなせる値は 100 倍
#     - それ以外は 0-100 とみなし、範囲外は maxScore/baseScore から再計算を試みる
#     取得できなければ None。
#     """

#     if not isinstance(score_info, dict):
#         return None

#     try:
#         acc = score_info.get("accuracy")
#         if acc is None:
#             acc = score_info.get("acc")

#         if acc is not None:
#             acc_f = float(acc)
#             if not math.isfinite(acc_f) or acc_f <= 0:
#                 acc_f = 0.0
#             if acc_f > 0.0:
#                 # 0-1 の場合は百分率に変換
#                 if acc_f <= 1.0:
#                     return acc_f * 100.0
#                 # 0-100 をそのまま利用
#                 if acc_f <= 100.0:
#                     return acc_f
#                 # 0-10000 くらいのケースは 100 で割る
#                 if acc_f <= 10000.0:
#                     return acc_f / 100.0

#         base = score_info.get("baseScore")
#         max_score = score_info.get("maxScore")
#         if base is None:
#             base = score_info.get("score")
#         if base is None or max_score is None:
#             return None

#         base_f = float(base)
#         max_f = float(max_score)
#         if not math.isfinite(base_f) or not math.isfinite(max_f) or max_f <= 0:
#             return None

#         return max(0.0, min(100.0, base_f / max_f * 100.0))
#     except (TypeError, ValueError):  # noqa: BLE001
#         return None


def _extract_beatleader_accuracy(score_info: dict) -> Optional[float]:
    """BeatLeader のスコア情報から精度(%)を推定して返す。

    BeatLeader 側も accuracy / acc が 0.0-1.0 の割合で入っているケースを想定し、
    それ以外は ScoreSaber と同様に score/maxScore から再計算を試みる。
    取得できなければ None。
    """

    if not isinstance(score_info, dict):
        return None

    try:
        acc = score_info.get("accuracy")
        if acc is None:
            acc = score_info.get("acc")

        if acc is not None:
            acc_f = float(acc)
            if not math.isfinite(acc_f) or acc_f <= 0:
                acc_f = 0.0
            if acc_f > 0.0:
                if acc_f <= 1.0:
                    return acc_f * 100.0
                if acc_f <= 100.0:
                    return acc_f
                if acc_f <= 10000.0:
                    return acc_f / 100.0

        base = score_info.get("baseScore")
        if base is None:
            base = score_info.get("modifiedScore")
        max_score = score_info.get("maxScore")
        if base is None or max_score is None:
            return None

        base_f = float(base)
        max_f = float(max_score)
        if not math.isfinite(base_f) or not math.isfinite(max_f) or max_f <= 0:
            return None

        return max(0.0, min(100.0, base_f / max_f * 100.0))
    except (TypeError, ValueError):  # noqa: BLE001
        return None


def collect_beatleader_star_stats(
    beatleader_id: str,
    session: Optional[requests.Session] = None,
    progress: Optional[Callable[[str, float], None]] = None,
    retry_failed_pages_only: bool = False,
    warning_callback: Optional[Callable[[str], None]] = None,
) -> list[StarClearStat]:
    """BeatLeader ★別統計収集を beatleader.py 側の実装に委譲するラッパー。"""

    return _bl_collect_beatleader_star_stats(
        beatleader_id,
        session,
        progress=progress,
        retry_failed_pages_only=retry_failed_pages_only,
        warning_callback=warning_callback,
    )


def collect_beatleader_star_stats_from_cache(beatleader_id: str) -> list[StarClearStat]:
    """現在の BeatLeader キャッシュだけを使って★別統計を再計算する。"""

    return _bl_collect_beatleader_star_stats_from_cache(beatleader_id)
#     except Exception:  # noqa: BLE001
#         return None

#     try:
#         data = resp.json()
#     except Exception:  # noqa: BLE001
#         return None

#     info = data.get("playerInfo") or data.get("player") or data
#     if not isinstance(info, dict):
#         return None

#     try:
#         pid = str(info.get("id") or scoresaber_id)
#         name = str(info.get("name") or "")
#         country = str(info.get("country") or "")
#         pp_val = info.get("pp") or info.get("ppAcc") or 0.0
#         pp = float(pp_val)
#         global_rank_val = info.get("rank") or info.get("globalRank") or 0
#         global_rank = int(global_rank_val)
#         country_rank_val = info.get("countryRank") or 0
#         country_rank = int(country_rank_val)
#     except (TypeError, ValueError):  # noqa: BLE001
#         return None

#     return ScoreSaberPlayer(
#         id=pid,
#         name=name,
#         country=country,
#         pp=pp,
#         global_rank=global_rank,
#         country_rank=country_rank,
#     )


def _get_beatleader_player_stats(player_id: str, session: requests.Session) -> dict:
    """BeatLeader の /player/{id} からスコア統計を取得する。

    戻り値は scoreStats 部分の dict。失敗した場合は空 dict を返す。
    """

    if not player_id:
        return {}

    url = f"{BL_BASE_URL}/player/{player_id}"
    try:
        resp = session.get(url, timeout=10)
        print(f"Fetching BeatLeader player stats... URL: {resp.url}")
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        return {}

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {}

    stats = data.get("scoreStats")
    if isinstance(stats, dict):
        return stats
    return {}


#     star_ss_count: dict[int, int] = defaultdict(int)
#     # ★別の平均精度算出用
#     star_acc_sum: dict[int, float] = defaultdict(float)
#     star_acc_count: dict[int, int] = defaultdict(int)

#     # leaderboardId ごとに「クリア有り / NF有り / SS有り」とベスト精度を記録する
#     class _PerLeaderboardState(TypedDict):
#         star: int
#         clear: bool
#         nf: bool
#         ss: bool
#         best_acc: Optional[float]

#     per_leaderboard: dict[str, _PerLeaderboardState] = {}

#     scores = _get_scoresaber_player_scores(scoresaber_id, session)
#     print(f"取得したスコア件数: {len(scores)}")
#     for item in scores:
#         score_info = item.get("score") if isinstance(item, dict) else None
#         leaderboard = item.get("leaderboard") if isinstance(item, dict) else None

#         if leaderboard is None and isinstance(item, dict):
#             print(f"leaderboard 情報が score オブジェクトに無いケース発生。item={item}")
#             leaderboard = item
#         # print(f"処理中 score item: {leaderboard.get('id') if isinstance(leaderboard, dict) else 'N/A'}")

#         if not isinstance(leaderboard, dict):
#             print("leaderboard 情報が辞書型でないケース発生。スキップ")
#             continue

#         diff = leaderboard.get("difficulty") or {}

#         lb_id_raw = leaderboard.get("id") or diff.get("leaderboardId")
#         if lb_id_raw is None:
#             continue
#         lb_id = str(lb_id_raw)
#         tmp_stars = leaderboard.get("stars")  # or diff.get("stars")
#         ranked_flag = leaderboard.get("ranked")
#         if ranked_flag is False:
#             continue

#         # if lb_id == "685895" or lb_id == "682135":
#         #     print(f"●処理中 leaderboard ID: {lb_id}")

#         # Ranked マップ一覧に存在しない ID は無視（非 Ranked など）
#         # if lb_id not in leaderboard_star_bucket:
#         #     # print(f"スキップ non-ranked leaderboard ID: {lb_id}")
#         #     continue

#         # if lb_id == "685895" or lb_id == "682135":
#         #     print(f"●2 処理中 leaderboard ID: {lb_id}")
#         star_bucket = -1
#         if tmp_stars is not None:
#             star_bucket: int = int(tmp_stars)

#         if star_bucket < 0:
#             continue
#         # star_bucket = leaderboard_star_bucket[lb_id]
#         # if star_bucket != 11:
#         #     # TODO
#         #     continue
#         # if lb_id == "685895" or lb_id == "682135":
#             # print(f"●3 処理中 leaderboard ID: {lb_id}")
#         # print(f"処理中 leaderboard ID: {lb_id} 星: {star_bucket}")
#         # if lb_id == "685895" or lb_id == "685896" or lb_id == "682135":
#         #     print(f"★処理中 leaderboard ID: {lb_id} 星: {star_bucket}")
#         state = per_leaderboard.get(lb_id)
#         if state is None:
#             state = _PerLeaderboardState(star=star_bucket, clear=False, nf=False, ss=False, best_acc=None)
#             per_leaderboard[lb_id] = state

#         modifiers = ""
#         if isinstance(score_info, dict):
#             modifiers = str(score_info.get("modifiers") or "")

#         mods_upper = modifiers.upper()
#         is_nf = "NF" in mods_upper
#         is_ss = "SS" in mods_upper

#         if is_nf:
#             state["nf"] = True
#         elif is_ss:
#             state["ss"] = True
#         else:
#             state["clear"] = True
#             # print(f"★クリア済み leaderboard ID: {lb_id} 星: {star_bucket}")

#             # NF/SS なしスコアの精度(%)を best_acc として保持
#             acc: Optional[float] = None
#             if isinstance(score_info, dict):
#                 # まずスコアオブジェクト単体から推定
#                 acc = _extract_scoresaber_accuracy(score_info)

#                 # ScoreSaber の playerScores では maxScore が leaderboard 側にあるので、
#                 # そちらからも再計算を試みる
#                 if acc is None and isinstance(leaderboard, dict):
#                     try:
#                         base = score_info.get("baseScore") or score_info.get("score") or score_info.get("modifiedScore")
#                         max_score_lb = leaderboard.get("maxScore")
#                         if base is not None and max_score_lb is not None:
#                             base_f = float(base)
#                             max_f = float(max_score_lb)
#                             if math.isfinite(base_f) and math.isfinite(max_f) and max_f > 0:
#                                 acc = max(0.0, min(100.0, base_f / max_f * 100.0))
#                     except (TypeError, ValueError):  # noqa: BLE001
#                         acc = None

#             if acc is not None:
#                 best = state.get("best_acc")
#                 if best is None or acc > best:
#                     state["best_acc"] = acc

#     # leaderboard ごとの状態から★別のクリア数 / NF数を算出
#     print(f"集計対象 leaderboard 数: {len(per_leaderboard)}")
#     for state in per_leaderboard.values():
#         star_bucket = int(state["star"])
#         has_clear = bool(state["clear"])
#         has_nf = bool(state["nf"])
#         has_ss = bool(state["ss"])

#         if has_clear:
#             # print(f"クリア済み leaderboard (星 {star_bucket}){state.get("best_acc")=}")
#             star_clear_count[star_bucket] += 1
#             # クリア済み譜面については best_acc を★別に集計
#             best_acc = state.get("best_acc")
#             if isinstance(best_acc, (int, float)) and math.isfinite(float(best_acc)):
#                 star_acc_sum[star_bucket] += float(best_acc)
#                 star_acc_count[star_bucket] += 1
#         elif has_nf:
#             # クリアはしていないが NF プレイはある譜面
#             # print(f"NF leaderboard (星 {star_bucket})")
#             star_nf_count[star_bucket] += 1
#         elif has_ss:
#             # クリアはしていないが SS(スローソング)でのプレイはある譜面
#             # print(f"SS leaderboard (星 {star_bucket})")
#             star_ss_count[star_bucket] += 1

#     # 3) StarClearStat へ変換
#     stats: list[StarClearStat] = []

#     for star in sorted(star_map_count.keys()):
#         map_count = star_map_count[star]
#         clear_count = star_clear_count.get(star, 0)
#         nf_count = star_nf_count.get(star, 0)
#         ss_count = star_ss_count.get(star, 0)
#         clear_rate = (clear_count / map_count) if map_count > 0 else 0.0

#         avg_acc: float | None
#         cnt = star_acc_count.get(star, 0)
#         if cnt > 0:
#             avg_acc = star_acc_sum.get(star, 0.0) / cnt
#         else:
#             avg_acc = None

#         stats.append(
#             StarClearStat(
#                 star=star,
#                 map_count=map_count,
#                 clear_count=clear_count,
#                 nf_count=nf_count,
#                 ss_count=ss_count,
#                 clear_rate=clear_rate,
#                 average_acc=avg_acc,
#             )
#         )

#     return stats


class _BackgroundFetch:
    """独立した API フェッチをバックグラウンドスレッドで実行する小さなヘルパー。

    - スレッド間で requests.Session を共有しないよう、専用 Session を持つ
    - progress は (page, max_pages) を保持するだけで、UI への反映は
      メインスレッド側が wait() の pump 経由で行う（Qt をスレッドから触らない）
    - 例外は exc に保持し、呼び出し側が従来どおりベストエフォートで処理する
    """

    def __init__(self, fn: Callable[["_BackgroundFetch"], None]) -> None:
        self.exc: Optional[Exception] = None
        self.progress_state: tuple[int, Optional[int]] = (0, None)
        self.session = requests.Session()

        def _run() -> None:
            try:
                fn(self)
            except Exception as exc:  # noqa: BLE001
                self.exc = exc

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def on_page(self, page: int, max_pages: Optional[int]) -> None:
        self.progress_state = (page, max_pages)

    def wait(self, pump: Optional[Callable[[int, Optional[int]], None]] = None) -> None:
        """完了までメインスレッドで待機する。pump は定期的に呼ばれる（キャンセル例外はそのまま伝播）。"""
        while self._thread.is_alive():
            self._thread.join(0.2)
            if pump is not None:
                page, max_pages = self.progress_state
                pump(page, max_pages)


def create_snapshot_for_steam_id(
    steam_id: str,
    session: Optional[requests.Session] = None,
    snapshot_dir: Optional[Path] = None,
    progress: Optional[Callable[[str, float], None]] = None,
    options: Optional[SnapshotOptions] = None,
    on_warning: Optional[Callable[[str], None]] = None,
) -> Snapshot:
    """指定 SteamID(または players_index のキー)の現在ステータスから Snapshot を生成する。

    players_index.json に無いプレイヤーは ScoreSaber / BeatLeader API から直接取得して補完する。
    options で各データソースの取得を個別にスキップできる。
    """
    # options が None の場合はすべて取得
    if options is None:
        options = SnapshotOptions()

    ss_new_ranked_ids: set[str] = set()
    bl_new_ranked_ids: set[str] = set()

    # フォールバックなどの警告メッセージを收集する
    _warnings: list[str] = []

    def _add_warning(message: str) -> None:
        if not message:
            return
        if message not in _warnings:
            _warnings.append(message)
            if on_warning is not None:
                try:
                    on_warning(message)
                except Exception:
                    pass

    # 外部から渡される progress(message, frac) を、この関数内では _step(frac, message)
    # という形で扱えるようにするヘルパー。
    def _step(frac: float, message: str) -> None:
        if progress is None:
            return
        progress(message, frac)

    def _rethrow_if_cancelled(exc: Exception) -> None:
        """進捗ダイアログのキャンセル(RuntimeError('SNAPSHOT_CANCELLED'))だけは握りつぶさずに再スローする。"""
        if isinstance(exc, RuntimeError) and str(exc) == "SNAPSHOT_CANCELLED":
            raise

    # 以下の処理では requests.Session を必須とする関数を多数呼び出すため、
    # この関数内では session を必ず非 None の Session インスタンスに正規化して扱う。
    if session is None:
        session = requests.Session()
    assert session is not None

    # ScoreSaber / BeatLeader の Ranked Maps キャッシュを先に更新しておく。
    # 初回は全件取得、2回目以降は差分（ScoreSaber）またはメタデータの増分検知（BeatLeader）のみ。
    # BeatLeader 側は ScoreSaber 側と完全に独立（別ホスト・別キャッシュファイル）なので、
    # バックグラウンドで並行取得して所要時間を短縮する。
    bl_ranked_bg: Optional[_BackgroundFetch] = None
    _bl_ranked_cache_path = CACHE_DIR / "beatleader_ranked_maps.json"
    _bl_ranked_before_ids: set[str] = set()
    if options.fetch_bl_ranked_maps:
        try:
            _bl_ranked_before_ids = _load_beatleader_ranked_ids(_bl_ranked_cache_path)
            print("3. BeatLeader Ranked Maps キャッシュ更新（バックグラウンドで並行実行）...")
            if options.bl_ranked_until is not None:
                bl_ranked_until = options.bl_ranked_until
            else:
                bl_last_fetched = _read_cache_fetched_at(_bl_ranked_cache_path)
                if bl_last_fetched is not None:
                    # 月次更新でrankedTime相当フィールドが変わる可能性があるため、前回取得日時より60日前から再取得する
                    bl_ranked_until = bl_last_fetched - timedelta(days=60)
                    print(f"BeatLeader Ranked Maps 前回取得日時: {bl_last_fetched.strftime('%Y-%m-%d %H:%M:%S')} UTC → 60日遡り: {bl_ranked_until.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                else:
                    bl_ranked_until = None
                    print("BeatLeader Ranked Maps: 初回取得のため全件取得")

            def _bl_ranked_task(task: _BackgroundFetch, _until=bl_ranked_until) -> None:
                _get_beatleader_leaderboards_ranked(task.session, progress=task.on_page, fetch_until=_until)

            bl_ranked_bg = _BackgroundFetch(_bl_ranked_task)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            bl_ranked_bg = None

    if options.fetch_ss_ranked_maps:
        try:
            _ss_ranked_cache_path = CACHE_DIR / "scoresaber_ranked_maps.json"
            _ss_ranked_before_ids = _load_scoresaber_ranked_ids(_ss_ranked_cache_path)

            def _ss_leaderboard_progress(page: int, max_pages: Optional[int]) -> None:
                if max_pages and max_pages > 0:
                    phase_frac = max(0.0, min(1.0, page / max_pages))
                    page_text = f"{page}/{max_pages}"
                else:
                    phase_frac = 0.0 if page <= 1 else 1.0
                    page_text = f"{page}/?"
                global_ratio = 0.02 + 0.03 * phase_frac
                phase_percent = int(phase_frac * 100)
                msg = f"Updating ScoreSaber Ranked Maps ({phase_percent}%, page {page_text})..."
                _step(global_ratio, msg)

            print("2. ScoreSaber Ranked Maps キャッシュ更新...")
            if options.ss_ranked_until is not None:
                ss_ranked_until = options.ss_ranked_until
            else:
                ss_last_fetched = _read_cache_fetched_at(CACHE_DIR / "scoresaber_ranked_maps.json")
                if ss_last_fetched is not None:
                    # 月次更新でrankedDate遡及変更される可能性があるため、前回取得日時より60日前から再取得する
                    ss_ranked_until = ss_last_fetched - timedelta(days=60)
                    print(f"ScoreSaber Ranked Maps 前回取得日時: {ss_last_fetched.strftime('%Y-%m-%d %H:%M:%S')} UTC → 60日遡り: {ss_ranked_until.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                else:
                    ss_ranked_until = None
                    print("ScoreSaber Ranked Maps: 初回取得のため全件取得")
            fetch_label = ss_ranked_until.strftime('%Y-%m-%d %H:%M') if ss_ranked_until else "full"
            _step(0.02, f"Updating ScoreSaber Ranked Maps (last fetch: {fetch_label})...")
            ss_ranked_leaderboards = _get_scoresaber_leaderboards_ranked(session, progress=_ss_leaderboard_progress, fetch_until=ss_ranked_until)
            map_store = MapStore()
            map_store.ss_ranked_maps = ss_ranked_leaderboards
            ss_new_ranked_ids = _load_scoresaber_ranked_ids(_ss_ranked_cache_path) - _ss_ranked_before_ids
            if ss_new_ranked_ids:
                print(f"ScoreSaber Ranked Maps: 新規 ranked 譜面 {len(ss_new_ranked_ids)} 件")
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            pass
    else:
        print("2. ScoreSaber Ranked Maps 取得スキップ（オプションが無効）")
        _step(0.05, "Skipping ScoreSaber Ranked Maps...")

    if bl_ranked_bg is not None:
        try:
            def _bl_leaderboard_progress(page: int, max_pages: Optional[int]) -> None:
                if max_pages and max_pages > 0:
                    phase_frac = max(0.0, min(1.0, page / max_pages))
                    page_text = f"{page}/{max_pages}"
                else:
                    phase_frac = 0.0 if page <= 1 else 1.0
                    page_text = f"{page}/?"
                global_ratio = 0.05 + 0.03 * phase_frac
                phase_percent = int(phase_frac * 100)
                msg = f"Updating BeatLeader Ranked Maps ({phase_percent}%, page {page_text})..."
                _step(global_ratio, msg)

            print("3.1 BeatLeader Ranked Maps バックグラウンド取得の完了待ち...")
            _step(0.05, "Updating BeatLeader Ranked Maps...")
            bl_ranked_bg.wait(_bl_leaderboard_progress)
            if bl_ranked_bg.exc is not None:
                print(f"BeatLeader Ranked Maps 取得エラー（続行）: {bl_ranked_bg.exc}")
            else:
                bl_new_ranked_ids = _load_beatleader_ranked_ids(_bl_ranked_cache_path) - _bl_ranked_before_ids
                if bl_new_ranked_ids:
                    print(f"BeatLeader Ranked Maps: 新規 ranked 譜面 {len(bl_new_ranked_ids)} 件")
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            pass
    elif not options.fetch_bl_ranked_maps:
        print("3. BeatLeader Ranked Maps 取得スキップ（オプションが無効）")
        _step(0.08, "Skipping BeatLeader Ranked Maps...")

    # プレイヤーインデックスを読み込み、必要なら API からプレイヤー情報を補完する
    print("4. プレイヤーインデックスの確認...")
    _step(0.08, "Loading player index...")
    player_index = _load_player_index()
    print("4.1 プレイヤーインデックスの確認完了。")
    map_store = MapStore()
    map_store.player_index = player_index
    
    entry = player_index.get(steam_id)
    if not entry:
        # players_index.json に存在しない場合でも、可能であれば ScoreSaber / BeatLeader
        # の API から直接プレイヤー情報を取得してスナップショットを作成できるようにする。
        ss: Optional[ScoreSaberPlayer] = None
        bl: Optional[BeatLeaderPlayer] = None

        if _is_steam_id(steam_id):
            _step(0.10, "Fetching player from ScoreSaber / BeatLeader...")
            try:
                print("4.3 players_index.json に存在しない SteamID。ScoreSaber から情報取得を試みます...")
                ss = _fetch_scoresaber_player_basic(steam_id, session)
                map_store.ss_players[steam_id] = ss
            except Exception as exc:  # noqa: BLE001
                _rethrow_if_cancelled(exc)
                ss = None
            try:
                print("4.4 BeatLeader から情報取得を試みます...")
                bl = fetch_bl_player(steam_id, session=session)
                map_store.bl_players[steam_id] = bl
            except Exception as exc:  # noqa: BLE001
                _rethrow_if_cancelled(exc)
                bl = None

        if ss is None and bl is None:
            raise RuntimeError(
                f"steam_id {steam_id!r} not found in players_index.json "
                "and failed to fetch from ScoreSaber/BeatLeader APIs.",
            )

        entry = {}
        if ss is not None:
            entry["scoresaber"] = ss
        if bl is not None:
            entry["beatleader"] = bl
        player_index[steam_id] = entry
        try:
            _save_player_index(player_index, update_fetched_at=False)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            # インデックス保存に失敗してもスナップショット作成自体は続行する
            pass

    # インデックス（または API 補完）から ScoreSaber / BeatLeader 情報を取り出す
    raw_ss = entry.get("scoresaber") if isinstance(entry, dict) else None
    ss: Optional[ScoreSaberPlayer] = raw_ss if isinstance(raw_ss, ScoreSaberPlayer) else None
    raw_bl = entry.get("beatleader") if isinstance(entry, dict) else None
    bl: Optional[BeatLeaderPlayer] = raw_bl if isinstance(raw_bl, BeatLeaderPlayer) else None

    scoresaber_id: Optional[str] = ss.id if ss is not None else None
    scoresaber_name: Optional[str] = ss.name if ss is not None else None
    scoresaber_country: Optional[str] = ss.country if ss is not None else None
    scoresaber_pp: Optional[float] = ss.pp if ss is not None else None
    scoresaber_rank_global: Optional[int] = ss.global_rank if ss is not None else None
    scoresaber_rank_country: Optional[int] = ss.country_rank if ss is not None else None
    scoresaber_average_ranked_acc: Optional[float] = None
    scoresaber_total_play_count: Optional[int] = None
    scoresaber_ranked_play_count: Optional[int] = None

    # ScoreSaber player scores はキャッシュファイル書き込みのみで後続処理と独立なため、
    # バックグラウンドで取得しつつ BeatLeader 側の処理を並行して進める。
    ss_scores_bg: Optional[_BackgroundFetch] = None

    if scoresaber_id:
        if options.fetch_scoresaber:
            # まず ScoreSaber の基本情報（PP / ランク）を最新化しておく。
            try:
                print("5. ScoreSaber 基本情報更新...")
                ss_latest = _fetch_scoresaber_player_basic(scoresaber_id, session)
            except Exception as exc:  # noqa: BLE001
                _rethrow_if_cancelled(exc)
                ss_latest = None

            if ss_latest is not None:
                scoresaber_name = ss_latest.name or scoresaber_name
                scoresaber_country = ss_latest.country or scoresaber_country
                scoresaber_pp = ss_latest.pp
                scoresaber_rank_global = ss_latest.global_rank
                scoresaber_rank_country = ss_latest.country_rank

                try:
                    entry["scoresaber"] = ss_latest
                    _save_player_index(player_index, update_fetched_at=False)
                except Exception as exc:  # noqa: BLE001
                    _rethrow_if_cancelled(exc)
                    pass

            # スナップショット取得時にプレイヤースコアキャッシュも更新しておく。
            # 取得自体はバックグラウンドで開始し、BeatLeader 側の処理と並行させる
            # （完了待ちは BeatLeader プレイヤーデータ取得後に行う）。
            try:
                _step(0.15, "Fetching ScoreSaber player scores (page 1/?)...")
                print("5. ScoreSaber プレイヤースコアキャッシュ更新（バックグラウンドで並行実行）...")
                _ss_effective_until = datetime(2000, 1, 1) if options.ss_fetch_all else options.ss_fetch_until
                if options.ss_fetch_all:
                    print("ScoreSaber: 全スコア再取得モード (fetch_all=True)")
                _ss_score_cache_path = CACHE_DIR / f"scoresaber_player_scores_{scoresaber_id}.json"
                _ss_score_fetched_at = _read_cache_fetched_at(_ss_score_cache_path)
                if _ss_effective_until is None and ss_new_ranked_ids and _ss_score_fetched_at is not None:
                    _ss_effective_until = _ss_score_fetched_at - timedelta(days=_SCORE_BACKFILL_DAYS)
                    print(
                        "ScoreSaber player scores: 新規 ranked 譜面検出のため "
                        f"player_scores fetched_at から {_SCORE_BACKFILL_DAYS} 日遡り "
                        f"({_ss_effective_until.strftime('%Y-%m-%d %H:%M:%S')} UTC)"
                    )
                if _ss_score_fetched_at is not None:
                    print(f"ScoreSaberプレイヤースコア 前回取得日時: {_ss_score_fetched_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                    _ss_score_label = _ss_score_fetched_at.strftime('%Y-%m-%d %H:%M')
                else:
                    print("ScoreSaberプレイヤースコア: 初回取得")
                    _ss_score_label = "new"
                _step(0.15, f"Fetching ScoreSaber player scores (last fetch: {_ss_score_label}, page 1/?)...")

                def _ss_scores_task(task: _BackgroundFetch, _sid=scoresaber_id, _until=_ss_effective_until) -> None:
                    _get_scoresaber_player_scores(_sid, task.session, progress=task.on_page, fetch_until=_until)

                ss_scores_bg = _BackgroundFetch(_ss_scores_task)
            except Exception as exc:  # noqa: BLE001
                _rethrow_if_cancelled(exc)
                pass

            print("6. ScoreSaber プレイヤーステータス取得...")
            _step(0.20, "Fetching ScoreSaber player stats...")
            stats = _get_scoresaber_player_stats(scoresaber_id, session)
            if stats:
                try:
                    avg = stats.get("averageRankedAccuracy")
                    if avg is not None:
                        scoresaber_average_ranked_acc = float(avg)
                except (TypeError, ValueError):
                    pass

                try:
                    total_pc = stats.get("totalPlayCount")
                    if total_pc is not None:
                        scoresaber_total_play_count = int(total_pc)
                except (TypeError, ValueError):
                    pass

                try:
                    ranked_pc = stats.get("rankedPlayCount")
                    if ranked_pc is not None:
                        scoresaber_ranked_play_count = int(ranked_pc)
                except (TypeError, ValueError):
                    pass
        else:
            print("5-6. ScoreSaber プレイヤーデータ取得スキップ（オプションが無効）")
            _step(0.20, "Skipping ScoreSaber player data...")

    # BeatLeader 側も、Snapshot 取得時に基本情報（PP / ランク）を最新化しておく。
    beatleader: Optional[BeatLeaderPlayer] = bl
    beatleader_lookup_id = beatleader.id if beatleader is not None else steam_id
    if beatleader_lookup_id and options.fetch_beatleader:
        try:
            print("6. BeatLeader 基本情報更新...")
            bl_latest = fetch_bl_player(beatleader_lookup_id, session=session)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            bl_latest = None

        if bl_latest is not None:
            beatleader = bl_latest

            # 可能であれば players_index.json 側の BeatLeader 情報も更新しておく
            try:
                entry["beatleader"] = bl_latest
                _save_player_index(player_index, update_fetched_at=False)
            except Exception as exc:  # noqa: BLE001
                _rethrow_if_cancelled(exc)
                pass
    elif beatleader_lookup_id and not options.fetch_beatleader:
        print("6. BeatLeader 基本情報取得スキップ（オプションが無効）")

    beatleader_id: Optional[str] = beatleader.id if beatleader is not None else None
    beatleader_name: Optional[str] = beatleader.name if beatleader is not None else None
    beatleader_country: Optional[str] = beatleader.country if beatleader is not None else None
    beatleader_pp: Optional[float] = beatleader.pp if beatleader is not None else None
    beatleader_rank_global: Optional[int] = beatleader.global_rank if beatleader is not None else None
    beatleader_rank_country: Optional[int] = beatleader.country_rank if beatleader is not None else None
    beatleader_level: Optional[int] = beatleader.level if beatleader is not None else None
    beatleader_experience: Optional[int] = beatleader.experience if beatleader is not None else None
    beatleader_prestige: Optional[int] = beatleader.prestige if beatleader is not None else None
    beatleader_prestige_icon_url: Optional[str] = beatleader.prestige_icon_url if beatleader is not None else None

    # BeatLeader の追加統計（average_acc, play_count 系）は scoreStats から取得する（ベストエフォート）。
    beatleader_average_ranked_acc: Optional[float] = None
    beatleader_total_play_count: Optional[int] = None
    beatleader_ranked_play_count: Optional[int] = None

    if beatleader_id:
        if options.fetch_beatleader:
            # BeatLeader 側もスナップショット取得時にプレイヤースコアキャッシュを更新しておく。
            try:
                _step(0.30, "Fetching BeatLeader player scores (page 1/?)...")

                def _bl_scores_progress(page: int, max_pages: Optional[int]) -> None:
                    if max_pages and max_pages > 0:
                        frac = max(0.0, min(1.0, page / max_pages))
                        msg = f"Fetching BeatLeader player scores (page {page}/{max_pages})..."
                    else:
                        frac = 0.0
                        msg = f"Fetching BeatLeader player scores (page {page}/?)..."
                    _step(0.30 + 0.05 * frac, msg)
                print("7. BeatLeader プレイヤースコアキャッシュ更新...")
                _bl_effective_until = datetime(2000, 1, 1) if options.bl_fetch_all else options.bl_fetch_until
                if options.bl_fetch_all:
                    print("BeatLeader: 全スコア再取得モード (fetch_all=True)")
                _bl_score_cache_path = CACHE_DIR / f"beatleader_player_scores_{beatleader_id}.json"
                _bl_score_fetched_at = _read_cache_fetched_at(_bl_score_cache_path)
                if _bl_effective_until is None and bl_new_ranked_ids and _bl_score_fetched_at is not None:
                    _bl_effective_until = _bl_score_fetched_at - timedelta(days=_SCORE_BACKFILL_DAYS)
                    print(
                        "BeatLeader player scores: 新規 ranked 譜面検出のため "
                        f"player_scores fetched_at から {_SCORE_BACKFILL_DAYS} 日遡り "
                        f"({_bl_effective_until.strftime('%Y-%m-%d %H:%M:%S')} UTC)"
                    )
                if _bl_score_fetched_at is not None:
                    print(f"BeatLeaderプレイヤースコア 前回取得日時: {_bl_score_fetched_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                    _bl_score_label = _bl_score_fetched_at.strftime('%Y-%m-%d %H:%M')
                else:
                    print("BeatLeaderプレイヤースコア: 初回取得")
                    _bl_score_label = "new"
                _step(0.30, f"Fetching BeatLeader player scores (last fetch: {_bl_score_label}, page 1/?)...")
                _get_beatleader_player_scores(
                    beatleader_id,
                    session,
                    progress=_bl_scores_progress,
                    fetch_until=_bl_effective_until,
                    warning_callback=_add_warning,
                )
                if options.bl_fetch_all:
                    _set_service_reconcile_state("beatleader", steam_id, full_scan_at=_dt_to_utc_z(datetime.utcnow()))
            except Exception as exc:  # noqa: BLE001
                _rethrow_if_cancelled(exc)
                pass

            print("8. BeatLeader プレイヤーステータス取得...")
            _step(0.35, "Fetching BeatLeader player stats...")
            bl_stats = _get_beatleader_player_stats(beatleader_id, session)
            if bl_stats:
                print("8.1 BeatLeader プレイヤーステータス取得完了。")
                try:
                    bl_avg = bl_stats.get("averageRankedAccuracy")
                    if bl_avg is not None:
                        beatleader_average_ranked_acc = float(bl_avg) * 100.0
                except (TypeError, ValueError):
                    pass

                try:
                    bl_total_pc = bl_stats.get("totalPlayCount")
                    if bl_total_pc is not None:
                        beatleader_total_play_count = int(bl_total_pc)
                except (TypeError, ValueError):
                    pass

                try:
                    bl_ranked_pc = bl_stats.get("rankedPlayCount")
                    if bl_ranked_pc is not None:
                        beatleader_ranked_play_count = int(bl_ranked_pc)
                except (TypeError, ValueError):
                    pass
        else:
            print("7-8. BeatLeader プレイヤーデータ取得スキップ（オプションが無効）")
            _step(0.35, "Skipping BeatLeader player data...")

    # バックグラウンドで開始した ScoreSaber player scores 取得の完了を待つ。
    if ss_scores_bg is not None:
        try:
            def _ss_scores_pump(page: int, max_pages: Optional[int]) -> None:
                if max_pages and max_pages > 0:
                    frac = max(0.0, min(1.0, page / max_pages))
                    msg = f"Fetching ScoreSaber player scores (page {page}/{max_pages})..."
                else:
                    frac = 0.0
                    msg = f"Fetching ScoreSaber player scores (page {page}/?)..."
                _step(0.35 + 0.02 * frac, msg)

            print("5.1 ScoreSaber プレイヤースコア バックグラウンド取得の完了待ち...")
            ss_scores_bg.wait(_ss_scores_pump)
            if ss_scores_bg.exc is not None:
                print(f"ScoreSaber プレイヤースコア取得エラー（続行）: {ss_scores_bg.exc}")
            elif options.ss_fetch_all:
                _set_service_reconcile_state("scoresaber", steam_id, full_scan_at=_dt_to_utc_z(datetime.utcnow()))
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            pass

    accsaber_reloaded_overall_total_maps: Optional[int] = None
    accsaber_reloaded_true_total_maps: Optional[int] = None
    accsaber_reloaded_standard_total_maps: Optional[int] = None
    accsaber_reloaded_tech_total_maps: Optional[int] = None
    if options.fetch_accsaber_reloaded:
        # AccSaber (Reloaded) 総譜面数を更新する（accsaber_reloaded_map_counts.json）
        try:
            _rl_map_counts = _fetch_reloaded_map_counts(session=session)
            accsaber_reloaded_overall_total_maps = _rl_map_counts.get("overall")
            accsaber_reloaded_true_total_maps = _rl_map_counts.get("true")
            accsaber_reloaded_standard_total_maps = _rl_map_counts.get("standard")
            accsaber_reloaded_tech_total_maps = _rl_map_counts.get("tech")
        except Exception:  # noqa: BLE001
            pass

    # AccSaber Reloaded ランク情報を取得する
    accsaber_reloaded_overall_rank:          Optional[int]   = None
    accsaber_reloaded_overall_rank_country:  Optional[int]   = None
    accsaber_reloaded_overall_ap:            Optional[float] = None
    accsaber_reloaded_overall_ranked_plays:  Optional[int]   = None
    accsaber_reloaded_true_rank:             Optional[int]   = None
    accsaber_reloaded_true_rank_country:     Optional[int]   = None
    accsaber_reloaded_true_ap:               Optional[float] = None
    accsaber_reloaded_true_ranked_plays:     Optional[int]   = None
    accsaber_reloaded_standard_rank:         Optional[int]   = None
    accsaber_reloaded_standard_rank_country: Optional[int]   = None
    accsaber_reloaded_standard_ap:           Optional[float] = None
    accsaber_reloaded_standard_ranked_plays: Optional[int]   = None
    accsaber_reloaded_tech_rank:             Optional[int]   = None
    accsaber_reloaded_tech_rank_country:     Optional[int]   = None
    accsaber_reloaded_tech_ap:               Optional[float] = None
    accsaber_reloaded_tech_ranked_plays:     Optional[int]   = None
    accsaber_reloaded_overall_avg_acc:       Optional[float] = None
    accsaber_reloaded_true_avg_acc:          Optional[float] = None
    accsaber_reloaded_standard_avg_acc:      Optional[float] = None
    accsaber_reloaded_tech_avg_acc:          Optional[float] = None
    accsaber_reloaded_xp:                    Optional[float] = None
    accsaber_reloaded_xp_level:              Optional[int]   = None
    accsaber_reloaded_xp_rank:               Optional[int]   = None
    accsaber_reloaded_xp_rank_country:       Optional[int]   = None
    accsaber_reloaded_milestones_completed:  Optional[int]   = None
    accsaber_reloaded_milestones_total:      Optional[int]   = None

    # AccSaber Reloaded の userId は Steam ID（BeatLeader ID と同じ）なので beatleader_id を優先する。
    # ScoreSaber が非 Steam 形式の ID（例: 3117609721598571）の場合に scoresaber_id を渡すと
    # リーダーボードを全ページ走査してもマッチしないため、長時間固まる原因となる。
    _rl_player_id = beatleader_id or scoresaber_id
    if options.fetch_accsaber_reloaded and _rl_player_id:
        print("9.4R AccSaber Reloaded プレイヤーステータス取得...")
        _step(0.62, "Fetching AccSaber Reloaded ranks...")
        _rl_country = (scoresaber_country or "").upper() or None
        try:
            _rl_result = _fetch_accsaber_reloaded(_rl_player_id, country=_rl_country, session=session)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            _rl_result = {}

        def _pick(cat: str, attr: str, conv):
            p = _rl_result.get(cat)
            if p is None:
                return None
            v = getattr(p, attr, None)
            if v is None:
                return None
            try:
                return conv(v)
            except (TypeError, ValueError):
                return None

        accsaber_reloaded_overall_rank          = _pick("overall",  "rank_global",   int)
        accsaber_reloaded_overall_rank_country  = _pick("overall",  "rank_country",  int)
        accsaber_reloaded_overall_ap            = _pick("overall",  "ap",            float)
        accsaber_reloaded_overall_ranked_plays  = _pick("overall",  "ranked_plays",  int)
        accsaber_reloaded_true_rank             = _pick("true",     "rank_global",   int)
        accsaber_reloaded_true_rank_country     = _pick("true",     "rank_country",  int)
        accsaber_reloaded_true_ap               = _pick("true",     "ap",            float)
        accsaber_reloaded_true_ranked_plays     = _pick("true",     "ranked_plays",  int)
        accsaber_reloaded_standard_rank         = _pick("standard", "rank_global",   int)
        accsaber_reloaded_standard_rank_country = _pick("standard", "rank_country",  int)
        accsaber_reloaded_standard_ap           = _pick("standard", "ap",            float)
        accsaber_reloaded_standard_ranked_plays = _pick("standard", "ranked_plays",  int)
        accsaber_reloaded_tech_rank             = _pick("tech",     "rank_global",   int)
        accsaber_reloaded_tech_rank_country     = _pick("tech",     "rank_country",  int)
        accsaber_reloaded_tech_ap               = _pick("tech",     "ap",            float)
        accsaber_reloaded_tech_ranked_plays     = _pick("tech",     "ranked_plays",  int)
        accsaber_reloaded_overall_avg_acc       = _pick("overall",  "average_acc",   lambda v: float(v) * 100.0)
        accsaber_reloaded_true_avg_acc          = _pick("true",     "average_acc",   lambda v: float(v) * 100.0)
        accsaber_reloaded_standard_avg_acc      = _pick("standard", "average_acc",   lambda v: float(v) * 100.0)
        accsaber_reloaded_tech_avg_acc          = _pick("tech",     "average_acc",   lambda v: float(v) * 100.0)

        # XP ランク
        try:
            _rl_xp_result = _fetch_accsaber_reloaded_xp(_rl_player_id, country=_rl_country, session=session)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            _rl_xp_result = None
        if _rl_xp_result is not None:
            accsaber_reloaded_xp            = _rl_xp_result.xp
            accsaber_reloaded_xp_level      = _rl_xp_result.level
            accsaber_reloaded_xp_rank       = _rl_xp_result.rank_global
            accsaber_reloaded_xp_rank_country = _rl_xp_result.rank_country

        # マイルストーン達成状況
        try:
            _rl_milestones = _fetch_accsaber_reloaded_milestones(_rl_player_id, session=session)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            _rl_milestones = None
        if _rl_milestones is not None:
            accsaber_reloaded_milestones_completed, accsaber_reloaded_milestones_total = _rl_milestones

        print("9.4R AccSaber Reloaded プレイヤーステータス取得完了。")

        # AccSaber Reloaded 全マップデータをキャッシュに保存する
        try:
            _step(0.65, "Fetching AccSaber Reloaded map data for playlist cache...")
            _fetch_and_save_rl_maps(session=session)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
        # AccSaber Reloaded プレイヤースコアをキャッシュに保存する
        try:
            _step(0.66, "Fetching AccSaber Reloaded player scores for cache...")
            _fetch_and_save_rl_player_scores(_rl_player_id, session=session)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)

        try:
            _rl_effective_play_counts = _compute_rl_effective_played_counts(_rl_player_id)
            if _rl_effective_play_counts:
                accsaber_reloaded_overall_ranked_plays = _rl_effective_play_counts.get("overall")
                accsaber_reloaded_true_ranked_plays = _rl_effective_play_counts.get("true")
                accsaber_reloaded_standard_ranked_plays = _rl_effective_play_counts.get("standard")
                accsaber_reloaded_tech_ranked_plays = _rl_effective_play_counts.get("tech")
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
    else:
        if not options.fetch_accsaber_reloaded:
            print("9.4R AccSaber Reloaded 取得スキップ（オプションが無効）")
        else:
            print("9.4R AccSaber Reloaded 取得スキップ（BeatLeader ID / ScoreSaber ID が未取得）")

    # ScoreSaber / BeatLeader のスコア一覧から★別統計を集計する（失敗した場合は空リスト）。
    if options.fetch_ss_star_stats:
        try:
            print("9.5 ScoreSaber ★別統計集計...")
            _step(0.70, "Collecting ScoreSaber star stats...")
            star_stats: list[StarClearStat] = _collect_star_stats_from_scoresaber(scoresaber_id, session) if scoresaber_id else []
        except Exception:  # noqa: BLE001
            print("★別統計の集計に失敗しました。")
            star_stats = []
    else:
        print("9.5 ScoreSaber ★別統計集計スキップ（オプションが無効）")
        _step(0.70, "Skipping ScoreSaber star stats...")
        star_stats = []

    if options.fetch_bl_star_stats:
        try:
            print("9.6 BeatLeader ★別統計集計...")
            _step(0.80, "Collecting BeatLeader star stats...")
            def _bl_star_stats_progress(message: str, fraction: float) -> None:
                _step(0.80 + 0.10 * max(0.0, min(1.0, fraction)), message)

            beatleader_star_stats: list[StarClearStat] = (
                collect_beatleader_star_stats(
                    beatleader_id,
                    session,
                    progress=_bl_star_stats_progress,
                    retry_failed_pages_only=True,
                    warning_callback=_add_warning,
                ) if beatleader_id else []
            )
        except Exception:  # noqa: BLE001
            beatleader_star_stats = []
            _add_warning("BeatLeader star stats: failed to collect during snapshot.")
            print("9.6 BeatLeader ★別統計集計完了。")
    else:
        print("9.6 BeatLeader ★別統計集計スキップ（オプションが無効）")
        _step(0.80, "Skipping BeatLeader star stats...")
        beatleader_star_stats = []

    if options.fetch_scoresaber and options.fetch_ss_star_stats and scoresaber_id and scoresaber_pp is not None:
        ss_local_pp_total = _sum_pp_contribution(star_stats)
        ss_pp_drift = abs(float(scoresaber_pp) - ss_local_pp_total)
        ss_state = _get_service_reconcile_state("scoresaber", steam_id)
        ss_needs_bootstrap = not ss_state.get("full_scan_at") and not ss_state.get("oldest_snapshot_scan_at")
        if ss_pp_drift >= _PP_RECONCILE_THRESHOLD or ss_needs_bootstrap:
            ss_reconcile_until, ss_reason, ss_state_updates = _resolve_reconcile_fetch_until("scoresaber", steam_id)
            if ss_reconcile_until is not None and ss_reason is not None:
                print(
                    "ScoreSaber PP reconcile: "
                    f"profile={float(scoresaber_pp):.2f} local={ss_local_pp_total:.2f} drift={ss_pp_drift:.2f}"
                )
                print(ss_reason)

                def _ss_reconcile_progress(page: int, max_pages: Optional[int]) -> None:
                    if max_pages and max_pages > 0:
                        frac = max(0.0, min(1.0, page / max_pages))
                        msg = f"Reconciling ScoreSaber player scores (page {page}/{max_pages})..."
                    else:
                        frac = 0.0
                        msg = f"Reconciling ScoreSaber player scores (page {page}/?)..."
                    _step(0.66 + 0.04 * frac, msg)

                _step(0.66, "Reconciling ScoreSaber player scores...")
                _get_scoresaber_player_scores(
                    scoresaber_id,
                    session,
                    progress=_ss_reconcile_progress,
                    fetch_until=ss_reconcile_until,
                )
                _step(0.70, "Recollecting ScoreSaber star stats...")
                star_stats = _collect_star_stats_from_scoresaber(scoresaber_id, session)
                if ss_state_updates:
                    _set_service_reconcile_state("scoresaber", steam_id, **ss_state_updates)

    if options.fetch_beatleader and options.fetch_bl_star_stats and beatleader_id and beatleader_pp is not None:
        bl_local_pp_total = _sum_pp_contribution(beatleader_star_stats)
        bl_pp_drift = abs(float(beatleader_pp) - bl_local_pp_total)
        bl_state = _get_service_reconcile_state("beatleader", steam_id)
        bl_needs_bootstrap = not bl_state.get("full_scan_at") and not bl_state.get("oldest_snapshot_scan_at")
        if bl_pp_drift >= _PP_RECONCILE_THRESHOLD or bl_needs_bootstrap:
            bl_reconcile_until, bl_reason, bl_state_updates = _resolve_reconcile_fetch_until("beatleader", steam_id)
            if bl_reconcile_until is not None and bl_reason is not None:
                print(
                    "BeatLeader PP reconcile: "
                    f"profile={float(beatleader_pp):.2f} local={bl_local_pp_total:.2f} drift={bl_pp_drift:.2f}"
                )
                print(bl_reason)

                def _bl_reconcile_progress(page: int, max_pages: Optional[int]) -> None:
                    if max_pages and max_pages > 0:
                        frac = max(0.0, min(1.0, page / max_pages))
                        msg = f"Reconciling BeatLeader player scores (page {page}/{max_pages})..."
                    else:
                        frac = 0.0
                        msg = f"Reconciling BeatLeader player scores (page {page}/?)..."
                    _step(0.76 + 0.04 * frac, msg)

                def _bl_reconcile_star_stats_progress(message: str, fraction: float) -> None:
                    _step(0.80 + 0.10 * max(0.0, min(1.0, fraction)), message)

                _step(0.76, "Reconciling BeatLeader player scores...")
                _get_beatleader_player_scores(
                    beatleader_id,
                    session,
                    progress=_bl_reconcile_progress,
                    fetch_until=bl_reconcile_until,
                    warning_callback=_add_warning,
                )
                _step(0.80, "Recollecting BeatLeader star stats...")
                beatleader_star_stats = collect_beatleader_star_stats(
                    beatleader_id,
                    session,
                    progress=_bl_reconcile_star_stats_progress,
                    retry_failed_pages_only=True,
                    warning_callback=_add_warning,
                ) if beatleader_id else []
                if bl_state_updates:
                    _set_service_reconcile_state("beatleader", steam_id, **bl_state_updates)
    # スナップショットオブジェクトを構築して保存する
    print("10. スナップショットオブジェクト構築...")
    now = datetime.utcnow().replace(microsecond=0)

    snapshot = Snapshot(
        taken_at=now.isoformat() + "Z",
        steam_id=steam_id,
        scoresaber_id=scoresaber_id,
        scoresaber_name=scoresaber_name,
        scoresaber_country=scoresaber_country,
        scoresaber_pp=scoresaber_pp,
        scoresaber_rank_global=scoresaber_rank_global,
        scoresaber_rank_country=scoresaber_rank_country,
        scoresaber_average_ranked_acc=scoresaber_average_ranked_acc,
        scoresaber_total_play_count=scoresaber_total_play_count,
        scoresaber_ranked_play_count=scoresaber_ranked_play_count,
        beatleader_id=beatleader_id,
        beatleader_name=beatleader_name,
        beatleader_country=beatleader_country,
        beatleader_pp=beatleader_pp,
        beatleader_rank_global=beatleader_rank_global,
        beatleader_rank_country=beatleader_rank_country,
        beatleader_level=beatleader_level,
        beatleader_experience=beatleader_experience,
        beatleader_prestige=beatleader_prestige,
        beatleader_prestige_icon_url=beatleader_prestige_icon_url,
        beatleader_average_ranked_acc=beatleader_average_ranked_acc,
        beatleader_total_play_count=beatleader_total_play_count,
        beatleader_ranked_play_count=beatleader_ranked_play_count,
        # AccSaber (Reloaded) ランク
        accsaber_reloaded_overall_rank=accsaber_reloaded_overall_rank,
        accsaber_reloaded_overall_rank_country=accsaber_reloaded_overall_rank_country,
        accsaber_reloaded_overall_ap=accsaber_reloaded_overall_ap,
        accsaber_reloaded_overall_ranked_plays=accsaber_reloaded_overall_ranked_plays,
        accsaber_reloaded_overall_total_maps=accsaber_reloaded_overall_total_maps,
        accsaber_reloaded_true_rank=accsaber_reloaded_true_rank,
        accsaber_reloaded_true_rank_country=accsaber_reloaded_true_rank_country,
        accsaber_reloaded_true_ap=accsaber_reloaded_true_ap,
        accsaber_reloaded_true_ranked_plays=accsaber_reloaded_true_ranked_plays,
        accsaber_reloaded_true_total_maps=accsaber_reloaded_true_total_maps,
        accsaber_reloaded_standard_rank=accsaber_reloaded_standard_rank,
        accsaber_reloaded_standard_rank_country=accsaber_reloaded_standard_rank_country,
        accsaber_reloaded_standard_ap=accsaber_reloaded_standard_ap,
        accsaber_reloaded_standard_ranked_plays=accsaber_reloaded_standard_ranked_plays,
        accsaber_reloaded_standard_total_maps=accsaber_reloaded_standard_total_maps,
        accsaber_reloaded_tech_rank=accsaber_reloaded_tech_rank,
        accsaber_reloaded_tech_rank_country=accsaber_reloaded_tech_rank_country,
        accsaber_reloaded_tech_ap=accsaber_reloaded_tech_ap,
        accsaber_reloaded_tech_ranked_plays=accsaber_reloaded_tech_ranked_plays,
        accsaber_reloaded_tech_total_maps=accsaber_reloaded_tech_total_maps,
        accsaber_reloaded_overall_avg_acc=accsaber_reloaded_overall_avg_acc,
        accsaber_reloaded_true_avg_acc=accsaber_reloaded_true_avg_acc,
        accsaber_reloaded_standard_avg_acc=accsaber_reloaded_standard_avg_acc,
        accsaber_reloaded_tech_avg_acc=accsaber_reloaded_tech_avg_acc,
        accsaber_reloaded_xp=accsaber_reloaded_xp,
        accsaber_reloaded_xp_level=accsaber_reloaded_xp_level,
        accsaber_reloaded_xp_rank=accsaber_reloaded_xp_rank,
        accsaber_reloaded_xp_rank_country=accsaber_reloaded_xp_rank_country,
        accsaber_reloaded_milestones_completed=accsaber_reloaded_milestones_completed,
        accsaber_reloaded_milestones_total=accsaber_reloaded_milestones_total,
        star_stats=star_stats,
        beatleader_star_stats=beatleader_star_stats,
    )
    print("10.1 スナップショットオブジェクト構築完了。")
    _step(0.90, "Saving snapshot...")
    snapshot.warnings = _warnings

    if snapshot_dir is not None:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        name = f"{steam_id}_{now:%Y%m%d-%H%M%S}.json"
        path = snapshot.save(snapshot_dir / name)
    else:
        path = snapshot.save()

    print(f"10.2 スナップショット保存完了: {path}")
    _step(1.0, f"Done: {path.name}")
    return snapshot


# --- original CLI entrypoint retained ---


def main() -> None:
    """コマンドラインから指定 SteamID のスナップショットを作成する。"""
    import argparse
    # pylint: disable=import-outside-toplevel
    parser = argparse.ArgumentParser(description="Create a snapshot for given SteamID using cached rankings.")
    parser.add_argument("steam_id", help="SteamID (or key used in players_index.json)")
    # Optional argument to specify snapshot directory
    parser.add_argument(
        "--snapshot-dir",
        dest="snapshot_dir",
        help="Directory to save snapshot JSON (default: snapshots under project root)",
    )
    args = parser.parse_args()

    snapshot_dir: Optional[Path]
    if args.snapshot_dir:
        snapshot_dir = Path(args.snapshot_dir)
    else:
        snapshot_dir = None

    def _cli_progress(message: str, fraction: float) -> None:
        """CLI 用の進捗表示。"""
        bar_width = 40
        filled = int(bar_width * fraction)
        bar = "#" * filled + "-" * (bar_width - filled)
        percent = int(fraction * 100)
        print(f"\r[{bar}] {percent:3d}% {message:40s}", end="", flush=True)

    # Create snapshot
    snapshot = create_snapshot_for_steam_id(args.steam_id, snapshot_dir=snapshot_dir, progress=_cli_progress)
    print()  # newline after progress bar
    print("Snapshot created:")
    print(json.dumps(asdict(snapshot), ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
