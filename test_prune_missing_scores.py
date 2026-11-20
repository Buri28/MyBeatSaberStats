"""PP の取り直し（ランク譜面のスコアだけを全部読み直す）で返らなかったスコアを外す処理の回帰テスト。

模擬 session で応答を作り、キャッシュは一時フォルダに向ける（実キャッシュ・実 API には触らない）。
実行: .venv\\Scripts\\python.exe test_prune_missing_scores.py  または  pytest test_prune_missing_scores.py
"""
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent / "src"))
from mybeatsaberstats.collector import beatleader as bl  # noqa: E402
from mybeatsaberstats.collector import scoresaber as ss  # noqa: E402


class _Resp:
    url = "x"
    headers: dict = {}

    def __init__(self, data, status_code=200):
        self._d = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._d


# ---------------- BeatLeader ----------------

def bl_item(i, status=3, pp=100.0):
    return {"id": i, "pp": pp, "timeset": str(1700000000 + i),
            "leaderboard": {"id": f"L{i}", "difficulty": {"status": status, "stars": 5.0}}}


def bl_write_ranked(tmp, ids):
    maps = [{"id": f"L{i}", "difficulty": {"status": 3, "rankedTime": 0, "stars": 5.0}} for i in ids]
    payload = {"fetched_at": "2026-11-18T00:00:00Z", "pages": [
        {"page": 1, "params": {"type": "Ranked"}, "data": {"metadata": {"total": len(maps)}, "data": maps}}]}
    (tmp / "beatleader_ranked_maps.json").write_text(json.dumps(payload), encoding="utf-8")


def bl_write_scores(tmp, items, failed_pages=(5,)):
    scores = {it["leaderboard"]["id"]: it for it in items}
    (tmp / "beatleader_player_scores_7.json").write_text(json.dumps(
        {"fetched_at": "2026-11-18T00:00:00Z", "total_play_count": len(scores), "scores": scores,
         "failed_pages": list(failed_pages)}), encoding="utf-8")


def bl_read(tmp):
    return json.loads((tmp / "beatleader_player_scores_7.json").read_text(encoding="utf-8"))


class BlApi:
    """type=ranked・sortBy=pp の応答を返す。fail_page 以降は例外、status_page 以降はその HTTP 状態。"""

    def __init__(self, items, per_page=100, total=None, fail_page=None, status_page=None, status_code=429,
                 endless=False):
        self.items = items
        self.per_page = per_page
        self.total = len(items) if total is None else total
        self.fail_page = fail_page
        self.status_page = status_page
        self.status_code = status_code
        self.endless = endless
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        assert params["type"] == "ranked" and params["sortBy"] == "pp"
        page = int(params["page"])
        if self.fail_page is not None and page >= self.fail_page:
            raise ConnectionError("boom")
        if self.status_page is not None and page >= self.status_page:
            return _Resp({}, status_code=self.status_code)
        if self.endless:
            part = [bl_item(100000 + page * 100 + j) for j in range(100)]
        else:
            part = self.items[(page - 1) * self.per_page: page * self.per_page]
        return _Resp({"metadata": {"total": self.total}, "data": part})


def bl_setup(tmp, n_ranked=150):
    """キャッシュ：ランク pp>0 が n_ranked 件（L0..）、ランク pp 0（L900）、ランク外（L901）、
    ランク譜面キャッシュに無いがスコア上は status 3 で pp>0（L902＝ランクから外れたばかり）。"""
    ranked_scores = [bl_item(i, 3, 100.0 + i) for i in range(n_ranked)]
    others = [bl_item(900, 3, 0.0), bl_item(901, 0, 0.0), bl_item(902, 3, 50.0)]
    bl_write_scores(tmp, ranked_scores + others)
    bl_write_ranked(tmp, list(range(n_ranked)) + [900])
    return ranked_scores


def bl_refresh(tmp, api, **kw):
    # 失敗のログは実際のログフォルダに書かない
    with mock.patch.object(bl, "CACHE_DIR", tmp), mock.patch.object(bl, "_log_api_failure"):
        return bl.refresh_beatleader_ranked_scores("7", api, **kw)


def test_bl_complete_removes_only_missing_ranked(tmp_path):
    ranked = bl_setup(tmp_path)
    returned = [x for x in ranked if x["leaderboard"]["id"] != "L3"]  # L3 だけサーバーで消えた
    st = {"complete": True}
    n = bl_refresh(tmp_path, BlApi(returned), status=st)
    saved = bl_read(tmp_path)
    keys = set(saved["scores"])
    assert n == len(returned)
    assert "L3" not in keys, "返らなかったランク譜面のスコアが残った"
    assert {"L900", "L901", "L902"} <= keys, "pp 0・ランク外・ランク譜面キャッシュに無いスコアを外した"
    assert len(keys) == len(ranked) + 3 - 1
    assert saved["failed_pages"] == [5], "failed_pages が消えた"
    assert st["complete"] is True


def test_bl_exception_midway_keeps_all(tmp_path):
    ranked = bl_setup(tmp_path)
    returned = [x for x in ranked if x["leaderboard"]["id"] != "L3"]
    st = {"complete": True}
    bl_refresh(tmp_path, BlApi(returned, fail_page=2), status=st)
    saved = bl_read(tmp_path)
    assert "L3" in saved["scores"] and len(saved["scores"]) == len(ranked) + 3
    assert saved["failed_pages"] == [5]
    assert st["complete"] is False


def test_bl_http_429_midway_keeps_all(tmp_path):
    ranked = bl_setup(tmp_path)
    returned = [x for x in ranked if x["leaderboard"]["id"] != "L120"]
    st = {"complete": True}
    bl_refresh(tmp_path, BlApi(returned, status_page=2, status_code=429), status=st)
    saved = bl_read(tmp_path)
    assert "L120" in saved["scores"] and len(saved["scores"]) == len(ranked) + 3
    assert st["complete"] is False


def test_bl_page_cap_keeps_all(tmp_path):
    ranked = bl_setup(tmp_path, n_ranked=10)
    bl_refresh(tmp_path, BlApi([], total=30000, endless=True))  # 300 ページ目まで 100 件ずつ返り続ける
    saved = bl_read(tmp_path)
    assert all(x["leaderboard"]["id"] in saved["scores"] for x in ranked), "ページ上限で止まったのに外した"
    assert saved["failed_pages"] == [5]


def test_bl_zero_items_keeps_all(tmp_path):
    ranked = bl_setup(tmp_path)
    assert bl_refresh(tmp_path, BlApi([])) == 0
    saved = bl_read(tmp_path)
    assert len(saved["scores"]) == len(ranked) + 3


def test_bl_total_mismatch_keeps_all(tmp_path):
    ranked = bl_setup(tmp_path)
    returned = [x for x in ranked if x["leaderboard"]["id"] != "L3"]
    bl_refresh(tmp_path, BlApi(returned, total=len(returned) + 50))
    saved = bl_read(tmp_path)
    assert "L3" in saved["scores"], "total と食い違うのに外した"


def test_bl_prior_incomplete_or_disabled_keeps_all(tmp_path):
    ranked = bl_setup(tmp_path)
    returned = [x for x in ranked if x["leaderboard"]["id"] != "L3"]
    bl_refresh(tmp_path, BlApi(returned), status={"complete": False})  # ランク譜面の取り直しが先に失敗
    assert "L3" in bl_read(tmp_path)["scores"]
    bl_refresh(tmp_path, BlApi(returned), prune_missing=False)
    assert "L3" in bl_read(tmp_path)["scores"]


def test_bl_too_many_missing_keeps_all(tmp_path):
    ranked = bl_setup(tmp_path, n_ranked=300)
    returned = ranked[:200]  # 100 件（1/3）返らない → API の異常を疑う
    bl_refresh(tmp_path, BlApi(returned))
    assert len(bl_read(tmp_path)["scores"]) == 300 + 3


def test_bl_newly_ranked_flow_does_not_prune(tmp_path):
    """新しくランクになった譜面の取り直しでは外さない（ランク譜面キャッシュが差分取得だけで古いことがあるため）。"""
    ranked = bl_setup(tmp_path)
    stale = bl_item(950, 0, 0.0)  # スコア上はランク外、ランク譜面キャッシュではランク
    data = bl_read(tmp_path)
    data["scores"]["L950"] = stale
    (tmp_path / "beatleader_player_scores_7.json").write_text(json.dumps(data), encoding="utf-8")
    bl_write_ranked(tmp_path, list(range(len(ranked))) + [900, 950])
    returned = [x for x in ranked if x["leaderboard"]["id"] != "L3"] + [bl_item(950, 3, 12.0)]
    returned.sort(key=lambda x: -x["pp"])
    with mock.patch.object(bl, "CACHE_DIR", tmp_path), \
            mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", tmp_path / "traversal.json"):
        assert bl.refresh_scores_of_newly_ranked_maps("7", BlApi(returned)) == 1
    saved = bl_read(tmp_path)
    assert "L3" in saved["scores"], "新しくランクになった譜面の取り直しで外した"
    assert saved["scores"]["L950"]["pp"] == 12.0
    assert {"L900", "L901", "L902"} <= set(saved["scores"])
    assert saved["failed_pages"] == [5]


def test_bl_limit_boundary(tmp_path):
    """外すのは 20 件まで。21 件なら API の異常を疑って何も外さない。"""
    ranked = bl_setup(tmp_path, n_ranked=200)
    bl_refresh(tmp_path, BlApi(ranked[20:]))
    assert len(bl_read(tmp_path)["scores"]) == 200 + 3 - 20
    d = tmp_path / "b"
    d.mkdir()
    ranked = bl_setup(d, n_ranked=200)
    bl_refresh(d, BlApi(ranked[21:]))
    assert len(bl_read(d)["scores"]) == 200 + 3


def test_bl_empty_last_page_keeps_all(tmp_path):
    """最後のページ（14 件）が一時的に空で返った。total に届かないので外さない。"""
    ranked = bl_setup(tmp_path, n_ranked=214)

    class Api(BlApi):
        def get(self, url, params=None, timeout=None):
            if params["page"] == "3":
                self.calls += 1
                return _Resp({"metadata": {"total": self.total}, "data": []})
            return super().get(url, params, timeout)
    bl_refresh(tmp_path, Api(ranked))
    assert len(bl_read(tmp_path)["scores"]) == 214 + 3


def test_bl_missing_total_keeps_all(tmp_path):
    ranked = bl_setup(tmp_path)
    returned = [x for x in ranked if x["leaderboard"]["id"] != "L3"]

    class Api(BlApi):
        def get(self, url, params=None, timeout=None):
            r = super().get(url, params, timeout)
            r._d["metadata"] = {}
            return r
    bl_refresh(tmp_path, Api(returned))
    assert "L3" in bl_read(tmp_path)["scores"]


def test_bl_order_shift_duplicate_keeps_all(tmp_path):
    """読んでいる途中で pp の並びが動き、1 件が 2 ページに重複して 1 件が読み飛ばされた。外さない。"""
    ranked = bl_setup(tmp_path)
    order = list(ranked)

    class Api(BlApi):
        def get(self, url, params=None, timeout=None):
            self.calls += 1
            page = int(params["page"])
            if page == 1:
                part = order[:100]
            else:  # L120 が 1 ページ目へ移り、L99 が 2 ページ目にも出る
                part = [order[99]] + [x for x in order[100:] if x["leaderboard"]["id"] != "L120"]
            return _Resp({"metadata": {"total": len(order)}, "data": part})
    bl_refresh(tmp_path, Api(order))
    assert "L120" in bl_read(tmp_path)["scores"]


def test_bl_no_ranked_cache_keeps_all(tmp_path):
    ranked = bl_setup(tmp_path)
    (tmp_path / "beatleader_ranked_maps.json").unlink()
    bl_refresh(tmp_path, BlApi([x for x in ranked if x["leaderboard"]["id"] != "L3"]))
    assert "L3" in bl_read(tmp_path)["scores"]


# ---------------- ScoreSaber ----------------

def ss_item(i, ranked=True, pp=100.0):
    return {"score": {"id": i, "pp": pp, "timeSet": "2026-01-01T00:00:00.000Z", "modifiers": ""},
            "leaderboard": {"id": i, "ranked": ranked, "stars": 5.0 if ranked else 0}}


def ss_write_ranked(tmp, ids):
    payload = {"fetched_at": "2026-11-18T00:00:00.000000Z",
               "leaderboards": {str(i): {"id": i, "ranked": True, "stars": 5.0} for i in ids},
               "max_pages": 1, "total_maps": len(ids)}
    (tmp / "scoresaber_ranked_maps.json").write_text(json.dumps(payload), encoding="utf-8")


def ss_write_scores(tmp, items, resume_stop_ts="2026-01-02T00:00:00.000Z"):
    scores = {str(it["leaderboard"]["id"]): it for it in items}
    payload = {"fetched_at": "2026-11-18T00:00:00Z", "total_play_count": len(scores), "scores": scores}
    if resume_stop_ts:
        payload["resume_stop_ts"] = resume_stop_ts
    (tmp / "scoresaber_player_scores_1.json").write_text(json.dumps(payload), encoding="utf-8")


def ss_read(tmp):
    return json.loads((tmp / "scoresaber_player_scores_1.json").read_text(encoding="utf-8"))


class SsApi:
    """sort=top の応答。返す全件のあとに pp 0 のスコア（ランク外）が続く。"""

    def __init__(self, items, tail_unranked=True, fail_page=None, status_page=None, status_code=429,
                 endless=False):
        self.items = list(items) + ([ss_item(5000 + j, False, 0.0) for j in range(30)] if tail_unranked else [])
        self.fail_page = fail_page
        self.status_page = status_page
        self.status_code = status_code
        self.endless = endless
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        assert params["sort"] == "top"
        page = int(params["page"])
        if self.fail_page is not None and page >= self.fail_page:
            raise ConnectionError("boom")
        if self.status_page is not None and page >= self.status_page:
            return _Resp({}, status_code=self.status_code)
        if self.endless:
            part = [ss_item(100000 + page * 100 + j) for j in range(100)]
        else:
            part = self.items[(page - 1) * 100: page * 100]
        return _Resp({"playerScores": part, "metadata": {"total": len(self.items)}})


def ss_setup(tmp, n_ranked=150):
    ranked_scores = [ss_item(i, True, 300.0 - i) for i in range(n_ranked)]
    others = [ss_item(900, True, 0.0), ss_item(901, False, 0.0), ss_item(902, True, 50.0)]
    ss_write_scores(tmp, ranked_scores + others)
    ss_write_ranked(tmp, list(range(n_ranked)) + [900])
    return ranked_scores


def ss_refresh(tmp, api, **kw):
    # プロフィールの rankedPlayCount（サーバー上の pp 付きスコアの数）。指定が無ければ API が返す件数にする
    if "ranked_play_count" not in kw and isinstance(getattr(api, "items", None), list):
        kw["ranked_play_count"] = sum(1 for x in api.items if ss._ss_score_pp(x) > 0)
    with mock.patch.object(ss, "CACHE_DIR", tmp), mock.patch.object(ss, "log_api_failure"):
        return ss.refresh_scoresaber_ranked_scores("1", api, **kw)


def test_ss_complete_removes_only_missing_ranked(tmp_path):
    ranked = ss_setup(tmp_path)
    returned = [x for x in ranked if x["leaderboard"]["id"] != 3]
    st = {"complete": True}
    n = ss_refresh(tmp_path, SsApi(returned), status=st)
    saved = ss_read(tmp_path)
    keys = set(saved["scores"])
    assert n == len(returned)
    assert "3" not in keys, "返らなかったランク譜面のスコアが残った"
    assert {"900", "901", "902"} <= keys, "pp 0・ランク外・ランク譜面キャッシュに無いスコアを外した"
    assert len(keys) == len(ranked) + 3 - 1
    assert saved.get("resume_stop_ts") == "2026-01-02T00:00:00.000Z", "resume_stop_ts が消えた"
    assert st["complete"] is True


def test_ss_complete_without_unranked_tail(tmp_path):
    """pp 0 のスコアが出る前に最後のページ（100 件未満）で終わっても、最後まで読めた扱い。"""
    ranked = ss_setup(tmp_path)
    returned = [x for x in ranked if x["leaderboard"]["id"] != 7]
    ss_refresh(tmp_path, SsApi(returned, tail_unranked=False))
    assert "7" not in ss_read(tmp_path)["scores"]


def test_ss_exception_midway_keeps_all(tmp_path):
    ranked = ss_setup(tmp_path)
    returned = [x for x in ranked if x["leaderboard"]["id"] != 3]
    st = {"complete": True}
    ss_refresh(tmp_path, SsApi(returned, fail_page=2), status=st)
    saved = ss_read(tmp_path)
    assert "3" in saved["scores"] and len(saved["scores"]) == len(ranked) + 3
    assert saved.get("resume_stop_ts") == "2026-01-02T00:00:00.000Z"
    assert st["complete"] is False


def test_ss_rate_limited_midway_keeps_all(tmp_path):
    ranked = ss_setup(tmp_path)
    returned = [x for x in ranked if x["leaderboard"]["id"] != 120]
    st = {"complete": True}
    ss_refresh(tmp_path, SsApi(returned, status_page=2, status_code=429), status=st)
    saved = ss_read(tmp_path)
    assert "120" in saved["scores"] and len(saved["scores"]) == len(ranked) + 3
    assert st["complete"] is False


def test_ss_page_cap_keeps_all(tmp_path):
    ranked = ss_setup(tmp_path, n_ranked=10)
    with mock.patch.object(ss, "_MAX_PAGES_GUARD", 3):
        ss_refresh(tmp_path, SsApi([], endless=True))
    saved = ss_read(tmp_path)
    assert all(str(x["leaderboard"]["id"]) in saved["scores"] for x in ranked), "ページ上限で止まったのに外した"
    assert saved.get("resume_stop_ts") == "2026-01-02T00:00:00.000Z"


def test_ss_zero_items_keeps_all(tmp_path):
    ranked = ss_setup(tmp_path)
    assert ss_refresh(tmp_path, SsApi([], tail_unranked=False)) == 0
    assert len(ss_read(tmp_path)["scores"]) == len(ranked) + 3
    assert ss_refresh(tmp_path, SsApi([])) == 0  # 最初から pp 0 だけ
    assert len(ss_read(tmp_path)["scores"]) == len(ranked) + 3


def test_ss_prior_incomplete_or_disabled_keeps_all(tmp_path):
    ranked = ss_setup(tmp_path)
    returned = [x for x in ranked if x["leaderboard"]["id"] != 3]
    ss_refresh(tmp_path, SsApi(returned), status={"complete": False})
    assert "3" in ss_read(tmp_path)["scores"]
    ss_refresh(tmp_path, SsApi(returned), prune_missing=False)
    assert "3" in ss_read(tmp_path)["scores"]


def test_ss_too_many_missing_keeps_all(tmp_path):
    ranked = ss_setup(tmp_path, n_ranked=300)
    ss_refresh(tmp_path, SsApi(ranked[:200]))
    assert len(ss_read(tmp_path)["scores"]) == 300 + 3


def test_ss_newly_ranked_flow_does_not_prune(tmp_path):
    ranked = ss_setup(tmp_path)
    data = ss_read(tmp_path)
    data["scores"]["950"] = ss_item(950, False, 0.0)
    (tmp_path / "scoresaber_player_scores_1.json").write_text(json.dumps(data), encoding="utf-8")
    ss_write_ranked(tmp_path, list(range(len(ranked))) + [900, 950])
    returned = [x for x in ranked if x["leaderboard"]["id"] != 3] + [ss_item(950, True, 12.0)]
    returned.sort(key=lambda x: -x["score"]["pp"])
    with mock.patch.object(ss, "CACHE_DIR", tmp_path), \
            mock.patch.object(ss, "_RANKED_FULL_TRAVERSAL_STATE", tmp_path / "traversal.json"):
        assert ss.refresh_scores_of_newly_ranked_maps("1", SsApi(returned)) == 1
    saved = ss_read(tmp_path)
    assert "3" in saved["scores"], "新しくランクになった譜面の取り直しで外した"
    assert saved["scores"]["950"]["score"]["pp"] == 12.0
    assert {"900", "901", "902"} <= set(saved["scores"])
    assert saved.get("resume_stop_ts") == "2026-01-02T00:00:00.000Z"


def test_ss_limit_boundary(tmp_path):
    ranked = ss_setup(tmp_path, n_ranked=200)
    ss_refresh(tmp_path, SsApi(ranked[20:]))
    assert len(ss_read(tmp_path)["scores"]) == 200 + 3 - 20
    d = tmp_path / "b"
    d.mkdir()
    ranked = ss_setup(d, n_ranked=200)
    ss_refresh(d, SsApi(ranked[21:]))
    assert len(ss_read(d)["scores"]) == 200 + 3


def test_ss_404_or_empty_midway_keeps_all(tmp_path):
    """途中のページが 404・空で返った。一覧の終わりまで読めていないので外さない。"""
    for code in (404, 200):
        d = tmp_path / str(code)
        d.mkdir()
        ranked = ss_setup(d, n_ranked=250)

        class Api(SsApi):
            def get(self, url, params=None, timeout=None, _code=code):
                if params["page"] == "3":
                    self.calls += 1
                    return _Resp({"playerScores": []}, status_code=_code)
                return super().get(url, params, timeout)
        ss_refresh(d, Api(ranked), status={"complete": True})
        assert len(ss_read(d)["scores"]) == 250 + 3, code


def test_ss_order_shift_duplicate_keeps_all(tmp_path):
    ranked = ss_setup(tmp_path)
    order = list(ranked)

    class Api(SsApi):
        def get(self, url, params=None, timeout=None):
            self.calls += 1
            if params["page"] == "1":
                part = order[:100]
            else:
                part = [order[99]] + [x for x in order[100:] if x["leaderboard"]["id"] != 120] \
                    + [ss_item(5000, False, 0.0)]
            return _Resp({"playerScores": part})
    ss_refresh(tmp_path, Api(order))
    assert "120" in ss_read(tmp_path)["scores"]


def test_ss_no_ranked_cache_keeps_all(tmp_path):
    ranked = ss_setup(tmp_path)
    (tmp_path / "scoresaber_ranked_maps.json").unlink()
    ss_refresh(tmp_path, SsApi([x for x in ranked if x["leaderboard"]["id"] != 3]))
    assert "3" in ss_read(tmp_path)["scores"]


def test_ss_seen_ids_and_listing_short(tmp_path):
    """ランク譜面の全件取得で列挙数が総数に届かなかっただけなら外す。ただし今回の一覧に出た譜面だけ。"""
    ranked = ss_setup(tmp_path)
    returned = [x for x in ranked if x["leaderboard"]["id"] not in (3, 4)]
    seen = {str(i) for i in range(len(ranked)) if i != 4}  # 4 は一覧に出なかった（ランク外れかもしれない）
    st = {"complete": False, "ss_ranked_listing_short": True, "ss_ranked_seen_ids": seen}
    ss_refresh(tmp_path, SsApi(returned), status=st)
    keys = set(ss_read(tmp_path)["scores"])
    assert "3" not in keys and "4" in keys


def test_ss_ranked_listing_sets_seen_ids(tmp_path):
    """ランク譜面の全件取得が、一覧に出た譜面と「総数に届かなかっただけ」の印を status に入れる。失敗時は入れない。"""
    import test_ss_ranked_cases as T
    from datetime import datetime as _dt
    maps = T.base_maps()

    class ShortApi(T.FakeApi):
        def get(self, url, params=None, timeout=None):
            r = super().get(url, params, timeout)
            r._d["metadata"]["total"] = len(self.maps) + 3  # 列挙数が総数に届かない
            return r

    def fetch(d, api, st):
        T.write_cache(d, maps, fetched_days_ago=1)
        with mock.patch.object(ss, "CACHE_DIR", d), \
                mock.patch.object(ss, "_RANKED_FULL_TRAVERSAL_STATE", d / "traversal.json"), \
                mock.patch.object(ss, "_is_rate_limited", return_value=False), \
                mock.patch.object(ss, "log_api_failure"):
            ss._get_scoresaber_leaderboards_ranked(api, fetch_until=_dt(2000, 1, 1), status=st)

    st = {"complete": True}
    fetch(tmp_path, ShortApi(maps), st)
    # 指摘 2：一覧を最後まで読めて件数だけ足りないのは失敗ではない（complete は落とさない）
    assert st["complete"] is True
    assert st.get("ss_ranked_listing_short") is True
    assert st["ss_ranked_seen_ids"] == {str(m["id"]) for m in maps}
    d = tmp_path / "fail"
    d.mkdir()
    st2 = {"complete": True}
    fetch(d, T.FakeApi(maps, fail_page=2), st2)
    assert st2["complete"] is False and not st2.get("ss_ranked_listing_short")


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
