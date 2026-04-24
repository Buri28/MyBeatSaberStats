from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..playlist_view import (
    MapEntry,
    _CACHE_DIR,
    _enrich_entries_with_beatsaver_cache,
    load_accsaber_maps,
    load_accsaber_reloaded_maps,
)


_MAP_ENTRIES_CACHE: Dict[Tuple[str, str, bool, Tuple[Tuple[str, bool, int, int], ...]], List[MapEntry]] = {}


def _diff_from_raw(raw_str: str, diff_num: int = 0) -> str:
    """SS の difficultyRaw / difficulty 番号から表示名へ変換。"""
    if raw_str:
        for pat, name in [
            ("_ExpertPlus_", "ExpertPlus"),
            ("_Expert_", "Expert"),
            ("_Hard_", "Hard"),
            ("_Normal_", "Normal"),
            ("_Easy_", "Easy"),
        ]:
            if pat in raw_str:
                return name
    _num_map = {1: "Easy", 3: "Normal", 5: "Hard", 7: "Expert", 9: "ExpertPlus"}
    return _num_map.get(diff_num, str(diff_num))


def _mode_from_gamemode(game_mode: str) -> str:
    """SoloStandard → Standard"""
    return game_mode.replace("Solo", "") if game_mode else "Standard"


def _parse_iso_datetime_to_ts(value: object) -> int:
    from datetime import datetime, timezone

    if not isinstance(value, str) or not value:
        return 0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _parse_unix_datetime_to_ts(value: object) -> int:
    if value is None:
        return 0
    try:
        if isinstance(value, (int, float, str)):
            raw_value = value
        else:
            raw_value = str(value)
        return int(float(raw_value))
    except (TypeError, ValueError):
        return 0


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


def _ss_player_score_info(scores: Dict, lb_id: str, max_score_from_map: int = 0) -> Tuple[float, bool, bool, float, int, str]:
    """SS player scores から (player_pp, cleared, nf_clear, acc, rank, mods) を返す。"""
    entry = scores.get(str(lb_id))
    if not entry:
        return 0.0, False, False, 0.0, 0, ""
    sc = entry.get("score", {})
    player_pp = float(sc.get("pp") or 0)
    base_score = int(sc.get("baseScore") or 0)
    modifiers = (sc.get("modifiers") or "").upper()
    mods_str = ",".join(modifiers[i:i+2] for i in range(0, len(modifiers), 2)) if modifiers else ""
    rank = int(sc.get("rank") or 0)
    acc = (base_score / max_score_from_map * 100.0) if max_score_from_map > 0 and base_score > 0 else 0.0
    has_nf = "NF" in modifiers
    cleared = base_score > 0 and not has_nf
    nf_clear = base_score > 0 and has_nf
    return player_pp, cleared, nf_clear, acc, rank, mods_str


def _bl_player_score_info(scores: Dict, map_id: str) -> Tuple[float, bool, bool, float, int, str]:
    """BL player scores から (player_pp, cleared, nf_clear, acc, rank, mods) を返す。"""
    entry = scores.get(str(map_id))
    if not entry:
        return 0.0, False, False, 0.0, 0, ""
    player_pp = float(entry.get("pp") or 0)
    base_score = int(entry.get("baseScore") or 0)
    modifiers = (entry.get("modifiers") or "").upper()
    mods_str = ",".join(m.strip() for m in modifiers.replace(",", " ").split() if m.strip()) if modifiers else ""
    accuracy = float(entry.get("accuracy") or 0)
    rank = int(entry.get("rank") or 0)
    acc = accuracy * 100.0 if accuracy else 0.0
    has_nf = "NF" in modifiers
    cleared = base_score > 0 and not has_nf
    nf_clear = base_score > 0 and has_nf
    return player_pp, cleared, nf_clear, acc, rank, mods_str


def _ss_player_score_timeset(scores: Dict, lb_id: str) -> int:
    entry = scores.get(str(lb_id)) or {}
    sc = entry.get("score", {})
    return _parse_iso_datetime_to_ts(sc.get("timeSet"))


def _bl_player_score_timeset(scores: Dict, map_id: str) -> int:
    entry = scores.get(str(map_id)) or {}
    return _parse_unix_datetime_to_ts(entry.get("timeset"))


def _file_signature(*paths: Path) -> Tuple[Tuple[str, bool, int, int], ...]:
    signature: List[Tuple[str, bool, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
            signature.append((str(path), True, stat.st_mtime_ns, stat.st_size))
        except OSError:
            signature.append((str(path), False, 0, 0))
    return tuple(signature)


def _clone_entries(entries: List[MapEntry]) -> List[MapEntry]:
    return [replace(entry) for entry in entries]


def load_ss_maps(steam_id: Optional[str] = None, filter_stars: bool = True) -> List[MapEntry]:
    path = _CACHE_DIR / "scoresaber_ranked_maps.json"
    if not path.exists():
        return []
    ss_player_path = _CACHE_DIR / f"scoresaber_player_scores_{steam_id}.json" if steam_id else Path()
    bl_ranked_path = _CACHE_DIR / "beatleader_ranked_maps.json"
    bl_player_path = _CACHE_DIR / f"beatleader_player_scores_{steam_id}.json" if steam_id else Path()
    cache_key = (
        "ss",
        steam_id or "",
        bool(filter_stars),
        _file_signature(path, ss_player_path, bl_ranked_path, bl_player_path),
    )
    cached_entries = _MAP_ENTRIES_CACHE.get(cache_key)
    if cached_entries is not None:
        return _enrich_entries_with_beatsaver_cache(_clone_entries(cached_entries))

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    if "leaderboards" in raw:
        maps_dict: Dict[str, dict] = raw["leaderboards"]
    else:
        maps_dict = {
            k: v for k, v in raw.items()
            if k not in ("fetched_at", "max_pages", "total_maps") and isinstance(v, dict)
        }

    ss_scores: Dict[str, dict] = {}
    if steam_id and ss_player_path.exists():
        try:
            sd = json.loads(ss_player_path.read_text(encoding="utf-8"))
            ss_scores = sd.get("scores", {})
        except Exception:
            pass

    bl_maps = load_bl_maps(steam_id)
    bl_duration_index = {
        (e.song_hash.upper(), e.mode, e.difficulty): e.duration_seconds
        for e in bl_maps
        if e.duration_seconds > 0
    }
    bl_link_index = {
        (e.song_hash.upper(), e.mode, e.difficulty): e
        for e in bl_maps
        if e.leaderboard_id or e.beatleader_page_url or e.beatleader_replay_url
    }

    entries: List[MapEntry] = []
    for lb_id_str, m in maps_dict.items():
        stars = float(m.get("stars") or 0)
        if filter_stars and stars <= 0:
            continue

        diff_info = m.get("difficulty", {})
        diff_num = int(diff_info.get("difficulty") or 0)
        diff_raw = diff_info.get("difficultyRaw") or ""
        game_mode = diff_info.get("gameMode") or "SoloStandard"
        max_score = int(m.get("maxScore") or 0)
        song_hash = (m.get("songHash") or "").upper()
        difficulty = _diff_from_raw(diff_raw, diff_num)
        mode = _mode_from_gamemode(game_mode)
        ranked_date_ts = _parse_iso_datetime_to_ts(m.get("rankedDate") or m.get("qualifiedDate"))
        bl_link_entry = bl_link_index.get((song_hash, mode, difficulty))

        player_pp, cleared, nf_clear, acc, rank, mods = _ss_player_score_info(ss_scores, lb_id_str, max_score)
        played_at_ts = _ss_player_score_timeset(ss_scores, lb_id_str)
        _fc = bool((ss_scores.get(str(lb_id_str)) or {}).get("score", {}).get("fullCombo"))

        entries.append(MapEntry(
            song_name=m.get("songName") or "",
            song_author=m.get("songAuthorName") or "",
            mapper=m.get("levelAuthorName") or "",
            song_hash=song_hash,
            difficulty=difficulty,
            mode=mode,
            stars=stars,
            max_pp=float(m.get("maxPP") or 0),
            player_pp=player_pp,
            cleared=cleared,
            nf_clear=nf_clear,
            player_acc=acc,
            player_rank=rank,
            leaderboard_id=lb_id_str,
            source="scoresaber",
            player_mods=mods,
            full_combo=_fc,
            score_source="SS",
            duration_seconds=bl_duration_index.get((song_hash, mode, difficulty), 0),
            played_at_ts=played_at_ts,
            source_date_ts=ranked_date_ts,
            beatleader_page_url=bl_link_entry.beatleader_page_url if bl_link_entry else "",
            beatleader_replay_url=bl_link_entry.beatleader_replay_url if bl_link_entry else "",
        ))

    _MAP_ENTRIES_CACHE[cache_key] = _clone_entries(entries)
    return _enrich_entries_with_beatsaver_cache(entries)


def load_bl_maps(steam_id: Optional[str] = None) -> List[MapEntry]:
    path = _CACHE_DIR / "beatleader_ranked_maps.json"
    if not path.exists():
        return []
    bl_player_path = _CACHE_DIR / f"beatleader_player_scores_{steam_id}.json" if steam_id else Path()
    cache_key = (
        "bl",
        steam_id or "",
        True,
        _file_signature(path, bl_player_path),
    )
    cached_entries = _MAP_ENTRIES_CACHE.get(cache_key)
    if cached_entries is not None:
        return _enrich_entries_with_beatsaver_cache(_clone_entries(cached_entries))

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    all_maps: List[dict] = []
    for page in raw.get("pages", []):
        page_data = page.get("data", {})
        if isinstance(page_data, dict):
            inner = page_data.get("data", [])
            if isinstance(inner, list):
                all_maps.extend(inner)

    bl_scores: Dict[str, dict] = {}
    if steam_id and bl_player_path.exists():
        try:
            bd = json.loads(bl_player_path.read_text(encoding="utf-8"))
            bl_scores = bd.get("scores", {})
        except Exception:
            pass

    entries: List[MapEntry] = []
    bl_replay_idx = _build_bl_replay_hash_index(bl_scores) if bl_scores else {}
    for m in all_maps:
        diff = m.get("difficulty", {})
        song = m.get("song", {})
        map_id = str(m.get("id") or "")
        stars = float(diff.get("stars") or 0)
        ranked_date_ts = int(diff.get("rankedTime") or diff.get("qualifiedTime") or diff.get("nominatedTime") or 0)
        diff_name = diff.get("difficultyName") or "ExpertPlus"
        mode_name = diff.get("modeName") or "Standard"
        song_hash = (song.get("hash") or "").upper()
        replay_url = bl_replay_idx.get((song_hash, mode_name, diff_name), "")

        player_pp, cleared, nf_clear, acc, rank, mods = _bl_player_score_info(bl_scores, map_id)
        played_at_ts = _bl_player_score_timeset(bl_scores, map_id)
        _fc = bool((bl_scores.get(str(map_id)) or {}).get("fullCombo"))

        entries.append(MapEntry(
            song_name=song.get("name") or "",
            song_author=song.get("author") or "",
            mapper=song.get("mapper") or "",
            song_hash=song_hash,
            difficulty=diff_name,
            mode=mode_name,
            stars=stars,
            max_pp=0.0,
            player_pp=player_pp,
            cleared=cleared,
            nf_clear=nf_clear,
            player_acc=acc,
            player_rank=rank,
            leaderboard_id=map_id,
            source="beatleader",
            player_mods=mods,
            full_combo=_fc,
            score_source="BL",
            duration_seconds=_normalize_duration_seconds(song.get("duration")),
            played_at_ts=played_at_ts,
            source_date_ts=ranked_date_ts,
            beatleader_page_url=f"https://beatleader.com/leaderboard/global/{map_id}" if map_id else "",
            beatleader_replay_url=replay_url,
            beatleader_attempts=int(m.get("attempts") or 0),
            beatleader_plays=int(m.get("plays") or 0),
        ))

    _MAP_ENTRIES_CACHE[cache_key] = _clone_entries(entries)
    return _enrich_entries_with_beatsaver_cache(entries)


def _build_ss_score_hash_index(ss_scores: Dict[str, dict]) -> Dict[Tuple[str, str, str], Tuple[float, bool, bool, float, int, str, int]]:
    idx: Dict[Tuple[str, str, str], Tuple[float, bool, bool, float, int, str, int]] = {}
    for lb_id_str, entry in ss_scores.items():
        lb = entry.get("leaderboard", {})
        song_hash = (lb.get("songHash") or "").upper()
        if not song_hash:
            continue
        diff_info = lb.get("difficulty", {})
        diff_raw = diff_info.get("difficultyRaw") or ""
        diff_num = int(diff_info.get("difficulty") or 0)
        game_mode = diff_info.get("gameMode") or "SoloStandard"
        diff_name = _diff_from_raw(diff_raw, diff_num)
        mode = _mode_from_gamemode(game_mode)
        max_score = int(lb.get("maxScore") or 0)
        pp, cleared, nf_clear, acc, rank, mods = _ss_player_score_info(ss_scores, lb_id_str, max_score)
        played_at_ts = _ss_player_score_timeset(ss_scores, lb_id_str)
        key = (song_hash, mode, diff_name)
        if key not in idx or (cleared and not idx[key][1]) or pp > idx[key][0]:
            idx[key] = (pp, cleared, nf_clear, acc, rank, mods, played_at_ts)
    return idx


def _build_ss_hash_index(entries: List[MapEntry]) -> Dict[Tuple[str, str, str], MapEntry]:
    idx: Dict[Tuple[str, str, str], MapEntry] = {}
    for e in entries:
        idx[(e.song_hash.upper(), e.mode, e.difficulty)] = e
    return idx


def _build_bl_hash_index(entries: List[MapEntry]) -> Dict[Tuple[str, str, str], MapEntry]:
    return _build_ss_hash_index(entries)


def _build_bl_score_hash_index(bl_scores: Dict[str, dict]) -> Dict[Tuple[str, str, str], Tuple[float, bool, bool, float, int, str, int]]:
    idx: Dict[Tuple[str, str, str], Tuple[float, bool, bool, float, int, str, int]] = {}
    for map_id, entry in bl_scores.items():
        lb = entry.get("leaderboard") or {}
        song = lb.get("song") or {}
        diff = lb.get("difficulty") or {}
        song_hash = (song.get("hash") or "").upper()
        if not song_hash:
            continue
        diff_name = diff.get("difficultyName") or "ExpertPlus"
        mode = diff.get("modeName") or "Standard"
        pp, cleared, nf_clear, acc, rank, mods = _bl_player_score_info(bl_scores, str(map_id))
        played_at_ts = _bl_player_score_timeset(bl_scores, str(map_id))
        key = (song_hash, mode, diff_name)
        if key not in idx or (cleared and not idx[key][1]) or pp > idx[key][0]:
            idx[key] = (pp, cleared, nf_clear, acc, rank, mods, played_at_ts)
    return idx


def _build_bl_replay_hash_index(bl_scores: Dict[str, dict]) -> Dict[Tuple[str, str, str], str]:
    idx: Dict[Tuple[str, str, str], str] = {}
    best_meta: Dict[Tuple[str, str, str], Tuple[bool, float, int]] = {}
    for map_id, entry in bl_scores.items():
        lb = entry.get("leaderboard") or {}
        song = lb.get("song") or {}
        diff = lb.get("difficulty") or {}
        song_hash = (song.get("hash") or "").upper()
        if not song_hash:
            continue
        diff_name = diff.get("difficultyName") or "ExpertPlus"
        mode = diff.get("modeName") or "Standard"
        key = (song_hash, mode, diff_name)
        score_id = str(entry.get("id") or entry.get("originalId") or "").strip()
        if not score_id:
            continue
        pp = float(entry.get("pp") or 0.0)
        played_at_ts = _bl_player_score_timeset(bl_scores, str(map_id))
        cleared = bool(entry.get("baseScore") or 0)
        candidate = (cleared, pp, played_at_ts)
        current = best_meta.get(key)
        if current is None or candidate > current:
            best_meta[key] = candidate
            idx[key] = f"https://replay.beatleader.com/?scoreId={score_id}"
    return idx


def _build_bl_leaderboard_hash_index(bl_scores: Dict[str, dict]) -> Dict[Tuple[str, str, str], str]:
    idx: Dict[Tuple[str, str, str], str] = {}
    best_meta: Dict[Tuple[str, str, str], Tuple[bool, float, int]] = {}
    for map_id, entry in bl_scores.items():
        lb = entry.get("leaderboard") or {}
        song = lb.get("song") or {}
        diff = lb.get("difficulty") or {}
        song_hash = (song.get("hash") or "").upper()
        if not song_hash:
            continue
        diff_name = diff.get("difficultyName") or "ExpertPlus"
        mode = diff.get("modeName") or "Standard"
        leaderboard_id = str(entry.get("leaderboardId") or lb.get("id") or map_id or "")
        if not leaderboard_id:
            continue
        key = (song_hash, mode, diff_name)
        pp = float(entry.get("pp") or 0.0)
        played_at_ts = _bl_player_score_timeset(bl_scores, str(map_id))
        cleared = bool(entry.get("baseScore") or 0)
        candidate = (cleared, pp, played_at_ts)
        current = best_meta.get(key)
        if current is None or candidate > current:
            best_meta[key] = candidate
            idx[key] = leaderboard_id
    return idx


def _load_cached_player_score_dicts(steam_id: Optional[str]) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    ss_scores_raw: Dict[str, dict] = {}
    bl_scores_raw: Dict[str, dict] = {}
    if not steam_id:
        return ss_scores_raw, bl_scores_raw

    ss_path = _CACHE_DIR / f"scoresaber_player_scores_{steam_id}.json"
    if ss_path.exists():
        try:
            ss_data = json.loads(ss_path.read_text(encoding="utf-8"))
            ss_scores_raw = ss_data.get("scores", {})
        except Exception:
            ss_scores_raw = {}

    bl_path = _CACHE_DIR / f"beatleader_player_scores_{steam_id}.json"
    if bl_path.exists():
        try:
            bl_data = json.loads(bl_path.read_text(encoding="utf-8"))
            bl_scores_raw = bl_data.get("scores", {})
        except Exception:
            bl_scores_raw = {}

    return ss_scores_raw, bl_scores_raw


def _refresh_entries_from_cached_player_scores(entries: List[MapEntry], steam_id: Optional[str]) -> set[str]:
    if not entries or not steam_id:
        return set()

    ss_scores_raw, bl_scores_raw = _load_cached_player_score_dicts(steam_id)
    if not ss_scores_raw and not bl_scores_raw:
        return set()

    ss_score_idx = _build_ss_score_hash_index(ss_scores_raw) if ss_scores_raw else {}
    bl_score_idx = _build_bl_score_hash_index(bl_scores_raw) if bl_scores_raw else {}
    bl_replay_idx = _build_bl_replay_hash_index(bl_scores_raw) if bl_scores_raw else {}
    bl_leaderboard_idx = _build_bl_leaderboard_hash_index(bl_scores_raw) if bl_scores_raw else {}
    bl_ranked_idx = _build_bl_hash_index(load_bl_maps())
    changed_hashes: set[str] = set()

    for entry in entries:
        song_hash = (entry.song_hash or "").upper()
        if not song_hash or not entry.difficulty:
            continue

        key = (song_hash, entry.mode or "Standard", entry.difficulty)
        ss_match = ss_score_idx.get(key)
        bl_match = bl_score_idx.get(key)
        bl_ranked_entry = bl_ranked_idx.get(key)

        best_match: Optional[Tuple[float, bool, bool, float, int, str, int]] = None
        new_score_source = entry.score_source
        if entry.source == "scoresaber":
            best_match = ss_match
            new_score_source = "SS" if ss_match else ""
        elif entry.source == "beatleader":
            best_match = bl_match
            new_score_source = "BL" if bl_match else ""
        else:
            if ss_match and bl_match:
                ss_priority = (2 if ss_match[1] else 1 if ss_match[2] else 0, ss_match[3])
                bl_priority = (2 if bl_match[1] else 1 if bl_match[2] else 0, bl_match[3])
                best_match = ss_match if ss_priority >= bl_priority else bl_match
                new_score_source = "SS" if best_match is ss_match else "BL"
            elif ss_match:
                best_match = ss_match
                new_score_source = "SS"
            elif bl_match:
                best_match = bl_match
                new_score_source = "BL"
            else:
                new_score_source = ""

        new_played_at_ts = best_match[6] if best_match is not None else 0
        new_bl_leaderboard_id = bl_leaderboard_idx.get(key) or (bl_ranked_entry.leaderboard_id if bl_ranked_entry else "")
        new_bl_page_url = f"https://beatleader.com/leaderboard/global/{new_bl_leaderboard_id}" if new_bl_leaderboard_id else ""
        new_bl_replay_url = bl_replay_idx.get(key, "")
        new_bl_attempts = bl_ranked_entry.beatleader_attempts if bl_ranked_entry else entry.beatleader_attempts
        new_bl_plays = bl_ranked_entry.beatleader_plays if bl_ranked_entry else entry.beatleader_plays

        old_state = (
            entry.played_at_ts,
            entry.score_source,
            entry.leaderboard_id,
            entry.beatleader_page_url,
            entry.beatleader_replay_url,
            entry.beatleader_attempts,
            entry.beatleader_plays,
        )

        entry.played_at_ts = new_played_at_ts
        entry.score_source = new_score_source
        if entry.source != "scoresaber":
            entry.leaderboard_id = new_bl_leaderboard_id
        entry.beatleader_page_url = new_bl_page_url
        entry.beatleader_replay_url = new_bl_replay_url
        entry.beatleader_attempts = new_bl_attempts
        entry.beatleader_plays = new_bl_plays

        new_state = (
            entry.played_at_ts,
            entry.score_source,
            entry.leaderboard_id,
            entry.beatleader_page_url,
            entry.beatleader_replay_url,
            entry.beatleader_attempts,
            entry.beatleader_plays,
        )
        if new_state != old_state:
            changed_hashes.add(song_hash)

    return changed_hashes


def _apply_entry_snapshot_service_field(entry: MapEntry, service_entry: MapEntry) -> None:
    if service_entry.source == "scoresaber":
        entry.ss_stars = service_entry.stars
        entry.ss_player_pp = service_entry.player_pp
        entry.ss_player_acc = service_entry.player_acc
        entry.ss_player_rank = service_entry.player_rank
        entry.ss_played_at_ts = service_entry.played_at_ts
        entry.ss_leaderboard_id = service_entry.leaderboard_id
        return
    if service_entry.source == "beatleader":
        entry.bl_stars = service_entry.stars
        entry.bl_player_pp = service_entry.player_pp
        entry.bl_player_acc = service_entry.player_acc
        entry.bl_player_rank = service_entry.player_rank
        entry.bl_played_at_ts = service_entry.played_at_ts
        entry.bl_leaderboard_id = service_entry.leaderboard_id
        entry.beatleader_attempts = service_entry.beatleader_attempts
        entry.beatleader_plays = service_entry.beatleader_plays
        if service_entry.beatleader_page_url:
            entry.beatleader_page_url = service_entry.beatleader_page_url
        if service_entry.beatleader_replay_url:
            entry.beatleader_replay_url = service_entry.beatleader_replay_url
        return
    if service_entry.source == "accsaber":
        entry.acc_category_value = service_entry.acc_category
        entry.acc_complexity_value = service_entry.acc_complexity
        entry.acc_player_acc = service_entry.player_acc
        entry.acc_player_rank_value = service_entry.player_rank
        entry.acc_ap_value = service_entry.acc_rl_ap
        entry.acc_played_at_ts = service_entry.played_at_ts
        return
    if service_entry.source == "accsaber_reloaded":
        entry.rl_category_value = service_entry.acc_category
        entry.rl_complexity_value = service_entry.acc_complexity
        entry.rl_player_acc = service_entry.player_acc
        entry.rl_player_rank_value = service_entry.player_rank
        entry.rl_ap_value = service_entry.acc_rl_ap
        entry.rl_played_at_ts = service_entry.played_at_ts


def _load_snapshot_service_entries_from_cache(steam_id: Optional[str]) -> Dict[str, List[MapEntry]]:
    service_entries: Dict[str, List[MapEntry]] = {
        "scoresaber": [],
        "beatleader": [],
        "accsaber": [],
        "accsaber_reloaded": [],
    }
    try:
        service_entries["scoresaber"] = load_ss_maps(steam_id)
    except Exception:
        service_entries["scoresaber"] = []
    try:
        service_entries["beatleader"] = load_bl_maps(steam_id)
    except Exception:
        service_entries["beatleader"] = []

    acc_maps_cache = _CACHE_DIR / "accsaber_maps.json"
    acc_score_cache = _CACHE_DIR / f"accsaber_player_scores_{steam_id}.json" if steam_id else Path()
    if acc_maps_cache.exists():
        acc_steam_id = steam_id if acc_score_cache.exists() else None
        try:
            service_entries["accsaber"] = load_accsaber_maps(acc_steam_id, "all")
        except Exception:
            service_entries["accsaber"] = []

    rl_maps_cache = _CACHE_DIR / "accsaber_reloaded_maps.json"
    rl_score_cache = _CACHE_DIR / f"accsaber_reloaded_player_scores_{steam_id}.json" if steam_id else Path()
    if rl_maps_cache.exists():
        rl_steam_id = steam_id if rl_score_cache.exists() else None
        try:
            service_entries["accsaber_reloaded"] = load_accsaber_reloaded_maps(rl_steam_id, "all")
        except Exception:
            service_entries["accsaber_reloaded"] = []

    return service_entries


def _refresh_snapshot_entries_service_columns(entries: List[MapEntry], steam_id: Optional[str]) -> None:
    if not entries:
        return

    service_entries = _load_snapshot_service_entries_from_cache(steam_id)
    ss_scores_raw, bl_scores_raw = _load_cached_player_score_dicts(steam_id)
    service_indices = {
        service: {
            ((item.song_hash or "").upper(), item.mode or "Standard", item.difficulty or ""): item
            for item in items
            if item.song_hash and item.difficulty
        }
        for service, items in service_entries.items()
    }
    ss_score_idx = _build_ss_score_hash_index(ss_scores_raw) if ss_scores_raw else {}
    bl_score_idx = _build_bl_score_hash_index(bl_scores_raw) if bl_scores_raw else {}
    bl_replay_idx = _build_bl_replay_hash_index(bl_scores_raw) if bl_scores_raw else {}
    bl_leaderboard_idx = _build_bl_leaderboard_hash_index(bl_scores_raw) if bl_scores_raw else {}

    for entry in entries:
        entry.ss_stars = 0.0
        entry.ss_player_pp = 0.0
        entry.ss_player_acc = 0.0
        entry.ss_player_rank = 0
        entry.ss_played_at_ts = 0
        entry.ss_leaderboard_id = ""
        entry.bl_stars = 0.0
        entry.bl_player_pp = 0.0
        entry.bl_player_acc = 0.0
        entry.bl_player_rank = 0
        entry.bl_played_at_ts = 0
        entry.bl_leaderboard_id = ""
        entry.acc_category_value = ""
        entry.acc_complexity_value = 0.0
        entry.acc_player_acc = 0.0
        entry.acc_player_rank_value = 0
        entry.acc_ap_value = 0.0
        entry.acc_played_at_ts = 0
        entry.rl_category_value = ""
        entry.rl_complexity_value = 0.0
        entry.rl_player_acc = 0.0
        entry.rl_player_rank_value = 0
        entry.rl_ap_value = 0.0
        entry.rl_played_at_ts = 0

        _apply_entry_snapshot_service_field(entry, entry)

        key = ((entry.song_hash or "").upper(), entry.mode or "Standard", entry.difficulty or "")
        if not key[0] or not key[2]:
            continue
        for service in ("scoresaber", "beatleader", "accsaber", "accsaber_reloaded"):
            service_entry = service_indices.get(service, {}).get(key)
            if service_entry is not None:
                _apply_entry_snapshot_service_field(entry, service_entry)

        ss_match = ss_score_idx.get(key)
        if ss_match is not None:
            entry.ss_player_pp = ss_match[0]
            entry.ss_player_acc = ss_match[3]
            entry.ss_player_rank = ss_match[4]
            entry.ss_played_at_ts = ss_match[6]

        bl_match = bl_score_idx.get(key)
        if bl_match is not None:
            entry.bl_player_pp = bl_match[0]
            entry.bl_player_acc = bl_match[3]
            entry.bl_player_rank = bl_match[4]
            entry.bl_played_at_ts = bl_match[6]

        if not entry.bl_leaderboard_id:
            entry.bl_leaderboard_id = bl_leaderboard_idx.get(key, "")
        if entry.bl_leaderboard_id and not entry.beatleader_page_url:
            entry.beatleader_page_url = f"https://beatleader.com/leaderboard/global/{entry.bl_leaderboard_id}"
        if not entry.beatleader_replay_url:
            entry.beatleader_replay_url = bl_replay_idx.get(key, "")