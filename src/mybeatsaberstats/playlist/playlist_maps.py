from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

from ..beatsaver_cache import BEATSAVER_API_BASE
from ..http_client import make_session, request_scope
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
    _load_cached_player_score_dicts,
    _parse_iso_datetime_to_ts,
    load_accsaber_reloaded_maps,
    load_bl_maps,
    load_ss_maps,
)


_BL_LEADERBOARD_CACHE_PATH = _CACHE_DIR / "beatleader_leaderboards_by_hash.json"
_BL_LEADERBOARD_CACHE_LOCK = threading.Lock()
#: プロセス内に保持する永続キャッシュの実体。ページごとに読み直さないよう一度だけ読む。
_BL_LEADERBOARD_MEMO: Optional[Dict[str, Dict[Tuple[str, str], str]]] = None


def _load_bl_leaderboard_disk_cache() -> Dict[str, Dict[Tuple[str, str], str]]:
    """hash -> {(mode, difficulty): leaderboard_id} の永続キャッシュを読む。

    以前はプロセス内メモリにしか持っていなかったため、アプリを再起動するたびに
    同じ hash を BeatLeader に問い合わせ直していた。
    """
    try:
        raw = json.loads(_BL_LEADERBOARD_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    result: Dict[str, Dict[Tuple[str, str], str]] = {}
    for song_hash, mapping in entries.items():
        if not isinstance(mapping, dict):
            continue
        converted: Dict[Tuple[str, str], str] = {}
        for key, leaderboard_id in mapping.items():
            # 保存時に "mode|difficulty" の文字列へ潰しているので復元する
            mode, _, difficulty = str(key).partition("|")
            if mode and difficulty:
                converted[(mode, difficulty)] = str(leaderboard_id or "")
        result[str(song_hash).upper()] = converted
    return result


def _save_bl_leaderboard_disk_cache(cache: Dict[str, Dict[Tuple[str, str], str]]) -> None:
    payload = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entries": {
            song_hash: {f"{mode}|{difficulty}": lb_id for (mode, difficulty), lb_id in mapping.items()}
            for song_hash, mapping in cache.items()
        },
    }
    try:
        _BL_LEADERBOARD_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _BL_LEADERBOARD_CACHE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        return


def _fetch_bl_leaderboards_by_hash(session: requests.Session, song_hash: str) -> Dict[Tuple[str, str], str]:
    """song hash に対応する BeatLeader leaderboard id 一覧を取得する。"""
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


def _prefetch_bl_leaderboards_for_docs(
    session: requests.Session,
    docs: List[dict],
    cache: Dict[str, Dict[Tuple[str, str], str]],
    max_workers: int = 4,
) -> None:
    """docs に含まれる song hash の BeatLeader leaderboard を並列取得して cache へ格納する。

    BeatLeader は 1 hash につき 1 リクエストが必要なので、リクエスト数を減らすため
    永続キャッシュを先に参照し、本当に未知の hash だけを問い合わせる。
    並列数も抑えてある（実際の送出間隔は http_client 側でもホスト単位で制御される）。
    """
    global _BL_LEADERBOARD_MEMO

    # 既知の結果はディスクキャッシュから取り込み、API を叩かずに済ませる。
    # この関数はページごとに呼ばれるので、ファイル読み込みは最初の 1 回だけにする。
    with _BL_LEADERBOARD_CACHE_LOCK:
        if _BL_LEADERBOARD_MEMO is None:
            _BL_LEADERBOARD_MEMO = _load_bl_leaderboard_disk_cache()
        disk_cache = _BL_LEADERBOARD_MEMO

    hashes: List[str] = []
    seen: set = set()
    for doc in docs:
        versions = doc.get("versions") or []
        version = next(
            (item for item in versions if item.get("hash") or item.get("key")),
            versions[0] if versions else {},
        )
        song_hash = (version.get("hash") or "").upper()
        if not song_hash or song_hash in seen or song_hash in cache:
            continue
        seen.add(song_hash)
        cached_entry = disk_cache.get(song_hash)
        if cached_entry is not None:
            # 過去に取得済み → API を叩かない
            cache[song_hash] = cached_entry
            continue
        hashes.append(song_hash)
    if not hashes:
        return
    workers = max(1, min(max_workers, len(hashes)))
    fetched: Dict[str, Dict[Tuple[str, str], str]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_fetch_bl_leaderboards_by_hash, session, h): h
            for h in hashes
        }
        for future in as_completed(futures):
            song_hash = futures[future]
            try:
                result = future.result()
            except Exception:
                result = {}
            cache[song_hash] = result
            # 空の結果（取得失敗・BL 未登録）は永続化しない。
            # 失敗を焼き付けてしまうと、次回以降も誤った空扱いになるため。
            if result:
                fetched[song_hash] = result

    if fetched:
        with _BL_LEADERBOARD_CACHE_LOCK:
            # 他プロセス／別スレッドが書いた分を失わないよう、保存直前に読み直して統合する
            merged = _load_bl_leaderboard_disk_cache()
            merged.update(_BL_LEADERBOARD_MEMO or {})
            merged.update(fetched)
            _save_bl_leaderboard_disk_cache(merged)
            _BL_LEADERBOARD_MEMO = merged


def _fetch_bl_top_replay_url(session: requests.Session, leaderboard_id: str, countries: str = "") -> str:
    """BeatLeader leaderboard から top replay の URL を取得する。"""
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
    """曲長表現を正の整数秒へ正規化する。"""
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


_BPLIST_DIFF_ALIASES: Dict[str, str] = {
    "easy": "Easy",
    "normal": "Normal",
    "hard": "Hard",
    "expert": "Expert",
    "expertplus": "ExpertPlus",
    "expert+": "ExpertPlus",
    "expertplusplus": "ExpertPlus",
    "ex+": "ExpertPlus",
}

_BPLIST_MODE_ALIASES: Dict[str, str] = {
    "standard": "Standard",
    "onesaber": "OneSaber",
    "noarrows": "NoArrows",
    "90degree": "90Degree",
    "360degree": "360Degree",
    "lightshow": "Lightshow",
    "lawless": "Lawless",
    "legacy": "Legacy",
}


def _normalize_bplist_difficulty(value: object) -> str:
    """bplist の難易度名 (小文字表記など) を内部表記へ揃える。"""
    name = str(value or "").strip()
    if not name:
        return "ExpertPlus"
    return _BPLIST_DIFF_ALIASES.get(name.replace("_", "").replace(" ", "").lower(), name)


def _normalize_bplist_characteristic(value: object) -> str:
    """bplist の characteristic (小文字表記など) を内部表記へ揃える。"""
    name = str(value or "").strip()
    if not name:
        return "Standard"
    name = name.replace("Solo", "") or "Standard"
    return _BPLIST_MODE_ALIASES.get(name.replace("_", "").replace(" ", "").lower(), name)


def load_bplist_maps(
    bplist_path: Path,
    service: str,
    steam_id: Optional[str] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> List[MapEntry]:
    """.bplist / .json を読み込み、既存 ranked 情報と突き合わせて MapEntry 化する。"""
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
    elif service in ("accsaber_rl", "accsaber"):
        ranked = load_accsaber_reloaded_maps(steam_id, "all", on_progress=on_progress)
        idx = _build_ss_hash_index(ranked)
    else:
        idx = {}
        ranked = []

    # ranked 一覧に無い譜面 (unranked など) でもプレイ済み状態を出せるように、
    # ローカルの player score cache からスコア情報を引けるようにしておく。
    ss_scores_raw, bl_scores_raw = _load_cached_player_score_dicts(steam_id)
    ss_score_idx = _build_ss_score_hash_index(ss_scores_raw)
    bl_score_idx = _build_bl_score_hash_index(bl_scores_raw)
    bl_replay_idx = _build_bl_replay_hash_index(bl_scores_raw)
    bl_leaderboard_idx = _build_bl_leaderboard_hash_index(bl_scores_raw)

    def _make_open_entry(song_name: str, song_hash: str, diff_name: str, mode: str) -> MapEntry:
        """ranked 索引に無い譜面を、cache 済みスコアで補完しつつ MapEntry 化する。"""
        key = (song_hash, mode, diff_name)
        ss_match = ss_score_idx.get(key)
        bl_match = bl_score_idx.get(key)
        if ss_match and bl_match:
            # cleared > NF > 未プレイ、同条件なら PP が高い方を採用する。
            ss_rankable = (2 if ss_match[1] else 1 if ss_match[2] else 0, ss_match[0])
            bl_rankable = (2 if bl_match[1] else 1 if bl_match[2] else 0, bl_match[0])
            best_match = ss_match if ss_rankable >= bl_rankable else bl_match
            score_source = "SS" if best_match is ss_match else "BL"
        elif ss_match:
            best_match, score_source = ss_match, "SS"
        elif bl_match:
            best_match, score_source = bl_match, "BL"
        else:
            best_match, score_source = None, ""

        if best_match is None:
            pp = acc = 0.0
            cleared = nf_clear = False
            rank = 0
            mods = ""
            played_at_ts = 0
        else:
            pp, cleared, nf_clear, acc, rank, mods, played_at_ts = best_match

        bl_leaderboard_id = bl_leaderboard_idx.get(key, "")
        return MapEntry(
            song_name=song_name,
            song_author="",
            mapper="",
            song_hash=song_hash,
            difficulty=diff_name,
            mode=mode,
            stars=0.0,
            max_pp=0.0,
            player_pp=pp,
            cleared=cleared,
            nf_clear=nf_clear,
            player_acc=acc,
            player_rank=rank,
            leaderboard_id=bl_leaderboard_id,
            source="open",
            player_mods=mods,
            score_source=score_source,
            duration_seconds=0,
            played_at_ts=played_at_ts,
            beatleader_page_url=(
                f"https://beatleader.com/leaderboard/global/{bl_leaderboard_id}" if bl_leaderboard_id else ""
            ),
            beatleader_replay_url=bl_replay_idx.get(key, ""),
        )

    entries: List[MapEntry] = []
    for song in songs:
        song_hash = (song.get("hash") or "").upper()
        song_name = song.get("songName") or ""
        diffs = song.get("difficulties") or []

        if not diffs:
            matched = [entry for entry in ranked or [] if entry.song_hash == song_hash]
            if matched:
                entries.extend(matched)
                continue
            # 難易度指定なし = 全難易度。cache 済みスコアから既知の難易度を復元する。
            played_keys = sorted(
                {key for key in ss_score_idx if key[0] == song_hash}
                | {key for key in bl_score_idx if key[0] == song_hash}
            )
            if played_keys:
                for _, mode, diff_name in played_keys:
                    entries.append(_make_open_entry(song_name, song_hash, diff_name, mode))
            else:
                entries.append(_make_open_entry(song_name, song_hash, "", ""))
            continue

        for diff in diffs:
            characteristic = _normalize_bplist_characteristic(diff.get("characteristic"))
            diff_name = _normalize_bplist_difficulty(diff.get("name") or diff.get("difficulty"))
            key = (song_hash, characteristic, diff_name)
            if key in idx:
                entries.append(idx[key])
            else:
                entries.append(_make_open_entry(song_name, song_hash, diff_name, characteristic))

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
    on_batch: Optional[Callable[[List[MapEntry]], None]] = None,
    session: Optional[requests.Session] = None,
    bl_lookup_workers: int = 4,
) -> List[MapEntry]:
    """BeatSaver 検索を実行し、API 別のリクエスト数をログへ出力する。

    実処理は :func:`_load_beatsaver_maps` に委譲する。
    """
    with request_scope(f"Maps 検索 query={query!r}"):
        return _load_beatsaver_maps(
            steam_id=steam_id,
            query=query,
            days=days,
            min_rating=min_rating,
            min_votes=min_votes,
            max_maps=max_maps,
            from_dt=from_dt,
            to_dt=to_dt,
            unranked_only=unranked_only,
            exclude_ai=exclude_ai,
            on_progress=on_progress,
            on_batch=on_batch,
            session=session,
            bl_lookup_workers=bl_lookup_workers,
        )


def _load_beatsaver_maps(
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
    on_batch: Optional[Callable[[List[MapEntry]], None]] = None,
    session: Optional[requests.Session] = None,
    bl_lookup_workers: int = 4,
) -> List[MapEntry]:
    """BeatSaver 検索 API とローカル score cache を突き合わせて Maps 一覧を構築する。

    on_batch: 指定すると 1 ページ処理し終えるごとに、そのページ分の MapEntry 一覧を
        渡す。UI 側で段階的に描画するために使う（体感速度の改善用）。
    bl_lookup_workers: BeatLeader leaderboard 取得を並列化する際のワーカー数。
    """
    if on_progress:
        on_progress(0, 1, "Preparing... score caches")
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
    if on_progress:
        on_progress(0, 1, "Preparing... BL ranked index")
    bl_ranked_idx = _build_bl_hash_index(load_bl_maps())

    session = session or make_session()
    entries: List[MapEntry] = []
    pages = 1
    search_query = query.strip()
    bl_api_hash_cache: Dict[str, Dict[Tuple[str, str], str]] = {}

    for page in range(0, 20):
        if max_maps is not None and len(entries) >= max_maps:
            break
        if on_progress:
            on_progress(page, max(pages, 1), f"Search: page {page + 1}/{max(pages, 1)}...")
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
        resp = session.get(f"{BEATSAVER_API_BASE}/search/text/{page}", params=search_params, timeout=15)
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
        if on_progress:
            on_progress(page, max(pages, 1), f"Search: page {page + 1}/{max(pages, 1)} ({len(docs)})")

        # このページに含まれる譜面の BeatLeader leaderboard を並列で先読みしておく。
        # 以前は 1 譜面ごとに直列で API を叩いていたため非常に遅かった。
        _prefetch_bl_leaderboards_for_docs(
            session, docs, bl_api_hash_cache, max_workers=bl_lookup_workers
        )

        page_entries: List[MapEntry] = []
        for doc_index, doc in enumerate(docs, start=1):
            if max_maps is not None and len(entries) + len(page_entries) >= max_maps:
                break
            if on_progress and (doc_index == 1 or doc_index % 10 == 0 or doc_index == len(docs)):
                on_progress(
                    page,
                    max(pages, 1),
                    f"Search: page {page + 1}/{max(pages, 1)} ({doc_index}/{len(docs)})",
                )
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
                bl_leaderboard_id = bl_leaderboard_idx.get(key) or (bl_entry.leaderboard_id if bl_entry else "") or bl_api_hash_cache.get(song_hash, {}).get((characteristic, difficulty), "")
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

                page_entries.append(MapEntry(
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

        entries.extend(page_entries)
        # 1 ページ処理し終えた時点で UI 側へ渡し、段階的に描画できるようにする。
        if on_batch and page_entries:
            on_batch(page_entries)
        if page + 1 >= pages:
            break

    if on_progress:
        on_progress(1, 1, "Done")
    return entries if max_maps is None else entries[:max_maps]