from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

from ..playlist_view import (
    MapEntry,
    _CACHE_DIR,
    _build_bl_hash_index,
    _build_bl_leaderboard_hash_index,
    _build_bl_replay_hash_index,
    _build_bl_score_hash_index,
    _build_ss_hash_index,
    _build_ss_score_hash_index,
    _enrich_entries_with_beatsaver_cache,
    _parse_iso_datetime_to_ts,
    load_accsaber_maps,
    load_accsaber_reloaded_maps,
    load_bl_maps,
    load_ss_maps,
)


def _fetch_bl_leaderboards_by_hash(session: requests.Session, song_hash: str) -> Dict[Tuple[str, str], str]:
    if not song_hash:
        return {}
    try:
        resp = session.get(f"https://api.beatleader.xyz/leaderboards/hash/{song_hash}", timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return {}

    result: Dict[Tuple[str, str], str] = {}
    for item in payload.get("leaderboards") or []:
        diff = item.get("difficulty") or {}
        difficulty = str(diff.get("difficultyName") or "ExpertPlus")
        mode = str(diff.get("modeName") or "Standard")
        leaderboard_id = str(item.get("id") or "")
        if leaderboard_id:
            result[(mode, difficulty)] = leaderboard_id
    return result


def _fetch_bl_top_replay_url(session: requests.Session, leaderboard_id: str, countries: str = "") -> str:
    if not leaderboard_id:
        return ""
    params = {
        "page": 1,
        "count": 1,
        "sortBy": "rank",
        "order": "desc",
    }
    if countries:
        params["countries"] = countries
    try:
        resp = session.get(f"https://api.beatleader.xyz/leaderboard/{leaderboard_id}", params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return ""

    scores = payload.get("scores") or []
    if not scores:
        return ""
    score_id = str(scores[0].get("id") or scores[0].get("originalId") or "").strip()
    if not score_id:
        return ""
    return f"https://replay.beatleader.com/?scoreId={score_id}"


def _normalize_duration_seconds(value: object) -> int:
    if value is None:
        return 0
    try:
        if isinstance(value, (int, float, str)):
            raw_value = value
        else:
            raw_value = str(value)
        seconds = int(round(float(raw_value)))
    except (TypeError, ValueError):
        return 0
    return seconds if seconds > 0 else 0


def load_bplist_maps(
    bplist_path: Path,
    service: str,
    steam_id: Optional[str] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> List[MapEntry]:
    try:
        bplist = json.loads(bplist_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"bplist load error: {exc}") from exc

    songs = bplist.get("songs") or bplist.get("Songs") or []

    if service == "scoresaber":
        ranked = load_ss_maps(steam_id)
        idx = _build_ss_hash_index(ranked)
    elif service == "beatleader":
        ranked = load_bl_maps(steam_id)
        idx = _build_bl_hash_index(ranked)
    elif service == "accsaber_rl":
        ranked = load_accsaber_reloaded_maps(steam_id, "all", on_progress=on_progress)
        idx = _build_ss_hash_index(ranked)
    elif service == "accsaber":
        ranked = load_accsaber_maps(steam_id, "all", on_progress=on_progress)
        idx = _build_ss_hash_index(ranked)
    else:
        idx = {}
        ranked = []

    entries: List[MapEntry] = []
    for song in songs:
        song_hash = (song.get("hash") or "").upper()
        song_name = song.get("songName") or ""
        diffs = song.get("difficulties") or []

        if not diffs:
            for entry in ranked or []:
                if entry.song_hash == song_hash:
                    entries.append(entry)
            if not ranked:
                entries.append(MapEntry(
                    song_name=song_name,
                    song_author="",
                    mapper="",
                    song_hash=song_hash,
                    difficulty="",
                    mode="",
                    stars=0.0,
                    max_pp=0.0,
                    player_pp=0.0,
                    cleared=False,
                    nf_clear=False,
                    player_acc=0.0,
                    player_rank=0,
                    leaderboard_id="",
                    source="open",
                    duration_seconds=0,
                ))
            continue

        for diff in diffs:
            characteristic = diff.get("characteristic") or "Standard"
            diff_name = diff.get("name") or "ExpertPlus"
            key = (song_hash, characteristic, diff_name)
            if key in idx:
                entries.append(idx[key])
            else:
                entries.append(MapEntry(
                    song_name=song_name,
                    song_author="",
                    mapper="",
                    song_hash=song_hash,
                    difficulty=diff_name,
                    mode=characteristic,
                    stars=0.0,
                    max_pp=0.0,
                    player_pp=0.0,
                    cleared=False,
                    nf_clear=False,
                    player_acc=0.0,
                    player_rank=0,
                    leaderboard_id="",
                    source="open",
                    duration_seconds=0,
                ))

    return _enrich_entries_with_beatsaver_cache(entries)


def load_beatsaver_maps(
    steam_id: Optional[str] = None,
    query: str = "",
    days: int = 7,
    min_rating: float = 0.0,
    min_votes: int = 0,
    max_maps: Optional[int] = None,
    from_dt: Optional[datetime] = None,
    to_dt: Optional[datetime] = None,
    unranked_only: bool = True,
    exclude_ai: bool = True,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    session: Optional[requests.Session] = None,
) -> List[MapEntry]:
    no_date_filter = days == 0 and from_dt is None and to_dt is None
    now = datetime.now(timezone.utc)
    if no_date_filter:
        from_dt_api: Optional[datetime] = None
        to_dt_api: Optional[datetime] = None
    else:
        if to_dt is None:
            to_dt = now
        elif to_dt.tzinfo is None:
            to_dt = to_dt.replace(tzinfo=timezone.utc)
        else:
            to_dt = to_dt.astimezone(timezone.utc)
        if from_dt is None:
            from_dt = to_dt - timedelta(days=max(1, days) - 1)
        elif from_dt.tzinfo is None:
            from_dt = from_dt.replace(tzinfo=timezone.utc)
        else:
            from_dt = from_dt.astimezone(timezone.utc)
        if from_dt > to_dt:
            from_dt, to_dt = to_dt, from_dt
        from_dt_api = from_dt
        to_dt_api = to_dt

    ss_scores_raw: Dict[str, dict] = {}
    bl_scores_raw: Dict[str, dict] = {}
    if steam_id:
        ss_path = _CACHE_DIR / f"scoresaber_player_scores_{steam_id}.json"
        if ss_path.exists():
            try:
                ss_data = json.loads(ss_path.read_text(encoding="utf-8"))
                ss_scores_raw = ss_data.get("scores", {})
            except Exception:
                pass
        bl_path = _CACHE_DIR / f"beatleader_player_scores_{steam_id}.json"
        if bl_path.exists():
            try:
                bl_data = json.loads(bl_path.read_text(encoding="utf-8"))
                bl_scores_raw = bl_data.get("scores", {})
            except Exception:
                pass

    ss_score_idx = _build_ss_score_hash_index(ss_scores_raw)
    bl_score_idx = _build_bl_score_hash_index(bl_scores_raw)
    bl_replay_idx = _build_bl_replay_hash_index(bl_scores_raw)
    bl_leaderboard_idx = _build_bl_leaderboard_hash_index(bl_scores_raw)
    bl_ranked_idx = _build_bl_hash_index(load_bl_maps())

    session = session or requests.Session()
    entries: List[MapEntry] = []
    pages = 1
    search_query = query.strip()
    bl_api_hash_cache: Dict[str, Dict[Tuple[str, str], str]] = {}

    for page in range(0, 20):
        if max_maps is not None and len(entries) >= max_maps:
            break
        if on_progress:
            on_progress(page, max(pages, 1), f"Searching BeatSaver... {page + 1}/{max(pages, 1)}")
        search_params: Dict[str, str] = {
            "q": search_query,
            "pageSize": str(100 if max_maps is None else min(100, max_maps)),
            "minRating": str(min_rating),
            "minVotes": str(min_votes),
            "order": "Latest",
            "ascending": "false",
        }
        if from_dt_api is not None and to_dt_api is not None:
            search_params["from"] = from_dt_api.isoformat().replace("+00:00", "Z")
            search_params["to"] = to_dt_api.isoformat().replace("+00:00", "Z")
        resp = session.get(f"https://api.beatsaver.com/search/text/{page}", params=search_params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        docs = payload.get("docs") or []
        info = payload.get("info") or {}
        try:
            pages = max(1, int(info.get("pages") or 1))
        except (TypeError, ValueError):
            pages = 1
        if not docs:
            break

        for doc in docs:
            if max_maps is not None and len(entries) >= max_maps:
                break
            if unranked_only and any(doc.get(flag) for flag in ("ranked", "qualified", "blRanked", "blQualified")):
                continue
            tags = [str(tag).lower() for tag in (doc.get("tags") or [])]
            if exclude_ai and (doc.get("automapper") or str(doc.get("declaredAi") or "None") != "None" or "ai" in tags):
                continue

            metadata = doc.get("metadata") or {}
            stats = doc.get("stats") or {}
            versions = doc.get("versions") or []
            version = next((item for item in versions if item.get("hash") or item.get("key")), versions[0] if versions else {})
            song_hash = (version.get("hash") or "").upper()
            if not song_hash:
                continue

            rating_value = float(stats.get("score") or 0.0)
            rating_percent = rating_value * 100.0 if rating_value <= 1.0 else rating_value
            upvotes = int(stats.get("upvotes") or 0)
            downvotes = int(stats.get("downvotes") or 0)
            votes = upvotes + downvotes
            uploaded_ts = _parse_iso_datetime_to_ts(doc.get("lastPublishedAt") or doc.get("uploaded") or doc.get("createdAt"))
            description = str(doc.get("description") or "").replace("\r\n", "\n").strip()
            cover_url = version.get("coverURL") or ""
            preview_url = version.get("previewURL") or ""
            download_url = version.get("downloadURL") or ""
            beatsaver_key = str(doc.get("id") or doc.get("key") or version.get("key") or "")
            page_url = f"https://beatsaver.com/maps/{beatsaver_key}" if beatsaver_key else ""
            duration_seconds = _normalize_duration_seconds(metadata.get("duration"))
            difficulties = version.get("diffs") or []
            if not difficulties:
                difficulties = [{"difficulty": "ExpertPlus", "characteristic": "Standard", "nps": 0.0, "stars": 0.0}]

            for diff in difficulties:
                characteristic = diff.get("characteristic") or "Standard"
                if characteristic in ("Lightshow", "Legacy"):
                    continue
                difficulty = diff.get("difficulty") or diff.get("label") or "ExpertPlus"
                nps_value = float(diff.get("nps") or 0.0)
                star_value = float(diff.get("stars") or diff.get("blStars") or 0.0)
                key = (song_hash, characteristic, difficulty)
                ss_match = ss_score_idx.get(key)
                bl_match = bl_score_idx.get(key)
                bl_entry = bl_ranked_idx.get(key)
                if song_hash not in bl_api_hash_cache:
                    bl_api_hash_cache[song_hash] = _fetch_bl_leaderboards_by_hash(session, song_hash)
                bl_leaderboard_id = bl_leaderboard_idx.get(key) or (bl_entry.leaderboard_id if bl_entry else "") or bl_api_hash_cache[song_hash].get((characteristic, difficulty), "")
                bl_page_url = f"https://beatleader.com/leaderboard/global/{bl_leaderboard_id}" if bl_leaderboard_id else ""
                bl_replay_url = bl_replay_idx.get(key, "")

                cleared = False
                nf_clear = False
                score_source = ""
                played_at_ts = 0
                best_match: Optional[Tuple[float, bool, bool, float, int, str, int]] = None
                if ss_match and bl_match:
                    best_match = ss_match if (2 if ss_match[1] else 1 if ss_match[2] else 0, ss_match[3]) >= (2 if bl_match[1] else 1 if bl_match[2] else 0, bl_match[3]) else bl_match
                    score_source = "SS" if best_match is ss_match else "BL"
                elif ss_match:
                    best_match = ss_match
                    score_source = "SS"
                elif bl_match:
                    best_match = bl_match
                    score_source = "BL"
                if best_match is not None:
                    _, cleared, nf_clear, _, _, _, played_at_ts = best_match

                entries.append(MapEntry(
                    song_name=metadata.get("songName") or doc.get("name") or "",
                    song_author=metadata.get("songAuthorName") or "",
                    mapper=metadata.get("levelAuthorName") or (doc.get("uploader") or {}).get("name") or "",
                    song_hash=song_hash,
                    difficulty=difficulty,
                    mode=characteristic,
                    stars=star_value,
                    max_pp=0.0,
                    player_pp=rating_percent,
                    cleared=cleared,
                    nf_clear=nf_clear,
                    player_acc=nps_value,
                    player_rank=votes,
                    leaderboard_id=bl_leaderboard_id,
                    source="beatsaver",
                    score_source=score_source,
                    duration_seconds=duration_seconds,
                    played_at_ts=played_at_ts,
                    source_date_ts=uploaded_ts,
                    beatsaver_key=beatsaver_key,
                    beatsaver_cover_url=cover_url,
                    beatsaver_preview_url=preview_url,
                    beatsaver_page_url=page_url,
                    beatsaver_download_url=download_url or (f"https://beatsaver.com/api/download/key/{beatsaver_key}" if beatsaver_key else ""),
                    beatsaver_rating=rating_value,
                    beatsaver_votes=votes,
                    beatsaver_upvotes=upvotes,
                    beatsaver_downvotes=downvotes,
                    beatsaver_uploaded_ts=uploaded_ts,
                    beatsaver_description=description,
                    beatsaver_curated=bool(doc.get("curatedAt")),
                    beatsaver_verified_mapper=bool((doc.get("uploader") or {}).get("verifiedMapper")),
                    beatleader_page_url=bl_page_url,
                    beatleader_replay_url=bl_replay_url,
                    beatleader_global1_replay_url="",
                    beatleader_local1_replay_url="",
                    beatleader_attempts=bl_entry.beatleader_attempts if bl_entry else 0,
                    beatleader_replays_watched=bl_entry.beatleader_replays_watched if bl_entry else 0,
                ))
        if page + 1 >= pages:
            break

    if on_progress:
        on_progress(1, 1, "Done")
    return entries if max_maps is None else entries[:max_maps]