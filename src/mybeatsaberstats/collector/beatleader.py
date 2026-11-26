from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Optional, Callable

import math
import requests
import time
import traceback

from ..snapshot import BASE_DIR, StarClearStat
from ..http_client import make_session
from ..fileio import atomic_write_text
from ..ranked_sync import (
    flag_star_drift_if_any, load_newly_ranked_checked, full_traversal_due, mismatch_traversal_allowed,
    record_full_traversal, save_newly_ranked_checked, flag_star_drift, stars_differ,
    star_sync_not_found_mark, star_sync_skip,
)
from ..modifiers import classify_play
from ..http_client import get_memoized
from ..beatleader import BASE_URL as _BL_API_BASE_URL

CACHE_DIR = BASE_DIR / "cache"
LOG_DIR = BASE_DIR / "logs"
LOG_PATH = LOG_DIR / "beatleader_api.log"

BL_BASE_URL = _BL_API_BASE_URL
BEATLEADER_LEADERBOARDS_URL = f"{BL_BASE_URL}/leaderboards"
# ランク譜面の全件取り直しの記録（いつ・最後まで取れたか・総数が合わないまま終わったか）。判定は ranked_sync.py。
_RANKED_FULL_TRAVERSAL_STATE = BASE_DIR / "cache" / "ranked_full_traversal.json"


def _short_error_text(exc: BaseException, limit: int = 160) -> str:
    """警告に添える短いエラー文。例外の種類と本文の先頭だけにする（全文は logs/beatleader_api.log）。"""
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


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


def _save_cached_pages(path: Path, pages: list[dict]) -> None:
    """ページリストをキャッシュファイル(JSON)として保存する (leaderboards 用)."""
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


def _load_cached_player_score_failed_pages(path: Path) -> list[int]:
    """BeatLeader プレイヤースコアキャッシュから未回収ページ番号一覧を返す。"""

    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        failed_pages = raw.get("failed_pages")
        if isinstance(failed_pages, list):
            pages: list[int] = []
            for value in failed_pages:
                try:
                    page = int(value)
                except (TypeError, ValueError):
                    continue
                if page > 0:
                    pages.append(page)
            return sorted(set(pages))
    except Exception:
        return []
    return []


# スコアの取得は 1 ページ 100 件（/player/{id}/scores の count）
_PAGE_SIZE = 100
# 全件取得（Fetch ALL）の遡り指定。collector._FULL_FETCH_UNTIL と同じ値（UTC の 2000-01-01）
_FULL_FETCH_UNTIL_TS = int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp())


def _score_sort_ts(item: dict) -> Optional[int]:
    """API の並び（sortBy=date）の基準になる timeset。"""
    ts = _extract_player_timeset_from_bl_score_item(item)
    if ts is None:
        ts = _extract_leaderboard_timeset_from_score_item(item)
    return ts


def _range_pages(r: dict) -> list[int]:
    first = int(r.get("page") or 1)
    last = max(first, int(r.get("last_page") or first))
    return list(range(first, last + 1))


def _normalize_failed_range(value: object) -> Optional[dict]:
    if not isinstance(value, dict):
        return None

    def _opt_int(v: object) -> Optional[int]:
        if v is None:
            return None
        try:
            return int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    page = _opt_int(value.get("page"))
    if page is None or page < 1:
        return None
    last_page = _opt_int(value.get("last_page")) or page
    return {
        "page": page,
        "last_page": max(page, last_page),
        "newer_ts": _opt_int(value.get("newer_ts")),
        "older_ts": _opt_int(value.get("older_ts")),
        "to_end": bool(value.get("to_end")),
    }


def _legacy_failed_ranges(pages: list[int], scores: dict) -> list[dict]:
    """範囲（failed_ranges）を持たない古いキャッシュの failed_pages を範囲に直す。

    - page 1 だけの印：普段の差分取得（page 1 から、差分の無いページまで）で埋まる範囲にする。
    - 深いページの短い印（3 ページまで）：その回の時刻の範囲が分からないので、キャッシュの並び（新しい順）から
      そのページの前後 1 ページ分を広げた時刻の範囲を見積もる。
    - 長く続く印・最後のページまで続く印（古い版が「続けて失敗したら残りのページ全部」に付けたもの）：
      最後まで読みに行くとほぼ全ページになるので、印の始まりから差分の無いページまでを取り直す
      （普段の差分取得と同じ止め方。キャッシュにあって変わっていないページで止まる）。
    """
    ts_list = sorted((t for t in (_score_sort_ts(v) for v in scores.values() if isinstance(v, dict)) if t is not None),
                     reverse=True)
    runs: list[tuple[int, int]] = []
    for p in sorted(set(pages)):
        if runs and p == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], p)
        else:
            runs.append((p, p))
    out: list[dict] = []
    for first, last in runs:
        older_idx = last * _PAGE_SIZE + _PAGE_SIZE
        if (first == 1 and last == 1) or last - first + 1 > 3 or older_idx >= len(ts_list):
            # 差分の無いページまで。始まりは印の 1 つ前のページの最後のスコア（広げない。広げると、
            # 変わっていない前のページで止まってしまう）
            newer_idx = (first - 1) * _PAGE_SIZE - 1
            newer_ts = ts_list[newer_idx] if 0 <= newer_idx < len(ts_list) else None
            out.append({"page": first, "last_page": last, "newer_ts": newer_ts, "older_ts": None, "to_end": False})
            continue
        newer_idx = (first - 1) * _PAGE_SIZE - 1 - _PAGE_SIZE
        newer_ts = ts_list[newer_idx] if 0 <= newer_idx < len(ts_list) else None
        out.append({"page": first, "last_page": last, "newer_ts": newer_ts, "older_ts": ts_list[older_idx],
                    "to_end": False})
    return out


def _load_cached_player_score_failed_ranges(path: Path) -> list[dict]:
    """BeatLeader プレイヤースコアキャッシュから、取りこぼした範囲の一覧を返す。

    1 つの範囲は {"page", "last_page", "newer_ts", "older_ts", "to_end"}。
    page / last_page は失敗した回の時点のページ番号（表示と見積もり用。新しいスコアが増えるとずれる）。
    timeset が older_ts 以上 newer_ts 以下のスコアを読めていない（newer_ts=None は一覧の先頭から）。
    older_ts=None のときは、to_end=True なら一覧の最後まで、False なら差分の無いページまでを取り直す。
    """
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []
    ranges = raw.get("failed_ranges")
    if isinstance(ranges, list):
        return [r for r in (_normalize_failed_range(v) for v in ranges) if r is not None]
    pages = _load_cached_player_score_failed_pages(path)
    if not pages:
        return []
    scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
    return _legacy_failed_ranges(pages, scores)


def _load_cached_fetched_at(path: Path) -> Optional[str]:
    """スコアキャッシュの fetched_at（文字列のまま）。無ければ None。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:  # noqa: BLE001
        return None
    value = raw.get("fetched_at") if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value else None


def _save_cached_player_scores(
    path: Path, scores: dict, failed_ranges: Optional[list[dict]] = None, fetched_at: Optional[str] = None,
) -> None:
    """BeatLeaderプレイヤースコアをマップ形式でキャッシュファイル(JSON)として保存する。

    scores は leaderboard の id をキーにした dict を想定する。
    failed_ranges は取りこぼした範囲（_load_cached_player_score_failed_ranges の形）。
    failed_pages は古い版・collector の判定用に、範囲のページ番号を並べたもの。
    fetched_at を渡すとその値を残す（1 ページも読めなかった回に、取りこぼしの印だけを書き足すとき用。
    fetched_at は 60 日遡りの基準なので、何も読めていない回に進めない）。
    """

    print("Entering _save_cached_player_scores")

    ranges = [r for r in (_normalize_failed_range(v) for v in (failed_ranges or [])) if r is not None]
    pages = sorted({p for r in ranges for p in _range_pages(r)})
    payload = {
        "fetched_at": fetched_at or (datetime.utcnow().isoformat() + "Z"),
        "total_play_count": len(scores),
        "scores": scores,
        "failed_pages": pages,
        "failed_ranges": ranges,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return


def _utc_timestamp(value: datetime) -> int:
    """datetime を Unix 秒にする。タイムゾーンの無い値は UTC として扱い、タイムゾーン付きの値はそのまま換算する。

    この中の fetch_until はタイムゾーン無しの UTC で渡される（画面の「Fetch from:」も collector.local_input_to_utc で
    UTC に直してから渡す）。そのまま .timestamp() に渡すとローカル時刻として扱われ、UTC より西の地域では
    区切りが遅れてスコアを取り逃す。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def _extract_leaderboard_timeset_from_score_item(item: dict) -> Optional[int]:
    """BeatLeaderのスコアオブジェクトから譜面更新時刻(timeset)を取り出して int で返す。

    leaderboard や difficulty 内に timeset があればそれを優先的に使う。
    壊れている/欠けている場合は None。
    """

    if not isinstance(item, dict):
        return None

    # /player/{id}/scores の item はスコアそのもので、timeset は一番上にある（文字列の Unix 秒）。
    # 以前は leaderboard / difficulty の中しか見ておらず常に None になり、再プレイを検知できなかった。
    ts = item.get("timeset")
    if ts is None and isinstance(item.get("score"), dict):
        ts = item["score"].get("timeset")
    if ts is not None:
        try:
            return int(ts)
        except (TypeError, ValueError):
            pass

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


def _load_cached_ranked_leaderboards(path: Path) -> list[dict]:
    """BeatLeader ranked maps cache から leaderboard 配列を復元する。"""

    pages = _load_cached_pages(path) or []
    leaderboards: list[dict] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        data = page.get("data") or {}
        items = data.get("data") or data.get("leaderboards") or []
        if isinstance(items, list):
            leaderboards.extend(lb for lb in items if isinstance(lb, dict))
    return leaderboards


def _build_beatleader_star_stats(
    leaderboards: list[dict],
    scores: list[dict],
    progress: Optional[Callable[[str, float], None]] = None,
) -> list[StarClearStat]:
    """BeatLeader の ranked maps + player scores から★別統計を組み立てる。"""

    def _step(message: str, fraction: float) -> None:
        if progress is not None:
            progress(message, max(0.0, min(1.0, fraction)))

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

        stars_value = diff.get("stars")
        if stars_value is None:
            stars_value = diff.get("difficultyRating")
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

    star_clear_count: dict[int, int] = defaultdict(int)
    star_nf_count: dict[int, int] = defaultdict(int)
    star_ss_count: dict[int, int] = defaultdict(int)
    star_na_count: dict[int, int] = defaultdict(int)
    star_acc_sum: dict[int, float] = defaultdict(float)
    star_acc_count: dict[int, int] = defaultdict(int)
    star_acc_left_sum: dict[int, float] = defaultdict(float)
    star_acc_left_count: dict[int, int] = defaultdict(int)
    star_acc_right_sum: dict[int, float] = defaultdict(float)
    star_acc_right_count: dict[int, int] = defaultdict(int)
    star_fc_count_dict: dict[int, int] = defaultdict(int)
    star_pp_sum: dict[int, float] = defaultdict(float)

    per_leaderboard: dict[str, dict] = {}

    score_count = len(scores)
    for idx, item in enumerate(scores, start=1):
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
            # Player 画面 / Playlist 画面の件数は current ranked maps cache 基準で揃える。
            # 現在の ranked cache から外れた過去スコアは★帯件数へ含めない。
            continue

        star_bucket = leaderboard_star_bucket[lb_id]

        state = per_leaderboard.get(lb_id)
        if state is None:
            state = {
                "star": star_bucket,
                "clear": False,
                "nf": False,
                "ss": False,
                "na": False,
                "best_acc": None,
                "best_acc_left": None,
                "best_acc_right": None,
                "has_fc": False,
                "best_pp": None,
            }
            per_leaderboard[lb_id] = state

        score_info = item.get("score") if isinstance(item, dict) else None
        if not isinstance(score_info, dict):
            score_info = item if isinstance(item, dict) else None

        modifiers = ""
        if isinstance(score_info, dict):
            modifiers = str(score_info.get("modifiers") or "")

        # クリア / NF / SS / NA の判定は modifiers.classify_play にそろえる（プレイリストの ✔ と同じ基準）
        play_kind = classify_play(modifiers)
        is_nf = play_kind == "nf"
        is_ss = play_kind == "ss"
        is_na = play_kind == "na"

        # PP は修飾子（クリア / NF / SS / NA）に関係なく、サーバーが pp を付けたスコアを全部入れる。
        # BeatLeader のプロフィールの pp はそうやって合計されている（例: NA のスコアにも pp が付く）。
        # 1つでも外すとプロフィールの pp と合わなくなり、PP のずれ検知（reconcile）が誤判定する。
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

            acc = _extract_beatleader_accuracy(score_info) if isinstance(score_info, dict) else None
            if acc is not None:
                best = state.get("best_acc")
                if best is None or acc > best:
                    state["best_acc"] = acc
                    if isinstance(score_info, dict):
                        al = score_info.get("accLeft")
                        ar = score_info.get("accRight")
                        state["best_acc_left"] = float(al) if al is not None else None
                        state["best_acc_right"] = float(ar) if ar is not None else None

        if score_count > 0 and (idx == 1 or idx == score_count or idx % 200 == 0):
            _step(
                f"Collecting BeatLeader star stats: aggregating {idx:,}/{score_count:,}",
                0.90 + 0.10 * (idx / score_count),
            )

    for state in per_leaderboard.values():
        star_bucket = int(state["star"])
        has_clear = bool(state["clear"])
        has_nf = bool(state["nf"])
        has_ss = bool(state["ss"])

        if has_clear:
            star_clear_count[star_bucket] += 1
            if bool(state.get("has_fc")):
                star_fc_count_dict[star_bucket] += 1
            best_acc = state.get("best_acc")
            if isinstance(best_acc, (int, float)) and math.isfinite(float(best_acc)):
                star_acc_sum[star_bucket] += float(best_acc)
                star_acc_count[star_bucket] += 1
                best_left = state.get("best_acc_left")
                best_right = state.get("best_acc_right")
                best_acc_val = float(best_acc)
                if (
                    best_left is not None
                    and best_right is not None
                    and math.isfinite(float(best_left))
                    and math.isfinite(float(best_right))
                ):
                    al = float(best_left)
                    ar = float(best_right)
                    if al + ar > 0:
                        l_val = 2.0 * best_acc_val * al / (al + ar)
                        r_val = 2.0 * best_acc_val * ar / (al + ar)
                        if math.isfinite(l_val) and math.isfinite(r_val):
                            star_acc_left_sum[star_bucket] += l_val
                            star_acc_right_sum[star_bucket] += r_val
                            star_acc_left_count[star_bucket] += 1
                            star_acc_right_count[star_bucket] += 1
        elif has_nf:
            star_nf_count[star_bucket] += 1
        elif has_ss:
            star_ss_count[star_bucket] += 1
        elif bool(state.get("na")):
            star_na_count[star_bucket] += 1

    cleared_pp_entries_bl: list[tuple[int, float]] = []
    for state in per_leaderboard.values():
        # 修飾子で絞らない（上の best_pp のコメント参照）
        pp_val = state.get("best_pp")
        if isinstance(pp_val, (int, float)) and math.isfinite(float(pp_val)) and float(pp_val) > 0:
            cleared_pp_entries_bl.append((int(state["star"]), float(pp_val)))
    cleared_pp_entries_bl.sort(key=lambda x: x[1], reverse=True)
    for rank, (star_bucket, pp_val) in enumerate(cleared_pp_entries_bl, start=1):
        weight = 0.965 ** (rank - 1)
        star_pp_sum[star_bucket] += pp_val * weight

    star_pp_solo_sum: dict[int, float] = defaultdict(float)
    star_pp_list_bl: dict[int, list[float]] = defaultdict(list)
    for star_bucket, pp_val in cleared_pp_entries_bl:
        star_pp_list_bl[star_bucket].append(pp_val)
    for star_bucket, pp_vals in star_pp_list_bl.items():
        for local_rank, pp_v in enumerate(sorted(pp_vals, reverse=True), start=1):
            star_pp_solo_sum[star_bucket] += pp_v * (0.965 ** (local_rank - 1))

    all_stars_bl = (
        set(star_map_count.keys())
        | set(star_clear_count.keys())
        | set(star_nf_count.keys())
        | set(star_ss_count.keys())
        | set(star_na_count.keys())
    )
    stats: list[StarClearStat] = []
    for star in sorted(all_stars_bl):
        map_count = star_map_count.get(star, 0)
        cleared = star_clear_count.get(star, 0)
        nf = star_nf_count.get(star, 0)
        ss = star_ss_count.get(star, 0)
        na = star_na_count.get(star, 0)
        acc_sum = star_acc_sum.get(star, 0.0)
        acc_count = star_acc_count.get(star, 0)

        avg_acc = None
        if acc_count > 0 and math.isfinite(float(acc_sum)):
            avg_acc = acc_sum / acc_count

        avg_acc_left = None
        left_cnt = star_acc_left_count.get(star, 0)
        if left_cnt > 0:
            avg_acc_left = star_acc_left_sum.get(star, 0.0) / left_cnt

        avg_acc_right = None
        right_cnt = star_acc_right_count.get(star, 0)
        if right_cnt > 0:
            avg_acc_right = star_acc_right_sum.get(star, 0.0) / right_cnt

        fc_count = star_fc_count_dict.get(star, 0)
        clear_rate = (cleared / map_count) if map_count > 0 else 0.0

        pp_total = star_pp_sum.get(star)
        pp_contribution = float(pp_total) if pp_total is not None and pp_total > 0 else (0.0 if cleared > 0 else None)
        pp_solo_val = star_pp_solo_sum.get(star)
        pp_solo = float(pp_solo_val) if pp_solo_val is not None and pp_solo_val > 0 else (0.0 if cleared > 0 else None)

        stats.append(StarClearStat(
            star=star,
            map_count=map_count,
            clear_count=cleared,
            nf_count=nf,
            ss_count=ss,
            na_count=na,
            clear_rate=clear_rate,
            average_acc=avg_acc,
            fc_count=fc_count,
            avg_acc_left=avg_acc_left,
            avg_acc_right=avg_acc_right,
            pp_contribution=pp_contribution,
            pp_solo=pp_solo,
        ))

    return stats


def collect_beatleader_star_stats_from_cache(beatleader_id: str) -> list[StarClearStat]:
    """現在の BeatLeader キャッシュだけを使って★別統計を再計算する。"""

    if not beatleader_id:
        return []

    ranked_cache_path = CACHE_DIR / "beatleader_ranked_maps.json"
    score_cache_path = CACHE_DIR / f"beatleader_player_scores_{beatleader_id}.json"

    leaderboards = _load_cached_ranked_leaderboards(ranked_cache_path)
    scores_dict = _load_cached_player_scores(score_cache_path) or {}
    return _build_beatleader_star_stats(leaderboards, list(scores_dict.values()))


def _ranked_time(lb: dict) -> int:
    """譜面の rankedTime（無い・不正なら 0）。"""
    diff = lb.get("difficulty") if isinstance(lb, dict) else None
    try:
        return int(diff.get("rankedTime") or 0) if isinstance(diff, dict) else 0
    except (TypeError, ValueError):
        return 0


def _get_beatleader_leaderboards_ranked(
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    fetch_until: Optional[datetime] = None,
    status: Optional[dict] = None,
) -> list[dict]:
    """
    BeatLeaderのRanked譜面リストをAPIから全件取得し、キャッシュも利用する。
    進捗コールバック(progress)対応。

    status に dict を渡すと、途中で失敗して最後まで取れなかったときに status["complete"] = False を入れる。

    fetch_until が指定された場合、その日時より古い timeset の譜面が現れた時点で取得を終了する。
    sortBy は fetch_until 指定時は timestamp (新しい順) を使用する。
    """
    print("Entering _get_beatleader_leaderboards_ranked")
    cache_path = CACHE_DIR / "beatleader_ranked_maps.json"

    # fetch_until を Unix タイムスタンプに変換
    fetch_until_ts: Optional[int] = None
    if fetch_until is not None:
        fetch_until_ts = _utc_timestamp(fetch_until)
        print(f"BeatLeader Ranked Maps fetch_until 指定: {fetch_until.isoformat()} (Unix: {fetch_until_ts})")
        if fetch_until_ts >= 978307200 and full_traversal_due(_RANKED_FULL_TRAVERSAL_STATE, "beatleader"):
            # 窓の外で変わった★は日付の窓では拾えない。初回と★のずれを見つけたときだけ全件取り直す。
            print("BL Ranked Maps: 全件取得の記録が無いか★のずれを検知したため、全件取り直しをします")
            fetch_until = datetime(2000, 1, 1)
            fetch_until_ts = _utc_timestamp(fetch_until)

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
            # fetch_until が指定されている場合は timestamp 降順で取得するため sortBy を切り替える
            sort_by = "timestamp" if fetch_until_ts is not None else "stars"
            params_first = {
                "page": "1",
                "count": str(page_size),
                "type": "Ranked",
                "sortBy": sort_by,
                "order": "desc",
            }
            resp = session.get(BEATLEADER_LEADERBOARDS_URL, params=params_first, timeout=10)
            if resp.status_code == 404:
                # 1 ページ目から取れなかった。キャッシュをそのまま返すが、最後まで取れていないことは伝える
                # （ランクから外れたばかりの譜面のスコアを外さないため）。
                print("BL Ranked Maps: 1 ページ目が 404 のため、キャッシュをそのまま使います")
                if status is not None:
                    status["complete"] = False
                if fetch_until_ts is not None and fetch_until_ts < 978307200:
                    record_full_traversal(_RANKED_FULL_TRAVERSAL_STATE, "beatleader", False, None)
                if progress is not None:
                    progress(1, 1)
                return leaderboards
            else:
                resp.raise_for_status()
                data_first = resp.json()
                meta = data_first.get("metadata") or {}
                try:
                    new_total = int(meta.get("total", cached_total))
                except (TypeError, ValueError):
                    new_total = cached_total

                # fetch_until が指定されていない場合のみ、増分なしでキャッシュ返却する
                if new_total <= cached_total and fetch_until_ts is None:
                    if progress is not None:
                        progress(1, 1)
                    # 取得済みを記録するため fetched_at だけ更新する
                    _touch_cache_fetched_at(cache_path)
                    return leaderboards

                # leaderboard id → アイテム の辞書を既存キャッシュから構築（重複排除用）
                lb_by_id: dict[str, dict] = {str(lb.get("id")): lb for lb in leaderboards if isinstance(lb, dict)}
                pages = []
                reached_fetch_until = False
                seen_ids: set[str] = set()
                # 2000 年まで遡る＝全件取得。このときは見つかった譜面だけでキャッシュを作り直し、ランクから外れた譜面を消す。
                full_traversal = fetch_until_ts is not None and fetch_until_ts < 978307200  # 2001-01-01
                # 一覧の終わり（100 件未満のページ）まで自然に読み終えたか。404・空ページで抜けたときは False
                # （ただし、読んだ件数が API の総数に届いた後の 404・空ページは、ちょうど割り切れた一覧の終わりとみなす）。
                natural_end = False
                raw_items = 0  # 一覧で読んだ件数（重複を含む）

                page = 1
                while True:
                    if progress is not None:
                        progress(page, None)
                    params = {
                        "page": str(page),
                        "count": str(page_size),
                        "type": "Ranked",
                        "sortBy": sort_by,
                        "order": "desc",
                    }
                    if page == 1:
                        data = data_first
                    else:
                        resp_page = session.get(BEATLEADER_LEADERBOARDS_URL, params=params, timeout=10)
                        if resp_page.status_code == 404:
                            natural_end = 0 < raw_items >= new_total
                            break
                        resp_page.raise_for_status()
                        data = resp_page.json()

                    pages.append({"page": page, "params": params, "data": data})

                    items = data.get("data") if isinstance(data, dict) else None
                    if items is None and isinstance(data, dict):
                        items = data.get("leaderboards")
                    if not isinstance(items, list) or not items:
                        # 1 件も読めていない（総数 0 の異常な応答など）ときは、一覧の終わりとみなさない
                        natural_end = 0 < raw_items >= new_total
                        break
                    raw_items += len(items)

                    for lb in items:
                        if not isinstance(lb, dict):
                            continue
                        # fetch_until_ts が指定されている場合は rankedTime で停止判定
                        if fetch_until_ts is not None:
                            diff = lb.get("difficulty") or {}
                            ts_raw = diff.get("rankedTime") if isinstance(diff, dict) else None
                            try:
                                ts_val = int(ts_raw) if ts_raw is not None else None
                            except (TypeError, ValueError):
                                ts_val = None
                            if ts_val is not None and ts_val > 0 and ts_val < fetch_until_ts:
                                print(f"BL Ranked Maps: fetch_until {fetch_until_ts} に到達 (rankedTime={ts_val})")
                                reached_fetch_until = True
                                break
                        lb_id = str(lb.get("id"))
                        lb_by_id[lb_id] = lb
                        seen_ids.add(lb_id)

                    if reached_fetch_until:
                        print(f"BL Ranked Maps: fetch_until 境界に達したため取得終了。総件数: {len(lb_by_id)}")
                        natural_end = True
                        break

                    if len(items) < page_size:
                        natural_end = True
                        break

                    page += 1

                if not full_traversal and fetch_until_ts is not None and not reached_fetch_until \
                        and len(seen_ids) >= new_total:
                    # 境界に達する前に一覧が終わり、API の総数まで全部読めた＝実質、全件取得
                    full_traversal = True
                if full_traversal:
                    complete_full = bool(natural_end and seen_ids and len(seen_ids) >= new_total)
                    if complete_full:
                        # API の総数まで全部取れたので、見つからなかった譜面（ランクから外れたもの）を消す
                        lb_by_id = {lb_id: lb for lb_id, lb in lb_by_id.items() if lb_id in seen_ids}
                    else:
                        # 途中で止まった（404・空ページなど）か、列挙数が総数に届かなかった。
                        # 消すと残りの譜面まで消えるので消さない
                        print(f"BL Ranked Maps: 全件取得が途中で止まりました（{len(seen_ids)}/{new_total} 件）。古い譜面は消しません")
                        if status is not None:
                            status["complete"] = False
                    if natural_end:
                        # 一覧が最後まで読めた。キャッシュの件数が API の総数と合えば完了。
                        # 総数が合わないときは API の総数も残して繰り返さない。
                        ok = len(lb_by_id) == new_total
                        record_full_traversal(
                            _RANKED_FULL_TRAVERSAL_STATE, "beatleader", ok,
                            new_total if not ok else None,
                        )
                    else:
                        # 途中で止まった（404・空ページ）。キャッシュの古い分で件数が合っても完了にしない
                        # （★のずれの印も残し、次回も全件取り直す）。
                        record_full_traversal(_RANKED_FULL_TRAVERSAL_STATE, "beatleader", False, None)
                else:
                    if reached_fetch_until and fetch_until_ts is not None:
                        # 窓の中（rankedTime が境界以降）の譜面は全部読んだ。API に無い＝ランクから外れた/入れ替わった。
                        # 総数が同じままの入れ替わりは総数の突き合わせでは見えないので、ここで消す。
                        # （rankedTime が 0 の譜面は並び順が分からないので対象外。誤って消しても総数チェックで戻る）
                        stale = [
                            lb_id for lb_id, cached_lb in lb_by_id.items()
                            if lb_id not in seen_ids and _ranked_time(cached_lb) > 0
                            and _ranked_time(cached_lb) >= fetch_until_ts
                        ]
                        for lb_id in stale:
                            del lb_by_id[lb_id]
                        if stale:
                            print(f"BL Ranked Maps: 窓の中でランクから外れた譜面 {len(stale)} 件をキャッシュから消しました")
                    if (
                        fetch_until_ts is not None and len(lb_by_id) != new_total
                        and mismatch_traversal_allowed(_RANKED_FULL_TRAVERSAL_STATE, "beatleader", new_total)
                    ):
                        # キャッシュと API の総数が合わない＝窓より古い所の取りこぼし/ランク外れが残っている。全件取り直す。
                        print(f"BL Ranked Maps: キャッシュ {len(lb_by_id)} 件 ≠ API {new_total} 件のため、全件取り直します")
                        return _get_beatleader_leaderboards_ranked(
                            session, progress=progress, fetch_until=datetime(2000, 1, 1), status=status,
                        )

                leaderboards = list(lb_by_id.values())
                # 増分更新後は全マップを含む統合単一ページとして保存する。
                # 増分ページだけを保存すると次回起動時に古いマップが失われ、
                # キャッシュが徐々に劣化するため、必ず全データをまとめて上書きする。
                consolidated = [
                    {
                        "page": 1,
                        "params": {
                            "page": "1",
                            "count": str(len(leaderboards)),
                            "type": "Ranked",
                            "sortBy": "timestamp",
                            "order": "desc",
                        },
                        "data": {
                            "metadata": {
                                "total": new_total,
                                "page": 1,
                                "itemsPerPage": len(leaderboards),
                            },
                            "data": leaderboards,
                        },
                    }
                ]
                try:
                    _save_cached_pages(cache_path, consolidated)
                except Exception:
                    pass

                if progress is not None:
                    progress(page, None)
                return leaderboards
        except Exception as exc:  # noqa: BLE001
            _log_api_failure(
                "_get_beatleader_leaderboards_ranked",
                f"Failed during metadata refresh url={BEATLEADER_LEADERBOARDS_URL} fetch_until={fetch_until}",
                exc,
            )
            # 進捗ダイアログのキャンセルは握りつぶさない
            if isinstance(exc, RuntimeError) and str(exc) == "SNAPSHOT_CANCELLED":
                raise
            if status is not None:
                status["complete"] = False
            if fetch_until_ts is not None and fetch_until_ts < 978307200:
                # 全件取得が途中で失敗した。キャッシュは変えない。次回（1 時間後以降）取り直す。
                record_full_traversal(_RANKED_FULL_TRAVERSAL_STATE, "beatleader", False, None)
            # メタデータ確認に失敗した場合は、既存キャッシュをそのまま返す
            if progress is not None:
                progress(1, 1)
            return leaderboards

    else:
        pages = []
        leaderboards = []
        lb_by_id_fresh: dict[str, dict] = {}
        # fetch_until が指定されている場合は timestamp 降順で取得
        sort_by = "timestamp" if fetch_until_ts is not None else "stars"
        reached_fetch_until_fresh = False
        page = 1
        while True:
            if progress is not None:
                progress(page, None)
            params = {
                "page": str(page),
                "count": str(page_size),
                "type": "Ranked",
                "sortBy": sort_by,
                "order": "desc",
            }
            try:
                resp = session.get(BEATLEADER_LEADERBOARDS_URL, params=params, timeout=10)
            except Exception as exc:  # noqa: BLE001
                _log_api_failure(
                    "_get_beatleader_leaderboards_ranked",
                    f"Request failed url={BEATLEADER_LEADERBOARDS_URL} params={params}",
                    exc,
                )
                raise
            if resp.status_code == 404:
                break
            try:
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                _log_api_failure(
                    "_get_beatleader_leaderboards_ranked",
                    f"Bad response url={resp.url} status={resp.status_code}",
                    exc,
                )
                raise

            pages.append({"page": page, "params": params, "data": data})

            items = data.get("data") if isinstance(data, dict) else None
            if items is None and isinstance(data, dict):
                items = data.get("leaderboards")
            if not isinstance(items, list) or not items:
                break

            for lb in items:
                if not isinstance(lb, dict):
                    continue
                if fetch_until_ts is not None:
                    diff = lb.get("difficulty") or {}
                    ts_raw = diff.get("rankedTime") if isinstance(diff, dict) else None
                    try:
                        ts_val = int(ts_raw) if ts_raw is not None else None
                    except (TypeError, ValueError):
                        ts_val = None
                    if ts_val is not None and ts_val > 0 and ts_val < fetch_until_ts:
                        print(f"BL Ranked Maps (新規): fetch_until {fetch_until_ts} に到達 (rankedTime={ts_val})")
                        reached_fetch_until_fresh = True
                        break
                lb_id_f = str(lb.get("id"))
                lb_by_id_fresh[lb_id_f] = lb

            if reached_fetch_until_fresh:
                break

            leaderboards = list(lb_by_id_fresh.values())

            if len(items) < page_size:
                break

            page += 1

        # ループ終了後に leaderboards を確定（fetch_until 到達時も含む）
        leaderboards = list(lb_by_id_fresh.values())

    if pages:
        try:
            _save_cached_pages(cache_path, pages)
        except Exception:
            pass

    if progress is not None:
        progress(int(page), None)  # type: ignore

    return leaderboards


def _extract_player_timeset_from_bl_score_item(item: dict) -> Optional[int]:
    """BeatLeaderのスコアオブジェクトからプレイヤーがプレイした時刻(Unixタイムスタンプ)を取得する。

    APIの sortBy=date&order=desc で並んでいる基準の timeset を返す。
    取得できない場合は None を返す。
    """
    if not isinstance(item, dict):
        return None
    ts = item.get("timeset")
    if ts is None:
        return None
    try:
        return int(ts)
    except (TypeError, ValueError):
        return None


def _compact_pages(pages: list[int]) -> str:
    """[1, 2, 3, 57, 58] → "1-3, 57-58"（cp932 のコンソールにも出せるよう ASCII のハイフン）。"""
    runs: list[list[int]] = []
    for p in sorted(set(pages)):
        if runs and p == runs[-1][1] + 1:
            runs[-1][1] = p
        else:
            runs.append([p, p])
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in runs)


def _get_beatleader_player_scores(
    player_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    fetch_until: Optional[datetime] = None,
    retry_failed_pages_only: bool = False,
    warning_callback: Optional[Callable[[str], None]] = None,
    status: Optional[dict] = None,
) -> list[dict]:
    """BeatLeader のプレイヤースコア一覧を API から取得し、キャッシュに足す。

    `/player/{id}/scores?sortBy=date&order=desc&type=best` を 1 ページ 100 件で読む
    （譜面ごとのベストスコアを、プレイした日時 timeset の新しい順）。
    ページごとの進捗を progress(page, max_pages) で通知する。

    - fetch_until=None：差分取得。page 1 から読み、キャッシュとの差分が無いページ
      （件数が totalPlayCount に届いた後）で止まる。
    - fetch_until 指定：その日時より古いスコアに届くまで遡る（新ランク譜面の 60 日遡り・期間指定・Fetch ALL）。

    読めなかったページは「取りこぼした範囲」（timeset の範囲）としてキャッシュに残し、次の回に
    その範囲だけを取り直す。ページ番号は新しいスコアが増えるとずれるので、取り直すページは
    「いまのキャッシュでその範囲より新しいスコアの件数」から見積もり、読んだページの timeset で
    範囲の始まりと終わりを含んだことを確かめる。確かめられなかった範囲は残す。
    retry_failed_pages_only=True のときは、残っている範囲（一覧の先頭からの範囲を除く）だけを取り直す。
    """

    print("Entering _get_beatleader_player_scores")
    cache_path = CACHE_DIR / f"beatleader_player_scores_{player_id}.json"

    def _warn(message: str) -> None:
        print(message)
        if warning_callback is not None:
            try:
                warning_callback(message)
            except Exception:
                pass

    scores_by_lb_id: dict[str, dict] = {}
    cached_scores = _load_cached_player_scores(cache_path)
    pending_ranges = _load_cached_player_score_failed_ranges(cache_path)
    if cached_scores is not None:
        print(f"BeatLeaderのキャッシュ読み込み成功: {player_id} スコア件数: {len(cached_scores)}")
        scores_by_lb_id.update(cached_scores)
    else:
        cached_scores = {}
    # キャッシュで一番新しいスコアの timeset（遡り取得の止め所・page 1 が取れなかった回の範囲に使う）
    latest_cached_ts: Optional[int] = max(
        (t for t in (_score_sort_ts(v) for v in cached_scores.values() if isinstance(v, dict)) if t is not None),
        default=None,
    )
    if pending_ranges:
        print(f"BeatLeaderスコア取得: 前回の取りこぼし {pending_ranges}")

    def _sorted_score_items() -> list[dict]:
        return [item for _, item in sorted(
            scores_by_lb_id.items(),
            key=lambda kv: _extract_leaderboard_timeset_from_score_item(kv[1]) or 0,
            reverse=True,
        )]

    def _get_score_pp(src: dict) -> float:
        sc = src.get("score") if isinstance(src, dict) else None
        if not isinstance(sc, dict):
            sc = src
        try:
            return float(sc.get("pp") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _save(ranges: list[dict], keep_fetched_at: bool = False) -> None:
        ordered_scores = {lb_id: item for lb_id, item in sorted(
            scores_by_lb_id.items(),
            key=lambda kv: _extract_leaderboard_timeset_from_score_item(kv[1]) or 0,
            reverse=True,
        )}
        try:
            _save_cached_player_scores(
                cache_path, ordered_scores, failed_ranges=ranges,
                fetched_at=_load_cached_fetched_at(cache_path) if keep_fetched_at else None,
            )
        except Exception:
            pass

    page_size = _PAGE_SIZE
    # 直前の _fetch_scores_page で失敗した理由（警告に添える）。
    last_fetch_error: dict[str, str] = {"text": ""}
    # PoliteSession が Retry-After に従って待った後も 429 が返ったか。返ったらこの回の読み込みを止める
    # （次のページを取りに行っても同じく 429 になるだけ。SS のスコア取得と同じ）
    rate_limited = {"hit": False}

    def _fetch_scores_page(page_no: int) -> tuple[Optional[requests.Response], Optional[dict], bool, bool]:
        url = f"{BL_BASE_URL}/player/{player_id}/scores"
        params = {
            "page": str(page_no),
            "count": str(page_size),
            "sortBy": "date",
            "order": "desc",
            "type": "best",
        }
        resp: Optional[requests.Response] = None
        data: Optional[dict] = None
        page_failed = False
        not_found = False
        last_fetch_error["text"] = ""
        for attempt in range(1, 4):
            try:
                resp = session.get(url, params=params, timeout=10)
            except Exception as exc:  # noqa: BLE001
                last_fetch_error["text"] = _short_error_text(exc)
                _log_api_failure(
                    "_get_beatleader_player_scores",
                    f"Request failed url={url} player_id={player_id} params={params} attempt={attempt}/3",
                    exc,
                )
                if attempt >= 3:
                    page_failed = True
                    break
                time.sleep(0.5 * attempt)
                continue

            print(f"Fetching BeatLeader player scores page {page_no}... URL: {resp.url} params: {params}")
            if resp.status_code == 404:
                print("BeatLeaderスコア取得: 404 Not Found")
                _log_api_failure("_get_beatleader_player_scores", f"404 Not Found url={url} player_id={player_id} params={params}")
                not_found = True
                break

            try:
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:  # noqa: BLE001
                last_fetch_error["text"] = f"HTTP {resp.status_code}: {_short_error_text(exc)}"
                _log_api_failure(
                    "_get_beatleader_player_scores",
                    f"Bad response url={resp.url} status={resp.status_code} player_id={player_id} attempt={attempt}/3",
                    exc,
                )
                # 429 / 503 は PoliteSession が Retry-After に従って既にリトライしている。
                # ここでも重ねてリトライすると、障害時に 1 ページあたり最大 9 回になるので外す。
                if resp.status_code == 429:
                    rate_limited["hit"] = True
                retryable_status = resp.status_code in {500, 502, 504}
                if retryable_status and attempt < 3:
                    time.sleep(0.5 * attempt)
                    continue
                page_failed = True
                break
        return resp, data, page_failed, not_found

    def _merge_item(item: object) -> bool:
        """1 件をキャッシュに足す。キャッシュ（この回の読み込み時点）と違えば True。"""
        if not isinstance(item, dict):
            return False
        leaderboard = item.get("leaderboard")
        if leaderboard is None:
            leaderboard = item
        if not isinstance(leaderboard, dict):
            return False
        diff = leaderboard.get("difficulty") or {}
        lb_id_raw = leaderboard.get("id") or diff.get("leaderboardId") or diff.get("id")
        if lb_id_raw is None:
            return False
        lb_id = str(lb_id_raw)
        old_item = cached_scores.get(lb_id)
        if old_item is None:
            scores_by_lb_id[lb_id] = item
            return True
        old_ts = _extract_leaderboard_timeset_from_score_item(old_item)
        new_ts = _extract_leaderboard_timeset_from_score_item(item)
        pp_changed = abs(_get_score_pp(item) - _get_score_pp(old_item)) > 0.01
        if (new_ts is not None and (old_ts is None or new_ts > old_ts)) or pp_changed:
            scores_by_lb_id[lb_id] = item
            return True
        return False

    # この回に読めたページ（番号はこの回の時点のもの）。取り直しで同じページを二度取りに行かない。
    page_items: dict[int, list] = {}
    page_has_diff: dict[int, bool] = {}
    # この回に API へ取りに行ったページ（失敗も含む）。メッセージの「読んだページ」に使う。
    requested_pages: list[int] = []
    page_meta: dict[str, Optional[int]] = {"max_pages": None, "total": None}

    def _load_page(page_no: int) -> tuple[Optional[list], bool]:
        """ページを読んでキャッシュに足す。(items, not_found)。読めなければ items=None。"""
        if page_no in page_items:
            return page_items[page_no], False
        requested_pages.append(page_no)
        _resp, data, page_failed, not_found = _fetch_scores_page(page_no)
        if not_found or page_failed or data is None:
            return None, not_found
        if page_meta["max_pages"] is None:
            meta = data.get("metadata") or {}
            try:
                total = int(meta.get("total", 0))
                per_page = int(meta.get("itemsPerPage", page_size)) or page_size
            except (TypeError, ValueError):
                total = 0
                per_page = page_size
            if total > 0 and per_page > 0:
                page_meta["total"] = total
                page_meta["max_pages"] = min(math.ceil(total / per_page), 300)
            else:
                page_meta["max_pages"] = 100
        items = data.get("data") or data.get("scores") or []
        if not isinstance(items, list):
            return None, False
        page_items[page_no] = items
        # 全件を足す（any() に生成式を渡すと途中で止まるので、先に全部足す）
        page_has_diff[page_no] = any([_merge_item(it) for it in items])
        return items, False

    def _first_ts(items: list) -> Optional[int]:
        for it in items:
            ts = _score_sort_ts(it) if isinstance(it, dict) else None
            if ts is not None:
                return ts
        return None

    def _last_ts(items: list) -> Optional[int]:
        return _first_ts(list(reversed(items)))

    def _is_last_page(page_no: int, items: list) -> bool:
        max_pages = page_meta["max_pages"]
        return len(items) < page_size or (max_pages is not None and page_no >= max_pages)

    def _recover_range(r: dict) -> tuple[bool, dict, int]:
        """取りこぼした範囲を取り直す。(埋められたか, 残す範囲, 読んだ範囲内の件数)。

        取り直すページは、いまのキャッシュで newer_ts より新しいスコアの件数から見積もる
        （page 1 からの差分取得で増えた件数の分だけ、失敗した回からずれている）。
        見積もりのページが範囲の始まりより後ろから始まっていたら、前に戻る。キャッシュに API に無い古いスコアが
        残っていると見積もりが後ろにずれるので、そのページの先頭から newer_ts までのキャッシュの件数の分だけ戻る。
        この回に読んだページへ戻るのは API を呼ばないので数えず、新しく取りに行くのは 5 回まで。
        """
        newer = r.get("newer_ts")
        older = r.get("older_ts")
        to_end = bool(r.get("to_end"))
        if newer is None:
            p = 1
        else:
            above = sum(1 for v in scores_by_lb_id.values() if (_score_sort_ts(v) or 0) > newer)
            p = above // page_size + 1
            if page_meta["max_pages"]:
                p = min(p, int(page_meta["max_pages"]))
        print(f"BeatLeaderスコア取得: 取りこぼし {r} を page {p} から取り直します")
        back_fetches = 0
        while True:
            already = p in page_items
            items, _nf = _load_page(p)
            if progress is not None:
                progress(p, page_meta["max_pages"])
            if items is None:
                return False, r, 0
            first = _first_ts(items)
            if p > 1 and newer is not None and (not items or first is None or first < newer):
                if not already:
                    back_fetches += 1
                if back_fetches > 5:
                    print(f"BeatLeaderスコア取得: 範囲の始まり（timeset {newer}）を含むページが見つかりません")
                    return False, r, 0
                if first is None:
                    step = 1
                else:
                    between = sum(1 for v in scores_by_lb_id.values() if first <= (_score_sort_ts(v) or 0) <= newer)
                    step = max(1, between // page_size)
                p = max(1, p - step)
                continue
            break
        in_range = 0
        while True:
            for it in items:
                ts = _score_sort_ts(it) if isinstance(it, dict) else None
                if ts is not None and (newer is None or ts <= newer) and (older is None or ts >= older):
                    in_range += 1
            max_pages = page_meta["max_pages"]
            if not items and max_pages is not None and p < max_pages:
                # 最後のページより前で空のページが返った（API の一時的なずれ）。読めたとは言えないので残す
                return False, r, in_range
            if not items or _is_last_page(p, items):
                return True, r, in_range
            last = _last_ts(items)
            if older is not None and last is not None and last <= older:
                return True, r, in_range
            first = _first_ts(items)
            if (
                older is None and not to_end and not page_has_diff.get(p, True)
                and (newer is None or (first is not None and first <= newer))
            ):
                # 差分の無いページまで来た（普段の差分取得と同じ止め方）
                return True, r, in_range
            p += 1
            items, _nf = _load_page(p)
            if progress is not None:
                progress(p, page_meta["max_pages"])
            if items is None:
                # 読めたところまで範囲を狭めて残す
                narrowed = dict(r)
                narrowed["page"] = p
                narrowed["last_page"] = max(p, int(r.get("last_page") or p))
                if last is not None:
                    narrowed["newer_ts"] = last
                return False, narrowed, in_range

    if retry_failed_pages_only:
        # 一覧の先頭からの範囲（page 1 の印）は、次の回の差分取得が page 1 から読むときに埋めるので、ここでは扱わない
        # （同じ回に page 1 の失敗を二重に警告しないため）。
        targets = [r for r in pending_ranges if r.get("newer_ts") is not None]
        if not targets:
            if progress is not None:
                progress(1, 1)
            return _sorted_score_items()
        kept = [r for r in pending_ranges if r.get("newer_ts") is None]
        consecutive_failures = 0
        recovered_pages: list[int] = []
        for r in targets:
            if consecutive_failures >= 2 or rate_limited["hit"]:
                # 障害中・レート制限中とみて打ち切る。残りは次に回す。
                kept.append(r)
                continue
            ok, rest, _n = _recover_range(r)
            if ok:
                consecutive_failures = 0
                recovered_pages.extend(_range_pages(r))
                continue
            consecutive_failures += 1
            kept.append(rest)
            _warn(
                f"BeatLeader player scores: page {r['page']} could not be recovered during snapshot; "
                "next snapshot will refetch only that range."
            )
        if requested_pages:
            print(f"BeatLeaderスコア取得: 取り直しで読んだページ {_compact_pages(requested_pages)}")
        if recovered_pages:
            ok_pages = sorted(p for p in set(requested_pages) if p in page_items)
            _warn(
                f"BeatLeaderスコア取得: 未回収ページ {sorted(set(recovered_pages))} を★統計の取得時に取り直しました"
                f"（読んだページ {len(ok_pages)}/{page_meta['max_pages'] or '?'}: {_compact_pages(ok_pages) or 'なし'}）。"
            )
        if scores_by_lb_id:
            # 取りこぼした範囲を取り直しただけで、差分取得を済ませたことにはならないので fetched_at は変えない
            _save(kept, keep_fetched_at=True)
        if kept and status is not None:
            status["complete"] = False
        return _sorted_score_items()

    total_play_count_target: Optional[int] = None
    try:
        stats = _get_beatleader_player_stats(player_id, session)
        if isinstance(stats, dict):
            rpc = stats.get("totalPlayCount")
            if rpc is not None:
                total_play_count_target = int(rpc)
    except Exception:
        total_play_count_target = None

    fetch_until_ts: Optional[int] = None
    if fetch_until is not None:
        fetch_until_ts = _utc_timestamp(fetch_until)
        print(f"BeatLeader fetch_until 指定: {fetch_until.isoformat()} (Unix: {fetch_until_ts})")
    # Fetch ALL（利用者が選んだ全件取得）。読めなかった残りを次の回に自動で読み直さない（全ページ取得を勝手にしない）。
    full_fetch = fetch_until_ts is not None and fetch_until_ts <= _FULL_FETCH_UNTIL_TS
    # 遡り取得の止め所。キャッシュの最新より新しいスコアは全部読む必要があるので、「指定した日時」と
    # 「キャッシュの最新 timeset」の早い方にする（指定がキャッシュの最新より新しいと、その間のスコアを
    # 読まないまま止まる。普段の差分取得も page 1 に差分が無いと止まるので、取りこぼしたままになる）。
    stop_ts: Optional[int] = fetch_until_ts
    if stop_ts is not None and latest_cached_ts is not None and latest_cached_ts < stop_ts:
        print(f"BeatLeader: 指定の日時 {stop_ts} がキャッシュの最新 timeset {latest_cached_ts} より新しいため、そこまで遡ります")
        stop_ts = latest_cached_ts

    previous_ranges = list(pending_ranges)
    new_ranges: list[dict] = []
    open_range: Optional[dict] = None
    prev_last_ts: Optional[int] = None
    reached_last_page = False
    page = 1
    # 続けてこの数だけページの取得に失敗したら、障害とみて打ち切る
    max_consecutive_failures = 2
    consecutive_failures = 0
    # 失敗が続いて打ち切ったか（障害中とみて、前回の取りこぼしの取り直しには行かない）
    aborted = False

    while True:
        items, not_found = _load_page(page)
        if not_found:
            break

        if items is None:
            if page <= 1:
                cause = last_fetch_error["text"] or "unknown error"
                # page 1 が取れないときは、一覧の先頭から「読むはずだった所」までを範囲として残す
                # （遡り取得は止め所まで、普段の差分取得はキャッシュの最新 timeset まで）。
                # 普段の差分取得でも残す：同じ Snapshot の★/pp 同期・PP のずれの取り直しが最近のランク譜面の
                # スコアだけをキャッシュに入れると、次の回は page 1 に差分が無いと判定して止まり、
                # その間のランク外のスコアを読まなくなるため（SS の resume_ranges と同じ扱い）。
                # Fetch ALL はキャッシュの最新 timeset まで（キャッシュに古い値があるので、取りこぼしが出るのはそれより
                # 新しい所だけ。次の回に勝手に全ページは読まない。全部読み直すなら選び直してもらう。SS の required_stop と同じ）。
                top_stop = latest_cached_ts if full_fetch else (stop_ts if fetch_until_ts is not None else latest_cached_ts)
                if (
                    top_stop is not None
                    and not any(r.get("newer_ts") is None and r.get("older_ts") is not None
                                and r["older_ts"] <= top_stop for r in pending_ranges)
                ):
                    # 既にある範囲は残したまま足す（先頭からの範囲が別の止め方のものでも、捨てると取りこぼす）
                    pending_ranges = pending_ranges + [{
                        "page": 1, "last_page": 1, "newer_ts": None, "older_ts": top_stop, "to_end": False,
                    }]
                    if scores_by_lb_id:
                        # 何も読めていないので、fetched_at（60 日遡りの基準）は進めない
                        _save(pending_ranges, keep_fetched_at=True)
                if full_fetch:
                    next_step = "Fetch ALL did not run; select it again to refetch all pages."
                elif pending_ranges:
                    next_step = "the next snapshot will refetch only the unfetched range."
                else:
                    next_step = "the next snapshot will fetch new scores as usual."
                _warn(
                    f"BeatLeader player scores: page 1 could not be fetched ({cause}); "
                    f"using existing cache for this snapshot; {next_step}"
                )
                if status is not None:
                    status["complete"] = False
                return _sorted_score_items()
            if open_range is None:
                open_range = {"page": page, "last_page": page, "newer_ts": prev_last_ts, "older_ts": None, "to_end": False}
            else:
                open_range["last_page"] = page
            _warn(
                f"BeatLeader player scores: page {page} fetch failed; will retry failed pages during BeatLeader star stats."
            )
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures or rate_limited["hit"]:
                if rate_limited["hit"]:
                    _warn(
                        "BeatLeader player scores: rate limited (HTTP 429); "
                        "stopped fetching and will retry the remaining pages later."
                    )
                elif full_fetch:
                    _warn(
                        f"BeatLeader player scores: {consecutive_failures} pages failed in a row; "
                        "Fetch ALL stopped. Failed pages will be retried later; "
                        "select Fetch ALL again to refetch all pages."
                    )
                else:
                    _warn(
                        f"BeatLeader player scores: {consecutive_failures} pages failed in a row; "
                        "stopped fetching and will retry the remaining pages later."
                    )
                aborted = True
                break
            if page_meta["max_pages"] is not None and page >= int(page_meta["max_pages"]):
                break
            page += 1
            continue

        consecutive_failures = 0
        if open_range is not None:
            # 読めなかったページの範囲の終わり＝このページの先頭
            open_range["older_ts"] = _first_ts(items) if items else None
            open_range["to_end"] = not items
            new_ranges.append(open_range)
            open_range = None

        if not items:
            print("BeatLeaderスコア取得: スコアデータ無し")
            reached_last_page = True
            break

        if progress is not None:
            progress(page, page_meta["max_pages"])
        last_ts = _last_ts(items)
        if last_ts is not None:
            prev_last_ts = last_ts

        if stop_ts is not None and last_ts is not None and last_ts < stop_ts:
            print(f"BeatLeader fetch_until 境界に到達: timeset={last_ts} < stop_ts={stop_ts} ページ: {page}")
            break

        diff_here = page_has_diff.get(page, False)
        print(f"Completed fetching page {page} of BeatLeader scores. ranked_play_count_target: {total_play_count_target}, page_has_diff: {diff_here}")
        if fetch_until_ts is None:
            current_count = len(scores_by_lb_id)
            print(f"Current cached score count: {current_count}, Target ranked play count: {total_play_count_target} page_has_diff: {diff_here}")
            # totalPlayCount が取れなかった回（/player/{id} の失敗）も、差分の無いページで止める
            # （止めないと全ページを読む。初回はどのページも差分があるので最後まで読む）
            if not diff_here and (total_play_count_target is None or current_count >= total_play_count_target):
                break

        if _is_last_page(page, items):
            reached_last_page = True
            break

        page += 1

    if open_range is not None:
        # 失敗が続いて打ち切った（または 404）。範囲の終わりは、遡り取得ならその日時、
        # 差分取得・Fetch ALL なら「差分の無いページまで」にする（Fetch ALL の残りを全部は読みに行かない）。
        if stop_ts is not None and not full_fetch:
            open_range["older_ts"] = stop_ts
        new_ranges.append(open_range)
        open_range = None

    main_pages = sorted(set(page_items))

    # 前の回の取りこぼしを、範囲だけ取り直す（この回に読んだページはもう一度取りに行かない）
    # 失敗が続いて打ち切った回は取りに行かない（障害中とみる）。取り直しの途中でも 2 つ続けて失敗したら打ち切る
    # （SS の取りこぼしの取り直し・BL の retry_failed_pages_only と同じ）。残りは次に回す。
    remaining: list[dict] = []
    recovered_items = 0
    recover_failures = 0
    if aborted and previous_ranges:
        print("BeatLeaderスコア取得: 失敗が続いたため、前回の取りこぼしの取り直しは次の回に回します")
    for r in previous_ranges:
        if aborted or recover_failures >= max_consecutive_failures or rate_limited["hit"]:
            remaining.append(r)
            continue
        ok, rest, n = _recover_range(r)
        recovered_items += n
        if ok:
            recover_failures = 0
            continue
        recover_failures += 1
        remaining.append(rest)
    pending_ranges = remaining + new_ranges

    if previous_ranges:
        prev_pages = sorted({p for r in previous_ranges for p in _range_pages(r)})
        read_pages = sorted(p for p in set(requested_pages) if p in page_items)
        failed_read = sorted(p for p in set(requested_pages) if p not in page_items)
        extra_pages = sorted(set(read_pages) - set(main_pages))
        max_pages_text = page_meta["max_pages"] or "?"
        api_total = page_meta["total"]
        count_text = f"キャッシュ {len(scores_by_lb_id)} 件 / API {api_total if api_total is not None else '?'} 件"
        pages_text = f"読んだページ {len(read_pages)}/{max_pages_text}: {_compact_pages(read_pages) or 'なし'}"
        if failed_read:
            pages_text += f"、取れなかったページ {_compact_pages(failed_read)}"
        if remaining:
            rest_pages = sorted({p for r in remaining for p in _range_pages(r)})
            _warn(
                f"BeatLeaderスコア取得: 前回の未回収ページ {prev_pages} のうち {rest_pages} を取り直せませんでした"
                f"（{pages_text}。{count_text}）。このあと★統計の取得時と次回に、その範囲だけ取り直します。"
            )
        else:
            if extra_pages:
                how = f"うち取り直し {_compact_pages(extra_pages)}、範囲のスコア {recovered_items} 件"
            else:
                how = "差分取得で読んだページだけで埋まりました"
            _warn(
                f"BeatLeaderスコア取得: 前回の未回収ページ {prev_pages} を取り直しました"
                f"（{pages_text}。{how}。{count_text}）。"
            )

    if status is not None and (pending_ranges or (full_fetch and not reached_last_page)):
        status["complete"] = False

    if scores_by_lb_id:
        _save(pending_ranges)
        return _sorted_score_items()

    return []


def _get_beatleader_player_stats(player_id: str, session: requests.Session) -> dict:
    """
    BeatLeaderのプレイヤー統計情報(scoreStats)を取得。
    失敗時は空dict。
    """
    print("Entering _get_beatleader_player_stats")
    url = BL_BASE_URL + f"/player/{player_id}"
    try:
        resp = get_memoized(session, url, timeout=10)
        if resp.status_code == 404:
            _log_api_failure("_get_beatleader_player_stats", f"404 Not Found url={url} player_id={player_id}")
            return {}
        resp.raise_for_status()
    except Exception as exc:
        _log_api_failure("_get_beatleader_player_stats", f"Request failed url={url} player_id={player_id}", exc)
        return {}

    try:
        data = resp.json()
    except Exception as exc:
        _log_api_failure("_get_beatleader_player_stats", f"Invalid JSON url={url} player_id={player_id}", exc)
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
    # print("Entering _extract_beatleader_accuracy")
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


# 読めた件数が metadata.total を超えてよい幅（読んでいる間に新しいスコアが入った分）。下回るときは許さない
_PRUNE_TOTAL_OVER_TOLERANCE = 5
# 一度に外す件数がこれを超えたら、API の異常（読み飛ばしなど）を疑って外さない。
# サーバー側でスコアが消えるのは普通は数件なので、小さい固定値にする。
_PRUNE_MAX = 20


def _bl_score_pp(item: dict) -> float:
    try:
        return float(item.get("pp") or 0)
    except (TypeError, ValueError):
        return 0.0


def _prune_missing_ranked_scores(
    scores: dict, fetched: dict, items_read: int, api_total: Optional[int],
) -> int:
    """ランク譜面のスコアの取り直し（最後まで読めたもの）で返らなかったスコアを scores から外す。

    外すのは、キャッシュ上で leaderboard.difficulty.status == 3（Ranked）かつ pp > 0 で、
    ランク譜面キャッシュでも Ranked(3) の譜面なのに、今回返ってこなかったものだけ
    （サーバー側でスコアが消えたもの）。ランク外・pp 0 のスコアは Clears・プレイ数・プレイリストの ✔ に使うので外さない。
    ランクから外れたばかりでキャッシュの status がまだ 3 のスコアを外さないよう、ランク譜面キャッシュと突き合わせる。
    次のときは何も外さない：metadata.total と読めた件数が大きく食い違う／ランク譜面キャッシュが無い／
    外す件数が多すぎる。読んだ件数に重複がある（読んでいる途中で pp の並びが動いた）。戻り値は外した件数。
    """
    if not fetched:
        return 0
    if api_total is None:
        print("BeatLeader: API の総数が分からないため、返らなかったスコアは外しません")
        return 0
    if items_read != len(fetched):
        # 再プレイ・再計算で並びが動くと、重複して返るスコアと、どのページにも出ないスコアができる
        print("BeatLeader: 取り直しの途中で並びが動いた（重複あり）ため、返らなかったスコアは外しません")
        return 0
    if not (api_total <= items_read <= api_total + _PRUNE_TOTAL_OVER_TOLERANCE):
        # 足りない＝途中のページが空で返った・読み飛ばした
        print(
            f"BeatLeader: 取り直しの件数（{items_read}）が API の総数（{api_total}）と食い違うため、"
            "返らなかったスコアは外しません"
        )
        return 0
    ranked_ids = {
        str(lb.get("id"))
        for lb in _load_cached_ranked_leaderboards(CACHE_DIR / "beatleader_ranked_maps.json")
        if lb.get("id") is not None and ((lb.get("difficulty") or {}).get("status") == 3)
    }
    if not ranked_ids:
        print("BeatLeader: ランク譜面キャッシュが無いため、返らなかったスコアは外しません")
        return 0
    cached_ranked = [
        k for k, item in scores.items()
        if isinstance(item, dict)
        and (((item.get("leaderboard") or {}).get("difficulty") or {}).get("status") == 3)
        and _bl_score_pp(item) > 0
    ]
    missing = [k for k in cached_ranked if k not in fetched and k in ranked_ids]
    if not missing:
        return 0
    if len(missing) > _PRUNE_MAX:
        print(
            f"BeatLeader: 取り直しで返らなかったランク譜面のスコアが {len(missing)} 件と多すぎるため、外しません"
        )
        return 0
    for k in missing:
        del scores[k]
    print(f"BeatLeader: 取り直しで返らなかったランク譜面のスコア {len(missing)} 件をキャッシュから外しました")
    return len(missing)


def refresh_beatleader_ranked_scores(
    player_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    status: Optional[dict] = None,
    prune_missing: bool = True,
) -> int:
    """ランク譜面のスコアだけを pp の高い順に全部取り直し、スコアキャッシュに上書きする。

    PP の作り直し（PPv4 など）で変わるのは pp の付くランク譜面のスコアだけなので、
    全スコア（ランク外を含めて 200 ページ以上）ではなく、ランク譜面の分（約 30 ページ）だけ読む。
    途中で失敗したら status["complete"] = False を入れ、それまでに取れた分だけ反映する。
    最後まで読めたときだけ、今回返ってこなかったランク譜面の pp > 0 のスコア（サーバー側で消えたもの）を
    キャッシュから外す（prune_missing=False なら外さない。条件は _prune_missing_ranked_scores）。
    戻り値は取り直したスコアの件数。
    """
    # 呼ぶ前に別の取得（ランク譜面の取り直しなど）が失敗していたら、ランク譜面キャッシュが古いかもしれないので外さない
    prior_incomplete = status is not None and status.get("complete") is False
    cache_path = CACHE_DIR / f"beatleader_player_scores_{player_id}.json"
    url = f"{BL_BASE_URL}/player/{player_id}/scores"
    fetched: dict[str, dict] = {}
    page = 1
    max_pages: Optional[int] = None
    complete = True
    hit_page_cap = False
    ended_short = False  # 100 件未満のページで終わった
    items_read = 0
    api_total: Optional[int] = None
    while True:
        if progress is not None:
            progress(page, max_pages)
        params = {"page": str(page), "count": "100", "sortBy": "pp", "order": "desc", "type": "ranked"}
        try:
            resp = session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            _log_api_failure(
                "refresh_beatleader_ranked_scores",
                f"Request failed url={url} player_id={player_id} params={params}",
                exc,
            )
            complete = False
            break
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            break
        items_read += len(items)
        if max_pages is None:
            try:
                total = int((data.get("metadata") or {}).get("total") or 0)
                max_pages = max(1, math.ceil(total / 100)) if total > 0 else None
                api_total = total if total > 0 else None
            except (TypeError, ValueError):
                max_pages = None
        for item in items:
            if not isinstance(item, dict):
                continue
            leaderboard = item.get("leaderboard") or {}
            diff = leaderboard.get("difficulty") or {}
            lb_id_raw = leaderboard.get("id") or diff.get("leaderboardId") or diff.get("id")
            if lb_id_raw is not None:
                fetched[str(lb_id_raw)] = item
        if len(items) < 100:
            ended_short = True
            break
        if page >= 300:
            # ページ上限で打ち切った。続きがあるかもしれないので、返らなかったスコアは外さない
            hit_page_cap = True
            break
        page += 1

    if not complete and status is not None:
        status["complete"] = False
    if status is not None:
        # 一覧の終わりまで読めたか。100 件未満のページで終わった、または読んだ件数が総数に届いた後の空ページ。
        # 空ページ（総数に届く前）・ページ上限で抜けたときは False。新ランク譜面の検知済みの印に使う
        status["ranked_scores_read_to_end"] = bool(
            complete and not hit_page_cap
            and (ended_short or (api_total is not None and 0 < items_read >= api_total))
        )
        # 今回 API から読めたスコアの leaderboard id（★同期で、キャッシュに元からあるスコアと区別するため）
        status["refreshed_ids"] = sorted(fetched)
    if not fetched:
        return 0
    scores = _load_cached_player_scores(cache_path) or {}
    scores.update(fetched)
    if prune_missing:
        if not complete or hit_page_cap or prior_incomplete:
            print("BeatLeader: 取り直しを最後まで終えられなかったため、返らなかったスコアは外しません")
        else:
            _prune_missing_ranked_scores(scores, fetched, items_read, api_total)
    ordered = dict(
        sorted(scores.items(), key=lambda kv: _extract_leaderboard_timeset_from_score_item(kv[1]) or 0, reverse=True)
    )
    # fetched_at（60 日遡りの基準）は変えない（ランク譜面のスコアしか読んでいないので、差分取得を済ませたことにはならない）
    _save_cached_player_scores(
        cache_path, ordered, failed_ranges=_load_cached_player_score_failed_ranges(cache_path),
        fetched_at=_load_cached_fetched_at(cache_path),
    )
    return len(fetched)


def refresh_scores_of_newly_ranked_maps(
    player_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    status: Optional[dict] = None,
) -> int:
    """新しくランクになった譜面に自分の古いプレイがあるとき、そのスコアの pp を取り直す。

    スコアの差分取得は timeset（プレイした日時）で打ち切るので、ランクになる前の古いプレイは拾えない。
    検知は API を使わずキャッシュ同士で行う：スコアキャッシュ上で status が Ranked(3) でない譜面が、ランク譜面キャッシュにある。
    見つかったときだけ、ランク譜面のスコアを取り直す。取り直しても更新されなかった譜面（ランクから外れた譜面が
    キャッシュに残っているなど）は、スコアの譜面情報は書き換えず、「検知済み」の印（newly_ranked_checked.json）だけ
    残して毎回の再検知を防ぐ。印はランクになった日時（rankedTime）ごとなので、ランクになり直したら再び検知する。
    戻り値は検知した譜面の数。
    """
    cache_path = CACHE_DIR / f"beatleader_player_scores_{player_id}.json"
    checked_path = CACHE_DIR / "newly_ranked_checked.json"
    ranked = {str(lb.get("id")): lb for lb in _load_cached_ranked_leaderboards(CACHE_DIR / "beatleader_ranked_maps.json")}
    scores = _load_cached_player_scores(cache_path) or {}
    checked = load_newly_ranked_checked(checked_path, "beatleader", player_id)

    def _mark(k: str) -> str:
        return str(_ranked_time(ranked[k]))

    def _stale() -> list[str]:
        out = []
        for k, item in scores.items():
            if k not in ranked or not isinstance(item, dict):
                continue
            diff = (item.get("leaderboard") or {}).get("difficulty") or {}
            # ランク譜面キャッシュの側も Ranked(3) のものだけ（掃除が遅れてランク外れが残っていても書き換えない）
            if (
                diff.get("status") != 3 and ((ranked[k].get("difficulty") or {}).get("status") == 3)
                and checked.get(k) != _mark(k)
            ):
                out.append(k)
        return out

    def _star_entries():
        for k, item in scores.items():
            if k not in ranked or not isinstance(item, dict):
                continue
            ts = _extract_leaderboard_timeset_from_score_item(item)
            try:
                played = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
            except (OverflowError, OSError, ValueError):
                played = None
            yield (((item.get("leaderboard") or {}).get("difficulty") or {}).get("stars"),
                   (ranked[k].get("difficulty") or {}).get("stars"), played)

    # 窓の外で★が変わった譜面を、最近のプレイのスコアから検知する（次回の全件取り直しを前倒し）
    if flag_star_drift_if_any(_RANKED_FULL_TRAVERSAL_STATE, "beatleader", _star_entries()):
        print("BeatLeader: ランク譜面キャッシュの★が最新のスコアと食い違うため、次回全件取り直します")

    stale = _stale()
    if not stale:
        return 0
    print(f"BeatLeader: 新しくランクになった譜面に自分のスコアがある（{len(stale)} 件）。ランク譜面のスコアを取り直します")
    local_status: dict = {}
    # ここでは外さない：目的は新しくランクになった譜面の pp の取得で、直前のランク譜面の取得は差分だけなので
    # キャッシュにランクから外れた譜面が残っていることがある（外すのは PP のずれの取り直しのときだけ）
    refresh_beatleader_ranked_scores(
        player_id, session, progress=progress, status=local_status, prune_missing=False,
    )
    if local_status.get("complete") is False:
        if status is not None:
            status["complete"] = False
        return len(stale)
    if not local_status.get("ranked_scores_read_to_end"):
        # 空ページ・ページ上限で途中までしか読めていない。読んでいない譜面に印を付けない（次回また検知する）
        return len(stale)
    scores = _load_cached_player_scores(cache_path) or {}
    still = _stale()
    if still:
        # スコアは書き換えず、検知済みの印だけ残す（ランク譜面キャッシュに無くなった譜面の印は捨てる）
        marks = {k: v for k, v in checked.items() if k in ranked}
        marks.update({k: _mark(k) for k in still})
        save_newly_ranked_checked(checked_path, "beatleader", player_id, marks)
    return len(stale)


# ★・pp が変わった譜面だけ取り直す（リウェイト対策）。PP のずれ（reconcile）より小さい変化も拾う。
BL_ACCGRAPH_URL = f"{BL_BASE_URL}/player/{{player_id}}/accgraph"
#: 取り直しても食い違いが残った譜面の印（同じ食い違いで毎回取り直さないため）。
_STAR_SYNC_CHECKED = CACHE_DIR / "star_sync_checked.json"
#: pp の食い違いとみなす差（accgraph と /player/{id}/scores は同じ値を返す。2026-11-25 に 2917 件で差 0 を確認）。
_STAR_SYNC_PP_EPSILON = 0.01
#: ランク譜面キャッシュへ写す、譜面の★と AccPP / TechPP / PassPP の元になる値。
_BL_RATING_KEYS = (
    "stars", "passRating", "accRating", "techRating", "predictedAcc", "modifiersRating", "modifierValues",
)


def _bl_hash_of(item: dict) -> str:
    song = (item.get("leaderboard") or {}).get("song") or {}
    return str(song.get("hash") or "").lower()


def _bl_star_sync_targets(
    accgraph: list, scores: dict, ranked: dict[str, dict],
) -> dict[str, tuple[str, str]]:
    """accgraph（自分のランクのスコア全部の今の★・pp）とキャッシュを比べ、取り直す譜面を返す。

    戻り値は {leaderboard id: (曲の hash, 食い違いの印)}。次のどれかなら対象にする。
    - pp がスコアキャッシュと違う（リウェイト・PP の作り直しで古いスコアの pp が変わった／キャッシュに無い）。
    - 修飾子の無いスコアで、★がランク譜面キャッシュと違う（ランク譜面キャッシュの★が古い）。
      修飾子付き（SF・FS・SC など）は accgraph の★が修飾子込みの値になるので★では比べない（pp で拾う）。
    """
    targets: dict[str, tuple[str, str]] = {}
    for entry in accgraph:
        if not isinstance(entry, dict):
            continue
        lb_id = str(entry.get("leaderboardId") or "")
        song_hash = str(entry.get("hash") or "").lower()
        if not lb_id or not song_hash:
            continue
        try:
            api_pp = float(entry.get("pp") or 0)
        except (TypeError, ValueError):
            continue
        api_stars = entry.get("stars")
        item = scores.get(lb_id)
        cache_pp = _bl_score_pp(item) if isinstance(item, dict) else None
        reasons = []
        if cache_pp is None:
            reasons.append("missing")
        elif abs(cache_pp - api_pp) > _STAR_SYNC_PP_EPSILON:
            reasons.append(f"pp {cache_pp:.3f}->{api_pp:.3f}")
        lb = ranked.get(lb_id)
        ranked_stars = ((lb or {}).get("difficulty") or {}).get("stars")
        if not str(entry.get("modifiers") or "") and lb is not None and stars_differ(api_stars, ranked_stars):
            reasons.append(f"stars {ranked_stars}->{api_stars}")
        if reasons:
            targets[lb_id] = (song_hash, ";".join(reasons))
    return targets


def sync_changed_ranked_scores(
    player_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    status: Optional[dict] = None,
) -> dict:
    """リウェイトなどで★・pp が変わった譜面だけ、自分のスコアを取り直す。

    PP のずれの取り直し（collector の reconcile）は、合計 PP が 0.1 以上ずれたときしか動かない。重みの小さい
    古いスコア（0.965^n）の pp や★が変わっても合計はほとんど動かないので、そのままでは古い値が残る。そこで:
    1. /player/{id}/accgraph（1 リクエスト。自分のランクのスコア全部の今の★・pp が返る）をキャッシュと比べる。
    2. 食い違う譜面の曲ごとに /player/{id}/scores?search=<hash>&type=ranked で取り直す（曲の数だけのリクエスト）。
       曲の数がランクのスコアのページ数（約 30）を超えるときは、refresh_beatleader_ranked_scores（全ページ）の方が少ない。
    3. 取り直したスコアの譜面情報（今の★・Pass/Acc/Tech の rating）でランク譜面キャッシュを書き換える。
       書き換えた譜面があれば★のずれの印を付け、自分がプレイしていない譜面の★は次の Snapshot の全件取り直しで直す。
    取り直しても食い違いが残った譜面は star_sync_checked.json に印を残し、同じ食い違いでは取り直さない。
    検索しても目当ての譜面が返らなかったときは確認済みにせず、待ち時間（ranked_sync.star_sync_skip）の後に探し直す。
    戻り値は結果のまとめ（ログ・テスト用）。
    """
    summary: dict = {"targets": 0, "songs": 0, "requests": 0, "mode": "none", "scores_updated": 0, "maps_updated": 0}
    cache_path = CACHE_DIR / f"beatleader_player_scores_{player_id}.json"
    ranked_path = CACHE_DIR / "beatleader_ranked_maps.json"
    scores = _load_cached_player_scores(cache_path)
    ranked_pages = _load_cached_pages(ranked_path)
    if not scores or not ranked_pages:
        summary["mode"] = "no_cache"
        return summary
    ranked: dict[str, dict] = {}
    for page in ranked_pages:
        data = (page.get("data") or {}) if isinstance(page, dict) else {}
        for lb in (data.get("data") or data.get("leaderboards") or []):
            if isinstance(lb, dict) and lb.get("id") is not None:
                ranked[str(lb.get("id"))] = lb

    url = BL_ACCGRAPH_URL.format(player_id=player_id)
    try:
        if progress is not None:
            progress(1, None)
        resp = session.get(url, timeout=60)
        summary["requests"] += 1
        resp.raise_for_status()
        accgraph = resp.json()
        if not isinstance(accgraph, list):
            raise ValueError("accgraph is not a list")
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, RuntimeError) and str(exc) == "SNAPSHOT_CANCELLED":
            raise
        _log_api_failure("sync_changed_ranked_scores", f"Request failed url={url}", exc)
        print(f"BeatLeader ★/pp 同期: accgraph を取れませんでした（{_short_error_text(exc)}）。今回は何もしません")
        summary["mode"] = "accgraph_failed"
        if status is not None:
            status["complete"] = False
        return summary

    checked = load_newly_ranked_checked(_STAR_SYNC_CHECKED, "beatleader", player_id)
    found = _bl_star_sync_targets(accgraph, scores, ranked)
    targets = {k: v for k, v in found.items() if not star_sync_skip(checked.get(k), v[1])}
    summary["targets"] = len(targets)
    if not targets:
        print(f"BeatLeader ★/pp 同期: accgraph {len(accgraph)} 件とキャッシュは一致（取り直しなし）")
        if set(checked) - set(found):
            # 食い違いが消えた譜面の印は捨てる
            save_newly_ranked_checked(
                _STAR_SYNC_CHECKED, "beatleader", player_id, {k: v for k, v in checked.items() if k in found},
            )
        return summary
    hashes = sorted({h for h, _ in targets.values()})
    summary["songs"] = len(hashes)
    for k, (_, why) in list(targets.items())[:10]:
        print(f"BeatLeader ★/pp 同期: {k} {why}")
    bulk_pages = max(1, math.ceil(len(accgraph) / 100))
    refreshed: dict[str, dict] = {}
    # 検索（取り直し）を最後まで終えた対象。ここに入っていて refreshed に無い対象は「探しても返らなかった」
    searched: set[str] = set()
    complete = True
    if len(hashes) > bulk_pages:
        # 曲ごとに取るより、ランクのスコアを全部（約 bulk_pages ページ）取る方が少ない
        print(
            f"BeatLeader ★/pp 同期: 食い違い {len(targets)} 譜面（{len(hashes)} 曲）が多いため、"
            f"ランク譜面のスコアを全部取り直します（約 {bulk_pages} ページ）"
        )
        summary["mode"] = "bulk"
        local_status: dict = {}
        refresh_beatleader_ranked_scores(player_id, session, progress=progress, status=local_status, prune_missing=False)
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
        print(
            f"BeatLeader ★/pp 同期: 食い違い {len(targets)} 譜面（{len(hashes)} 曲）。曲ごとに {len(hashes)} リクエストで取り直します"
        )
        summary["mode"] = "per_song"
        scores_url = f"{BL_BASE_URL}/player/{player_id}/scores"
        for i, song_hash in enumerate(hashes, start=1):
            if progress is not None:
                progress(i, len(hashes))
            params = {"search": song_hash, "type": "ranked", "count": "100", "page": "1"}
            try:
                resp = session.get(scores_url, params=params, timeout=15)
                summary["requests"] += 1
                if getattr(resp, "status_code", None) == 429:
                    # PoliteSession が Retry-After に従ってリトライした後もなお 429。残りの曲は次回に回す（印は付けない）
                    _log_api_failure(
                        "sync_changed_ranked_scores",
                        f"rate limited (HTTP 429) after retries url={scores_url} params={params}; "
                        "the remaining songs will be refetched next time",
                    )
                    print("BeatLeader ★/pp 同期: レート制限（429）のため、残りの曲は次回に取り直します")
                    complete = False
                    break
                resp.raise_for_status()
                items = (resp.json() or {}).get("data") or []
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, RuntimeError) and str(exc) == "SNAPSHOT_CANCELLED":
                    raise
                _log_api_failure("sync_changed_ranked_scores", f"Request failed url={scores_url} params={params}", exc)
                complete = False
                continue
            searched |= {k for k, (h, _) in targets.items() if h == song_hash}
            for item in items:
                if not isinstance(item, dict) or _bl_hash_of(item) != song_hash:
                    continue
                lb_id = (item.get("leaderboard") or {}).get("id")
                if lb_id is None:
                    continue
                # 同じ曲の他の難易度も今の値なので入れる（キャッシュにある譜面か、今回の対象だけ）
                if str(lb_id) in targets or str(lb_id) in scores:
                    refreshed[str(lb_id)] = item
        if refreshed:
            scores.update(refreshed)
            ordered = dict(sorted(
                scores.items(), key=lambda kv: _extract_leaderboard_timeset_from_score_item(kv[1]) or 0, reverse=True,
            ))
            # fetched_at（60 日遡りの基準）は変えない（一部のスコアを取り直しただけで、差分取得を済ませたことにはならない）
            _save_cached_player_scores(
                cache_path, ordered, failed_ranges=_load_cached_player_score_failed_ranges(cache_path),
                fetched_at=_load_cached_fetched_at(cache_path),
            )
    summary["scores_updated"] = len(refreshed)

    # 取り直したスコアの譜面情報（今の★・rating）でランク譜面キャッシュを書き換える
    maps_updated = 0
    # refreshed は今回 API から読めたスコアだけ（全部の取り直しが途中で止まったときも、キャッシュに元からある古いスコアは
    # read_ids で除いてある）。読めた譜面は今の★で書き換える（書き換えずに確認済みの印を付けると、★が古いまま固まる）
    for lb_id, item in refreshed.items():
        lb = ranked.get(lb_id)
        new_diff = (item.get("leaderboard") or {}).get("difficulty") or {}
        if lb is None or not isinstance(new_diff, dict) or new_diff.get("status") != 3:
            continue
        diff = lb.setdefault("difficulty", {})
        if not stars_differ(diff.get("stars"), new_diff.get("stars")):
            continue
        for key in _BL_RATING_KEYS:
            if key in new_diff:
                diff[key] = new_diff[key]
        maps_updated += 1
    summary["maps_updated"] = maps_updated
    if maps_updated:
        try:
            raw = json.loads(ranked_path.read_text(encoding="utf-8"))
            raw["pages"] = ranked_pages  # 中の譜面は上で書き換え済み。fetched_at は変えない（差分取得の窓を進めない）
            atomic_write_text(ranked_path, json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"BeatLeader ★/pp 同期: ランク譜面キャッシュを保存できませんでした（{exc}）")
        # 自分がプレイした譜面で★が変わっていた＝リウェイトがあった。プレイしていない譜面の★は次回の全件取り直しで直す
        flag_star_drift(_RANKED_FULL_TRAVERSAL_STATE, "beatleader")
        print(f"BeatLeader ★/pp 同期: ランク譜面キャッシュの★を {maps_updated} 譜面書き換え、次回ランク譜面一覧を全件取り直します")

    # まだ食い違う譜面に印を残す（取り直しが失敗・429 で止まった譜面は印を変えず、次回また取り直す）
    remaining = _bl_star_sync_targets(accgraph, scores, ranked)
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
    if not_found:
        print(f"BeatLeader ★/pp 同期: 検索しても返らなかった譜面 {not_found} 件は、時間を置いて探し直します")
    save_newly_ranked_checked(_STAR_SYNC_CHECKED, "beatleader", player_id, marks)
    print(
        f"BeatLeader ★/pp 同期: スコア {len(refreshed)} 件・ランク譜面 {maps_updated} 件を更新"
        f"（リクエスト {summary['requests']} 回、残り {len(remaining)} 件）"
    )
    if not complete and status is not None:
        status["complete"] = False
    return summary


def collect_beatleader_star_stats(
    beatleader_id: str,
    session: Optional[requests.Session] = None,
    progress: Optional[Callable[[str, float], None]] = None,
    retry_failed_pages_only: bool = False,
    warning_callback: Optional[Callable[[str], None]] = None,
    use_cached_ranked: bool = False,
) -> list[StarClearStat]:
    """
    BeatLeaderのRanked譜面・プレイヤースコアから星別クリア数・NF数・平均精度を集計。

    use_cached_ranked=True なら、ランク譜面は API を呼ばずキャッシュから読む
    （スナップショットの中で直前に取得済みのとき、同じ 1 ページ目を取り直さないため）。
    """
    print("Entering collect_beatleader_star_stats")
    if not beatleader_id:
        return []

    if session is None:
        session = make_session()

    def _step(message: str, fraction: float) -> None:
        if progress is not None:
            progress(message, max(0.0, min(1.0, fraction)))

    def _leaderboard_progress(page: int, max_pages: Optional[int]) -> None:
        if max_pages and max_pages > 0:
            frac = max(0.0, min(1.0, page / max_pages))
            page_text = f"{page}/{max_pages}"
        else:
            frac = 0.0 if page <= 1 else 1.0
            page_text = f"{page}/?"
        _step(f"Collecting BeatLeader star stats: maps {page_text}", 0.45 * frac)

    def _scores_progress(page: int, max_pages: Optional[int]) -> None:
        if max_pages and max_pages > 0:
            frac = max(0.0, min(1.0, page / max_pages))
            page_text = f"{page}/{max_pages}"
        else:
            frac = 0.0 if page <= 1 else 1.0
            page_text = f"{page}/?"
        _step(f"Collecting BeatLeader star stats: scores {page_text}", 0.45 + 0.45 * frac)

    _step("Collecting BeatLeader star stats: maps...", 0.0)
    if use_cached_ranked:
        leaderboards = _load_cached_ranked_leaderboards(CACHE_DIR / "beatleader_ranked_maps.json")
    else:
        leaderboards = _get_beatleader_leaderboards_ranked(session, progress=_leaderboard_progress)
    if not leaderboards:
        return []

    _step("Collecting BeatLeader star stats: scores...", 0.45)
    scores = _get_beatleader_player_scores(
        beatleader_id,
        session,
        progress=_scores_progress,
        retry_failed_pages_only=retry_failed_pages_only,
        warning_callback=warning_callback,
    )
    stats = _build_beatleader_star_stats(leaderboards, scores, progress=progress)
    _step("Collecting BeatLeader star stats: done", 1.0)
    return stats
