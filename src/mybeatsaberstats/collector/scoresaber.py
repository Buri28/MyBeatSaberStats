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

from ..api_error_log import log_api_failure
from ..fileio import atomic_write_text, mark_now, recently_marked
from ..modifiers import classify_play
from ..http_client import get_memoized

CACHE_DIR = BASE_DIR / "cache"

SCORESABER_LEADERBOARDS_URL = "https://scoresaber.com/api/leaderboards"
SCORESABER_PLAYER_SCORES_URL = "https://scoresaber.com/api/player/{player_id}/scores"
# ランク譜面の全件取り直しをした時刻（「キャッシュ > API の総数」での全件取得を 24 時間に 1 回までにする）
_RANKED_FULL_TRAVERSAL_STATE = CACHE_DIR / "ranked_full_traversal.json"
_RANKED_FULL_TRAVERSAL_INTERVAL_HOURS = 24
SCORESABER_PLAYER_FULL_URL = "https://scoresaber.com/api/player/{player_id}/full"

#: 無限ページングを防ぐための保険。
#: 正常系を途中で打ち切って取りこぼさないよう、実データより十分大きく取る。
#: 1 ページ 100 件なので 1000 ページ = 10 万件相当。ranked 譜面（数千件）も
#: 重量級プレイヤーのスコア数もこれを下回るため、到達するのは API 異常時だけ。
_MAX_PAGES_GUARD = 1000


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


def _save_cached_player_scores(path: Path, scores: dict, resume_stop_ts: Optional[str] = None) -> None:
    """
    ページリストをキャッシュファイル(JSON)として保存する。

    resume_stop_ts: 差分取得が途中で失敗したとき、次回はこの timeSet まで遡って取り直す。
    """
    print("Entering _save_cached_player_scores")
    # キーで降順ソート（leaderboard_id ベース）
    scores = dict(sorted(scores.items(), key=lambda item: str(item[0]), reverse=True))
    payload = {
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "total_play_count": len(scores),
        "scores": scores,
    }
    if resume_stop_ts:
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
        fetch_until_str = fetch_until.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        print(f"ScoreSaber Ranked Maps fetch_until 指定: {fetch_until_str}")

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
                    break
                for lb in lbs_dt:
                    cd = lb.get("rankedDate") or lb.get("createdDate") or lb.get("created_date") or ""
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
                    break
                meta_dt = data_dt.get("metadata") or {}
                try:
                    per_page_dt = int(meta_dt.get("itemsPerPage", 100)) or 100
                except (TypeError, ValueError):
                    per_page_dt = 100
                if len(lbs_dt) < per_page_dt:
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
        if full_traversal:
            mark_now(_RANKED_FULL_TRAVERSAL_STATE, "scoresaber")
            if run_complete and seen_ids and api_total is not None and len(seen_ids) >= api_total:
                # API の総数まで全部取れたので、見つからなかった譜面（ランクから外れたもの）を消す
                stale = [lb_id for lb_id in leaderboards if lb_id not in seen_ids]
                for lb_id in stale:
                    del leaderboards[lb_id]
                removed = len(stale)
            elif run_complete:
                # 途中で止まった（404・空ページなど）。消すと残りの譜面まで消えるので消さない
                print(f"SS Ranked Maps: 全件取得が途中で止まりました（{len(seen_ids)}/{api_total} 件）。古い譜面は消しません")
                run_complete = False
                if status is not None:
                    status["complete"] = False
        elif (
            run_complete and api_total is not None and len(leaderboards) > api_total
            and not recently_marked(_RANKED_FULL_TRAVERSAL_STATE, "scoresaber", _RANKED_FULL_TRAVERSAL_INTERVAL_HOURS)
        ):
            # キャッシュの方が多い＝ランクから外れた譜面が残っている。全件取り直して消す。
            # （全件取得からは呼ばないので再帰が続かない。API の総数が食い違っていても 24 時間に 1 回まで）
            print(
                f"SS Ranked Maps: キャッシュ {len(leaderboards)} 件 > API {api_total} 件のため、全件取り直します"
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


def _get_scoresaber_player_scores(
    scoresaber_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    fetch_until: Optional[datetime] = None,
    status: Optional[dict] = None,
) -> list[dict]:
    """ScoreSaber のプレイヤースコアをキャッシュ付きで全件取得する。

    progress が与えられた場合、現在のページ番号と推定最大ページ数を通知する。

    fetch_until が指定された場合、その日時まで遡ってスコアを取得する（ギャップ補完用）。
    None の場合はキャッシュ最新 timeSet に達した時点で取得を打ち切る（通常動作）。
    """

    # cache_path = CACHE_DIR / f"scoresaber_player_scores_{scoresaber_id}.json"
    cache_path = CACHE_DIR / f"scoresaber_player_scores_{scoresaber_id}.json"
    # leaderboard_id をキーにしたDictionaryで管理（同一譜面の再プレイ時に最新スコアで上書きされるよう）
    scores_by_lb_id: dict[str, dict] = {}

    print(f"ScoreSaberのキャッシュ読み込み: {scoresaber_id}")
    
    cached_pages = None
    cached_scores = None
    resume_stop_ts: Optional[str] = None
    if Path.exists(cache_path):
        cached_scores = _load_cached_player_scores(cache_path)
        try:
            _raw_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            _resume = _raw_cache.get("resume_stop_ts") if isinstance(_raw_cache, dict) else None
            resume_stop_ts = _resume if isinstance(_resume, str) and _resume else None
        except Exception:  # noqa: BLE001
            resume_stop_ts = None

    # 途中で失敗したかどうか。失敗したら resume_stop_ts を残して、次回そこまで遡る。
    run_complete = True

    def _mark_incomplete() -> None:
        nonlocal run_complete
        run_complete = False
        if status is not None:
            status["complete"] = False

    # キャッシュから最新のtimeSetを取得（差分取得用）
    latest_cached_timeset: Optional[str] = None
    
    if cached_scores is not None:
        print(f"ScoreSaberのキャッシュ読み込み成功: {scoresaber_id} スコア件数: {len(cached_scores)}")
        for it in cached_scores.values():
            lb = it.get("leaderboard") if isinstance(it, dict) else None
            if isinstance(lb, dict):
                lb_id_raw = lb.get("id")
            else:
                lb_id_raw = None
            if lb_id_raw is None:
                # 旧形式（score_idキー）の場合もロードする
                scoreDict = it.get("score")
                if isinstance(scoreDict, dict):
                    lb_id_raw = scoreDict.get("leaderboardId")
            if lb_id_raw is None:
                continue
            lb_id = str(lb_id_raw)
            scores_by_lb_id[lb_id] = it

            scoreDict = it.get("score")
            if not isinstance(scoreDict, dict):
                continue
            # 最新のtimeSetを記録
            timeset = scoreDict.get("timeSet")
            if timeset and (latest_cached_timeset is None or timeset > latest_cached_timeset):
                latest_cached_timeset = timeset

    print(f"ScoreSaberのキャッシュ読み込み完了: {scoresaber_id} 件数: {len(scores_by_lb_id)} 最新timeSet: {latest_cached_timeset}")

    # 有効な停止基準を決定する
    # fetch_until が指定されている場合はその日時を優先し、キャッシュの timeSet を無視して遡る
    fetch_until_str: Optional[str] = None
    if fetch_until is not None:
        fetch_until_str = fetch_until.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        print(f"ScoreSaber fetch_until 指定: {fetch_until_str}")

    effective_stop_ts: Optional[str] = fetch_until_str if fetch_until_str is not None else latest_cached_timeset
    # 前回の差分取得が途中で失敗していたら、そのとき目指していた所まで遡る。
    # キャッシュの最新 timeSet だけを基準にすると、途中のページの抜けが二度と埋まらない。
    if resume_stop_ts and (effective_stop_ts is None or resume_stop_ts < effective_stop_ts):
        print(f"ScoreSaber: 前回の取得が途中で失敗したため {resume_stop_ts} まで遡ります")
        effective_stop_ts = resume_stop_ts

    append_pages: list[dict] = []
    limit_param = 100
    latest_total = 0
    reached_cached_in_first_page = False  # 最初のページで既に停止基準に到達したか
    
    try:
        url = SCORESABER_PLAYER_SCORES_URL.format(player_id=scoresaber_id)
        params_check = {"limit": str(limit_param), "sort": "recent", "page": "1"}
        resp = session.get(url, params=params_check, timeout=10)
        print(f"最新スコアチェック... URL: {resp.url} params: {params_check}")
        if resp.status_code != 404:
            resp.raise_for_status()
            data = resp.json()
            items = data.get("playerScores") 
            # metadataからtotalを取得
            metadata = data.get("metadata") 
            latest_total = metadata.get("total")
            print(f"最新スコアの総譜面数: {latest_total} for {scoresaber_id}")
            append_pages.append({"page": 1, "params": params_check, "data": data})

            for item in items:
                scoreDict = item.get("score")
                if not isinstance(scoreDict, dict):
                    continue
                
                # 停止基準（fetch_until またはキャッシュ最新timeSet）に到達したかチェック
                timeset = scoreDict.get("timeSet")
                if effective_stop_ts and timeset and timeset < effective_stop_ts:
                    print(f"ScoreSaberの最初のページで停止基準 {effective_stop_ts} に到達しました。")
                    reached_cached_in_first_page = True
                    break
                
                lb = item.get("leaderboard") or {}
                lb_id_raw = lb.get("id") if isinstance(lb, dict) else None
                if lb_id_raw is None:
                    continue
                lb_id = str(lb_id_raw)
                # APIからの最新データで上書き（再プレイ時に古いスコアを更新する）
                scores_by_lb_id[lb_id] = item
    except Exception as exc:  # noqa: BLE001
        log_api_failure(
            "scoresaber",
            "_get_scoresaber_player_scores",
            f"freshness check failed scoresaber_id={scoresaber_id}",
            exc,
        )
        _mark_incomplete()
        # If anything goes wrong during the freshness check, fall back to cached data
        pass

    if progress is not None:
        # キャッシュ読み込み時は 1 ページのみとして通知
        progress(1, 1)
    
    # 最初のページでキャッシュに到達していれば、それ以降の取得はスキップ
    if reached_cached_in_first_page:
        print(f"ScoreSaberの差分取得完了: 最初のページでキャッシュに到達しました。{scoresaber_id}")
        if scores_by_lb_id:
            try:
                _save_cached_player_scores(
                    cache_path, scores_by_lb_id,
                    resume_stop_ts=None if run_complete else (effective_stop_ts or _RESUME_FROM_BEGINNING),
                )
            except Exception:  # noqa: BLE001
                pass
        return list(scores_by_lb_id.values())
    
    page = 2  # ページ1は事前チェック済みのためページ2から開始
    max_pages_sc: int | None = None
    reached_cached_timeset = False  # キャッシュ済みtimeSetに到達したかのフラグ

    while True:
        url = SCORESABER_PLAYER_SCORES_URL.format(player_id=scoresaber_id)
        params = {
            "limit": str(limit_param),
            "sort": "recent",
            "page": str(page),
        }
        print(f"ScoreSaberのキャッシュ取得API呼び出し: {scoresaber_id} ページ: {page}")
        try:
            resp = session.get(url, params=params, timeout=10)
            print(f"Fetching ScoreSaber player scores page {page} for star stats... URL: {resp.url} params: {params}")
            if resp.status_code == 404:
                break
            if _is_rate_limited(resp, "_get_scoresaber_player_scores", f"player={scoresaber_id} page={page}"):
                _mark_incomplete()
                break
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log_api_failure(
                "scoresaber",
                "_get_scoresaber_player_scores",
                f"paged fetch failed url={url} scoresaber_id={scoresaber_id} params={params}",
                exc,
            )
            _mark_incomplete()
            break

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log_api_failure(
                "scoresaber",
                "_get_scoresaber_player_scores",
                f"invalid json url={resp.url} scoresaber_id={scoresaber_id} params={params}",
                exc,
            )
            _mark_incomplete()
            break

        append_pages.append({"page": page, "params": params, "data": data})

        items = data.get("playerScores")
        if isinstance(items, list):
            for it in items:
                scoreDict = it.get("score")
                if not isinstance(scoreDict, dict):
                    continue
                
                # 停止基準（fetch_until またはキャッシュ最新timeSet）に到達したら差分取得完了
                timeset = scoreDict.get("timeSet")
                if effective_stop_ts and timeset and timeset < effective_stop_ts:
                    print(f"ScoreSaberの差分取得完了: 停止基準 {effective_stop_ts} に到達しました。ページ: {page}")
                    reached_cached_timeset = True
                    break
                
                lb = it.get("leaderboard") or {}
                lb_id_raw = lb.get("id") if isinstance(lb, dict) else None
                if lb_id_raw is None:
                    continue
                lb_id = str(lb_id_raw)
                # APIからの最新データで上書き（再プレイ時に古いスコアを更新する）
                scores_by_lb_id[lb_id] = it
        
        print(f"ScoreSaberのキャッシュ取得中: {scoresaber_id} ページ: {page} 最新譜面数: {latest_total}  取得件数: {len(items)} 総件数: {len(scores_by_lb_id)}")

        # ページ進捗をコールバックで通知
        if progress is not None:
            progress(page, max_pages_sc)
        
        # キャッシュ済みtimeSetに到達したら終了
        if reached_cached_timeset:
            print("ScoreSaberのキャッシュ取得完了条件に達しました。キャッシュ済みスコアに到達しました。" f"ページ: {page} 総件数: {len(scores_by_lb_id)}")
            break

        if len(items) < limit_param or (max_pages_sc is not None and page >= max_pages_sc):
            print("ScoreSaberのキャッシュ取得完了条件に達しました。" f"ページ: {page} アイテム数: {len(items)}")
            break

        if page >= _MAX_PAGES_GUARD:
            print(f"ScoreSaberのスコア取得: ページ数上限 {_MAX_PAGES_GUARD} に達したため打ち切ります")
            break

        page += 1

    if scores_by_lb_id:
        try:
            _save_cached_player_scores(
                cache_path, scores_by_lb_id,
                resume_stop_ts=None if run_complete else (effective_stop_ts or _RESUME_FROM_BEGINNING),
            )
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


def refresh_scoresaber_ranked_scores(
    scoresaber_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    status: Optional[dict] = None,
) -> int:
    """ランク譜面のスコアだけを pp の高い順（sort=top）に取り直し、スコアキャッシュに上書きする。

    pp が変わるのはランク譜面のスコアだけなので、全スコア（200 ページ以上）ではなく
    pp の付いたスコア（約 60 ページ）だけ読む。pp が 0 のスコアが出てきたら止める。
    途中で失敗したら status["complete"] = False を入れ、それまでに取れた分だけ反映する。
    戻り値は取り直したスコアの件数。
    """
    cache_path = CACHE_DIR / f"scoresaber_player_scores_{scoresaber_id}.json"
    url = SCORESABER_PLAYER_SCORES_URL.format(player_id=scoresaber_id)
    fetched: dict[str, dict] = {}
    page = 1
    max_pages: Optional[int] = None
    complete = True
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
            if lb_id is not None:
                fetched[str(lb_id)] = item
        if reached_unranked or len(items) < 100 or page >= _MAX_PAGES_GUARD:
            break
        page += 1

    if not complete and status is not None:
        status["complete"] = False
    if not fetched:
        return 0
    scores = _load_cached_player_scores(cache_path) or {}
    scores.update(fetched)
    # 差分取得の途中失敗の印（resume_stop_ts）は残す
    resume_stop_ts: Optional[str] = None
    try:
        _raw = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else None
        if isinstance(_raw, dict) and isinstance(_raw.get("resume_stop_ts"), str):
            resume_stop_ts = _raw["resume_stop_ts"]
    except Exception:  # noqa: BLE001
        resume_stop_ts = None
    _save_cached_player_scores(cache_path, scores, resume_stop_ts=resume_stop_ts)
    return len(fetched)


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