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
    REST API https://api.accsaber.com/players/{steam_id} を使用する。
    404 の場合は「未参加」とみなして False を返す。
    ネットワークエラーや予期しないレスポンスなど、不確実な場合は True（参加している前提）とする。
    """

    url = f"https://api.accsaber.com/players/{steam_id}"

    try:
        resp = session.get(url, timeout=10)
    except Exception:
        return True

    if resp.status_code == 404:
        return False

    # 200 以外でも、確認不能な場合は参加していると仮定する
    return True


def _load_list_cache(path: Path, cls):
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
