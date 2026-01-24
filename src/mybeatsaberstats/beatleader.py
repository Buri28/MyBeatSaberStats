from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Callable

import requests
import time


BASE_URL = "https://api.beatleader.xyz"


@dataclass
class BeatLeaderPlayer:
    """ BeatLeader のプレイヤー情報。"""
    id: str
    name: str
    country: Optional[str]
    pp: float
    global_rank: int
    country_rank: int
    # Ranked play count if API exposes it
    ranked_play_count: int | None = None


def fetch_player(player_id: str, session: Optional[requests.Session] = None) -> Optional[BeatLeaderPlayer]:
    """BeatLeader の単一プレイヤー情報を ID から取得する。

    player_id は SteamID / OculusID などの BeatLeader / ScoreSaber 共通 ID を想定。
    見つからない場合やエラー時は None を返す。
    """

    if not player_id:
        return None

    if session is None:
        session = requests.Session()

    url = f"{BASE_URL}/player/{player_id}"
    try:
        # タイムアウトは短めにして UI フリーズを避ける
        resp = session.get(url, timeout=3)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    except Exception:
        # BeatLeader 側が落ちている、タイムアウトなどは呼び出し元で無視できるよう None
        return None

    try:
        data = resp.json()
    except Exception:
        return None
    # API レスポンスのフィールドを安全に取り出す
    pid = str(data.get("id") or player_id)
    name = str(data.get("name") or "")
    country = data.get("country") or None

    pp = float(data.get("pp") or 0.0)
    global_rank = int(data.get("rank") or 0)
    country_rank = int(data.get("countryRank") or 0)

    return BeatLeaderPlayer(
        id=pid,
        name=name,
        country=country,
        pp=pp,
        global_rank=global_rank,
        country_rank=country_rank,
    )


def fetch_players_ranking(
    min_pp: float = 0.0,
    session: Optional[requests.Session] = None,
    page_size: int = 100,
    max_pages: int = 200,
    progress: Optional[Callable[[int, int], None]] = None,
    country: Optional[str] = None,
) -> List[BeatLeaderPlayer]:
    """BeatLeader のランキングからプレイヤー一覧を取得する。

    /players エンドポイントを使用し、pp 降順でページングしながら取得する。
    min_pp を指定すると、その PP 以上のプレイヤーだけを対象にする。

    country に 2 文字の国コード ("JP" など) を指定すると、その国のランキングに
    サーバ側でもフィルタを掛ける。これにより、グローバルではなく「表示している
    国籍の BeatLeader ランキング」だけを対象にし、ページ数を減らしてタイムアウトや
    レートリミットのリスクを下げる。

    注意:
    - 以前はサーバ側の `pp_range` フィルタを使っていたが、BeatLeader API 側で
      返却件数に上限があるため (上位 500 位程度で打ち切られる)、これを廃止した。
    - 代わりに「ランキングを上から順にページングし、ページ末尾のプレイヤーの
      PP が min_pp を下回ったら打ち切る」方式に変更している。
      これにより、min_pp 以上のプレイヤーを可能な限りすべて取得できる。
    """

    if session is None:
        session = requests.Session()

    players: List[BeatLeaderPlayer] = []

    params_base: dict[str, str] = {
        "sortBy": "pp",
        "order": "desc",
        "count": str(page_size),
    }
    if country:
        # BeatLeader 側で国コードによるフィルタを掛ける
        params_base["countries"] = country.upper()

    print(f"fBeatLeader: Starting fetch with min_pp={min_pp}, page_size={page_size}, max_pages={max_pages}")
    for page in range(1, max_pages + 1):
        if progress is not None:
            try:
                progress(page, max_pages)
            except Exception:
                # 進捗コールバック側のエラーでループが止まらないようにする
                pass
        params = dict(params_base)
        params["page"] = str(page)

        # レートリミット対策: 1ページごとに少し待つ + 429 のときは Retry-After を見てリトライ
        retries = 0
        while True:
            try:
                resp = session.get(f"{BASE_URL}/players", params=params, timeout=10)
            except requests.exceptions.ReadTimeout as e:
                # タイムアウトは何度かリトライしてみる
                retries += 1
                wait_sec = 5.0
                print(
                    f"fBeatLeader: Read timeout on page {page}, retry {retries}. "
                    f"Sleeping {wait_sec} seconds... ({e})"
                )
                if retries >= 3:
                    print("fBeatLeader: Too many read timeouts, aborting this page.")
                    resp = None
                    break
                time.sleep(wait_sec)
                continue
            except Exception as e:
                # その他のネットワークレベルのエラー
                print(f"fBeatLeader: Request error on page {page}: {e}")
                resp = None

            if resp is None:
                break

            if resp.status_code == 429:
                # API calls quota exceeded (10 per 10s) 対策
                retries += 1
                retry_after_header = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
                try:
                    retry_after_sec = float(retry_after_header) if retry_after_header is not None else 10.0
                except (TypeError, ValueError):
                    retry_after_sec = 10.0
                print(
                    f"fBeatLeader: HTTP 429 on page {page}, retry {retries}. "
                    f"Sleeping {retry_after_sec} seconds..."
                )
                time.sleep(retry_after_sec)

                if retries >= 5:
                    # あまりにも連続して 429 が出る場合は諦める
                    print("fBeatLeader: Too many 429 responses, aborting.")
                    break
                # 同じページをリトライ
                continue

            if resp.status_code != 200:
                # HTTP ステータスエラー。BeatLeader 側のレートリミットや一時エラーの可能性もある
                text_snippet = resp.text[:200].replace("\n", " ") if hasattr(resp, "text") else ""
                print(
                    f"fBeatLeader: HTTP {resp.status_code} on page {page}. "
                    f"params={params} body_snippet={text_snippet}"
                )
                break

            # 正常に 200 が返ってきたのでループを抜けて処理を続行
            break

        if resp is None or resp.status_code != 200:
            # ネットワークエラー or ステータスエラーで中断
            break

        try:
            data = resp.json()
        except Exception as e:
            print(f"fBeatLeader: Failed to parse JSON response on page {page}: {e}")
            break

        # /players のレスポンスは { "metadata": ..., "data": [PlayerResponseWithStats, ...] }
        items = data.get("data") or []
        if not items:
            print("fBeatLeader: No more data")
            break

        for p in items:
            try:
                pp = float(p.get("pp", 0.0))
                print(f"fBeatLeader pp: {pp}")
                # ローカル側で min_pp フィルタを掛ける
                if min_pp > 0 and pp < min_pp:
                    print("fBeatLeader: Skipping player below min_pp")
                    continue
                players.append(
                    BeatLeaderPlayer(
                        id=str(p.get("id", "")),
                        name=str(p.get("name", "")),
                        country=(p.get("country") or None),
                        pp=pp,
                        global_rank=int(p.get("rank", 0)),
                        country_rank=int(p.get("countryRank", 0)),
                    )
                )
            except (TypeError, ValueError):
                print("fBeatLeader: Skipping invalid player data")
                continue

        # ページ末尾のプレイヤーの PP を見て、しきい値を下回ったら
        # それ以降のページもすべて min_pp 未満になるので終了する。
        try:
            last_pp = float(items[-1].get("pp", 0.0) or 0.0)
        except (TypeError, ValueError, IndexError):
            last_pp = 0.0
        print(f"fBeatLeader last_pp: {last_pp}")
        if min_pp > 0 and last_pp < min_pp:
            print("fBeatLeader: Last page reached due to min_pp threshold")
            break

        # ある程度ページを読み切っても件数が page_size 未満になった場合も
        # 最終ページとみなして終了する。
        if len(items) < page_size:
            print("fBeatLeader: Last page reached")
            break

    return players
