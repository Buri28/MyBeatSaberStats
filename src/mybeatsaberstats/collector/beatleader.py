from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
from typing import Optional, Callable

import math
import requests
import time
import traceback

from ..snapshot import BASE_DIR, StarClearStat

CACHE_DIR = BASE_DIR / "cache"
LOG_DIR = BASE_DIR / "logs"
LOG_PATH = LOG_DIR / "beatleader_api.log"

BEATLEADER_LEADERBOARDS_URL = "https://api.beatleader.xyz/leaderboards"
BL_BASE_URL = "https://api.beatleader.xyz"


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
            path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
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


def _save_cached_player_scores(path: Path, scores: dict, failed_pages: Optional[list[int]] = None) -> None:
    """BeatLeaderプレイヤースコアをマップ形式でキャッシュファイル(JSON)として保存する。

    scores は leaderboard の id をキーにした dict を想定する。
    """

    print("Entering _save_cached_player_scores")

    payload = {
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "total_play_count": len(scores),
        "scores": scores,
        "failed_pages": sorted(set(int(p) for p in (failed_pages or []) if int(p) > 0)),
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
    fetch_until: Optional[datetime] = None,
) -> list[dict]:
    """
    BeatLeaderのRanked譜面リストをAPIから全件取得し、キャッシュも利用する。
    進捗コールバック(progress)対応。

    fetch_until が指定された場合、その日時より古い timeset の譜面が現れた時点で取得を終了する。
    sortBy は fetch_until 指定時は timestamp (新しい順) を使用する。
    """
    print("Entering _get_beatleader_leaderboards_ranked")
    cache_path = CACHE_DIR / "beatleader_ranked_maps.json"

    # fetch_until を Unix タイムスタンプに変換
    fetch_until_ts: Optional[int] = None
    if fetch_until is not None:
        fetch_until_ts = int(fetch_until.timestamp())
        print(f"BeatLeader Ranked Maps fetch_until 指定: {fetch_until.isoformat()} (Unix: {fetch_until_ts})")

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
            if resp.status_code != 404:
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
                            break
                        resp_page.raise_for_status()
                        data = resp_page.json()

                    pages.append({"page": page, "params": params, "data": data})

                    items = data.get("data") if isinstance(data, dict) else None
                    if items is None and isinstance(data, dict):
                        items = data.get("leaderboards")
                    if not isinstance(items, list) or not items:
                        break

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

                    if reached_fetch_until:
                        print(f"BL Ranked Maps: fetch_until 境界に達したため取得終了。総件数: {len(lb_by_id)}")
                        break

                    if len(items) < page_size:
                        break

                    page += 1

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


def _get_beatleader_player_scores(
    player_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    fetch_until: Optional[datetime] = None,
    retry_failed_pages_only: bool = False,
    warning_callback: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """BeatLeader のプレイヤースコア一覧を API から全件取得し、キャッシュも利用する。

    以前 collector.py 側で使用していた動作確認済みのエンドポイント
    `/player/{id}/scores` + `page/count/sortBy/order` を利用しつつ、
    ページごとの進捗を progress(page, max_pages) で通知する。

    fetch_until が指定された場合、それより古いスコアまで遡って取得する（ギャップ補完用）。
    None の場合は差分のない最初のページで停止する（通常動作）。
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
    pending_failed_pages = set(_load_cached_player_score_failed_pages(cache_path))
    if cached_scores is not None:
        print(f"BeatLeaderのキャッシュ読み込み成功: {player_id} スコア件数: {len(cached_scores)}")
        scores_by_lb_id.update(cached_scores)
    else:
        cached_scores = {}

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

    page_size = 100

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
        for attempt in range(1, 4):
            try:
                resp = session.get(url, params=params, timeout=10)
            except Exception as exc:  # noqa: BLE001
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
                _log_api_failure(
                    "_get_beatleader_player_scores",
                    f"Bad response url={resp.url} status={resp.status_code} player_id={player_id} attempt={attempt}/3",
                    exc,
                )
                retryable_status = resp.status_code in {429, 500, 502, 503, 504}
                if retryable_status and attempt < 3:
                    time.sleep(0.5 * attempt)
                    continue
                page_failed = True
                break
        return resp, data, page_failed, not_found

    def _merge_score_items(items: list[dict]) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            leaderboard = item.get("leaderboard") if isinstance(item, dict) else None
            if leaderboard is None:
                leaderboard = item
            if not isinstance(leaderboard, dict):
                continue
            diff = leaderboard.get("difficulty") or {}
            lb_id_raw = leaderboard.get("id") or diff.get("leaderboardId") or diff.get("id")
            if lb_id_raw is None:
                continue
            scores_by_lb_id[str(lb_id_raw)] = item

    if retry_failed_pages_only:
        retry_pages = sorted(pending_failed_pages)
        if not retry_pages:
            if progress is not None:
                progress(1, 1)
            return _sorted_score_items()

        for retry_index, retry_page in enumerate(retry_pages, start=1):
            _resp, data, page_failed, not_found = _fetch_scores_page(retry_page)
            if progress is not None:
                progress(retry_index, len(retry_pages))

            if not_found or page_failed or data is None:
                _warn(
                    f"BeatLeader player scores: page {retry_page} could not be recovered during snapshot; next snapshot will refetch all pages."
                )
                continue

            items = data.get("data") or data.get("scores") or []
            if not isinstance(items, list):
                _warn(
                    f"BeatLeader player scores: page {retry_page} returned invalid data during snapshot retry; next snapshot will refetch all pages."
                )
                continue

            _merge_score_items(items)
            pending_failed_pages.discard(retry_page)

        if scores_by_lb_id:
            ordered_scores = {
                lb_id: item
                for lb_id, item in sorted(
                    scores_by_lb_id.items(),
                    key=lambda kv: _extract_leaderboard_timeset_from_score_item(kv[1]) or 0,
                    reverse=True,
                )
            }
            try:
                _save_cached_player_scores(cache_path, ordered_scores, failed_pages=sorted(pending_failed_pages))
            except Exception:
                pass
            return [item for _, item in ordered_scores.items()]
        return []

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
    max_pages_bl: Optional[int] = None
    fetch_until_ts: Optional[int] = None
    if fetch_until is not None:
        fetch_until_ts = int(fetch_until.timestamp())
        print(f"BeatLeader fetch_until 指定: {fetch_until.isoformat()} (Unix: {fetch_until_ts})")
    force_full_refresh = fetch_until_ts is None and bool(pending_failed_pages)
    if force_full_refresh:
        _warn(
            "BeatLeaderスコア取得: 前回の未回収ページが残っているため、"
            "今回は差分取得を行わず全ページを再取得します。"
        )
    reached_fetch_until = False

    while True:
        _resp, data, page_failed, not_found = _fetch_scores_page(page)

        if not_found:
            break

        if page_failed:
            pending_failed_pages.add(page)
            if page <= 1:
                _warn("BeatLeader player scores: page 1 fetch failed; using existing cache for this snapshot.")
                try:
                    _save_cached_player_scores(cache_path, scores_by_lb_id, failed_pages=sorted(pending_failed_pages))
                except Exception:
                    pass
                return _sorted_score_items()
            _warn(
                f"BeatLeader player scores: page {page} fetch failed; will retry failed pages during BeatLeader star stats."
            )
            if max_pages_bl is not None and page >= max_pages_bl:
                break
            page += 1
            continue

        if data is None:
            break

        pending_failed_pages.discard(page)

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

        page_has_diff = False

        for item in items:
            if not isinstance(item, dict):
                print("Skipping invalid score item (not a dict)")
                continue

            if fetch_until_ts is not None:
                player_ts = _extract_player_timeset_from_bl_score_item(item)
                if player_ts is not None and player_ts < fetch_until_ts:
                    print(f"BeatLeader fetch_until 境界に到達: timeset={player_ts} < fetch_until_ts={fetch_until_ts} ページ: {page}")
                    reached_fetch_until = True
                    break

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
            old_item = cached_scores.get(lb_id)
            if old_item is None:
                page_has_diff = True
                scores_by_lb_id[lb_id] = item
            else:
                old_ts = _extract_leaderboard_timeset_from_score_item(old_item)
                new_ts = _extract_leaderboard_timeset_from_score_item(item)
                old_pp = _get_score_pp(old_item)
                new_pp = _get_score_pp(item)
                pp_changed = abs(new_pp - old_pp) > 0.01

                if (new_ts is not None and (old_ts is None or new_ts > old_ts)) or pp_changed:
                    page_has_diff = True
                    scores_by_lb_id[lb_id] = item
                else:
                    if lb_id not in scores_by_lb_id:
                        scores_by_lb_id[lb_id] = old_item

        if progress is not None:
            progress(page, max_pages_bl)

        if reached_fetch_until:
            print(f"BeatLeader fetch_until 境界に到達したため取得を終了します。ページ: {page} 総件数: {len(scores_by_lb_id)}")
            break

        print(f"Completed fetching page {page} of BeatLeader scores. ranked_play_count_target: {total_play_count_target}, page_has_diff: {page_has_diff}")
        if fetch_until_ts is None and total_play_count_target is not None and not force_full_refresh:
            current_count = len(scores_by_lb_id)
            print(f"Current cached score count: {current_count}, Target ranked play count: {total_play_count_target} page_has_diff: {page_has_diff}")
            if current_count >= total_play_count_target and not page_has_diff and not pending_failed_pages:
                break

        if len(items) < page_size or (max_pages_bl is not None and page >= max_pages_bl):
            break

        page += 1

    if scores_by_lb_id:
        sorted_items = sorted(
            scores_by_lb_id.items(),
            key=lambda kv: _extract_leaderboard_timeset_from_score_item(kv[1]) or 0,
            reverse=True,
        )
        ordered_scores: dict[str, dict] = {lb_id: item for lb_id, item in sorted_items}
        try:
            _save_cached_player_scores(cache_path, ordered_scores, failed_pages=sorted(pending_failed_pages))
        except Exception:
            pass
        return [item for _, item in sorted_items]

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


def collect_beatleader_star_stats(
    beatleader_id: str,
    session: Optional[requests.Session] = None,
    progress: Optional[Callable[[str, float], None]] = None,
    retry_failed_pages_only: bool = False,
    warning_callback: Optional[Callable[[str], None]] = None,
) -> list[StarClearStat]:
    """
    BeatLeaderのRanked譜面・プレイヤースコアから星別クリア数・NF数・平均精度を集計。
    """
    print("Entering collect_beatleader_star_stats")
    if not beatleader_id:
        return []

    if session is None:
        session = requests.Session()

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
    leaderboards = _get_beatleader_leaderboards_ranked(session, progress=_leaderboard_progress)
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

    _step("Collecting BeatLeader star stats: scores...", 0.45)
    scores = _get_beatleader_player_scores(
        beatleader_id,
        session,
        progress=_scores_progress,
        retry_failed_pages_only=retry_failed_pages_only,
        warning_callback=warning_callback,
    )

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
    # ★別 PP 合計
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
            # キャッシュ外のマップ: pp > 0 のスコアのみ対象（API の rankedPlayCount 定義と合わせる）
            try:
                pp_pre = float(item.get("pp") or 0)
            except (TypeError, ValueError):
                pp_pre = 0.0
            if not (math.isfinite(pp_pre) and pp_pre > 0):
                continue
            try:
                stars_pre = float(diff.get("stars") or diff.get("difficultyRating") or 0)
            except (TypeError, ValueError):
                stars_pre = 0.0
            if not (math.isfinite(stars_pre) and stars_pre > 0):
                continue
            star_bucket_pre = max(0, int(stars_pre))
            leaderboard_star_bucket[lb_id] = star_bucket_pre
            # star_map_count には加算しない（Maps 列はキャッシュ内の Ranked 譜面数のみ）

        star_bucket = leaderboard_star_bucket[lb_id]

        state = per_leaderboard.get(lb_id)
        if state is None:
            state = {"star": star_bucket, "clear": False, "nf": False, "ss": False, "na": False, "best_acc": None, "best_acc_left": None, "best_acc_right": None, "has_fc": False, "best_pp": None}
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
        is_na = "NA" in mods_upper

        if is_nf:
            state["nf"] = True
        elif is_na:
            state["na"] = True
        elif is_ss:
            state["ss"] = True
            # BeatLeader では SS（Slower Song）も PP 対象（score.pp に MOD 補正済みで反映）
            if isinstance(score_info, dict):
                try:
                    pp_val = float(score_info.get("pp") or 0)
                    if math.isfinite(pp_val) and pp_val > 0:
                        prev_pp = state.get("best_pp")
                        if prev_pp is None or pp_val > prev_pp:
                            state["best_pp"] = pp_val
                except (TypeError, ValueError):
                    pass
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

            # クリア済みスコアの pp 値を記録（最大値を保持）
            if isinstance(score_info, dict):
                try:
                    pp_val = float(score_info.get("pp") or 0)
                    if math.isfinite(pp_val) and pp_val > 0:
                        prev_pp = state.get("best_pp")
                        if prev_pp is None or pp_val > prev_pp:
                            state["best_pp"] = pp_val
                except (TypeError, ValueError):
                    pass

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
                # L/R acc の計算: 2 * acc * accL / (accL + accR)
                # accLeft/accRight はヒットしたノーツのみの平均点（ミス除外）なので、
                # 単純に /115 * acc とするとミスが二重にペナルティされる。
                # ノーツが L/R 均等と仮定すると:
                #   acc = hit_rate * (accL + accR) / (2*115) * 100
                #   L_acc = hit_rate * accL / 115 * 100
                #         = 2 * acc * accL / (accL + accR)
                # これにより avg(L_acc, R_acc) = acc が成立する。
                if (best_left is not None and best_right is not None
                        and math.isfinite(float(best_left))
                        and math.isfinite(float(best_right))):
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

    # PP を全スコア降順でソートし、重み 0.965^(rank-1) を掛けて★別に集計
    cleared_pp_entries_bl: list[tuple[int, float]] = []
    for state in per_leaderboard.values():
        # SS スコアも BeatLeader では PP 対象なので clear と同様に含める
        if not (bool(state["clear"]) or bool(state["ss"])):
            continue
        pp_val = state.get("best_pp")
        if isinstance(pp_val, (int, float)) and math.isfinite(float(pp_val)) and float(pp_val) > 0:
            cleared_pp_entries_bl.append((int(state["star"]), float(pp_val)))
    cleared_pp_entries_bl.sort(key=lambda x: x[1], reverse=True)
    for rank, (star_bucket, pp_val) in enumerate(cleared_pp_entries_bl, start=1):
        weight = 0.965 ** (rank - 1)
        star_pp_sum[star_bucket] += pp_val * weight

    # ★帯内ローカルランクで Solo PP を計算
    star_pp_solo_sum: dict[int, float] = defaultdict(float)
    star_pp_list_bl: dict[int, list[float]] = defaultdict(list)
    for star_bucket, pp_val in cleared_pp_entries_bl:
        star_pp_list_bl[star_bucket].append(pp_val)
    for star_bucket, pp_vals in star_pp_list_bl.items():
        for local_rank, pp_v in enumerate(sorted(pp_vals, reverse=True), start=1):
            star_pp_solo_sum[star_bucket] += pp_v * (0.965 ** (local_rank - 1))

    # キャッシュ内の★帯 + キャッシュ外プレイがある★帯の両方を対象にする
    all_stars_bl = set(star_map_count.keys()) | set(star_clear_count.keys()) | set(star_nf_count.keys()) | set(star_ss_count.keys()) | set(star_na_count.keys())
    stats: list[StarClearStat] = []
    for star in sorted(all_stars_bl):
        map_count = star_map_count.get(star, 0)  # 0 = Ranked キャッシュ外(プレイ記録のみある)
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

        # クリア率 (0.0-1.0): 全 Ranked 譜面に対する通常クリアの割合
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

    _step("Collecting BeatLeader star stats: done", 1.0)
    return stats
