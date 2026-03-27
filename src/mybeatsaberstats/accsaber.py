from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from .snapshot import BASE_DIR

BASE_URL = "https://accsaber.com/api/categories"

_PLAYLIST_URLS: Dict[str, str] = {
    "true": "https://accsaber.com/api/playlists/true",
    "standard": "https://accsaber.com/api/playlists/standard",
    "tech": "https://accsaber.com/api/playlists/tech",
}

# in-memory cache: Optional[Tuple[counts, fetched_ats, from_cache_flags]]
_PLAYLIST_MAP_COUNTS_CACHE: Optional[Tuple[Dict[str, int], Dict[str, Optional[str]], Dict[str, bool]]] = None
_PLAYLIST_COUNTS_CACHE_FILE: Path = BASE_DIR / "cache" / "accsaber_playlist_counts.json"


def _load_playlist_file_cache() -> Dict[str, Dict]:
    """ファイルキャッシュから前回の総譜面数を読み込む。

    Returns: {"true": {"count": 74, "fetched_at": "..."}, ...}
    """
    try:
        data = json.loads(_PLAYLIST_COUNTS_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            result: Dict[str, Dict] = {}
            for k in ("true", "standard", "tech"):
                entry = data.get(k)
                if isinstance(entry, dict):
                    count = entry.get("count")
                    fat = entry.get("fetched_at")
                    if isinstance(count, (int, float)) and count > 0:
                        result[k] = {"count": int(count), "fetched_at": fat}
            return result
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_playlist_file_cache(per_cat: Dict[str, Dict]) -> None:
    """総譜面数をファイルキャッシュに保存する。"""
    try:
        _PLAYLIST_COUNTS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PLAYLIST_COUNTS_CACHE_FILE.write_text(
            json.dumps(per_cat, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


# AccSaber グローバルランキングをキャッシュする際の下限 AP
# AccSaber の総登録者数が少ない（~1700人程度）ため全件取得する
ACCSABER_MIN_AP_GLOBAL = 0.0

# True / Standard / Tech のランキングで扱う下限 AP
# AccSaber の各カテゴリも総登録者数が少ない（~1500人程度）ため全件取得する
ACCSABER_MIN_AP_SKILL = 0.0


@dataclass
class AccSaberPlayer:
    rank: int
    name: str
    total_ap: str
    average_acc: str
    plays: str
    top_play_pp: str
    true_ap: str = ""
    standard_ap: str = ""
    tech_ap: str = ""
    scoresaber_id: Optional[str] = None


def _fetch_leaderboard(
    category: str,
    country: Optional[str] = None,
    page: int = 1,
    session: Optional[requests.Session] = None,
) -> List[AccSaberPlayer]:
    """AccSaber 公式 REST API からカテゴリ別ランキングを取得する。

    https://api.accsaber.com/categories/{category}/standings を使用する。
    API は全プレイヤーを一括返却するため page=1 のみデータを返し、
    page>1 は空リストを返す（ページング打ち切り対応）。
    """

    if session is None:
        session = requests.Session()

    # API は全件を一括返却するので page>1 は空
    if page > 1:
        return []

    url = f"{BASE_URL}/{category}/standings"

    resp = session.get(url, timeout=30)
    resp.raise_for_status()

    try:
        data = resp.json()
    except ValueError:
        return []

    if not isinstance(data, list):
        return []

    players: List[AccSaberPlayer] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue

        player_id = entry.get("playerId")
        if not player_id:
            continue

        try:
            rank = int(entry.get("rank", 0))
        except (ValueError, TypeError):
            continue

        players.append(
            AccSaberPlayer(
                rank=rank,
                name=str(entry.get("playerName", "")),
                total_ap=str(entry.get("ap", "0")),
                average_acc=str(entry.get("averageAcc", "")),
                plays=str(entry.get("rankedPlays", "")),
                top_play_pp=str(entry.get("averageApPerMap", "")),
                scoresaber_id=str(player_id),
            )
        )

    # country フィルタが指定された場合はクライアント側でフィルタ
    if country:
        players = _filter_by_country(players, country, category, session)

    return players


def _filter_by_country(
    players: List[AccSaberPlayer],
    country: str,
    category: str,
    session: requests.Session,
) -> List[AccSaberPlayer]:
    """国別ランキングを取得する（api.accsaber.com/countries/{country}/categories/{category}/standings）。"""

    url = f"https://accsaber.com/api/countries/{country.lower()}/categories/{category}/standings"

    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        # 国別エンドポイントが使えない場合は全データ内から avatarUrl や playerName でのフィルタは不可なので空返却
        return []

    if not isinstance(data, list):
        return []

    result: List[AccSaberPlayer] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        player_id = entry.get("playerId")
        if not player_id:
            continue
        try:
            rank = int(entry.get("rank", 0))
        except (ValueError, TypeError):
            continue
        result.append(
            AccSaberPlayer(
                rank=rank,
                name=str(entry.get("playerName", "")),
                total_ap=str(entry.get("ap", "0")),
                average_acc=str(entry.get("averageAcc", "")),
                plays=str(entry.get("rankedPlays", "")),
                top_play_pp=str(entry.get("averageApPerMap", "")),
                scoresaber_id=str(player_id),
            )
        )
    return result


def fetch_overall(
    country: Optional[str] = None,
    page: int = 1,
    session: Optional[requests.Session] = None,
) -> List[AccSaberPlayer]:
    """AccSaber のオーバーオールランキングを 1 ページ分取得する。"""

    return _fetch_leaderboard("overall", country=country, page=page, session=session)


def fetch_true(
    country: Optional[str] = None,
    page: int = 1,
    session: Optional[requests.Session] = None,
) -> List[AccSaberPlayer]:
    """True Acc リーダーボードを 1 ページ分取得する。"""

    return _fetch_leaderboard("true", country=country, page=page, session=session)


def fetch_standard(
    country: Optional[str] = None,
    page: int = 1,
    session: Optional[requests.Session] = None,
) -> List[AccSaberPlayer]:
    """Standard Acc リーダーボードを 1 ページ分取得する。"""

    return _fetch_leaderboard("standard", country=country, page=page, session=session)


def fetch_tech(
    country: Optional[str] = None,
    page: int = 1,
    session: Optional[requests.Session] = None,
) -> List[AccSaberPlayer]:
    """Tech Acc リーダーボードを 1 ページ分取得する。"""

    return _fetch_leaderboard("tech", country=country, page=page, session=session)


def fetch_overall_jp(page: int = 1, session: Optional[requests.Session] = None) -> List[AccSaberPlayer]:
    """日本オーバーオールランキングのショートカット。"""

    return fetch_overall(country="jp", page=page, session=session)


def _count_maps_in_playlist_obj(obj: dict) -> int:
    """AccSaber の playlist JSON オブジェクトから譜面総数を数える。

    Beat Saber のプレイリスト形式を前提とし、各 song の difficulties を数える。
    difficulties が無い/空の場合は 1 譜面として扱う。
    """

    songs = obj.get("songs")
    if not isinstance(songs, list):
        return 0

    total = 0
    for song in songs:
        if not isinstance(song, dict):
            continue
        diffs = song.get("difficulties")
        if isinstance(diffs, list) and diffs:
            total += len(diffs)
        else:
            total += 1
    return total


def _fetch_playlist_map_count(url: str, session: Optional[requests.Session] = None) -> int:
    """指定 URL の AccSaber playlist から譜面総数を取得する。"""

    if session is None:
        session = requests.Session()

    resp = session.get(url, timeout=30)
    resp.raise_for_status()

    try:
        data = resp.json()
    except ValueError:
        return 0

    # playlist オブジェクト 1 つ、もしくは playlist 配列のどちらにも対応する
    if isinstance(data, dict):
        return _count_maps_in_playlist_obj(data)
    if isinstance(data, list):
        total = 0
        for item in data:
            if isinstance(item, dict):
                total += _count_maps_in_playlist_obj(item)
        return total
    return 0


def get_accsaber_playlist_map_counts_from_cache(
) -> Tuple[Dict[str, int], Dict[str, Optional[str]], Dict[str, bool]]:
    """ファイルキャッシュだけを読んで AccSaber の総譜面数とメタ情報を返す。API は叩かない。

    表示目的専用。API 更新は TakeSnapshot / Fetch Ranking Data のタイミングでのみ行う。
    """
    file_cache = _load_playlist_file_cache()
    counts: Dict[str, int] = {}
    fetched_ats: Dict[str, Optional[str]] = {}
    from_cache_flags: Dict[str, bool] = {}
    for key in ("true", "standard", "tech"):
        if key in file_cache:
            counts[key] = file_cache[key]["count"]
            fetched_ats[key] = file_cache[key].get("fetched_at")
            from_cache_flags[key] = False  # 表示目的の読み取りは通常動作。警告を出さない。
    return counts, fetched_ats, from_cache_flags


def get_accsaber_playlist_map_counts_with_meta(
    session: Optional[requests.Session] = None,
) -> Tuple[Dict[str, int], Dict[str, Optional[str]], Dict[str, bool]]:
    """AccSaber True/Standard/Tech の譜面総数とメタ情報を返す。API を叩いて更新する。

    Returns:
        counts       : {"true": 74, "standard": 200, "tech": 156}
        fetched_ats  : {"true": "2026-03-01T12:00:00Z", ...}  取得成功日時
        from_cache   : {"true": True, ...}  True = ファイルキャッシュから取得

    API 取得に失敗したカテゴリはファイルキャッシュの前回値を使用する。
    全カテゴリが空の場合はメモリキャッシュしない（次回再取得を試みる）。
    """

    global _PLAYLIST_MAP_COUNTS_CACHE

    if _PLAYLIST_MAP_COUNTS_CACHE is not None:
        return _PLAYLIST_MAP_COUNTS_CACHE

    if session is None:
        session = requests.Session()

    file_cache = _load_playlist_file_cache()
    counts: Dict[str, int] = {}
    fetched_ats: Dict[str, Optional[str]] = {}
    from_cache_flags: Dict[str, bool] = {}
    now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    per_cat_for_file: Dict[str, Dict] = dict(file_cache)  # 既存ファイル値を保持しつつ更新
    updated = False

    for key, url in _PLAYLIST_URLS.items():
        fresh_count = 0
        try:
            fresh_count = _fetch_playlist_map_count(url, session=session)
        except Exception:  # noqa: BLE001
            pass

        if fresh_count > 0:
            counts[key] = fresh_count
            fetched_ats[key] = now_str
            from_cache_flags[key] = False
            per_cat_for_file[key] = {"count": fresh_count, "fetched_at": now_str}
            updated = True
        elif key in file_cache:
            # API 失敗 → ファイルキャッシュの前回値を使用
            counts[key] = file_cache[key]["count"]
            fetched_ats[key] = file_cache[key].get("fetched_at")
            from_cache_flags[key] = True
        # else: ファイルキャッシュにも無い → このカテゴリはスキップ

    if updated:
        _save_playlist_file_cache(per_cat_for_file)

    result: Tuple[Dict[str, int], Dict[str, Optional[str]], Dict[str, bool]] = (
        counts,
        fetched_ats,
        from_cache_flags,
    )
    # 空の場合はメモリキャッシュしない（次回再取得を試みる）
    if counts:
        _PLAYLIST_MAP_COUNTS_CACHE = result
    return result


def get_accsaber_playlist_map_counts(session: Optional[requests.Session] = None) -> Dict[str, int]:
    """互換 API: 総譜面数のみを返す。"""
    counts, _, _ = get_accsaber_playlist_map_counts_with_meta(session)
    return counts
