from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

import requests

from .snapshot import BASE_DIR
from .http_client import make_session
from .api_error_log import log_api_failure

_CACHE_PATH = BASE_DIR / "cache" / "beatsaver_map_details.json"
_BEATSAVER_REQUEST_TIMEOUT = (3, 10)
#: /maps/hash/ が 1 リクエストで受け付けるハッシュ数の上限
_BEATSAVER_HASH_BATCH_SIZE = 50


# BeatSaver の API ベース URL。
# api.beatsaver.com は全エンドポイントが 404 を返すようになったため、
# 本体ドメイン配下の /api を使う（ダウンロード URL は元々こちらを使っている）。
BEATSAVER_API_BASE = "https://beatsaver.com/api"


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
        if isinstance(value, bool):
            # Python では False == 0 なので、下の falsy 判定に入れると
            # beatsaver_curated / beatsaver_verified_mapper の False が
            # 「未設定」と誤判定されて書き込まれない。bool は常に採用する。
            merged[key] = value
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
    # 譜面が再アップロードされていると versions に複数版が並ぶ。
    # 先頭を無条件に採る（旧実装）と、要求した hash とは別バージョンの
    # downloadURL / coverURL がそのハッシュのエントリに書き込まれ、
    # ワンクリックダウンロードが意図しない版を落としてしまう。
    # 要求ハッシュと一致する版があれば必ずそれを使う。
    wanted_hash = _normalize_hash(fallback_hash)
    version = None
    if wanted_hash:
        version = next(
            (
                item
                for item in versions
                if isinstance(item, dict) and _normalize_hash(item.get("hash")) == wanted_hash
            ),
            None,
        )
    if version is None:
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
    return _fetch_beatsaver_maps_by_hashes(session, [song_hash]).get(_normalize_hash(song_hash))


def _fetch_beatsaver_maps_by_hashes(
    session: requests.Session, song_hashes: list[str]
) -> Dict[str, dict]:
    """BeatSaver の一括ハッシュ取得 API で、複数譜面のメタ情報をまとめて取る。

    /maps/hash/{hash1,hash2,...} は 1 リクエストで最大 50 件を返せる。
    以前は 1 ハッシュ = 1 リクエストだったため、初回同期で数千リクエストが
    連続していた。まとめ取りでリクエスト数を約 1/50 に減らす。
    """
    normalized = [_normalize_hash(h) for h in song_hashes]
    normalized = [h for h in normalized if h]
    if not normalized:
        return {}

    try:
        resp = session.get(
            f"{BEATSAVER_API_BASE}/maps/hash/{','.join(normalized)}",
            timeout=_BEATSAVER_REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        # まとめ取りなので 1 回の失敗で最大 50 件が欠ける。
        # 黙って捨てると「なぜかカバーが出ない」状態の原因が追えないため記録する。
        log_api_failure(
            "beatsaver",
            "_fetch_beatsaver_maps_by_hashes",
            f"bulk hash lookup failed hashes={len(normalized)}",
            exc,
        )
        return {}

    results: Dict[str, dict] = {}

    def _absorb(item: object, requested_hash: str = "") -> None:
        """map ペイロードを meta 化して、要求したハッシュをキーに格納する。

        譜面が更新されていると versions に複数版が入り、meta 側の hash が
        こちらの要求した hash と食い違うことがある。呼び出し元は要求した hash で
        引くので、必ず要求 hash をキーにする（meta の hash をキーにすると
        毎回「未取得」と判定され、同じ hash を永久に取りに行ってしまう）。
        """
        if not isinstance(item, dict):
            return
        meta = _meta_from_map_payload(item, fallback_hash=requested_hash)
        if meta is None:
            return
        key = requested_hash or _normalize_hash(meta.get("hash"))
        if not key:
            return
        # キャッシュの引き当ては要求ハッシュで行うため、hash フィールドも揃えておく
        meta["hash"] = key
        results[key] = meta

    single_map_payload = isinstance(payload, dict) and bool(payload.get("versions"))

    if isinstance(payload, dict) and not single_map_payload:
        # 複数指定時は {"<hash小文字>": <map>, ...} 形式で返る
        for raw_hash, item in payload.items():
            _absorb(item, requested_hash=_normalize_hash(raw_hash))
    elif isinstance(payload, list):
        for item in payload:
            _absorb(item)
    else:
        # 単一ハッシュ指定時は map オブジェクトそのものが返る
        _absorb(payload, requested_hash=normalized[0] if len(normalized) == 1 else "")

    return results


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
        active_session = session or make_session()
        total = len(missing_hashes)
        done = 0
        for offset in range(0, total, _BEATSAVER_HASH_BATCH_SIZE):
            batch = missing_hashes[offset:offset + _BEATSAVER_HASH_BATCH_SIZE]
            fetched = _fetch_beatsaver_maps_by_hashes(active_session, batch)
            for song_hash in batch:
                meta = fetched.get(song_hash)
                if meta is not None:
                    cache[song_hash] = meta
                    updated = True
                elif song_hash in normalized_seeds:
                    seeded = _seed_meta_from_hash_and_key(song_hash, normalized_seeds[song_hash])
                    if seeded is not None:
                        # シードは中身が空の骨組みなので、素で代入すると既存の
                        # カバー URL や曲名を消してしまう。必ずマージすること
                        # （バッチ取得が 1 回失敗すると最大 50 件が巻き添えになる）。
                        merged = _merge_meta_entry(cache.get(song_hash) or {}, seeded)
                        if merged != cache.get(song_hash):
                            cache[song_hash] = merged
                            updated = True
                done += 1
                if on_progress is not None:
                    on_progress(done, total)
            # 大量に取得する場合でも、途中で落ちた分を捨てずに済むよう逐次保存する
            if updated:
                _save_beatsaver_meta_cache(cache)
                updated = False

    if updated:
        _save_beatsaver_meta_cache(cache)
    return cache