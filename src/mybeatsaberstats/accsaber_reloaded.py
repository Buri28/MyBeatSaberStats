from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from .snapshot import BASE_DIR

BASE_URL = "https://api.accsaberreloaded.com/v1"

# AccSaber Reloaded カテゴリ UUID（/v1/categories で確認済み）
CATEGORY_IDS: Dict[str, str] = {
    "overall":  "b0000000-0000-0000-0000-000000000005",
    "true":     "b0000000-0000-0000-0000-000000000001",
    "standard": "b0000000-0000-0000-0000-000000000002",
    "tech":     "b0000000-0000-0000-0000-000000000003",
}

# overall は maps エンドポイントでは集計しない（true+standard+tech の合計で算出）
_MAP_COUNT_CATEGORY_IDS: Dict[str, str] = {k: v for k, v in CATEGORY_IDS.items() if k != "overall"}

_PAGE_SIZE = 50

_MAP_COUNTS_CACHE_FILE: Path = BASE_DIR / "cache" / "accsaber_reloaded_map_counts.json"


def _load_map_counts_file_cache() -> Dict[str, Dict]:
    """ファイルキャッシュから前回の総譜面数を読み込む。"""
    try:
        data = json.loads(_MAP_COUNTS_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            result: Dict[str, Dict] = {}
            for k in ("true", "standard", "tech"):
                entry = data.get(k)
                if isinstance(entry, dict):
                    count = entry.get("count")
                    if isinstance(count, (int, float)) and count > 0:
                        result[k] = {"count": int(count)}
            return result
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_map_counts_file_cache(per_cat: Dict[str, Dict]) -> None:
    """総譜面数をファイルキャッシュに保存する。"""
    try:
        now_z = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data: Dict = {"fetched_at": now_z}
        data.update(per_cat)
        _MAP_COUNTS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MAP_COUNTS_CACHE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


def fetch_reloaded_map_counts(
    session: Optional[requests.Session] = None,
) -> Dict[str, int]:
    """AccSaber Reloaded の各カテゴリのランク済み難易度数を取得してキャッシュする。

    /v1/maps を全ページ取得し、各 difficulty の categoryId を数える。
    (タイトル単位ではなく難易度単位でカウントするため全ページ走査が必要)
    overall = true + standard + tech の合計で算出。
    取得失敗時はファイルキャッシュの前回値を使用。
    戻り値: {"true": N, "standard": N, "tech": N, "overall": N}
    """
    if session is None:
        session = requests.Session()

    # UUID → カテゴリ名 の逆引き辞書
    _uuid_to_cat: Dict[str, str] = {v: k for k, v in _MAP_COUNT_CATEGORY_IDS.items()}

    try:
        raw_counts: Dict[str, int] = {cat: 0 for cat in _MAP_COUNT_CATEGORY_IDS}
        page = 0
        page_size = 50
        while True:
            resp = session.get(
                f"{BASE_URL}/maps",
                params={"page": page, "size": page_size},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            for song in data.get("content", []):
                for diff in song.get("difficulties", []):
                    if not diff.get("active", False):
                        continue
                    cat = _uuid_to_cat.get(diff.get("categoryId", ""))
                    if cat:
                        raw_counts[cat] += 1
            if data.get("last", True):
                break
            page += 1

        per_cat: Dict[str, Dict] = {cat: {"count": cnt} for cat, cnt in raw_counts.items() if cnt > 0}
        if per_cat:
            _save_map_counts_file_cache(per_cat)

        counts: Dict[str, int] = dict(raw_counts)
        overall_parts = [counts[k] for k in ("true", "standard", "tech") if counts.get(k, 0) > 0]
        if overall_parts:
            counts["overall"] = sum(overall_parts)
        return counts

    except Exception:  # noqa: BLE001
        # API 失敗時はファイルキャッシュにフォールバック
        return get_reloaded_map_counts_from_cache()


def get_reloaded_map_counts_from_cache() -> Dict[str, int]:
    """ファイルキャッシュから AccSaber Reloaded の総譜面数を返す。API は叩かない。

    戻り値: {"true": 109, "standard": ..., "tech": ..., "overall": ...}
    存在しないカテゴリのキーは含まれない。
    """
    file_cache = _load_map_counts_file_cache()
    counts: Dict[str, int] = {k: v["count"] for k, v in file_cache.items()}
    overall_parts = [counts[k] for k in ("true", "standard", "tech") if k in counts]
    if overall_parts:
        counts["overall"] = sum(overall_parts)
    return counts


@dataclass
class AccSaberReloadedPlayer:
    player_id: str
    name: str
    country: str
    ap: float
    average_acc: float
    ranked_plays: int
    rank_global: int
    rank_country: int


def _search_in_leaderboard(
    category_uuid: str,
    player_id: str,
    country: Optional[str],
    session: requests.Session,
) -> Optional[AccSaberReloadedPlayer]:
    """指定カテゴリのリーダーボードからプレイヤーを検索する。

    country を指定すると国別エンドポイント（/leaderboards/{uuid}/country/{cc}）を使い、
    ページ数を大幅に削減できる。見つからない場合は None を返す。
    レスポンスの `ranking` フィールドには全体順位が、`countryRanking` には国内順位が入る。
    """
    if country:
        url = f"{BASE_URL}/leaderboards/{category_uuid}/country/{country.upper()}"
    else:
        url = f"{BASE_URL}/leaderboards/{category_uuid}"

    page = 0
    while True:
        try:
            resp = session.get(url, params={"page": page, "size": _PAGE_SIZE}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception:  # noqa: BLE001
            break

        content = data.get("content")
        if not isinstance(content, list) or not content:
            break

        for entry in content:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("userId", "")) == player_id:
                try:
                    return AccSaberReloadedPlayer(
                        player_id=player_id,
                        name=str(entry.get("userName", "")),
                        country=str(entry.get("country", "")),
                        ap=float(entry.get("ap", 0.0)),
                        average_acc=float(entry.get("averageAcc", 0.0)),
                        ranked_plays=int(entry.get("rankedPlays", 0)),
                        rank_global=int(entry.get("ranking", 0)),
                        rank_country=int(entry.get("countryRanking", 0)),
                    )
                except (TypeError, ValueError):
                    return None

        # 最終ページなら終了
        if data.get("last", True):
            break
        page += 1

    return None


def fetch_player_all_categories(
    player_id: str,
    country: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> Dict[str, Optional[AccSaberReloadedPlayer]]:
    """指定プレイヤーの AccSaber Reloaded 全カテゴリのランク情報を取得する。

    戻り値: {"overall": ..., "true": ..., "standard": ..., "tech": ...}
    各カテゴリで見つからない / API エラーの場合は None。
    """
    if not player_id:
        return {cat: None for cat in CATEGORY_IDS}

    if session is None:
        session = requests.Session()

    result: Dict[str, Optional[AccSaberReloadedPlayer]] = {}
    for category, uuid in CATEGORY_IDS.items():
        try:
            result[category] = _search_in_leaderboard(uuid, player_id, country, session)
        except Exception:  # noqa: BLE001
            result[category] = None

    return result


# ---------------------------------------------------------------------------
# XP ランキング
# ---------------------------------------------------------------------------

@dataclass
class AccSaberReloadedXP:
    xp: float
    level: int
    rank_global: int
    rank_country: int


def fetch_player_xp(
    player_id: str,
    country: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> Optional[AccSaberReloadedXP]:
    """プレイヤーの XP・レベル・XP ランクを取得する。

    /v1/leaderboards/xp から country フィルターでページを走査し、
    対象プレイヤーを見つけたら AccSaberReloadedXP を返す。
    見つからない場合は None。
    """
    if not player_id:
        return None

    if session is None:
        session = requests.Session()

    params: Dict = {"size": _PAGE_SIZE}
    if country:
        params["country"] = country.upper()

    url = f"{BASE_URL}/leaderboards/xp"
    page = 0
    while True:
        try:
            resp = session.get(url, params={**params, "page": page}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception:  # noqa: BLE001
            break

        content = data.get("content")
        if not isinstance(content, list) or not content:
            break

        for entry in content:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("userId", "")) == player_id:
                try:
                    return AccSaberReloadedXP(
                        xp=float(entry.get("totalXp", 0.0)),
                        level=int(entry.get("level", 0)),
                        rank_global=int(entry.get("ranking", 0)),
                        rank_country=int(entry.get("countryRanking", 0)),
                    )
                except (TypeError, ValueError):
                    return None

        if data.get("last", True):
            break
        page += 1

    return None


# ---------------------------------------------------------------------------
# 未プレイ抽出用
# ---------------------------------------------------------------------------

# AccSaber Reloaded の difficulty 名 → Beat Saber bplist 形式の難易度名
_RL_DIFF_TO_BS: Dict[str, str] = {
    "EASY":         "Easy",
    "NORMAL":       "Normal",
    "HARD":         "Hard",
    "EXPERT":       "Expert",
    "EXPERT_PLUS":  "ExpertPlus",
}


def fetch_all_maps_full(
    session: Optional[requests.Session] = None,
    on_progress=None,
) -> List[Dict]:
    """AccSaber Reloaded の全マップ情報を全ページ取得して返す。

    戻り値: content 配列の要素をそのまま結合したリスト。
    各要素には songHash, songName, beatsaverCode, difficulties[] が含まれる。

    on_progress(current_page: int, total_pages: int) が指定されていれば各ページで呼び出す。
    RuntimeError を投げると取得を中断できる（キャンセル用）。
    """
    if session is None:
        session = requests.Session()

    all_maps: List[Dict] = []
    page = 0
    total_pages: Optional[int] = None

    while True:
        resp = session.get(
            f"{BASE_URL}/maps",
            params={"page": page, "size": _PAGE_SIZE},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        all_maps.extend(data.get("content", []))

        if total_pages is None:
            total_pages = data.get("totalPages", 1)

        if on_progress is not None:
            on_progress(page + 1, total_pages)

        if data.get("last", True):
            break
        page += 1

    return all_maps


def fetch_player_scored_diff_ids(
    player_id: str,
    session: Optional[requests.Session] = None,
) -> Dict[str, set]:
    """AccSaber Reloaded でプレイヤーがスコアを持つ難易度 UUID をカテゴリ別に取得する。

    Returns:
        {"true": {uuid, ...}, "standard": {uuid, ...}, "tech": {uuid, ...}}
    空のセットも含む辞書を返す。API エラー時も同様に空辞書を返す。
    """
    if not player_id:
        return {}

    if session is None:
        session = requests.Session()

    _uuid_to_cat = {v: k for k, v in CATEGORY_IDS.items()}
    result: Dict[str, set] = {"true": set(), "standard": set(), "tech": set()}

    page = 0
    while True:
        try:
            resp = session.get(
                f"{BASE_URL}/users/{player_id}/scores",
                params={"page": page, "size": _PAGE_SIZE},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:  # noqa: BLE001
            break

        content = data.get("content", [])
        if not content:
            break

        for score in content:
            diff_id = score.get("mapDifficultyId")
            cat = _uuid_to_cat.get(score.get("categoryId", ""))
            if diff_id and cat and cat in result:
                result[cat].add(diff_id)

        if data.get("last", True):
            break
        page += 1

    return result


def build_unplayed_bplist(
    all_maps: List[Dict],
    played_set: set,
    category: str,
    played_bl_ids: Optional[set] = None,
    played_ss_ids: Optional[set] = None,
    played_rl_diff_ids: Optional[set] = None,
) -> Dict:
    """プレイ済み譜面を除いた AccSaber Reloaded カテゴリのプレイリスト (bplist) を構築する。

    played_rl_diff_ids: RL スコア API から取得した difficulty UUID の集合（最高精度）。
      指定時はこれのみでプレイ済み判定し、BL/SS 照合は行わない。
    played_set: {(hash_lower, characteristic, difficulty_bs_name)} の集合。
    played_bl_ids: BeatLeader leaderboard.id の集合（blLeaderboardId と照合）。
    played_ss_ids: ScoreSaber leaderboard.id の集合（ssLeaderboardId と照合）。
    category: "true" / "standard" / "tech"

    リパブリッシュ対応の方針:
      マップが再アップロードされると BeatSaver 上のハッシュが変わり、
      BeatLeader / ScoreSaber に新しいリーダーボードが作成される。
      AccSaber Reloaded は新リーダーボードに切り替える場合があるため、
      上記 3 沿いで検出できないケースが生じる場合がある。
    """
    cat_uuid = _MAP_COUNT_CATEGORY_IDS.get(category)
    if not cat_uuid:
        return {}

    songs_dict: Dict[str, Dict] = {}  # hash → {hash, songName, difficulties:[]}

    for song in all_maps:
        song_hash = song.get("songHash", "").lower()
        if not song_hash:
            continue

        for diff in song.get("difficulties", []):
            if not diff.get("active", False):
                continue
            if diff.get("categoryId") != cat_uuid:
                continue

            characteristic = diff.get("characteristic", "Standard")
            bs_diff = _RL_DIFF_TO_BS.get(diff.get("difficulty", ""))
            if not bs_diff:
                continue

            if played_rl_diff_ids is not None:
                # RL スコア API によるプレイ済み判定（最高精度: BL/SS 照合より優先）
                rl_diff_id = diff.get("id", "")
                if rl_diff_id and rl_diff_id in played_rl_diff_ids:
                    continue
            else:
                # フォールバック: BL/SS キャッシュによるプレイ済み推定
                # 照合優先度: blLeaderboardId > ssLeaderboardId > (hash, char, diff)
                # blLeaderboardId / ssLeaderboardId であればリパブリッシュ後のハッシュ変更にも対応できる
                bl_lb_id = diff.get("blLeaderboardId", "")
                if bl_lb_id and played_bl_ids and bl_lb_id in played_bl_ids:
                    continue
                ss_lb_id = str(diff.get("ssLeaderboardId", "") or "")
                if ss_lb_id and played_ss_ids and ss_lb_id in played_ss_ids:
                    continue
                if (song_hash, characteristic, bs_diff) in played_set:
                    continue

            if song_hash not in songs_dict:
                songs_dict[song_hash] = {
                    "hash": song_hash,
                    "songName": song.get("songName", ""),
                    "difficulties": [],
                }
            songs_dict[song_hash]["difficulties"].append(
                {"characteristic": characteristic, "name": bs_diff}
            )

    cat_label = {"true": "True", "standard": "Standard", "tech": "Tech"}.get(
        category, category.capitalize()
    )
    return {
        "playlistTitle": f"AccSaber Reloaded Unplayed - {cat_label}",
        "playlistAuthor": "MyBeatSaberStats",
        "image": "",
        "songs": list(songs_dict.values()),
    }
