"""ss_reweights（wiki の月例リウェイトで SS ランク譜面キャッシュの★を更新）のテスト。

wiki の HTML は 2026-11-21 に 1 回だけ取得して tests/fixtures/ss_reweights に保存したものを使う。SS API はモック。
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mybeatsaberstats import ss_reweights as sr

FIXTURES = Path(__file__).parent / "tests" / "fixtures" / "ss_reweights"
INDEX_HTML = (FIXTURES / "reweights_index.html").read_text(encoding="utf-8")
NOV_HTML = (FIXTURES / "November-2026.html").read_text(encoding="utf-8")
NOV_URL = "https://wiki.scoresaber.com/ranking/reweights/2026/November-2026.html"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


class _Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, pages=None, infos=None):
        self.pages = dict(pages or {})
        self.infos = dict(infos or {})  # (hash, difficulty) -> _Resp
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        if url in self.pages:
            value = self.pages[url]
            if isinstance(value, Exception):
                raise value
            return value if isinstance(value, _Resp) else _Resp(200, value)
        if "/api/leaderboard/by-hash/" in url:
            song_hash = url.split("/by-hash/")[1].split("/")[0]
            return self.infos.get((song_hash, params["difficulty"]), _Resp(404))
        return _Resp(404)


def _lb(lb_id, name, mapper, diff, stars, sub="", song_hash=None):
    return {
        "id": lb_id, "songHash": song_hash or f"H{lb_id}", "songName": name, "songSubName": sub,
        "levelAuthorName": mapper, "stars": stars, "maxPP": stars * 42.0,
        "difficulty": {"leaderboardId": lb_id, "difficulty": diff, "gameMode": "SoloStandard"},
        "rankedDate": "2026-01-01T00:00:00Z",
    }


def _setup(tmp_path, leaderboards, complete="2026-11-10T00:00:00Z", state=None):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "scoresaber_ranked_maps.json").write_text(json.dumps({
        "fetched_at": "2026-11-20T11:35:42Z", "leaderboards": {str(lb["id"]): lb for lb in leaderboards},
        "max_pages": 3, "total_maps": len(leaderboards),
    }), encoding="utf-8")
    traversal = {"scoresaber": complete, "scoresaber_last_ok": True}
    if complete:
        traversal["scoresaber_complete"] = complete
    (cache / "ranked_full_traversal.json").write_text(json.dumps(traversal), encoding="utf-8")
    if state is not None:
        (cache / sr.STATE_FILE_NAME).write_text(json.dumps(state), encoding="utf-8")
    return cache


def _read(cache, name):
    return json.loads((cache / name).read_text(encoding="utf-8"))


OCT_URL = "https://wiki.scoresaber.com/ranking/reweights/2026/October-2026.html"
SEP_URL = "https://wiki.scoresaber.com/ranking/reweights/2026/September-2026.html"
# 10 月・9 月のページは取得していない（wiki のアクセスは 2 回まで）。11 月のページの日付だけ変えて代わりにする
OCT_HTML = NOV_HTML.replace("15 November 2026", "11 October 2026")
SEP_HTML = NOV_HTML.replace("15 November 2026", "13 September 2026")


def _nov_session(**kw):
    return FakeSession(pages={sr.REWEIGHTS_INDEX_URL: INDEX_HTML, OCT_URL: OCT_HTML, NOV_URL: NOV_HTML}, **kw)


# 11 月のページの 18 行のうち、全部の行に合う譜面を作る（★は「新」= 反映済み）
def _all_reflected_maps():
    parsed = sr.parse_page(NOV_HTML)
    diffs = {"Expert+": 9, "Expert": 7, "Hard": 5, "Normal": 3, "Easy": 1}
    return [_lb(1000 + i, r["name"], r["mapper"], diffs[r["difficulty"]], r["new"]) for i, r in enumerate(parsed["rows"])]


# ------------------------------------------------------------ HTML の読み取り

def test_parse_index_reads_month_links():
    pages = sr.parse_index(INDEX_HTML)
    slugs = [p["slug"] for p in pages]
    assert "2026/November-2026" in slugs
    nov = next(p for p in pages if p["slug"] == "2026/November-2026")
    assert (nov["year"], nov["month"], nov["url"], nov["global"]) == (2026, 11, NOV_URL, False)
    glob = next(p for p in pages if p["slug"] == "2024/Global-August-2024")
    assert glob["global"] is True and glob["month"] == 8
    assert [(p["year"], p["month"]) for p in pages] == sorted((p["year"], p["month"]) for p in pages)


def test_parse_november_page():
    parsed = sr.parse_page(NOV_HTML)
    assert parsed["date"].isoformat() == "2026-11-15"
    assert parsed["global"] is False
    assert len(parsed["rows"]) == 18
    last = parsed["rows"][-1]
    assert last == {"name": "Cirno's Perfect Math Class", "difficulty": "Normal", "mapper": "Cosmo", "old": 4.05, "new": 5.85}
    assert parsed["rows"][0]["mapper"] == "VoltageO & Bitz"


def test_parse_errors_on_changed_html():
    with pytest.raises(sr.ReweightParseError):
        sr.parse_page("<html><h1>Monthly Reweight - 15 November 2026</h1><p>no table</p></html>")
    with pytest.raises(sr.ReweightParseError):
        sr.parse_page(NOV_HTML.replace("Monthly Reweight - 15 November 2026", "Something else"))
    with pytest.raises(sr.ReweightParseError):
        sr.parse_index("<html>nothing</html>")


# ------------------------------------------------------------ 行の判定

def test_classify_new_old_neither_and_ambiguous():
    row = {"name": "Song", "difficulty": "Expert+", "mapper": "A & B", "old": 10.0, "new": 11.0}
    assert sr.classify_row(row, {"1": _lb(1, "Song", "A & B", 9, 11.0)}) == ("reflected", "1")
    assert sr.classify_row(row, {"1": _lb(1, "Song", "B & A", 9, 10.0)}) == ("refetch", "1")
    assert sr.classify_row(row, {"1": _lb(1, "Song", "A & B", 9, 12.5)}) == ("flag", "1")
    # 見つからない（難易度違い・譜面作者違い）
    assert sr.classify_row(row, {"1": _lb(1, "Song", "A & B", 7, 11.0)}) == ("flag", None)
    assert sr.classify_row(row, {"1": _lb(1, "Song", "C", 9, 11.0)}) == ("flag", None)
    # 1 つに決まらない
    two = {"1": _lb(1, "Song", "A & B", 9, 10.0), "2": _lb(2, "Song", "A & B", 9, 10.0)}
    assert sr.classify_row(row, two) == ("flag", None)
    # 曲名 + サブ名でも合う
    row2 = dict(row, name="Song (Remix)")
    assert sr.classify_row(row2, {"1": _lb(1, "Song", "A & B", 9, 11.0, sub="(Remix)")}) == ("reflected", "1")


# ------------------------------------------------------------ 本体

def test_refetch_updates_only_that_map(tmp_path):
    maps = _all_reflected_maps()
    target = next(m for m in maps if m["songName"] == "STAXX")  # Expert ⭐ 7.97 → 8.22
    target["stars"] = 7.97
    cache = _setup(tmp_path, maps)
    session = _nov_session(infos={
        (target["songHash"], 7): _Resp(200, payload={"id": target["id"], "stars": 8.22, "maxPP": 345.0}),
    })
    summary = sr.apply_scoresaber_reweights(session, cache, now=NOW)
    assert summary["status"] == "done"
    page = summary["pages"][-1]
    assert (page["reflected"], page["refetched"], page["flagged"]) == (17, 1, 0)
    saved = _read(cache, "scoresaber_ranked_maps.json")
    assert saved["leaderboards"][str(target["id"])]["stars"] == 8.22
    assert saved["leaderboards"][str(target["id"])]["maxPP"] == 345.0
    assert saved["fetched_at"] == "2026-11-20T11:35:42Z"  # 差分取得の窓は進めない
    assert "scoresaber_stars_drift" not in _read(cache, "ranked_full_traversal.json")
    # SS API は取り直した 1 譜面だけ
    api_calls = [c for c in session.calls if "scoresaber.com/api" in c[0]]
    assert api_calls == [(f"https://scoresaber.com/api/leaderboard/by-hash/{target['songHash']}/info",
                          {"difficulty": 7, "gameMode": "SoloStandard"})]
    assert "2026/November-2026" in _read(cache, sr.STATE_FILE_NAME)["processed"]


def test_all_reflected_does_nothing_to_cache(tmp_path):
    cache = _setup(tmp_path, _all_reflected_maps())
    before = (cache / "scoresaber_ranked_maps.json").read_text(encoding="utf-8")
    session = _nov_session()
    summary = sr.apply_scoresaber_reweights(session, cache, now=NOW)
    assert summary["pages"][-1]["reflected"] == 18
    assert (cache / "scoresaber_ranked_maps.json").read_text(encoding="utf-8") == before
    assert not [c for c in session.calls if "scoresaber.com/api" in c[0]]


def test_neither_star_flags_drift(tmp_path):
    maps = _all_reflected_maps()
    maps[0]["stars"] = 13.0  # Anomaly E+ は 14.25 → 14.2。どちらでもない
    cache = _setup(tmp_path, maps)
    summary = sr.apply_scoresaber_reweights(_nov_session(), cache, now=NOW)
    assert summary["pages"][-1]["flagged"] == 1
    assert _read(cache, "ranked_full_traversal.json")["scoresaber_stars_drift"] is True
    assert _read(cache, "scoresaber_ranked_maps.json")["leaderboards"][str(maps[0]["id"])]["stars"] == 13.0
    assert "2026/November-2026" in _read(cache, sr.STATE_FILE_NAME)["processed"]


def test_ambiguous_flags_drift(tmp_path):
    maps = _all_reflected_maps()
    dup = dict(maps[0], id=9999, songHash="DUP")
    cache = _setup(tmp_path, maps + [dup])
    summary = sr.apply_scoresaber_reweights(_nov_session(), cache, now=NOW)
    assert summary["pages"][-1]["flagged"] == 1
    assert _read(cache, "ranked_full_traversal.json")["scoresaber_stars_drift"] is True


def test_not_processed_twice(tmp_path):
    cache = _setup(tmp_path, _all_reflected_maps())
    sr.apply_scoresaber_reweights(_nov_session(), cache, now=NOW)
    session = _nov_session()
    summary = sr.apply_scoresaber_reweights(session, cache, now=NOW + timedelta(days=2))
    assert summary["status"] == "no_new_pages"
    assert [c[0] for c in session.calls] == [sr.REWEIGHTS_INDEX_URL]  # 一覧だけ読み、11 月は読まない


def test_index_read_at_most_once_a_day(tmp_path):
    cache = _setup(tmp_path, _all_reflected_maps())
    sr.apply_scoresaber_reweights(_nov_session(), cache, now=NOW)
    session = _nov_session()
    summary = sr.apply_scoresaber_reweights(session, cache, now=NOW + timedelta(hours=23))
    assert summary["status"] == "checked_recently"
    assert session.calls == []


def test_index_failure_does_nothing_and_waits_a_day(tmp_path, capsys):
    cache = _setup(tmp_path, _all_reflected_maps())
    before = (cache / "scoresaber_ranked_maps.json").read_text(encoding="utf-8")
    session = FakeSession(pages={sr.REWEIGHTS_INDEX_URL: _Resp(503)})
    assert sr.apply_scoresaber_reweights(session, cache, now=NOW)["status"] == "index_failed"
    assert capsys.readouterr().out.count("SS reweight:") == 1
    assert (cache / "scoresaber_ranked_maps.json").read_text(encoding="utf-8") == before
    assert "processed" not in _read(cache, sr.STATE_FILE_NAME)
    # 1 日以内は読み直さない
    session2 = _nov_session()
    assert sr.apply_scoresaber_reweights(session2, cache, now=NOW + timedelta(hours=1))["status"] == "checked_recently"
    assert session2.calls == []
    # 1 日経てば読み直す
    assert sr.apply_scoresaber_reweights(_nov_session(), cache, now=NOW + timedelta(hours=25))["status"] == "done"


@pytest.mark.parametrize("page_value", [_Resp(404), ConnectionError("boom"), "<html><h1>Monthly Reweight - 15 November 2026</h1></html>"])
def test_page_failure_is_not_marked_processed(tmp_path, page_value):
    cache = _setup(tmp_path, _all_reflected_maps())
    session = FakeSession(pages={sr.REWEIGHTS_INDEX_URL: INDEX_HTML, OCT_URL: OCT_HTML, NOV_URL: page_value})
    assert sr.apply_scoresaber_reweights(session, cache, now=NOW)["status"] == "page_failed"
    state = _read(cache, sr.STATE_FILE_NAME)
    assert "2026/November-2026" not in (state.get("processed") or {})
    assert "scoresaber_stars_drift" not in _read(cache, "ranked_full_traversal.json")


def test_api_failure_leaves_cache_and_page_unprocessed(tmp_path):
    maps = _all_reflected_maps()
    target = next(m for m in maps if m["songName"] == "STAXX")
    target["stars"] = 7.97
    cache = _setup(tmp_path, maps)
    before = (cache / "scoresaber_ranked_maps.json").read_text(encoding="utf-8")
    session = _nov_session(infos={(target["songHash"], 7): _Resp(429)})
    assert sr.apply_scoresaber_reweights(session, cache, now=NOW)["status"] == "page_failed"
    assert (cache / "scoresaber_ranked_maps.json").read_text(encoding="utf-8") == before
    assert "2026/November-2026" not in (_read(cache, sr.STATE_FILE_NAME).get("processed") or {})


def test_api_returns_other_map_flags(tmp_path):
    maps = _all_reflected_maps()
    target = next(m for m in maps if m["songName"] == "STAXX")
    target["stars"] = 7.97
    cache = _setup(tmp_path, maps)
    session = _nov_session(infos={(target["songHash"], 7): _Resp(200, payload={"id": 1, "stars": 8.22})})
    summary = sr.apply_scoresaber_reweights(session, cache, now=NOW)
    assert summary["pages"][-1]["flagged"] == 1
    assert _read(cache, "scoresaber_ranked_maps.json")["leaderboards"][str(target["id"])]["stars"] == 7.97
    assert _read(cache, "ranked_full_traversal.json")["scoresaber_stars_drift"] is True


def test_only_pages_after_full_traversal(tmp_path):
    # 全件取得が 11/18 → 11/15 のリウェイトは入っている。読んでも何もせず処理済みにする。前月（10 月）までは読み、9 月以前は読まない
    maps = _all_reflected_maps()
    maps[0]["stars"] = 1.0  # 処理すれば印が付くはずの状態
    cache = _setup(tmp_path, maps, complete="2026-11-18T12:26:37Z")
    session = _nov_session()
    summary = sr.apply_scoresaber_reweights(session, cache, now=NOW)
    assert [c[0] for c in session.calls] == [sr.REWEIGHTS_INDEX_URL, OCT_URL, NOV_URL]
    assert [p["skipped"] for p in summary["pages"]] == ["before_full_traversal"] * 2
    assert "scoresaber_stars_drift" not in _read(cache, "ranked_full_traversal.json")
    assert {"2026/October-2026", "2026/November-2026"} <= set(_read(cache, sr.STATE_FILE_NAME)["processed"])
    # 全件取得が 10/20 なら、9 月（読むが前）・10 月（前）・11 月（対象）
    (tmp_path / "b").mkdir()
    cache2 = _setup(tmp_path / "b", _all_reflected_maps(), complete="2026-10-20T00:00:00Z")
    session2 = FakeSession(pages={sr.REWEIGHTS_INDEX_URL: INDEX_HTML, SEP_URL: SEP_HTML, OCT_URL: OCT_HTML, NOV_URL: NOV_HTML})
    summary2 = sr.apply_scoresaber_reweights(session2, cache2, now=NOW)
    assert [c[0] for c in session2.calls] == [sr.REWEIGHTS_INDEX_URL, SEP_URL, OCT_URL, NOV_URL]
    assert [p.get("skipped") for p in summary2["pages"]] == ["before_full_traversal", "before_full_traversal", None]


@pytest.mark.parametrize("complete, processed", [
    ("2026-11-15T23:00:00Z", True),   # 同じ日
    ("2026-11-16T05:00:00Z", True),   # 翌日（見出しの日付のタイムゾーンが分からないので 1 日の余裕）
    ("2026-11-17T00:00:00Z", False),  # 2 日後は全件取得より前とみなす
])
def test_page_date_boundary(tmp_path, complete, processed):
    maps = _all_reflected_maps()
    maps[0]["stars"] = 1.0  # 照合すれば印が付く
    cache = _setup(tmp_path, maps, complete=complete)
    summary = sr.apply_scoresaber_reweights(_nov_session(), cache, now=NOW)
    nov = next(p for p in summary["pages"] if p["slug"] == "2026/November-2026")
    assert ("skipped" not in nov) is processed
    assert bool(_read(cache, "ranked_full_traversal.json").get("scoresaber_stars_drift")) is processed


def test_api_still_old_is_not_processed(tmp_path):
    # SS にまだ反映されていない（取り直しても「旧」）。書き換えず、処理済みにしないで翌日読み直す
    maps = _all_reflected_maps()
    target = next(m for m in maps if m["songName"] == "STAXX")
    target["stars"] = 7.97
    cache = _setup(tmp_path, maps)
    before = (cache / "scoresaber_ranked_maps.json").read_text(encoding="utf-8")
    session = _nov_session(infos={(target["songHash"], 7): _Resp(200, payload={"id": target["id"], "stars": 7.97})})
    summary = sr.apply_scoresaber_reweights(session, cache, now=NOW)
    assert summary["status"] == "pending"
    assert summary["pages"][-1]["pending"] == 1
    assert (cache / "scoresaber_ranked_maps.json").read_text(encoding="utf-8") == before
    assert "2026/November-2026" not in _read(cache, sr.STATE_FILE_NAME)["processed"]
    assert "scoresaber_stars_drift" not in _read(cache, "ranked_full_traversal.json")
    # 翌日、SS に反映されていれば書き換えて処理済みにする
    session2 = _nov_session(infos={(target["songHash"], 7): _Resp(200, payload={"id": target["id"], "stars": 8.22})})
    summary2 = sr.apply_scoresaber_reweights(session2, cache, now=NOW + timedelta(days=1))
    assert summary2["status"] == "done"
    assert _read(cache, "scoresaber_ranked_maps.json")["leaderboards"][str(target["id"])]["stars"] == 8.22
    assert "2026/November-2026" in _read(cache, sr.STATE_FILE_NAME)["processed"]


def test_api_returns_third_value_writes_and_flags(tmp_path):
    maps = _all_reflected_maps()
    target = next(m for m in maps if m["songName"] == "STAXX")
    target["stars"] = 7.97
    cache = _setup(tmp_path, maps)
    session = _nov_session(infos={(target["songHash"], 7): _Resp(200, payload={"id": target["id"], "stars": 8.5})})
    summary = sr.apply_scoresaber_reweights(session, cache, now=NOW)
    assert summary["pages"][-1]["flagged"] == 1
    assert _read(cache, "scoresaber_ranked_maps.json")["leaderboards"][str(target["id"])]["stars"] == 8.5
    assert _read(cache, "ranked_full_traversal.json")["scoresaber_stars_drift"] is True


def test_second_page_failure_keeps_first_processed(tmp_path):
    cache = _setup(tmp_path, _all_reflected_maps())
    session = FakeSession(pages={sr.REWEIGHTS_INDEX_URL: INDEX_HTML, OCT_URL: OCT_HTML, NOV_URL: _Resp(500)})
    assert sr.apply_scoresaber_reweights(session, cache, now=NOW)["status"] == "page_failed"
    processed = _read(cache, sr.STATE_FILE_NAME)["processed"]
    assert "2026/October-2026" in processed and "2026/November-2026" not in processed
    # 翌日は 11 月だけ読む
    session2 = _nov_session()
    assert sr.apply_scoresaber_reweights(session2, cache, now=NOW + timedelta(days=1))["status"] == "done"
    assert [c[0] for c in session2.calls] == [sr.REWEIGHTS_INDEX_URL, NOV_URL]


def test_global_link_flags_without_reading_page(tmp_path):
    cache = _setup(tmp_path, _all_reflected_maps())
    glob_url = "https://wiki.scoresaber.com/ranking/reweights/2026/Global-November-2026.html"
    index = INDEX_HTML.replace('href="/ranking/reweights/2026/November-2026"',
                               'href="/ranking/reweights/2026/November-2026"></a><a href="/ranking/reweights/2026/Global-November-2026"')
    session = FakeSession(pages={sr.REWEIGHTS_INDEX_URL: index, OCT_URL: OCT_HTML, NOV_URL: NOV_HTML, glob_url: _Resp(404)})
    summary = sr.apply_scoresaber_reweights(session, cache, now=NOW)
    assert summary["status"] == "done"
    assert glob_url not in [c[0] for c in session.calls]
    assert _read(cache, "ranked_full_traversal.json")["scoresaber_stars_drift"] is True
    assert "2026/Global-November-2026" in _read(cache, sr.STATE_FILE_NAME)["processed"]


def test_page_without_charset_is_read_as_utf8(tmp_path):
    # requests は charset の無い text/html を ISO-8859-1 で読む。content を UTF-8 で読むこと
    class Latin1Resp(_Resp):
        def __init__(self, text):
            super().__init__(200, text.encode("utf-8").decode("latin-1"))
            self.content = text.encode("utf-8")
    cache = _setup(tmp_path, _all_reflected_maps())
    session = FakeSession(pages={sr.REWEIGHTS_INDEX_URL: Latin1Resp(INDEX_HTML), OCT_URL: Latin1Resp(OCT_HTML),
                                 NOV_URL: Latin1Resp(NOV_HTML)})
    summary = sr.apply_scoresaber_reweights(session, cache, now=NOW)
    assert summary["pages"][-1]["reflected"] == 18


@pytest.mark.parametrize("where", ["page", "api"])
def test_cancel_during_page_or_api_is_not_swallowed(tmp_path, where):
    maps = _all_reflected_maps()
    target = next(m for m in maps if m["songName"] == "STAXX")
    target["stars"] = 7.97
    cache = _setup(tmp_path, maps)
    session = _nov_session()
    if where == "page":
        session.pages[NOV_URL] = RuntimeError("SNAPSHOT_CANCELLED")
    else:
        class _Raising(dict):
            def get(self, *a, **k):
                raise RuntimeError("SNAPSHOT_CANCELLED")
        session.infos = _Raising()
    with pytest.raises(RuntimeError, match="SNAPSHOT_CANCELLED"):
        sr.apply_scoresaber_reweights(session, cache, now=NOW)
    assert "2026/November-2026" not in (_read(cache, sr.STATE_FILE_NAME).get("processed") or {})


def test_no_full_traversal_skips_everything(tmp_path):
    cache = _setup(tmp_path, _all_reflected_maps(), complete=None)
    session = _nov_session()
    assert sr.apply_scoresaber_reweights(session, cache, now=NOW)["status"] == "no_full_traversal"
    assert session.calls == []


def test_global_page_flags_drift(tmp_path):
    cache = _setup(tmp_path, _all_reflected_maps())
    glob_html = NOV_HTML.replace("Monthly Reweight - 15 November 2026", "Global Reweight - 15 November 2026")
    session = FakeSession(pages={sr.REWEIGHTS_INDEX_URL: INDEX_HTML, OCT_URL: OCT_HTML, NOV_URL: glob_html})
    summary = sr.apply_scoresaber_reweights(session, cache, now=NOW)
    assert summary["pages"][-1]["skipped"] == "global_reweight"
    assert _read(cache, "ranked_full_traversal.json")["scoresaber_stars_drift"] is True
    assert not [c for c in session.calls if "scoresaber.com/api" in c[0]]


def test_cancel_is_not_swallowed(tmp_path):
    cache = _setup(tmp_path, _all_reflected_maps())
    session = FakeSession(pages={sr.REWEIGHTS_INDEX_URL: RuntimeError("SNAPSHOT_CANCELLED")})
    with pytest.raises(RuntimeError, match="SNAPSHOT_CANCELLED"):
        sr.apply_scoresaber_reweights(session, cache, now=NOW)
