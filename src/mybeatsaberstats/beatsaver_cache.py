from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

import requests

from .snapshot import BASE_DIR

_CACHE_PATH = BASE_DIR / "cache" / "beatsaver_map_details.json"
_BEATSAVER_REQUEST_TIMEOUT = (3, 6)


def _normalize_hash(song_hash: object) -> str:
    return str(song_hash or "").strip().upper()


def _normalize_key(key: object) -> str:
    return str(key or "").strip()


def _page_url_from_key(key: object) -> str:
    normalized = _normalize_key(key)
    return f"https://beatsaver.com/maps/{normalized}" if normalized else ""


def _download_url_from_key(key: object) -> str:
    normalized = _normalize_key(key)
    return f"https://beatsaver.com/api/download/key/{normalized}" if normalized else ""


def _parse_iso_datetime_to_ts(value: object) -> int:
    if not isinstance(value, str) or not value:
        return 0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def load_beatsaver_meta_cache() -> Dict[str, dict]:
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return {}
    normalized: Dict[str, dict] = {}
    for song_hash, entry in entries.items():
        normalized_hash = _normalize_hash(song_hash)
        if normalized_hash and isinstance(entry, dict):
            normalized[normalized_hash] = dict(entry)
    return normalized


def _save_beatsaver_meta_cache(entries: Dict[str, dict]) -> None:
    payload = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entries": dict(sorted(entries.items())),
    }
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return


def _merge_meta_entry(existing: dict, incoming: dict) -> dict:
    merged = dict(existing)
    for key, value in incoming.items():
        if key == "hash":
            continue
        if value in (None, "", 0, 0.0):
            continue
        merged[key] = value
    if "hash" in incoming:
        merged["hash"] = incoming["hash"]
    return merged


def upsert_beatsaver_meta_cache(meta_entries: Iterable[dict]) -> Dict[str, dict]:
    cache = load_beatsaver_meta_cache()
    updated = False
    for meta in meta_entries:
        if not isinstance(meta, dict):
            continue
        song_hash = _normalize_hash(meta.get("hash"))
        if not song_hash:
            continue
        incoming = dict(meta)
        incoming["hash"] = song_hash
        existing = cache.get(song_hash, {})
        merged = _merge_meta_entry(existing, incoming)
        if merged != existing:
            cache[song_hash] = merged
            updated = True
    if updated:
        _save_beatsaver_meta_cache(cache)
    return cache


def _meta_from_map_payload(payload: dict, fallback_hash: str = "", fallback_key: str = "") -> Optional[dict]:
    if not isinstance(payload, dict):
        return None

    versions = payload.get("versions") or []
    version = next(
        (item for item in versions if isinstance(item, dict) and (item.get("hash") or item.get("key"))),
        versions[0] if versions and isinstance(versions[0], dict) else {},
    )
    metadata = payload.get("metadata") or {}
    stats = payload.get("stats") or {}
    song_hash = _normalize_hash(version.get("hash") or fallback_hash)
    if not song_hash:
        return None
    beatsaver_key = _normalize_key(payload.get("id") or payload.get("key") or version.get("key") or fallback_key)
    upvotes = int(stats.get("upvotes") or 0)
    downvotes = int(stats.get("downvotes") or 0)
    return {
        "hash": song_hash,
        "beatsaver_key": beatsaver_key,
        "beatsaver_page_url": _page_url_from_key(beatsaver_key),
        "beatsaver_download_url": str(version.get("downloadURL") or "") or _download_url_from_key(beatsaver_key),
        "beatsaver_cover_url": str(version.get("coverURL") or ""),
        "beatsaver_preview_url": str(version.get("previewURL") or ""),
        "beatsaver_description": str(payload.get("description") or "").replace("\r\n", "\n").strip(),
        "beatsaver_uploaded_ts": _parse_iso_datetime_to_ts(
            payload.get("lastPublishedAt") or payload.get("uploaded") or payload.get("createdAt")
        ),
        "beatsaver_rating": float(stats.get("score") or 0.0),
        "beatsaver_upvotes": upvotes,
        "beatsaver_downvotes": downvotes,
        "beatsaver_votes": upvotes + downvotes,
        "song_name": str(metadata.get("songName") or payload.get("name") or ""),
        "song_author": str(metadata.get("songAuthorName") or ""),
        "mapper": str(metadata.get("levelAuthorName") or (payload.get("uploader") or {}).get("name") or ""),
        "beatsaver_curated": bool(payload.get("curatedAt")),
        "beatsaver_verified_mapper": bool((payload.get("uploader") or {}).get("verifiedMapper")),
    }


def _seed_meta_from_hash_and_key(song_hash: object, beatsaver_key: object) -> Optional[dict]:
    normalized_hash = _normalize_hash(song_hash)
    normalized_key = _normalize_key(beatsaver_key)
    if not normalized_hash or not normalized_key:
        return None
    return {
        "hash": normalized_hash,
        "beatsaver_key": normalized_key,
        "beatsaver_page_url": _page_url_from_key(normalized_key),
        "beatsaver_download_url": _download_url_from_key(normalized_key),
        "beatsaver_cover_url": "",
        "beatsaver_preview_url": "",
        "beatsaver_description": "",
        "beatsaver_uploaded_ts": 0,
        "beatsaver_rating": 0.0,
        "beatsaver_upvotes": 0,
        "beatsaver_downvotes": 0,
        "beatsaver_votes": 0,
        "song_name": "",
        "song_author": "",
        "mapper": "",
    }


def _has_full_beatsaver_meta(entry: Optional[dict]) -> bool:
    if not isinstance(entry, dict):
        return False
    # "beatsaver_curated" キーが存在しない場合は旧フォーマットのキャッシュ → 再取得が必要
    if "beatsaver_curated" not in entry:
        return False
    return bool(
        str(entry.get("beatsaver_cover_url") or "").strip()
        or str(entry.get("beatsaver_preview_url") or "").strip()
        or str(entry.get("beatsaver_description") or "").strip()
        or int(entry.get("beatsaver_uploaded_ts") or 0) > 0
        or int(entry.get("beatsaver_votes") or 0) > 0
        or float(entry.get("beatsaver_rating") or 0.0) > 0.0
    )


def _fetch_beatsaver_map_by_hash(session: requests.Session, song_hash: str) -> Optional[dict]:
    try:
        resp = session.get(
            f"https://api.beatsaver.com/maps/hash/{song_hash}",
            timeout=_BEATSAVER_REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return None

    if isinstance(payload, list):
        for item in payload:
            meta = _meta_from_map_payload(item, fallback_hash=song_hash)
            if meta is not None:
                return meta
        return None
    return _meta_from_map_payload(payload, fallback_hash=song_hash)


def update_beatsaver_meta_cache(
    song_hashes: Iterable[str],
    session: Optional[requests.Session] = None,
    seed_map: Optional[Dict[str, str]] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, dict]:
    cache = load_beatsaver_meta_cache()
    normalized_seeds = {
        _normalize_hash(song_hash): _normalize_key(key)
        for song_hash, key in (seed_map or {}).items()
        if _normalize_hash(song_hash)
    }

    updated = False
    for song_hash, beatsaver_key in normalized_seeds.items():
        if not beatsaver_key:
            continue
        existing = cache.get(song_hash)
        if _has_full_beatsaver_meta(existing):
            continue
        seeded = _seed_meta_from_hash_and_key(song_hash, beatsaver_key)
        if seeded is not None:
            merged = _merge_meta_entry(existing or {}, seeded)
            if merged != existing:
                cache[song_hash] = merged
                updated = True

    missing_hashes = []
    for raw_hash in song_hashes:
        song_hash = _normalize_hash(raw_hash)
        if not song_hash:
            continue
        existing = cache.get(song_hash)
        if _has_full_beatsaver_meta(existing):
            continue
        missing_hashes.append(song_hash)

    if missing_hashes:
        active_session = session or requests.Session()
        total = len(missing_hashes)
        for index, song_hash in enumerate(missing_hashes, start=1):
            meta = _fetch_beatsaver_map_by_hash(active_session, song_hash)
            if meta is not None:
                cache[song_hash] = meta
                updated = True
            elif song_hash in normalized_seeds:
                seeded = _seed_meta_from_hash_and_key(song_hash, normalized_seeds[song_hash])
                if seeded is not None:
                    cache[song_hash] = seeded
                    updated = True
            if on_progress is not None:
                on_progress(index, total)

    if updated:
        _save_beatsaver_meta_cache(cache)
    return cache