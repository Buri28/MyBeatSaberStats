from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from .api_error_log import log_api_failure
from .snapshot import BASE_DIR

BASE_URL = "https://api.accsaberreloaded.com/v1"

# AccSaber Reloaded カテゴリ UUID（/v1/categories で確認済み）
CATEGORY_IDS: Dict[str, str] = {
    "overall":  "b0000000-0000-0000-0000-000000000005",
    "true":     "b0000000-0000-0000-0000-000000000001",
    "standard": "b0000000-0000-0000-0000-000000000002",
    "tech":     "b0000000-0000-0000-0000-000000000003",
}

# overall は maps エンドポイントでは集計しない（true+standard+tech の合計で算出）
_MAP_COUNT_CATEGORY_IDS: Dict[str, str] = {k: v for k, v in CATEGORY_IDS.items() if k != "overall"}

_PAGE_SIZE = 50

_MAP_COUNTS_CACHE_FILE: Path = BASE_DIR / "cache" / "accsaber_reloaded_map_counts.json"

# AccSaber Reloaded 全マップデータのキャッシュファイル
_ALL_MAPS_CACHE_FILE: Path = BASE_DIR / "cache" / "accsaber_reloaded_maps.json"


def is_pending_difficulty(diff: dict) -> bool:
    """AccSaber Reloaded difficulty が pending 扱いかを返す。"""
    if not isinstance(diff, dict):
        return False

    status = str(diff.get("status") or "").upper()
    if status in {"QUEUE", "PENDING"}:
        return True
    return False


def is_active_difficulty(diff: dict) -> bool:
    """AccSaber Reloaded difficulty が有効譜面かを返す。

    新 API では difficulty.active が省略されることがあるため、
    active が無い場合は status から判定する。
    """
    if not isinstance(diff, dict):
        return False

    active = diff.get("active")
    if isinstance(active, bool):
        return active

    status = str(diff.get("status") or "").upper()
    if status in {"RANKED", "APPROVED", "PASSED"}:
        return True
    if status in {"QUEUE", "PENDING"}:
        return False

    return active is not None and bool(active)


def _count_non_pending_map_counts(all_maps: List[Dict]) -> Dict[str, int]:
    """全マップ一覧から pending を除いたカテゴリ別譜面数を返す。"""
    uuid_to_cat: Dict[str, str] = {v: k for k, v in _MAP_COUNT_CATEGORY_IDS.items()}
    counts: Dict[str, int] = {cat: 0 for cat in _MAP_COUNT_CATEGORY_IDS}

    for song in all_maps:
        if not isinstance(song, dict):
            continue
        for diff in song.get("difficulties", []):
            if not isinstance(diff, dict):
                continue
            if not is_active_difficulty(diff):
                continue
            if is_pending_difficulty(diff):
                continue
            cat = uuid_to_cat.get(diff.get("categoryId", ""))
            if cat:
                counts[cat] += 1

    overall_parts = [counts[k] for k in ("true", "standard", "tech") if counts.get(k, 0) > 0]
    if overall_parts:
        counts["overall"] = sum(overall_parts)
    return counts


def _load_map_counts_file_cache() -> Dict[str, Dict]:
    """ファイルキャッシュから前回の総譜面数を読み込む。"""
    payload = _load_map_counts_cache_payload()
    if not isinstance(payload, dict):
        return {}

    result: Dict[str, Dict] = {}
    for k in ("true", "standard", "tech"):
        entry = payload.get(k)
        if isinstance(entry, dict):
            count = entry.get("count")
            if isinstance(count, (int, float)) and count > 0:
                result[k] = {"count": int(count)}
    return result


def _load_map_counts_cache_payload() -> Dict:
    """総譜面数キャッシュの生データを返す。"""
    try:
        data = json.loads(_MAP_COUNTS_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_map_counts_file_cache(per_cat: Dict[str, Dict]) -> None:
    """総譜面数をファイルキャッシュに保存する。"""
    try:
        now_z = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data: Dict = {"fetched_at": now_z}
        data.update(per_cat)
        _MAP_COUNTS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MAP_COUNTS_CACHE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


def _save_map_counts_from_all_maps(all_maps: List[Dict]) -> None:
    """全マップ一覧から総譜面数キャッシュを同期更新する。"""
    raw_counts = {
        k: v
        for k, v in _count_non_pending_map_counts(all_maps).items()
        if k != "overall"
    }
    per_cat: Dict[str, Dict] = {
        cat: {"count": cnt}
        for cat, cnt in raw_counts.items()
        if cnt > 0
    }
    if per_cat:
        _save_map_counts_file_cache(per_cat)


def _load_all_maps_cache_payload() -> Dict:
    """全マップキャッシュの生データを返す。"""
    try:
        data = json.loads(_ALL_MAPS_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_all_maps_cache(all_maps: List[Dict], fetched_at: Optional[str] = None) -> None:
    """全マップキャッシュを保存し、総譜面数キャッシュも同期する。"""
    now_z = fetched_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {"fetched_at": now_z, "maps": all_maps}
    _ALL_MAPS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ALL_MAPS_CACHE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _save_map_counts_from_all_maps(all_maps)


def _parse_utc_timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(normalized).astimezone(timezone.utc)
    except ValueError:
        return None


def _count_non_pending_batch_difficulties(difficulties: List[Dict]) -> Dict[str, int]:
    """batch difficulties から pending を除いたカテゴリ別譜面数を返す。"""
    uuid_to_cat: Dict[str, str] = {v: k for k, v in _MAP_COUNT_CATEGORY_IDS.items()}
    counts: Dict[str, int] = {cat: 0 for cat in _MAP_COUNT_CATEGORY_IDS}

    for diff in difficulties:
        if not isinstance(diff, dict):
            continue
        if not is_active_difficulty(diff):
            continue
        if is_pending_difficulty(diff):
            continue
        cat = uuid_to_cat.get(str(diff.get("categoryId") or ""))
        if cat:
            counts[cat] += 1
    return counts


def _fetch_batch_count_deltas_since(
    since: datetime,
    session: requests.Session,
) -> Dict[str, int]:
    """指定日時以後に公開された batch の譜面数差分を返す。"""
    counts: Dict[str, int] = {cat: 0 for cat in _MAP_COUNT_CATEGORY_IDS}
    page = 0
    page_size = 20

    while True:
        resp = session.get(
            f"{BASE_URL}/batches",
            params={"page": page, "size": page_size},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content", [])
        if not isinstance(content, list) or not content:
            break

        reached_older_batch = False
        for batch in content:
            if not isinstance(batch, dict):
                continue
            released_at = _parse_utc_timestamp(batch.get("releasedAt") or batch.get("createdAt"))
            if released_at is not None and released_at <= since:
                reached_older_batch = True
                continue

            diff_counts = _count_non_pending_batch_difficulties(batch.get("difficulties", []))
            for cat, value in diff_counts.items():
                counts[cat] += value

        if reached_older_batch or data.get("last", True):
            break
        page += 1

    return counts


def _iter_recent_batches_since(
    since: datetime,
    session: requests.Session,
) -> List[Dict]:
    """指定日時以後に公開された batch 一覧を新しい順で返す。"""
    batches: List[Dict] = []
    page = 0
    page_size = 20

    while True:
        resp = session.get(
            f"{BASE_URL}/batches",
            params={"page": page, "size": page_size},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content", [])
        if not isinstance(content, list) or not content:
            break

        reached_older_batch = False
        for batch in content:
            if not isinstance(batch, dict):
                continue
            released_at = _parse_utc_timestamp(batch.get("releasedAt") or batch.get("createdAt"))
            if released_at is not None and released_at <= since:
                reached_older_batch = True
                continue
            batches.append(batch)

        if reached_older_batch or data.get("last", True):
            break
        page += 1

    return batches


def _fetch_map_by_code(
    beatsaver_code: str,
    session: requests.Session,
) -> Optional[Dict]:
    """map by code endpoint から full map レコードを取得する。"""
    code = str(beatsaver_code or "").strip()
    if not code:
        return None
    resp = session.get(f"{BASE_URL}/maps/by-code/{code}", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else None


def _merge_all_maps_with_recent_batches(
    session: requests.Session,
) -> Optional[List[Dict]]:
    """全マップキャッシュを recent batch で前進させる。"""
    payload = _load_all_maps_cache_payload()
    if not isinstance(payload, dict):
        return None

    cached_maps = payload.get("maps")
    fetched_at = _parse_utc_timestamp(payload.get("fetched_at"))
    if not isinstance(cached_maps, list) or fetched_at is None:
        return None

    recent_batches = _iter_recent_batches_since(fetched_at, session)
    if not recent_batches:
        return cached_maps

    recent_codes: List[str] = []
    seen_codes: set[str] = set()
    for batch in recent_batches:
        for diff in batch.get("difficulties", []):
            if not isinstance(diff, dict):
                continue
            code = str(diff.get("beatsaverCode") or "").strip()
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            recent_codes.append(code)

    if not recent_codes:
        return cached_maps

    merged_by_map_id: Dict[str, Dict] = {}
    ordered_keys: List[str] = []
    for song in cached_maps:
        if not isinstance(song, dict):
            continue
        key = str(song.get("id") or song.get("mapId") or song.get("beatsaverCode") or "").strip()
        if not key:
            continue
        if key not in merged_by_map_id:
            ordered_keys.append(key)
        merged_by_map_id[key] = song

    changed = False
    for code in recent_codes:
        full_map = _fetch_map_by_code(code, session)
        if not isinstance(full_map, dict):
            continue
        key = str(full_map.get("id") or full_map.get("mapId") or full_map.get("beatsaverCode") or "").strip()
        if not key:
            continue
        if key not in merged_by_map_id:
            ordered_keys.append(key)
        if merged_by_map_id.get(key) != full_map:
            merged_by_map_id[key] = full_map
            changed = True

    merged_maps = [merged_by_map_id[key] for key in ordered_keys if key in merged_by_map_id]
    if changed:
        _save_all_maps_cache(merged_maps)
    return merged_maps


def load_all_maps_with_recent_batch_fallback(
    session: Optional[requests.Session] = None,
) -> Optional[List[Dict]]:
    """全マップキャッシュを読み込み、必要なら recent batch で補完する。"""
    cached_maps = load_all_maps_from_cache()
    if cached_maps is None:
        return None
    if session is None:
        session = requests.Session()
    try:
        merged_maps = _merge_all_maps_with_recent_batches(session)
        if isinstance(merged_maps, list):
            return merged_maps
    except Exception as exc:  # noqa: BLE001
        log_api_failure(
            "accsaber_reloaded",
            "load_all_maps_with_recent_batch_fallback",
            "recent batch fallback failed while updating full map cache",
            exc,
        )
    return cached_maps


def _merge_file_cache_with_recent_batches(
    session: requests.Session,
) -> Optional[Dict[str, int]]:
    """/maps 失敗時に batch API から総譜面数キャッシュを前進させる。"""
    payload = _load_map_counts_cache_payload()
    if not isinstance(payload, dict):
        return None

    fetched_at = _parse_utc_timestamp(payload.get("fetched_at"))
    file_cache = _load_map_counts_file_cache()
    if fetched_at is None or not file_cache:
        return None

    counts: Dict[str, int] = {k: int(v["count"]) for k, v in file_cache.items()}
    deltas = _fetch_batch_count_deltas_since(fetched_at, session)
    if not any(value > 0 for value in deltas.values()):
        overall_parts = [counts[k] for k in ("true", "standard", "tech") if k in counts]
        if overall_parts:
            counts["overall"] = sum(overall_parts)
        return counts

    merged_per_cat: Dict[str, Dict] = {}
    for cat in _MAP_COUNT_CATEGORY_IDS:
        merged = counts.get(cat, 0) + deltas.get(cat, 0)
        if merged > 0:
            merged_per_cat[cat] = {"count": merged}
            counts[cat] = merged

    if merged_per_cat:
        _save_map_counts_file_cache(merged_per_cat)

    overall_parts = [counts[k] for k in ("true", "standard", "tech") if k in counts]
    if overall_parts:
        counts["overall"] = sum(overall_parts)
    return counts


def fetch_reloaded_map_counts(
    session: Optional[requests.Session] = None,
) -> Dict[str, int]:
    """AccSaber Reloaded の各カテゴリのランク済み難易度数を取得してキャッシュする。

    /v1/maps を全ページ取得し、各 difficulty の categoryId を数える。
    (タイトル単位ではなく難易度単位でカウントするため全ページ走査が必要)
    overall = true + standard + tech の合計で算出。
    取得失敗時はファイルキャッシュの前回値を使用。
    戻り値: {"true": N, "standard": N, "tech": N, "overall": N}
    """
    if session is None:
        session = requests.Session()

    # UUID → カテゴリ名 の逆引き辞書
    _uuid_to_cat: Dict[str, str] = {v: k for k, v in _MAP_COUNT_CATEGORY_IDS.items()}

    try:
        page = 0
        page_size = 50
        all_maps: List[Dict] = []
        while True:
            resp = session.get(
                f"{BASE_URL}/maps",
                params={"page": page, "size": page_size},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            all_maps.extend(data.get("content", []))
            if data.get("last", True):
                break
            page += 1

        _save_map_counts_from_all_maps(all_maps)

        return _count_non_pending_map_counts(all_maps)

    except Exception as exc:  # noqa: BLE001
        log_api_failure("accsaber_reloaded", "fetch_reloaded_map_counts", "request failed while fetching /maps pages", exc)
        try:
            batch_counts = _merge_file_cache_with_recent_batches(session)
            if batch_counts:
                return batch_counts
        except Exception as batch_exc:  # noqa: BLE001
            log_api_failure(
                "accsaber_reloaded",
                "fetch_reloaded_map_counts",
                "batch fallback failed while updating cached counts",
                batch_exc,
            )
        # API 失敗時はファイルキャッシュにフォールバック
        return get_reloaded_map_counts_from_cache()


def get_reloaded_map_counts_from_cache() -> Dict[str, int]:
    """ファイルキャッシュから AccSaber Reloaded の総譜面数を返す。API は叩かない。

    戻り値: {"true": 109, "standard": ..., "tech": ..., "overall": ...}
    存在しないカテゴリのキーは含まれない。
    """
    file_cache = _load_map_counts_file_cache()
    if file_cache:
        counts: Dict[str, int] = {k: v["count"] for k, v in file_cache.items()}
    else:
        all_maps = load_all_maps_from_cache()
        if isinstance(all_maps, list) and all_maps:
            return _count_non_pending_map_counts(all_maps)
        counts = {}

    overall_parts = [counts[k] for k in ("true", "standard", "tech") if k in counts]
    if overall_parts:
        counts["overall"] = sum(overall_parts)
    return counts


@dataclass
class AccSaberReloadedPlayer:
    player_id: str
    name: str
    country: str
    ap: float
    average_acc: float
    ranked_plays: int
    rank_global: int
    rank_country: int


def _search_in_leaderboard(
    category_uuid: str,
    player_id: str,
    country: Optional[str],
    session: requests.Session,
) -> Optional[AccSaberReloadedPlayer]:
    """指定カテゴリのリーダーボードからプレイヤーを検索する。

    country を指定すると国別エンドポイント（/leaderboards/{uuid}/country/{cc}）を使い、
    ページ数を大幅に削減できる。見つからない場合は None を返す。
    レスポンスの `ranking` フィールドには全体順位が、`countryRanking` には国内順位が入る。
    """
    if country:
        url = f"{BASE_URL}/leaderboards/{category_uuid}/country/{country.upper()}"
    else:
        url = f"{BASE_URL}/leaderboards/{category_uuid}"

    page = 0
    while True:
        try:
            resp = session.get(url, params={"page": page, "size": _PAGE_SIZE}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log_api_failure("accsaber_reloaded", "_search_in_leaderboard", f"request failed url={url} category_uuid={category_uuid} player_id={player_id} country={country} page={page}", exc)
            break

        content = data.get("content")
        if not isinstance(content, list) or not content:
            break

        for entry in content:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("userId", "")) == player_id:
                try:
                    return AccSaberReloadedPlayer(
                        player_id=player_id,
                        name=str(entry.get("userName", "")),
                        country=str(entry.get("country", "")),
                        ap=float(entry.get("ap", 0.0)),
                        average_acc=float(entry.get("averageAcc", 0.0)),
                        ranked_plays=int(entry.get("rankedPlays", 0)),
                        rank_global=int(entry.get("ranking", 0)),
                        rank_country=int(entry.get("countryRanking", 0)),
                    )
                except (TypeError, ValueError):
                    return None

        # 最終ページなら終了
        if data.get("last", True):
            break
        page += 1

    return None


def fetch_player_all_categories(
    player_id: str,
    country: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> Dict[str, Optional[AccSaberReloadedPlayer]]:
    """指定プレイヤーの AccSaber Reloaded 全カテゴリのランク情報を取得する。

    戻り値: {"overall": ..., "true": ..., "standard": ..., "tech": ...}
    各カテゴリで見つからない / API エラーの場合は None。
    """
    if not player_id:
        return {cat: None for cat in CATEGORY_IDS}

    if session is None:
        session = requests.Session()

    result: Dict[str, Optional[AccSaberReloadedPlayer]] = {}
    for category, uuid in CATEGORY_IDS.items():
        try:
            result[category] = _search_in_leaderboard(uuid, player_id, country, session)
        except Exception:  # noqa: BLE001
            result[category] = None

    return result


# ---------------------------------------------------------------------------
# XP ランキング
# ---------------------------------------------------------------------------

@dataclass
class AccSaberReloadedXP:
    xp: float
    level: int
    rank_global: int
    rank_country: int


def fetch_player_xp(
    player_id: str,
    country: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> Optional[AccSaberReloadedXP]:
    """プレイヤーの XP・レベル・XP ランクを取得する。

    /v1/leaderboards/xp から country フィルターでページを走査し、
    対象プレイヤーを見つけたら AccSaberReloadedXP を返す。
    見つからない場合は None。
    """
    if not player_id:
        return None

    if session is None:
        session = requests.Session()

    params: Dict = {"size": _PAGE_SIZE}
    if country:
        params["country"] = country.upper()

    url = f"{BASE_URL}/leaderboards/xp"
    page = 0
    while True:
        try:
            resp = session.get(url, params={**params, "page": page}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log_api_failure("accsaber_reloaded", "fetch_player_xp", f"request failed url={url} player_id={player_id} country={country} page={page}", exc)
            break

        content = data.get("content")
        if not isinstance(content, list) or not content:
            break

        for entry in content:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("userId", "")) == player_id:
                try:
                    return AccSaberReloadedXP(
                        xp=float(entry.get("totalXp", 0.0)),
                        level=int(entry.get("level", 0)),
                        rank_global=int(entry.get("ranking", 0)),
                        rank_country=int(entry.get("countryRanking", 0)),
                    )
                except (TypeError, ValueError):
                    return None

        if data.get("last", True):
            break
        page += 1

    return None


# ---------------------------------------------------------------------------
# 未プレイ抽出用
# ---------------------------------------------------------------------------

# AccSaber Reloaded の difficulty 名 → Beat Saber bplist 形式の難易度名
_RL_DIFF_TO_BS: Dict[str, str] = {
    "EASY":         "Easy",
    "NORMAL":       "Normal",
    "HARD":         "Hard",
    "EXPERT":       "Expert",
    "EXPERT_PLUS":  "ExpertPlus",
}


def fetch_all_maps_full(
    session: Optional[requests.Session] = None,
    on_progress=None,
) -> List[Dict]:
    """AccSaber Reloaded の全マップ情報を全ページ取得して返す。

    戻り値: content 配列の要素をそのまま結合したリスト。
    各要素には songHash, songName, beatsaverCode, difficulties[] が含まれる。

    on_progress(current_page: int, total_pages: int) が指定されていれば各ページで呼び出す。
    RuntimeError を投げると取得を中断できる（キャンセル用）。
    """
    if session is None:
        session = requests.Session()

    all_maps: List[Dict] = []
    page = 0
    total_pages: Optional[int] = None

    while True:
        try:
            resp = session.get(
                f"{BASE_URL}/maps",
                params={"page": page, "size": _PAGE_SIZE},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log_api_failure("accsaber_reloaded", "fetch_all_maps_full", f"request failed page={page}", exc)
            raise

        all_maps.extend(data.get("content", []))

        if total_pages is None:
            total_pages = data.get("totalPages", 1)

        if on_progress is not None:
            on_progress(page + 1, total_pages)

        if data.get("last", True):
            break
        page += 1

    return all_maps


def fetch_player_scored_diff_ids(
    player_id: str,
    session: Optional[requests.Session] = None,
) -> Dict[str, set]:
    """AccSaber Reloaded でプレイヤーがスコアを持つ難易度 UUID をカテゴリ別に取得する。

    Returns:
        {"true": {uuid, ...}, "standard": {uuid, ...}, "tech": {uuid, ...}}
    空のセットも含む辞書を返す。API エラー時も同様に空辞書を返す。
    """
    if not player_id:
        return {}

    if session is None:
        session = requests.Session()

    _uuid_to_cat = {v: k for k, v in CATEGORY_IDS.items()}
    result: Dict[str, set] = {"true": set(), "standard": set(), "tech": set()}

    page = 0
    while True:
        try:
            resp = session.get(
                f"{BASE_URL}/users/{player_id}/scores",
                params={"page": page, "size": _PAGE_SIZE},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log_api_failure("accsaber_reloaded", "fetch_player_scored_diff_ids", f"request failed player_id={player_id} page={page}", exc)
            break

        content = data.get("content", [])
        if not content:
            break

        for score in content:
            diff_id = score.get("mapDifficultyId")
            cat = _uuid_to_cat.get(score.get("categoryId", ""))
            if diff_id and cat and cat in result:
                result[cat].add(diff_id)

        if data.get("last", True):
            break
        page += 1

    return result


# ──────────────────────────────────────────────────────────────────────────────
# AccSaber Reloaded 全マップデータキャッシュ
# ──────────────────────────────────────────────────────────────────────────────

def fetch_and_save_all_maps_cache(
    session: Optional[requests.Session] = None,
    on_progress=None,
) -> None:
    """AccSaber Reloaded の全マップを取得してキャッシュファイルに保存する。

    Snapshot 取得時に呼び出す。プレイリスト作成時は load_all_maps_from_cache() で読む。
    accsaber_reloaded_maps.json に以下の形式で保存する::

        {
            "fetched_at": "2026-04-07T12:00:00Z",
            "maps": [...]
        }
    """
    try:
        all_maps = fetch_all_maps_full(session=session, on_progress=on_progress)
        _save_all_maps_cache(all_maps)
    except Exception as exc:  # noqa: BLE001
        log_api_failure("accsaber_reloaded", "fetch_and_save_all_maps_cache", f"cache refresh failed path={_ALL_MAPS_CACHE_FILE}", exc)
        try:
            if session is None:
                session = requests.Session()
            _merge_all_maps_with_recent_batches(session)
        except Exception as batch_exc:  # noqa: BLE001
            log_api_failure(
                "accsaber_reloaded",
                "fetch_and_save_all_maps_cache",
                "recent batch fallback failed while updating full map cache",
                batch_exc,
            )


def load_all_maps_from_cache() -> Optional[List[Dict]]:
    """accsaber_reloaded_maps.json キャッシュを読み込んで全マップリストを返す。

    ファイルが存在しないか形式が不正な場合は None を返す。
    """
    try:
        data = json.loads(_ALL_MAPS_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("maps"), list):
            return data["maps"]
    except Exception:  # noqa: BLE001
        pass
    return None


# ──────────────────────────────────────────────────────────────────────────────
# AccSaber Reloaded プレイヤースコアキャッシュ
# ──────────────────────────────────────────────────────────────────────────────

def _rl_player_scores_cache_path(player_id: str) -> Path:
    return BASE_DIR / "cache" / f"accsaber_reloaded_player_scores_{player_id}.json"


def fetch_and_save_player_scores_cache(
    player_id: str,
    session: Optional[requests.Session] = None,
) -> None:
    """AccSaber Reloaded プレイヤースコアを全ページ取得してキャッシュファイルに保存する。

    Snapshot 取得時に呼び出す。プレイリスト作成時は load_player_scores_from_cache() で読む。
    """
    if not player_id:
        return
    if session is None:
        session = requests.Session()
    all_scores: List[Dict] = []
    try:
        page = 0
        while True:
            resp = session.get(
                f"{BASE_URL}/users/{player_id}/scores",
                params={"page": page, "size": _PAGE_SIZE},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            all_scores.extend(data.get("content", []))
            if data.get("last", True):
                break
            page += 1
        now_z = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cache_data = {"fetched_at": now_z, "scores": all_scores}
        path = _rl_player_scores_cache_path(player_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def load_player_scores_from_cache(player_id: str) -> Optional[List[Dict]]:
    """キャッシュから AccSaber Reloaded プレイヤースコアリストを返す。なければ None。"""
    try:
        path = _rl_player_scores_cache_path(player_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("scores"), list):
            return data["scores"]
    except Exception:  # noqa: BLE001
        pass
    return None


def compute_effective_played_counts_from_cache(player_id: str) -> Dict[str, int]:
    """現在の AccSaber Reloaded 対象譜面に対する既プレイ数を cache から再計算する。

    Playlist 画面と同様に、以下のいずれかにスコアがあれば既プレイとみなす。
    - AccSaber Reloaded player scores (mapDifficultyId)
    - BeatLeader player scores (blLeaderboardId)
    - ScoreSaber player scores (ssLeaderboardId)

    戻り値は {"overall": N, "true": N, "standard": N, "tech": N}。
    全マップ cache が無い場合は空 dict を返す。
    """

    if not player_id:
        return {}

    all_maps = load_all_maps_from_cache()
    if not isinstance(all_maps, list) or not all_maps:
        return {}

    rl_scores = load_player_scores_from_cache(player_id) or []
    rl_played_diff_ids: set[str] = set()
    for score in rl_scores:
        if not isinstance(score, dict):
            continue
        diff_id = score.get("mapDifficultyId")
        try:
            ap = float(score.get("ap") or 0)
        except (TypeError, ValueError):
            ap = 0.0
        if diff_id and ap > 0:
            rl_played_diff_ids.add(str(diff_id))

    def _load_score_dict(path: Path) -> Dict[str, dict]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            scores = data.get("scores")
            return scores if isinstance(scores, dict) else {}
        except Exception:
            return {}

    bl_scores = _load_score_dict(BASE_DIR / "cache" / f"beatleader_player_scores_{player_id}.json")
    ss_scores = _load_score_dict(BASE_DIR / "cache" / f"scoresaber_player_scores_{player_id}.json")

    def _has_bl_score(leaderboard_id: str) -> bool:
        if not leaderboard_id:
            return False
        entry = bl_scores.get(str(leaderboard_id)) or {}
        try:
            return float(entry.get("pp") or 0) > 0 or int(entry.get("baseScore") or 0) > 0
        except (TypeError, ValueError):
            return False

    def _has_ss_score(leaderboard_id: str) -> bool:
        if not leaderboard_id:
            return False
        entry = ss_scores.get(str(leaderboard_id)) or {}
        score = entry.get("score") if isinstance(entry, dict) else None
        if not isinstance(score, dict):
            score = entry if isinstance(entry, dict) else {}
        try:
            return float(score.get("pp") or 0) > 0 or int(score.get("baseScore") or score.get("modifiedScore") or score.get("score") or 0) > 0
        except (TypeError, ValueError):
            return False

    uuid_to_cat = {v: k for k, v in CATEGORY_IDS.items() if k != "overall"}
    counts: Dict[str, int] = {"true": 0, "standard": 0, "tech": 0}
    seen_diff_ids: set[str] = set()

    for song in all_maps:
        if not isinstance(song, dict):
            continue
        for diff in song.get("difficulties") or []:
            if not isinstance(diff, dict):
                continue
            if not is_active_difficulty(diff):
                continue
            if is_pending_difficulty(diff):
                continue
            cat = uuid_to_cat.get(str(diff.get("categoryId") or ""))
            if not cat:
                continue

            diff_id = str(diff.get("id") or "")
            if not diff_id or diff_id in seen_diff_ids:
                continue
            seen_diff_ids.add(diff_id)

            played = (
                diff_id in rl_played_diff_ids
                or _has_bl_score(str(diff.get("blLeaderboardId") or ""))
                or _has_ss_score(str(diff.get("ssLeaderboardId") or ""))
            )
            if played:
                counts[cat] += 1

    counts["overall"] = counts.get("true", 0) + counts.get("standard", 0) + counts.get("tech", 0)
    return counts


def build_unplayed_bplist(
    all_maps: List[Dict],
    played_set: set,
    category: str,
    played_bl_ids: Optional[set] = None,
    played_ss_ids: Optional[set] = None,
    played_rl_diff_ids: Optional[set] = None,
) -> Dict:
    """プレイ済み譜面を除いた AccSaber Reloaded カテゴリのプレイリスト (bplist) を構築する。

    played_rl_diff_ids: RL スコア API から取得した difficulty UUID の集合（最高精度）。
      指定時はこれのみでプレイ済み判定し、BL/SS 照合は行わない。
    played_set: {(hash_lower, characteristic, difficulty_bs_name)} の集合。
    played_bl_ids: BeatLeader leaderboard.id の集合（blLeaderboardId と照合）。
    played_ss_ids: ScoreSaber leaderboard.id の集合（ssLeaderboardId と照合）。
    category: "true" / "standard" / "tech"

    リパブリッシュ対応の方針:
      マップが再アップロードされると BeatSaver 上のハッシュが変わり、
      BeatLeader / ScoreSaber に新しいリーダーボードが作成される。
      AccSaber Reloaded は新リーダーボードに切り替える場合があるため、
      上記 3 沿いで検出できないケースが生じる場合がある。
    """
    cat_uuid = _MAP_COUNT_CATEGORY_IDS.get(category)
    if not cat_uuid:
        return {}

    songs_dict: Dict[str, Dict] = {}  # hash → {hash, songName, difficulties:[]}

    for song in all_maps:
        song_hash = song.get("songHash", "").lower()
        if not song_hash:
            continue

        for diff in song.get("difficulties", []):
            if not is_active_difficulty(diff):
                continue
            if diff.get("categoryId") != cat_uuid:
                continue

            characteristic = diff.get("characteristic", "Standard")
            bs_diff = _RL_DIFF_TO_BS.get(diff.get("difficulty", ""))
            if not bs_diff:
                continue

            pending = is_pending_difficulty(diff)

            if played_rl_diff_ids is not None:
                # RL スコア API によるプレイ済み判定（最高精度: BL/SS 照合より優先）
                rl_diff_id = diff.get("id", "")
                if rl_diff_id and rl_diff_id in played_rl_diff_ids:
                    continue
            else:
                # フォールバック: BL/SS キャッシュによるプレイ済み推定
                # 照合優先度: blLeaderboardId > ssLeaderboardId > (hash, char, diff)
                # blLeaderboardId / ssLeaderboardId であればリパブリッシュ後のハッシュ変更にも対応できる
                bl_lb_id = diff.get("blLeaderboardId", "")
                if bl_lb_id and played_bl_ids and bl_lb_id in played_bl_ids:
                    continue
                ss_lb_id = str(diff.get("ssLeaderboardId", "") or "")
                if ss_lb_id and played_ss_ids and ss_lb_id in played_ss_ids:
                    continue
                if (song_hash, characteristic, bs_diff) in played_set:
                    continue

            if song_hash not in songs_dict:
                songs_dict[song_hash] = {
                    "hash": song_hash,
                    "songName": song.get("songName", ""),
                    "difficulties": [],
                }
            songs_dict[song_hash]["difficulties"].append(
                {"characteristic": characteristic, "name": bs_diff}
            )

    cat_label = {"true": "True", "standard": "Standard", "tech": "Tech"}.get(
        category, category.capitalize()
    )
    return {
        "playlistTitle": f"AccSaber Reloaded Unplayed - {cat_label}",
        "playlistAuthor": "MyBeatSaberStats",
        "image": "",
        "songs": list(songs_dict.values()),
    }
