from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import requests


BASE_URL = "https://scoresaber.com/api/players"


@dataclass
class ScoreSaberPlayer:
    id: str
    name: str
    country: str
    pp: float
    global_rank: int
    country_rank: int
    # Ranked play count (if available from API)
    ranked_play_count: int | None = None


def fetch_players(
    country: Optional[str] = None,
    page: int = 1,
    session: Optional[requests.Session] = None,
) -> List[ScoreSaberPlayer]:
    """ScoreSaber のプレイヤー一覧を 1 ページ分取得する。

    country を指定すると、その国のランキングにフィルタされる。
    """

    if session is None:
        session = requests.Session()

    # requests の params は文字列系を想定しているので str に揃える
    params: dict[str, str] = {"page": str(page)}
    if country:
        params["countries"] = country.upper()

    resp = session.get(BASE_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    players_data = data.get("players", [])
    players: List[ScoreSaberPlayer] = []
    for p in players_data:
        try:
            players.append(
                ScoreSaberPlayer(
                    id=str(p.get("id", "")),
                    name=str(p.get("name", "")),
                    country=str(p.get("country", "")),
                    pp=float(p.get("pp", 0.0)),
                    global_rank=int(p.get("rank", 0)),
                    country_rank=int(p.get("countryRank", 0)),
                    ranked_play_count=(
                        int(p.get("rankedPlayCount")) if p.get("rankedPlayCount") is not None else None
                    ),
                )
            )
        except (TypeError, ValueError):
            continue

    return players
