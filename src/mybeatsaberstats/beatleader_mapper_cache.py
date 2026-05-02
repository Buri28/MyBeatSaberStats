from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

import requests

from .collector.beatleader import _get_beatleader_leaderboards_ranked, _get_beatleader_player_scores
from .snapshot import BASE_DIR


_CACHE_DIR = BASE_DIR / "cache"


def _mapper_cache_path(steam_id: str) -> Path:
    return _CACHE_DIR / f"beatleader_mapper_played_{steam_id}.json"


def _player_scores_cache_path(steam_id: str) -> Path:
    return _CACHE_DIR / f"beatleader_player_scores_{steam_id}.json"


def _ranked_maps_cache_path() -> Path:
    return _CACHE_DIR / "beatleader_ranked_maps.json"


def _parse_utc_z(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z"))
    except ValueError:
        return None


def _now_utc_z() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def load_bl_mapper_played_cache(steam_id: str) -> Optional[dict]:
    path = _mapper_cache_path(steam_id)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    counts_raw = raw.get("counts")
    if not isinstance(counts_raw, dict):
        return None
    counts: Dict[str, int] = {}
    for mapper, count in counts_raw.items():
        mapper_name = str(mapper or "").strip()
        if not mapper_name:
            continue
        try:
            count_value = int(count)
        except (TypeError, ValueError):
            continue
        if count_value > 0:
            counts[mapper_name] = count_value
    return {
        "fetched_at": str(raw.get("fetched_at") or ""),
        "steam_id": str(raw.get("steam_id") or steam_id),
        "source": str(raw.get("source") or "beatleader_player_scores_best"),
        "total_ranked_played_maps": int(raw.get("total_ranked_played_maps") or sum(counts.values())),
        "unique_mappers": int(raw.get("unique_mappers") or len(counts)),
        "unknown_maps": int(raw.get("unknown_maps") or 0),
        "counts": counts,
    }


def _save_bl_mapper_played_cache(steam_id: str, payload: dict) -> dict:
    normalized = {
        "fetched_at": str(payload.get("fetched_at") or _now_utc_z()),
        "steam_id": steam_id,
        "source": str(payload.get("source") or "beatleader_player_scores_best"),
        "total_ranked_played_maps": int(payload.get("total_ranked_played_maps") or 0),
        "unique_mappers": int(payload.get("unique_mappers") or 0),
        "unknown_maps": int(payload.get("unknown_maps") or 0),
        "counts": dict(sorted((payload.get("counts") or {}).items(), key=lambda kv: (-int(kv[1]), kv[0].lower()))),
    }
    path = _mapper_cache_path(steam_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def _load_ranked_mapper_index() -> Dict[str, str]:
    path = _ranked_maps_cache_path()
    if not path.exists():
        raise FileNotFoundError("BeatLeader ranked maps cache not found.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("Failed to read BeatLeader ranked maps cache.") from exc

    mapper_by_leaderboard_id: Dict[str, str] = {}
    for page in raw.get("pages", []):
        page_data = page.get("data", {}) if isinstance(page, dict) else {}
        items = page_data.get("data", []) if isinstance(page_data, dict) else []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            leaderboard_id = str(item.get("id") or "").strip()
            if not leaderboard_id:
                continue
            song = item.get("song") or {}
            mapper_by_leaderboard_id[leaderboard_id] = str(song.get("mapper") or "").strip()
    return mapper_by_leaderboard_id


def _load_player_score_entries(steam_id: str) -> Dict[str, dict]:
    path = _player_scores_cache_path(steam_id)
    if not path.exists():
        raise FileNotFoundError("BeatLeader player scores cache not found.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("Failed to read BeatLeader player scores cache.") from exc
    scores = raw.get("scores")
    if not isinstance(scores, dict):
        raise RuntimeError("BeatLeader player scores cache is invalid.")
    return scores


def _mapper_from_score_entry(score_entry: object, ranked_mapper: str = "") -> str:
    if isinstance(score_entry, dict):
        leaderboard = score_entry.get("leaderboard") or score_entry
        if isinstance(leaderboard, dict):
            song = leaderboard.get("song") or {}
            if isinstance(song, dict):
                mapper_name = str(song.get("mapper") or "").strip()
                if mapper_name:
                    return mapper_name
    return str(ranked_mapper or "").strip()


def build_bl_mapper_played_cache_from_local(
    steam_id: str,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    if progress is not None:
        progress(0, 3, "Loading BeatLeader ranked maps cache...")
    mapper_index = _load_ranked_mapper_index()

    if progress is not None:
        progress(1, 3, "Loading BeatLeader player scores cache...")
    score_entries = _load_player_score_entries(steam_id)

    counts: Dict[str, int] = {}
    unknown_maps = 0
    if progress is not None:
        progress(2, 3, "Aggregating mapper counts...")
    for leaderboard_id, score_entry in score_entries.items():
        mapper_name = _mapper_from_score_entry(score_entry, mapper_index.get(str(leaderboard_id), ""))
        if not mapper_name:
            unknown_maps += 1
            continue
        counts[mapper_name] = counts.get(mapper_name, 0) + 1

    payload = {
        "fetched_at": _now_utc_z(),
        "steam_id": steam_id,
        "source": "beatleader_player_scores_best",
        "total_played_maps": len(score_entries),
        "total_ranked_played_maps": len(score_entries),
        "unique_mappers": len(counts),
        "unknown_maps": unknown_maps,
        "counts": counts,
    }
    if progress is not None:
        progress(3, 3, "Done")
    return _save_bl_mapper_played_cache(steam_id, payload)


def refresh_bl_mapper_played_cache(
    steam_id: str,
    refresh_mode: str,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    cache_data = load_bl_mapper_played_cache(steam_id)
    fetch_until: Optional[datetime] = None
    if refresh_mode == "since" and cache_data is not None:
        fetch_until = _parse_utc_z(cache_data.get("fetched_at"))
    elif refresh_mode == "full":
        fetch_until = datetime(2000, 1, 1)

    session = requests.Session()
    try:
        if progress is not None:
            progress(0, 3, "Refreshing BeatLeader ranked maps cache...")

        def _ranked_progress(page: int, max_pages: Optional[int]) -> None:
            if progress is None:
                return
            if max_pages and max_pages > 0:
                progress(0, 3, f"Refreshing BeatLeader ranked maps... page {page}/{max_pages}")
            else:
                progress(0, 3, f"Refreshing BeatLeader ranked maps... page {page}/?")

        _get_beatleader_leaderboards_ranked(session, progress=_ranked_progress, fetch_until=fetch_until)

        if progress is not None:
            progress(1, 3, "Refreshing BeatLeader player scores cache...")

        def _scores_progress(page: int, max_pages: Optional[int]) -> None:
            if progress is None:
                return
            if max_pages and max_pages > 0:
                progress(1, 3, f"Refreshing BeatLeader player scores... page {page}/{max_pages}")
            else:
                progress(1, 3, f"Refreshing BeatLeader player scores... page {page}/?")

        _get_beatleader_player_scores(steam_id, session, progress=_scores_progress, fetch_until=fetch_until)
    finally:
        session.close()

    return build_bl_mapper_played_cache_from_local(steam_id, progress=progress)