
from __future__ import annotations
def _is_steam_id(value: str | None) -> bool:
    return isinstance(value, str) and value.isdigit() and len(value) == 17
from ..ranking_view import _load_player_index

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
from typing import Optional, Dict, TypedDict, Callable

import math
from collections import defaultdict

import requests

from ..snapshot import BASE_DIR, StarClearStat, Snapshot
from ..scoresaber import ScoreSaberPlayer, fetch_players
from .scoresaber import _collect_star_stats_from_scoresaber
from .scoresaber import _get_scoresaber_player_scores
from .scoresaber import _get_scoresaber_leaderboards_ranked
from .scoresaber import _get_scoresaber_player_stats
from .scoresaber import _fetch_scoresaber_player_basic
from .scoresaber import _load_cached_pages, _save_cached_pages
from .beatleader import (
    _get_beatleader_player_scores as _bl_get_beatleader_player_scores,
    collect_beatleader_star_stats as _bl_collect_beatleader_star_stats,
)
from ..beatleader import BeatLeaderPlayer, fetch_player as fetch_bl_player, fetch_players_ranking
from .map_store import MapStore

from ..accsaber import (
    AccSaberPlayer,
    fetch_overall,
    fetch_true,
    fetch_standard,
    fetch_tech,
    ACCSABER_MIN_AP_GLOBAL,
    ACCSABER_MIN_AP_SKILL,
)
from .accsaber import (
    _accsaber_profile_exists,
    _load_list_cache,
    _find_accsaber_for_scoresaber_id,
    _find_accsaber_skill_for_scoresaber_id,
)

# キャッシュディレクトリ(app.py と同じ BASE_DIR / "cache" を利用)
CACHE_DIR = BASE_DIR / "cache"


SCORESABER_MIN_PP_GLOBAL = 4000.0
BEATLEADER_MIN_PP_GLOBAL = 5000.0

# SCORESABER_LEADERBOARDS_URL = "https://scoresaber.com/api/leaderboards"
# SCORESABER_PLAYER_SCORES_URL = "https://scoresaber.com/api/player/{player_id}/scores"
# SCORESABER_PLAYER_FULL_URL = "https://scoresaber.com/api/player/{player_id}/full"

BEATLEADER_LEADERBOARDS_URL = "https://api.beatleader.xyz/leaderboards"
BL_BASE_URL = "https://api.beatleader.xyz"




def _save_player_index(index: Dict[str, Dict[str, object]]) -> None:
    """players_index.json を app.MainWindow と同一フォーマットで保存する。"""

    path = CACHE_DIR / "players_index.json"
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, object]] = []
        for steam_id, entry in index.items():
            row: dict[str, object] = {"steam_id": steam_id}
            ss = entry.get("scoresaber")
            bl = entry.get("beatleader")
            if isinstance(ss, ScoreSaberPlayer):
                row["scoresaber"] = asdict(ss)
            if isinstance(bl, BeatLeaderPlayer):
                row["beatleader"] = asdict(bl)
            rows.append(row)

        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        return


def _save_list_cache(path: Path, items) -> None:
    """汎用のリストキャッシュセーバー。dataclass のリストを JSON に保存する。"""

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        serializable = [asdict(x) for x in items]
        path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        return


def rebuild_player_index_from_global() -> None:
    """scoresaber_ALL / beatleader_ALL から players_index.json を再構築する。

    app.MainWindow._rebuild_player_index_from_global と同等の処理を
    GUI 非依存の関数として切り出したもの。
    """

    def _norm_name(name: str) -> str:
        return "".join(ch for ch in name.lower() if ch.isalnum())

    # ScoreSaber グローバル
    ss_global_path = CACHE_DIR / "scoresaber_ranking.json"
    ss_global: list[ScoreSaberPlayer] = []
    if ss_global_path.exists():
        try:
            ss_global = _load_list_cache(ss_global_path, ScoreSaberPlayer)
        except Exception:  # noqa: BLE001
            ss_global = []

    # BeatLeader グローバル
    bl_global_path = CACHE_DIR / "beatleader_ranking.json"
    bl_global: list[BeatLeaderPlayer] = []
    if bl_global_path.exists():
        try:
            data = json.loads(bl_global_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                bl_global = [BeatLeaderPlayer(**item) for item in data if isinstance(item, dict)]
        except Exception:  # noqa: BLE001
            bl_global = []

    index: Dict[str, Dict[str, object]] = {}

    # 名前+国コードごとのマップも作成しておき、ID が一致しないプレイヤー同士を
    # 後段で安全な範囲で紐付ける（Marsh_era / A-tach などのケースを想定）
    ss_by_name_country: Dict[tuple[str, str], list[ScoreSaberPlayer]] = {}
    for p in ss_global:
        if not p.name or not p.country:
            continue
        key = (_norm_name(p.name), p.country.upper())
        ss_by_name_country.setdefault(key, []).append(p)



def _load_accsaber_players() -> list[AccSaberPlayer]:
    """AccSaber グローバルリーダーボードキャッシュをすべて読み込む。"""

    acc_path = CACHE_DIR / "accsaber_ranking.json"
    return _load_list_cache(acc_path, AccSaberPlayer)


def _find_accsaber_skill_for_scoresaber_id(
    scoresaber_id: str,
    fetch_func,
    session: Optional[requests.Session] = None,
    max_pages: int = 200,
) -> Optional[AccSaberPlayer]:
    """AccSaber の True / Standard / Tech リーダーボードから、指定IDのプレイヤーを探す。

    fetch_func には fetch_true / fetch_standard / fetch_tech のいずれかを渡す想定。
    見つからなければ None。
    """

    if not scoresaber_id:
        return None

    # 単一プレイヤーの AP / Playcount を取る用途なので、
    # 指定された max_pages の範囲でページング検索する。
    # ただし、そのリーダーボードの末尾プレイヤーの AP が
    # ACCSABER_MIN_AP_SKILL を下回った時点で、それ以降のページには
    # しきい値以上のプレイヤーが存在しないとみなして打ち切る。

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
        except Exception:  # noqa: BLE001
            break

        if not players:
            break

        for p in players:
            if getattr(p, "scoresaber_id", None) == scoresaber_id:
                return p

        # 末尾プレイヤーの AP が 3000 未満になったら、
        # それ以降のページには 3000AP 以上のプレイヤーはいないとみなして打ち切る。
        last_ap = _parse_ap(getattr(players[-1], "total_ap", ""))
        if last_ap < ACCSABER_MIN_AP_SKILL:
            break

    return None

def _save_cached_pages(path: Path, pages: list[dict]) -> None:
    payload = {
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "pages": pages,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_global_rank_caches(
    session: Optional[requests.Session] = None,
    progress: Optional[Callable[[str, float], None]] = None,
    steam_id: Optional[str] = None,
) -> None:
    """ScoreSaber / BeatLeader / AccSaber のランキングキャッシュを用意する。

    steam_id が指定されている場合は、以下のフローでキャッシュを構築する:

    1. AccSaber の Overall ランキングから 10000AP 以上を accsaber_ranking.json に保存
    2. steam_id から国籍 (country) を特定する
    3. 特定した国籍の ScoreSaber ランクから 4000pp 以上を scoresaber_ranking.json に保存
    4. 特定した国籍の BeatLeader ランクから 5000pp 以上を beatleader_ranking.json に保存
    5. 上記キャッシュから players_index.json を再構築

    steam_id が None の場合は、従来どおりグローバルランキング (ALL) を対象にキャッシュを構築する。
    """

    if session is None:
        session = requests.Session()

    def _step(ratio: float, message: str) -> None:
        if progress is not None:
            progress(message, max(0.0, min(1.0, ratio)))

    ss_rank_path = CACHE_DIR / "scoresaber_ranking.json"
    bl_rank_path = CACHE_DIR / "beatleader_ranking.json"
    acc_rank_path = CACHE_DIR / "accsaber_ranking.json"

    # steam_id が指定されていない場合のみ、「既に 3 種のキャッシュが揃っていれば何もしない」従来挙動を維持する
    if steam_id is None and ss_rank_path.exists() and bl_rank_path.exists() and acc_rank_path.exists():
        return

    # この呼び出し対象のプレイヤーが AccSaber に参加していない場合は、
    # AccSaber のリーダーボード取得自体をスキップする（Scoresaber/BeatLeader は通常どおり更新）。
    skip_accsaber = False
    if steam_id and _is_steam_id(steam_id):
        try:
            if not _accsaber_profile_exists(steam_id, session):
                skip_accsaber = True
        except Exception:  # noqa: BLE001
            skip_accsaber = False

    # まずは AccSaber overall (10000AP 以上) を最新化する
    if not skip_accsaber:
        try:
            acc_players: list[AccSaberPlayer] = []
            max_pages = 200

            def _parse_ap(text: str | None) -> float:
                if not text:
                    return 0.0
                t = text.replace(",", "")
                import re as _re

                m = _re.search(r"[-+]?\d*\.?\d+", t)
                if not m:
                    return 0.0
                try:
                    return float(m.group(0))
                except ValueError:
                    return 0.0

            for page in range(1, max_pages + 1):
                # 0.00〜0.30 を AccSaber フェーズとして使う
                phase_frac = min(1.0, page / max_pages)
                _step(0.0 + 0.30 * phase_frac, f"Fetching AccSaber overall ranking... (page {page})")

                page_players = fetch_overall(country=None, page=page, session=session)
                if not page_players:
                    break
                for p in page_players:
                    ap_value = _parse_ap(getattr(p, "total_ap", ""))
                    if ap_value >= ACCSABER_MIN_AP_GLOBAL:
                        acc_players.append(p)

                # 最後のプレイヤーの AP がしきい値を下回ったら、それ以降のページも対象外とみなして打ち切る
                last_ap = _parse_ap(getattr(page_players[-1], "total_ap", ""))
                if last_ap < ACCSABER_MIN_AP_GLOBAL:
                    break

            # Overall 10000AP 以上のプレイヤー集合に対して、True / Standard / Tech の AP だけを埋める
            by_id: dict[str, AccSaberPlayer] = {}
            for p in acc_players:
                sid = getattr(p, "scoresaber_id", None)
                if not sid:
                    continue
                by_id[str(sid)] = p

            def _enrich_skill(leaderboard_fetch, attr_name: str, label: str) -> None:
                if not by_id:
                    return

                remaining_ids: set[str] = set(by_id.keys())
                max_pages_skill = 200
                for page in range(1, max_pages_skill + 1):
                    if not remaining_ids:
                        break

                    # 0.00〜0.30 の中で簡易的に進捗を動かす（詳細な割合は気にしない）
                    _step(0.15, f"Fetching AccSaber {label} AP... (page {page})")

                    try:
                        skill_players = leaderboard_fetch(country=None, page=page, session=session)
                    except Exception:  # noqa: BLE001
                        break
                    if not skill_players:
                        break

                    for sp in skill_players:
                        sid = getattr(sp, "scoresaber_id", None)
                        if not sid:
                            continue
                        # AP が 3000 未満のプレイヤーはスキルランキング対象外とする
                        ap_value = _parse_ap(getattr(sp, "total_ap", ""))
                        if ap_value < ACCSABER_MIN_AP_SKILL:
                            continue

                        sid_str = str(sid)
                        if sid_str not in remaining_ids:
                            continue
                        target = by_id.get(sid_str)
                        if target is None:
                            continue
                        # skill 側の total_ap を対応するフィールドにコピーする
                        setattr(target, attr_name, getattr(sp, "total_ap", ""))
                        remaining_ids.discard(sid_str)

                    # 最後のプレイヤーの AP がしきい値を下回ったら、それ以降のページも対象外とみなして打ち切る
                    last_ap_skill = _parse_ap(getattr(skill_players[-1], "total_ap", ""))
                    if last_ap_skill < ACCSABER_MIN_AP_SKILL:
                        break

            try:
                _enrich_skill(fetch_true, "true_ap", "True")
                _enrich_skill(fetch_standard, "standard_ap", "Standard")
                _enrich_skill(fetch_tech, "tech_ap", "Tech")
            except Exception:  # noqa: BLE001
                # Skill AP 詳細取得に失敗しても Overall 自体は使えるようにする
                pass
            map_store_instance = MapStore()
            map_store_instance.acc_players = {p.scoresaber_id: p for p in acc_players if p.scoresaber_id}   
            _save_list_cache(acc_rank_path, acc_players)
        except Exception:  # noqa: BLE001
            _step(0.30, "Failed to fetch AccSaber ranking (continuing)...")

    # steam_id から国籍を特定する (あれば大文字 2 文字コード)
    target_country: Optional[str] = None
    if steam_id:
        try:
            existing_index = _load_player_index()
        except Exception:  # noqa: BLE001
            existing_index = {}

        entry = existing_index.get(steam_id)
        if isinstance(entry, dict):
            ss_entry = entry.get("scoresaber")
            if isinstance(ss_entry, ScoreSaberPlayer) and ss_entry.country:
                target_country = ss_entry.country.upper()
            else:
                bl_entry = entry.get("beatleader")
                if isinstance(bl_entry, BeatLeaderPlayer) and bl_entry.country:
                    target_country = bl_entry.country.upper()

        # players_index に無い場合は、API から直接プレイヤー情報を取得して国籍を推定する
        if not target_country and _is_steam_id(steam_id):
            try:
                ss_basic = _fetch_scoresaber_player_basic(steam_id, session)
            except Exception:  # noqa: BLE001
                ss_basic = None
            if ss_basic is not None and ss_basic.country:
                target_country = ss_basic.country.upper()
            map_store_instance = MapStore()
            map_store_instance.ss_basic_info[steam_id] = ss_basic

            if not target_country:
                try:
                    bl_basic = fetch_bl_player(steam_id, session=session)
                except Exception:  # noqa: BLE001
                    bl_basic = None
                if bl_basic is not None and bl_basic.country:
                    target_country = bl_basic.country.upper()
                map_store_instance = MapStore()
                map_store_instance.bl_basic_info[steam_id] = bl_basic

    # ScoreSaber (4000pp 以上) - target_country があればその国に絞る
    try:
        label_country = target_country or "ALL"
        _step(0.30, f"Fetching ScoreSaber rankings ({label_country})... (page 1)")
        ss_players: list[ScoreSaberPlayer] = []
        max_pages_ss = 200
        for page in range(1, max_pages_ss + 1):
            page_players = fetch_players(country=target_country, page=page, session=session)
            if not page_players:
                break

            for p in page_players:
                # 念のため country フィルタもローカル側で確認する
                if target_country and (p.country or "").upper() != target_country:
                    continue
                if p.pp >= SCORESABER_MIN_PP_GLOBAL:
                    ss_players.append(p)

            if page_players[-1].pp < SCORESABER_MIN_PP_GLOBAL:
                break

            # 0.30〜0.65 を ScoreSaber フェーズとして使う
            phase_frac = min(1.0, page / max_pages_ss)
            _step(0.30 + 0.35 * phase_frac, f"Fetching ScoreSaber rankings ({label_country})... (page {page})")

        _save_list_cache(ss_rank_path, ss_players)
    except Exception:  # noqa: BLE001
        _step(0.65, "Failed to fetch ScoreSaber rankings (continuing)...")

    # BeatLeader (5000pp 以上) - target_country があればローカルフィルタで絞る
    try:
        bl_all_players = fetch_players_ranking(
            min_pp=BEATLEADER_MIN_PP_GLOBAL,
            session=session,
            progress=lambda page, max_pages: _step(
                0.65 + 0.25 * min(1.0, page / float(max_pages or 1)),
                f"Fetching BeatLeader rankings... (page {page})",
            ),
        )

        if target_country:
            bl_players = [
                p for p in bl_all_players if (p.country or "").upper() == target_country
            ]
        else:
            bl_players = bl_all_players

        _save_list_cache(bl_rank_path, bl_players)
    except Exception:  # noqa: BLE001
        _step(0.90, "Failed to fetch BeatLeader rankings (continuing)...")

    # プレイヤーインデックスを再構築
    try:
        _step(0.95, "Rebuilding player index...")
        rebuild_player_index_from_global()
        _step(1.0, "Ranking caches ready.")
    except Exception:  # noqa: BLE001
        _step(1.0, "Failed to rebuild player index.")





def _get_beatleader_leaderboards_ranked(
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> list[dict]:
    """BeatLeader の Ranked leaderboards をキャッシュ付きで全件取得する。

    ScoreSaber の scoresaber_ranked_maps.json と同様に、
    beatleader_ranked_maps.json を cache ディレクトリに作成する。
    progress が指定されている場合は 0.0〜1.0 の範囲で簡易進捗をコールバックする。
    """

    cache_path = CACHE_DIR / "beatleader_ranked_maps.json"

    page_size = 100

    cached_pages = _load_cached_pages(cache_path)

    # 既存キャッシュが「type=Ranked」指定で取得されたものかを確認し、
    # そうでなければ捨てて再取得する（古いフォーマットのキャッシュ対策）。
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

    # 1. キャッシュがある場合はそれを読み込み、新しいランク譜面があるかだけ 1ページ目で確認する
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

        # キャッシュ側の total をメタデータから取得（なければ単純に件数を total とみなす）
        cached_total = len(leaderboards)
        if pages:
            first_meta = (pages[0].get("data") or {}).get("metadata") or {}
            try:
                cached_total = int(first_meta.get("total", cached_total))
            except (TypeError, ValueError):
                cached_total = len(leaderboards)

        # 1ページだけ最新のメタデータを取りに行き、total が増えていなければキャッシュをそのまま返す
        try:
            params_first = {
                "page": "1",
                "count": str(page_size),
                "type": "Ranked",
                "sortBy": "stars",
                "order": "desc",
            }
            resp = session.get(BEATLEADER_LEADERBOARDS_URL, params=params_first, timeout=10)
            print(
                "... "
                f"URL: {resp.url} params: {params_first}"
            )
            if resp.status_code != 404:
                resp.raise_for_status()
                data_first = resp.json()
                meta = data_first.get("metadata") or {}
                try:
                    new_total = int(meta.get("total", cached_total))
                except (TypeError, ValueError):
                    new_total = cached_total

                # total が増えていなければキャッシュをそのまま利用
                if new_total <= cached_total:
                    if progress is not None:
                        progress(1, 1)
                    return leaderboards

                # total が増えている場合は、安全のためすべてのページを再取得する
                pages = []
                leaderboards = []

                page = 1
                while True:
                    # 対象の国籍を指定する
                    params = {
                        "page": str(page),
                        "count": str(page_size),
                        "type": "Ranked",
                        "sortBy": "stars",
                        "order": "desc",
                        # 国籍対象を明示的に指定
                        # "countries": "",
                    }
                    if page == 1:
                        data = data_first
                    else:
                        resp_page = session.get(BEATLEADER_LEADERBOARDS_URL, params=params, timeout=10)
                        print(
                            "Fetching BeatLeader leaderboards page "
                            f"{page} for star stats... URL: {resp_page.url} params: {params}"
                        )
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
                    except Exception:  # noqa: BLE001
                        pass

                if progress is not None:
                    progress(page, None)
                return leaderboards
        except Exception:  # noqa: BLE001
            # メタデータ確認に失敗した場合は、既存キャッシュをそのまま返す
            if progress is not None:
                progress(1, 1)
            return leaderboards

        # 念のためキャッシュを返す
        if progress is not None:
            progress(1, 1)
        return leaderboards

    # 2. キャッシュが無い場合は API をページングして取得し、その全レスポンスを保存する
    pages: list[dict] = []
    leaderboards: list[dict] = []

    page = 1

    while True:
        params = {
            "page": str(page),
            "count": str(page_size),
            "type": "Ranked",
            "sortBy": "stars",
            "order": "desc",
            # "countries": "",  # 国籍対象を明示的に指定
        }
        try:
            resp = session.get(BEATLEADER_LEADERBOARDS_URL, params=params, timeout=10)
            print(
                f"Fetching BeatLeader leaderboards page {page} for star stats... "
                f"URL: {resp.url} params: {params}"
            )
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

        items = data.get("data") if isinstance(data, dict) else None
        if items is None and isinstance(data, dict):
            items = data.get("leaderboards")
        if not isinstance(items, list) or not items:
            break

        leaderboards.extend(lb for lb in items if isinstance(lb, dict))

        if progress is not None:
            # 最大ページ数が分からないので、page/?, として通知
            progress(page, None)

        if len(items) < page_size:
            break

        page += 1

    if pages:
        try:
            _save_cached_pages(cache_path, pages)
        except Exception:  # noqa: BLE001
            pass

    if progress is not None:
        progress(page, None)

    return leaderboards



def _get_beatleader_player_scores(
    beatleader_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> list[dict]:
    """BeatLeader のプレイヤースコア取得を beatleader.py 側の実装に委譲するラッパー。

    実際の API 呼び出しとキャッシュ処理は
    src/mybeatsaberstats/collector/beatleader.py 内の
    _get_beatleader_player_scores に集約する。
    """

    if not beatleader_id:
        return []

    return _bl_get_beatleader_player_scores(beatleader_id, session, progress)


# def _extract_scoresaber_accuracy(score_info: dict) -> Optional[float]:
#     """ScoreSaber のスコア情報から精度(%)を推定して返す。

#     - accuracy / acc フィールドがあればそれを優先
#     - 0.0-1.0 とみなせる値は 100 倍
#     - それ以外は 0-100 とみなし、範囲外は maxScore/baseScore から再計算を試みる
#     取得できなければ None。
#     """

#     if not isinstance(score_info, dict):
#         return None

#     try:
#         acc = score_info.get("accuracy")
#         if acc is None:
#             acc = score_info.get("acc")

#         if acc is not None:
#             acc_f = float(acc)
#             if not math.isfinite(acc_f) or acc_f <= 0:
#                 acc_f = 0.0
#             if acc_f > 0.0:
#                 # 0-1 の場合は百分率に変換
#                 if acc_f <= 1.0:
#                     return acc_f * 100.0
#                 # 0-100 をそのまま利用
#                 if acc_f <= 100.0:
#                     return acc_f
#                 # 0-10000 くらいのケースは 100 で割る
#                 if acc_f <= 10000.0:
#                     return acc_f / 100.0

#         base = score_info.get("baseScore")
#         max_score = score_info.get("maxScore")
#         if base is None:
#             base = score_info.get("score")
#         if base is None or max_score is None:
#             return None

#         base_f = float(base)
#         max_f = float(max_score)
#         if not math.isfinite(base_f) or not math.isfinite(max_f) or max_f <= 0:
#             return None

#         return max(0.0, min(100.0, base_f / max_f * 100.0))
#     except (TypeError, ValueError):  # noqa: BLE001
#         return None


def _extract_beatleader_accuracy(score_info: dict) -> Optional[float]:
    """BeatLeader のスコア情報から精度(%)を推定して返す。

    BeatLeader 側も accuracy / acc が 0.0-1.0 の割合で入っているケースを想定し、
    それ以外は ScoreSaber と同様に score/maxScore から再計算を試みる。
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
    except (TypeError, ValueError):  # noqa: BLE001
        return None


def collect_beatleader_star_stats(beatleader_id: str, session: Optional[requests.Session] = None) -> list[StarClearStat]:
    """BeatLeader ★別統計収集を beatleader.py 側の実装に委譲するラッパー。"""

    return _bl_collect_beatleader_star_stats(beatleader_id, session)
#     except Exception:  # noqa: BLE001
#         return None

#     try:
#         data = resp.json()
#     except Exception:  # noqa: BLE001
#         return None

#     info = data.get("playerInfo") or data.get("player") or data
#     if not isinstance(info, dict):
#         return None

#     try:
#         pid = str(info.get("id") or scoresaber_id)
#         name = str(info.get("name") or "")
#         country = str(info.get("country") or "")
#         pp_val = info.get("pp") or info.get("ppAcc") or 0.0
#         pp = float(pp_val)
#         global_rank_val = info.get("rank") or info.get("globalRank") or 0
#         global_rank = int(global_rank_val)
#         country_rank_val = info.get("countryRank") or 0
#         country_rank = int(country_rank_val)
#     except (TypeError, ValueError):  # noqa: BLE001
#         return None

#     return ScoreSaberPlayer(
#         id=pid,
#         name=name,
#         country=country,
#         pp=pp,
#         global_rank=global_rank,
#         country_rank=country_rank,
#     )


def _get_beatleader_player_stats(player_id: str, session: requests.Session) -> dict:
    """BeatLeader の /player/{id} からスコア統計を取得する。

    戻り値は scoreStats 部分の dict。失敗した場合は空 dict を返す。
    """

    if not player_id:
        return {}

    url = f"{BL_BASE_URL}/player/{player_id}"
    try:
        resp = session.get(url, timeout=10)
        print(f"Fetching BeatLeader player stats... URL: {resp.url}")
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        return {}

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {}

    stats = data.get("scoreStats")
    if isinstance(stats, dict):
        return stats
    return {}


#     star_ss_count: dict[int, int] = defaultdict(int)
#     # ★別の平均精度算出用
#     star_acc_sum: dict[int, float] = defaultdict(float)
#     star_acc_count: dict[int, int] = defaultdict(int)

#     # leaderboardId ごとに「クリア有り / NF有り / SS有り」とベスト精度を記録する
#     class _PerLeaderboardState(TypedDict):
#         star: int
#         clear: bool
#         nf: bool
#         ss: bool
#         best_acc: Optional[float]

#     per_leaderboard: dict[str, _PerLeaderboardState] = {}

#     scores = _get_scoresaber_player_scores(scoresaber_id, session)
#     print(f"取得したスコア件数: {len(scores)}")
#     for item in scores:
#         score_info = item.get("score") if isinstance(item, dict) else None
#         leaderboard = item.get("leaderboard") if isinstance(item, dict) else None

#         if leaderboard is None and isinstance(item, dict):
#             print(f"leaderboard 情報が score オブジェクトに無いケース発生。item={item}")
#             leaderboard = item
#         # print(f"処理中 score item: {leaderboard.get('id') if isinstance(leaderboard, dict) else 'N/A'}")

#         if not isinstance(leaderboard, dict):
#             print("leaderboard 情報が辞書型でないケース発生。スキップ")
#             continue

#         diff = leaderboard.get("difficulty") or {}

#         lb_id_raw = leaderboard.get("id") or diff.get("leaderboardId")
#         if lb_id_raw is None:
#             continue
#         lb_id = str(lb_id_raw)
#         tmp_stars = leaderboard.get("stars")  # or diff.get("stars")
#         ranked_flag = leaderboard.get("ranked")
#         if ranked_flag is False:
#             continue

#         # if lb_id == "685895" or lb_id == "682135":
#         #     print(f"●処理中 leaderboard ID: {lb_id}")

#         # Ranked マップ一覧に存在しない ID は無視（非 Ranked など）
#         # if lb_id not in leaderboard_star_bucket:
#         #     # print(f"スキップ non-ranked leaderboard ID: {lb_id}")
#         #     continue

#         # if lb_id == "685895" or lb_id == "682135":
#         #     print(f"●2 処理中 leaderboard ID: {lb_id}")
#         star_bucket = -1
#         if tmp_stars is not None:
#             star_bucket: int = int(tmp_stars)

#         if star_bucket < 0:
#             continue
#         # star_bucket = leaderboard_star_bucket[lb_id]
#         # if star_bucket != 11:
#         #     # TODO
#         #     continue
#         # if lb_id == "685895" or lb_id == "682135":
#             # print(f"●3 処理中 leaderboard ID: {lb_id}")
#         # print(f"処理中 leaderboard ID: {lb_id} 星: {star_bucket}")
#         # if lb_id == "685895" or lb_id == "685896" or lb_id == "682135":
#         #     print(f"★処理中 leaderboard ID: {lb_id} 星: {star_bucket}")
#         state = per_leaderboard.get(lb_id)
#         if state is None:
#             state = _PerLeaderboardState(star=star_bucket, clear=False, nf=False, ss=False, best_acc=None)
#             per_leaderboard[lb_id] = state

#         modifiers = ""
#         if isinstance(score_info, dict):
#             modifiers = str(score_info.get("modifiers") or "")

#         mods_upper = modifiers.upper()
#         is_nf = "NF" in mods_upper
#         is_ss = "SS" in mods_upper

#         if is_nf:
#             state["nf"] = True
#         elif is_ss:
#             state["ss"] = True
#         else:
#             state["clear"] = True
#             # print(f"★クリア済み leaderboard ID: {lb_id} 星: {star_bucket}")

#             # NF/SS なしスコアの精度(%)を best_acc として保持
#             acc: Optional[float] = None
#             if isinstance(score_info, dict):
#                 # まずスコアオブジェクト単体から推定
#                 acc = _extract_scoresaber_accuracy(score_info)

#                 # ScoreSaber の playerScores では maxScore が leaderboard 側にあるので、
#                 # そちらからも再計算を試みる
#                 if acc is None and isinstance(leaderboard, dict):
#                     try:
#                         base = score_info.get("baseScore") or score_info.get("score") or score_info.get("modifiedScore")
#                         max_score_lb = leaderboard.get("maxScore")
#                         if base is not None and max_score_lb is not None:
#                             base_f = float(base)
#                             max_f = float(max_score_lb)
#                             if math.isfinite(base_f) and math.isfinite(max_f) and max_f > 0:
#                                 acc = max(0.0, min(100.0, base_f / max_f * 100.0))
#                     except (TypeError, ValueError):  # noqa: BLE001
#                         acc = None

#             if acc is not None:
#                 best = state.get("best_acc")
#                 if best is None or acc > best:
#                     state["best_acc"] = acc

#     # leaderboard ごとの状態から★別のクリア数 / NF数を算出
#     print(f"集計対象 leaderboard 数: {len(per_leaderboard)}")
#     for state in per_leaderboard.values():
#         star_bucket = int(state["star"])
#         has_clear = bool(state["clear"])
#         has_nf = bool(state["nf"])
#         has_ss = bool(state["ss"])

#         if has_clear:
#             # print(f"クリア済み leaderboard (星 {star_bucket}){state.get("best_acc")=}")
#             star_clear_count[star_bucket] += 1
#             # クリア済み譜面については best_acc を★別に集計
#             best_acc = state.get("best_acc")
#             if isinstance(best_acc, (int, float)) and math.isfinite(float(best_acc)):
#                 star_acc_sum[star_bucket] += float(best_acc)
#                 star_acc_count[star_bucket] += 1
#         elif has_nf:
#             # クリアはしていないが NF プレイはある譜面
#             # print(f"NF leaderboard (星 {star_bucket})")
#             star_nf_count[star_bucket] += 1
#         elif has_ss:
#             # クリアはしていないが SS(スローソング)でのプレイはある譜面
#             # print(f"SS leaderboard (星 {star_bucket})")
#             star_ss_count[star_bucket] += 1

#     # 3) StarClearStat へ変換
#     stats: list[StarClearStat] = []

#     for star in sorted(star_map_count.keys()):
#         map_count = star_map_count[star]
#         clear_count = star_clear_count.get(star, 0)
#         nf_count = star_nf_count.get(star, 0)
#         ss_count = star_ss_count.get(star, 0)
#         clear_rate = (clear_count / map_count) if map_count > 0 else 0.0

#         avg_acc: float | None
#         cnt = star_acc_count.get(star, 0)
#         if cnt > 0:
#             avg_acc = star_acc_sum.get(star, 0.0) / cnt
#         else:
#             avg_acc = None

#         stats.append(
#             StarClearStat(
#                 star=star,
#                 map_count=map_count,
#                 clear_count=clear_count,
#                 nf_count=nf_count,
#                 ss_count=ss_count,
#                 clear_rate=clear_rate,
#                 average_acc=avg_acc,
#             )
#         )

#     return stats


def create_snapshot_for_steam_id(
    steam_id: str,
    session: Optional[requests.Session] = None,
    snapshot_dir: Optional[Path] = None,
    progress: Optional[Callable[[str, float], None]] = None,
) -> Snapshot:
    """指定 SteamID(または players_index のキー)の現在ステータスから Snapshot を生成する。

    事前に GUI 側の Full Sync などで players_index.json / accsaber_ranking.json を
    用意しておく前提。
    """
    # 外部から渡される progress(message, frac) を、この関数内では _step(frac, message)
    # という形で扱えるようにするヘルパー。
    def _step(frac: float, message: str) -> None:
        if progress is None:
            return
        progress(message, frac)

    def _rethrow_if_cancelled(exc: Exception) -> None:
        """進捗ダイアログのキャンセル(RuntimeError('SNAPSHOT_CANCELLED'))だけは握りつぶさずに再スローする。"""
        if isinstance(exc, RuntimeError) and str(exc) == "SNAPSHOT_CANCELLED":
            raise

    # 以下の処理では requests.Session を必須とする関数を多数呼び出すため、
    # この関数内では session を必ず非 None の Session インスタンスに正規化して扱う。
    if session is None:
        session = requests.Session()
    assert session is not None

    # 条件: accsaber_ranking.json / scoresaber_ranking.json / beatleader_ranking.json のうち
    # いずれか1つでも存在しない場合のみランキング取得を行う。
    ss_rank_path = CACHE_DIR / "scoresaber_ranking.json"
    bl_rank_path = CACHE_DIR / "beatleader_ranking.json"
    acc_rank_path = CACHE_DIR / "accsaber_ranking.json"

    # ランキングキャッシュが全て揃っているか確認
    # コメントは日本語、画面の表示は英語
    print("1.1 ランキングキャッシュの確認...")
    need_ranking_fetch = not (ss_rank_path.exists() and bl_rank_path.exists() and acc_rank_path.exists())

    if need_ranking_fetch:
        try:
            def _fullsync_progress(message: str, frac: float) -> None:
                # 0.00 → 0.02 を Full Sync 用に使う
                _step(0.02 * frac, message)
            print("1.1.1 ランキングキャッシュ作成...")
            ensure_global_rank_caches(session=session, progress=_fullsync_progress, steam_id=steam_id)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            # ランキングキャッシュの準備に失敗しても、可能な範囲でスナップショット作成を続行する
            pass
    
    # ScoreSaber / BeatLeader の Ranked Maps キャッシュを先に更新しておく。
    # 初回は全件取得、2回目以降は差分（ScoreSaber）またはメタデータの増分検知（BeatLeader）のみ。
    try:
        _step(0.02, "Updating ScoreSaber Ranked Maps (0%, page 0/?)...")

        def _ss_leaderboard_progress(page: int, max_pages: Optional[int]) -> None:
            # 0.02 → 0.05 の範囲で「全体進捗」を動かしつつ、
            # メッセージ内の%はこのフェーズ内の進捗を表示する。
            if max_pages and max_pages > 0:
                phase_frac = max(0.0, min(1.0, page / max_pages))  # このフェーズ内の進捗
                page_text = f"{page}/{max_pages}"
            else:
                phase_frac = 0.0 if page <= 1 else 1.0
                page_text = f"{page}/?"

            # 全体進捗は 0.02→0.05 のサブレンジで表現
            global_ratio = 0.02 + 0.03 * phase_frac
            # 表示用はフェーズ内の%（0〜100%）
            phase_percent = int(phase_frac * 100)
            msg = f"Updating ScoreSaber Ranked Maps ({phase_percent}%, page {page_text})..."
            _step(global_ratio, msg)

        # ScoreSaber のリーダーボード取得中もプログレスバーが動くようにする
        print("2. ScoreSaber Ranked Maps キャッシュ更新...")
        leaderboards = _get_scoresaber_leaderboards_ranked(session, progress=_ss_leaderboard_progress)
        map_store = MapStore()
        map_store.ss_ranked_maps = leaderboards
    except Exception as exc:  # noqa: BLE001
        _rethrow_if_cancelled(exc)
        pass

    try:
        _step(0.05, "Updating BeatLeader Ranked Maps (0%, page 0/?)...")

        def _bl_leaderboard_progress(page: int, max_pages: Optional[int]) -> None:
            # 0.05 → 0.08 の範囲で「全体進捗」を動かしつつ、
            # メッセージ内の%はこのフェーズ内の進捗を表示する。
            if max_pages and max_pages > 0:
                phase_frac = max(0.0, min(1.0, page / max_pages))
                page_text = f"{page}/{max_pages}"
            else:
                phase_frac = 0.0 if page <= 1 else 1.0
                page_text = f"{page}/?"

            global_ratio = 0.05 + 0.03 * phase_frac
            phase_percent = int(phase_frac * 100)
            msg = f"Updating BeatLeader Ranked Maps ({phase_percent}%, page {page_text})..."
            _step(global_ratio, msg)
        print("3. BeatLeader Ranked Maps キャッシュ更新...")
        _get_beatleader_leaderboards_ranked(session, progress=_bl_leaderboard_progress)
    except Exception as exc:  # noqa: BLE001
        _rethrow_if_cancelled(exc)
        pass

    # プレイヤーインデックスを読み込み、必要なら API からプレイヤー情報を補完する
    print("4. プレイヤーインデックスの確認...")
    _step(0.08, "Loading player index...")
    player_index = _load_player_index()
    print("4.1 プレイヤーインデックスの確認完了。")
    # players_index.json がまだ無い / 空の場合は、可能であれば
    # scoresaber_ALL / beatleader_ALL からインデックスを自動再構築する。
    if not player_index:
        try:
            rebuild_player_index_from_global()
            player_index = _load_player_index()
            print("4.2 プレイヤーインデックスの再構築完了。")
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            player_index = {}
    map_store = MapStore()
    map_store.player_index = player_index
    
    entry = player_index.get(steam_id)
    if not entry:
        # players_index.json に存在しない場合でも、可能であれば ScoreSaber / BeatLeader
        # の API から直接プレイヤー情報を取得してスナップショットを作成できるようにする。
        ss: Optional[ScoreSaberPlayer] = None
        bl: Optional[BeatLeaderPlayer] = None

        if _is_steam_id(steam_id):
            _step(0.10, "Fetching player from ScoreSaber / BeatLeader...")
            try:
                print("4.3 players_index.json に存在しない SteamID。ScoreSaber から情報取得を試みます...")
                ss = _fetch_scoresaber_player_basic(steam_id, session)
                map_store.ss_players[steam_id] = ss
            except Exception as exc:  # noqa: BLE001
                _rethrow_if_cancelled(exc)
                ss = None
            try:
                print("4.4 BeatLeader から情報取得を試みます...")
                bl = fetch_bl_player(steam_id, session=session)
                map_store.bl_players[steam_id] = bl
            except Exception as exc:  # noqa: BLE001
                _rethrow_if_cancelled(exc)
                bl = None

        if ss is None and bl is None:
            raise RuntimeError(
                f"steam_id {steam_id!r} not found in players_index.json "
                "and failed to fetch from ScoreSaber/BeatLeader APIs.",
            )

        entry = {}
        if ss is not None:
            entry["scoresaber"] = ss
        if bl is not None:
            entry["beatleader"] = bl
        player_index[steam_id] = entry
        try:
            _save_player_index(player_index)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            # インデックス保存に失敗してもスナップショット作成自体は続行する
            pass

    # インデックス（または API 補完）から ScoreSaber / BeatLeader 情報を取り出す
    raw_ss = entry.get("scoresaber") if isinstance(entry, dict) else None
    ss: Optional[ScoreSaberPlayer] = raw_ss if isinstance(raw_ss, ScoreSaberPlayer) else None
    raw_bl = entry.get("beatleader") if isinstance(entry, dict) else None
    bl: Optional[BeatLeaderPlayer] = raw_bl if isinstance(raw_bl, BeatLeaderPlayer) else None

    scoresaber_id: Optional[str] = ss.id if ss is not None else None
    scoresaber_name: Optional[str] = ss.name if ss is not None else None
    scoresaber_country: Optional[str] = ss.country if ss is not None else None
    scoresaber_pp: Optional[float] = ss.pp if ss is not None else None
    scoresaber_rank_global: Optional[int] = ss.global_rank if ss is not None else None
    scoresaber_rank_country: Optional[int] = ss.country_rank if ss is not None else None
    scoresaber_average_ranked_acc: Optional[float] = None
    scoresaber_total_play_count: Optional[int] = None
    scoresaber_ranked_play_count: Optional[int] = None

    if scoresaber_id:
        # スナップショット取得時にプレイヤースコアキャッシュも更新しておく。
        try:
            _step(0.15, "Fetching ScoreSaber player scores (page 1/?)...")

            def _ss_scores_progress(page: int, max_pages: Optional[int]) -> None:
                # 0.15 → 0.20 の範囲で進捗を動かす
                if max_pages and max_pages > 0:
                    frac = max(0.0, min(1.0, page / max_pages))
                    msg = f"Fetching ScoreSaber player scores (page {page}/{max_pages})..."
                else:
                    # 最大ページ数がまだ分からない場合
                    frac = 0.0
                    msg = f"Fetching ScoreSaber player scores (page {page}/?)..."

                _step(0.15 + 0.05 * frac, msg)

            print("5. ScoreSaber プレイヤースコアキャッシュ更新...")
            _get_scoresaber_player_scores(scoresaber_id, session, progress=_ss_scores_progress)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            pass
        
        print("6. ScoreSaber プレイヤーステータス取得...")
        _step(0.20, "Fetching ScoreSaber player stats...")
        stats = _get_scoresaber_player_stats(scoresaber_id, session)
        if stats:
            try:
                avg = stats.get("averageRankedAccuracy")
                if avg is not None:
                    scoresaber_average_ranked_acc = float(avg)
            except (TypeError, ValueError):
                pass

            try:
                total_pc = stats.get("totalPlayCount")
                if total_pc is not None:
                    scoresaber_total_play_count = int(total_pc)
            except (TypeError, ValueError):
                pass

            try:
                ranked_pc = stats.get("rankedPlayCount")
                if ranked_pc is not None:
                    scoresaber_ranked_play_count = int(ranked_pc)
            except (TypeError, ValueError):
                pass

    # BeatLeader 側は、キャッシュに無い場合は API から1回だけ取得してみる。
    beatleader: Optional[BeatLeaderPlayer] = bl
    if beatleader is None and scoresaber_id:
        beatleader = fetch_bl_player(scoresaber_id, session=session)

    beatleader_id: Optional[str] = beatleader.id if beatleader is not None else None
    beatleader_name: Optional[str] = beatleader.name if beatleader is not None else None
    beatleader_country: Optional[str] = beatleader.country if beatleader is not None else None
    beatleader_pp: Optional[float] = beatleader.pp if beatleader is not None else None
    beatleader_rank_global: Optional[int] = beatleader.global_rank if beatleader is not None else None
    beatleader_rank_country: Optional[int] = beatleader.country_rank if beatleader is not None else None

    # BeatLeader の追加統計（average_acc, play_count 系）は scoreStats から取得する（ベストエフォート）。
    beatleader_average_ranked_acc: Optional[float] = None
    beatleader_total_play_count: Optional[int] = None
    beatleader_ranked_play_count: Optional[int] = None

    if beatleader_id:
        # BeatLeader 側もスナップショット取得時にプレイヤースコアキャッシュを更新しておく。
        try:
            _step(0.30, "Fetching BeatLeader player scores (page 1/?)...")

            def _bl_scores_progress(page: int, max_pages: Optional[int]) -> None:
                # 0.30 → 0.35 の範囲で進捗を動かす
                if max_pages and max_pages > 0:
                    frac = max(0.0, min(1.0, page / max_pages))
                    msg = f"Fetching BeatLeader player scores (page {page}/{max_pages})..."
                else:
                    frac = 0.0
                    msg = f"Fetching BeatLeader player scores (page {page}/?)..."

                _step(0.30 + 0.05 * frac, msg)
            print("7. BeatLeader プレイヤースコアキャッシュ更新...")
            _get_beatleader_player_scores(beatleader_id, session, progress=_bl_scores_progress)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            pass
        
        print("8. BeatLeader プレイヤーステータス取得...")
        _step(0.35, "Fetching BeatLeader player stats...")
        bl_stats = _get_beatleader_player_stats(beatleader_id, session)
        if bl_stats:
            print("8.1 BeatLeader プレイヤーステータス取得完了。")
            # BeatLeader の averageRankedAccuracy は 0.0-1.0 の値なので、百分率に揃えるため 100 倍する。
            try:
                bl_avg = bl_stats.get("averageRankedAccuracy")
                if bl_avg is not None:
                    beatleader_average_ranked_acc = float(bl_avg) * 100.0
            except (TypeError, ValueError):
                pass

            # プレイ回数系フィールドは存在すれば取得する（存在しない場合は None のまま）。
            try:
                bl_total_pc = bl_stats.get("totalPlayCount")
                if bl_total_pc is not None:
                    beatleader_total_play_count = int(bl_total_pc)
            except (TypeError, ValueError):
                pass

            try:
                bl_ranked_pc = bl_stats.get("rankedPlayCount")
                if bl_ranked_pc is not None:
                    beatleader_ranked_play_count = int(bl_ranked_pc)
            except (TypeError, ValueError):
                pass

    # AccSaber ランク / プレイ回数: まずは Overall のキャッシュから紐付け。
    # *_rank_global にグローバル順位、*_rank_country に国別順位を保持する。
    print("9. AccSaber プレイヤーステータス取得...")
    _step(0.40, "Loading AccSaber overall cache...")
    acc_overall = _find_accsaber_for_scoresaber_id(scoresaber_id, session=session) if scoresaber_id else None
    # グローバルランク
    acc_overall_rank_global: Optional[int] = None
    acc_true_rank_global: Optional[int] = None
    acc_standard_rank_global: Optional[int] = None
    acc_tech_rank_global: Optional[int] = None

    # 国別ランク
    acc_overall_rank_country: Optional[int] = None
    acc_true_rank_country: Optional[int] = None
    acc_standard_rank_country: Optional[int] = None
    acc_tech_rank_country: Optional[int] = None
    acc_overall_play_count: Optional[int] = None
    acc_true_play_count: Optional[int] = None
    acc_standard_play_count: Optional[int] = None
    acc_tech_play_count: Optional[int] = None

    acc_overall_ap: Optional[float] = None
    acc_true_ap: Optional[float] = None
    acc_standard_ap: Optional[float] = None
    acc_tech_ap: Optional[float] = None

    def _parse_ap(text: str | None) -> float:
        """AccSaber の AP 文字列から数値を抽出して float に変換する。"""

        if not text:
            return 0.0
        t = text.replace(",", "")
        import re as _re

        m = _re.search(r"[-+]?\d*\.?\d+", t)
        if not m:
            return 0.0
        try:
            return float(m.group(0))
        except ValueError:
            return 0.0

    def _parse_acc_plays(text: str | None) -> Optional[int]:
        if not text:
            return None
        import re as _re

        t = text.replace(",", "")
        m = _re.search(r"[-+]?\d+", t)
        if not m:
            return None
        try:
            return int(m.group(0))
        except ValueError:
            return None

    # Overall はキャッシュから rank / plays を取得（rank はグローバル順位）
    if acc_overall is not None:
        acc_overall_rank_global = acc_overall.rank
        acc_overall_play_count = _parse_acc_plays(getattr(acc_overall, "plays", ""))
        acc_overall_ap = _parse_ap(getattr(acc_overall, "total_ap", ""))

    # True / Standard / Tech は必要になったときだけ API 経由で取得（ベストエフォート）。
    if scoresaber_id:
        try:
            print("9.1 AccSaber True プレイヤーステータス取得...")
            _step(0.45, "Fetching AccSaber True leaderboard...")
            acc_true = _find_accsaber_skill_for_scoresaber_id(scoresaber_id, fetch_true, session=session)
        except Exception:  # noqa: BLE001
            acc_true = None
        if acc_true is not None:
            acc_true_rank_global = acc_true.rank
            acc_true_play_count = _parse_acc_plays(getattr(acc_true, "plays", ""))
            acc_true_ap = _parse_ap(getattr(acc_true, "total_ap", ""))

        try:
            print("9.2 AccSaber Standard プレイヤーステータス取得...")
            _step(0.50, "Fetching AccSaber Standard leaderboard...")
            acc_standard = _find_accsaber_skill_for_scoresaber_id(scoresaber_id, fetch_standard, session=session)
        except Exception:  # noqa: BLE001
            acc_standard = None
        if acc_standard is not None:
            acc_standard_rank_global = acc_standard.rank
            acc_standard_play_count = _parse_acc_plays(getattr(acc_standard, "plays", ""))
            acc_standard_ap = _parse_ap(getattr(acc_standard, "total_ap", ""))

        try:
            print("9.3 AccSaber Tech プレイヤーステータス取得...")
            _step(0.55, "Fetching AccSaber Tech leaderboard...")
            acc_tech = _find_accsaber_skill_for_scoresaber_id(scoresaber_id, fetch_tech, session=session)
        except Exception:  # noqa: BLE001
            acc_tech = None
        if acc_tech is not None:
            acc_tech_rank_global = acc_tech.rank
            acc_tech_play_count = _parse_acc_plays(getattr(acc_tech, "plays", ""))
            acc_tech_ap = _parse_ap(getattr(acc_tech, "total_ap", ""))

        # JP 国内順位は accsaber_ranking.json 全体と players_index.json を使って計算する
        try:
            print("9.4 AccSaber 国内ランク計算...")
            _step(0.60, "Loading AccSaber players for JP ranks...")
            acc_players = _load_accsaber_players()
        except Exception:  # noqa: BLE001
            acc_players = []

        if acc_players and scoresaber_country:
            # ScoreSaber ID -> 国コード のマップを作る
            ss_country_by_id: dict[str, str] = {}
            try:
                index_all = _load_player_index()
            except Exception:  # noqa: BLE001
                index_all = {}

            for entry_all in index_all.values():
                ss_player = entry_all.get("scoresaber")
                if isinstance(ss_player, ScoreSaberPlayer) and ss_player.id and ss_player.country:
                    ss_country_by_id[str(ss_player.id)] = str(ss_player.country).upper()

            country = str(scoresaber_country).upper()

            # 同一国のプレイヤーだけを集める
            same_country_players: list[AccSaberPlayer] = []
            for p in acc_players:
                sid = getattr(p, "scoresaber_id", None)
                if not sid:
                    continue
                sid_str = str(sid)
                cc = ss_country_by_id.get(sid_str)
                if cc != country:
                    continue
                same_country_players.append(p)

            if same_country_players:
                def _rank_for(get_ap) -> Optional[int]:
                    # 3000AP 未満のプレイヤーはスキル別ランキング対象外とし、
                    # 同じ国かつしきい値以上の集合の中で順位を付ける。
                    filtered = [
                        p
                        for p in same_country_players
                        if _parse_ap(get_ap(p)) >= ACCSABER_MIN_AP_SKILL
                    ]
                    if not filtered:
                        return None

                    players_sorted = sorted(
                        filtered,
                        key=lambda p: _parse_ap(get_ap(p)),
                        reverse=True,
                    )
                    rank_val = 1
                    for p in players_sorted:
                        sid = getattr(p, "scoresaber_id", None)
                        if str(sid) == scoresaber_id:
                            return rank_val
                        rank_val += 1
                    return None

                acc_overall_rank_country = _rank_for(lambda p: getattr(p, "total_ap", ""))
                acc_true_rank_country = _rank_for(lambda p: getattr(p, "true_ap", ""))
                acc_standard_rank_country = _rank_for(lambda p: getattr(p, "standard_ap", ""))
                acc_tech_rank_country = _rank_for(lambda p: getattr(p, "tech_ap", ""))

    # Overall AP は True / Standard / Tech の AP を合算した値とする
    if any(v is not None for v in (acc_true_ap, acc_standard_ap, acc_tech_ap)):
        acc_overall_ap = (acc_true_ap or 0.0) + (acc_standard_ap or 0.0) + (acc_tech_ap or 0.0)

    # ScoreSaber / BeatLeader のスコア一覧から★別統計を集計する（失敗した場合は空リスト）。
    try:
        print("9.5 ScoreSaber ★別統計集計...")
        _step(0.70, "Collecting ScoreSaber star stats...")
        star_stats: list[StarClearStat] = _collect_star_stats_from_scoresaber(scoresaber_id, session) if scoresaber_id else []
    except Exception:  # noqa: BLE001
        print("★別統計の集計に失敗しました。")
        star_stats = []

    try:
        print("9.6 BeatLeader ★別統計集計...") 
        _step(0.80, "Collecting BeatLeader star stats...")
        beatleader_star_stats: list[StarClearStat] = (
            collect_beatleader_star_stats(beatleader_id, session) if beatleader_id else []
        )
    except Exception:  # noqa: BLE001
        beatleader_star_stats = []
        print("9.6 BeatLeader ★別統計集計完了。")
    # スナップショットオブジェクトを構築して保存する
    print("10. スナップショットオブジェクト構築...")
    now = datetime.utcnow().replace(microsecond=0)

    snapshot = Snapshot(
        taken_at=now.isoformat() + "Z",
        steam_id=steam_id,
        scoresaber_id=scoresaber_id,
        scoresaber_name=scoresaber_name,
        scoresaber_country=scoresaber_country,
        scoresaber_pp=scoresaber_pp,
        scoresaber_rank_global=scoresaber_rank_global,
        scoresaber_rank_country=scoresaber_rank_country,
        scoresaber_average_ranked_acc=scoresaber_average_ranked_acc,
        scoresaber_total_play_count=scoresaber_total_play_count,
        scoresaber_ranked_play_count=scoresaber_ranked_play_count,
        beatleader_id=beatleader_id,
        beatleader_name=beatleader_name,
        beatleader_country=beatleader_country,
        beatleader_pp=beatleader_pp,
        beatleader_rank_global=beatleader_rank_global,
        beatleader_rank_country=beatleader_rank_country,
        # AccSaber グローバルランク
        accsaber_overall_rank=acc_overall_rank_global,
        accsaber_true_rank=acc_true_rank_global,
        accsaber_standard_rank=acc_standard_rank_global,
        accsaber_overall_play_count=acc_overall_play_count,
        accsaber_true_play_count=acc_true_play_count,
        accsaber_standard_play_count=acc_standard_play_count,
        accsaber_tech_rank=acc_tech_rank_global,
        accsaber_tech_play_count=acc_tech_play_count,
        accsaber_overall_ap=acc_overall_ap,
        accsaber_true_ap=acc_true_ap,
        accsaber_standard_ap=acc_standard_ap,
        accsaber_tech_ap=acc_tech_ap,
        # AccSaber 国別ランク
        accsaber_overall_rank_country=acc_overall_rank_country,
        accsaber_true_rank_country=acc_true_rank_country,
        accsaber_standard_rank_country=acc_standard_rank_country,
        accsaber_tech_rank_country=acc_tech_rank_country,
        beatleader_average_ranked_acc=beatleader_average_ranked_acc,
        beatleader_total_play_count=beatleader_total_play_count,
        beatleader_ranked_play_count=beatleader_ranked_play_count,
        star_stats=star_stats,
        beatleader_star_stats=beatleader_star_stats,
    )
    print("10.1 スナップショットオブジェクト構築完了。")
    _step(0.90, "Saving snapshot...")

    if snapshot_dir is not None:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        name = f"{steam_id}_{now:%Y%m%d-%H%M%S}.json"
        path = snapshot.save(snapshot_dir / name)
    else:
        path = snapshot.save()

    print(f"10.2 スナップショット保存完了: {path}")
    _step(1.0, f"Done: {path.name}")
    return snapshot


# --- original CLI entrypoint retained ---


def main() -> None:
    """コマンドラインから指定 SteamID のスナップショットを作成する。"""
    import argparse
    # pylint: disable=import-outside-toplevel
    parser = argparse.ArgumentParser(description="Create a snapshot for given SteamID using cached rankings.")
    parser.add_argument("steam_id", help="SteamID (or key used in players_index.json)")
    # Optional argument to specify snapshot directory
    parser.add_argument(
        "--snapshot-dir",
        dest="snapshot_dir",
        help="Directory to save snapshot JSON (default: snapshots under project root)",
    )
    args = parser.parse_args()

    snapshot_dir: Optional[Path]
    if args.snapshot_dir:
        snapshot_dir = Path(args.snapshot_dir)
    else:
        snapshot_dir = None

    def _cli_progress(message: str, fraction: float) -> None:
        """CLI 用の進捗表示。"""
        bar_width = 40
        filled = int(bar_width * fraction)
        bar = "#" * filled + "-" * (bar_width - filled)
        percent = int(fraction * 100)
        print(f"\r[{bar}] {percent:3d}% {message:40s}", end="", flush=True)

    # Create snapshot
    snapshot = create_snapshot_for_steam_id(args.steam_id, snapshot_dir=snapshot_dir, progress=_cli_progress)
    print()  # newline after progress bar
    print("Snapshot created:")
    print(json.dumps(asdict(snapshot), ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
