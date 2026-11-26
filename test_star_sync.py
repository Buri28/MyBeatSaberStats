"""リウェイトで★・pp が変わった譜面だけ自分のスコアを取り直す（sync_changed_ranked_scores）の回帰テスト。

合計 PP の変化がしきい値（0.1）より小さいリウェイトでは PP のずれの取り直し（reconcile）が動かず、
古いスコアの pp と★が古いまま残っていた。
- BL：accgraph（1 リクエスト）とキャッシュを比べ、食い違う譜面の曲だけ search=<hash> で取り直す。
  ランク譜面キャッシュの★・rating も書き換え、★のずれの印を付ける（プレイしていない譜面は次回の全件取り直し）。
  2 回目は accgraph の 1 リクエストだけ。取り直しても食い違いが残った譜面は印を付け、2 回目は取り直さない。
  曲が多いときはランク譜面のスコアの全ページ取り直し（約 30 ページ）に切り替える。
- SS：スコアキャッシュの★とランク譜面キャッシュの★（wiki のリウェイトで新しくなる）を比べ、曲名の search で取り直す。
- AccSaber：取ったばかりのスコアの complexity で全マップキャッシュを書き換える。

模擬 session で応答を作り、キャッシュは一時フォルダに向ける（実キャッシュ・実 API には触らない）。
実行: .venv\\Scripts\\python.exe -m pytest test_star_sync.py
"""
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent / "src"))
from mybeatsaberstats.collector import beatleader as bl  # noqa: E402
from mybeatsaberstats.collector import scoresaber as ss  # noqa: E402
from mybeatsaberstats import accsaber_reloaded as rl  # noqa: E402

PID = "7"


class _Resp:
    headers: dict = {}

    def __init__(self, data, status_code=200):
        self._d = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._d


# ---------------------------------------------------------------- BeatLeader

def _bl_item(lb_id, song_hash, stars, pp, status=3):
    return {"pp": pp, "accuracy": 0.95, "baseScore": 1000, "modifiers": "", "timeset": "1700000000",
            "leaderboard": {"id": lb_id, "song": {"hash": song_hash},
                            "difficulty": {"status": status, "stars": stars, "accRating": stars + 1,
                                           "passRating": stars - 1, "techRating": stars / 2}}}


def _bl_lb(lb_id, stars):
    return {"id": lb_id, "difficulty": {"status": 3, "stars": stars, "accRating": stars + 1,
                                        "passRating": stars - 1, "techRating": stars / 2, "rankedTime": 1}}


class BlApi:
    """BL の今の状態（譜面の★・自分のスコアの pp）を持つ。accgraph と search=<hash> に答える。"""

    def __init__(self, maps):
        # maps: {lb_id: (hash, stars, pp, modifiers)}
        self.maps = maps
        self.calls: list[str] = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        if url.endswith("/accgraph"):
            self.calls.append("accgraph")
            return _Resp([{"leaderboardId": k, "hash": h, "stars": s, "pp": p, "modifiers": m}
                          for k, (h, s, p, m) in self.maps.items()])
        if url.endswith(f"/player/{PID}/scores") and "search" in params:
            self.calls.append(f"search:{params['search']}")
            data = [_bl_item(k, h, s, p) for k, (h, s, p, _m) in self.maps.items() if h == params["search"]]
            return _Resp({"metadata": {"total": len(data)}, "data": data})
        if url.endswith(f"/player/{PID}/scores"):
            self.calls.append(f"ranked:{params.get('page')}")
            data = [_bl_item(k, h, s, p) for k, (h, s, p, _m) in self.maps.items()]
            page = int(params.get("page") or 1)
            return _Resp({"metadata": {"total": len(data)}, "data": data if page == 1 else []})
        raise AssertionError(f"unexpected url {url} {params}")


def _write_bl_cache(tmp, maps_cached, scores_cached):
    (tmp / "beatleader_ranked_maps.json").write_text(json.dumps({
        "fetched_at": "2026-11-20T00:00:00Z",
        "pages": [{"page": 1, "params": {"type": "Ranked"},
                   "data": {"metadata": {"total": len(maps_cached)},
                            "data": [_bl_lb(k, s) for k, s in maps_cached.items()]}}],
    }), encoding="utf-8")
    (tmp / f"beatleader_player_scores_{PID}.json").write_text(json.dumps({
        "fetched_at": "2026-11-20T00:00:00Z",
        "scores": {k: _bl_item(k, h, s, p) for k, (h, s, p) in scores_cached.items()},
    }), encoding="utf-8")


def _bl_run(tmp, api):
    with mock.patch.object(bl, "CACHE_DIR", tmp), \
            mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", tmp / "traversal.json"), \
            mock.patch.object(bl, "_STAR_SYNC_CHECKED", tmp / "star_sync_checked.json"):
        return bl.sync_changed_ranked_scores(PID, api)


def _bl_state(tmp):
    scores = json.loads((tmp / f"beatleader_player_scores_{PID}.json").read_text(encoding="utf-8"))["scores"]
    ranked = json.loads((tmp / "beatleader_ranked_maps.json").read_text(encoding="utf-8"))
    lbs = {lb["id"]: lb for lb in ranked["pages"][0]["data"]["data"]}
    return scores, lbs, ranked


def test_bl_small_reweight_refetches_only_changed_song(tmp_path):
    # 古いスコア（重み 0.965^200 程度）の譜面 A1 が 9.0 → 9.5★ にリウェイト。pp は 100 → 104（合計はほぼ動かない）
    _write_bl_cache(
        tmp_path,
        {"A1": 9.0, "B1": 8.0, "C1": 7.0},
        {"A1": ("aaa", 9.0, 100.0), "B1": ("bbb", 8.0, 300.0), "C1": ("ccc", 7.0, 200.0)},
    )
    api = BlApi({"A1": ("aaa", 9.5, 104.0, ""), "B1": ("bbb", 8.0, 300.0, ""), "C1": ("ccc", 7.0, 200.0, "")})
    summary = _bl_run(tmp_path, api)
    assert api.calls == ["accgraph", "search:aaa"]  # 全ページは読まない
    assert summary["requests"] == 2 and summary["mode"] == "per_song"
    scores, lbs, ranked = _bl_state(tmp_path)
    assert scores["A1"]["pp"] == 104.0
    assert scores["A1"]["leaderboard"]["difficulty"]["stars"] == 9.5
    assert lbs["A1"]["difficulty"]["stars"] == 9.5 and lbs["A1"]["difficulty"]["accRating"] == 10.5
    assert lbs["B1"]["difficulty"]["stars"] == 8.0
    assert ranked["fetched_at"] == "2026-11-20T00:00:00Z"  # 差分取得の窓は進めない
    # プレイしていない譜面の★は次回の全件取り直しで直す
    assert json.loads((tmp_path / "traversal.json").read_text(encoding="utf-8"))["beatleader_stars_drift"] is True

    # 2 回目：accgraph の 1 リクエストだけ
    api.calls.clear()
    summary2 = _bl_run(tmp_path, api)
    assert api.calls == ["accgraph"] and summary2["targets"] == 0


def test_bl_pp_only_change_is_refetched_without_star_flag(tmp_path):
    # 修飾子付き（SF）のスコア：accgraph の★は修飾子込みなので★では比べない。pp の変化で拾う
    _write_bl_cache(tmp_path, {"A1": 9.0}, {"A1": ("aaa", 9.0, 100.0)})
    api = BlApi({"A1": ("aaa", 9.0, 98.0, "SF")})
    api_sf_stars = BlApi({"A1": ("aaa", 11.0, 100.0, "SF")})  # 修飾子込みの★だけ違う→取り直さない
    _bl_run(tmp_path, api_sf_stars)
    assert api_sf_stars.calls == ["accgraph"]
    _bl_run(tmp_path, api)
    assert api.calls == ["accgraph", "search:aaa"]
    scores, lbs, _ = _bl_state(tmp_path)
    assert scores["A1"]["pp"] == 98.0
    assert not (tmp_path / "traversal.json").exists()  # ★は変わっていないので印は付けない


def test_bl_unresolved_mismatch_is_not_refetched_again(tmp_path):
    # search で返らない（API の都合）→ 印を残し、同じ食い違いでは 2 回目に取り直さない
    _write_bl_cache(tmp_path, {"A1": 9.0}, {"A1": ("aaa", 9.0, 100.0)})
    api = BlApi({"A1": ("aaa", 9.0, 104.0, "")})
    api.get_orig = api.get

    def _get(url, params=None, timeout=None):
        if params and "search" in params:
            api.calls.append(f"search:{params['search']}")
            return _Resp({"metadata": {"total": 0}, "data": []})
        return api.get_orig(url, params=params, timeout=timeout)

    api.get = _get
    _bl_run(tmp_path, api)
    assert api.calls == ["accgraph", "search:aaa"]
    api.calls.clear()
    _bl_run(tmp_path, api)
    assert api.calls == ["accgraph"]
    # pp がさらに変わったら（印と違う食い違い）もう一度取り直す
    api.maps["A1"] = ("aaa", 9.0, 106.0, "")
    api.calls.clear()
    _bl_run(tmp_path, api)
    assert api.calls == ["accgraph", "search:aaa"]


def test_bl_failed_search_is_retried_next_time(tmp_path):
    _write_bl_cache(tmp_path, {"A1": 9.0}, {"A1": ("aaa", 9.0, 100.0)})
    api = BlApi({"A1": ("aaa", 9.5, 104.0, "")})
    real_get = api.get

    def _get(url, params=None, timeout=None):
        if params and "search" in params:
            api.calls.append("search-failed")
            return _Resp({}, status_code=500)
        return real_get(url, params=params, timeout=timeout)

    api.get = _get
    _bl_run(tmp_path, api)
    api.get = real_get
    api.calls.clear()
    _bl_run(tmp_path, api)
    assert api.calls == ["accgraph", "search:aaa"]
    scores, _, _ = _bl_state(tmp_path)
    assert scores["A1"]["pp"] == 104.0


def test_bl_many_songs_switch_to_ranked_pages(tmp_path):
    # 食い違いの曲がランクのスコアのページ数（ここでは 1）より多い→全ページの取り直し（曲ごとより少ない）
    _write_bl_cache(tmp_path, {"A1": 9.0, "B1": 8.0}, {"A1": ("aaa", 9.0, 100.0), "B1": ("bbb", 8.0, 90.0)})
    api = BlApi({"A1": ("aaa", 9.5, 104.0, ""), "B1": ("bbb", 8.5, 95.0, "")})
    summary = _bl_run(tmp_path, api)
    assert summary["mode"] == "bulk"
    assert [c for c in api.calls if c.startswith("search")] == []
    scores, lbs, _ = _bl_state(tmp_path)
    assert scores["A1"]["pp"] == 104.0 and scores["B1"]["pp"] == 95.0
    assert lbs["B1"]["difficulty"]["stars"] == 8.5


def test_bl_accgraph_failure_changes_nothing(tmp_path):
    _write_bl_cache(tmp_path, {"A1": 9.0}, {"A1": ("aaa", 9.0, 100.0)})

    class Down:
        def get(self, url, params=None, timeout=None):
            return _Resp({}, status_code=503)

    status: dict = {}
    with mock.patch.object(bl, "CACHE_DIR", tmp_path), \
            mock.patch.object(bl, "_STAR_SYNC_CHECKED", tmp_path / "star_sync_checked.json"):
        summary = bl.sync_changed_ranked_scores(PID, Down(), status=status)
    assert summary["mode"] == "accgraph_failed" and status["complete"] is False
    scores, _, _ = _bl_state(tmp_path)
    assert scores["A1"]["pp"] == 100.0


# ---------------------------------------------------------------- ScoreSaber

def _ss_item(lb_id, name, stars, pp, max_pp=500.0):
    return {"score": {"pp": pp, "timeSet": "2025-01-01T00:00:00.000Z", "baseScore": 900},
            "leaderboard": {"id": lb_id, "songName": name, "ranked": True, "stars": stars, "maxPP": max_pp}}


class SsApi:
    def __init__(self, maps):
        # maps: {lb_id: (name, stars, pp)}
        self.maps = maps
        self.calls: list[str] = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        assert url.endswith(f"/player/{PID}/scores")
        if "search" in params:
            assert len(params["search"]) <= 32
            self.calls.append(f"search:{params['search']}")
            data = [_ss_item(int(k), n, s, p, 600.0) for k, (n, s, p) in self.maps.items()
                    if n.startswith(params["search"])]
            return _Resp({"playerScores": data, "metadata": {"total": len(data)}})
        self.calls.append(f"top:{params.get('page')}")
        data = [_ss_item(int(k), n, s, p, 600.0) for k, (n, s, p) in self.maps.items()]
        return _Resp({"playerScores": data if params.get("page") == "1" else []})


def _write_ss_cache(tmp, ranked_stars, scores_cached):
    (tmp / "scoresaber_ranked_maps.json").write_text(json.dumps({
        "fetched_at": "2026-11-20T00:00:00Z",
        "leaderboards": {k: {"id": int(k), "ranked": True, "stars": s, "maxPP": 500.0} for k, s in ranked_stars.items()},
        "max_pages": 1, "total_maps": len(ranked_stars),
    }), encoding="utf-8")
    (tmp / f"scoresaber_player_scores_{PID}.json").write_text(json.dumps({
        "fetched_at": "2026-11-20T00:00:00Z",
        "scores": {k: _ss_item(int(k), n, s, p) for k, (n, s, p) in scores_cached.items()},
    }), encoding="utf-8")


def _ss_run(tmp, api):
    with mock.patch.object(ss, "CACHE_DIR", tmp), \
            mock.patch.object(ss, "_STAR_SYNC_CHECKED", tmp / "star_sync_checked.json"):
        return ss.sync_changed_ranked_scores(PID, api)


def _ss_state(tmp):
    scores = json.loads((tmp / f"scoresaber_player_scores_{PID}.json").read_text(encoding="utf-8"))["scores"]
    ranked = json.loads((tmp / "scoresaber_ranked_maps.json").read_text(encoding="utf-8"))
    return scores, ranked


def test_ss_reweighted_map_refetches_only_that_song(tmp_path):
    long_name = "A Very Long Song Name That Exceeds Thirty Two Chars"
    # wiki のリウェイトでランク譜面キャッシュの★は 9.5 になった。スコアキャッシュは 9.0・pp 100 のまま
    _write_ss_cache(tmp_path, {"1": 9.5, "2": 8.0}, {"1": (long_name, 9.0, 100.0), "2": ("Other", 8.0, 300.0)})
    api = SsApi({"1": (long_name, 9.5, 104.0), "2": ("Other", 8.0, 300.0)})
    summary = _ss_run(tmp_path, api)
    assert api.calls == [f"search:{long_name[:32].strip()}"]
    assert summary["requests"] == 1
    scores, ranked = _ss_state(tmp_path)
    assert scores["1"]["score"]["pp"] == 104.0 and scores["1"]["leaderboard"]["stars"] == 9.5
    assert ranked["fetched_at"] == "2026-11-20T00:00:00Z"
    api.calls.clear()
    assert _ss_run(tmp_path, api)["targets"] == 0
    assert api.calls == []  # 2 回目は API を使わない


def test_ss_stale_ranked_cache_is_updated_from_refetched_score(tmp_path):
    # ランク譜面キャッシュが 9.0、スコアキャッシュは 9.2（どちらかが古い）。取り直すと 9.5 → ランク譜面キャッシュを直す
    _write_ss_cache(tmp_path, {"1": 9.0}, {"1": ("Song", 9.2, 100.0)})
    api = SsApi({"1": ("Song", 9.5, 104.0)})
    summary = _ss_run(tmp_path, api)
    assert summary["maps_updated"] == 1
    scores, ranked = _ss_state(tmp_path)
    assert ranked["leaderboards"]["1"]["stars"] == 9.5 and ranked["leaderboards"]["1"]["maxPP"] == 600.0
    assert scores["1"]["score"]["pp"] == 104.0


def test_ss_unresolved_is_marked(tmp_path):
    _write_ss_cache(tmp_path, {"1": 9.5}, {"1": ("Song", 9.0, 100.0)})
    api = SsApi({})  # 検索で返らない
    _ss_run(tmp_path, api)
    assert api.calls == ["search:Song"]
    api.calls.clear()
    _ss_run(tmp_path, api)
    assert api.calls == []


def test_ss_rate_limit_stops_and_retries_next_time(tmp_path):
    _write_ss_cache(tmp_path, {"1": 9.5, "2": 8.5}, {"1": ("Song", 9.0, 100.0), "2": ("Zed", 8.0, 50.0)})
    # bulk に切り替わらないよう、ランクのスコアを増やしておく（ページ数 > 曲の数）
    scores = json.loads((tmp_path / f"scoresaber_player_scores_{PID}.json").read_text(encoding="utf-8"))
    for i in range(100, 400):
        scores["scores"][str(i)] = _ss_item(i, f"N{i}", 5.0, 10.0)
    (tmp_path / f"scoresaber_player_scores_{PID}.json").write_text(json.dumps(scores), encoding="utf-8")

    class Limited:
        calls = 0

        def get(self, url, params=None, timeout=None):
            Limited.calls += 1
            return _Resp({}, status_code=429)

    status: dict = {}
    with mock.patch.object(ss, "CACHE_DIR", tmp_path), \
            mock.patch.object(ss, "_STAR_SYNC_CHECKED", tmp_path / "star_sync_checked.json"):
        ss.sync_changed_ranked_scores(PID, Limited(), status=status)
    assert Limited.calls == 1 and status["complete"] is False
    api = SsApi({"1": ("Song", 9.5, 104.0), "2": ("Zed", 8.5, 55.0)})
    _ss_run(tmp_path, api)
    assert sorted(api.calls) == ["search:Song", "search:Zed"]


# ---------------------------------------------------------------- AccSaber

def test_accsaber_complexity_follows_fresh_scores(tmp_path):
    maps_path = tmp_path / "accsaber_reloaded_maps.json"
    maps_path.write_text(json.dumps({
        "fetched_at": "2026-11-24T00:00:00Z",
        "maps": [{"songName": "S", "difficulties": [{"id": "d1", "difficulty": "EXPERT", "complexity": 8.4},
                                                    {"id": "d2", "difficulty": "HARD", "complexity": 5.0}]}],
    }), encoding="utf-8")
    with mock.patch.object(rl, "_ALL_MAPS_CACHE_FILE", maps_path):
        assert rl.sync_map_complexity_from_scores([
            {"mapDifficultyId": "d1", "complexity": 9.1}, {"mapDifficultyId": "d2", "complexity": 5.0},
            {"mapDifficultyId": "zz", "complexity": 3.0},
        ]) == 1
        data = json.loads(maps_path.read_text(encoding="utf-8"))
        assert data["fetched_at"] == "2026-11-24T00:00:00Z"
        assert [d["complexity"] for d in data["maps"][0]["difficulties"]] == [9.1, 5.0]
        assert rl.sync_map_complexity_from_scores([{"mapDifficultyId": "d1", "complexity": 9.1}]) == 0


# ---------------------------------------------------------------- Playlist ウィンドウ（保存した行の復元）

def test_restored_playlist_rows_take_current_stars_and_pp():
    from dataclasses import replace
    from mybeatsaberstats.playlist import playlist_snapshot as ps
    from mybeatsaberstats.playlist_view import MapEntry

    def _entry(stars, pp, source="beatleader"):
        return MapEntry(song_name="Memecore", song_author="a", mapper="m", song_hash="ABC", difficulty="Normal",
                        mode="Standard", stars=stars, max_pp=0.0, player_pp=pp, cleared=True, nf_clear=False,
                        player_acc=95.0, player_rank=1, leaderboard_id="e72b31", source=source)

    saved = _entry(5.04, 365.7)  # リウェイト前に保存した行
    saved = replace(saved, bl_stars=5.04, bl_player_pp=365.7)
    current_map = _entry(5.34, 0.0)  # ランク譜面キャッシュ（今の★）
    bl_scores = {"e72b31": {"pp": 370.7, "accuracy": 0.95, "baseScore": 1000, "modifiers": "", "timeset": "1700000000",
                            "leaderboard": {"song": {"hash": "abc"},
                                            "difficulty": {"difficultyName": "Normal", "modeName": "Standard"}}}}
    with mock.patch.object(ps, "_load_snapshot_service_entries_from_cache",
                           return_value={"scoresaber": [], "beatleader": [current_map], "accsaber": [], "accsaber_reloaded": []}), \
            mock.patch.object(ps, "_load_cached_player_score_dicts", return_value=({}, bl_scores)):
        ps._refresh_snapshot_entries_service_columns([saved], PID)
    # 列（BL ★・BL PP）も、行そのものの★・pp（★の範囲フィルタ・★別の書き出しに使う）も今の値
    assert saved.bl_stars == 5.34 and saved.bl_player_pp == 370.7
    assert saved.stars == 5.34 and saved.player_pp == 370.7
