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

BEATLEADER_LEADERBOARDS_URL = "https://api.beatleader.xyz/leaderboards"
BL_BASE_URL = "https://api.beatleader.xyz"


def _load_cached_pages(path: Path) -> Optional[list[dict]]:
    """BeatLeader等のAPIレスポンスをキャッシュしたJSONファイルからページリストを読み込む。

    壊れている場合や形式違いはNoneを返す。
    leaderboards用に利用する。
    """
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
    """ページリストをキャッシュファイル(JSON)として保存する (leaderboards 用)。"""
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


def _load_cached_player_scores(path: Path) -> Optional[dict]:
    """BeatLeaderプレイヤースコア用のキャッシュローダー。

    フォーマット: {"fetched_at": str, "total_play_count": int, "scores": {leaderboard_id: score_obj, ...}}
    という形を想定し、scores dict を返す。壊れている場合は None。
    """

    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        scores = raw.get("scores")
        if isinstance(scores, dict):
            return scores
    except Exception:
        return None
    return None


def _save_cached_player_scores(path: Path, scores: dict) -> None:
    """BeatLeaderプレイヤースコアをマップ形式でキャッシュファイル(JSON)として保存する。

    scores は leaderboard の id をキーにした dict を想定する。
    """

    print("Entering _save_cached_player_scores")

    payload = {
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "total_play_count": len(scores),
        "scores": scores,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return


def _extract_leaderboard_timeset_from_score_item(item: dict) -> Optional[int]:
    """BeatLeaderのスコアオブジェクトから譜面更新時刻(timeset)を取り出して int で返す。

    leaderboard や difficulty 内に timeset があればそれを優先的に使う。
    壊れている/欠けている場合は None。
    """

    if not isinstance(item, dict):
        return None

    leaderboard = item.get("leaderboard") if isinstance(item, dict) else None
    if leaderboard is None:
        leaderboard = item

    if not isinstance(leaderboard, dict):
        return None

    diff = leaderboard.get("difficulty") or {}

    ts = leaderboard.get("timeset")
    if ts is None and isinstance(diff, dict):
        ts = diff.get("timeset")

    if ts is None:
        return None

    try:
        return int(ts)
    except (TypeError, ValueError):
        return None


def _get_beatleader_leaderboards_ranked(
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> list[dict]:
    """
    BeatLeaderのRanked譜面リストをAPIから全件取得し、キャッシュも利用する。
    進捗コールバック(progress)対応。
    """
    print("Entering _get_beatleader_leaderboards_ranked")
    cache_path = CACHE_DIR / "beatleader_ranked_maps.json"

    page = 1

    page_size = 100
    cached_pages = _load_cached_pages(cache_path)

    if cached_pages is not None:
        is_ranked_only = False
        for page in cached_pages:
            if not isinstance(page, dict):
                continue
            params = page.get("params") or {}
            if isinstance(params, dict) and params.get("type") == "Ranked":
                is_ranked_only = True
                break
        if not is_ranked_only:
            cached_pages = None

    if cached_pages is not None:
        pages: list[dict] = []
        leaderboards: list[dict] = []

        for page in cached_pages:
            if not isinstance(page, dict):
                continue
            pages.append(page)
            data = page.get("data") or {}
            items = data.get("data") or data.get("leaderboards") or []
            if isinstance(items, list):
                leaderboards.extend(lb for lb in items if isinstance(lb, dict))

        cached_total = len(leaderboards)
        if pages:
            first_meta = (pages[0].get("data") or {}).get("metadata") or {}
            try:
                cached_total = int(first_meta.get("total", cached_total))
            except (TypeError, ValueError):
                cached_total = len(leaderboards)

        try:
            params_first = {
                "page": "1",
                "count": str(page_size),
                "type": "Ranked",
                "sortBy": "stars",
                "order": "desc",
            }
            resp = session.get(BEATLEADER_LEADERBOARDS_URL, params=params_first, timeout=10)
            if resp.status_code != 404:
                resp.raise_for_status()
                data_first = resp.json()
                meta = data_first.get("metadata") or {}
                try:
                    new_total = int(meta.get("total", cached_total))
                except (TypeError, ValueError):
                    new_total = cached_total

                if new_total <= cached_total:
                    if progress is not None:
                        progress(1, 1)
                    return leaderboards

                pages = []
                leaderboards = []

                page = 1
                while True:
                    params = {
                        "page": str(page),
                        "count": str(page_size),
                        "type": "Ranked",
                        "sortBy": "stars",
                        "order": "desc",
                    }
                    if page == 1:
                        data = data_first
                    else:
                        resp_page = session.get(BEATLEADER_LEADERBOARDS_URL, params=params, timeout=10)
                        if resp_page.status_code == 404:
                            break
                        resp_page.raise_for_status()
                        data = resp_page.json()

                    pages.append({"page": page, "params": params, "data": data})

                    items = data.get("data") if isinstance(data, dict) else None
                    if items is None and isinstance(data, dict):
                        items = data.get("leaderboards")
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
        except Exception:  # noqa: BLE001
            # メタデータ確認に失敗した場合は、既存キャッシュをそのまま返す
            if progress is not None:
                progress(1, 1)
            return leaderboards

    else:
        pages = []
        leaderboards = []
        page = 1
        while True:
            params = {
                "page": str(page),
                "count": str(page_size),
                "type": "Ranked",
                "sortBy": "stars",
                "order": "desc",
            }
            resp = session.get(BEATLEADER_LEADERBOARDS_URL, params=params, timeout=10)
            if resp.status_code == 404:
                break
            resp.raise_for_status()
            data = resp.json()

            pages.append({"page": page, "params": params, "data": data})

            items = data.get("data") if isinstance(data, dict) else None
            if items is None and isinstance(data, dict):
                items = data.get("leaderboards")
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


def _get_beatleader_player_scores(
    player_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> list[dict]:
    """BeatLeader のプレイヤースコア一覧を API から全件取得し、キャッシュも利用する。

    以前 collector.py 側で使用していた動作確認済みのエンドポイント
    `/player/{id}/scores` + `page/count/sortBy/order` を利用しつつ、
    ページごとの進捗を progress(page, max_pages) で通知する。
    """

    print("Entering _get_beatleader_player_scores")
    cache_path = CACHE_DIR / f"beatleader_player_scores_{player_id}.json"

    # leaderboard の id をキーにしたマップ形式でスコアを保持する
    scores_by_lb_id: dict[str, dict] = {}

    # 既存キャッシュの読み込み
    cached_scores = _load_cached_player_scores(cache_path)
    if cached_scores is not None:
        print(f"BeatLeaderのキャッシュ読み込み成功: {player_id} スコア件数: {len(cached_scores)}")
        scores_by_lb_id.update(cached_scores)
    else:
        cached_scores = {}

    # プレイカウント(特に rankedPlayCount)を scoreStats から取得して、
    # 差分更新後の目標件数として利用する。
    # ここでは「キャッシュ件数が十分だからネットワークを完全にスキップする」ことはせず、
    # 必ず少なくとも先頭ページは取得して timeset の更新有無を確認する。
    total_play_count_target: Optional[int] = None
    try:
        stats = _get_beatleader_player_stats(player_id, session)
        if isinstance(stats, dict):
            rpc = stats.get("totalPlayCount")
            if rpc is not None:
                total_play_count_target = int(rpc)
    except Exception:
        total_play_count_target = None

    page = 1
    page_size = 100
    max_pages_bl: Optional[int] = None

    while True:
        url = f"{BL_BASE_URL}/player/{player_id}/scores"
        params = {
            "page": str(page),
            "count": str(page_size),
            "sortBy": "date",
            "order": "desc",
            "type" : "best",
        }
        resp = session.get(url, params=params, timeout=10)
        print(f"Fetching BeatLeader player scores page {page}... URL: {resp.url} params: {params}")
        if resp.status_code == 404:
            print("BeatLeaderスコア取得: 404 Not Found")
            break
        resp.raise_for_status()

        data = resp.json()

        # 1ページ目でメタデータから推定最大ページ数を計算
        if max_pages_bl is None:
            meta = data.get("metadata") or {}
            try:
                total = int(meta.get("total", 0))
                per_page = int(meta.get("itemsPerPage", page_size)) or page_size
            except (TypeError, ValueError):
                total = 0
                per_page = page_size

            if total > 0 and per_page > 0:
                computed_pages = math.ceil(total / per_page)
                max_pages_bl = min(computed_pages, 300)
            else:
                max_pages_bl = 100

        items = data.get("data") or data.get("scores") or []
        if not isinstance(items, list) or not items:
            print("BeatLeaderスコア取得: スコアデータ無し")
            break

        # このページで新規/更新スコアがあったかどうか
        page_has_diff = False

        for item in items:
            if not isinstance(item, dict):
                print("Skipping invalid score item (not a dict)")
                continue

            leaderboard = item.get("leaderboard") if isinstance(item, dict) else None
            if leaderboard is None:
                leaderboard = item

            if not isinstance(leaderboard, dict):
                print("Skipping invalid leaderboard item (not a dict)")
                continue

            diff = leaderboard.get("difficulty") or {}
            lb_id_raw = leaderboard.get("id") or diff.get("leaderboardId") or diff.get("id")
            if lb_id_raw is None:
                print("Skipping score item with missing leaderboard id")
                continue

            lb_id = str(lb_id_raw)

            # 事前キャッシュに存在するか、timeset が更新されているかを判定
            old_item = cached_scores.get(lb_id)
            if old_item is None:
                # 新規にプレイされた譜面
                page_has_diff = True
                scores_by_lb_id[lb_id] = item
            else:
                old_ts = _extract_leaderboard_timeset_from_score_item(old_item)
                new_ts = _extract_leaderboard_timeset_from_score_item(item)

                if new_ts is not None and (old_ts is None or new_ts > old_ts):
                    # 譜面側の timeset が更新されている場合は差し替える
                    page_has_diff = True
                    scores_by_lb_id[lb_id] = item
                else:
                    # 変更なしの場合は既存キャッシュをそのまま利用
                    if lb_id not in scores_by_lb_id:
                        scores_by_lb_id[lb_id] = old_item

        # ページごとの進捗をコールバックで通知
        if progress is not None:
            progress(page, max_pages_bl)
        
        print(f"Completed fetching page {page} of BeatLeader scores. ranked_play_count_target: {total_play_count_target}, page_has_diff: {page_has_diff}")
        # 差分更新の結果、プレイカウントと件数が一致し、かつこのページで変更がなければ終了
        if total_play_count_target is not None:
            current_count = len(scores_by_lb_id)
            print(f"Current cached score count: {current_count}, Target ranked play count: {total_play_count_target} page_has_diff: {page_has_diff}")
            if current_count >= total_play_count_target and not page_has_diff:
                break

        # 1ページあたりの件数が page_size 未満、または推定最大ページ数に達したら終了
        if len(items) < page_size or (max_pages_bl is not None and page >= max_pages_bl):
            break

        page += 1

    # timeset の新しい順に並べ替えてからキャッシュを更新する
    if scores_by_lb_id:
        sorted_items = sorted(
            scores_by_lb_id.items(),
            key=lambda kv: _extract_leaderboard_timeset_from_score_item(kv[1]) or 0,
            reverse=True,
        )
        ordered_scores: dict[str, dict] = {lb_id: item for lb_id, item in sorted_items}

        try:
            _save_cached_player_scores(cache_path, ordered_scores)
        except Exception:
            pass

        # 呼び出し元には並べ替え後のリストを返す
        return [item for _, item in sorted_items]

    # 既存キャッシュも含めて何も無ければ空リスト
    return []


def _get_beatleader_player_stats(player_id: str, session: requests.Session) -> dict:
    """
    BeatLeaderのプレイヤー統計情報(scoreStats)を取得。
    失敗時は空dict。
    """
    print("Entering _get_beatleader_player_stats")
    url = BL_BASE_URL + f"/player/{player_id}"
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

    stats = data.get("scoreStats")
    if isinstance(stats, dict):
        return stats
    return {}


def _extract_beatleader_accuracy(score_info: dict) -> Optional[float]:
    """
    BeatLeaderスコアオブジェクトから精度(%)を推定して返す。
    形式の違いも吸収。
    """
    print("Entering _extract_beatleader_accuracy")
    if not isinstance(score_info, dict):
        return None

    try:
        acc = score_info.get("accuracy")
        if acc is None:
            acc = score_info.get("acc")

        if acc is not None:
            acc_f = float(acc)
            if not math.isfinite(acc_f) or acc_f <= 0:
                acc_f = 0.0
            if acc_f > 0.0:
                if acc_f <= 1.0:
                    return acc_f * 100.0
                if acc_f <= 100.0:
                    return acc_f
                if acc_f <= 10000.0:
                    return acc_f / 100.0

        base = score_info.get("baseScore")
        if base is None:
            base = score_info.get("modifiedScore")
        max_score = score_info.get("maxScore")
        if base is None or max_score is None:
            return None

        base_f = float(base)
        max_f = float(max_score)
        if not math.isfinite(base_f) or not math.isfinite(max_f) or max_f <= 0:
            return None

        return max(0.0, min(100.0, base_f / max_f * 100.0))
    except (TypeError, ValueError):
        return None


def collect_beatleader_star_stats(beatleader_id: str, session: Optional[requests.Session] = None) -> list[StarClearStat]:
    """
    BeatLeaderのRanked譜面・プレイヤースコアから星別クリア数・NF数・平均精度を集計。
    """
    print("Entering collect_beatleader_star_stats")
    if not beatleader_id:
        return []

    if session is None:
        session = requests.Session()

    leaderboards = _get_beatleader_leaderboards_ranked(session)
    if not leaderboards:
        return []

    star_map_count: dict[int, int] = defaultdict(int)
    leaderboard_star_bucket: dict[str, int] = {}

    for lb in leaderboards:
        if not isinstance(lb, dict):
            continue

        diff = lb.get("difficulty") or {}

        try:
            status_val = int(diff.get("status", 0))
        except (TypeError, ValueError):
            status_val = 0
        if status_val != 3:
            continue

        stars_value = diff.get("stars") or diff.get("difficultyRating")
        if stars_value is None:
            continue
        try:
            stars = float(stars_value)
        except (TypeError, ValueError):
            continue

        if not (stars >= 0):
            continue

        star_bucket = int(stars)
        if star_bucket < 0:
            star_bucket = 0

        lb_id_raw = lb.get("id") or diff.get("leaderboardId") or diff.get("id")
        if lb_id_raw is None:
            continue
        lb_id = str(lb_id_raw)

        leaderboard_star_bucket[lb_id] = star_bucket
        star_map_count[star_bucket] += 1

    if not star_map_count or not leaderboard_star_bucket:
        return []

    scores = _get_beatleader_player_scores(beatleader_id, session)

    star_clear_count: dict[int, int] = defaultdict(int)
    star_nf_count: dict[int, int] = defaultdict(int)
    star_ss_count: dict[int, int] = defaultdict(int)
    star_acc_sum: dict[int, float] = defaultdict(float)
    star_acc_count: dict[int, int] = defaultdict(int)

    per_leaderboard: dict[str, dict] = {}

    for item in scores:
        leaderboard = item.get("leaderboard") if isinstance(item, dict) else None
        if leaderboard is None and isinstance(item, dict):
            leaderboard = item

        if not isinstance(leaderboard, dict):
            continue

        diff = leaderboard.get("difficulty") or {}

        lb_id_raw = leaderboard.get("id") or diff.get("leaderboardId") or diff.get("id")
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

        score_info = item.get("score") if isinstance(item, dict) else None
        if not isinstance(score_info, dict):
            score_info = item if isinstance(item, dict) else None

        modifiers = ""
        if isinstance(score_info, dict):
            modifiers = str(score_info.get("modifiers") or "")

        mods_upper = modifiers.upper()
        is_nf = "NF" in mods_upper
        is_ss = "SS" in mods_upper

        if is_nf:
            state["nf"] = True
        elif is_ss:
            state["ss"] = True
        else:
            state["clear"] = True

            acc = _extract_beatleader_accuracy(score_info) if isinstance(score_info, dict) else None
            if acc is not None:
                best = state.get("best_acc")
                if best is None or acc > best:
                    state["best_acc"] = acc

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

        # ScoreSaber 側と同様にクリア率 (0.0-1.0) を計算
        clear_rate = (cleared / map_count) if map_count > 0 else 0.0

        stats.append(StarClearStat(
            star=star,
            map_count=map_count,
            clear_count=cleared,
            nf_count=nf,
            ss_count=ss,
            clear_rate=clear_rate,
            average_acc=avg_acc))

    return stats
