"""ScoreSaber のスコア取得で、途中で読めなかった範囲だけを次の回に取り直す処理の回帰テスト。

模擬 session（sort=recent、timeSet の新しい順、1 ページ 100 件）で応答を作り、キャッシュは一時フォルダに向ける
（実キャッシュ・実 API には触らない）。
実行: .venv\\Scripts\\python.exe -m pytest test_ss_player_scores_resume.py
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent / "src"))
from mybeatsaberstats.collector import scoresaber as ss  # noqa: E402

BASE = datetime(2020, 1, 1)
SID = "1"


def ts(i):
    """i が大きいほど新しい timeSet。"""
    return (BASE + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def item(i, lb=None, t=None):
    return {"score": {"timeSet": ts(i) if t is None else t, "pp": 0},
            "leaderboard": {"id": i if lb is None else lb, "ranked": False}}


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


class RecentApi:
    """sort=recent の応答。fail_pages のページは通信の失敗にする。"""

    def __init__(self, items, fail_pages=()):
        self.items = list(items)
        self.fail_pages = set(fail_pages)
        self.pages: list[int] = []

    def ordered(self):
        return sorted(self.items, key=lambda x: x["score"]["timeSet"], reverse=True)

    def get(self, url, params=None, timeout=None):
        assert params["sort"] == "recent"
        page = int(params["page"])
        self.pages.append(page)
        if page in self.fail_pages:
            raise ConnectionError("boom")
        part = self.ordered()[(page - 1) * 100: page * 100]
        if not part and page > 1:
            return _Resp({}, status_code=404)
        return _Resp({"playerScores": part, "metadata": {"total": len(self.items), "page": page, "itemsPerPage": 100}})


def run(tmp, api, fetch_until=None):
    status = {"complete": True}
    with mock.patch.object(ss, "CACHE_DIR", tmp), mock.patch.object(ss, "log_api_failure"):
        api.pages = []
        out = ss._get_scoresaber_player_scores(SID, api, fetch_until=fetch_until, status=status)
    return out, status


def cache(tmp):
    return json.loads((tmp / f"scoresaber_player_scores_{SID}.json").read_text(encoding="utf-8"))


def write_cache(tmp, items, **extra):
    payload = {"fetched_at": "2026-11-18T00:00:00Z", "scores": {str(it["leaderboard"]["id"]): it for it in items}}
    payload.update(extra)
    (tmp / f"scoresaber_player_scores_{SID}.json").write_text(json.dumps(payload), encoding="utf-8")


def ids(tmp):
    return set(cache(tmp)["scores"].keys())


def all_items(n):
    return [item(i) for i in range(n)]


def test_initial_failure_resumes_only_rest(tmp_path):
    api = RecentApi(all_items(1000), fail_pages={5})
    _, st = run(tmp_path, api)
    assert st["complete"] is False
    assert api.pages == [1, 2, 3, 4, 5]
    c = cache(tmp_path)
    assert len(c["scores"]) == 400
    assert c["resume_ranges"] == [{"newer_ts": ts(600), "older_ts": ss._RESUME_FROM_BEGINNING}]
    assert any("page 5 could not be fetched" in w for w in st["warnings"])

    api.fail_pages = set()
    _, st = run(tmp_path, api)
    assert st["complete"] is True
    # page 1（差分）と、取りこぼした page 4（境目の確かめ）〜10 だけ。page 2-3 は読み直さない
    assert 2 not in api.pages and 3 not in api.pages
    assert api.pages[0] == 1 and api.pages[-1] == 10
    assert ids(tmp_path) == {str(i) for i in range(1000)}
    assert "resume_ranges" not in cache(tmp_path) and "resume_stop_ts" not in cache(tmp_path)
    assert any("前回の取りこぼしを取り直しました" in w for w in st["warnings"])

    # 3 回目は普段の差分取得（1 ページ、メッセージなし）
    _, st = run(tmp_path, api)
    assert api.pages == [1] and not st["warnings"]


def test_shifted_by_new_scores_does_not_miss(tmp_path):
    api = RecentApi(all_items(1000), fail_pages={5})
    run(tmp_path, api)
    # 新しいスコアが 150 件増え、古い譜面 2 件が再プレイされた（先頭へ移る）
    api.items += [item(2000 + j) for j in range(150)]
    for k in (3, 250):
        api.items = [x for x in api.items if x["leaderboard"]["id"] != k] + [item(3000 + k, lb=k)]
    api.fail_pages = set()
    _, st = run(tmp_path, api)
    assert st["complete"] is True
    assert len(set(api.pages)) < 12  # 全 12 ページは読まない
    assert ids(tmp_path) == {str(x["leaderboard"]["id"]) for x in api.items}


def test_stale_cache_entries_step_back(tmp_path):
    # キャッシュに API から消えた新しめのスコアが 250 件残っていると、見積もりのページが後ろにずれる
    api = RecentApi(all_items(1000), fail_pages={6})
    run(tmp_path, api)
    c = cache(tmp_path)
    for j in range(250):
        c["scores"][f"x{j}"] = item(900 + j % 50, lb=f"x{j}", t=ts(900) )
    (tmp_path / f"scoresaber_player_scores_{SID}.json").write_text(json.dumps(c), encoding="utf-8")
    api.fail_pages = set()
    _, st = run(tmp_path, api)
    assert st["complete"] is True
    assert {str(i) for i in range(1000)} <= ids(tmp_path)


def test_backfill_failure_then_only_gap(tmp_path):
    write_cache(tmp_path, all_items(1000))
    api = RecentApi(all_items(1000) + [item(1000 + j) for j in range(50)], fail_pages={4})
    until = datetime.strptime(ts(600), "%Y-%m-%dT%H:%M:%S.000Z")
    _, st = run(tmp_path, api, fetch_until=until)
    assert api.pages == [1, 2, 3, 4] and st["complete"] is False
    r = cache(tmp_path)["resume_ranges"]
    assert r == [{"newer_ts": ts(750), "older_ts": until.strftime("%Y-%m-%dT%H:%M:%SZ")}]
    api.fail_pages = set()
    _, st = run(tmp_path, api)
    assert st["complete"] is True
    assert api.pages == [1, 3, 4, 5]  # page 2 は読み直さない
    assert "resume_ranges" not in cache(tmp_path)


def test_fetch_all_failure_does_not_auto_refetch_all(tmp_path):
    write_cache(tmp_path, all_items(1000))
    api = RecentApi(all_items(1000), fail_pages={4})
    _, st = run(tmp_path, api, fetch_until=datetime(2000, 1, 1))
    assert st["complete"] is False
    assert any("select it again" in w for w in st["warnings"])
    assert "resume_ranges" not in cache(tmp_path)
    api.fail_pages = set()
    _, st = run(tmp_path, api)
    assert api.pages == [1]


def test_fetch_all_failure_keeps_unread_new_scores(tmp_path):
    write_cache(tmp_path, all_items(1000))
    api = RecentApi(all_items(1000) + [item(1000 + j) for j in range(250)], fail_pages={2})
    run(tmp_path, api, fetch_until=datetime(2000, 1, 1))
    assert cache(tmp_path)["resume_ranges"] == [{"newer_ts": ts(1150), "older_ts": ts(999)}]
    api.fail_pages = set()
    _, st = run(tmp_path, api)
    assert st["complete"] is True
    assert ids(tmp_path) == {str(x["leaderboard"]["id"]) for x in api.items}
    assert max(api.pages) <= 4


def test_recovery_failure_narrows_and_keeps_range(tmp_path):
    api = RecentApi(all_items(1000), fail_pages={3})
    run(tmp_path, api)
    api.fail_pages = {7}
    _, st = run(tmp_path, api)
    assert st["complete"] is False
    assert cache(tmp_path)["resume_ranges"] == [{"newer_ts": ts(400), "older_ts": ss._RESUME_FROM_BEGINNING}]
    assert any("unfetched range" in w for w in st["warnings"])
    api.fail_pages = set()
    _, st = run(tmp_path, api)
    assert st["complete"] is True
    assert 3 not in api.pages and 4 not in api.pages
    assert ids(tmp_path) == {str(i) for i in range(1000)}


def test_page1_failure_keeps_range_to_latest_cached(tmp_path):
    # page 1 から読めなくても、キャッシュの最新までの範囲を残す。同じ回の PP の取り直し（sort=top）が
    # 最近のランク譜面のプレイをキャッシュに入れて最新 timeSet が進んでも、その間を読み落とさない
    write_cache(tmp_path, all_items(1000))
    api = RecentApi(all_items(1000) + [item(1000 + j) for j in range(150)], fail_pages={1})
    _, st = run(tmp_path, api)
    assert api.pages == [1] and st["complete"] is False
    assert cache(tmp_path)["resume_ranges"] == [{"newer_ts": None, "older_ts": ts(999)}]
    c = cache(tmp_path)
    ranked = item(1140)
    ranked["score"]["pp"] = 300.0
    c["scores"]["1140"] = ranked  # PP の取り直しで入った最近のランク譜面のプレイ
    (tmp_path / f"scoresaber_player_scores_{SID}.json").write_text(json.dumps(c), encoding="utf-8")
    api.fail_pages = set()
    _, st = run(tmp_path, api)
    assert st["complete"] is True and api.pages == [1, 2]
    assert ids(tmp_path) == {str(x["leaderboard"]["id"]) for x in api.items}
    assert "resume_ranges" not in cache(tmp_path)
    # キャッシュの最新が進んだ所まで読み直したので、取り直しの知らせを出す
    assert any("前回の取りこぼしを取り直しました" in w for w in st["warnings"])


def test_page1_failure_then_normal_diff_has_no_message(tmp_path):
    write_cache(tmp_path, all_items(1000))
    api = RecentApi(all_items(1000), fail_pages={1})
    run(tmp_path, api)
    api.fail_pages = set()
    _, st = run(tmp_path, api)
    assert api.pages == [1] and st["complete"] is True and not st["warnings"]
    assert "resume_ranges" not in cache(tmp_path)


def test_legacy_resume_from_beginning_ignores_old_ranked_scores(tmp_path):
    # 古い版の初回取得が page 5 で止まったあと、ランク譜面の取り直しで古い pp 付きスコアが入っていた
    olds = []
    for i in (10, 20, 30):
        it = item(i)
        it["score"]["pp"] = 200.0
        olds.append(it)
    write_cache(tmp_path, [item(i) for i in range(600, 1000)] + olds, resume_stop_ts=ss._RESUME_FROM_BEGINNING)
    api = RecentApi(all_items(1000))
    _, st = run(tmp_path, api)
    assert st["complete"] is True
    assert ids(tmp_path) == {str(i) for i in range(1000)}
    assert 2 not in api.pages


def test_fetch_all_without_cache_failure_resumes(tmp_path):
    api = RecentApi(all_items(1000), fail_pages={5})
    _, st = run(tmp_path, api, fetch_until=datetime(2000, 1, 1))
    assert not any("select it again" in w for w in st["warnings"])
    api.fail_pages = set()
    _, st = run(tmp_path, api)
    assert ids(tmp_path) == {str(i) for i in range(1000)}
    assert 2 not in api.pages and 3 not in api.pages


def test_legacy_resume_from_beginning_does_not_read_all(tmp_path):
    # 古い版の初回取得の途中失敗の印。キャッシュには新しい 400 件だけ
    write_cache(tmp_path, [item(i) for i in range(600, 1000)], resume_stop_ts=ss._RESUME_FROM_BEGINNING)
    api = RecentApi(all_items(1000))
    _, st = run(tmp_path, api)
    assert st["complete"] is True
    assert 2 not in api.pages and 3 not in api.pages
    assert ids(tmp_path) == {str(i) for i in range(1000)}


def test_legacy_resume_stop_reads_from_page1_to_stop(tmp_path):
    write_cache(tmp_path, all_items(1000), resume_stop_ts=ts(750))
    api = RecentApi(all_items(1000))
    _, st = run(tmp_path, api)
    assert api.pages == [1, 2, 3]
    assert "resume_stop_ts" not in cache(tmp_path)


def test_ranked_refresh_keeps_resume_ranges(tmp_path):
    ranges = [{"newer_ts": ts(400), "older_ts": ss._RESUME_FROM_BEGINNING}]
    write_cache(tmp_path, all_items(10), resume_ranges=ranges)
    with mock.patch.object(ss, "CACHE_DIR", tmp_path):
        ss._resave_player_scores(tmp_path / f"scoresaber_player_scores_{SID}.json", cache(tmp_path)["scores"])
    c = cache(tmp_path)
    assert c["resume_ranges"] == ranges and c["resume_stop_ts"] == ss._RESUME_FROM_BEGINNING


def test_merge_ranges():
    m = ss._merge_resume_ranges([
        {"newer_ts": ts(10), "older_ts": ts(5)},
        {"newer_ts": ts(7), "older_ts": ts(2)},
        {"newer_ts": None, "older_ts": ts(50)},
    ])
    assert m == [{"newer_ts": None, "older_ts": ts(50)}, {"newer_ts": ts(10), "older_ts": ts(2)}]
