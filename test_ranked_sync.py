"""ランク譜面の取りこぼしケースを擬似 API で再現する回帰テスト（ScoreSaber・BeatLeader）。

どのテストも state・キャッシュを一時フォルダに向ける（実キャッシュには触らない）。
実行: .venv\\Scripts\\python.exe test_ranked_sync.py
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent / "src"))
from mybeatsaberstats.collector import beatleader as bl  # noqa: E402
from mybeatsaberstats.collector import scoresaber as ss  # noqa: E402
import test_ss_ranked_cases as T  # noqa: E402
from test_ss_ranked_cases import FakeApi, base_maps, iso, lb, mark_full, run, write_cache  # noqa: E402


def saved_stars(saved, i):
    return saved["leaderboards"][str(i)]["stars"]


def state(tmp):
    return json.loads((tmp / "traversal.json").read_text(encoding="utf-8"))


# ---------------- ScoreSaber ----------------

def test_ss_old_map_stars_change_fixed_by_drift(tmp_path):
    """窓の外の譜面の★だけが変わる。★のずれの印による全件取り直しで直る。"""
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    api_maps = [dict(m, stars=7.5) if m["id"] == 110 else m for m in maps]
    mark_full(tmp_path, days_ago=20, drift=True)
    _, saved = run(tmp_path, FakeApi(api_maps), 1)
    assert saved_stars(saved, 110) == 7.5, "窓外の★変更を取りこぼした"
    assert "scoresaber_complete" in state(tmp_path)
    assert "scoresaber_stars_drift" not in state(tmp_path)


def test_ss_old_map_stars_change_without_drift_is_limit(tmp_path):
    """★のずれの印が無ければ、窓の外の★は更新されない（ずれを検知するまでの遅れ＝仕様の上限）。"""
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    api_maps = [dict(m, stars=7.5) if m["id"] == 110 else m for m in maps]
    _, saved = run(tmp_path, FakeApi(api_maps), 1)  # 1 日前に全件取得済み
    assert saved_stars(saved, 110) == 5.0


def test_ss_swap_outside_window_same_total(tmp_path):
    """窓の外で 1 件外れ 1 件入れ替わり（総数は同じ）。総数では見えず、全件取り直しの印が無いうちは残る（仕様の上限）。
    ★のずれ等で全件取り直せば直る。"""
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    api_maps = [m for m in maps if m["id"] != 110] + [lb(400, 250)]
    mark_full(tmp_path, days_ago=20)
    ids, _ = run(tmp_path, FakeApi(api_maps), 1)
    assert ids == {str(m["id"]) for m in maps}
    mark_full(tmp_path, days_ago=20, drift=True)
    ids, _ = run(tmp_path, FakeApi(api_maps), 1)
    assert ids == {str(m["id"]) for m in api_maps}


def test_ss_swap_inside_window_same_total(tmp_path):
    """窓の中で入れ替わり（総数は同じ）。全件取り直しなしで、窓の中の突き合わせで直る。"""
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    api_maps = [m for m in maps if m["id"] != 205] + [lb(300, 1)]
    api = FakeApi(api_maps)
    ids, _ = run(tmp_path, api, 1)
    assert ids == {str(m["id"]) for m in api_maps}, "窓内の入れ替わりを見逃した"
    assert api.calls <= 3, f"全件取り直しになった calls={api.calls}"


def test_ss_long_gap_with_old_full_record(tmp_path):
    """取得を 90 日・400 日空けた後（全件取得の記録も古い）でも、全件が一致する。"""
    for gap in (90, 400):
        d = tmp_path / str(gap)
        d.mkdir()
        maps = base_maps()
        write_cache(d, maps, fetched_days_ago=gap)
        api_maps = [m for m in maps if m["id"] != 120] + [lb(300 + i, gap - 10 - i * 3) for i in range(10)]
        api_maps = [dict(m, stars=9.0) if m["id"] == 101 else m for m in api_maps]
        mark_full(d, days_ago=gap)
        ids, saved = run(d, FakeApi(api_maps), gap)
        assert ids == {str(m["id"]) for m in api_maps}, gap
        assert saved_stars(saved, 101) == 9.0, gap


def test_ss_full_traversal_failure_keeps_cache_and_retries(tmp_path):
    """全件取得が途中で失敗：キャッシュは壊れず（古い譜面を消さない）、完了の記録も進まず、1 時間後に取り直す。"""
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    before = json.loads((tmp_path / "scoresaber_ranked_maps.json").read_text(encoding="utf-8"))
    api_maps = [m for m in maps if m["id"] != 110]
    mark_full(tmp_path, days_ago=20, drift=True)
    complete_before = state(tmp_path)["scoresaber_complete"]
    ids, saved = run(tmp_path, FakeApi(api_maps, fail_page=3), 1)
    assert ids >= {str(m["id"]) for m in maps}, "失敗したのにキャッシュから譜面が消えた"
    assert saved["fetched_at"] == before["fetched_at"], "失敗したのに取得日時が進んだ"
    st = state(tmp_path)
    assert st["scoresaber_complete"] == complete_before, "失敗したのに完了の記録が進んだ"
    # 直後（1 時間以内）は連打しない
    api2 = FakeApi(api_maps)
    run(tmp_path, api2, 1)
    assert api2.calls <= 3, f"失敗直後にまた全件取得した calls={api2.calls}"
    # 1 時間以上経ったら取り直して直る
    st["scoresaber"] = (datetime.utcnow() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp_path / "traversal.json").write_text(json.dumps(st), encoding="utf-8")
    ids3, _ = run(tmp_path, FakeApi(api_maps), 1)
    assert ids3 == {str(m["id"]) for m in api_maps}


class DupApi(FakeApi):
    """ページ境界で、前ページの最後の 1 件がもう一度現れる（新ランクで順序がずれた状態）。"""

    def get(self, url, params=None, timeout=None):
        r = super().get(url, params, timeout)
        page = int(params["page"])
        if page > 1:
            prev = super().get(url, dict(params, page=str(page - 1)), timeout)
            r._d["leaderboards"] = prev._d["leaderboards"][-1:] + r._d["leaderboards"]
        return r


def test_ss_page_boundary_duplicates(tmp_path):
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    api_maps = [m for m in maps if m["id"] != 110] + [lb(300, 0.5)]
    mark_full(tmp_path, days_ago=20, drift=True)
    ids, _ = run(tmp_path, DupApi(api_maps), 1)
    assert ids == {str(m["id"]) for m in api_maps}


class SkipOnceApi(FakeApi):
    """最初の 1 回だけ、1 件が抜ける（巡回中にランクが外れて順序がずれた状態）。"""

    def __init__(self, maps, skip_id, **kw):
        super().__init__(maps, **kw)
        self.skip_id = skip_id
        self.skipped = False

    def get(self, url, params=None, timeout=None):
        r = super().get(url, params, timeout)
        if not self.skipped:
            lbs = r._d["leaderboards"]
            if any(str(m["id"]) == str(self.skip_id) for m in lbs):
                r._d["leaderboards"] = [m for m in lbs if str(m["id"]) != str(self.skip_id)]
                self.skipped = True
        return r


def test_ss_page_boundary_miss_self_heals(tmp_path):
    """窓の中の譜面が 1 件だけ読み落とされても、総数の食い違いで全件取り直して戻る（消えたままにならない）。"""
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    ids, _ = run(tmp_path, SkipOnceApi(maps, skip_id=203), 1)
    assert ids == {str(m["id"]) for m in maps}, "読み落としで譜面が消えたまま"


def test_ss_persistent_mismatch_not_looping(tmp_path):
    """総数が実際の列挙数と合わない API でも、全件取り直しは繰り返さない（24 時間）。総数が変われば直ちに取り直す。"""
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)

    class Quirk(FakeApi):
        extra = 2

        def get(self, url, params=None, timeout=None):
            r = super().get(url, params, timeout)
            r._d["metadata"]["total"] = len(self.maps) + self.extra
            return r
    api = Quirk(maps)
    run(tmp_path, api, 1)
    first = api.calls
    run(tmp_path, api, 1)
    assert api.calls - first < first
    api.extra = 3  # 総数が変わった（新しい食い違い）→ 取り直す
    before = api.calls
    run(tmp_path, api, 1)
    assert api.calls - before >= 4


class Api404(FakeApi):
    """3 ページ目以降が 404（一時的に一覧が途切れた）。"""

    def get(self, url, params=None, timeout=None):
        r = super().get(url, params, timeout)
        if int(params["page"]) >= 3:
            r.status_code = 404
        return r


def test_ss_midway_404_is_not_complete(tmp_path):
    """途中で 404 になった全件取得を「最後まで取れた」と記録しない（★のずれの印も残り、取り直しが止まらない）。"""
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    mark_full(tmp_path, days_ago=20, drift=True)
    before = state(tmp_path)["scoresaber_complete"]
    ids, _ = run(tmp_path, Api404(maps + [lb(400, 250)]), 1)  # 404 より後ろに未取得の譜面がある
    assert ids >= {str(m["id"]) for m in maps}
    assert state(tmp_path)["scoresaber_complete"] == before
    assert state(tmp_path).get("scoresaber_stars_drift") is True


def test_ss_persistent_mismatch_waits_14_days_after_second(tmp_path):
    """同じ総数の食い違いが 2 回続いたら、24 時間後でも全件取り直さない（14 日待つ）。"""
    from mybeatsaberstats import ranked_sync as rs
    p = tmp_path / "t.json"
    rs.record_full_traversal(p, "x", False, 50)
    rs.record_full_traversal(p, "x", False, 50)
    d = json.loads(p.read_text(encoding="utf-8"))
    d["x"] = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p.write_text(json.dumps(d), encoding="utf-8")
    assert not rs.mismatch_traversal_allowed(p, "x", 50)
    assert not rs.full_traversal_due(p, "x")
    assert rs.mismatch_traversal_allowed(p, "x", 51)  # 総数が変われば直ちに


def test_ss_ranked_out_map_not_rewritten_as_ranked(tmp_path):
    """ランク譜面キャッシュ側でもランクでない（掃除が遅れた）譜面は、スコア側を「ランク」に書き換えない。"""
    api = _ss_setup(tmp_path, pp_in_api=0)
    p = tmp_path / "scoresaber_ranked_maps.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["leaderboards"] = {"300": dict(d["leaderboards"]["300"], ranked=False)}
    p.write_text(json.dumps(d), encoding="utf-8")
    with mock.patch.object(ss, "CACHE_DIR", tmp_path):
        assert ss.refresh_scores_of_newly_ranked_maps("1", api) == 0
    assert api.calls == 0


class UnstableTailApi(FakeApi):
    """全件巡回で最後のページに前のページの譜面が重複して返り、その分の譜面が列挙されない
    （rankedDate が null の譜面の並びが不安定で、実 API は 6243 件中 6161 件しか列挙しない）。
    一覧そのものは最後のページ（件数が 1 ページ分未満）まで読める。"""

    def get(self, url, params=None, timeout=None):
        r = super().get(url, params, timeout)
        if int(params["page"]) >= 4:
            first = super().get(url, dict(params, page="1"), timeout)._d["leaderboards"]
            r._d["leaderboards"] = first[:len(r._d["leaderboards"])]
        return r


class EmptyMidPageApi(FakeApi):
    """全件巡回の途中のページ（3 ページ目以降）が空で返る＝一覧の途中で止まった。"""

    def get(self, url, params=None, timeout=None):
        r = super().get(url, params, timeout)
        if int(params["page"]) >= 3:
            r._d["leaderboards"] = []
        return r


def test_ss_unstable_listing_with_consistent_cache_is_complete(tmp_path):
    """列挙数が総数に届かなくても、キャッシュの件数が総数と合っていれば完了とし、何も消さず、繰り返し全件取得もしない。"""
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    mark_full(tmp_path, days_ago=20, drift=True)
    ids, _ = run(tmp_path, UnstableTailApi(maps), 1)
    assert ids == {str(m["id"]) for m in maps}, "列挙が欠けただけで譜面が消えた"
    st = state(tmp_path)
    assert st["scoresaber_complete"] > (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert st["scoresaber_unresolved_total"] is None


def test_ss_full_traversal_stopped_midway_is_not_complete(tmp_path):
    """指摘 1：全件取得が途中の空ページで止まったら、キャッシュの件数が総数と合っていても完了にしない。
    ★のずれの印も消さず、status にも失敗を入れる（直す前は len(キャッシュ) == 総数 で完了扱いだった）。"""
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    mark_full(tmp_path, days_ago=20)
    st0 = state(tmp_path)
    st0["scoresaber_stars_drift"] = True
    (tmp_path / "traversal.json").write_text(json.dumps(st0), encoding="utf-8")
    status = {"complete": True}
    with mock.patch.object(ss, "CACHE_DIR", tmp_path),             mock.patch.object(ss, "_RANKED_FULL_TRAVERSAL_STATE", tmp_path / "traversal.json"),             mock.patch.object(ss, "_is_rate_limited", return_value=False):
        res = ss._get_scoresaber_leaderboards_ranked(EmptyMidPageApi(maps), fetch_until=datetime(2000, 1, 1), status=status)
    assert {str(k) for k in res} == {str(m["id"]) for m in maps}, "途中で止まったのに譜面を消した"
    st = state(tmp_path)
    assert st["scoresaber_complete"] == st0["scoresaber_complete"], "途中で止まったのに完了の記録が進んだ"
    assert st["scoresaber_last_ok"] is False and st["scoresaber_unresolved_total"] is None
    assert st.get("scoresaber_stars_drift") is True, "途中で止まったのに★のずれの印が消えた"
    assert status["complete"] is False and not status.get("ss_ranked_listing_short")


# ---------------- 新ランク譜面の自分の古いプレイ ----------------

def _ss_score(lid, ranked, pp):
    return {"score": {"id": lid, "pp": pp, "timeSet": "2024-01-01T00:00:00.000Z", "baseScore": 1},
            "leaderboard": {"id": lid, "ranked": ranked, "stars": 6.0 if ranked else 0}}


class ScoreApi:
    def __init__(self, items):
        self.items = items
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return T._Resp({"playerScores": self.items if params.get("page") == "1" else [],
                        "metadata": {"total": len(self.items)}})


def _ss_setup(tmp, pp_in_api):
    ranked = {"300": dict(lb(300, 3), ranked=True, stars=6.0), "301": dict(lb(301, 3), ranked=True, stars=7.0)}
    (tmp / "scoresaber_ranked_maps.json").write_text(
        json.dumps({"fetched_at": iso(T.NOW), "leaderboards": ranked}), encoding="utf-8")
    scores = {"300": _ss_score(300, False, 0), "301": _ss_score(301, False, 0), "999": _ss_score(999, False, 0)}
    (tmp / "scoresaber_player_scores_1.json").write_text(
        json.dumps({"fetched_at": iso(T.NOW), "total_play_count": 3, "scores": scores}), encoding="utf-8")
    items = [_ss_score(300, True, pp_in_api)] if pp_in_api else []
    return ScoreApi(items)


def test_ss_new_ranked_map_with_old_play_gets_pp(tmp_path):
    """新ランク譜面に自分の古いプレイがある。pp の差（0.1 pp 未満でも）や reconcile に関係なく、その譜面の pp を取り直す。"""
    api = _ss_setup(tmp_path, pp_in_api=0.04)
    with mock.patch.object(ss, "CACHE_DIR", tmp_path), mock.patch.object(ss, "_is_rate_limited", return_value=False):
        n = ss.refresh_scores_of_newly_ranked_maps("1", api)
        saved = json.loads((tmp_path / "scoresaber_player_scores_1.json").read_text(encoding="utf-8"))["scores"]
        assert n == 2
        assert saved["300"]["score"]["pp"] == 0.04 and saved["300"]["leaderboard"]["ranked"] is True
        # 指摘 4：pp が付かなかった 301 の譜面情報は書き換えない（ranked=True にしない）。検知済みの印だけ残し、再検知しない
        assert saved["301"]["leaderboard"]["ranked"] is False, "ランクのスコアにならなかったのに譜面情報を書き換えた"
        assert saved["999"]["leaderboard"]["ranked"] is False
        marks = json.loads((tmp_path / "newly_ranked_checked.json").read_text(encoding="utf-8"))
        assert set(marks["scoresaber"]["1"]) == {"301"}
        calls = api.calls
        assert ss.refresh_scores_of_newly_ranked_maps("1", api) == 0
        assert api.calls == calls, "検知済みなのに API を呼んだ"
        # プレイし直して、スコアの譜面情報が（ランク外のまま）新しくなっても、取り直さない
        p = tmp_path / "scoresaber_player_scores_1.json"
        raw = json.loads(p.read_text(encoding="utf-8"))
        raw["scores"]["301"] = _ss_score(301, False, 0)
        raw["scores"]["301"]["score"]["timeSet"] = "2026-11-19T00:00:00.000Z"
        p.write_text(json.dumps(raw), encoding="utf-8")
        assert ss.refresh_scores_of_newly_ranked_maps("1", api) == 0
        assert api.calls == calls, "プレイし直すたびに取り直している"


def test_ss_no_new_ranked_no_api_calls(tmp_path):
    api = _ss_setup(tmp_path, pp_in_api=0)
    (tmp_path / "scoresaber_ranked_maps.json").write_text(
        json.dumps({"fetched_at": iso(T.NOW), "leaderboards": {}}), encoding="utf-8")  # ランク譜面なし
    with mock.patch.object(ss, "CACHE_DIR", tmp_path):
        assert ss.refresh_scores_of_newly_ranked_maps("1", api) == 0
    assert api.calls == 0


def test_ss_refresh_failure_keeps_detection(tmp_path):
    api = _ss_setup(tmp_path, pp_in_api=3.0)

    def boom(url, params=None, timeout=None):
        raise ConnectionError("x")
    api.get = boom
    st = {}
    with mock.patch.object(ss, "CACHE_DIR", tmp_path), mock.patch.object(ss, "_is_rate_limited", return_value=False):
        ss.refresh_scores_of_newly_ranked_maps("1", api, status=st)
        assert st.get("complete") is False
        saved = json.loads((tmp_path / "scoresaber_player_scores_1.json").read_text(encoding="utf-8"))["scores"]
        assert saved["300"]["leaderboard"]["ranked"] is False, "失敗したのに再検知できなくなった"


# ---------------- BeatLeader ----------------

BL_NOW = datetime(2026, 11, 18)


def bl_lb(i, days_ago, stars=5.0):
    rt = 0 if days_ago is None else int((BL_NOW - timedelta(days=days_ago)).timestamp())
    return {"id": f"L{i}", "difficulty": {"rankedTime": rt, "status": 3, "stars": stars}}


class BlFake:
    def __init__(self, maps, fail_page=None):
        self.maps = maps
        self.calls = 0
        self.fail_page = fail_page

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        page, n = int(params["page"]), int(params["count"])
        if self.fail_page and page >= self.fail_page:
            raise ConnectionError("boom")
        ordered = sorted(self.maps, key=lambda m: m["difficulty"]["rankedTime"], reverse=True)
        return T._Resp({"metadata": {"total": len(self.maps)}, "data": ordered[(page - 1) * n: page * n]})


def bl_base():
    m = [bl_lb(i, 150 + i * 5) for i in range(330)]  # 古い（窓の外）330 件
    m += [bl_lb(1000 + i, 5 + i) for i in range(10)]  # 最近 10 件
    m += [bl_lb(2000 + i, None) for i in range(5)]     # rankedTime 0
    return m


def bl_write(tmp, maps, fetched_days_ago):
    payload = {"fetched_at": iso(BL_NOW - timedelta(days=fetched_days_ago)), "pages": [
        {"page": 1, "params": {"type": "Ranked"}, "data": {"metadata": {"total": len(maps)}, "data": maps}}]}
    (tmp / "beatleader_ranked_maps.json").write_text(json.dumps(payload), encoding="utf-8")


def bl_mark(tmp, days_ago, drift=False):
    """BL の全件取得が最後まで取れた記録。drift=True なら★のずれの印も付け、次の取得を全件取り直しにする。"""
    stamp = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    st = {"beatleader": stamp, "beatleader_complete": stamp}
    if drift:
        st["beatleader_stars_drift"] = True
    (tmp / "traversal.json").write_text(json.dumps(st), encoding="utf-8")


def bl_run(tmp, api, fetched_days_ago=1):
    until = BL_NOW - timedelta(days=fetched_days_ago + 60)
    with mock.patch.object(bl, "CACHE_DIR", tmp), \
            mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", tmp / "traversal.json"):
        res = bl._get_beatleader_leaderboards_ranked(api, fetch_until=until)
    return {x["id"]: x for x in res}, json.loads((tmp / "beatleader_ranked_maps.json").read_text(encoding="utf-8"))


def test_bl_old_map_stars_change_fixed_by_drift(tmp_path):
    maps = bl_base()
    bl_write(tmp_path, maps, 1)
    api_maps = [json.loads(json.dumps(m)) for m in maps]
    api_maps[10]["difficulty"]["stars"] = 8.0
    bl_mark(tmp_path, 20, drift=True)
    got, _ = bl_run(tmp_path, BlFake(api_maps))
    assert got[api_maps[10]["id"]]["difficulty"]["stars"] == 8.0


def test_bl_swap_outside_and_inside_window(tmp_path):
    maps = bl_base()
    bl_write(tmp_path, maps, 1)
    # 窓の中の入れ替わり（全件取り直しなしで直る）
    api_maps = [m for m in maps if m["id"] != "L1005"] + [bl_lb(3900, 1)]
    bl_mark(tmp_path, 1)
    api = BlFake(api_maps)
    got, _ = bl_run(tmp_path, api)
    assert set(got) == {m["id"] for m in api_maps}
    assert api.calls <= 2, api.calls
    # 窓の外の入れ替わり（★のずれ等による全件取り直しで直る）
    d = tmp_path / "b"
    d.mkdir()
    bl_write(d, maps, 1)
    api_maps2 = [m for m in maps if m["id"] != "L20"] + [bl_lb(3901, 300)]
    bl_mark(d, 20, drift=True)
    got2, _ = bl_run(d, BlFake(api_maps2))
    assert set(got2) == {m["id"] for m in api_maps2}


def test_bl_full_traversal_failure_keeps_cache(tmp_path):
    maps = bl_base()
    bl_write(tmp_path, maps, 1)
    api_maps = [m for m in maps if m["id"] != "L20"]
    bl_mark(tmp_path, 20, drift=True)
    got, _ = bl_run(tmp_path, BlFake(api_maps, fail_page=2))
    assert set(got) >= {m["id"] for m in maps}, "失敗したのに譜面が消えた"
    st = state(tmp_path)
    assert st["beatleader_complete"] < st["beatleader"], "失敗したのに完了の記録が進んだ"
    api2 = BlFake(api_maps)
    bl_run(tmp_path, api2)
    assert api2.calls <= 3, "失敗直後にまた全件取得した"


class BlStatusFake(BlFake):
    """指定ページから 404 または空ページを返す。"""

    def __init__(self, maps, stop_page, mode):
        super().__init__(maps)
        self.stop_page, self.mode = stop_page, mode

    def get(self, url, params=None, timeout=None):
        page = int(params["page"])
        if page >= self.stop_page:
            self.calls += 1
            if self.mode == 404:
                r = T._Resp({})
                r.status_code = 404
                return r
            return T._Resp({"metadata": {"total": len(self.maps)}, "data": []})
        return super().get(url, params, timeout)


def _bl_full(tmp, api, status):
    with mock.patch.object(bl, "CACHE_DIR", tmp),             mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", tmp / "traversal.json"),             mock.patch.object(bl, "_log_api_failure"):
        return bl._get_beatleader_leaderboards_ranked(api, fetch_until=datetime(2000, 1, 1), status=status)


def test_bl_full_traversal_stopped_midway_is_not_complete(tmp_path):
    """指摘 1：全件取得が途中の 404・空ページで止まったら、キャッシュの件数が総数と合っていても完了にしない。
    ★のずれの印も消さない（直す前は len(キャッシュ) == 総数 で完了扱いだった）。"""
    for mode in (404, 200):
        d = tmp_path / str(mode)
        d.mkdir()
        maps = bl_base()
        bl_write(d, maps, 1)
        bl_mark(d, 20)
        st0 = state(d)
        st0["beatleader_stars_drift"] = True
        (d / "traversal.json").write_text(json.dumps(st0), encoding="utf-8")
        status = {"complete": True}
        got = _bl_full(d, BlStatusFake(maps, 3, mode), status)
        assert {x["id"] for x in got} == {m["id"] for m in maps}, (mode, "途中で止まったのに譜面を消した")
        st = state(d)
        assert st["beatleader_complete"] == st0["beatleader_complete"], (mode, "完了の記録が進んだ")
        assert st["beatleader_last_ok"] is False, mode
        assert st.get("beatleader_stars_drift") is True, (mode, "★のずれの印が消えた")
        assert status["complete"] is False, mode


def test_bl_full_traversal_exact_multiple_ends_with_empty_page(tmp_path):
    """総数がページ数でちょうど割り切れ、最後の次のページが空・404 で返るのは一覧の終わり（完了）。"""
    for mode in (404, 200):
        d = tmp_path / str(mode)
        d.mkdir()
        maps = [bl_lb(i, 10 + i) for i in range(200)]
        bl_write(d, maps, 1)
        bl_mark(d, 20)
        status = {"complete": True}
        _bl_full(d, BlStatusFake(maps, 3, mode), status)
        st = state(d)
        assert st["beatleader_complete"] == st["beatleader"] and st["beatleader_last_ok"] is True, mode
        assert status["complete"] is True, mode


def test_bl_first_page_404_marks_incomplete(tmp_path):
    """指摘 6：キャッシュを使う経路で 1 ページ目が 404 なら status["complete"] = False を入れる。"""
    for until in (None, BL_NOW - timedelta(days=61), datetime(2000, 1, 1)):
        d = tmp_path / str(len(list(tmp_path.iterdir())))
        d.mkdir()
        maps = bl_base()
        bl_write(d, maps, 1)
        bl_mark(d, 1)
        status = {"complete": True}
        with mock.patch.object(bl, "CACHE_DIR", d),                 mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", d / "traversal.json"):
            got = bl._get_beatleader_leaderboards_ranked(BlStatusFake(maps, 1, 404), fetch_until=until, status=status)
        assert len(got) == len(maps), until
        assert status["complete"] is False, until


def test_bl_first_page_404_keeps_scores_of_unranked_map(tmp_path):
    """指摘 6 の影響：1 ページ目 404 の後のスコアの取り直しで、ランクから外れたばかりの譜面のスコアを外さない。"""
    ranked = [bl_lb(i, 10 + i) for i in range(5)]
    bl_write(tmp_path, ranked, 1)
    bl_mark(tmp_path, 1)

    def item(i, pp):
        return {"pp": pp, "timeset": "1700000000",
                "leaderboard": {"id": f"L{i}", "difficulty": {"status": 3, "rankedTime": 1}}}
    scores = {f"L{i}": item(i, 100.0 - i) for i in range(5)}
    (tmp_path / "beatleader_player_scores_7.json").write_text(
        json.dumps({"fetched_at": iso(BL_NOW), "total_play_count": 5, "scores": scores}), encoding="utf-8")

    class ScoreApi:
        def get(self, url, params=None, timeout=None):
            data = [item(i, 100.0 - i) for i in range(4)] if params["page"] == "1" else []  # L4 はランク外れで返らない
            return T._Resp({"metadata": {"total": 4}, "data": data})
    status = {"complete": True}
    with mock.patch.object(bl, "CACHE_DIR", tmp_path),             mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", tmp_path / "traversal.json"):
        bl._get_beatleader_leaderboards_ranked(BlStatusFake(ranked, 1, 404), fetch_until=datetime(2000, 1, 1),
                                               status=status)
        bl.refresh_beatleader_ranked_scores("7", ScoreApi(), status=status)
    saved = json.loads((tmp_path / "beatleader_player_scores_7.json").read_text(encoding="utf-8"))["scores"]
    assert "L4" in saved, "1 ページ目 404 なのにスコアを外した"


def test_bl_long_gap(tmp_path):
    for gap in (90, 400):
        d = tmp_path / str(gap)
        d.mkdir()
        maps = bl_base()
        bl_write(d, maps, gap)
        api_maps = maps + [bl_lb(3700 + i, gap - 10 - i) for i in range(5)]
        bl_mark(d, gap)
        got, _ = bl_run(d, BlFake(api_maps), gap)
        assert set(got) == {m["id"] for m in api_maps}, gap


def test_bl_new_ranked_with_old_play(tmp_path):
    ranked = [bl_lb(1, 3), bl_lb(2, 3)]
    bl_write(tmp_path, ranked, 1)

    def item(i, status, pp):
        return {"pp": pp, "timeset": "1700000000",
                "leaderboard": {"id": f"L{i}", "difficulty": {"status": status, "rankedTime": 0}}}
    scores = {"L1": item(1, 0, 0), "L2": item(2, 0, 0), "L9": item(9, 0, 0)}
    (tmp_path / "beatleader_player_scores_7.json").write_text(
        json.dumps({"fetched_at": iso(BL_NOW), "total_play_count": 3, "scores": scores}), encoding="utf-8")

    class Api:
        calls = 0

        def get(self, url, params=None, timeout=None):
            self.calls += 1
            data = [item(1, 3, 0.05)] if params["page"] == "1" else []
            return T._Resp({"metadata": {"total": 1}, "data": data})
    api = Api()
    with mock.patch.object(bl, "CACHE_DIR", tmp_path):
        assert bl.refresh_scores_of_newly_ranked_maps("7", api) == 2
        saved = json.loads((tmp_path / "beatleader_player_scores_7.json").read_text(encoding="utf-8"))["scores"]
        assert saved["L1"]["pp"] == 0.05 and saved["L1"]["leaderboard"]["difficulty"]["status"] == 3
        # 指摘 4：取り直してもランクのスコアにならなかった L2 は書き換えず、検知済みの印だけ残す
        assert saved["L2"]["leaderboard"]["difficulty"]["status"] == 0, "譜面情報を書き換えた"
        assert saved["L9"]["leaderboard"]["difficulty"]["status"] == 0
        marks = json.loads((tmp_path / "newly_ranked_checked.json").read_text(encoding="utf-8"))
        assert set(marks["beatleader"]["7"]) == {"L2"}
        calls = api.calls
        assert bl.refresh_scores_of_newly_ranked_maps("7", api) == 0
        assert api.calls == calls
        # プレイし直しても取り直さない
        p = tmp_path / "beatleader_player_scores_7.json"
        raw = json.loads(p.read_text(encoding="utf-8"))
        raw["scores"]["L2"] = dict(item(2, 0, 0), timeset="1800000000")
        p.write_text(json.dumps(raw), encoding="utf-8")
        assert bl.refresh_scores_of_newly_ranked_maps("7", api) == 0
        assert api.calls == calls, "プレイし直すたびに取り直している"
        # ランクになり直した（rankedTime が変わった）ら、もう一度検知する
        lbs = [bl_lb(1, 3), dict(bl_lb(2, 1))]
        lbs[1]["difficulty"] = dict(lbs[1]["difficulty"], rankedTime=lbs[1]["difficulty"]["rankedTime"] + 5)
        bl_write(tmp_path, lbs, 1)
        assert bl.refresh_scores_of_newly_ranked_maps("7", api) == 1


if __name__ == "__main__":
    import tempfile
    import traceback
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn(Path(tempfile.mkdtemp()))
                print("OK   ", name, file=sys.stderr)
            except AssertionError as e:
                fails += 1
                print("FAIL ", name, e, file=sys.stderr)
            except Exception:
                fails += 1
                print("ERROR", name, file=sys.stderr)
                traceback.print_exc()
    sys.exit(1 if fails else 0)

