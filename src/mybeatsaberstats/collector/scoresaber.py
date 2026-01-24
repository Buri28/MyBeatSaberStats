from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
from typing import Optional, Callable

import math
import requests
from ..scoresaber import ScoreSaberPlayer
from ..snapshot import BASE_DIR, StarClearStat
from typing import Optional, Dict, TypedDict, Callable

CACHE_DIR = BASE_DIR / "cache"

SCORESABER_LEADERBOARDS_URL = "https://scoresaber.com/api/leaderboards"
SCORESABER_PLAYER_SCORES_URL = "https://scoresaber.com/api/player/{player_id}/scores"
SCORESABER_PLAYER_FULL_URL = "https://scoresaber.com/api/player/{player_id}/full"


def _load_cached_rank_maps(path: Path) -> Optional[list[dict]]:
    """
    ScoreSaber等のAPIレスポンスをキャッシュしたJSONファイルからページリストを読み込む。
    壊れている場合や形式違いはNoneを返す。
    """
    print("Entering _load_cached_rank_maps")
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        leaderboards = raw.get("leaderboards")
        if isinstance(leaderboards, list):
            return leaderboards
    except Exception:
        return None
    return None

def _save_cached_ranked_maps(path: Path, ranked_maps: list[dict], max_pages: int, total_maps: int) -> None:
    """
    ページリストをキャッシュファイル(JSON)として保存する。
    """
    print("Entering _save_cached_ranked_maps")
    payload = {
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "leaderboards": ranked_maps,
        "max_pages": max_pages,
        "total_maps": total_maps,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return

def _save_cached_pages(path: Path, pages: list[dict]) -> None:
    """
    ページリストをキャッシュファイル(JSON)として保存する。
    """
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
    """ScoreSaber の Ranked leaderboards をキャッシュ付きで全件取得する。

    progress が指定されている場合は、0.0〜1.0 の範囲で簡易的に進捗をコールバックする。
    GUI 側のプログレスバー更新用（初回の全件取得が長時間になる対策）。
    """

    cache_path = CACHE_DIR / "scoresaber_ranked_maps.json"

    # コンソール出力は日本語、画面表示は英語
    print("ScoreSaberのRanked譜面をキャッシュ付きで取得中...")
    leaderboards: list[dict] = []
    cached_leaderboards = _load_cached_rank_maps(cache_path)
    print(f"既存のScoreSaberリーダーボードキャッシュを読み込み完了。{len(cached_leaderboards) if cached_leaderboards is not None else 0}件")

    #すべての譜面データのidを格納するdicationaryを作成
    existing_lb_ids = {}
    if cached_leaderboards is not None:
        # 重複を省くため、IDをキーにした辞書に格納
        for lb in cached_leaderboards:
            lb_id = lb.get("id")
            if existing_lb_ids.get(lb_id) is None:
                existing_lb_ids[lb_id] = lb
                leaderboards.append(lb)
    
    print("既存のScoreSaberリーダーボードキャッシュを読み込み中...")

    # キャッシュから既存譜面データを読み込み
    cached_total = len(leaderboards) #load_cached_ranked_maps(cached_leaderboards, leaderboards, existing_lb_ids)
    latest_total = 0
    new_total = cached_total

    # 1ページだけ最新のメタデータを取りに行き、total が増えていなければキャッシュをそのまま返す
    try:
        page_no = 1
        params = {
            "ranked": "true",
            "page": str(page_no),
        }

        resp = session.get(SCORESABER_LEADERBOARDS_URL, params=params, timeout=10)
        print(f"最新のScoreSaberリーダーボードのメタデータを確認中... URL: {resp.url} params: {params}")
        if resp.status_code != 404:
            # メタデータ取得成功
            resp.raise_for_status()
            data = resp.json()
            meta = data.get("metadata") or {}
            try:
                latest_total = int(meta.get("total", 0))
                per_page = int(meta.get("itemsPerPage", 0)) or 1
            except (TypeError, ValueError):
                latest_total = cached_total
                per_page = 100

            print(f"既存キャッシュの総譜面数: {cached_total}, 最新総譜面数: {latest_total} ページあたり: {per_page}")
            # total が増えていなければキャッシュをそのまま利用
            if latest_total <= cached_total:
                if progress is not None:
                    # ページ数情報が無いので 1/1 として通知
                    progress(1, 1)
                print("ScoreSaberリーダーボードのキャッシュは最新です。①")
                return leaderboards
            
            # total が増えている場合は、キャッシュ済みの最後のページ以降だけを追加取得する
            print(f"ScoreSaberリーダーボードに新しい譜面が追加されています。{latest_total - cached_total}件の差分を取得し、キャッシュを更新します...")                
           
            append_leaderboards = []
            data_lbs = data.get("leaderboards") or []
            add_leaderboards(append_leaderboards, existing_lb_ids, data_lbs)
            new_total = cached_total + len(append_leaderboards)
            
            max_cached_page = math.ceil(cached_total / per_page)
            total_pages_new = math.ceil(new_total / per_page)
            print(f"既存キャッシュの最終ページ: {max_cached_page}, 新しい総ページ数: {total_pages_new}")    
            print(f"取得済み譜面数: {new_total}, 最新総譜面数: {latest_total} ")
            if new_total < latest_total:
                total_maps_new = cached_total
                page_no += 1
                
                for i in range(page_no, total_pages_new + 1):
                    if progress is not None:
                        progress(i, total_pages_new)
                    params_page = {
                        "ranked": "true",
                        "page": str(i),
                    }
                    try:
                        # 追加のページを取得
                        resp_page = session.get(SCORESABER_LEADERBOARDS_URL, params=params_page, timeout=10)
                        print(
                            f"ScoreSaberリーダーボードの追加ページ {i} を取得中... URL: {resp_page.url} params: {params_page}"
                        )
                        if resp_page.status_code == 404:
                            break
                        resp_page.raise_for_status()
                    except Exception:  # noqa: BLE001
                        break

                    try:
                        # レスポンス JSON をパース
                        data_page = resp_page.json()
                        data_lbs = data_page.get("leaderboards") or []
                        
                        add_leaderboards(append_leaderboards, existing_lb_ids, data_lbs)
                        new_total = cached_total + len(append_leaderboards)
                            # マップ数が一致した場合は終了
                        if new_total >= latest_total:
                            print("総譜面数が一致したため取得を終了します。：new_total:", new_total, " latest_total:", latest_total)
                            break
                        else:
                            print("総譜面数不一致：new_total:", new_total, " latest_total:", latest_total)
                    except Exception:  # noqa: BLE001
                        break
                    # ページ情報をキャッシュ用に保存
                    #pages[:0] = [{"page": page_no, "params": params_page, "data": data_page}]
                    # リーダーボード情報を追加取得
                    lbs = data_page.get("leaderboards") or []
                    if not lbs:
                        break
                    
                    print(f"追加取得した譜面数: {len(lbs)} 総追加譜面数: {len(append_leaderboards)}")
                    # if isinstance(lbs, list):
                    #     # 既存のリーダーボードリストの先頭に追加する形でマージ
                    #     leaderboards[:0] = [lb for lb in lbs if isinstance(lb, dict)]
            if append_leaderboards:
                print("ScoreSaberリーダーボードの保存中...")
                # キャッシュを保存
                try:
                    leaderboards[:0] = append_leaderboards
                    # leaderboardsをidの降順でソートする
                    leaderboards.sort(key=lambda x: x.get("id", 0), reverse=True)

                    _save_cached_ranked_maps(cache_path, leaderboards, \
                                                max_pages=total_pages_new, \
                                                total_maps=len(leaderboards))
                except Exception:  # noqa: BLE001
                    pass

            if progress is not None:
                progress(total_pages_new, total_pages_new)
            print("ScoreSaberリーダーボードのキャッシュ更新が完了しました。")
            return leaderboards
    except Exception:  # noqa: BLE001
        print("ScoreSaberリーダーボードのメタデータ確認に失敗しました。キャッシュを利用します。")
        # メタデータ確認に失敗した場合は、既存キャッシュをそのまま返す
        if progress is not None:
            progress(1, 1)
        return leaderboards

    # ここには基本的に来ない想定だが、安全のためキャッシュを返す
    if progress is not None:
        progress(1, 1)
    print("ScoreSaberリーダーボードのキャッシュは最新です。②")
    return leaderboards

def add_leaderboards(leaderboards, existing_lb_ids, data_lbs):    
    for lb in data_lbs:
        lb_id = lb.get("id")
        if lb_id not in existing_lb_ids:
            leaderboards.append(lb)
            existing_lb_ids[lb_id] = lb
        
def _get_scoresaber_player_scores(
    scoresaber_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> list[dict]:
    """ScoreSaber のプレイヤースコアをキャッシュ付きで全件取得する。

    progress が与えられた場合、現在のページ番号と推定最大ページ数を通知する。
    """

    cache_path = CACHE_DIR / f"scoresaber_player_scores_{scoresaber_id}.json"

    print(f"ScoreSaberのキャッシュ読み込み: {scoresaber_id}")
    cached_pages = _load_cached_pages(cache_path)
    if cached_pages is not None:
        print(f"ScoreSaberのキャッシュ読み込み成功: {scoresaber_id}")

        # Try to read cached fetched_at timestamp (if available) so we can check
        # whether there are new "recent" scores to fetch.
        fetched_at: Optional[datetime] = None
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            fa = raw.get("fetched_at")
            if isinstance(fa, str) and fa:
                if fa.endswith("Z"):
                    fa = fa[:-1]
                fetched_at = datetime.fromisoformat(fa)
        except Exception:  # noqa: BLE001
            fetched_at = None

        # Aggregate existing cached scores
        scores: list[dict] = []
        for page_obj in cached_pages:
            if not isinstance(page_obj, dict):
                continue
            data = page_obj.get("data") or {}
            items = data.get("playerScores") or data.get("scores") or []
            if isinstance(items, list):
                scores.extend(it for it in items if isinstance(it, dict))

        # If this cache was produced with sort=recent and we have a fetched_at,
        # check page 1 of the API for newer items and fetch new pages if necessary.
        try:
            page1 = None
            for p in cached_pages:
                if isinstance(p, dict) and p.get("page") == 1:
                    page1 = p
                    break
            sort_is_recent = False
            limit_param = 100
            if page1:
                params_page1 = page1.get("params") or {}
                if isinstance(params_page1, dict):
                    if params_page1.get("sort") == "recent":
                        sort_is_recent = True
                    if params_page1.get("limit"):
                        try:
                            # Ensure we convert to string first to handle non-str types
                            limit_param = int(str(params_page1.get("limit")))
                        except Exception:  # noqa: BLE001
                            limit_param = 100

            if fetched_at is not None and sort_is_recent:
                url = SCORESABER_PLAYER_SCORES_URL.format(player_id=scoresaber_id)
                params_check = {"limit": str(limit_param), "sort": "recent", "page": "1"}
                resp = session.get(url, params=params_check, timeout=10)
                print(f"最新スコアチェック... URL: {resp.url} params: {params_check}")
                if resp.status_code != 404:
                    resp.raise_for_status()
                    data = resp.json()
                    items = data.get("playerScores") or data.get("scores") or []
                    if isinstance(items, list) and items:
                        first = items[0]
                        score_obj = first.get("score") if isinstance(first, dict) else first
                        tstr = None
                        if isinstance(score_obj, dict):
                            tstr = score_obj.get("timeSet")

                        if isinstance(tstr, str):
                            try:
                                tcmp = datetime.fromisoformat(tstr[:-1]) if tstr.endswith("Z") else datetime.fromisoformat(tstr)
                            except Exception:
                                tcmp = None

                            if tcmp is not None and tcmp > fetched_at:
                                new_pages: list[dict] = []
                                max_new_pages = 50
                                for page_num in range(1, max_new_pages + 1):
                                    params_page = {"limit": str(limit_param), "sort": "recent", "page": str(page_num)}
                                    try:
                                        resp_page = session.get(url, params=params_page, timeout=10)
                                        if resp_page.status_code == 404:
                                            break
                                        resp_page.raise_for_status()
                                    except Exception:  # noqa: BLE001
                                        break

                                    try:
                                        data_page = resp_page.json()
                                    except Exception:  # noqa: BLE001
                                        break

                                    items_page = data_page.get("playerScores") or data_page.get("scores") or []
                                    if not items_page:
                                        break

                                    first_page = items_page[0]
                                    score_first_page = first_page.get("score") if isinstance(first_page, dict) else first_page
                                    tfirst = None
                                    if isinstance(score_first_page, dict):
                                        tfirst = score_first_page.get("timeSet")
                                    if isinstance(tfirst, str):
                                        try:
                                            tfirst_dt = datetime.fromisoformat(tfirst[:-1]) if tfirst.endswith("Z") else datetime.fromisoformat(tfirst)
                                        except Exception:
                                            tfirst_dt = None
                                    else:
                                        tfirst_dt = None

                                    if tfirst_dt is None or not (tfirst_dt > fetched_at):
                                        break

                                    new_pages.append({"page": page_num, "params": params_page, "data": data_page})

                                if new_pages:
                                    last_new_page = new_pages[-1].get("page") or 0
                                    remaining_cached = [p for p in cached_pages if (p.get("page") or 0) > last_new_page]
                                    pages_merged = new_pages + remaining_cached
                                    try:
                                        _save_cached_pages(cache_path, pages_merged)
                                    except Exception:  # noqa: BLE001
                                        pass

                                    scores = []
                                    for page_obj in pages_merged:
                                        if not isinstance(page_obj, dict):
                                            continue
                                        data = page_obj.get("data") or {}
                                        items = data.get("playerScores") or data.get("scores") or []
                                        if isinstance(items, list):
                                            scores.extend(it for it in items if isinstance(it, dict))

        except Exception:  # noqa: BLE001
            # If anything goes wrong during the freshness check, fall back to cached data
            pass

        if progress is not None:
            # キャッシュ読み込み時は 1 ページのみとして通知
            progress(1, 1)
        print(f"ScoreSaberのキャッシュ返却: {scoresaber_id} 件数: {len(scores)}")
        return scores

    pages: list[dict] = []
    scores: list[dict] = []

    page = 1
    max_pages_sc: int | None = None
    page_size = 100

    while True:
        url = SCORESABER_PLAYER_SCORES_URL.format(player_id=scoresaber_id)
        params = {
            "limit": str(page_size),
            "sort": "recent",
            "page": str(page),
        }
        print(f"ScoreSaberのキャッシュ取得API呼び出し: {scoresaber_id} ページ: {page}")
        try:
            resp = session.get(url, params=params, timeout=10)
            print(f"Fetching ScoreSaber player scores page {page} for star stats... URL: {resp.url} params: {params}")
            if resp.status_code == 404:
                break
            resp.raise_for_status()
        except Exception:  # noqa: BLE001
            break

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            break

        pages.append({"page": page, "params": params, "data": data})

        if max_pages_sc is None:
            meta = data.get("metadata") or {}
            try:
                total = int(meta.get("total", 0))
                per_page = int(meta.get("itemsPerPage", page_size)) or page_size
            except (TypeError, ValueError):
                total = 0
                per_page = page_size

            if total > 0 and per_page > 0:
                computed_pages = math.ceil(total / per_page)
                max_pages_sc = min(computed_pages, 300)
            else:
                max_pages_sc = 100

        items = data.get("playerScores") or data.get("scores") or []
        if not items:
            break
        if isinstance(items, list):
            scores.extend(it for it in items if isinstance(it, dict))

        # ページ進捗をコールバックで通知
        if progress is not None:
            progress(page, max_pages_sc)

        if len(items) < page_size or (max_pages_sc is not None and page >= max_pages_sc):
            break

        page += 1

    if pages:
        try:
            _save_cached_pages(cache_path, pages)
        except Exception:  # noqa: BLE001
            pass

    return scores


# def _get_scoresaber_player_scores(scoresaber_id: str, session: requests.Session, progress: Optional[Callable[[int, Optional[int]], None]] = None) -> list[dict]:
#     """ScoreSaber のプレイヤースコアをキャッシュ付きで全件取得する。

#     progress が与えられた場合、現在のページ番号と推定最大ページ数を通知する。
#     """
#     print(f"ScoreSaberのプレイヤースコア取得開始: {scoresaber_id}")
#     cache_path = CACHE_DIR / f"scoresaber_player_scores_{scoresaber_id}.json"

#     # キャッシュから読み込み
#     pages = _load_cached_pages(cache_path) or []

#     if pages:
#         data: dict = pages[0].get("data") or {}
#         items = data.get("scores") or data.get("data") or []
#         if isinstance(items, list):
#             return items

#     page = 1
#     all_scores: list[dict] = []

#     # コメントは日本語
#     print ("ScoreSaberのプレイヤースコア取得開始")
#     while True:
#         url = SCORESABER_PLAYER_SCORES_URL.format(player_id=scoresaber_id)
#         params = {"page": str(page), "count": "100"}
#         resp = session.get(url, params=params, timeout=10)
#         if resp.status_code == 404:
#             break
#         resp.raise_for_status()
#         data = resp.json()

#         items = data.get("scores") or data.get("data") or []
#         if not isinstance(items, list) or not items:
#             break

#         all_scores.extend(item for item in items if isinstance(item, dict))

#         pages.append({"page": page, "params": params, "data": data})

#         if len(items) < 100:
#             break

#         page += 1

#     if pages:
#         try:
#             _save_cached_pages(cache_path, pages)
#         except Exception:
#             pass

#     return all_scores


def _get_scoresaber_player_stats(scoresaber_id: str, session: requests.Session) -> dict:
    """
    ScoreSaberのプレイヤー統計情報(playerStats/scoreStats)を取得。
    失敗時は空dict。
    """
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

def _load_cached_pages(path: Path) -> Optional[list[dict]]:
    """共通のページキャッシュローダー。

    フォーマット: {"fetched_at": str, "pages": [{"page": int, "params": dict, "data": object}, ...]}
    という形を想定し、pages 配列だけを返す。
    壊れたファイルや形式違いの場合は None を返して再取得させる。
    """

    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        pages = raw.get("pages")
        if isinstance(pages, list):
            # 各要素は dict 想定だが、型が怪しいものは後段で弾く
            return pages
    except Exception:  # noqa: BLE001
        return None
    return None

def _fetch_scoresaber_player_basic(
    scoresaber_id: str,
    session: requests.Session,
) -> Optional[ScoreSaberPlayer]:
    """ScoreSaber のプレイヤー情報を /player/{id}/full から取得して ScoreSaberPlayer に詰める。

    players_index.json に存在しないプレイヤーのスナップショット作成時に利用する。
    失敗した場合は None を返す。
    """

    if not scoresaber_id:
        return None

    url = SCORESABER_PLAYER_FULL_URL.format(player_id=scoresaber_id)
    try:
        resp = session.get(url, timeout=10)
        print(f"Fetching ScoreSaber player info... URL: {resp.url}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        return None

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None

    info = data.get("playerInfo") or data.get("player") or data
    if not isinstance(info, dict):
        return None

    try:
        pid = str(info.get("id") or scoresaber_id)
        name = str(info.get("name") or "")
        country = str(info.get("country") or "")
        pp_val = info.get("pp") or info.get("ppAcc") or 0.0
        pp = float(pp_val)
        global_rank_val = info.get("rank") or info.get("globalRank") or 0
        global_rank = int(global_rank_val)
        country_rank_val = info.get("countryRank") or 0
        country_rank = int(country_rank_val)
    except (TypeError, ValueError):  # noqa: BLE001
        return None

    return ScoreSaberPlayer(
        id=pid,
        name=name,
        country=country,
        pp=pp,
        global_rank=global_rank,
        country_rank=country_rank,
    )


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

    if not scoresaber_id:
        return []

    # 1) Ranked マップ一覧から "全譜面" を集計（キャッシュ利用）
    star_map_count: dict[int, int] = defaultdict(int)
    leaderboard_star_bucket: dict[str, int] = {}

    leaderboards = _get_scoresaber_leaderboards_ranked(session)
    for lb in leaderboards:
        # 念のため ranked フラグを確認
        if lb.get("ranked") is False:
            continue

        # Deleted フラグが true の譜面は対象外
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

        # 四捨五入はしない
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
        # Ranked マップ情報が取れない場合は何も返せない
        return []

    # 2) プレイヤースコアから各 Ranked マップのクリア / NF 状態を集計（キャッシュ利用）
    star_clear_count: dict[int, int] = defaultdict(int)
    star_nf_count: dict[int, int] = defaultdict(int)
    star_ss_count: dict[int, int] = defaultdict(int)
    # ★別の平均精度算出用
    star_acc_sum: dict[int, float] = defaultdict(float)
    star_acc_count: dict[int, int] = defaultdict(int)

    # leaderboardId ごとに「クリア有り / NF有り / SS有り」とベスト精度を記録する
    class _PerLeaderboardState(TypedDict):
        star: int
        clear: bool
        nf: bool
        ss: bool
        best_acc: Optional[float]

    per_leaderboard: dict[str, _PerLeaderboardState] = {}

    scores = _get_scoresaber_player_scores(scoresaber_id, session)
    print(f"取得したスコア件数: {len(scores)}")
    for item in scores:
        score_info = item.get("score") if isinstance(item, dict) else None
        leaderboard = item.get("leaderboard") if isinstance(item, dict) else None

        if leaderboard is None and isinstance(item, dict):
            print(f"leaderboard 情報が score オブジェクトに無いケース発生。item={item}")
            leaderboard = item
        # print(f"処理中 score item: {leaderboard.get('id') if isinstance(leaderboard, dict) else 'N/A'}")

        if not isinstance(leaderboard, dict):
            print("leaderboard 情報が辞書型でないケース発生。スキップ")
            continue

        diff = leaderboard.get("difficulty") or {}

        lb_id_raw = leaderboard.get("id") or diff.get("leaderboardId")
        if lb_id_raw is None:
            continue
        lb_id = str(lb_id_raw)
        tmp_stars = leaderboard.get("stars")  # or diff.get("stars")
        ranked_flag = leaderboard.get("ranked")
        if ranked_flag is False:
            continue

        # if lb_id == "685895" or lb_id == "682135":
        #     print(f"●処理中 leaderboard ID: {lb_id}")

        # Ranked マップ一覧に存在しない ID は無視（非 Ranked など）
        # if lb_id not in leaderboard_star_bucket:
        #     # print(f"スキップ non-ranked leaderboard ID: {lb_id}")
        #     continue

        # if lb_id == "685895" or lb_id == "682135":
        #     print(f"●2 処理中 leaderboard ID: {lb_id}")
        star_bucket = -1
        if tmp_stars is not None:
            star_bucket: int = int(tmp_stars)

        if star_bucket < 0:
            continue
        # star_bucket = leaderboard_star_bucket[lb_id]
        # if star_bucket != 11:
        #     # TODO
        #     continue
        # if lb_id == "685895" or lb_id == "682135":
            # print(f"●3 処理中 leaderboard ID: {lb_id}")
        # print(f"処理中 leaderboard ID: {lb_id} 星: {star_bucket}")
        # if lb_id == "685895" or lb_id == "685896" or lb_id == "682135":
        #     print(f"★処理中 leaderboard ID: {lb_id} 星: {star_bucket}")
        state = per_leaderboard.get(lb_id)
        if state is None:
            state = _PerLeaderboardState(star=star_bucket, clear=False, nf=False, ss=False, best_acc=None)
            per_leaderboard[lb_id] = state

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
            # print(f"★クリア済み leaderboard ID: {lb_id} 星: {star_bucket}")

            # NF/SS なしスコアの精度(%)を best_acc として保持
            acc: Optional[float] = None
            if isinstance(score_info, dict):
                # まずスコアオブジェクト単体から推定
                acc = _extract_scoresaber_accuracy(score_info)

                # ScoreSaber の playerScores では maxScore が leaderboard 側にあるので、
                # そちらからも再計算を試みる
                if acc is None and isinstance(leaderboard, dict):
                    try:
                        base = score_info.get("baseScore") or score_info.get("score") or score_info.get("modifiedScore")
                        max_score_lb = leaderboard.get("maxScore")
                        if base is not None and max_score_lb is not None:
                            base_f = float(base)
                            max_f = float(max_score_lb)
                            if math.isfinite(base_f) and math.isfinite(max_f) and max_f > 0:
                                acc = max(0.0, min(100.0, base_f / max_f * 100.0))
                    except (TypeError, ValueError):  # noqa: BLE001
                        acc = None

            if acc is not None:
                best = state.get("best_acc")
                if best is None or acc > best:
                    state["best_acc"] = acc

    # leaderboard ごとの状態から★別のクリア数 / NF数を算出
    print(f"集計対象 leaderboard 数: {len(per_leaderboard)}")
    for state in per_leaderboard.values():
        star_bucket = int(state["star"])
        has_clear = bool(state["clear"])
        has_nf = bool(state["nf"])
        has_ss = bool(state["ss"])

        if has_clear:
            # print(f"クリア済み leaderboard (星 {star_bucket}){state.get("best_acc")=}")
            star_clear_count[star_bucket] += 1
            # クリア済み譜面については best_acc を★別に集計
            best_acc = state.get("best_acc")
            if isinstance(best_acc, (int, float)) and math.isfinite(float(best_acc)):
                star_acc_sum[star_bucket] += float(best_acc)
                star_acc_count[star_bucket] += 1
        elif has_nf:
            # クリアはしていないが NF プレイはある譜面
            # print(f"NF leaderboard (星 {star_bucket})")
            star_nf_count[star_bucket] += 1
        elif has_ss:
            # クリアはしていないが SS(スローソング)でのプレイはある譜面
            # print(f"SS leaderboard (星 {star_bucket})")
            star_ss_count[star_bucket] += 1

    # 3) StarClearStat へ変換
    stats: list[StarClearStat] = []

    for star in sorted(star_map_count.keys()):
        map_count = star_map_count[star]
        clear_count = star_clear_count.get(star, 0)
        nf_count = star_nf_count.get(star, 0)
        ss_count = star_ss_count.get(star, 0)
        clear_rate = (clear_count / map_count) if map_count > 0 else 0.0

        avg_acc: float | None
        cnt = star_acc_count.get(star, 0)
        if cnt > 0:
            avg_acc = star_acc_sum.get(star, 0.0) / cnt
        else:
            avg_acc = None

        stats.append(
            StarClearStat(
                star=star,
                map_count=map_count,
                clear_count=clear_count,
                nf_count=nf_count,
                ss_count=ss_count,
                clear_rate=clear_rate,
                average_acc=avg_acc,
            )
        )

    return stats

def _extract_scoresaber_accuracy(score_info: dict) -> Optional[float]:
    """ScoreSaber のスコア情報から精度(%)を推定して返す。

    - accuracy / acc フィールドがあればそれを優先
    - 0.0-1.0 とみなせる値は 100 倍
    - それ以外は 0-100 とみなし、範囲外は maxScore/baseScore から再計算を試みる
    取得できなければ None。
    """

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
                # 0-1 の場合は百分率に変換
                if acc_f <= 1.0:
                    return acc_f * 100.0
                # 0-100 をそのまま利用
                if acc_f <= 100.0:
                    return acc_f
                # 0-10000 くらいのケースは 100 で割る
                if acc_f <= 10000.0:
                    return acc_f / 100.0

        base = score_info.get("baseScore")
        max_score = score_info.get("maxScore")
        if base is None:
            base = score_info.get("score")
        if base is None or max_score is None:
            return None

        base_f = float(base)
        max_f = float(max_score)
        if not math.isfinite(base_f) or not math.isfinite(max_f) or max_f <= 0:
            return None

        return max(0.0, min(100.0, base_f / max_f * 100.0))
    except (TypeError, ValueError):  # noqa: BLE001
        return None