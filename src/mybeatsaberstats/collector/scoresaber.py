from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
from typing import Optional, Callable

import math
import requests

from ..snapshot import BASE_DIR, StarClearStat

CACHE_DIR = BASE_DIR / "cache"

SCORESABER_LEADERBOARDS_URL = "https://scoresaber.com/api/leaderboards"
SCORESABER_PLAYER_SCORES_URL = "https://scoresaber.com/api/player/{player_id}/scores"
SCORESABER_PLAYER_FULL_URL = "https://scoresaber.com/api/player/{player_id}/full"


def _load_cached_pages(path: Path) -> Optional[list[dict]]:
    print("Entering _load_cached_pages")
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        pages = raw.get("pages")
        if isinstance(pages, list):
            return pages
    except Exception:
        return None
    return None


def _save_cached_pages(path: Path, pages: list[dict]) -> None:
    print("Entering _save_cached_pages")
    payload = {
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "pages": pages,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return


def _get_scoresaber_leaderboards_ranked(
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> list[dict]:
    print("Entering _get_scoresaber_leaderboards_ranked")
    cache_path = CACHE_DIR / "scoresaber_ranked_maps.json"
    
    page = 1

    page_size = 50

    cached_pages = _load_cached_pages(cache_path)

    if cached_pages is not None:
        pages: list[dict] = []
        leaderboards: list[dict] = []

        for page in cached_pages:
            if not isinstance(page, dict):
                continue
            pages.append(page)
            data = page.get("data") or {}
            items = data.get("leaderboards") or data.get("data") or []
            if isinstance(items, list):
                leaderboards.extend(lb for lb in items if isinstance(lb, dict))

        cached_total = len(leaderboards)
        if pages:
            try:
                first_meta = (pages[0].get("data") or {}).get("metadata") or {}
                cached_total = int(first_meta.get("total", cached_total))
            except Exception:
                cached_total = len(leaderboards)

        try:
            params_first = {"page": "1", "count": str(page_size), "ranked": "true"}
            resp = session.get(SCORESABER_LEADERBOARDS_URL, params=params_first, timeout=10)
            if resp.status_code != 404:
                resp.raise_for_status()
                data_first = resp.json()
                meta = data_first.get("metadata") or {}
                try:
                    new_total = int(meta.get("total", cached_total))
                except Exception:
                    new_total = cached_total

                if new_total <= cached_total:
                    if progress is not None:
                        progress(1, 1)
                    return leaderboards

                pages = []
                leaderboards = []

                page = 1
                while True:
                    params = {"page": str(page), "count": str(page_size), "ranked": "true"}
                    if page == 1:
                        data = data_first
                    else:
                        resp_page = session.get(SCORESABER_LEADERBOARDS_URL, params=params, timeout=10)
                        if resp_page.status_code == 404:
                            break
                        resp_page.raise_for_status()
                        data = resp_page.json()

                    pages.append({"page": page, "params": params, "data": data})

                    items = data.get("leaderboards") if isinstance(data, dict) else None
                    if items is None and isinstance(data, dict):
                        items = data.get("data")
                    if not isinstance(items, list) or not items:
                        break

                    leaderboards.extend(lb for lb in items if isinstance(lb, dict))

                    if len(items) < page_size:
                        break

                    page += 1

                if pages:
                    try:
                        _save_cached_pages(cache_path, pages)
                    except Exception:
                        pass

                if progress is not None:
                    progress(page, None)
                return leaderboards
        except Exception:
            # メタデータ確認に失敗した場合は、既存キャッシュをそのまま返す
            if progress is not None:
                progress(1, 1)
            return leaderboards

    else:
        pages = []
        leaderboards = []
        page = 1
        while True:
            params = {"page": str(page), "count": str(page_size), "ranked": "true"}
            resp = session.get(SCORESABER_LEADERBOARDS_URL, params=params, timeout=10)
            if resp.status_code == 404:
                break
            resp.raise_for_status()
            data = resp.json()

            pages.append({"page": page, "params": params, "data": data})

            items = data.get("leaderboards") if isinstance(data, dict) else None
            if items is None and isinstance(data, dict):
                items = data.get("data")
            if not isinstance(items, list) or not items:
                break

            leaderboards.extend(lb for lb in items if isinstance(lb, dict))

            if len(items) < page_size:
                break

            page += 1

    if pages:
        try:
            _save_cached_pages(cache_path, pages)
        except Exception:
            pass
    
    if progress is not None:
        progress(int(page), None)  # type: ignore

    return leaderboards


def _get_scoresaber_player_scores(scoresaber_id: str, session: requests.Session, progress: Optional[Callable[[int, Optional[int]], None]] = None) -> list[dict]:
    print("Entering _get_scoresaber_player_scores")
    cache_path = CACHE_DIR / f"scoresaber_player_scores_{scoresaber_id}.json"

    pages = _load_cached_pages(cache_path) or []

    if pages:
        data: dict = pages[0].get("data") or {}
        items = data.get("scores") or data.get("data") or []
        if isinstance(items, list):
            return items

    page = 1
    all_scores: list[dict] = []

    while True:
        url = SCORESABER_PLAYER_SCORES_URL.format(player_id=scoresaber_id)
        params = {"page": str(page), "count": "100"}
        resp = session.get(url, params=params, timeout=10)
        if resp.status_code == 404:
            break
        resp.raise_for_status()
        data = resp.json()

        items = data.get("scores") or data.get("data") or []
        if not isinstance(items, list) or not items:
            break

        all_scores.extend(item for item in items if isinstance(item, dict))

        pages.append({"page": page, "params": params, "data": data})

        if len(items) < 100:
            break

        page += 1

    if pages:
        try:
            _save_cached_pages(cache_path, pages)
        except Exception:
            pass

    return all_scores


def _get_scoresaber_player_stats(scoresaber_id: str, session: requests.Session) -> dict:
    print("Entering _get_scoresaber_player_stats")
    url = SCORESABER_PLAYER_FULL_URL.format(player_id=scoresaber_id)
    try:
        resp = session.get(url, timeout=10)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
    except Exception:
        return {}

    try:
        data = resp.json()
    except Exception:
        return {}

    stats = data.get("playerStats") or data.get("scoreStats")
    if isinstance(stats, dict):
        return stats
    return {}


def _fetch_scoresaber_player_basic(steam_id: str, session: requests.Session) -> dict:
    """ScoreSaber のプレイヤー情報を /player/{id}/full から取得して ScoreSaberPlayer に詰める。

    players_index.json に存在しないプレイヤーのスナップショット作成時に利用する。
    失敗した場合は None を返す。
    """
    print("Entering _fetch_scoresaber_player_basic")
    url = SCORESABER_PLAYER_FULL_URL.format(player_id=steam_id)
    try:
        resp = session.get(url, timeout=10)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
    except Exception:
        return {}

    try:
        data = resp.json()
    except Exception:
        return {}

    return data


def _collect_star_stats_from_scoresaber(scoresaber_id: str, session: requests.Session) -> list[StarClearStat]:
    """ScoreSaber の Ranked マップとプレイヤースコアから★別統計を集計する。

    - /api/leaderboards?ranked=true で Ranked マップ一覧を取得し、★別の「全譜面(Map数)」を集計
    - /api/player/{id}/scores でプレイヤースコアを取得し、各 Ranked マップごとに
            * NF なしスコアが1つでもあれば → クリア
            * NF しか無ければ → NF
        と判定して★別にカウント
    - クリア率 = クリア数 / Map数

    API 仕様の変更やネットワークエラーなどで途中失敗した場合は、
    その時点までに集計できた結果だけを用いる（完全に失敗した場合は空リスト）。
    """
    
    print("Entering _collect_star_stats_from_scoresaber")
    if not scoresaber_id:
        return []

    star_map_count: dict[int, int] = defaultdict(int)
    leaderboard_star_bucket: dict[str, int] = {}

    leaderboards = _get_scoresaber_leaderboards_ranked(session)
    for lb in leaderboards:
        if lb.get("ranked") is False:
            continue
        if lb.get("deleted") is True:
            continue
        diff = lb.get("difficulty") or {}
        stars_value = lb.get("stars") or diff.get("stars")
        if stars_value is None:
            continue
        try:
            stars = float(stars_value)
        except (TypeError, ValueError):
            continue

        if not math.isfinite(stars) or stars < 0:
            continue

        star_bucket = int(stars)
        if star_bucket < 0:
            star_bucket = 0

        lb_id_raw = lb.get("id") or diff.get("leaderboardId")
        if lb_id_raw is None:
            continue
        lb_id = str(lb_id_raw)

        leaderboard_star_bucket[lb_id] = star_bucket
        star_map_count[star_bucket] += 1

    if not star_map_count or not leaderboard_star_bucket:
        return []

    star_clear_count: dict[int, int] = defaultdict(int)
    star_nf_count: dict[int, int] = defaultdict(int)
    star_ss_count: dict[int, int] = defaultdict(int)
    star_acc_sum: dict[int, float] = defaultdict(float)
    star_acc_count: dict[int, int] = defaultdict(int)

    per_leaderboard: dict[str, dict] = {}

    scores = _get_scoresaber_player_scores(scoresaber_id, session)
    for item in scores:
        score_info = item.get("score") if isinstance(item, dict) else None
        leaderboard = item.get("leaderboard") if isinstance(item, dict) else None

        if leaderboard is None and isinstance(item, dict):
            leaderboard = item

        if not isinstance(leaderboard, dict):
            continue

        lb_id_raw = leaderboard.get("id") or (leaderboard.get("difficulty") or {}).get("leaderboardId")
        if lb_id_raw is None:
            continue
        lb_id = str(lb_id_raw)

        if lb_id not in leaderboard_star_bucket:
            continue

        star_bucket = leaderboard_star_bucket[lb_id]

        state = per_leaderboard.get(lb_id)
        if state is None:
            state = {"star": star_bucket, "clear": False, "nf": False, "ss": False, "best_acc": None}
            per_leaderboard[lb_id] = state

        if isinstance(score_info, dict):
            modifier_flags = score_info.get("modifierFlags")
            is_nf = bool(modifier_flags & 0x10) if isinstance(modifier_flags, int) else False
            is_ss = bool(modifier_flags & 0x100) if isinstance(modifier_flags, int) else False
        else:
            is_nf = False
            is_ss = False

        if is_nf:
            state["nf"] = True
        elif is_ss:
            state["ss"] = True
        else:
            state["clear"] = True
            # try to extract accuracy
            try:
                acc = None
                if isinstance(score_info, dict):
                    if "accuracy" in score_info and score_info["accuracy"] is not None:
                        acc = float(score_info["accuracy"])
                    elif "acc" in score_info and score_info["acc"] is not None:
                        acc = float(score_info["acc"])
                if acc is not None:
                    state["best_acc"] = max(state.get("best_acc") or 0.0, acc)
            except Exception:
                pass

    for state in per_leaderboard.values():
        star_bucket = int(state["star"])
        has_clear = bool(state["clear"])
        has_nf = bool(state["nf"])
        has_ss = bool(state["ss"])

        if has_clear:
            star_clear_count[star_bucket] += 1
            best_acc = state.get("best_acc")
            if isinstance(best_acc, (int, float)) and math.isfinite(float(best_acc)):
                star_acc_sum[star_bucket] += float(best_acc)
                star_acc_count[star_bucket] += 1
        elif has_nf:
            star_nf_count[star_bucket] += 1
        elif has_ss:
            star_ss_count[star_bucket] += 1

    stats: list[StarClearStat] = []
    for star, map_count in sorted(star_map_count.items(), key=lambda x: x[0]):
        cleared = star_clear_count.get(star, 0)
        nf = star_nf_count.get(star, 0)
        ss = star_ss_count.get(star, 0)
        acc_sum = star_acc_sum.get(star, 0.0)
        acc_count = star_acc_count.get(star, 0)

        avg_acc = None
        if acc_count > 0 and math.isfinite(float(acc_sum)):
            avg_acc = acc_sum / acc_count

        stats.append(StarClearStat(
            star=star,
            map_count=map_count,
            clear_count=cleared,
            nf_count=nf,
            ss_count=ss,
            average_acc=avg_acc))

    return stats
