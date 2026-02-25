from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import requests


BASE_URL = "https://api.accsaber.com/categories"

_PLAYLIST_URLS: Dict[str, str] = {
    "true": "https://api.accsaber.com/playlists/true",
    "standard": "https://api.accsaber.com/playlists/standard",
    "tech": "https://api.accsaber.com/playlists/tech",
}

_PLAYLIST_MAP_COUNTS_CACHE: Optional[Dict[str, int]] = None


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

    url = f"https://api.accsaber.com/countries/{country.lower()}/categories/{category}/standings"

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

    resp = session.get(url, timeout=10)
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


def get_accsaber_playlist_map_counts(session: Optional[requests.Session] = None) -> Dict[str, int]:
    """AccSaber True / Standard / Tech のプレイリストから譜面総数を取得する。

    結果はプロセス内でキャッシュされるため、複数回呼び出しても
    実際の HTTP アクセスは最初の1回のみとなる。
    取得に失敗したカテゴリは結果に含めない。
    """

    global _PLAYLIST_MAP_COUNTS_CACHE

    if _PLAYLIST_MAP_COUNTS_CACHE is not None:
        return _PLAYLIST_MAP_COUNTS_CACHE

    if session is None:
        session = requests.Session()

    counts: Dict[str, int] = {}
    for key, url in _PLAYLIST_URLS.items():
        try:
            count = _fetch_playlist_map_count(url, session=session)
        except Exception:  # noqa: BLE001
            continue
        if count > 0:
            counts[key] = count

    _PLAYLIST_MAP_COUNTS_CACHE = counts
    return counts
