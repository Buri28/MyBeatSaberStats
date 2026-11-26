from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Optional, Callable

import math
import time
import requests
from ..scoresaber import ScoreSaberPlayer
from ..snapshot import BASE_DIR, StarClearStat
from typing import Optional, Dict, TypedDict, Callable

from ..api_error_log import log_api_failure
from ..fileio import atomic_write_text
from ..ranked_sync import (
    flag_star_drift_if_any, load_newly_ranked_checked, full_traversal_due, mismatch_traversal_allowed,
    record_full_traversal, save_newly_ranked_checked, stars_differ, star_sync_not_found_mark, star_sync_skip,
)
from ..modifiers import classify_play
from ..http_client import get_memoized, retry_backoff_seconds

CACHE_DIR = BASE_DIR / "cache"

SCORESABER_LEADERBOARDS_URL = "https://scoresaber.com/api/leaderboards"
SCORESABER_PLAYER_SCORES_URL = "https://scoresaber.com/api/player/{player_id}/scores"
# ランク譜面の全件取り直しの記録（いつ・最後まで取れたか・総数が合わないまま終わったか）。判定は ranked_sync.py。
_RANKED_FULL_TRAVERSAL_STATE = CACHE_DIR / "ranked_full_traversal.json"
SCORESABER_PLAYER_FULL_URL = "https://scoresaber.com/api/player/{player_id}/full"

#: 無限ページングを防ぐための保険。
#: 正常系を途中で打ち切って取りこぼさないよう、実データより十分大きく取る。
#: 1 ページ 100 件なので 1000 ページ = 10 万件相当。ranked 譜面（数千件）も
#: 重量級プレイヤーのスコア数もこれを下回るため、到達するのは API 異常時だけ。
_MAX_PAGES_GUARD = 1000

#: スコアのページ取得で、5xx（500 / 502 / 504）・タイムアウト・接続の失敗のときに試す回数（BL と同じ 3 回）。
#: 429 / 503 は PoliteSession が Retry-After に従って既にリトライしているので、ここでは重ねない。
_PAGE_FETCH_ATTEMPTS = 3
_RETRYABLE_STATUS = frozenset({500, 502, 504})


def _until_str(value: datetime) -> str:
    """fetch_until を API の timeSet と比べる UTC の文字列にする。

    タイムゾーンの無い値は UTC として扱う（collector・画面から渡す値は UTC にそろえてある）。
    タイムゾーン付きの値は UTC に直す（そのまま strftime するとその地域の時刻のまま "Z" が付く）。
    """
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _is_rate_limited(resp: requests.Response, api_name: str, detail: str) -> bool:
    """レート制限を受けたレスポンスかどうか判定し、そうならログに残す。

    http_client 側で Retry-After に従ったリトライを行った上でなお 429 が返る場合に
    ここへ来る。黙って途中打ち切りにすると「データが欠けている」ことに気付けないため、
    明示的にログへ記録する。
    """
    if getattr(resp, "status_code", None) != 429:
        return False
    log_api_failure(
        "scoresaber",
        api_name,
        f"rate limited (HTTP 429) after retries: {detail} / "
        "取得を中断しました。しばらく時間を置いて再試行してください。",
    )
    print(f"ScoreSaber APIのレート制限に達しました。取得を中断します: {detail}")
    return True


def _normalize_leaderboard_key(value: object) -> Optional[str]:
    if value is None:
        return None
    key = str(value).strip()
    return key or None


def _normalize_leaderboard_cache(leaderboards: dict) -> dict[str, dict]:
    normalized: dict[str, dict] = {}
    for raw_key, lb in leaderboards.items():
        if not isinstance(lb, dict):
            continue
        lb_id = _normalize_leaderboard_key(lb.get("id") or raw_key)
        if lb_id is None:
            continue
        normalized[lb_id] = lb
    return normalized


def _load_cached_rank_maps(path: Path) -> Optional[dict]:
    """
    ScoreSaber等のAPIレスポンスをキャッシュしたJSONファイルからページリストを読み込む。
    壊れている場合や形式違いはNoneを返す。
    """
    print("■Call scoresaber._load_cached_rank_maps")
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        leaderboards = raw.get("leaderboards")
        if isinstance(leaderboards, dict):
            return _normalize_leaderboard_cache(leaderboards)
    except Exception:
        return None
    return None

def _touch_cache_fetched_at(path: Path) -> None:
    """既存キャッシュの fetched_at フィールドを現在時刻に更新する。データは変更しない。"""
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw["fetched_at"] = datetime.utcnow().isoformat() + "Z"
            atomic_write_text(path, json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _save_cached_ranked_maps(
    path: Path, ranked_maps: dict, max_pages: int, total_maps: int, fetched_at: Optional[str] = None,
) -> None:
    """
    ページリストをキャッシュファイル(JSON)として保存する。

    fetched_at を渡すとその値を残す（取得が途中で失敗したとき、次回の遡り範囲を縮めないため）。
    """
    print("Entering _save_cached_ranked_maps")
    normalized_ranked_maps = _normalize_leaderboard_cache(ranked_maps)
    payload = {
        "fetched_at": fetched_at or (datetime.utcnow().isoformat() + "Z"),
        "leaderboards": normalized_ranked_maps,
        "max_pages": max_pages,
        "total_maps": len(normalized_ranked_maps),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return

# 初回取得（遡る基準が無い）が途中で失敗したとき、次回は最初から全部遡るための印
_RESUME_FROM_BEGINNING = "2000-01-01T00:00:00Z"


def _save_cached_player_scores(
    path: Path, scores: dict, resume_stop_ts: Optional[str] = None, resume_ranges: Optional[list] = None,
    fetched_at: Optional[str] = None,
) -> None:
    """
    スコアをキャッシュファイル(JSON)として保存する。

    fetched_at を渡すとその値を残す（1 ページも読めなかった回に、取りこぼしの印だけを書き足すとき用。
    fetched_at は 60 日遡りの基準なので、何も読めていない回に進めない）。

    resume_ranges: 途中で読めなかった timeSet の範囲（_load_resume_ranges の形）。次回はその範囲だけ取り直す。
    resume_stop_ts: 古い形の印（次回はこの timeSet まで page 1 から遡る）。resume_ranges が無いときだけ書く。
    resume_ranges を書くときも、古い版が読めるよう resume_stop_ts に一番古い範囲の下限を入れる。
    """
    print("Entering _save_cached_player_scores")
    # キーで降順ソート（leaderboard_id ベース）
    scores = dict(sorted(scores.items(), key=lambda item: str(item[0]), reverse=True))
    payload = {
        "fetched_at": fetched_at or (datetime.utcnow().isoformat() + "Z"),
        "total_play_count": len(scores),
        "scores": scores,
    }
    ranges = _merge_resume_ranges(resume_ranges) if resume_ranges else []
    if ranges:
        payload["resume_ranges"] = ranges
        payload["resume_stop_ts"] = min(r["older_ts"] for r in ranges)
    elif resume_stop_ts and resume_ranges is None:
        payload["resume_stop_ts"] = resume_stop_ts
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return


def _build_scoresaber_star_stats(leaderboards: dict, scores: list[dict]) -> list[StarClearStat]:
    """ScoreSaber の ranked leaderboard / player score から★別統計を組み立てる。"""

    if not leaderboards:
        return []

    star_map_count: dict[int, int] = defaultdict(int)
    leaderboard_star_bucket: dict[str, int] = {}

    for lb in leaderboards.values():
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
    star_na_count: dict[int, int] = defaultdict(int)
    star_fc_count: dict[int, int] = defaultdict(int)
    star_acc_sum: dict[int, float] = defaultdict(float)
    star_acc_count: dict[int, int] = defaultdict(int)
    star_pp_sum: dict[int, float] = defaultdict(float)

    class _PerLeaderboardState(TypedDict):
        star: int
        clear: bool
        nf: bool
        ss: bool
        na: bool
        best_acc: Optional[float]
        has_fc: bool
        best_pp: Optional[float]

    per_leaderboard: dict[str, _PerLeaderboardState] = {}

    for item in scores:
        score_info = item.get("score") if isinstance(item, dict) else None
        leaderboard = item.get("leaderboard") if isinstance(item, dict) else None

        if leaderboard is None and isinstance(item, dict):
            leaderboard = item

        if not isinstance(leaderboard, dict):
            continue

        diff = leaderboard.get("difficulty") or {}
        lb_id_raw = leaderboard.get("id") or diff.get("leaderboardId")
        if lb_id_raw is None:
            continue
        lb_id = str(lb_id_raw)

        if lb_id not in leaderboard_star_bucket:
            continue

        star_bucket = leaderboard_star_bucket[lb_id]
        state = per_leaderboard.get(lb_id)
        if state is None:
            state = _PerLeaderboardState(
                star=star_bucket,
                clear=False,
                nf=False,
                ss=False,
                na=False,
                best_acc=None,
                has_fc=False,
                best_pp=None,
            )
            per_leaderboard[lb_id] = state

        modifiers = ""
        if isinstance(score_info, dict):
            modifiers = str(score_info.get("modifiers") or "")

        # クリア / NF / SS / NA の判定は modifiers.classify_play にそろえる（プレイリストの ✔ と同じ基準）
        play_kind = classify_play(modifiers)
        is_nf = play_kind == "nf"
        is_ss = play_kind == "ss"
        is_na = play_kind == "na"

        # PP は修飾子（クリア / NF / SS / NA）に関係なく、サーバーが pp を付けたスコアを全部入れる。
        # プロフィールの pp と同じ合計にして、PP のずれ検知（reconcile）が誤判定しないようにする。
        if isinstance(score_info, dict):
            try:
                pp_val = float(score_info.get("pp") or 0)
                if math.isfinite(pp_val) and pp_val > 0:
                    prev_pp = state.get("best_pp")
                    if prev_pp is None or pp_val > prev_pp:
                        state["best_pp"] = pp_val
            except (TypeError, ValueError):
                pass

        if is_nf:
            state["nf"] = True
        elif is_ss:
            state["ss"] = True
        elif is_na:
            state["na"] = True
        else:
            state["clear"] = True

            if isinstance(score_info, dict) and score_info.get("fullCombo") is True:
                state["has_fc"] = True

            acc: Optional[float] = None
            if isinstance(score_info, dict):
                acc = _extract_scoresaber_accuracy(score_info)
                if acc is None:
                    try:
                        base = score_info.get("baseScore") or score_info.get("score") or score_info.get("modifiedScore")
                        max_score_lb = leaderboard.get("maxScore")
                        if base is not None and max_score_lb is not None:
                            base_f = float(base)
                            max_f = float(max_score_lb)
                            if math.isfinite(base_f) and math.isfinite(max_f) and max_f > 0:
                                acc = max(0.0, min(100.0, base_f / max_f * 100.0))
                    except (TypeError, ValueError):
                        acc = None

            if acc is not None:
                best = state.get("best_acc")
                if best is None or acc > best:
                    state["best_acc"] = acc

    for state in per_leaderboard.values():
        star_bucket = int(state["star"])
        has_clear = bool(state["clear"])
        has_nf = bool(state["nf"])
        has_ss = bool(state["ss"])
        has_na = bool(state["na"])

        if has_clear:
            star_clear_count[star_bucket] += 1
            if state.get("has_fc"):
                star_fc_count[star_bucket] += 1
            best_acc = state.get("best_acc")
            if isinstance(best_acc, (int, float)) and math.isfinite(float(best_acc)):
                star_acc_sum[star_bucket] += float(best_acc)
                star_acc_count[star_bucket] += 1
        elif has_nf:
            star_nf_count[star_bucket] += 1
        elif has_ss:
            star_ss_count[star_bucket] += 1
        elif has_na:
            star_na_count[star_bucket] += 1

    cleared_pp_entries: list[tuple[int, float]] = []
    for state in per_leaderboard.values():
        # 修飾子で絞らない（上の best_pp のコメント参照）
        pp_val = state.get("best_pp")
        if isinstance(pp_val, (int, float)) and math.isfinite(float(pp_val)) and float(pp_val) > 0:
            cleared_pp_entries.append((int(state["star"]), float(pp_val)))

    cleared_pp_entries.sort(key=lambda x: x[1], reverse=True)
    for rank, (star_bucket, pp_val) in enumerate(cleared_pp_entries, start=1):
        star_pp_sum[star_bucket] += pp_val * (0.965 ** (rank - 1))

    star_pp_solo_sum: dict[int, float] = defaultdict(float)
    star_pp_list: dict[int, list[float]] = defaultdict(list)
    for star_bucket, pp_val in cleared_pp_entries:
        star_pp_list[star_bucket].append(pp_val)
    for star_bucket, pp_vals in star_pp_list.items():
        for local_rank, pp_v in enumerate(sorted(pp_vals, reverse=True), start=1):
            star_pp_solo_sum[star_bucket] += pp_v * (0.965 ** (local_rank - 1))

    all_stars = (
        set(star_map_count.keys())
        | set(star_clear_count.keys())
        | set(star_nf_count.keys())
        | set(star_ss_count.keys())
        | set(star_na_count.keys())
    )
    stats: list[StarClearStat] = []
    for star in sorted(all_stars):
        map_count = star_map_count.get(star, 0)
        clear_count = star_clear_count.get(star, 0)
        nf_count = star_nf_count.get(star, 0)
        ss_count = star_ss_count.get(star, 0)
        na_count = star_na_count.get(star, 0)
        clear_rate = (clear_count / map_count) if map_count > 0 else 0.0

        cnt = star_acc_count.get(star, 0)
        avg_acc = (star_acc_sum.get(star, 0.0) / cnt) if cnt > 0 else None

        pp_total = star_pp_sum.get(star)
        pp_contribution = float(pp_total) if pp_total is not None and pp_total > 0 else (0.0 if clear_count > 0 else None)
        pp_solo_val = star_pp_solo_sum.get(star)
        pp_solo = float(pp_solo_val) if pp_solo_val is not None and pp_solo_val > 0 else (0.0 if clear_count > 0 else None)

        stats.append(
            StarClearStat(
                star=star,
                map_count=map_count,
                clear_count=clear_count,
                nf_count=nf_count,
                ss_count=ss_count,
                na_count=na_count,
                clear_rate=clear_rate,
                average_acc=avg_acc,
                fc_count=star_fc_count.get(star, 0),
                pp_contribution=pp_contribution,
                pp_solo=pp_solo,
            )
        )

    return stats


def collect_scoresaber_star_stats_from_cache(scoresaber_id: str) -> list[StarClearStat]:
    """現在の ScoreSaber キャッシュだけを使って★別統計を再計算する。"""

    if not scoresaber_id:
        return []

    ranked_cache_path = CACHE_DIR / "scoresaber_ranked_maps.json"
    score_cache_path = CACHE_DIR / f"scoresaber_player_scores_{scoresaber_id}.json"

    leaderboards = _load_cached_rank_maps(ranked_cache_path) or {}
    scores_dict = _load_cached_player_scores(score_cache_path) or {}
    return _build_scoresaber_star_stats(leaderboards, list(scores_dict.values()))


def _get_scoresaber_leaderboards_ranked(
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    fetch_until: Optional[datetime] = None,
    status: Optional[dict] = None,
) -> dict:
    """ScoreSaber の Ranked leaderboards をキャッシュ付きで全件取得する。

    status に dict を渡すと、途中で失敗して最後まで取れなかったときに status["complete"] = False を入れる。

    progress が指定されている場合は、0.0〜1.0 の範囲で簡易的に進捗をコールバックする。
    GUI 側のプログレスバー更新用（初回の全件取得が長時間になる対策）。
    """

    cache_path = CACHE_DIR / "scoresaber_ranked_maps.json"

    # fetch_until が指定されている場合は ISO 文字列に変換して比較に使用する
    fetch_until_str: Optional[str] = None
    if fetch_until is not None:
        fetch_until_str = _until_str(fetch_until)
        print(f"ScoreSaber Ranked Maps fetch_until 指定: {fetch_until_str}")
        if fetch_until_str > "2001" and full_traversal_due(_RANKED_FULL_TRAVERSAL_STATE, "scoresaber"):
            # 窓の外で変わった★は日付の窓では拾えない。初回と★のずれを見つけたときだけ全件取り直す。
            print("SS Ranked Maps: 全件取得の記録が無いか★のずれを検知したため、全件取り直しをします")
            fetch_until = datetime(2000, 1, 1)
            fetch_until_str = fetch_until.strftime("%Y-%m-%dT%H:%M:%S") + "Z"

    # コンソール出力は日本語、画面表示は英語
    print("ScoreSaberのRanked譜面をキャッシュ付きで取得中...")
    leaderboards = {}
    
    cached_leaderboards = _load_cached_rank_maps(cache_path)
    print(f"既存のScoreSaberリーダーボードキャッシュを読み込み完了。{len(cached_leaderboards) if cached_leaderboards is not None else 0}件")

    #すべての譜面データのidを格納するdicationaryを作成
    #existing_lb_ids = {}
    if cached_leaderboards is not None:
        leaderboards = _normalize_leaderboard_cache(cached_leaderboards)
        # # 重複を省くため、IDをキーにした辞書に格納
        # for lb in cached_leaderboards:
        #     lb_id = lb.get("id")
        #     if leaderboards.get(lb_id) is None:
        #         leaderboards[lb_id] = lb

    
    # キャッシュから既存譜面データを読み込み
    cached_total = len(leaderboards) #load_cached_ranked_maps(cached_leaderboards, leaderboards, existing_lb_ids)
    latest_total = 0
    new_total = cached_total

    # fetch_until が指定されている場合は DateRanked 降順でページングし日付境界まで取得する
    if fetch_until_str is not None:
        print(f"ScoreSaber Ranked Maps: fetch_until モードで取得開始 (境界={fetch_until_str})")
        append_leaderboards: dict = {}
        reached_fetch_until = False
        page_no = 1
        run_complete = True
        seen_ids: set[str] = set()
        api_total: Optional[int] = None
        # 一覧の終わり（100 件未満のページ）まで自然に読み終えたか。404・空ページ・ページ上限で抜けたときは False
        # （ただし、読んだ件数が API の総数に届いた後の 404・空ページは、ちょうど割り切れた一覧の終わりとみなす）。
        natural_end = False
        raw_items = 0  # 一覧で読んだ件数（重複を含む）
        # 2000 年まで遡る＝全件取得。このときは見つかった譜面だけでキャッシュを作り直し、ランクから外れた譜面を消す。
        full_traversal = fetch_until_str <= "2001"
        previous_fetched_at: Optional[str] = None
        try:
            _prev_raw = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else None
            if isinstance(_prev_raw, dict) and isinstance(_prev_raw.get("fetched_at"), str):
                previous_fetched_at = _prev_raw["fetched_at"]
        except Exception:  # noqa: BLE001
            previous_fetched_at = None
        try:
            while True:
                if progress is not None:
                    progress(page_no, None)
                params_dt = {
                    "ranked": "true",
                    "category": "1",   # DateRanked
                    "sort": "0",        # Descending (newest first)
                    "page": str(page_no),
                }
                resp_dt = session.get(SCORESABER_LEADERBOARDS_URL, params=params_dt, timeout=10)
                print(f"SS Ranked Maps (DateRanked desc) page {page_no}: {resp_dt.url}")
                if resp_dt.status_code == 404:
                    natural_end = api_total is not None and 0 < raw_items >= api_total
                    break
                if _is_rate_limited(resp_dt, "_get_scoresaber_ranked_maps", f"ranked maps page={page_no}"):
                    run_complete = False
                    break
                resp_dt.raise_for_status()
                data_dt = resp_dt.json()
                if api_total is None:
                    try:
                        api_total = int((data_dt.get("metadata") or {}).get("total"))
                    except (TypeError, ValueError):
                        api_total = None
                lbs_dt = data_dt.get("leaderboards") or []
                if not lbs_dt:
                    # 1 件も読めていない（総数 0 の異常な応答など）ときは、一覧の終わりとみなさない
                    natural_end = api_total is not None and 0 < raw_items >= api_total
                    break
                raw_items += len(lbs_dt)
                for lb in lbs_dt:
                    # rankedDate 降順では、rankedDate が null の古い譜面（約300件）が先頭に並ぶ。
                    # createdDate（2018年など）で境界判定すると、1ページ目で打ち切って新しいランク譜面を取り逃がすため、
                    # rankedDate がある譜面だけで境界を判定する（null の譜面は取り込んで次へ進む）。
                    cd = lb.get("rankedDate") or ""
                    if isinstance(cd, str) and cd:
                        # rankedDate を fetch_until_str と比較 (ISO 文字列の辞書順比較)
                        if cd < fetch_until_str:
                            print(f"SS Ranked Maps: fetch_until に到達 (rankedDate={cd})")
                            reached_fetch_until = True
                            break
                    lb_id = _normalize_leaderboard_key(lb.get("id"))
                    if lb_id is None:
                        continue
                    seen_ids.add(lb_id)
                    # 既存マップも上書き（stars等が月次更新で変わっている可能性があるため）
                    if lb_id not in leaderboards or leaderboards[lb_id] != lb:
                        append_leaderboards[lb_id] = lb
                    leaderboards[lb_id] = lb
                if reached_fetch_until:
                    natural_end = True
                    break
                meta_dt = data_dt.get("metadata") or {}
                try:
                    per_page_dt = int(meta_dt.get("itemsPerPage", 100)) or 100
                except (TypeError, ValueError):
                    per_page_dt = 100
                if len(lbs_dt) < per_page_dt:
                    natural_end = True
                    break
                if page_no >= _MAX_PAGES_GUARD:
                    print(f"SS Ranked Maps: ページ数上限 {_MAX_PAGES_GUARD} に達したため打ち切ります")
                    break
                page_no += 1
        except Exception as exc:  # noqa: BLE001
            # 進捗ダイアログのキャンセルは握りつぶさない
            if isinstance(exc, RuntimeError) and str(exc) == "SNAPSHOT_CANCELLED":
                raise
            run_complete = False
        if not run_complete and status is not None:
            status["complete"] = False

        removed = 0
        if not full_traversal and run_complete and not reached_fetch_until and api_total is not None \
                and len(seen_ids) >= api_total:
            # 境界に達する前に一覧が終わり、API の総数まで全部読めた＝実質、全件取得
            full_traversal = True
        if full_traversal:
            # 失敗・レート制限でなく、一覧の終わり（100 件未満のページ）まで自然に読めた
            listing_ended = bool(run_complete and natural_end)
            complete_full = bool(listing_ended and seen_ids and api_total is not None and len(seen_ids) >= api_total)
            if complete_full:
                # API の総数まで全部取れたので、見つからなかった譜面（ランクから外れたもの）を消す
                stale = [lb_id for lb_id in leaderboards if lb_id not in seen_ids]
                for lb_id in stale:
                    del leaderboards[lb_id]
                removed = len(stale)
            elif not listing_ended and run_complete:
                # 途中で止まった（404・空ページ・ページ上限など）。消すと残りの譜面まで消えるので消さない。失敗として扱う
                print(f"SS Ranked Maps: 全件取得が途中で止まりました（{len(seen_ids)}/{api_total} 件）。古い譜面は消しません")
                run_complete = False
                if status is not None:
                    status["complete"] = False
            elif listing_ended:
                # 一覧は最後まで読めたが、列挙数が総数に届かない（null 日付の並びが不安定）。失敗ではないので
                # status["complete"] は落とさない。消さず、取得日時も進めない（次回も同じ範囲まで遡る）。
                print(f"SS Ranked Maps: 一覧は最後まで読めましたが、列挙数が総数に届きません（{len(seen_ids)}/{api_total} 件）")
                run_complete = False
            if listing_ended and status is not None:
                # PP の取り直しで返らなかったスコアを外すとき、今回の一覧に実際に出てきた（＝今ランクの）譜面だけに絞る。
                # 列挙数が総数に届かない（null 日付の並びが不安定）だけなら、失敗ではないことも伝える。
                status["ss_ranked_seen_ids"] = set(seen_ids)
                if not complete_full:
                    status["ss_ranked_listing_short"] = True
            # 完了＝一覧が最後まで読めて、キャッシュの件数が API の総数と合った。
            # 注意：rankedDate が null の譜面（約300件）は並び順が不安定で、巡回のたびに数十件が重複・欠落する
            # （実 API で 6243 件中 6161 件しか列挙されないことを確認）。列挙数（seen）が総数に届かなくても、
            # キャッシュの件数が総数と合っていれば欠けは無いとみなす（消す処理は seen が総数に届いたときだけ行う）。
            ok = bool(listing_ended and api_total is not None and len(leaderboards) == api_total)
            # 記録：一覧を最後まで読めたか（失敗でなく一覧が終わった）、読めたのに総数が合わなかったか。
            # 合わないまま繰り返さないため、総数が合わないときは API の総数も残す。
            record_full_traversal(
                _RANKED_FULL_TRAVERSAL_STATE, "scoresaber", ok,
                api_total if listing_ended and not ok else None,
            )
        else:
            if run_complete and reached_fetch_until:
                # 窓の中（rankedDate が境界以降）の譜面は全部読んだ。API に無い＝ランクから外れた/入れ替わった。
                # 総数が同じままの入れ替わりは総数の突き合わせでは見えないので、ここで消す。
                # （誤って消しても、直後の総数チェックで食い違えば全件取り直して戻る）
                stale = [
                    lb_id for lb_id, cached_lb in leaderboards.items()
                    if lb_id not in seen_ids
                    and isinstance(cached_lb.get("rankedDate"), str) and cached_lb["rankedDate"] >= fetch_until_str
                ]
                for lb_id in stale:
                    del leaderboards[lb_id]
                removed = len(stale)
                if stale:
                    print(f"SS Ranked Maps: 窓の中でランクから外れた譜面 {len(stale)} 件をキャッシュから消しました")
            if (
                run_complete and api_total is not None and len(leaderboards) != api_total
                and mismatch_traversal_allowed(_RANKED_FULL_TRAVERSAL_STATE, "scoresaber", api_total)
            ):
                # キャッシュと API の総数が合わない＝窓より古い所の取りこぼし/ランク外れが残っている。全件取り直す。
                print(
                    f"SS Ranked Maps: キャッシュ {len(leaderboards)} 件 ≠ API {api_total} 件のため、全件取り直します"
                )
                return _get_scoresaber_leaderboards_ranked(
                    session, progress=progress, fetch_until=datetime(2000, 1, 1), status=status,
                )

        if append_leaderboards or removed:
            try:
                leaderboards = dict(sorted(leaderboards.items(), key=lambda x: x[1].get("id", 0), reverse=True))
                _save_cached_ranked_maps(cache_path, leaderboards,
                                         max_pages=page_no,
                                         total_maps=len(leaderboards),
                                         # 途中で失敗したら取得日時を進めない（次回も同じ範囲まで遡る）
                                         fetched_at=None if run_complete else previous_fetched_at)
            except Exception:  # noqa: BLE001
                pass
        elif run_complete:
            # データ変更なし（plays/votesも含めて完全一致）でも fetched_at を更新して
            # 次回の60日窓が正しく前進するようにする（失敗したときは進めない）
            _touch_cache_fetched_at(cache_path)
        if progress is not None:
            progress(page_no, page_no)
        print(f"SS Ranked Maps fetch_until 完了: 追加={len(append_leaderboards)} 総計={len(leaderboards)}")
        return leaderboards

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
            # total が増えていなければキャッシュをそのまま利用（fetch_until 指定時は強制再取得 → 上のブロックで処理済み）
            if latest_total <= cached_total:
                if progress is not None:
                    # ページ数情報が無いので 1/1 として通知
                    progress(1, 1)
                print("ScoreSaberリーダーボードのキャッシュは最新です。①")
                # 取得済みを記録するため fetched_at だけ更新する
                _touch_cache_fetched_at(cache_path)
                return leaderboards
            
            # total がズレた場合は差分追加ではなく全件再構築する。
            # Ranked/Unranked の入れ替わりや過去の壊れたキャッシュを自己修復するため。
            print(f"ScoreSaberリーダーボードに差分があります。{latest_total - cached_total}件差のため、キャッシュを全件再構築します...")

            rebuilt_leaderboards: dict = {}
            data_lbs = data.get("leaderboards") or []
            add_leaderboards(rebuilt_leaderboards, rebuilt_leaderboards, data_lbs)

            total_pages_new = math.ceil(latest_total / per_page)
            rebuild_failed = False

            print(f"新しい総ページ数: {total_pages_new}")
            print(f"再構築開始時点の取得件数: {len(rebuilt_leaderboards)}, 最新総譜面数: {latest_total}")

            for i in range(2, total_pages_new + 1):
                if progress is not None:
                    progress(i, total_pages_new)
                params_page = {
                    "ranked": "true",
                    "page": str(i),
                }
                try:
                    resp_page = session.get(SCORESABER_LEADERBOARDS_URL, params=params_page, timeout=10)
                    print(
                        f"ScoreSaberリーダーボードの再構築ページ {i} を取得中... URL: {resp_page.url} params: {params_page}"
                    )
                    if resp_page.status_code == 404:
                        rebuild_failed = True
                        break
                    if _is_rate_limited(resp_page, "_get_scoresaber_ranked_maps", f"rebuild page={i}"):
                        # 中途半端なキャッシュで上書きしないよう失敗扱いにする
                        rebuild_failed = True
                        break
                    resp_page.raise_for_status()
                    data_page = resp_page.json()
                except Exception:  # noqa: BLE001
                    rebuild_failed = True
                    break

                page_lbs = data_page.get("leaderboards") or []
                if not page_lbs:
                    rebuild_failed = True
                    break

                add_leaderboards(rebuilt_leaderboards, rebuilt_leaderboards, page_lbs)
                print(f"再構築で取得した譜面数: {len(page_lbs)} 累計: {len(rebuilt_leaderboards)} / {latest_total}")

            if not rebuild_failed and rebuilt_leaderboards:
                print("ScoreSaberリーダーボードの保存中...")
                try:
                    rebuilt_leaderboards = dict(
                        sorted(rebuilt_leaderboards.items(), key=lambda x: x[1].get("id", 0), reverse=True)
                    )
                    _save_cached_ranked_maps(
                        cache_path,
                        rebuilt_leaderboards,
                        max_pages=total_pages_new,
                        total_maps=len(rebuilt_leaderboards),
                    )
                except Exception:  # noqa: BLE001
                    pass

                if progress is not None:
                    progress(total_pages_new, total_pages_new)
                print("ScoreSaberリーダーボードのキャッシュ再構築が完了しました。")
                return rebuilt_leaderboards

            print("ScoreSaberリーダーボードの再構築に失敗したため、既存キャッシュを利用します。")
            if progress is not None:
                progress(1, 1)
            return leaderboards
    except Exception as exc:  # noqa: BLE001
        log_api_failure(
            "scoresaber",
            "_get_scoresaber_leaderboards_ranked",
            f"metadata check failed url={SCORESABER_LEADERBOARDS_URL}",
            exc,
        )
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

def add_leaderboards(leaderboards: dict, existing_lb_ids: dict, data_lbs: list):    
    for lb in data_lbs:
        if not isinstance(lb, dict):
            continue
        lb_id = _normalize_leaderboard_key(lb.get("id"))
        if lb_id is None:
            continue
        if lb_id not in existing_lb_ids:
            leaderboards[lb_id] = lb
            existing_lb_ids[lb_id] = lb

# def _get_scoresaber_player_scores_old(
#     scoresaber_id: str,
#     session: requests.Session,
#     progress: Optional[Callable[[int, Optional[int]], None]] = None,
# ) -> list[dict]:
#     """ScoreSaber のプレイヤースコアをキャッシュ付きで全件取得する。

#     progress が与えられた場合、現在のページ番号と推定最大ページ数を通知する。
#     """

#     cache_path = CACHE_DIR / f"scoresaber_player_scores_{scoresaber_id}.json"

#     print(f"ScoreSaberのキャッシュ読み込み: {scoresaber_id}")
#     cached_pages = _load_cached_pages(cache_path)
#     if cached_pages is not None:
#         print(f"ScoreSaberのキャッシュ読み込み成功: {scoresaber_id}")

#         # Try to read cached fetched_at timestamp (if available) so we can check
#         # whether there are new "recent" scores to fetch.
#         fetched_at: Optional[datetime] = None
#         try:
#             raw = json.loads(cache_path.read_text(encoding="utf-8"))
#             fa = raw.get("fetched_at")
#             if isinstance(fa, str) and fa:
#                 if fa.endswith("Z"):
#                     fa = fa[:-1]
#                 fetched_at = datetime.fromisoformat(fa)
#         except Exception:  # noqa: BLE001
#             fetched_at = None

#         # Aggregate existing cached scores
#         scores: list[dict] = []
#         for page_obj in cached_pages:
#             if not isinstance(page_obj, dict):
#                 continue
#             data = page_obj.get("data") or {}
#             items = data.get("playerScores") or data.get("scores") or []
#             if isinstance(items, list):
#                 scores.extend(it for it in items if isinstance(it, dict))

#         # If this cache was produced with sort=recent and we have a fetched_at,
#         # check page 1 of the API for newer items and fetch new pages if necessary.
#         try:
#             page1 = None
#             for p in cached_pages:
#                 if isinstance(p, dict) and p.get("page") == 1:
#                     page1 = p
#                     break
#             sort_is_recent = False
#             limit_param = 100
#             if page1:
#                 params_page1 = page1.get("params") or {}
#                 if isinstance(params_page1, dict):
#                     if params_page1.get("sort") == "recent":
#                         sort_is_recent = True
#                     if params_page1.get("limit"):
#                         try:
#                             # Ensure we convert to string first to handle non-str types
#                             limit_param = int(str(params_page1.get("limit")))
#                         except Exception:  # noqa: BLE001
#                             limit_param = 100

#             if fetched_at is not None and sort_is_recent:
#                 url = SCORESABER_PLAYER_SCORES_URL.format(player_id=scoresaber_id)
#                 params_check = {"limit": str(limit_param), "sort": "recent", "page": "1"}
#                 resp = session.get(url, params=params_check, timeout=10)
#                 print(f"最新スコアチェック... URL: {resp.url} params: {params_check}")
#                 if resp.status_code != 404:
#                     resp.raise_for_status()
#                     data = resp.json()
#                     items = data.get("playerScores") or data.get("scores") or []
#                     if isinstance(items, list) and items:
#                         first = items[0]
#                         score_obj = first.get("score") if isinstance(first, dict) else first
#                         tstr = None
#                         if isinstance(score_obj, dict):
#                             tstr = score_obj.get("timeSet")

#                         if isinstance(tstr, str):
#                             try:
#                                 tcmp = datetime.fromisoformat(tstr[:-1]) if tstr.endswith("Z") else datetime.fromisoformat(tstr)
#                             except Exception:
#                                 tcmp = None

#                             if tcmp is not None and tcmp > fetched_at:
#                                 new_pages: list[dict] = []
#                                 max_new_pages = 50
#                                 for page_num in range(1, max_new_pages + 1):
#                                     params_page = {"limit": str(limit_param), "sort": "recent", "page": str(page_num)}
#                                     try:
#                                         resp_page = session.get(url, params=params_page, timeout=10)
#                                         if resp_page.status_code == 404:
#                                             break
#                                         resp_page.raise_for_status()
#                                     except Exception:  # noqa: BLE001
#                                         break

#                                     try:
#                                         data_page = resp_page.json()
#                                     except Exception:  # noqa: BLE001
#                                         break

#                                     items_page = data_page.get("playerScores") or data_page.get("scores") or []
#                                     if not items_page:
#                                         break

#                                     first_page = items_page[0]
#                                     score_first_page = first_page.get("score") if isinstance(first_page, dict) else first_page
#                                     tfirst = None
#                                     if isinstance(score_first_page, dict):
#                                         tfirst = score_first_page.get("timeSet")
#                                     if isinstance(tfirst, str):
#                                         try:
#                                             tfirst_dt = datetime.fromisoformat(tfirst[:-1]) if tfirst.endswith("Z") else datetime.fromisoformat(tfirst)
#                                         except Exception:
#                                             tfirst_dt = None
#                                     else:
#                                         tfirst_dt = None

#                                     if tfirst_dt is None or not (tfirst_dt > fetched_at):
#                                         break

#                                     new_pages.append({"page": page_num, "params": params_page, "data": data_page})

#                                 if new_pages:
#                                     last_new_page = new_pages[-1].get("page") or 0
#                                     remaining_cached = [p for p in cached_pages if (p.get("page") or 0) > last_new_page]
#                                     pages_merged = new_pages + remaining_cached
#                                     try:
#                                         _save_cached_pages(cache_path, pages_merged)
#                                     except Exception:  # noqa: BLE001
#                                         pass

#                                     scores = []
#                                     for page_obj in pages_merged:
#                                         if not isinstance(page_obj, dict):
#                                             continue
#                                         data = page_obj.get("data") or {}
#                                         items = data.get("playerScores") or data.get("scores") or []
#                                         if isinstance(items, list):
#                                             scores.extend(it for it in items if isinstance(it, dict))

#         except Exception:  # noqa: BLE001
#             # If anything goes wrong during the freshness check, fall back to cached data
#             pass

#         if progress is not None:
#             # キャッシュ読み込み時は 1 ページのみとして通知
#             progress(1, 1)
#         print(f"ScoreSaberのキャッシュ返却: {scoresaber_id} 件数: {len(scores)}")
#         return scores

#     pages: list[dict] = []
#     scores: list[dict] = []

#     page = 1
#     max_pages_sc: int | None = None
#     page_size = 100

#     while True:
#         url = SCORESABER_PLAYER_SCORES_URL.format(player_id=scoresaber_id)
#         params = {
#             "limit": str(page_size),
#             "sort": "recent",
#             "page": str(page),
#         }
#         print(f"ScoreSaberのキャッシュ取得API呼び出し: {scoresaber_id} ページ: {page}")
#         try:
#             resp = session.get(url, params=params, timeout=10)
#             print(f"Fetching ScoreSaber player scores page {page} for star stats... URL: {resp.url} params: {params}")
#             if resp.status_code == 404:
#                 break
#             resp.raise_for_status()
#         except Exception:  # noqa: BLE001
#             break

#         try:
#             data = resp.json()
#         except Exception:  # noqa: BLE001
#             break

#         pages.append({"page": page, "params": params, "data": data})

#         if max_pages_sc is None:
#             meta = data.get("metadata") or {}
#             try:
#                 total = int(meta.get("total", 0))
#                 per_page = int(meta.get("itemsPerPage", page_size)) or page_size
#             except (TypeError, ValueError):
#                 total = 0
#                 per_page = page_size

#             if total > 0 and per_page > 0:
#                 computed_pages = math.ceil(total / per_page)
#                 max_pages_sc = min(computed_pages, 300)
#             else:
#                 max_pages_sc = 100

#         items = data.get("playerScores") or data.get("scores") or []
#         if not items:
#             break
#         if isinstance(items, list):
#             scores.extend(it for it in items if isinstance(it, dict))

#         # ページ進捗をコールバックで通知
#         if progress is not None:
#             progress(page, max_pages_sc)

#         if len(items) < page_size or (max_pages_sc is not None and page >= max_pages_sc):
#             break

#         page += 1

#     if pages:
#         try:
#             _save_cached_pages(cache_path, pages)
#         except Exception:  # noqa: BLE001
#             pass

#     return scores


def _ss_item_ts(item: object) -> Optional[str]:
    """API の並び（sort=recent）の基準になる timeSet。"""
    if not isinstance(item, dict):
        return None
    sc = item.get("score")
    ts = sc.get("timeSet") if isinstance(sc, dict) else None
    return ts if isinstance(ts, str) and ts else None


def _ss_item_lb_id(item: object) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    lb = item.get("leaderboard")
    lb_id_raw = lb.get("id") if isinstance(lb, dict) else None
    if lb_id_raw is None:
        # 旧形式（score_idキー）の場合もロードする
        sc = item.get("score")
        if isinstance(sc, dict):
            lb_id_raw = sc.get("leaderboardId")
    return None if lb_id_raw is None else str(lb_id_raw)


def _normalize_resume_range(value: object) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    older = value.get("older_ts")
    newer = value.get("newer_ts")
    if not isinstance(older, str) or not older:
        return None
    if newer is not None and (not isinstance(newer, str) or not newer):
        newer = None
    if newer is not None and newer <= older:
        return None
    return {"newer_ts": newer, "older_ts": older}


# 範囲の比較で newer_ts=None（一覧の先頭）を表す値。timeSet の文字列（"20xx-..."）より必ず大きい。
_TOP_TS = "~"


def _merge_resume_ranges(ranges: list[dict]) -> list[dict]:
    """重なる・接する範囲をまとめる（同じページを二度読みに行かないため）。新しい範囲から順に並べる。"""
    items = sorted(
        (r for r in (_normalize_resume_range(v) for v in ranges) if r is not None),
        key=lambda r: r["older_ts"],
    )
    merged: list[dict] = []
    for r in items:
        newer = r["newer_ts"] or _TOP_TS
        if merged and (merged[-1]["newer_ts"] or _TOP_TS) >= r["older_ts"]:
            if newer > (merged[-1]["newer_ts"] or _TOP_TS):
                merged[-1]["newer_ts"] = r["newer_ts"]
            continue
        merged.append(dict(r))
    return sorted(merged, key=lambda r: r["newer_ts"] or _TOP_TS, reverse=True)


def _load_resume_ranges(path: Path, scores: Optional[dict] = None) -> list[dict]:
    """取りこぼした範囲（resume_ranges）を読む。

    1 つの範囲は {"newer_ts": str|None, "older_ts": str}。timeSet が older_ts 以上で newer_ts より古いスコアが
    まだ読めていない、という意味（newer_ts=None は一覧の先頭から）。
    範囲を持たない古いキャッシュの resume_stop_ts は次のように直す。
    - 初回取得の途中失敗の印（_RESUME_FROM_BEGINNING）：キャッシュには読めたところまでしか無いので、
      キャッシュの一番古い timeSet から先を範囲にする（先頭から全部は読み直さない）。
    - それ以外（差分取得の途中失敗）：止まった所が分からないので、一覧の先頭からその日時までを範囲にする。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(raw, dict):
        return []
    ranges = raw.get("resume_ranges")
    if isinstance(ranges, list):
        return _merge_resume_ranges(ranges)
    stop = raw.get("resume_stop_ts")
    if not isinstance(stop, str) or not stop:
        return []
    if stop <= _RESUME_FROM_BEGINNING:
        if scores is None:
            scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
        # ランク譜面の取り直し（sort=top）は pp の付いた古いスコアもキャッシュに入れるので、一番古い所は
        # pp の付かないスコア（sort=recent の読みでしか入らない）で決める。無ければ page 1 から読む。
        ts_list = [
            t for t in (_ss_item_ts(v) for v in scores.values() if _ss_score_pp(v) <= 0) if t is not None
        ]
        if ts_list:
            return _merge_resume_ranges([{"newer_ts": min(ts_list), "older_ts": stop}])
    return [{"newer_ts": None, "older_ts": stop}]


def _load_cached_fetched_at(path: Path) -> Optional[str]:
    """スコアキャッシュの fetched_at（文字列のまま）。無ければ None。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:  # noqa: BLE001
        return None
    value = raw.get("fetched_at") if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value else None


def _load_resume_fields(path: Path) -> dict:
    """スコアキャッシュを保存し直すときに引き継ぐ、取りこぼしの印（resume_ranges / resume_stop_ts）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(raw, dict):
        return {}
    if isinstance(raw.get("resume_ranges"), list):
        return {"resume_ranges": raw["resume_ranges"]}
    if isinstance(raw.get("resume_stop_ts"), str) and raw["resume_stop_ts"]:
        return {"resume_stop_ts": raw["resume_stop_ts"]}
    return {}


def _compact_pages(pages: list[int]) -> str:
    """[1, 2, 3, 57, 58] → "1-3, 57-58"（cp932 のコンソールにも出せるよう ASCII のハイフン）。"""
    runs: list[list[int]] = []
    for p in sorted(set(pages)):
        if runs and p == runs[-1][1] + 1:
            runs[-1][1] = p
        else:
            runs.append([p, p])
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in runs)


def _get_scoresaber_player_scores(
    scoresaber_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    fetch_until: Optional[datetime] = None,
    status: Optional[dict] = None,
) -> list[dict]:
    """ScoreSaber のプレイヤースコアをキャッシュ付きで取得する。

    `/api/player/{id}/scores?sort=recent` を 1 ページ 100 件で読む（プレイした日時 timeSet の新しい順。
    新しいスコアや再プレイは先頭に入るので、古いスコアのページ番号は後ろにずれる）。

    - fetch_until=None：差分取得。page 1 から、キャッシュの最新 timeSet より古いスコアに届くまで読む。
    - fetch_until 指定：その日時より古いスコアに届くまで遡る（新ランク譜面の 60 日遡り・期間指定・Fetch ALL）。

    途中で読めなかったら、読めなかった範囲（timeSet の範囲）を resume_ranges としてキャッシュに残し、
    次の回は普段の差分取得のあとに、その範囲だけを取り直す（page 1 から全部は読み直さない）。
    取り直すページは「いまのキャッシュで範囲より新しいスコアの件数」から見積もり、読んだページの先頭の
    timeSet が範囲の始まりより新しいことを確かめる（古ければ前のページへ戻る）。
    Fetch ALL が途中で止まったときは、読めなかった残りを次の回に自動で読み直さない（キャッシュに古い値が
    あるので取りこぼしは無い。選び直してもらう）。ただし、まだ読んでいない新しいスコアの範囲は残す。
    status があれば、complete（最後まで読めたか）・warnings（結果ダイアログに出す文）・pages_read を入れる。
    """

    cache_path = CACHE_DIR / f"scoresaber_player_scores_{scoresaber_id}.json"
    # leaderboard_id をキーにしたDictionaryで管理（同一譜面の再プレイ時に最新スコアで上書きされるよう）
    scores_by_lb_id: dict[str, dict] = {}

    print(f"ScoreSaberのキャッシュ読み込み: {scoresaber_id}")
    cached_scores = _load_cached_player_scores(cache_path) if cache_path.exists() else None
    latest_cached_timeset: Optional[str] = None
    if cached_scores is not None:
        print(f"ScoreSaberのキャッシュ読み込み成功: {scoresaber_id} スコア件数: {len(cached_scores)}")
        for it in cached_scores.values():
            lb_id = _ss_item_lb_id(it)
            if lb_id is None:
                continue
            scores_by_lb_id[lb_id] = it
            ts = _ss_item_ts(it)
            if ts and (latest_cached_timeset is None or ts > latest_cached_timeset):
                latest_cached_timeset = ts
    pending_ranges = _load_resume_ranges(cache_path, cached_scores) if cached_scores is not None else []
    print(f"ScoreSaberのキャッシュ読み込み完了: {scoresaber_id} 件数: {len(scores_by_lb_id)} 最新timeSet: {latest_cached_timeset}")
    if pending_ranges:
        print(f"ScoreSaberスコア取得: 前回の取りこぼし {pending_ranges}")

    warnings: list[str] = []

    def _warn(message: str) -> None:
        print(message)
        if message not in warnings:
            warnings.append(message)

    def _mark_incomplete() -> None:
        if status is not None:
            status["complete"] = False

    fetch_until_str: Optional[str] = None
    if fetch_until is not None:
        fetch_until_str = _until_str(fetch_until)
        print(f"ScoreSaber fetch_until 指定: {fetch_until_str}")
    # Fetch ALL（利用者が選んだ全件取得）
    full_fetch = fetch_until_str is not None and fetch_until_str <= "2001"

    # 1 回目の読み（page 1 から）の止め所。None は一覧の終わりまで（初回取得）。
    # 遡り取得（期間指定・60 日遡り）でも、キャッシュの最新 timeSet より新しいスコアは全部読む必要があるので、
    # 止め所は「指定した日時」と「キャッシュの最新 timeSet」の早い方にする（指定がキャッシュの最新より新しいと、
    # その間のスコアを読まないまま次の差分取得もそこで止まり、取りこぼしたままになる）。
    main_stop: Optional[str] = fetch_until_str if fetch_until_str is not None else latest_cached_timeset
    if main_stop is not None and latest_cached_timeset is not None and latest_cached_timeset < main_stop:
        print(
            f"ScoreSaber: 指定の日時 {main_stop} がキャッシュの最新 timeSet {latest_cached_timeset} より新しいため、"
            "そこまで遡ります"
        )
        main_stop = latest_cached_timeset
    # 途中で止まったときに「読めていない」と残す下限。Fetch ALL はキャッシュに古い値があるので、
    # 取りこぼし（キャッシュに無いスコア）が出るのはキャッシュの最新 timeSet より新しい所だけ。
    required_stop: Optional[str] = latest_cached_timeset if full_fetch and latest_cached_timeset else main_stop
    for r in pending_ranges:
        if r["newer_ts"] is None and main_stop is not None and r["older_ts"] < main_stop:
            # 一覧の先頭からの範囲（止まった所が分からない古い印・page 1 から読めなかった遡り取得）は、
            # 1 回目の読みをそこまで延ばして埋める
            print(f"ScoreSaber: 前回の取得が途中で失敗したため {r['older_ts']} まで遡ります")
            main_stop = r["older_ts"]
            if required_stop is None or r["older_ts"] < required_stop:
                required_stop = r["older_ts"]
    # 1 回目の読みで届かない範囲だけ、あとで取り直す
    gap_ranges = [r for r in pending_ranges if main_stop is not None and r["older_ts"] < main_stop]
    if gap_ranges:
        print(f"ScoreSaber: 前回の取りこぼし {len(gap_ranges)} 件は、差分取得のあとにその範囲だけ取り直します")

    limit_param = 100
    url = SCORESABER_PLAYER_SCORES_URL.format(player_id=scoresaber_id)
    page_items: dict[int, list] = {}
    requested_pages: list[int] = []
    page_meta: dict[str, Optional[int]] = {"max_pages": None, "total": None}
    last_error: dict[str, str] = {"text": ""}

    def _load_page(page_no: int) -> tuple[Optional[list], bool]:
        """ページを読んでキャッシュに足す。(items, not_found)。読めなければ items=None。"""
        if page_no in page_items:
            return page_items[page_no], False
        requested_pages.append(page_no)
        params = {"limit": str(limit_param), "sort": "recent", "page": str(page_no)}
        print(f"ScoreSaberのキャッシュ取得API呼び出し: {scoresaber_id} ページ: {page_no}")
        last_error["text"] = ""
        data = None
        for attempt in range(1, _PAGE_FETCH_ATTEMPTS + 1):
            retry = False
            try:
                resp = session.get(url, params=params, timeout=10)
                print(f"Fetching ScoreSaber player scores page {page_no}... URL: {resp.url} params: {params}")
                if resp.status_code == 404:
                    return None, True
                if _is_rate_limited(resp, "_get_scoresaber_player_scores", f"player={scoresaber_id} page={page_no}"):
                    last_error["text"] = "HTTP 429"
                    return None, False
                retry = resp.status_code in _RETRYABLE_STATUS
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                text = str(exc).splitlines()[0] if str(exc) else ""
                last_error["text"] = (text or type(exc).__name__)[:160]
                retry = retry or isinstance(exc, (requests.Timeout, requests.ConnectionError))
                log_api_failure(
                    "scoresaber",
                    "_get_scoresaber_player_scores",
                    f"paged fetch failed url={url} scoresaber_id={scoresaber_id} params={params} "
                    f"attempt={attempt}/{_PAGE_FETCH_ATTEMPTS}",
                    exc,
                )
                if retry and attempt < _PAGE_FETCH_ATTEMPTS:
                    time.sleep(retry_backoff_seconds(attempt))
                    continue
                return None, False
            break
        items = data.get("playerScores") if isinstance(data, dict) else None
        if not isinstance(items, list):
            last_error["text"] = "invalid response"
            return None, False
        if page_meta["max_pages"] is None:
            meta = data.get("metadata") or {}
            try:
                total = int(meta.get("total", 0))
                per_page = int(meta.get("itemsPerPage", limit_param)) or limit_param
            except (TypeError, ValueError):
                total, per_page = 0, limit_param
            if total > 0:
                page_meta["total"] = total
                page_meta["max_pages"] = math.ceil(total / per_page)
        for it in items:
            lb_id = _ss_item_lb_id(it)
            if lb_id is not None:
                # APIからの最新データで上書き（再プレイ時に古いスコアを更新する）
                scores_by_lb_id[lb_id] = it
        page_items[page_no] = items
        if progress is not None:
            progress(page_no, page_meta["max_pages"])
        return items, False

    def _first_ts(items: list) -> Optional[str]:
        for it in items:
            ts = _ss_item_ts(it)
            if ts is not None:
                return ts
        return None

    def _last_ts(items: list) -> Optional[str]:
        return _first_ts(list(reversed(items)))

    def _is_last_page(page_no: int, items: list) -> bool:
        max_pages = page_meta["max_pages"]
        return len(items) < limit_param or (max_pages is not None and page_no >= max_pages)

    def _ends_range(page_no: int, items: list, stop_ts: Optional[str]) -> bool:
        """このページで stop_ts より古いスコアに届いたか、一覧が終わったか。"""
        if not items or _is_last_page(page_no, items):
            return True
        lt = _last_ts(items)
        return stop_ts is not None and lt is not None and lt < stop_ts

    def _read_forward(start: int, stop_ts: Optional[str]) -> tuple[bool, int, Optional[str]]:
        """start ページから、stop_ts より古いスコアに届くか一覧の終わりまで読む。

        (最後まで読めたか, 最後に読めたページ, 読めた中で一番古い timeSet)。
        """
        p = start
        last_ok = start - 1
        oldest: Optional[str] = None
        while True:
            items, not_found = _load_page(p)
            if items is None:
                # 404 は一覧の終わり
                return not_found, last_ok, oldest
            last_ok = p
            lt = _last_ts(items)
            if lt is not None:
                oldest = lt
            if _ends_range(p, items, stop_ts):
                return True, p, oldest
            if p >= _MAX_PAGES_GUARD:
                print(f"ScoreSaberのスコア取得: ページ数上限 {_MAX_PAGES_GUARD} に達したため打ち切ります")
                return False, p, oldest
            p += 1

    def _contiguous_end() -> int:
        """page 1 から続けて読めた最後のページ（読めていなければ 0）。"""
        p = 0
        while (p + 1) in page_items:
            p += 1
        return p

    def _recover_range(r: dict) -> tuple[bool, Optional[dict]]:
        """取りこぼした範囲を取り直す。(埋められたか, 残す範囲)。"""
        newer = r["newer_ts"]
        older = r["older_ts"]
        head_end = _contiguous_end()
        head_items = page_items.get(head_end) if head_end else None
        head_last = _last_ts(head_items) if head_items else None
        if head_items is not None and (not head_items or _is_last_page(head_end, head_items)):
            # page 1 から一覧の終わりまで読めている
            return True, None
        if head_last is not None and head_last <= newer:
            # page 1 からの読みが範囲の始まりより古い所まで届いている。続きから読む
            if head_last < older:
                return True, None
            p = head_end + 1
        else:
            above = sum(1 for v in scores_by_lb_id.values() if (_ss_item_ts(v) or "") > newer)
            p = above // limit_param + 1
            if page_meta["max_pages"]:
                p = min(p, int(page_meta["max_pages"]))
            p = max(p, head_end + 1)
            print(f"ScoreSaberスコア取得: 取りこぼし {r} を page {p} から取り直します")
            back_fetches = 0
            while True:
                already = p in page_items
                items, not_found = _load_page(p)
                if items is None:
                    if not_found and p > head_end + 1 and back_fetches < 5:
                        # 見積もりが一覧の終わりを越えた。前のページへ戻る
                        back_fetches += 1
                        p -= 1
                        continue
                    return False, r
                first = _first_ts(items)
                if p > head_end + 1 and (first is None or first < newer):
                    # このページは範囲の始まりより古い所から始まっている（キャッシュに API に無い古いスコアが
                    # 残っていると見積もりが後ろにずれる）。間にあるキャッシュの件数の分だけ戻る。
                    if not already:
                        back_fetches += 1
                    if back_fetches > 5:
                        print(f"ScoreSaberスコア取得: 範囲の始まり（timeSet {newer}）を含むページが見つかりません")
                        return False, r
                    if first is None:
                        step = 1
                    else:
                        between = sum(1 for v in scores_by_lb_id.values() if first <= (_ss_item_ts(v) or "") <= newer)
                        step = max(1, between // limit_param)
                    p = max(head_end + 1, p - step)
                    continue
                break
            if _ends_range(p, items, older):
                return True, None
            p += 1
        ok, _last_ok, oldest = _read_forward(p, older)
        if ok:
            return True, None
        # 読めたところまで範囲を狭めて残す
        if oldest is not None and older < oldest < newer:
            return False, {"newer_ts": oldest, "older_ts": older}
        return False, r

    # 1) page 1 から差分（または遡り取得の境界・初回は一覧の終わり）まで読む
    print(f"ScoreSaberスコア取得: page 1 から {main_stop or '一覧の終わり'} まで読みます")
    main_ok, main_last, main_oldest = _read_forward(1, main_stop)
    kept: list[dict] = []
    filled = 0
    if not main_ok:
        _mark_incomplete()
        cause = last_error["text"] or "unknown error"
        # 前回の範囲のうち、今回読めた所（先頭から main_oldest まで）より古い部分は残す
        for r in pending_ranges:
            if main_last >= 1 and main_oldest is not None:
                if r["older_ts"] >= main_oldest:
                    continue  # 今回読めた所に収まった
                if (r["newer_ts"] or _TOP_TS) > main_oldest:
                    r = {"newer_ts": main_oldest, "older_ts": r["older_ts"]}
            kept.append(r)
        # 今回読めなかった範囲：読めた一番古い所（page 1 から読めなければ先頭）から required_stop まで
        stop_ts = required_stop if required_stop is not None else _RESUME_FROM_BEGINNING
        if main_last >= 1 and main_oldest is not None:
            if main_oldest > stop_ts:
                kept.append({"newer_ts": main_oldest, "older_ts": stop_ts})
        else:
            # page 1 から読めなかった。次の回に page 1 から stop_ts まで読む。普段の差分取得でも残す：
            # 同じ Snapshot の PP の取り直し（sort=top）が最近のランク譜面のプレイをキャッシュに入れると、
            # 次の回のキャッシュの最新 timeSet が進み、その間のランク外のスコアを読まなくなるため。
            kept.append({"newer_ts": None, "older_ts": stop_ts})
        kept = _merge_resume_ranges(kept)
        if full_fetch and latest_cached_timeset is not None:
            next_step = "Fetch ALL stopped; select it again to refetch the remaining pages."
        elif kept:
            next_step = "the next snapshot will refetch only the unfetched range."
        else:
            next_step = "the next snapshot will fetch new scores as usual."
        _warn(
            f"ScoreSaber player scores: page {main_last + 1} could not be fetched ({cause}); "
            f"using existing cache for this snapshot; {next_step}"
        )
    else:
        # 1 回目の読みで届いた範囲は埋まった
        # （Fetch ALL は全部読んでいるので、取り直しの知らせは出さない）
        # 普段の差分取得と同じ範囲（page 1 から読めなかった回の印）は、埋めても知らせない
        filled = 0 if full_fetch else sum(
            1 for r in pending_ranges if r not in gap_ranges
            and not (r["newer_ts"] is None and latest_cached_timeset is not None
                     and r["older_ts"] >= latest_cached_timeset)
        )
        # 2) 前回の取りこぼしだけを取り直す（新しい範囲から）
        failed = False
        for r in gap_ranges:
            if failed:
                # 障害中とみて打ち切る。残りは次に回す。
                kept.append(r)
                continue
            ok, rest = _recover_range(r)
            if ok:
                filled += 1
                continue
            failed = True
            _mark_incomplete()
            if rest is not None:
                kept.append(rest)
            _warn(
                "ScoreSaber player scores: the unfetched range from the previous snapshot could not be fetched "
                f"({last_error['text'] or 'unknown error'}); the next snapshot will refetch only that range."
            )
        kept = _merge_resume_ranges(kept)

    ok_pages = sorted(p for p in set(requested_pages) if p in page_items)
    if requested_pages:
        print(f"ScoreSaberスコア取得: 読んだページ {_compact_pages(ok_pages) or 'なし'} / {page_meta['max_pages'] or '?'}")
    if filled:
        _warn(
            f"ScoreSaberスコア取得: 前回の取りこぼしを取り直しました（読んだページ {len(ok_pages)}/"
            f"{page_meta['max_pages'] or '?'}: {_compact_pages(ok_pages) or 'なし'}。"
            f"キャッシュ {len(scores_by_lb_id)} 件 / API {page_meta['total'] if page_meta['total'] is not None else '?'} 件）。"
        )
    if status is not None:
        status.setdefault("warnings", [])
        for w in warnings:
            if w not in status["warnings"]:
                status["warnings"].append(w)
        status["pages_read"] = ok_pages
        status["max_pages"] = page_meta["max_pages"]

    if scores_by_lb_id:
        # 1 ページも読めなかった回は、取りこぼしの印だけ書き足し、fetched_at は前の値のままにする
        keep_fetched_at = _load_cached_fetched_at(cache_path) if not ok_pages else None
        try:
            _save_cached_player_scores(cache_path, scores_by_lb_id, resume_ranges=kept, fetched_at=keep_fetched_at)
        except Exception:  # noqa: BLE001
            pass
    print(f"ScoreSaberのキャッシュ返却: {scoresaber_id} 件数: {len(scores_by_lb_id)}")
    return list(scores_by_lb_id.values())


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


# 一度に外す件数がこれを超えたら、API の異常（読み飛ばしなど）を疑って外さない。
# サーバー側でスコアが消えるのは普通は数件なので、小さい固定値にする。
_PRUNE_MAX = 20


def _ss_score_pp(item: dict) -> float:
    try:
        return float((item.get("score") or {}).get("pp") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _prune_missing_ranked_scores(scores: dict, fetched: dict, seen_ranked_ids: Optional[set] = None) -> int:
    """ランク譜面のスコアの取り直し（最後まで読めたもの）で返らなかったスコアを scores から外す。

    外すのは、キャッシュ上で leaderboard.ranked が真かつ score.pp > 0 で、ランク譜面キャッシュでもランクの譜面なのに、
    今回返ってこなかったものだけ（サーバー側で消えたもの）。ランク外・pp 0 のスコアは Clears・プレイ数・
    プレイリストの ✔ に使うので外さない。ランクから外れたばかりでキャッシュの ranked がまだ真のスコア
    （sort=top では pp 0 になって返らない）を外さないよう、ランク譜面キャッシュと突き合わせる。
    seen_ranked_ids（直前のランク譜面の全件取得で実際に一覧に出た譜面）があれば、さらにそれに絞る
    （ランク譜面キャッシュには、ランクから外れた譜面が残っていることがあるため）。
    ランク譜面キャッシュが無い・外す件数が多すぎるときは何も外さない。戻り値は外した件数。
    """
    if not fetched:
        return 0
    ranked = _normalize_leaderboard_cache(_load_cached_rank_maps(CACHE_DIR / "scoresaber_ranked_maps.json") or {})
    ranked_ids = {k for k, lb in ranked.items() if lb.get("ranked", True)}
    if seen_ranked_ids is not None:
        ranked_ids &= {str(x) for x in seen_ranked_ids}
    if not ranked_ids:
        print("ScoreSaber: ランク譜面キャッシュが無いため、返らなかったスコアは外しません")
        return 0
    cached_ranked = [
        k for k, item in scores.items()
        if isinstance(item, dict) and (item.get("leaderboard") or {}).get("ranked") and _ss_score_pp(item) > 0
    ]
    missing = [k for k in cached_ranked if k not in fetched and k in ranked_ids]
    if not missing:
        return 0
    if len(missing) > _PRUNE_MAX:
        print(f"ScoreSaber: 取り直しで返らなかったランク譜面のスコアが {len(missing)} 件と多すぎるため、外しません")
        return 0
    for k in missing:
        del scores[k]
    print(f"ScoreSaber: 取り直しで返らなかったランク譜面のスコア {len(missing)} 件をキャッシュから外しました")
    return len(missing)


def refresh_scoresaber_ranked_scores(
    scoresaber_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    status: Optional[dict] = None,
    prune_missing: bool = True,
    ranked_play_count: Optional[int] = None,
) -> int:
    """ランク譜面のスコアだけを pp の高い順（sort=top）に取り直し、スコアキャッシュに上書きする。

    pp が変わるのはランク譜面のスコアだけなので、全スコア（200 ページ以上）ではなく
    pp の付いたスコア（約 60 ページ）だけ読む。pp が 0 のスコアが出てきたら止める。
    途中で失敗したら status["complete"] = False を入れ、それまでに取れた分だけ反映する。
    最後まで読めたときだけ、今回返ってこなかったランク譜面の pp > 0 のスコア（サーバー側で消えたもの）を
    キャッシュから外す（prune_missing=False なら外さない。条件は _prune_missing_ranked_scores）。
    ranked_play_count は同じ Snapshot で取った /full の rankedPlayCount。読んだ pp > 0 のスコアの件数と合うときだけ外す
    （取得中にページがずれて正常なスコアを読み飛ばしたときは件数が足りない）。None（取れていない）なら外さない。
    戻り値は取り直したスコアの件数。
    """
    # 呼ぶ前に別の取得（ランク譜面の取り直しなど）が失敗していたら、ランク譜面キャッシュが古いかもしれないので外さない。
    # ただし、ランク譜面の一覧を最後まで読めて列挙数が総数に届かなかっただけ（null 日付の並びが不安定）なら失敗ではない。
    prior_incomplete = (
        status is not None and status.get("complete") is False and not status.get("ss_ranked_listing_short")
    )
    seen_ranked_ids = status.get("ss_ranked_seen_ids") if status is not None else None
    cache_path = CACHE_DIR / f"scoresaber_player_scores_{scoresaber_id}.json"
    url = SCORESABER_PLAYER_SCORES_URL.format(player_id=scoresaber_id)
    fetched: dict[str, dict] = {}
    page = 1
    max_pages: Optional[int] = None
    complete = True
    # 一覧の終わりまで読めたか（pp 0 のスコアに当たった／100 件未満のページで終わった）。
    # 404・空ページ・ページ上限で抜けたときは、残りを読み飛ばしたかもしれないので外さない。
    natural_end = False
    pp_items_read = 0  # 読んだ pp > 0 のスコアの件数（重複を含む）。fetched と合わなければ並びが動いた
    while True:
        if progress is not None:
            progress(page, max_pages)
        params = {"limit": "100", "sort": "top", "page": str(page)}
        try:
            resp = session.get(url, params=params, timeout=15)
            if resp.status_code == 404:
                break
            if _is_rate_limited(resp, "refresh_scoresaber_ranked_scores", f"player={scoresaber_id} page={page}"):
                complete = False
                break
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log_api_failure(
                "scoresaber",
                "refresh_scoresaber_ranked_scores",
                f"request failed url={url} scoresaber_id={scoresaber_id} params={params}",
                exc,
            )
            complete = False
            break
        items = data.get("playerScores") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            break
        reached_unranked = False
        for item in items:
            score = item.get("score") if isinstance(item, dict) else None
            leaderboard = item.get("leaderboard") if isinstance(item, dict) else None
            if not isinstance(score, dict) or not isinstance(leaderboard, dict):
                continue
            try:
                pp_val = float(score.get("pp") or 0)
            except (TypeError, ValueError):
                pp_val = 0.0
            if pp_val <= 0:
                # pp の高い順なので、ここから先は pp の付かないスコアだけ
                reached_unranked = True
                break
            lb_id = leaderboard.get("id")
            pp_items_read += 1
            if lb_id is not None:
                fetched[str(lb_id)] = item
        if reached_unranked or len(items) < 100:
            natural_end = True
            break
        if page >= _MAX_PAGES_GUARD:
            # ページ上限で打ち切った。続きがあるかもしれないので、返らなかったスコアは外さない
            break
        page += 1

    if not complete and status is not None:
        status["complete"] = False
    if status is not None:
        # 一覧の終わりまで読めたか（404・空ページ・ページ上限で抜けたときは False）。新ランク譜面の検知済みの印に使う
        status["ranked_scores_read_to_end"] = bool(complete and natural_end)
        # 今回 API から読めたスコアの leaderboard id（★同期で、キャッシュに元からあるスコアと区別するため）
        status["refreshed_ids"] = sorted(fetched)
    if not fetched:
        return 0
    scores = _load_cached_player_scores(cache_path) or {}
    scores.update(fetched)
    if prune_missing:
        if not complete or not natural_end or prior_incomplete:
            print("ScoreSaber: 取り直しを最後まで終えられなかったため、返らなかったスコアは外しません")
        elif pp_items_read != len(fetched):
            # 読んでいる途中で pp の並びが動いた（再プレイ・再計算）。動いた分を読み飛ばしたかもしれない
            print("ScoreSaber: 取り直しの途中で並びが動いた（重複あり）ため、返らなかったスコアは外しません")
        elif ranked_play_count is None or ranked_play_count != len(fetched):
            # 読み飛ばし（ページのずれ）があると件数が合わない。プロフィールの件数が無いときも確かめられないので外さない
            print(
                f"ScoreSaber: 読んだ pp 付きスコア {len(fetched)} 件がプロフィールの rankedPlayCount "
                f"（{ranked_play_count}）と合わないため、返らなかったスコアは外しません"
            )
        else:
            _prune_missing_ranked_scores(scores, fetched, seen_ranked_ids)
    # 差分取得の途中失敗の印（resume_ranges / resume_stop_ts）は残す。fetched_at（60 日遡りの基準）も変えない
    # （ランク譜面のスコアしか読んでいないので、差分取得を済ませたことにはならない）
    _save_cached_player_scores(
        cache_path, scores, fetched_at=_load_cached_fetched_at(cache_path), **_load_resume_fields(cache_path),
    )
    return len(fetched)


def refresh_scores_of_newly_ranked_maps(
    scoresaber_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    status: Optional[dict] = None,
) -> int:
    """新しくランクになった譜面に自分の古いプレイがあるとき、そのスコアの pp を取り直す。

    スコアの差分取得は timeSet（プレイした日時）で打ち切るので、ランクになる前の古いプレイは拾えない。
    pp が 0 のまま PP のずれ（reconcile）が働くまで残ってしまう。
    検知は API を使わずキャッシュ同士で行う：スコアキャッシュ上で ranked でない譜面が、ランク譜面キャッシュではランクになっている。
    見つかったときだけ、ランク譜面のスコア（sort=top）を取り直す。
    取り直しても更新されなかった譜面（ランクから外れた譜面がキャッシュに残っている・pp が 0 のスコアなど）は、
    スコアの譜面情報は書き換えず、「検知済み」の印（newly_ranked_checked.json）だけ残して毎回の再検知を防ぐ。
    印はランク譜面キャッシュ側のランクになった日時ごとなので、ランクになり直したら再び検知する。
    戻り値は検知した譜面の数。
    """
    cache_path = CACHE_DIR / f"scoresaber_player_scores_{scoresaber_id}.json"
    checked_path = CACHE_DIR / "newly_ranked_checked.json"
    ranked = _normalize_leaderboard_cache(_load_cached_rank_maps(CACHE_DIR / "scoresaber_ranked_maps.json") or {})
    scores = _load_cached_player_scores(cache_path) or {}
    checked = load_newly_ranked_checked(checked_path, "scoresaber", scoresaber_id)

    def _mark(k: str) -> str:
        return str(ranked[k].get("rankedDate") or "")

    def _stale() -> list[str]:
        # ランク譜面キャッシュの側も「ランク」のものだけ（掃除が遅れてランク外れが残っていても書き換えない）
        return [
            k for k, item in scores.items()
            if k in ranked and ranked[k].get("ranked", True) and isinstance(item, dict)
            and not (item.get("leaderboard") or {}).get("ranked")
            and checked.get(k) != _mark(k)
        ]

    def _star_entries():
        for k, item in scores.items():
            if k not in ranked or not isinstance(item, dict):
                continue
            ts = (item.get("score") or {}).get("timeSet")
            try:
                played = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if played.tzinfo is None:
                    played = played.replace(tzinfo=timezone.utc)
            except ValueError:
                played = None
            yield (item.get("leaderboard") or {}).get("stars"), ranked[k].get("stars"), played

    # 窓の外で★が変わった譜面を、最近のプレイのスコアから検知する（次回の全件取り直しを前倒し）
    if flag_star_drift_if_any(_RANKED_FULL_TRAVERSAL_STATE, "scoresaber", _star_entries()):
        print("ScoreSaber: ランク譜面キャッシュの★が最新のスコアと食い違うため、次回全件取り直します")

    stale = _stale()
    if not stale:
        return 0
    print(f"ScoreSaber: 新しくランクになった譜面に自分のスコアがある（{len(stale)} 件）。ランク譜面のスコアを取り直します")
    local_status: dict = {}
    # ここでは外さない：目的は新しくランクになった譜面の pp の取得で、直前のランク譜面の取得は差分だけなので
    # キャッシュにランクから外れた譜面が残っていることがある（外すのは PP のずれの取り直しのときだけ）
    refresh_scoresaber_ranked_scores(
        scoresaber_id, session, progress=progress, status=local_status, prune_missing=False,
    )
    if local_status.get("complete") is False:
        # 途中で失敗。差し替えず、次回また検知して取り直す
        if status is not None:
            status["complete"] = False
        return len(stale)
    if not local_status.get("ranked_scores_read_to_end"):
        # 404・空ページ・ページ上限で途中までしか読めていない。読んでいない譜面に印を付けない（次回また検知する）
        return len(stale)
    scores = _load_cached_player_scores(cache_path) or {}
    still = _stale()
    if still:
        # スコアは書き換えず、検知済みの印だけ残す（ランク譜面キャッシュに無くなった譜面の印は捨てる）
        marks = {k: v for k, v in checked.items() if k in ranked}
        marks.update({k: _mark(k) for k in still})
        save_newly_ranked_checked(checked_path, "scoresaber", scoresaber_id, marks)
    return len(stale)


#: 取り直しても★の食い違いが残った譜面の印（同じ食い違いで毎回取り直さないため）。BL と同じファイル。
_STAR_SYNC_CHECKED = CACHE_DIR / "star_sync_checked.json"
#: ScoreSaber の search は 32 文字まで（2026-11-25、33 文字以上で 400 "Too big: expected string to have <=32 characters"）。
_SS_SEARCH_MAX_LEN = 32
#: 1 曲の検索で読むページの上限（曲名が短く、他の曲も多く当たるとき）。
_SS_SEARCH_MAX_PAGES = 3


def _ss_star_sync_targets(scores: dict, ranked: dict[str, dict]) -> dict[str, tuple[str, str]]:
    """スコアキャッシュの★とランク譜面キャッシュの★が違うランクのスコアを返す（API は使わない）。

    戻り値は {leaderboard id: (検索に使う曲名, 食い違いの印)}。ランク譜面キャッシュの★は、wiki の月例リウェイト
    （ss_reweights）・60 日の窓の差分取得・全件取り直しで新しくなる。スコアキャッシュの★と pp はプレイし直すか
    PP のずれの取り直しまで古いままなので、★が違うスコアは pp も古いとみなす。
    """
    targets: dict[str, tuple[str, str]] = {}
    for k, item in scores.items():
        if k not in ranked or not isinstance(item, dict) or not ranked[k].get("ranked", True):
            continue
        lb = item.get("leaderboard") or {}
        if not lb.get("ranked"):
            continue  # ランクになる前のスコアは refresh_scores_of_newly_ranked_maps が扱う
        if stars_differ(lb.get("stars"), ranked[k].get("stars")):
            name = str(lb.get("songName") or "")[:_SS_SEARCH_MAX_LEN].strip()
            targets[k] = (name, f"stars {lb.get('stars')}->{ranked[k].get('stars')}")
    return targets


def sync_changed_ranked_scores(
    scoresaber_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    status: Optional[dict] = None,
) -> dict:
    """ランク譜面キャッシュの★が変わった譜面だけ、自分のスコア（pp・★）を取り直す。

    PP のずれの取り直し（collector の reconcile）は合計 PP が 0.1 以上ずれたときしか動かないので、重みの小さい
    古いスコアのリウェイトは拾えない。ここではキャッシュ同士の比較（API なし）で★が食い違うスコアを見つけ、
    曲名で /player/{id}/scores?search=<曲名>&sort=top を引いて取り直す（曲の数だけのリクエスト）。
    曲の数がランクのスコアのページ数（約 57）を超えるときは、refresh_scoresaber_ranked_scores（全ページ）を使う。
    取り直したスコアの★がランク譜面キャッシュと違えば（ランク譜面キャッシュが古い）、その譜面の★・maxPP を書き換える。
    取り直しても食い違いが残った譜面は star_sync_checked.json に印を残し、同じ食い違いでは取り直さない。
    検索しても目当ての譜面が返らなかったときは確認済みにせず、待ち時間（ranked_sync.star_sync_skip）の後に探し直す。
    戻り値は結果のまとめ（ログ・テスト用）。
    """
    summary: dict = {"targets": 0, "songs": 0, "requests": 0, "mode": "none", "scores_updated": 0, "maps_updated": 0}
    cache_path = CACHE_DIR / f"scoresaber_player_scores_{scoresaber_id}.json"
    ranked_path = CACHE_DIR / "scoresaber_ranked_maps.json"
    scores = _load_cached_player_scores(cache_path)
    ranked = _load_cached_rank_maps(ranked_path)
    if not scores or not ranked:
        summary["mode"] = "no_cache"
        return summary
    checked = load_newly_ranked_checked(_STAR_SYNC_CHECKED, "scoresaber", scoresaber_id)
    found = _ss_star_sync_targets(scores, ranked)
    targets = {k: v for k, v in found.items() if not star_sync_skip(checked.get(k), v[1])}
    summary["targets"] = len(targets)
    if not targets:
        print("ScoreSaber ★同期: スコアとランク譜面の★は一致（取り直しなし）")
        if set(checked) - set(found):
            save_newly_ranked_checked(
                _STAR_SYNC_CHECKED, "scoresaber", scoresaber_id, {k: v for k, v in checked.items() if k in found},
            )
        return summary
    for k, (_, why) in list(targets.items())[:10]:
        print(f"ScoreSaber ★同期: {k} {why}")
    names = sorted({n for n, _ in targets.values() if n})
    summary["songs"] = len(names)
    ranked_count = sum(
        1 for item in scores.values()
        if isinstance(item, dict) and (item.get("leaderboard") or {}).get("ranked") and _ss_score_pp(item) > 0
    )
    bulk_pages = max(1, math.ceil(ranked_count / 100))
    refreshed: dict[str, dict] = {}
    # 検索（取り直し）を最後まで終えた対象。ここに入っていて refreshed に無い対象は「探しても返らなかった」
    searched: set[str] = set()
    complete = True
    if len(names) > bulk_pages:
        print(
            f"ScoreSaber ★同期: 食い違い {len(targets)} 譜面（{len(names)} 曲）が多いため、"
            f"ランク譜面のスコアを全部取り直します（約 {bulk_pages} ページ）"
        )
        summary["mode"] = "bulk"
        local_status: dict = {}
        refresh_scoresaber_ranked_scores(
            scoresaber_id, session, progress=progress, status=local_status, prune_missing=False,
        )
        summary["requests"] += bulk_pages
        complete = local_status.get("complete") is not False
        scores = _load_cached_player_scores(cache_path) or {}
        # 今回 API から読めたスコアだけ（途中で止まったとき、キャッシュに元からある古いスコアを「取り直した」としない）
        read_ids = {str(x) for x in local_status.get("refreshed_ids") or []}
        refreshed = {k: scores[k] for k in targets if k in scores and k in read_ids}
        if complete:
            # 一覧の終わりまで読めなかった回（総数に届く前の空ページ・ページ上限）も「探した」に入れる。
            # 返らなかった対象には「見つからなかった」の印（待ち時間の後にもう一度探す）を付け、
            # 毎回の Snapshot で全部の取り直しを繰り返さない
            searched = set(targets)
    else:
        print(f"ScoreSaber ★同期: 食い違い {len(targets)} 譜面（{len(names)} 曲）。曲ごとに取り直します")
        summary["mode"] = "per_song"
        url = SCORESABER_PLAYER_SCORES_URL.format(player_id=scoresaber_id)
        rate_limited = False
        for i, name in enumerate(names, start=1):
            if rate_limited:
                break  # 429 を受けたら残りの曲は次回に回す（印は付けない）
            if progress is not None:
                progress(i, len(names))
            wanted = {k for k, (n, _) in targets.items() if n == name}
            song_ok = True
            for page in range(1, _SS_SEARCH_MAX_PAGES + 1):
                params = {"search": name, "sort": "top", "limit": "100", "page": str(page)}
                try:
                    resp = session.get(url, params=params, timeout=15)
                    summary["requests"] += 1
                    if resp.status_code == 404:
                        break
                    if _is_rate_limited(resp, "sync_changed_ranked_scores", f"player={scoresaber_id} search={name}"):
                        complete = False
                        rate_limited = True
                        song_ok = False
                        break
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:  # noqa: BLE001
                    if isinstance(exc, RuntimeError) and str(exc) == "SNAPSHOT_CANCELLED":
                        raise
                    log_api_failure(
                        "scoresaber", "sync_changed_ranked_scores",
                        f"request failed url={url} scoresaber_id={scoresaber_id} params={params}", exc,
                    )
                    complete = False
                    song_ok = False
                    break
                items = data.get("playerScores") if isinstance(data, dict) else None
                if not isinstance(items, list) or not items:
                    break
                for item in items:
                    lb_id = _ss_item_lb_id(item)
                    # 今の値なので、キャッシュにある譜面は入れる（同じ曲の他の難易度など）
                    if lb_id is not None and (lb_id in wanted or lb_id in scores):
                        refreshed[lb_id] = item
                if wanted <= set(refreshed) or len(items) < 100:
                    break
            if song_ok:
                searched |= wanted
        if refreshed:
            scores.update(refreshed)
            _resave_player_scores(cache_path, scores)
    summary["scores_updated"] = len(refreshed)

    # 取り直したスコアの★がランク譜面キャッシュと違う＝ランク譜面キャッシュが古い。その譜面だけ書き換える
    maps_updated = 0
    raw: dict = {}
    try:
        raw = json.loads(ranked_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        raw = {}
    raw_lbs = raw.get("leaderboards") if isinstance(raw.get("leaderboards"), dict) else None
    # refreshed は今回 API から読めたスコアだけ（全部の取り直しが途中で止まったときも、キャッシュに元からある古いスコアは
    # read_ids で除いてある）。読めた譜面は今の★で書き換える（書き換えずに確認済みの印を付けると、★が古いまま固まる）
    for lb_id, item in refreshed.items():
        new_lb = item.get("leaderboard") or {}
        if lb_id not in ranked or not new_lb.get("ranked") or not stars_differ(ranked[lb_id].get("stars"), new_lb.get("stars")):
            continue
        for target in (ranked[lb_id], (raw_lbs or {}).get(lb_id)):
            if isinstance(target, dict):
                target["stars"] = new_lb.get("stars")
                if new_lb.get("maxPP") is not None:
                    target["maxPP"] = new_lb.get("maxPP")
        maps_updated += 1
    summary["maps_updated"] = maps_updated
    if maps_updated and raw_lbs is not None:
        try:
            # fetched_at は変えない（差分取得の 60 日の窓を進めない）
            atomic_write_text(ranked_path, json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"ScoreSaber ★同期: ランク譜面キャッシュの★を {maps_updated} 譜面書き換えました")
        except Exception as exc:  # noqa: BLE001
            print(f"ScoreSaber ★同期: ランク譜面キャッシュを保存できませんでした（{exc}）")

    remaining = _ss_star_sync_targets(scores, ranked)
    marks = {k: v for k, v in checked.items() if k in remaining}
    not_found = 0
    for k, (_, why) in remaining.items():
        if k not in targets:
            continue
        if k in refreshed:
            # 取り直しても食い違いが残った（確認済み）。同じ食い違いでは取り直さない
            marks[k] = why
        elif k in searched:
            # 探しても返らなかった。確認済みにはせず、待ち時間の後にもう一度探す
            marks[k] = star_sync_not_found_mark(checked.get(k), why)
            not_found += 1
        # 取り直しが失敗・429 で止まった譜面は印を変えない（次の回に取り直す）
    if not_found:
        print(f"ScoreSaber ★同期: 検索しても返らなかった譜面 {not_found} 件は、時間を置いて探し直します")
    save_newly_ranked_checked(_STAR_SYNC_CHECKED, "scoresaber", scoresaber_id, marks)
    print(
        f"ScoreSaber ★同期: スコア {len(refreshed)} 件・ランク譜面 {maps_updated} 件を更新"
        f"（リクエスト {summary['requests']} 回、残り {len(remaining)} 件）"
    )
    if not complete and status is not None:
        status["complete"] = False
    return summary


def _resave_player_scores(cache_path: Path, scores: dict) -> None:
    """スコアキャッシュを保存し直す（差分取得の途中失敗の印 resume_ranges / resume_stop_ts と、
    60 日遡りの基準の fetched_at は残す。一部のスコアを取り直しただけで、差分取得を済ませたことにはならないため）。"""
    _save_cached_player_scores(
        cache_path, scores, fetched_at=_load_cached_fetched_at(cache_path), **_load_resume_fields(cache_path),
    )


def _get_scoresaber_player_stats(scoresaber_id: str, session: requests.Session) -> dict:
    """
    ScoreSaberのプレイヤー統計情報(playerStats/scoreStats)を取得。
    失敗時は空dict。
    """
    print("Entering _get_scoresaber_player_stats")
    url = SCORESABER_PLAYER_FULL_URL.format(player_id=scoresaber_id)
    try:
        resp = get_memoized(session, url, timeout=10)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
    except Exception as exc:
        log_api_failure("scoresaber", "_get_scoresaber_player_stats", f"request failed url={url} scoresaber_id={scoresaber_id}", exc)
        return {}

    try:
        data = resp.json()
    except Exception as exc:
        log_api_failure("scoresaber", "_get_scoresaber_player_stats", f"invalid json url={url} scoresaber_id={scoresaber_id}", exc)
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
def _load_cached_player_scores(path: Path) -> Optional[dict]:
    """共通のプレイヤースコアキャッシュローダー。

    フォーマット: {"fetched_at": str, "total_play_count": int, "scores": {score_id: score_dict, ...}}
    という形を想定し、scores dict を返す。
    壊れたファイルや形式違いの場合は None を返して再取得させる。
    """

    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        scores = raw.get("scores")
        if isinstance(scores, dict):
            return scores
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
        resp = get_memoized(session, url, timeout=10)
        print(f"Fetching ScoreSaber player info... URL: {resp.url}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log_api_failure("scoresaber", "_fetch_scoresaber_player_basic", f"request failed url={url} scoresaber_id={scoresaber_id}", exc)
        return None

    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        log_api_failure("scoresaber", "_fetch_scoresaber_player_basic", f"invalid json url={url} scoresaber_id={scoresaber_id}", exc)
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

    leaderboards: dict = _get_scoresaber_leaderboards_ranked(session)
    print(f"取得した Ranked リーダーボード件数: {len(leaderboards)}")
    scores = _get_scoresaber_player_scores(scoresaber_id, session)
    print(f"取得したスコア件数: {len(scores)}")
    return _build_scoresaber_star_stats(leaderboards, scores)

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