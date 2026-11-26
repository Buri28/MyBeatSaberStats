"""BeatLeader プレイヤースコア取得の「取りこぼした範囲（未回収ページ）」の扱いの回帰テスト。

- page 1 が取れない回：遡り取得（60 日遡りなど）は一覧の先頭から遡りの日時まで、普段の差分取得と Fetch ALL は
  一覧の先頭からキャッシュの最新までの範囲を残す（Fetch ALL でも全ページを読む印は付けない）。
  警告には失敗の理由を添え、1 回だけ出す。
- 印が残っている回：全ページは読まない。
  - page 1 の印（古いキャッシュの [1]）は、普段の差分取得で読んだ範囲だけで埋めて印を消す。
  - 深いページの印は、その範囲（timeset）を含むページだけを取り直す。新しいスコアが増えてページ番号が
    ずれていても、読んだページの timeset で範囲の始まりと終わりを確かめる。
  - 埋めきれないときは印（範囲）を残す。
- メッセージには実際に読んだページ数を出す。
- 旧ドメイン（beatleader.xyz）の URL は使わない。

模擬 session で応答を作り、キャッシュは一時フォルダに向ける（実キャッシュ・実 API には触らない）。
実行: .venv\\Scripts\\python.exe -m pytest test_bl_player_scores_pending.py
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent / "src"))
from mybeatsaberstats.collector import beatleader as bl  # noqa: E402
from mybeatsaberstats.collector import collector as col  # noqa: E402
from mybeatsaberstats import beatleader as bl_top  # noqa: E402
from mybeatsaberstats import http_client as hc  # noqa: E402

PID = "7"


class _Resp:
    headers: dict = {}

    def __init__(self, data, url, status_code=200):
        self._d = data
        self.url = url
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._d


def item(i):
    return {"id": i, "pp": 0.0, "timeset": str(1700000000 + i),
            "leaderboard": {"id": f"L{i}", "difficulty": {"status": 0}}}


class Api:
    """/player/{id}/scores を sortBy=date desc で返す。down=True なら接続できない（SSL の失敗を模す）。"""

    def __init__(self, n_items, per_page=100, down=False, fail_pages=(), play_count=None):
        # 新しい順（timeset 大きい順）
        self.items = [item(i) for i in range(n_items, 0, -1)]
        self.n = n_items
        # /player/{id} の totalPlayCount。実データ（2026-11-25）では type=best の総数より少ない（21285 < 23168）
        self.play_count = play_count
        self.per_page = per_page
        self.down = down
        self.fail_pages = set(fail_pages)
        self.score_pages: list[int] = []
        self.urls: list[str] = []

    def get(self, url, params=None, timeout=None, **kw):
        self.urls.append(url)
        if self.down:
            raise ConnectionError("SSLError: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
        if url.endswith(f"/player/{PID}"):
            pc = len(self.items) if self.play_count is None else self.play_count
            return _Resp({"scoreStats": {"totalPlayCount": pc}}, url)
        assert url.endswith(f"/player/{PID}/scores"), url
        page = int(params["page"])
        self.score_pages.append(page)
        if page in self.fail_pages:
            raise ConnectionError(f"boom page {page}")
        start = (page - 1) * self.per_page
        data = self.items[start:start + self.per_page]
        return _Resp({"metadata": {"total": len(self.items), "itemsPerPage": self.per_page, "page": page},
                      "data": data}, url)

    def add_new(self, count):
        """新しいスコアを先頭に足す（ページ番号がずれる）。"""
        new = [item(i) for i in range(self.n + count, self.n, -1)]
        self.n += count
        self.items = new + self.items


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(bl, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(bl, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(bl, "LOG_PATH", tmp_path / "logs" / "beatleader_api.log")
    monkeypatch.setattr(bl.time, "sleep", lambda *_: None)
    # get_memoized の覚えをテストごとに消す
    hc.forget_memoized(None)
    return tmp_path


def write_cache(tmp, items, failed_pages=(), failed_ranges=None):
    scores = {it["leaderboard"]["id"]: it for it in items}
    payload = {"fetched_at": "2026-11-18T00:00:00Z", "total_play_count": len(scores), "scores": scores,
               "failed_pages": list(failed_pages)}
    if failed_ranges is not None:
        payload["failed_ranges"] = failed_ranges
    (tmp / f"beatleader_player_scores_{PID}.json").write_text(json.dumps(payload), encoding="utf-8")


def read_cache(tmp):
    return json.loads((tmp / f"beatleader_player_scores_{PID}.json").read_text(encoding="utf-8"))


def run(api, **kw):
    warnings: list[str] = []
    status: dict = {"complete": True}
    session = mock.Mock()
    session.get.side_effect = api.get
    out = bl._get_beatleader_player_scores(PID, session, warning_callback=warnings.append, status=status, **kw)
    return out, warnings, status


def test_page1_down_in_diff_mode_does_not_mark_full_refetch(cache_dir):
    api = Api(250)
    write_cache(cache_dir, api.items[:])
    before = read_cache(cache_dir)
    api.down = True
    out, warnings, status = run(api)
    assert len(out) == 250  # 既存のキャッシュを使う
    assert status["complete"] is False
    after = read_cache(cache_dir)
    # 何も読めていないので、スコアと fetched_at は変えない。全ページ取り直しの印は付けず、
    # 一覧の先頭からキャッシュの最新（timeset 250）までだけを取りこぼしとして残す（aitask #412 の指摘 2）
    assert after["scores"] == before["scores"] and after["fetched_at"] == before["fetched_at"]
    assert after["failed_ranges"] == [
        {"page": 1, "last_page": 1, "newer_ts": None, "older_ts": 1700000250, "to_end": False}]
    assert after["failed_pages"] == [1]
    assert len(warnings) == 1, warnings
    assert "page 1 could not be fetched" in warnings[0]
    assert "SSLError" in warnings[0]  # 理由を添える
    assert "refetch only the unfetched range" in warnings[0]
    assert not any("全ページを再取得" in w for w in warnings)
    # 次の回は page 1 だけで印の範囲（キャッシュの最新まで）に届いて印を消す（全ページは読まない）
    api.down = False
    hc.forget_memoized(None)
    _o, _w2, status2 = run(api)
    assert api.score_pages == [1] and status2["complete"] is True
    assert read_cache(cache_dir)["failed_ranges"] == []


def test_page1_down_in_full_fetch_does_not_mark(cache_dir):
    """Fetch ALL で page 1 が取れなくても、次の回に勝手に全ページを読む印は付けない。

    一覧の先頭からキャッシュの最新までは残す（SS の required_stop と同じ。aitask #413 のレビュー）。
    """
    api = Api(250)
    write_cache(cache_dir, api.items[:])
    before = read_cache(cache_dir)
    api.down = True
    _out, warnings, status = run(api, fetch_until=datetime(2000, 1, 1))
    saved = read_cache(cache_dir)
    assert saved["failed_ranges"] == [
        {"page": 1, "last_page": 1, "newer_ts": None, "older_ts": 1700000250, "to_end": False}]
    assert saved["fetched_at"] == before["fetched_at"]
    assert status["complete"] is False
    assert len(warnings) == 1 and "select it again" in warnings[0]
    # 次の普段の差分取得は page 1 だけで印の範囲（キャッシュの最新まで）に届いて消す（全ページは読まない）
    api.down = False
    hc.forget_memoized(None)
    _o, _w2, status2 = run(api)
    assert api.score_pages == [1] and status2["complete"] is True
    assert read_cache(cache_dir)["failed_ranges"] == []


def test_page1_down_in_backfill_marks_top_range_and_next_run_reads_only_that_range(cache_dir):
    """遡り取得（60 日遡りなど）で page 1 が取れなければ、先頭から遡りの日時までを残し、次の回はそこまでだけ読む。"""
    api = Api(2500, play_count=2000)  # 25 ページ
    write_cache(cache_dir, api.items[:])
    boundary_ts = 1700000000 + 2500 - 250  # 3 ページ目の途中
    boundary = datetime.utcfromtimestamp(boundary_ts)
    api.down = True
    _out, warnings, _status = run(api, fetch_until=boundary)
    saved = read_cache(cache_dir)
    assert saved["failed_pages"] == [1]
    assert saved["failed_ranges"] == [
        {"page": 1, "last_page": 1, "newer_ts": None, "older_ts": boundary_ts, "to_end": False}]
    assert len(warnings) == 1 and "refetch only the unfetched range" in warnings[0]
    # 次の回（普段の差分取得）。先頭から境界の 3 ページだけ読み、全 25 ページは読まない
    api.down = False
    hc.forget_memoized(None)
    out, warnings2, status2 = run(api)
    assert api.score_pages == [1, 2, 3]
    assert read_cache(cache_dir)["failed_pages"] == []
    assert status2["complete"] is True
    assert len(out) == 2500
    assert len(warnings2) == 1 and "取り直しました" in warnings2[0] and "読んだページ 3/25: 1-3" in warnings2[0], warnings2


def test_api_urls_use_beatleader_com():
    assert bl.BL_BASE_URL == "https://api.beatleader.com"
    assert bl.BEATLEADER_LEADERBOARDS_URL == "https://api.beatleader.com/leaderboards"
    assert col.BL_BASE_URL == "https://api.beatleader.com"
    assert bl_top.BASE_URL == "https://api.beatleader.com"
    assert "api.beatleader.com" in hc._POLICIES
    src = Path(__file__).parent / "src"
    # 文字列リテラルとして書かれた旧ドメインの URL（説明文・コメントは対象外）
    literal = re.compile(r"""["']https?://[^"'\s]*beatleader\.xyz""")
    offenders = []
    for path in src.rglob("*.py"):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if literal.search(line.split("#", 1)[0]):
                offenders.append(f"{path.name}:{no}: {line.strip()}")
    assert offenders == [], offenders


@pytest.mark.parametrize(("url", "expected"), [
    ("https://cdn.assets.beatleader.xyz/xp_prestiege_p8min.png", "https://cdn.assets.beatleader.com/xp_prestiege_p8min.png"),
    ("https://beatleader.xyz/u/1", "https://beatleader.com/u/1"),
    ("https://api.beatleader.xyz", "https://api.beatleader.com"),
    ("https://cdn.assets.beatleader.com/a.png", "https://cdn.assets.beatleader.com/a.png"),
    ("https://notbeatleader.xyz/a", "https://notbeatleader.xyz/a"),
    ("https://cdn.beatleader.xyzabc/a", "https://cdn.beatleader.xyzabc/a"),
    (None, None),
    ("", ""),
])
def test_normalize_legacy_url(url, expected):
    assert bl_top.normalize_beatleader_url(url) == expected




def ids(items):
    return {it["leaderboard"]["id"] for it in items}


def test_legacy_page1_mark_is_cleared_by_diff_fetch_only(cache_dir):
    """古いキャッシュの印 [1]（範囲なし）は、普段の差分取得で読んだページだけで埋めて消す。"""
    api = Api(2500, play_count=2000)  # 25 ページ
    write_cache(cache_dir, api.items[:], failed_pages=[1])
    api.add_new(5)
    out, warnings, status = run(api)
    # page 1 に新しい 5 件（差分あり）→ page 2 は差分なしで止まる。取り直しのための追加の取得は無い
    assert api.score_pages == [1, 2]
    saved = read_cache(cache_dir)
    assert saved["failed_pages"] == [] and saved["failed_ranges"] == []
    assert status["complete"] is True
    assert len(out) == 2505
    assert len(warnings) == 1, warnings
    assert "前回の未回収ページ [1] を取り直しました" in warnings[0]
    assert "読んだページ 2/26: 1-2。差分取得で読んだページだけで埋まりました" in warnings[0]
    assert "全ページ" not in warnings[0]
    # 次の回は普段どおり page 1 だけ・メッセージなし
    hc.forget_memoized(None)
    api.score_pages.clear()
    _o, w2, _s = run(api)
    assert api.score_pages == [1] and w2 == []


def _deep_failure_then_shift(cache_dir, new_scores=150):
    """遡り取得の回に page 12 だけ取れず、その後に新しいスコアが増えてページ番号がずれた状態を作る。"""
    api = Api(2500, play_count=2000, fail_pages={12})  # 25 ページ
    # キャッシュには page 12 の 100 件（位置 1100〜1199）が無い
    missing = api.items[1100:1200]
    write_cache(cache_dir, api.items[:1100] + api.items[1200:])
    boundary = datetime.utcfromtimestamp(1700000000 + 2500 - 1450)  # page 15 の途中まで遡る
    _out, warnings, status = run(api, fetch_until=boundary)
    assert sorted(set(api.score_pages)) == list(range(1, 16))
    saved = read_cache(cache_dir)
    assert saved["failed_pages"] == [12]
    rng = saved["failed_ranges"][0]
    # 範囲は page 11 の最後〜page 13 の先頭の timeset
    assert rng["newer_ts"] == int(api.items[1099]["timeset"]) and rng["older_ts"] == int(api.items[1200]["timeset"])
    assert status["complete"] is False
    api.fail_pages.clear()
    api.score_pages.clear()
    api.add_new(new_scores)
    hc.forget_memoized(None)
    return api, missing


def test_deep_mark_refetches_only_shifted_pages(cache_dir):
    """深いページの印は、新しいスコアでずれた分を見込んだ前後のページだけを取り直す（全ページは読まない）。"""
    api, missing = _deep_failure_then_shift(cache_dir, new_scores=150)
    out, warnings, status = run(api)
    # 差分取得 page 1〜3（新しい 150 件）＋ 取り直し 13〜14（元の位置 1100〜1199 は 150 件ずれて 1250〜1349）
    assert sorted(set(api.score_pages)) == [1, 2, 3, 13, 14], api.score_pages
    assert len(api.score_pages) == 5  # 同じページを二度取りに行かない
    assert ids(missing) <= ids(out)
    assert len(out) == 2650
    saved = read_cache(cache_dir)
    assert saved["failed_pages"] == [] and saved["failed_ranges"] == []
    assert status["complete"] is True
    assert len(warnings) == 1, warnings
    assert "前回の未回収ページ [12] を取り直しました" in warnings[0]
    assert "読んだページ 5/27: 1-3, 13-14。うち取り直し 13-14" in warnings[0]
    assert "範囲のスコア" in warnings[0] and "キャッシュ 2650 件 / API 2650 件" in warnings[0]


def test_deep_mark_is_recovered_in_retry_pass_without_page1(cache_dir):
    """★統計の取り直し（retry_failed_pages_only）でも、範囲だけを読み、page 1 は読まない。"""
    api, missing = _deep_failure_then_shift(cache_dir, new_scores=30)
    out, warnings, status = run(api, retry_failed_pages_only=True)
    assert 1 not in api.score_pages
    assert len(api.score_pages) <= 3, api.score_pages
    assert ids(missing) <= ids(out)
    assert read_cache(cache_dir)["failed_ranges"] == []
    assert status["complete"] is True
    assert len(warnings) == 1 and "[12] を★統計の取得時に取り直しました" in warnings[0], warnings


def test_deep_mark_unrecoverable_keeps_mark(cache_dir):
    """取り直すページも取れなければ印（範囲）を残し、成功とは言わない。全ページは読まない。"""
    api, missing = _deep_failure_then_shift(cache_dir, new_scores=150)
    api.fail_pages = set(range(10, 26))
    out, warnings, status = run(api)
    assert max(api.score_pages) < 16 and len(set(api.score_pages)) <= 5, api.score_pages
    saved = read_cache(cache_dir)
    assert saved["failed_pages"] == [12]
    assert saved["failed_ranges"][0]["newer_ts"] == int(api.items[1099 + 150]["timeset"])
    assert status["complete"] is False
    assert not ids(missing) <= ids(out)
    assert any("取り直せませんでした" in w and "その範囲だけ取り直します" in w and "取れなかったページ" in w
               for w in warnings), warnings
    assert not any("取り直しました" in w for w in warnings)


def test_partially_recovered_range_is_narrowed(cache_dir):
    """範囲の途中まで読めたら、読めたところまで範囲を狭めて残す。"""
    api, _missing = _deep_failure_then_shift(cache_dir, new_scores=150)
    api.fail_pages = {14}
    _out, _w, status = run(api)
    saved = read_cache(cache_dir)
    rng = saved["failed_ranges"]
    assert len(rng) == 1 and rng[0]["page"] == 14
    # page 13 の最後まで読めた
    assert rng[0]["newer_ts"] == int(api.items[1299]["timeset"])
    assert status["complete"] is False
    api.fail_pages.clear()
    api.score_pages.clear()
    hc.forget_memoized(None)
    _o2, w2, s2 = run(api)
    # 範囲の始まり（page 13 の最後）を含む page 13 から読む
    assert sorted(set(api.score_pages)) == [1, 13, 14], api.score_pages
    assert read_cache(cache_dir)["failed_ranges"] == [] and s2["complete"] is True


def test_legacy_deep_mark_without_range(cache_dir):
    """範囲の無い古いキャッシュの深いページの印は、キャッシュの並びから前後 1 ページ広げた範囲で取り直す。"""
    api = Api(2500, play_count=2000)
    missing = api.items[1100:1200]
    write_cache(cache_dir, api.items[:1100] + api.items[1200:], failed_pages=[12])
    out, warnings, status = run(api)
    # 差分取得の page 1 ＋ 見積もった範囲（前後 1 ページ広げた page 10〜15）
    assert sorted(set(api.score_pages)) == [1, 10, 11, 12, 13, 14, 15], api.score_pages
    assert ids(missing) <= ids(out)
    assert read_cache(cache_dir)["failed_ranges"] == [] and status["complete"] is True
    assert any("[12] を取り直しました" in w for w in warnings), warnings


def test_consecutive_failures_in_backfill_keep_bounded_range(cache_dir):
    """遡り取得で続けて失敗して打ち切ったら、残りは遡りの日時までの範囲として残し、次の回もそこまでだけ読む。"""
    api = Api(2500, play_count=2000, fail_pages={5, 6})
    write_cache(cache_dir, api.items[:])
    boundary_ts = 1700000000 + 2500 - 950  # page 10 の途中
    _out, _w, status = run(api, fetch_until=datetime.utcfromtimestamp(boundary_ts))
    assert sorted(set(api.score_pages)) == [1, 2, 3, 4, 5, 6]
    rng = read_cache(cache_dir)["failed_ranges"]
    assert rng == [{"page": 5, "last_page": 6, "newer_ts": int(api.items[399]["timeset"]),
                    "older_ts": boundary_ts, "to_end": False}]
    assert status["complete"] is False
    api.fail_pages.clear()
    api.score_pages.clear()
    hc.forget_memoized(None)
    _o, w2, s2 = run(api)
    # 範囲の始まり（page 4 の最後）を含む page 4 から、遡りの日時を含む page 10 まで
    assert sorted(set(api.score_pages)) == [1, 4, 5, 6, 7, 8, 9, 10], api.score_pages
    assert read_cache(cache_dir)["failed_ranges"] == [] and s2["complete"] is True


def test_full_fetch_stopped_by_failures_does_not_schedule_all_remaining_pages(cache_dir):
    """Fetch ALL が途中で止まっても、次の回に残り全部（全ページ）を読みには行かない。"""
    api = Api(2500, play_count=2000, fail_pages={5, 6})
    write_cache(cache_dir, api.items[:])
    _out, _w, status = run(api, fetch_until=datetime(2000, 1, 1))
    assert status["complete"] is False  # 全件取得済みとしない
    rng = read_cache(cache_dir)["failed_ranges"]
    assert len(rng) == 1 and rng[0]["older_ts"] is None and rng[0]["to_end"] is False
    api.fail_pages.clear()
    api.score_pages.clear()
    hc.forget_memoized(None)
    _o, _w2, s2 = run(api)
    # page 5〜6 を読み、差分の無いページで止まる
    assert len(set(api.score_pages)) <= 4 and max(api.score_pages) <= 7, api.score_pages
    assert read_cache(cache_dir)["failed_ranges"] == [] and s2["complete"] is True


def test_full_fetch_404_midway_is_not_complete(cache_dir):
    """Fetch ALL が 404 で途中で止まったら、全件取得済みにしない。"""
    api = Api(250)
    real_get = api.get

    def get(url, params=None, **kw):
        resp = real_get(url, params=params, **kw)
        if params and params.get("page") == "2":
            resp.status_code = 404
        return resp

    api.get = get
    write_cache(cache_dir, api.items[:])
    _out, _warnings, status = run(api, fetch_until=datetime(2000, 1, 1))
    assert status["complete"] is False


def test_refresh_ranked_scores_keeps_ranges(cache_dir):
    """スコアを書き直す別の処理（ランク譜面のスコアの取り直し）で、範囲が消えない。"""
    rng = [{"page": 12, "last_page": 12, "newer_ts": 5, "older_ts": 3, "to_end": False}]
    write_cache(cache_dir, [item(1)], failed_pages=[12], failed_ranges=rng)
    path = cache_dir / f"beatleader_player_scores_{PID}.json"
    bl._save_cached_player_scores(path, {"L1": item(1)}, failed_ranges=bl._load_cached_player_score_failed_ranges(path))
    saved = read_cache(cache_dir)
    assert saved["failed_ranges"] == rng and saved["failed_pages"] == [12]


def test_legacy_long_run_of_marks_does_not_read_all_pages(cache_dir):
    """古い版が付けた「残りのページ全部」の印（[5..25] など）でも、全ページは読まない（差分の無いページで止まる）。"""
    api = Api(2500, play_count=2000)  # 25 ページ
    write_cache(cache_dir, api.items[:], failed_pages=list(range(5, 26)))
    _out, warnings, status = run(api)
    assert len(set(api.score_pages)) <= 4, api.score_pages
    assert read_cache(cache_dir)["failed_ranges"] == [] and status["complete"] is True
    assert any("取り直しました" in w for w in warnings), warnings


def test_legacy_marks_from_page1_to_end_do_not_read_all_pages(cache_dir):
    api = Api(2500, play_count=2000)
    write_cache(cache_dir, api.items[:], failed_pages=list(range(1, 26)))
    _out, _warnings, status = run(api)
    assert len(set(api.score_pages)) <= 2, api.score_pages
    assert read_cache(cache_dir)["failed_ranges"] == [] and status["complete"] is True


def test_estimate_far_off_by_stale_cache_scores_still_recovers(cache_dir):
    """API に無い古いスコアが範囲より新しい側に 400 件キャッシュに残っていて見積もりが 4 ページずれても、範囲を埋めて印を消す。"""
    api = Api(2500, play_count=2000)
    missing = api.items[1100:1200]
    # 範囲より新しい timeset の、API に無いスコア（消えた譜面など）
    stale = [{"id": f"x{i}", "pp": 0.0, "timeset": str(1700000000 + 2000 + (i % 400) // 4 * 4 + 1),
              "leaderboard": {"id": f"X{i}", "difficulty": {"status": 0}}} for i in range(400)]
    write_cache(cache_dir, api.items[:1100] + api.items[1200:] + stale, failed_ranges=[
        {"page": 12, "last_page": 12, "newer_ts": int(api.items[1099]["timeset"]),
         "older_ts": int(api.items[1200]["timeset"]), "to_end": False}])
    out, _warnings, status = run(api)
    assert ids(missing) <= ids(out)
    assert read_cache(cache_dir)["failed_ranges"] == [] and status["complete"] is True
    assert len(set(api.score_pages)) <= 8, api.score_pages


def test_full_fetch_clears_ranges_even_when_estimate_is_off(cache_dir):
    """Fetch ALL で全ページを読んだ回は、見積もりがずれていても（読んだページだけで）範囲を消す。"""
    api = Api(2500, play_count=2000)
    stale = [{"id": f"x{i}", "pp": 0.0, "timeset": str(1700000000 + 2400 + i // 10),
              "leaderboard": {"id": f"X{i}", "difficulty": {"status": 0}}} for i in range(900)]
    write_cache(cache_dir, api.items[:1100] + api.items[1200:] + stale, failed_ranges=[
        {"page": 12, "last_page": 12, "newer_ts": int(api.items[1099]["timeset"]),
         "older_ts": int(api.items[1200]["timeset"]), "to_end": False}])
    _out, _warnings, status = run(api, fetch_until=datetime(2000, 1, 1))
    assert len(api.score_pages) == 25  # 利用者が選んだ全件取得のぶんだけ（取り直しで同じページを二度読まない）
    assert read_cache(cache_dir)["failed_ranges"] == [] and status["complete"] is True


def test_player_stats_failure_does_not_read_all_pages(cache_dir):
    """/player/{id}（totalPlayCount）が取れない回も、差分取得は差分の無いページで止まる。"""
    api = Api(2500, play_count=2000)
    write_cache(cache_dir, api.items[:])
    real_get = api.get

    def get(url, params=None, **kw):
        if url.endswith(f"/player/{PID}"):
            raise ConnectionError("SSLError")
        return real_get(url, params=params, **kw)

    api.get = get
    _out, _w, status = run(api)
    assert api.score_pages == [1]
    assert status["complete"] is True


def test_retry_pass_failure_keeps_marks_and_stops_after_two(cache_dir):
    """★統計の取り直しで取れなければ印を残し、2 つ続けて失敗したら残りは取りに行かない。"""
    api = Api(2500, play_count=2000, fail_pages=set(range(2, 26)))
    rngs = [{"page": p, "last_page": p, "newer_ts": int(api.items[(p - 1) * 100 - 1]["timeset"]),
             "older_ts": int(api.items[p * 100]["timeset"]), "to_end": False} for p in (5, 10, 15)]
    write_cache(cache_dir, api.items[:], failed_ranges=rngs)
    _out, warnings, status = run(api, retry_failed_pages_only=True)
    assert sorted(set(api.score_pages)) == [4, 9], api.score_pages  # 3 つ目（page 15 付近）は取りに行かない
    saved = read_cache(cache_dir)
    assert [r["page"] for r in saved["failed_ranges"]] == [5, 10, 15]
    assert status["complete"] is False
    assert len(warnings) == 2 and all("could not be recovered" in w for w in warnings), warnings


def test_empty_page_midway_does_not_clear_range(cache_dir):
    """取り直しの途中、最後のページより前で空のページが返ったら、印を消さない。"""
    api = Api(2500, play_count=2000)
    write_cache(cache_dir, api.items[:1100] + api.items[1200:], failed_ranges=[
        {"page": 12, "last_page": 12, "newer_ts": int(api.items[1099]["timeset"]),
         "older_ts": int(api.items[1200]["timeset"]), "to_end": False}])
    real_get = api.get

    def get(url, params=None, **kw):
        resp = real_get(url, params=params, **kw)
        if params and params.get("page") == "12":
            resp._d = dict(resp._d, data=[])
        return resp

    api.get = get
    _out, _w, status = run(api)
    assert [r["page"] for r in read_cache(cache_dir)["failed_ranges"]] == [12]
    assert status["complete"] is False


def test_page1_failure_in_backfill_keeps_existing_top_range(cache_dir):
    """遡り取得で page 1 が取れないとき、既にある先頭からの範囲を捨てない。"""
    api = Api(2500, play_count=2000)
    existing = {"page": 1, "last_page": 1, "newer_ts": None, "older_ts": None, "to_end": False}
    write_cache(cache_dir, api.items[:], failed_ranges=[existing])
    api.down = True
    boundary_ts = 1700000000 + 2500 - 250
    run(api, fetch_until=datetime.utcfromtimestamp(boundary_ts))
    rngs = read_cache(cache_dir)["failed_ranges"]
    assert existing in rngs
    assert {"page": 1, "last_page": 1, "newer_ts": None, "older_ts": boundary_ts, "to_end": False} in rngs
