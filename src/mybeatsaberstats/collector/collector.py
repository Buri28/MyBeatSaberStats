
from __future__ import annotations
def _is_steam_id(value: str | None) -> bool:
    return isinstance(value, str) and value.isdigit() and len(value) == 17
from ..ranking_view import _load_player_index

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
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
    _get_beatleader_leaderboards_ranked as _bl_get_beatleader_leaderboards_ranked,
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
    get_accsaber_playlist_map_counts_with_meta,
    fetch_and_save_accsaber_maps_cache as _fetch_and_save_accsaber_maps,
    fetch_and_save_player_scores_cache as _fetch_and_save_acc_player_scores,
)
from .accsaber import (
    _load_list_cache,
    _find_accsaber_for_scoresaber_id,
    _find_accsaber_skill_for_scoresaber_id,
)
from ..accsaber_reloaded import fetch_player_all_categories as _fetch_accsaber_reloaded
from ..accsaber_reloaded import fetch_player_xp as _fetch_accsaber_reloaded_xp
from ..accsaber_reloaded import fetch_reloaded_map_counts as _fetch_reloaded_map_counts
from ..accsaber_reloaded import fetch_and_save_all_maps_cache as _fetch_and_save_rl_maps
from ..accsaber_reloaded import fetch_and_save_player_scores_cache as _fetch_and_save_rl_player_scores

# キャッシュディレクトリ(app.py と同じ BASE_DIR / "cache" を利用)
CACHE_DIR = BASE_DIR / "cache"


@dataclass
class SnapshotOptions:
    """スナップショット取得時に各データソースの取得可否を制御するオプション。

    デフォルトはすべて True（全データを取得）。
    False にすると対応するステップをスキップし、既存キャッシュのデータをそのまま使用する。

    ss_fetch_until / bl_fetch_until を指定すると、それより古い日時のスコアまで遡って取得する。
    None の場合はキャッシュの最新 timeSet に達した時点で差分取得を終了する（通常動作）。
    """
    fetch_ss_ranked_maps: bool = True    # ScoreSaber Ranked Maps
    fetch_bl_ranked_maps: bool = True    # BeatLeader Ranked Maps
    fetch_scoresaber: bool = True        # ScoreSaber プレイヤー情報・スコア・統計
    fetch_beatleader: bool = True        # BeatLeader プレイヤー情報・スコア・統計
    fetch_accsaber: bool = True          # AccSaber ランク情報
    fetch_accsaber_reloaded: bool = True # AccSaber Reloaded ランク情報
    fetch_ss_star_stats: bool = True     # ScoreSaber ★別クリア統計
    fetch_bl_star_stats: bool = True     # BeatLeader ★別クリア統計
    ss_fetch_until: Optional[datetime] = None  # ScoreSaber スコア取得の遡り期限 (None=自動)
    bl_fetch_until: Optional[datetime] = None  # BeatLeader スコア取得の遡り期限 (None=自動)
    ss_ranked_until: Optional[datetime] = None  # ScoreSaber Ranked Maps 取得の遡り期限 (None=自動)
    bl_ranked_until: Optional[datetime] = None  # BeatLeader Ranked Maps 取得の遡り期限 (None=自動)
    ss_fetch_all: bool = False  # ScoreSaber: 全スコアを最初から再取得 (キャッシュ差分を無視)
    bl_fetch_all: bool = False  # BeatLeader: 全スコアを最初から再取得 (キャッシュ差分を無視)


SCORESABER_MIN_PP_GLOBAL = 4000.0
BEATLEADER_MIN_PP_GLOBAL = 5000.0

# SCORESABER_LEADERBOARDS_URL = "https://scoresaber.com/api/leaderboards"
# SCORESABER_PLAYER_SCORES_URL = "https://scoresaber.com/api/player/{player_id}/scores"
# SCORESABER_PLAYER_FULL_URL = "https://scoresaber.com/api/player/{player_id}/full"

BEATLEADER_LEADERBOARDS_URL = "https://api.beatleader.xyz/leaderboards"
BL_BASE_URL = "https://api.beatleader.xyz"


def _read_cache_fetched_at(path: Path) -> Optional[datetime]:
    """キャッシュ JSON の fetched_at フィールドを UTC datetime として返す。

    ファイルが存在しない・読めない・フィールドがない場合は None を返す。
    旧形式の plain list ファイルはファイルの修正時刻を UTC datetime として返す。
    """
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            fa = raw.get("fetched_at")
            if isinstance(fa, str) and fa:
                return datetime.fromisoformat(fa.rstrip("Z"))
        elif isinstance(raw, list) and raw:
            # 旧形式（plain list）: ファイルの修正時刻を返す
            return datetime.utcfromtimestamp(path.stat().st_mtime)
    except Exception:  # noqa: BLE001
        pass
    return None


def _save_player_index(
    index: Dict[str, Dict[str, object]],
    update_fetched_at: bool = True,
) -> None:
    """players_index.json を app.MainWindow と同一フォーマットで保存する。

    update_fetched_at=False の場合は既存ファイルの fetched_at を維持する。
    全件再構築（ensure_global_rank_caches など）では True を、
    スナップショット時の個別プレイヤー情報更新では False を渡す。
    """

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

        # 既存の fetched_at を維持する場合はファイルから読み取る
        if not update_fetched_at:
            existing_fa = _read_cache_fetched_at(path)
            fetched_at_str = (existing_fa.isoformat() + "Z") if existing_fa is not None else (datetime.utcnow().isoformat() + "Z")
        else:
            fetched_at_str = datetime.utcnow().isoformat() + "Z"

        payload = {
            "fetched_at": fetched_at_str,
            "rows": rows,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        return


def _save_list_cache(path: Path, items) -> None:
    """汎用のリストキャッシュセーバー。dataclass のリストを JSON に保存する。"""

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        serializable = [asdict(x) for x in items]
        payload = {
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "data": serializable,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
            bl_global = _load_list_cache(bl_global_path, BeatLeaderPlayer)
        except Exception:  # noqa: BLE001
            bl_global = []

    index: Dict[str, Dict[str, object]] = {}

    # ScoreSaber プレイヤーを steam_id でインデックス化
    for p in ss_global:
        if not p.id:
            continue
        index[str(p.id)] = {"scoresaber": p}

    # BeatLeader プレイヤーを id (SteamID) で突き合わせ
    # BL の id == SS の id == SteamID なので直接マッチング可能
    bl_by_id: Dict[str, BeatLeaderPlayer] = {}
    for p in bl_global:
        if p.id:
            bl_by_id[str(p.id)] = p

    # 名前+国コードでフォールバックマッチング用マップ
    ss_by_name_country: Dict[tuple[str, str], list[ScoreSaberPlayer]] = {}
    for p in ss_global:
        if not p.name or not p.country:
            continue
        key = (_norm_name(p.name), p.country.upper())
        ss_by_name_country.setdefault(key, []).append(p)

    for bl_id, bl_p in bl_by_id.items():
        if bl_id in index:
            # SS と直接 ID 一致 → BL 情報を追加
            index[bl_id]["beatleader"] = bl_p
        else:
            # SS に対応する ID がない場合、名前+国でフォールバック
            if bl_p.name and bl_p.country:
                key = (_norm_name(bl_p.name), bl_p.country.upper())
                ss_candidates = ss_by_name_country.get(key, [])
                if len(ss_candidates) == 1:
                    ss_id = str(ss_candidates[0].id)
                    index.setdefault(ss_id, {"scoresaber": ss_candidates[0]})
                    index[ss_id]["beatleader"] = bl_p
                else:
                    # SS に存在しない BL プレイヤーもインデックスに追加（BL-only）
                    index[bl_id] = {"beatleader": bl_p}

    _save_player_index(index)



def _load_accsaber_players() -> list[AccSaberPlayer]:
    """AccSaber グローバルリーダーボードキャッシュをすべて読み込む。"""

    acc_path = CACHE_DIR / "accsaber_ranking.json"
    return _load_list_cache(acc_path, AccSaberPlayer)


def _find_last_successful_accsaber_snapshot(
    steam_id: str,
    category: str,  # "true", "standard", "tech"
) -> Optional["Snapshot"]:
    """過去のスナップショットから、指定カテゴリの API 取得成功スナップショットを最新順で返す。

    fetched フラグが True のもの、またはフラグ未記録でもカテゴリの AP データが
    存在するもの（fetched フィールドが追加される前の旧フォーマット）を対象とする。
    """
    snap_dir = BASE_DIR / "snapshots"
    if not snap_dir.exists():
        return None
    flag_field = f"accsaber_{category}_fetched"
    ap_field = f"accsaber_{category}_ap"
    candidates = sorted(snap_dir.glob(f"{steam_id}_*.json"), reverse=True)
    for path in candidates:
        try:
            snap = Snapshot.load(path)
            fetched = getattr(snap, flag_field, False)
            ap_val = getattr(snap, ap_field, None)
            # fetched=True、または旧フォーマット（フィールド未記録）でも AP がある場合は有効
            if fetched or (ap_val is not None):
                return snap
        except Exception:  # noqa: BLE001
            continue
    return None


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
            if page == 1:
                raise  # 1 ページ目の失敗は呼び出し元に伝播させる
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


def _fetch_and_save_accsaber_ranking(
    session: requests.Session,
    progress: Optional[Callable[[str, float], None]] = None,
) -> bool:
    """AccSaber の Overall + True/Standard/Tech ランキングを全件取得して accsaber_ranking.json に保存する。

    progress(message, fraction) は 0.0–1.0 の相対進捗コールバック（省略可）。
    保存に成功したら True、失敗したら False を返す。
    RuntimeError（SNAPSHOT_CANCELLED 等）は呼び出し元へ再スローする。
    """
    import re as _re

    acc_rank_path = CACHE_DIR / "accsaber_ranking.json"

    def _step(ratio: float, message: str) -> None:
        if progress is not None:
            progress(message, max(0.0, min(1.0, ratio)))

    def _parse_ap_local(text: str | None) -> float:
        if not text:
            return 0.0
        t = text.replace(",", "")
        m = _re.search(r"[-+]?\d*\.?\d+", t)
        if not m:
            return 0.0
        try:
            return float(m.group(0))
        except ValueError:
            return 0.0

    try:
        acc_players: list[AccSaberPlayer] = []
        max_pages = 200

        for page in range(1, max_pages + 1):
            phase_frac = min(1.0, page / max_pages)
            _step(0.60 * phase_frac, f"Fetching AccSaber overall ranking... (page {page})")

            page_players = fetch_overall(country=None, page=page, session=session)
            if not page_players:
                break
            for p in page_players:
                ap_value = _parse_ap_local(getattr(p, "total_ap", ""))
                if ap_value >= ACCSABER_MIN_AP_GLOBAL:
                    acc_players.append(p)

            last_ap = _parse_ap_local(getattr(page_players[-1], "total_ap", ""))
            if last_ap < ACCSABER_MIN_AP_GLOBAL:
                break

        by_id: dict[str, AccSaberPlayer] = {}
        for p in acc_players:
            sid = getattr(p, "scoresaber_id", None)
            if not sid:
                continue
            by_id[str(sid)] = p

        def _enrich_skill(leaderboard_fetch, attr_name: str, label: str, base_ratio: float) -> None:
            max_pages_skill = 200
            for sk_page in range(1, max_pages_skill + 1):
                _step(base_ratio, f"Fetching AccSaber {label} AP... (page {sk_page})")
                try:
                    skill_players = leaderboard_fetch(country=None, page=sk_page, session=session)
                except Exception:  # noqa: BLE001
                    break
                if not skill_players:
                    break
                for sp in skill_players:
                    sid = getattr(sp, "scoresaber_id", None)
                    if not sid:
                        continue
                    sid_str = str(sid)
                    if sid_str in by_id:
                        setattr(by_id[sid_str], attr_name, getattr(sp, "total_ap", ""))
                    else:
                        new_p = AccSaberPlayer(
                            rank=getattr(sp, "rank", 0),
                            name=getattr(sp, "name", ""),
                            total_ap="0",
                            average_acc=getattr(sp, "average_acc", ""),
                            plays=getattr(sp, "plays", ""),
                            top_play_pp=getattr(sp, "top_play_pp", ""),
                            scoresaber_id=sid_str,
                        )
                        setattr(new_p, attr_name, getattr(sp, "total_ap", ""))
                        acc_players.append(new_p)
                        by_id[sid_str] = new_p

        try:
            _enrich_skill(fetch_true, "true_ap", "True", 0.65)
            _enrich_skill(fetch_standard, "standard_ap", "Standard", 0.75)
            _enrich_skill(fetch_tech, "tech_ap", "Tech", 0.85)
        except Exception:  # noqa: BLE001
            pass

        map_store_instance = MapStore()
        map_store_instance.acc_players = {p.scoresaber_id: p for p in acc_players if p.scoresaber_id}
        _save_list_cache(acc_rank_path, acc_players)
        return True
    except RuntimeError:
        raise  # SNAPSHOT_CANCELLED などは呼び出し元へ伝播させる
    except Exception:  # noqa: BLE001
        return False


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

    # AccSaber overall (10000AP 以上) を最新化する
    _acc_ok = _fetch_and_save_accsaber_ranking(
        session,
        progress=lambda msg, frac: _step(frac * 0.30, msg),
    )
    if not _acc_ok:
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

    # AccSaber プレイリスト総譜面数を更新する（accsaber_playlist_counts.json）
    try:
        get_accsaber_playlist_map_counts_with_meta(session=session)
    except Exception:  # noqa: BLE001
        pass

    # AccSaber Reloaded 総譜面数を更新する（accsaber_reloaded_map_counts.json）
    try:
        _fetch_reloaded_map_counts(session=session)
    except Exception:  # noqa: BLE001
        pass

    # AccSaber マップデータ（ranked-maps + プレイリスト）をキャッシュに保存する
    try:
        _fetch_and_save_accsaber_maps(session=session)
    except Exception:  # noqa: BLE001
        pass

    # AccSaber Reloaded 全マップデータをキャッシュに保存する
    try:
        _fetch_and_save_rl_maps(session=session)
    except Exception:  # noqa: BLE001
        pass





def _get_beatleader_leaderboards_ranked(
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    fetch_until: Optional[datetime] = None,
) -> list[dict]:
    """BeatLeader の Ranked leaderboards をキャッシュ付きで全件取得する。

    beatleader.py 内の同名関数に委譲する。
    """
    return _bl_get_beatleader_leaderboards_ranked(session, progress=progress, fetch_until=fetch_until)




def _get_beatleader_player_scores(
    beatleader_id: str,
    session: requests.Session,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    fetch_until: Optional[datetime] = None,
) -> list[dict]:
    """BeatLeader のプレイヤースコア取得を beatleader.py 側の実装に委譲するラッパー。

    実際の API 呼び出しとキャッシュ処理は
    src/mybeatsaberstats/collector/beatleader.py 内の
    _get_beatleader_player_scores に集約する。
    """

    if not beatleader_id:
        return []

    return _bl_get_beatleader_player_scores(beatleader_id, session, progress, fetch_until=fetch_until)


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
    options: Optional[SnapshotOptions] = None,
) -> Snapshot:
    """指定 SteamID(または players_index のキー)の現在ステータスから Snapshot を生成する。

    事前に GUI 側の Full Sync などで players_index.json / accsaber_ranking.json を
    用意しておく前提。
    options で各データソースの取得を個別にスキップできる。
    """
    # options が None の場合はすべて取得
    if options is None:
        options = SnapshotOptions()

    # フォールバックなどの警告メッセージを收集する
    _warnings: list[str] = []

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

    # ensure_global_rank_caches が AccSaber を再フェッチした場合は True になる
    _acc_ranking_refreshed = False

    if need_ranking_fetch:
        try:
            def _fullsync_progress(message: str, frac: float) -> None:
                # 0.00 → 0.02 を Full Sync 用に使う
                _step(0.02 * frac, message)
            print("1.1.1 ランキングキャッシュ作成...")
            ensure_global_rank_caches(session=session, progress=_fullsync_progress, steam_id=steam_id)
            _acc_ranking_refreshed = True
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            # ランキングキャッシュの準備に失敗しても、可能な範囲でスナップショット作成を続行する
            pass
    
    # ScoreSaber / BeatLeader の Ranked Maps キャッシュを先に更新しておく。
    # 初回は全件取得、2回目以降は差分（ScoreSaber）またはメタデータの増分検知（BeatLeader）のみ。
    if options.fetch_ss_ranked_maps:
        try:
            def _ss_leaderboard_progress(page: int, max_pages: Optional[int]) -> None:
                if max_pages and max_pages > 0:
                    phase_frac = max(0.0, min(1.0, page / max_pages))
                    page_text = f"{page}/{max_pages}"
                else:
                    phase_frac = 0.0 if page <= 1 else 1.0
                    page_text = f"{page}/?"
                global_ratio = 0.02 + 0.03 * phase_frac
                phase_percent = int(phase_frac * 100)
                msg = f"Updating ScoreSaber Ranked Maps ({phase_percent}%, page {page_text})..."
                _step(global_ratio, msg)

            print("2. ScoreSaber Ranked Maps キャッシュ更新...")
            if options.ss_ranked_until is not None:
                ss_ranked_until = options.ss_ranked_until
            else:
                ss_last_fetched = _read_cache_fetched_at(CACHE_DIR / "scoresaber_ranked_maps.json")
                if ss_last_fetched is not None:
                    # 月次更新でrankedDate遡及変更される可能性があるため、前回取得日時より60日前から再取得する
                    ss_ranked_until = ss_last_fetched - timedelta(days=60)
                    print(f"ScoreSaber Ranked Maps 前回取得日時: {ss_last_fetched.strftime('%Y-%m-%d %H:%M:%S')} UTC → 60日遡り: {ss_ranked_until.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                else:
                    ss_ranked_until = None
                    print("ScoreSaber Ranked Maps: 初回取得のため全件取得")
            fetch_label = ss_ranked_until.strftime('%Y-%m-%d %H:%M') if ss_ranked_until else "full"
            _step(0.02, f"Updating ScoreSaber Ranked Maps (last fetch: {fetch_label})...")
            leaderboards = _get_scoresaber_leaderboards_ranked(session, progress=_ss_leaderboard_progress, fetch_until=ss_ranked_until)
            map_store = MapStore()
            map_store.ss_ranked_maps = leaderboards
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            pass
    else:
        print("2. ScoreSaber Ranked Maps 取得スキップ（オプションが無効）")
        _step(0.05, "Skipping ScoreSaber Ranked Maps...")

    if options.fetch_bl_ranked_maps:
        try:
            def _bl_leaderboard_progress(page: int, max_pages: Optional[int]) -> None:
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
            if options.bl_ranked_until is not None:
                bl_ranked_until = options.bl_ranked_until
            else:
                bl_last_fetched = _read_cache_fetched_at(CACHE_DIR / "beatleader_ranked_maps.json")
                if bl_last_fetched is not None:
                    # 月次更新でrankedTime相当フィールドが変わる可能性があるため、前回取得日時より60日前から再取得する
                    bl_ranked_until = bl_last_fetched - timedelta(days=60)
                    print(f"BeatLeader Ranked Maps 前回取得日時: {bl_last_fetched.strftime('%Y-%m-%d %H:%M:%S')} UTC → 60日遡り: {bl_ranked_until.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                else:
                    bl_ranked_until = None
                    print("BeatLeader Ranked Maps: 初回取得のため全件取得")
            fetch_label = bl_ranked_until.strftime('%Y-%m-%d %H:%M') if bl_ranked_until else "full"
            _step(0.05, f"Updating BeatLeader Ranked Maps (last fetch: {fetch_label})...")
            _get_beatleader_leaderboards_ranked(session, progress=_bl_leaderboard_progress, fetch_until=bl_ranked_until)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            pass
    else:
        print("3. BeatLeader Ranked Maps 取得スキップ（オプションが無効）")
        _step(0.08, "Skipping BeatLeader Ranked Maps...")

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
            _save_player_index(player_index, update_fetched_at=False)
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
        if options.fetch_scoresaber:
            # まず ScoreSaber の基本情報（PP / ランク）を最新化しておく。
            try:
                print("5. ScoreSaber 基本情報更新...")
                ss_latest = _fetch_scoresaber_player_basic(scoresaber_id, session)
            except Exception as exc:  # noqa: BLE001
                _rethrow_if_cancelled(exc)
                ss_latest = None

            if ss_latest is not None:
                scoresaber_name = ss_latest.name or scoresaber_name
                scoresaber_country = ss_latest.country or scoresaber_country
                scoresaber_pp = ss_latest.pp
                scoresaber_rank_global = ss_latest.global_rank
                scoresaber_rank_country = ss_latest.country_rank

                try:
                    entry["scoresaber"] = ss_latest
                    _save_player_index(player_index, update_fetched_at=False)
                except Exception as exc:  # noqa: BLE001
                    _rethrow_if_cancelled(exc)
                    pass

            # スナップショット取得時にプレイヤースコアキャッシュも更新しておく。
            try:
                _step(0.15, "Fetching ScoreSaber player scores (page 1/?)...")

                def _ss_scores_progress(page: int, max_pages: Optional[int]) -> None:
                    if max_pages and max_pages > 0:
                        frac = max(0.0, min(1.0, page / max_pages))
                        msg = f"Fetching ScoreSaber player scores (page {page}/{max_pages})..."
                    else:
                        frac = 0.0
                        msg = f"Fetching ScoreSaber player scores (page {page}/?)..."
                    _step(0.15 + 0.05 * frac, msg)

                print("5. ScoreSaber プレイヤースコアキャッシュ更新...")
                _ss_effective_until = datetime(2000, 1, 1) if options.ss_fetch_all else options.ss_fetch_until
                if options.ss_fetch_all:
                    print("ScoreSaber: 全スコア再取得モード (fetch_all=True)")
                _ss_score_cache_path = CACHE_DIR / f"scoresaber_player_scores_{scoresaber_id}.json"
                _ss_score_fetched_at = _read_cache_fetched_at(_ss_score_cache_path)
                if _ss_score_fetched_at is not None:
                    print(f"ScoreSaberプレイヤースコア 前回取得日時: {_ss_score_fetched_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                    _ss_score_label = _ss_score_fetched_at.strftime('%Y-%m-%d %H:%M')
                else:
                    print("ScoreSaberプレイヤースコア: 初回取得")
                    _ss_score_label = "new"
                _step(0.15, f"Fetching ScoreSaber player scores (last fetch: {_ss_score_label}, page 1/?)...")
                _get_scoresaber_player_scores(scoresaber_id, session, progress=_ss_scores_progress, fetch_until=_ss_effective_until)
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
        else:
            print("5-6. ScoreSaber プレイヤーデータ取得スキップ（オプションが無効）")
            _step(0.20, "Skipping ScoreSaber player data...")

    # BeatLeader 側も、Snapshot 取得時に基本情報（PP / ランク）を最新化しておく。
    beatleader: Optional[BeatLeaderPlayer] = bl
    if scoresaber_id and options.fetch_beatleader:
        try:
            print("6. BeatLeader 基本情報更新...")
            bl_latest = fetch_bl_player(scoresaber_id, session=session)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            bl_latest = None

        if bl_latest is not None:
            beatleader = bl_latest

            # 可能であれば players_index.json 側の BeatLeader 情報も更新しておく
            try:
                entry["beatleader"] = bl_latest
                _save_player_index(player_index, update_fetched_at=False)
            except Exception as exc:  # noqa: BLE001
                _rethrow_if_cancelled(exc)
                pass
    elif scoresaber_id and not options.fetch_beatleader:
        print("6. BeatLeader 基本情報取得スキップ（オプションが無効）")

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
        if options.fetch_beatleader:
            # BeatLeader 側もスナップショット取得時にプレイヤースコアキャッシュを更新しておく。
            try:
                _step(0.30, "Fetching BeatLeader player scores (page 1/?)...")

                def _bl_scores_progress(page: int, max_pages: Optional[int]) -> None:
                    if max_pages and max_pages > 0:
                        frac = max(0.0, min(1.0, page / max_pages))
                        msg = f"Fetching BeatLeader player scores (page {page}/{max_pages})..."
                    else:
                        frac = 0.0
                        msg = f"Fetching BeatLeader player scores (page {page}/?)..."
                    _step(0.30 + 0.05 * frac, msg)
                print("7. BeatLeader プレイヤースコアキャッシュ更新...")
                _bl_effective_until = datetime(2000, 1, 1) if options.bl_fetch_all else options.bl_fetch_until
                if options.bl_fetch_all:
                    print("BeatLeader: 全スコア再取得モード (fetch_all=True)")
                _bl_score_cache_path = CACHE_DIR / f"beatleader_player_scores_{beatleader_id}.json"
                _bl_score_fetched_at = _read_cache_fetched_at(_bl_score_cache_path)
                if _bl_score_fetched_at is not None:
                    print(f"BeatLeaderプレイヤースコア 前回取得日時: {_bl_score_fetched_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                    _bl_score_label = _bl_score_fetched_at.strftime('%Y-%m-%d %H:%M')
                else:
                    print("BeatLeaderプレイヤースコア: 初回取得")
                    _bl_score_label = "new"
                _step(0.30, f"Fetching BeatLeader player scores (last fetch: {_bl_score_label}, page 1/?)...")
                _get_beatleader_player_scores(beatleader_id, session, progress=_bl_scores_progress, fetch_until=_bl_effective_until)
            except Exception as exc:  # noqa: BLE001
                _rethrow_if_cancelled(exc)
                pass

            print("8. BeatLeader プレイヤーステータス取得...")
            _step(0.35, "Fetching BeatLeader player stats...")
            bl_stats = _get_beatleader_player_stats(beatleader_id, session)
            if bl_stats:
                print("8.1 BeatLeader プレイヤーステータス取得完了。")
                try:
                    bl_avg = bl_stats.get("averageRankedAccuracy")
                    if bl_avg is not None:
                        beatleader_average_ranked_acc = float(bl_avg) * 100.0
                except (TypeError, ValueError):
                    pass

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
        else:
            print("7-8. BeatLeader プレイヤーデータ取得スキップ（オプションが無効）")
            _step(0.35, "Skipping BeatLeader player data...")

    # AccSaber ランク / プレイ回数: まずは Overall のキャッシュから紐付け。
    # *_rank_global にグローバル順位、*_rank_country に国別順位を保持する。
    acc_overall_rank_global: Optional[int] = None
    acc_true_rank_global: Optional[int] = None
    acc_standard_rank_global: Optional[int] = None
    acc_tech_rank_global: Optional[int] = None
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
    acc_overall_avg_acc: Optional[float] = None
    acc_true_avg_acc: Optional[float] = None
    acc_standard_avg_acc: Optional[float] = None
    acc_tech_avg_acc: Optional[float] = None
    accsaber_true_fetched: bool = False
    accsaber_standard_fetched: bool = False
    accsaber_tech_fetched: bool = False
    accsaber_true_fetch_failed: bool = False
    accsaber_standard_fetch_failed: bool = False
    accsaber_tech_fetch_failed: bool = False
    accsaber_true_data_as_of: Optional[str] = None
    accsaber_standard_data_as_of: Optional[str] = None
    accsaber_tech_data_as_of: Optional[str] = None

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

    if options.fetch_accsaber:
        print("9. AccSaber プレイヤーステータス取得...")
        # AccSaber ランキングキャッシュが未更新の場合は再フェッチして fetched_at を最新化する
        if not _acc_ranking_refreshed:
            print("9.0 AccSaber ランキングキャッシュ再フェッチ...")
            _step(0.40, "Fetching AccSaber overall ranking...")
            try:
                _fetch_and_save_accsaber_ranking(
                    session,
                    progress=lambda msg, frac: _step(0.40 + frac * 0.03, msg),
                )
            except Exception as exc:  # noqa: BLE001
                _rethrow_if_cancelled(exc)
        _step(0.43, "Loading AccSaber overall cache...")
        acc_overall = _find_accsaber_for_scoresaber_id(scoresaber_id, session=session) if scoresaber_id else None

        # Overall はキャッシュから rank / plays を取得（rank はグローバル順位）
        if acc_overall is not None:
            acc_overall_rank_global = acc_overall.rank
            acc_overall_play_count = _parse_acc_plays(getattr(acc_overall, "plays", ""))
            acc_overall_ap = _parse_ap(getattr(acc_overall, "total_ap", ""))
            _oavg = getattr(acc_overall, "average_acc", "")
            if _oavg:
                try:
                    acc_overall_avg_acc = float(str(_oavg).replace(",", "")) * 100.0
                except (ValueError, TypeError):
                    pass

        # True / Standard / Tech は必要になったときだけ API 経由で取得（ベストエフォート）。
        if scoresaber_id:
            _true_api_err = False
            try:
                print("9.1 AccSaber True プレイヤーステータス取得...")
                _step(0.45, "Fetching AccSaber True leaderboard...")
                acc_true = _find_accsaber_skill_for_scoresaber_id(scoresaber_id, fetch_true, session=session)
            except Exception:  # noqa: BLE001
                acc_true = None
                _true_api_err = True
            if acc_true is not None:
                acc_true_rank_global = acc_true.rank
                acc_true_play_count = _parse_acc_plays(getattr(acc_true, "plays", ""))
                acc_true_ap = _parse_ap(getattr(acc_true, "total_ap", ""))
                _tavg = getattr(acc_true, "average_acc", "")
                if _tavg:
                    try:
                        acc_true_avg_acc = float(str(_tavg).replace(",", "")) * 100.0
                    except (ValueError, TypeError):
                        pass
                accsaber_true_fetched = True
            elif _true_api_err:
                accsaber_true_fetch_failed = True
                _prev_true = _find_last_successful_accsaber_snapshot(steam_id, "true")
                if _prev_true is not None:
                    acc_true_rank_global = _prev_true.accsaber_true_rank
                    acc_true_play_count = _prev_true.accsaber_true_play_count
                    acc_true_ap = _prev_true.accsaber_true_ap
                    accsaber_true_data_as_of = _prev_true.taken_at
                    _msg = f"AccSaber True: API fetch failed, using data from previous snapshot ({_prev_true.taken_at[:10]})"
                elif acc_overall is not None:
                    _cached_true_ap = _parse_ap(getattr(acc_overall, "true_ap", ""))
                    if _cached_true_ap:
                        acc_true_ap = _cached_true_ap
                    _msg = "AccSaber True: API fetch failed, no previous snapshot found"
                else:
                    _msg = "AccSaber True: API fetch failed"
                print(f"9.1 {_msg}")
                _warnings.append(_msg)

            _std_api_err = False
            try:
                print("9.2 AccSaber Standard プレイヤーステータス取得...")
                _step(0.50, "Fetching AccSaber Standard leaderboard...")
                acc_standard = _find_accsaber_skill_for_scoresaber_id(scoresaber_id, fetch_standard, session=session)
            except Exception:  # noqa: BLE001
                acc_standard = None
                _std_api_err = True
            if acc_standard is not None:
                acc_standard_rank_global = acc_standard.rank
                acc_standard_play_count = _parse_acc_plays(getattr(acc_standard, "plays", ""))
                acc_standard_ap = _parse_ap(getattr(acc_standard, "total_ap", ""))
                _savg = getattr(acc_standard, "average_acc", "")
                if _savg:
                    try:
                        acc_standard_avg_acc = float(str(_savg).replace(",", "")) * 100.0
                    except (ValueError, TypeError):
                        pass
                accsaber_standard_fetched = True
            elif _std_api_err:
                accsaber_standard_fetch_failed = True
                _prev_std = _find_last_successful_accsaber_snapshot(steam_id, "standard")
                if _prev_std is not None:
                    acc_standard_rank_global = _prev_std.accsaber_standard_rank
                    acc_standard_play_count = _prev_std.accsaber_standard_play_count
                    acc_standard_ap = _prev_std.accsaber_standard_ap
                    accsaber_standard_data_as_of = _prev_std.taken_at
                    _msg = f"AccSaber Standard: API fetch failed, using data from previous snapshot ({_prev_std.taken_at[:10]})"
                elif acc_overall is not None:
                    _cached_standard_ap = _parse_ap(getattr(acc_overall, "standard_ap", ""))
                    if _cached_standard_ap:
                        acc_standard_ap = _cached_standard_ap
                    _msg = "AccSaber Standard: API fetch failed, no previous snapshot found"
                else:
                    _msg = "AccSaber Standard: API fetch failed"
                print(f"9.2 {_msg}")
                _warnings.append(_msg)

            _tech_api_err = False
            try:
                print("9.3 AccSaber Tech プレイヤーステータス取得...")
                _step(0.55, "Fetching AccSaber Tech leaderboard...")
                acc_tech = _find_accsaber_skill_for_scoresaber_id(scoresaber_id, fetch_tech, session=session)
            except Exception:  # noqa: BLE001
                acc_tech = None
                _tech_api_err = True
            if acc_tech is not None:
                acc_tech_rank_global = acc_tech.rank
                acc_tech_play_count = _parse_acc_plays(getattr(acc_tech, "plays", ""))
                acc_tech_ap = _parse_ap(getattr(acc_tech, "total_ap", ""))
                _techavg = getattr(acc_tech, "average_acc", "")
                if _techavg:
                    try:
                        acc_tech_avg_acc = float(str(_techavg).replace(",", "")) * 100.0
                    except (ValueError, TypeError):
                        pass
                accsaber_tech_fetched = True
            elif _tech_api_err:
                accsaber_tech_fetch_failed = True
                _prev_tech = _find_last_successful_accsaber_snapshot(steam_id, "tech")
                if _prev_tech is not None:
                    acc_tech_rank_global = _prev_tech.accsaber_tech_rank
                    acc_tech_play_count = _prev_tech.accsaber_tech_play_count
                    acc_tech_ap = _prev_tech.accsaber_tech_ap
                    accsaber_tech_data_as_of = _prev_tech.taken_at
                    _msg = f"AccSaber Tech: API fetch failed, using data from previous snapshot ({_prev_tech.taken_at[:10]})"
                elif acc_overall is not None:
                    _cached_tech_ap = _parse_ap(getattr(acc_overall, "tech_ap", ""))
                    if _cached_tech_ap:
                        acc_tech_ap = _cached_tech_ap
                    _msg = "AccSaber Tech: API fetch failed, no previous snapshot found"
                else:
                    _msg = "AccSaber Tech: API fetch failed"
                print(f"9.3 {_msg}")
                _warnings.append(_msg)

            # JP 国内順位は accsaber_ranking.json 全体と players_index.json を使って計算する
            try:
                print("9.4 AccSaber 国内ランク計算...")
                _step(0.60, "Loading AccSaber players for JP ranks...")
                acc_players = _load_accsaber_players()
            except Exception:  # noqa: BLE001
                acc_players = []

            if acc_players and scoresaber_country:
                ss_country_by_id: dict[str, str] = {}
                try:
                    index_all = _load_player_index()
                except Exception:  # noqa: BLE001
                    index_all = {}

                for entry_all in index_all.values():
                    ss_player = entry_all.get("scoresaber")
                    if isinstance(ss_player, ScoreSaberPlayer) and ss_player.id and ss_player.country:
                        ss_country_by_id[str(ss_player.id)] = str(ss_player.country).upper()

                # BeatLeader キャッシュを先に処理する（app.py と同じ優先順位）。
                # BL の id は Steam ID (= ScoreSaber ID) と共通。
                # scoresaber_ranking.json に古い国コードが残っている場合でも
                # BL の最新データが優先されるよう、SS ファイルループより前に処理する。
                for bl_cache_name in ["beatleader_JP.json", "beatleader_ranking.json"]:
                    bl_cache_path = CACHE_DIR / bl_cache_name
                    if not bl_cache_path.exists():
                        continue
                    try:
                        bl_cache_data = json.loads(bl_cache_path.read_text(encoding="utf-8"))
                        for bl_item in bl_cache_data:
                            if not isinstance(bl_item, dict):
                                continue
                            bl_id = str(bl_item.get("id") or "")
                            bl_cc = str(bl_item.get("country") or "").upper()
                            if bl_id and bl_cc and bl_id not in ss_country_by_id:
                                ss_country_by_id[bl_id] = bl_cc
                    except Exception:  # noqa: BLE001
                        continue

                # players_index に無い SS プレイヤーを scoresaber_ranking.json から補完
                # （BL-only として登録されているが実際は SS にも存在するプレイヤー対応）
                for ss_cache_name in ["scoresaber_ranking.json", "scoresaber_JP.json", "scoresaber_ALL.json"]:
                    ss_cache_path = CACHE_DIR / ss_cache_name
                    if not ss_cache_path.exists():
                        continue
                    try:
                        ss_cache_data = json.loads(ss_cache_path.read_text(encoding="utf-8"))
                        for ss_item in ss_cache_data:
                            if not isinstance(ss_item, dict):
                                continue
                            sid_c = str(ss_item.get("id") or "")
                            cc_c = str(ss_item.get("country") or "").upper()
                            if sid_c and cc_c and sid_c not in ss_country_by_id:
                                ss_country_by_id[sid_c] = cc_c
                    except Exception:  # noqa: BLE001
                        continue

                country = str(scoresaber_country).upper()
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
                    def _rank_for(get_ap, skip_zero: bool = False) -> Optional[int]:
                        # Overall は全員を母集団にする。
                        # True / Standard / Tech は AP > 0 のプレイヤーのみを母集団にする。
                        # ランキング画面 (app.py の _build_skill_country_ranks) と同じ方針。
                        pool = [
                            p for p in same_country_players
                            if not skip_zero or _parse_ap(get_ap(p)) > 0.0
                        ]
                        if not pool:
                            return None
                        players_sorted = sorted(
                            pool,
                            key=lambda p: _parse_ap(get_ap(p)),
                            reverse=True,
                        )
                        rank_val = 1
                        for p in players_sorted:
                            sid = getattr(p, "scoresaber_id", None)
                            if not sid:
                                continue  # sid なしは順位にカウントしない (app.py と同じ挙動)
                            if str(sid) == scoresaber_id:
                                return rank_val
                            rank_val += 1
                        return None

                    acc_overall_rank_country  = _rank_for(lambda p: getattr(p, "total_ap",   ""), skip_zero=False)
                    acc_true_rank_country     = _rank_for(lambda p: getattr(p, "true_ap",     ""), skip_zero=True)
                    acc_standard_rank_country = _rank_for(lambda p: getattr(p, "standard_ap", ""), skip_zero=True)
                    acc_tech_rank_country     = _rank_for(lambda p: getattr(p, "tech_ap",     ""), skip_zero=True)

        # AccSaber プレイリスト総譜面数を更新する（accsaber_playlist_counts.json）
        try:
            get_accsaber_playlist_map_counts_with_meta(session=session)
        except Exception:  # noqa: BLE001
            pass
        # AccSaber Reloaded 総譜面数を更新する（accsaber_reloaded_map_counts.json）
        try:
            _fetch_reloaded_map_counts(session=session)
        except Exception:  # noqa: BLE001
            pass
        # AccSaber マップデータ（ranked-maps + プレイリスト）をキャッシュに保存する
        try:
            _step(0.63, "Fetching AccSaber map data for playlist cache...")
            _fetch_and_save_accsaber_maps(session=session)
        except Exception:  # noqa: BLE001
            pass
        # AccSaber プレイヤースコアをキャッシュに保存する
        if scoresaber_id:
            try:
                _step(0.64, "Fetching AccSaber player scores for cache...")
                _fetch_and_save_acc_player_scores(scoresaber_id, session=session)
            except Exception:  # noqa: BLE001
                pass
    else:
        print("9. AccSaber 取得スキップ（オプションが無効）")
        _step(0.60, "Skipping AccSaber data...")

    # Overall AP は True / Standard / Tech の AP を合算した値とする
    if any(v is not None for v in (acc_true_ap, acc_standard_ap, acc_tech_ap)):
        acc_overall_ap = (acc_true_ap or 0.0) + (acc_standard_ap or 0.0) + (acc_tech_ap or 0.0)

    # AccSaber Reloaded ランク情報を取得する
    accsaber_reloaded_overall_rank:          Optional[int]   = None
    accsaber_reloaded_overall_rank_country:  Optional[int]   = None
    accsaber_reloaded_overall_ap:            Optional[float] = None
    accsaber_reloaded_overall_ranked_plays:  Optional[int]   = None
    accsaber_reloaded_true_rank:             Optional[int]   = None
    accsaber_reloaded_true_rank_country:     Optional[int]   = None
    accsaber_reloaded_true_ap:               Optional[float] = None
    accsaber_reloaded_true_ranked_plays:     Optional[int]   = None
    accsaber_reloaded_standard_rank:         Optional[int]   = None
    accsaber_reloaded_standard_rank_country: Optional[int]   = None
    accsaber_reloaded_standard_ap:           Optional[float] = None
    accsaber_reloaded_standard_ranked_plays: Optional[int]   = None
    accsaber_reloaded_tech_rank:             Optional[int]   = None
    accsaber_reloaded_tech_rank_country:     Optional[int]   = None
    accsaber_reloaded_tech_ap:               Optional[float] = None
    accsaber_reloaded_tech_ranked_plays:     Optional[int]   = None
    accsaber_reloaded_overall_avg_acc:       Optional[float] = None
    accsaber_reloaded_true_avg_acc:          Optional[float] = None
    accsaber_reloaded_standard_avg_acc:      Optional[float] = None
    accsaber_reloaded_tech_avg_acc:          Optional[float] = None
    accsaber_reloaded_xp:                    Optional[float] = None
    accsaber_reloaded_xp_level:              Optional[int]   = None
    accsaber_reloaded_xp_rank:               Optional[int]   = None
    accsaber_reloaded_xp_rank_country:       Optional[int]   = None

    # AccSaber Reloaded の userId は Steam ID（BeatLeader ID と同じ）なので beatleader_id を優先する。
    # ScoreSaber が非 Steam 形式の ID（例: 3117609721598571）の場合に scoresaber_id を渡すと
    # リーダーボードを全ページ走査してもマッチしないため、長時間固まる原因となる。
    _rl_player_id = beatleader_id or scoresaber_id
    if options.fetch_accsaber_reloaded and _rl_player_id:
        print("9.4R AccSaber Reloaded プレイヤーステータス取得...")
        _step(0.62, "Fetching AccSaber Reloaded ranks...")
        _rl_country = (scoresaber_country or "").upper() or None
        try:
            _rl_result = _fetch_accsaber_reloaded(_rl_player_id, country=_rl_country, session=session)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            _rl_result = {}

        def _pick(cat: str, attr: str, conv):
            p = _rl_result.get(cat)
            if p is None:
                return None
            v = getattr(p, attr, None)
            if v is None:
                return None
            try:
                return conv(v)
            except (TypeError, ValueError):
                return None

        accsaber_reloaded_overall_rank          = _pick("overall",  "rank_global",   int)
        accsaber_reloaded_overall_rank_country  = _pick("overall",  "rank_country",  int)
        accsaber_reloaded_overall_ap            = _pick("overall",  "ap",            float)
        accsaber_reloaded_overall_ranked_plays  = _pick("overall",  "ranked_plays",  int)
        accsaber_reloaded_true_rank             = _pick("true",     "rank_global",   int)
        accsaber_reloaded_true_rank_country     = _pick("true",     "rank_country",  int)
        accsaber_reloaded_true_ap               = _pick("true",     "ap",            float)
        accsaber_reloaded_true_ranked_plays     = _pick("true",     "ranked_plays",  int)
        accsaber_reloaded_standard_rank         = _pick("standard", "rank_global",   int)
        accsaber_reloaded_standard_rank_country = _pick("standard", "rank_country",  int)
        accsaber_reloaded_standard_ap           = _pick("standard", "ap",            float)
        accsaber_reloaded_standard_ranked_plays = _pick("standard", "ranked_plays",  int)
        accsaber_reloaded_tech_rank             = _pick("tech",     "rank_global",   int)
        accsaber_reloaded_tech_rank_country     = _pick("tech",     "rank_country",  int)
        accsaber_reloaded_tech_ap               = _pick("tech",     "ap",            float)
        accsaber_reloaded_tech_ranked_plays     = _pick("tech",     "ranked_plays",  int)
        accsaber_reloaded_overall_avg_acc       = _pick("overall",  "average_acc",   lambda v: float(v) * 100.0)
        accsaber_reloaded_true_avg_acc          = _pick("true",     "average_acc",   lambda v: float(v) * 100.0)
        accsaber_reloaded_standard_avg_acc      = _pick("standard", "average_acc",   lambda v: float(v) * 100.0)
        accsaber_reloaded_tech_avg_acc          = _pick("tech",     "average_acc",   lambda v: float(v) * 100.0)

        # XP ランク
        try:
            _rl_xp_result = _fetch_accsaber_reloaded_xp(_rl_player_id, country=_rl_country, session=session)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            _rl_xp_result = None
        if _rl_xp_result is not None:
            accsaber_reloaded_xp            = _rl_xp_result.xp
            accsaber_reloaded_xp_level      = _rl_xp_result.level
            accsaber_reloaded_xp_rank       = _rl_xp_result.rank_global
            accsaber_reloaded_xp_rank_country = _rl_xp_result.rank_country

        print("9.4R AccSaber Reloaded プレイヤーステータス取得完了。")

        # AccSaber Reloaded 全マップデータをキャッシュに保存する
        try:
            _step(0.65, "Fetching AccSaber Reloaded map data for playlist cache...")
            _fetch_and_save_rl_maps(session=session)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
        # AccSaber Reloaded プレイヤースコアをキャッシュに保存する
        try:
            _step(0.66, "Fetching AccSaber Reloaded player scores for cache...")
            _fetch_and_save_rl_player_scores(_rl_player_id, session=session)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
    else:
        if not options.fetch_accsaber_reloaded:
            print("9.4R AccSaber Reloaded 取得スキップ（オプションが無効）")
        else:
            print("9.4R AccSaber Reloaded 取得スキップ（BeatLeader ID / ScoreSaber ID が未取得）")

    # ScoreSaber / BeatLeader のスコア一覧から★別統計を集計する（失敗した場合は空リスト）。
    if options.fetch_ss_star_stats:
        try:
            print("9.5 ScoreSaber ★別統計集計...")
            _step(0.70, "Collecting ScoreSaber star stats...")
            star_stats: list[StarClearStat] = _collect_star_stats_from_scoresaber(scoresaber_id, session) if scoresaber_id else []
        except Exception:  # noqa: BLE001
            print("★別統計の集計に失敗しました。")
            star_stats = []
    else:
        print("9.5 ScoreSaber ★別統計集計スキップ（オプションが無効）")
        _step(0.70, "Skipping ScoreSaber star stats...")
        star_stats = []

    if options.fetch_bl_star_stats:
        try:
            print("9.6 BeatLeader ★別統計集計...")
            _step(0.80, "Collecting BeatLeader star stats...")
            beatleader_star_stats: list[StarClearStat] = (
                collect_beatleader_star_stats(beatleader_id, session) if beatleader_id else []
            )
        except Exception:  # noqa: BLE001
            beatleader_star_stats = []
            print("9.6 BeatLeader ★別統計集計完了。")
    else:
        print("9.6 BeatLeader ★別統計集計スキップ（オプションが無効）")
        _step(0.80, "Skipping BeatLeader star stats...")
        beatleader_star_stats = []
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
        accsaber_overall_avg_acc=acc_overall_avg_acc,
        accsaber_true_avg_acc=acc_true_avg_acc,
        accsaber_standard_avg_acc=acc_standard_avg_acc,
        accsaber_tech_avg_acc=acc_tech_avg_acc,
        # AccSaber 国別ランク
        accsaber_overall_rank_country=acc_overall_rank_country,
        accsaber_true_rank_country=acc_true_rank_country,
        accsaber_standard_rank_country=acc_standard_rank_country,
        accsaber_tech_rank_country=acc_tech_rank_country,
        beatleader_average_ranked_acc=beatleader_average_ranked_acc,
        beatleader_total_play_count=beatleader_total_play_count,
        beatleader_ranked_play_count=beatleader_ranked_play_count,
        accsaber_true_fetched=accsaber_true_fetched,
        accsaber_standard_fetched=accsaber_standard_fetched,
        accsaber_tech_fetched=accsaber_tech_fetched,
        accsaber_true_fetch_failed=accsaber_true_fetch_failed,
        accsaber_standard_fetch_failed=accsaber_standard_fetch_failed,
        accsaber_tech_fetch_failed=accsaber_tech_fetch_failed,
        accsaber_true_data_as_of=accsaber_true_data_as_of,
        accsaber_standard_data_as_of=accsaber_standard_data_as_of,
        accsaber_tech_data_as_of=accsaber_tech_data_as_of,
        # AccSaber Reloaded ランク
        accsaber_reloaded_overall_rank=accsaber_reloaded_overall_rank,
        accsaber_reloaded_overall_rank_country=accsaber_reloaded_overall_rank_country,
        accsaber_reloaded_overall_ap=accsaber_reloaded_overall_ap,
        accsaber_reloaded_overall_ranked_plays=accsaber_reloaded_overall_ranked_plays,
        accsaber_reloaded_true_rank=accsaber_reloaded_true_rank,
        accsaber_reloaded_true_rank_country=accsaber_reloaded_true_rank_country,
        accsaber_reloaded_true_ap=accsaber_reloaded_true_ap,
        accsaber_reloaded_true_ranked_plays=accsaber_reloaded_true_ranked_plays,
        accsaber_reloaded_standard_rank=accsaber_reloaded_standard_rank,
        accsaber_reloaded_standard_rank_country=accsaber_reloaded_standard_rank_country,
        accsaber_reloaded_standard_ap=accsaber_reloaded_standard_ap,
        accsaber_reloaded_standard_ranked_plays=accsaber_reloaded_standard_ranked_plays,
        accsaber_reloaded_tech_rank=accsaber_reloaded_tech_rank,
        accsaber_reloaded_tech_rank_country=accsaber_reloaded_tech_rank_country,
        accsaber_reloaded_tech_ap=accsaber_reloaded_tech_ap,
        accsaber_reloaded_tech_ranked_plays=accsaber_reloaded_tech_ranked_plays,
        accsaber_reloaded_overall_avg_acc=accsaber_reloaded_overall_avg_acc,
        accsaber_reloaded_true_avg_acc=accsaber_reloaded_true_avg_acc,
        accsaber_reloaded_standard_avg_acc=accsaber_reloaded_standard_avg_acc,
        accsaber_reloaded_tech_avg_acc=accsaber_reloaded_tech_avg_acc,
        accsaber_reloaded_xp=accsaber_reloaded_xp,
        accsaber_reloaded_xp_level=accsaber_reloaded_xp_level,
        accsaber_reloaded_xp_rank=accsaber_reloaded_xp_rank,
        accsaber_reloaded_xp_rank_country=accsaber_reloaded_xp_rank_country,
        star_stats=star_stats,
        beatleader_star_stats=beatleader_star_stats,
    )
    print("10.1 スナップショットオブジェクト構築完了。")
    _step(0.90, "Saving snapshot...")
    snapshot.warnings = _warnings
    snapshot.accsaber_cache_used = bool(_warnings)

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
