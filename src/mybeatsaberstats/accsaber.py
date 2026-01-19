from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import re
import requests
from bs4 import BeautifulSoup


BASE_URL = "https://accsaber.com/leaderboards/overall"

_PLAYLIST_URLS: Dict[str, str] = {
    "true": "https://api.accsaber.com/playlists/true",
    "standard": "https://api.accsaber.com/playlists/standard",
    "tech": "https://api.accsaber.com/playlists/tech",
}

_PLAYLIST_MAP_COUNTS_CACHE: Optional[Dict[str, int]] = None


# AccSaber グローバルランキングをキャッシュする際の下限 AP
ACCSABER_MIN_AP_GLOBAL = 10000.0

# True / Standard / Tech のランキングで扱う下限 AP
ACCSABER_MIN_AP_SKILL = 3000.0


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
    base_url: str,
    country: Optional[str] = None,
    page: int = 1,
    session: Optional[requests.Session] = None,
) -> List[AccSaberPlayer]:
    """指定されたリーダーボードURLから 1 ページ分のランキングを取得する共通処理。"""

    if session is None:
        session = requests.Session()

    # requests の params は文字列系を想定しているので str に揃える
    params: dict[str, str] = {"page": str(page)}
    if country:
        params["country"] = country.lower()

    resp = session.get(base_url, params=params, timeout=10)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if table is None:
        return []

    players: List[AccSaberPlayer] = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        cells = [td.get_text(strip=True) for td in tds]
        # 期待する列数: 8 (#, avatar, name, total, avg, plays, top, device)
        if len(cells) < 7:
            continue

        try:
            rank_str = cells[0].lstrip("#")
            rank = int(rank_str)
        except ValueError:
            continue

        # プレイヤー名とプロフィールリンクから ScoreSaber/Steam の ID を推定
        name = cells[2]

        scoresaber_id: Optional[str] = None
        # プレイヤー名のセル内リンクから ID と思しき数値列を抽出する
        if len(tds) >= 3:
            name_link = tds[2].find("a", href=True)
            if name_link is not None:
                href_val = name_link.get("href")
                href_str = str(href_val or "")
                # href 中に含まれる数字列のうち、最も長いものを ID とみなす
                nums = re.findall(r"(\d+)", href_str)
                if nums:
                    scoresaber_id = max(nums, key=len)
        total_ap = cells[3]
        average_acc = cells[4]
        plays = cells[5]
        top_play_pp = cells[6]

        players.append(
            AccSaberPlayer(
                rank=rank,
                name=name,
                total_ap=total_ap,
                average_acc=average_acc,
                plays=plays,
                top_play_pp=top_play_pp,
                scoresaber_id=scoresaber_id,
            )
        )

    return players


def fetch_overall(
    country: Optional[str] = None,
    page: int = 1,
    session: Optional[requests.Session] = None,
) -> List[AccSaberPlayer]:
    """AccSaber のオーバーオールランキングを 1 ページ分取得する。"""

    return _fetch_leaderboard(BASE_URL, country=country, page=page, session=session)


def fetch_true(
    country: Optional[str] = None,
    page: int = 1,
    session: Optional[requests.Session] = None,
) -> List[AccSaberPlayer]:
    """True Acc リーダーボードを 1 ページ分取得する。"""

    return _fetch_leaderboard("https://accsaber.com/leaderboards/true", country=country, page=page, session=session)


def fetch_standard(
    country: Optional[str] = None,
    page: int = 1,
    session: Optional[requests.Session] = None,
) -> List[AccSaberPlayer]:
    """Standard Acc リーダーボードを 1 ページ分取得する。"""

    return _fetch_leaderboard("https://accsaber.com/leaderboards/standard", country=country, page=page, session=session)


def fetch_tech(
    country: Optional[str] = None,
    page: int = 1,
    session: Optional[requests.Session] = None,
) -> List[AccSaberPlayer]:
    """Tech Acc リーダーボードを 1 ページ分取得する。"""

    return _fetch_leaderboard("https://accsaber.com/leaderboards/tech", country=country, page=page, session=session)


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
