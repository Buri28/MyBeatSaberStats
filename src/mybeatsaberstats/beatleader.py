from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import traceback

import requests
import time

from .snapshot import BASE_DIR


BASE_URL = "https://api.beatleader.xyz"
LOG_DIR = BASE_DIR / "logs"
LOG_PATH = LOG_DIR / "beatleader_api.log"
_PRESTIGE_LEVELS_CACHE: list[dict] | None = None


def _log_api_failure(api_name: str, message: str, exc: Optional[BaseException] = None) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        lines = [
            f"[{datetime.now().isoformat(timespec='seconds')}] {api_name}",
            f"message: {message}",
        ]
        if exc is not None:
            lines.append(f"error: {exc.__class__.__name__}: {exc}")
            lines.append("traceback:")
            lines.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())
        lines.append("")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except Exception:
        pass


@dataclass
class BeatLeaderPlayer:
    """ BeatLeader のプレイヤー情報。"""
    id: str
    name: str
    country: Optional[str]
    pp: float
    global_rank: int
    country_rank: int
    # Ranked play count if API exposes it
    ranked_play_count: int | None = None
    level: int | None = None
    experience: int | None = None
    prestige: int | None = None
    prestige_icon_url: str | None = None


def _safe_int(value: int | float | str | None) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_prestige_levels(session: requests.Session) -> list[dict]:
    global _PRESTIGE_LEVELS_CACHE
    if _PRESTIGE_LEVELS_CACHE is not None:
        return _PRESTIGE_LEVELS_CACHE

    url = f"{BASE_URL}/experience/levels"
    try:
        resp = session.get(url, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        _log_api_failure("_load_prestige_levels", f"Failed to load prestige levels url={url}", exc)
        _PRESTIGE_LEVELS_CACHE = []
        return _PRESTIGE_LEVELS_CACHE

    _PRESTIGE_LEVELS_CACHE = [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
    return _PRESTIGE_LEVELS_CACHE


def _resolve_prestige_icon_url(session: requests.Session, prestige: int | None) -> str | None:
    if prestige is None or prestige < 0:
        return None
    for item in _load_prestige_levels(session):
        if _safe_int(item.get("level")) == prestige:
            icon_url = str(item.get("smallIcon") or item.get("bigIcon") or "").strip()
            return icon_url or None
    return None


def fetch_player(player_id: str, session: Optional[requests.Session] = None) -> Optional[BeatLeaderPlayer]:
    """BeatLeader の単一プレイヤー情報を ID から取得する。

    player_id は SteamID / OculusID などの BeatLeader / ScoreSaber 共通 ID を想定。
    見つからない場合やエラー時は None を返す。
    """

    if not player_id:
        return None

    if session is None:
        session = requests.Session()

    url = f"{BASE_URL}/player/{player_id}"
    timeouts = (3, 5, 8)
    resp = None
    for attempt, timeout_sec in enumerate(timeouts, start=1):
        try:
            # 初回起動時は BeatLeader 側の応答が不安定なことがあるため、短い retry を行う。
            resp = session.get(url, timeout=timeout_sec)
            if resp.status_code == 404:
                _log_api_failure("fetch_player", f"404 Not Found url={url} player_id={player_id}")
                return None
            resp.raise_for_status()
            break
        except requests.HTTPError as exc:
            _log_api_failure(
                "fetch_player",
                f"HTTP error url={url} player_id={player_id} attempt={attempt}/{len(timeouts)} timeout={timeout_sec}",
                exc,
            )
            return None
        except requests.RequestException as exc:
            _log_api_failure(
                "fetch_player",
                f"Request failed url={url} player_id={player_id} attempt={attempt}/{len(timeouts)} timeout={timeout_sec}",
                exc,
            )
            resp = None
            if attempt >= len(timeouts):
                return None
            time.sleep(0.35 * attempt)
    if resp is None:
        return None

    try:
        data = resp.json()
    except Exception as exc:
        _log_api_failure("fetch_player", f"Invalid JSON url={url} player_id={player_id}", exc)
        return None
    # API レスポンスのフィールドを安全に取り出す
    pid = str(data.get("id") or player_id)
    name = str(data.get("name") or "")
    country = data.get("country") or None

    pp = float(data.get("pp") or 0.0)
    global_rank = int(data.get("rank") or 0)
    country_rank = int(data.get("countryRank") or 0)
    level = _safe_int(data.get("level"))
    experience = _safe_int(data.get("experience"))
    prestige = _safe_int(data.get("prestige"))

    return BeatLeaderPlayer(
        id=pid,
        name=name,
        country=country,
        pp=pp,
        global_rank=global_rank,
        country_rank=country_rank,
        level=level,
        experience=experience,
        prestige=prestige,
        prestige_icon_url=_resolve_prestige_icon_url(session, prestige),
    )


def fetch_players_ranking(
    min_pp: float = 0.0,
    session: Optional[requests.Session] = None,
    page_size: int = 100,
    max_pages: int = 200,
    progress: Optional[Callable[[int, int], None]] = None,
    country: Optional[str] = None,
    max_workers: int = 5,
) -> List[BeatLeaderPlayer]:
    """BeatLeader のランキングからプレイヤー一覧を取得する。

    /players エンドポイントをページングしながら取得する。
    max_workers 並列でページを同時取得するため、逐次取得に比べて大幅に高速化される。
    min_pp を指定すると、その PP 以上のプレイヤーだけを対象にする。

    country に 2 文字の国コード ("JP" など) を指定すると、サーバ側でフィルタを掛ける。
    """

    if session is None:
        session = requests.Session()

    params_base: dict[str, str] = {
        "sortBy": "pp",
        "order": "desc",
        "count": str(page_size),
    }
    if country:
        params_base["countries"] = country.upper()

    def _fetch_single_page(page: int) -> tuple[int, list, dict]:
        """1ページ分を取得して (page, items, metadata) を返す。エラー時は空リストを返す。"""
        params = dict(params_base)
        params["page"] = str(page)
        retries = 0
        while True:
            try:
                resp = session.get(f"{BASE_URL}/players", params=params, timeout=15)
            except requests.exceptions.ReadTimeout:
                retries += 1
                if retries >= 3:
                    return page, [], {}
                time.sleep(5.0)
                continue
            except Exception:
                return page, [], {}

            if resp.status_code == 429:
                retries += 1
                retry_after_header = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after_header) if retry_after_header else 10.0
                except (TypeError, ValueError):
                    wait = 10.0
                if retries >= 5:
                    return page, [], {}
                time.sleep(wait)
                continue

            if resp.status_code != 200:
                return page, [], {}

            try:
                data = resp.json()
            except Exception:
                return page, [], {}

            return page, data.get("data") or [], data.get("metadata") or {}

    # ──────────────────────────────────────────────
    # Phase 1: ページ 1 を取得してメタデータの total を得る
    # ──────────────────────────────────────────────
    _, page1_items, meta1 = _fetch_single_page(1)
    total: int = int(meta1.get("total") or 0)

    if not page1_items:
        return []

    # total から必要ページ数を推定（上限は max_pages）
    if total > 0:
        pages_needed = min(max_pages, math.ceil(total / page_size))
    else:
        pages_needed = max_pages

    if progress is not None:
        try:
            progress(1, pages_needed)
        except RuntimeError:
            raise
        except Exception:
            pass

    # ──────────────────────────────────────────────
    # Phase 2: 残りのページを並行取得
    # ──────────────────────────────────────────────
    all_page_items: dict[int, list] = {1: page1_items}
    fetched_pages = 1  # すでにページ 1 は取得済み

    if pages_needed > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_fetch_single_page, pg): pg
                for pg in range(2, pages_needed + 1)
            }
            for future in as_completed(futures):
                pg_result = future.result()
                pg = pg_result[0]
                items = pg_result[1]
                all_page_items[pg] = items
                fetched_pages += 1
                if progress is not None:
                    try:
                        progress(fetched_pages, pages_needed)
                    except RuntimeError:
                        raise  # キャンセルなど RuntimeError は呼び出し元に伝播させる
                    except Exception:
                        pass

    # ──────────────────────────────────────────────
    # Phase 3: ページ順に並べて min_pp フィルタを掛けながら集約
    # pp は降順なので、page N の末尾が min_pp 未満になった時点で残ページも不要
    # ──────────────────────────────────────────────
    players: List[BeatLeaderPlayer] = []
    for pg in range(1, pages_needed + 1):
        items = all_page_items.get(pg, [])
        if not items:
            break

        page_stop = False
        for p in items:
            try:
                pp = float(p.get("pp", 0.0))
            except (TypeError, ValueError):
                continue
            if min_pp > 0 and pp < min_pp:
                page_stop = True
                break
            players.append(
                BeatLeaderPlayer(
                    id=str(p.get("id", "")),
                    name=str(p.get("name", "")),
                    country=(p.get("country") or None),
                    pp=pp,
                    global_rank=int(p.get("rank", 0)),
                    country_rank=int(p.get("countryRank", 0)),
                    level=_safe_int(p.get("level")),
                    experience=_safe_int(p.get("experience")),
                    prestige=_safe_int(p.get("prestige")),
                    prestige_icon_url=_resolve_prestige_icon_url(session, _safe_int(p.get("prestige"))),
                )
            )
        if page_stop:
            break
        if len(items) < page_size:
            break

    return players
