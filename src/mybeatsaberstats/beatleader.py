from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
import math

import requests
import time


BASE_URL = "https://api.beatleader.xyz"


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
    try:
        # タイムアウトは短めにして UI フリーズを避ける
        resp = session.get(url, timeout=3)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    except Exception:
        # BeatLeader 側が落ちている、タイムアウトなどは呼び出し元で無視できるよう None
        return None

    try:
        data = resp.json()
    except Exception:
        return None
    # API レスポンスのフィールドを安全に取り出す
    pid = str(data.get("id") or player_id)
    name = str(data.get("name") or "")
    country = data.get("country") or None

    pp = float(data.get("pp") or 0.0)
    global_rank = int(data.get("rank") or 0)
    country_rank = int(data.get("countryRank") or 0)

    return BeatLeaderPlayer(
        id=pid,
        name=name,
        country=country,
        pp=pp,
        global_rank=global_rank,
        country_rank=country_rank,
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
                )
            )
        if page_stop:
            break
        if len(items) < page_size:
            break

    return players
