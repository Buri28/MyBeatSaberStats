from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import requests

from ..snapshot import BASE_DIR
from ..accsaber import AccSaberPlayer, fetch_overall, ACCSABER_MIN_AP_SKILL

CACHE_DIR = BASE_DIR / "cache"


def _accsaber_profile_exists(steam_id: str, session: requests.Session) -> bool:
    """指定した SteamID の AccSaber プロフィールが存在するかを確認する。

    Remix の data ルート
    https://accsaber.com/profile/{steam_id}?page=1&_data=routes%2Fprofile%2F%24playerId%2F%28%24category%29%2F%28scores%29
    にアクセスし、JSON の totalCount が 0 の場合は「未参加」とみなして False を返す。

    ネットワークエラーや JSON でないレスポンスなど、不確実な場合は True（参加している前提）とする。
    """
    
    print("Entering _accsaber_profile_exists")
    
    base_url = f"https://accsaber.com/profile/{steam_id}"
    params = {
        "page": "1",
        "_data": "routes/profile/$playerId/($category)/(scores)",
    }

    try:
        resp = session.get(base_url, params=params, timeout=10)
    except Exception:
        return True

    if resp.status_code == 404:
        return False

    try:
        data = resp.json()
    except Exception:
        return True

    scores_obj = data.get("scores") or {}
    total = scores_obj.get("totalCount")
    if total is None:
        return True
    total_int = int(total)

    return total_int > 0


def _load_list_cache(path: Path, cls):
    print("Entering _load_list_cache")
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [cls(**item) for item in data if isinstance(item, dict)]
    except Exception:
        return []
    return []


def _find_accsaber_for_scoresaber_id(
    scoresaber_id: str,
    session: Optional[requests.Session] = None,
) -> Optional[AccSaberPlayer]:
    print("Entering _find_accsaber_for_scoresaber_id")
    if not scoresaber_id:
        return None

    acc_path = CACHE_DIR / "accsaber_ranking.json"
    players = _load_list_cache(acc_path, AccSaberPlayer)
    for p in players:
        if getattr(p, "scoresaber_id", None) == scoresaber_id:
            return p

    if session is None:
        return None

    try:
        return _find_accsaber_skill_for_scoresaber_id(
            scoresaber_id,
            fetch_overall,
            session=session,
            max_pages=200,
        )
    except Exception:
        return None


def _find_accsaber_skill_for_scoresaber_id(
    scoresaber_id: str,
    fetch_func,
    session: Optional[requests.Session] = None,
    max_pages: int = 200,
) -> Optional[AccSaberPlayer]:
    print("Entering _find_accsaber_skill_for_scoresaber_id")
    if not scoresaber_id:
        return None

    def _parse_ap(text: str | None) -> float:
        if not text:
            return 0.0
        import re as _re

        t = text.replace(",", "")
        m = _re.search(r"[-+]?\d*\.?\d+", t)
        if not m:
            return 0.0
        try:
            return float(m.group(0))
        except ValueError:
            return 0.0

    for page in range(1, max_pages + 1):
        try:
            players = fetch_func(country=None, page=page, session=session)
        except Exception:
            break

        if not players:
            break

        for p in players:
            if getattr(p, "scoresaber_id", None) == scoresaber_id:
                return p

        last_ap = _parse_ap(getattr(players[-1], "total_ap", ""))
        if last_ap < ACCSABER_MIN_AP_SKILL:
            break

    return None
