"""ScoreSaber ランク譜面の取得判定を、擬似 API で状態を作って確かめる（ケース 1〜3, 6）。"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent / "src"))
from mybeatsaberstats.collector import scoresaber as ss  # noqa: E402

PER_PAGE = 14
NOW = datetime(2026, 11, 18)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def lb(i, days_ago):
    return {"id": i, "rankedDate": None if days_ago is None else iso(NOW - timedelta(days=days_ago)),
            "createdDate": "2018-07-01T00:00:00.000Z", "ranked": True, "stars": 5.0}


class _Resp:
    status_code = 200
    url = "x"
    headers: dict = {}

    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


class FakeApi:
    """rankedDate 降順（null が先頭）で返す。fail_page で指定ページ以降を失敗させる。"""

    def __init__(self, maps, fail_page=None):
        self.maps = maps
        self.fail_page = fail_page
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        page = int(params["page"])
        if self.fail_page is not None and page >= self.fail_page:
            raise ConnectionError("boom")
        ordered = sorted(self.maps, key=lambda m: (m["rankedDate"] is not None, m["rankedDate"] or ""), reverse=False)
        # null 先頭、以降は新しい順
        nulls = [m for m in self.maps if m["rankedDate"] is None]
        dated = sorted([m for m in self.maps if m["rankedDate"] is not None], key=lambda m: m["rankedDate"], reverse=True)
        ordered = nulls + dated
        part = ordered[(page - 1) * PER_PAGE: page * PER_PAGE]
        return _Resp({"metadata": {"total": len(self.maps), "itemsPerPage": PER_PAGE}, "leaderboards": part})


def write_cache(tmp, maps, fetched_days_ago):
    payload = {"fetched_at": iso(NOW - timedelta(days=fetched_days_ago)).replace(".000Z", ".000000Z"),
               "leaderboards": {str(m["id"]): m for m in maps}, "max_pages": 1, "total_maps": len(maps)}
    (tmp / "scoresaber_ranked_maps.json").write_text(json.dumps(payload), encoding="utf-8")


def mark_full(tmp, days_ago=1, drift=False):
    """全件取得が最後まで取れた記録（何日前でも全件取り直しにしない）。窓の判定だけを確かめるテスト用。
    drift=True なら★のずれの印も付け、次の取得を全件取り直しにする。"""
    stamp = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = {"scoresaber": stamp, "scoresaber_complete": stamp}
    if drift:
        state["scoresaber_stars_drift"] = True
    (tmp / "traversal.json").write_text(json.dumps(state), encoding="utf-8")


def run(tmp, api, fetched_days_ago, window_days=60, fresh_full=True):
    until = NOW - timedelta(days=fetched_days_ago + window_days)
    if fresh_full and not (tmp / "traversal.json").exists():
        mark_full(tmp)
    with mock.patch.object(ss, "CACHE_DIR", tmp), \
         mock.patch.object(ss, "_RANKED_FULL_TRAVERSAL_STATE", tmp / "traversal.json"), \
         mock.patch.object(ss, "_is_rate_limited", return_value=False):
        res = ss._get_scoresaber_leaderboards_ranked(api, fetch_until=until)
    return {str(k) for k in res}, json.loads((tmp / "scoresaber_ranked_maps.json").read_text(encoding="utf-8"))


def base_maps():
    # 古い 40 件（150〜500 日前）、null 5 件、最近 10 件
    m = [lb(i, None) for i in range(1, 6)]
    m += [lb(100 + i, 150 + i * 5) for i in range(40)]
    m += [lb(200 + i, 5 + i) for i in range(10)]
    return m


def test_case1_unranked_only(tmp_path):
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    api_maps = [m for m in maps if m["id"] != 120]  # 古い譜面がランクから外れた
    ids, saved = run(tmp_path, FakeApi(api_maps), 1)
    assert ids == {str(m["id"]) for m in api_maps}, "ランクから外れた譜面が残った"


def test_case1_unranked_recent(tmp_path):
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    api_maps = [m for m in maps if m["id"] != 200]  # 窓内の新しい譜面が外れた
    ids, _ = run(tmp_path, FakeApi(api_maps), 1)
    assert ids == {str(m["id"]) for m in api_maps}


def test_case2_ranked_only(tmp_path):
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    api_maps = maps + [lb(300, 0.5), lb(301, 0.2)]
    ids, _ = run(tmp_path, FakeApi(api_maps), 1)
    assert {"300", "301"} <= ids


def test_case3_gap_90_days(tmp_path):
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=90)
    api_maps = maps + [lb(300 + i, 80 - i * 3) for i in range(20)]  # 空けた間（90〜3 日前）に新ランク
    ids, _ = run(tmp_path, FakeApi(api_maps), 90)
    assert ids == {str(m["id"]) for m in api_maps}


def test_case3_gap_400_days(tmp_path):
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=400)
    api_maps = maps + [lb(300 + i, 390 - i * 15) for i in range(25)]
    ids, _ = run(tmp_path, FakeApi(api_maps), 400)
    assert ids == {str(m["id"]) for m in api_maps}


def test_case3_gap_with_unrank_and_rank(tmp_path):
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=90)
    api_maps = [m for m in maps if m["id"] != 130] + [lb(300, 10)]  # 増減が同時（総数は +0）
    ids, _ = run(tmp_path, FakeApi(api_maps), 90)
    assert ids == {str(m["id"]) for m in api_maps}


def test_case_cache_shortfall_heals(tmp_path):
    # キャッシュが古い譜面を欠いている（過去の取りこぼしの名残）。API 総数との突き合わせで直るべき
    maps = base_maps()
    write_cache(tmp_path, [m for m in maps if m["id"] not in (105, 110)], fetched_days_ago=1)
    ids, _ = run(tmp_path, FakeApi(maps), 1)
    assert ids == {str(m["id"]) for m in maps}


def test_case6_failure_keeps_fetched_at(tmp_path):
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=30)
    before = json.loads((tmp_path / "scoresaber_ranked_maps.json").read_text(encoding="utf-8"))["fetched_at"]
    api_maps = maps + [lb(300 + i, 25 - i) for i in range(20)]
    # 2 ページ目で失敗
    _, saved = run(tmp_path, FakeApi(api_maps, fail_page=2), 30)
    assert saved["fetched_at"] == before, "失敗したのに取得日時が進んだ"


def test_shortfall_traversal_is_rate_limited(tmp_path):
    # 総数が食い違い続ける場合（API 側が数えるが列挙しない譜面がある等）、全件取り直しは 7 日に 1 回まで
    maps = base_maps()
    api = FakeApi(maps)
    api.maps = maps + [lb(999, 3)]
    # 総数だけ +1 に見せ、実際には列挙されない譜面がある状態にする
    orig = api.get

    def get(url, params=None, timeout=None):
        r = orig(url, params, timeout)
        r._d["metadata"]["total"] = len(maps) + 2
        return r
    api.get = get
    api.maps = maps
    write_cache(tmp_path, maps, fetched_days_ago=1)
    run(tmp_path, api, 1)
    first = api.calls
    run(tmp_path, api, 1)
    second = api.calls - first
    assert second < first, f"2 回目も全件取り直した first={first} second={second}"


def test_case5_reconcile_resumes_when_drift_grows(tmp_path):
    from mybeatsaberstats.collector import collector as c
    now = c._dt_to_utc_z(datetime.utcnow() - timedelta(days=2))
    # 2 日前に全件取得したが、ずれ 0.5 が残った（仕組み上合わない）。この pp では取り直さない
    state = {"pp_mismatch_api_pp": "7000.0", "pp_mismatch_drift": "0.5", "full_scan_at": now}
    assert c._pp_reconcile_skip_reason(state, 7000.0, 0.5) is not None
    # pp が変わったが、ずれは残差と同程度 → 7 日待ち
    assert c._pp_reconcile_skip_reason(state, 7010.0, 0.55) is not None
    # 古いスコアの pp が変わって、ずれが 0.1 以上広がった → 7 日待たずに取り直す
    assert c._pp_reconcile_skip_reason(state, 7010.0, 0.7) is None
    # 失敗後 24 時間待ちは、広がっていても守る
    state2 = {"pp_reconcile_failed_at": c._dt_to_utc_z(datetime.utcnow() - timedelta(hours=2))}
    assert c._pp_reconcile_skip_reason(state2, 7010.0, 5.0) is not None


def test_case5_reconcile_first_drift_not_blocked(tmp_path):
    from mybeatsaberstats.collector import collector as c
    # 歯止めの記録が無い状態（普段）で 0.1 以上ずれたら取り直す
    assert c._pp_reconcile_skip_reason({}, 7000.0, 0.3) is None
    # 閾値未満（0.09）は main 側で取り直さない設定
    assert c._PP_RECONCILE_THRESHOLD == 0.1


if __name__ == "__main__":
    import tempfile
    import traceback
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn(Path(tempfile.mkdtemp()))
                print("落ちない  ", name)
            except AssertionError as e:
                print("落ちる    ", name, e)
            except Exception:
                print("ERROR     ", name)
                traceback.print_exc()
