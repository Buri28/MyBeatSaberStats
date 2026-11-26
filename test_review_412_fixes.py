"""aitask #412 のコードレビューで見つかった、スコア取得まわりの指摘の回帰テスト。

1. 「Fetch from:」にキャッシュの最新スコアより後の日時を入れても、その間のスコアを読む（SS・BL）。
2. BL の普段の差分取得で page 1 が取れなかった回は、一覧の先頭からキャッシュの最新までを取りこぼしとして残す。
3. ★同期の全件取り直しが途中で失敗したら、古いスコアの★でランク譜面キャッシュを書き戻さない（SS・BL）。
4. 「Fetch from:」の日時は PC のローカル時刻として扱い、UTC に直してから比べる。
5. ★同期で検索しても返らなかった譜面は確認済みにせず、待ち時間の後に探し直す（SS・BL）。
6. BL：失敗続きで止まった回は前回の取りこぼしを取りに行かない。曲ごとの★同期は 429 で止める。
7. SS のスコアのページ取得は 5xx・タイムアウトのときも 3 回まで試す。

模擬 session で応答を作り、キャッシュは一時フォルダに向ける（実キャッシュ・実 API には触らない）。
既存のテスト（test_ss_player_scores_resume / test_bl_player_scores_pending / test_star_sync）の模擬 API を使う。
実行: .venv\\Scripts\\python.exe -m pytest test_review_412_fixes.py
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent / "src"))
from mybeatsaberstats.collector import beatleader as bl  # noqa: E402
from mybeatsaberstats.collector import collector as col  # noqa: E402
from mybeatsaberstats.collector import scoresaber as ss  # noqa: E402
from mybeatsaberstats import http_client as hc  # noqa: E402
from mybeatsaberstats import ranked_sync as rs  # noqa: E402

import test_bl_player_scores_pending as B  # noqa: E402
import test_ss_player_scores_resume as S  # noqa: E402
import test_star_sync as Y  # noqa: E402

cache_dir = B.cache_dir  # BL の一時キャッシュ（fixture）

JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------- 1. 遡りの日時がキャッシュの最新より新しい

def test_ss_fetch_from_newer_than_cache_reads_down_to_cache_latest(tmp_path):
    S.write_cache(tmp_path, S.all_items(1000))  # キャッシュは timeSet 0〜999
    api = S.RecentApi(S.all_items(1500))  # API にはさらに 1000〜1499 の 500 件
    # 新しい方から 50 件目（timeSet 1450）を指定
    _out, st = S.run(tmp_path, api, fetch_until=S.BASE + timedelta(hours=1450))
    assert st["complete"] is True
    assert S.ids(tmp_path) == {str(i) for i in range(1500)}  # 1000〜1449 も抜けない
    assert api.pages == [1, 2, 3, 4, 5, 6]  # キャッシュの最新（999）を含む page 6 で止まる
    # 次の普段の差分取得は page 1 だけ
    _out, st = S.run(tmp_path, api)
    assert api.pages == [1] and st["complete"] is True


def test_bl_fetch_from_newer_than_cache_reads_down_to_cache_latest(cache_dir):
    api = B.Api(1500, play_count=1000)
    B.write_cache(cache_dir, api.items[500:])  # キャッシュは timeset 1〜1000
    until = datetime.utcfromtimestamp(1700000000 + 1450)  # 新しい方から 50 件目
    out, _w, status = B.run(api, fetch_until=until)
    assert status["complete"] is True
    assert B.ids(out) == B.ids(api.items)
    assert sorted(set(api.score_pages)) == [1, 2, 3, 4, 5, 6]
    hc.forget_memoized(None)
    api.score_pages.clear()
    _o, _w2, _s2 = B.run(api)
    assert api.score_pages == [1]


def test_fetch_from_older_than_cache_still_stops_at_given_date(tmp_path, cache_dir):
    """指定がキャッシュの最新より古いときは、今までどおり指定の日時で止まる。"""
    S.write_cache(tmp_path, S.all_items(1000))
    api = S.RecentApi(S.all_items(1000))
    S.run(tmp_path, api, fetch_until=S.BASE + timedelta(hours=750))
    assert api.pages == [1, 2, 3]  # page 3（799〜700）で 750 より古い所に届く

    bapi = B.Api(1000, play_count=1000)
    B.write_cache(cache_dir, bapi.items[:])
    B.run(bapi, fetch_until=datetime.utcfromtimestamp(1700000000 + 750))
    assert sorted(set(bapi.score_pages)) == [1, 2, 3]


# ---------------------------------------------------------------- 2. BL：page 1 が取れなかった普段の差分取得

def test_bl_page1_failure_in_diff_mode_keeps_unranked_scores_after_sync(cache_dir):
    api = B.Api(1200, play_count=1000)  # timeset 1〜1200。1001〜1200 がキャッシュに無い新しいスコア
    B.write_cache(cache_dir, api.items[200:])  # キャッシュは 1〜1000
    before = B.read_cache(cache_dir)
    api.down = True
    _out, warnings, status = B.run(api)
    assert status["complete"] is False
    saved = B.read_cache(cache_dir)
    # 一覧の先頭からキャッシュの最新（1000）までを取りこぼしとして残す。何も読めていないので fetched_at は進めない
    assert saved["failed_ranges"] == [
        {"page": 1, "last_page": 1, "newer_ts": None, "older_ts": 1700001000, "to_end": False}]
    assert saved["fetched_at"] == before["fetched_at"]
    assert saved["scores"] == before["scores"]
    assert len(warnings) == 1 and "refetch only the unfetched range" in warnings[0]

    # 同じ Snapshot の★/pp 同期が、最近のランク譜面のスコア（1101〜1200）だけをキャッシュに入れた
    saved["scores"].update({it["leaderboard"]["id"]: it for it in api.items[:100]})
    (cache_dir / f"beatleader_player_scores_{B.PID}.json").write_text(json.dumps(saved), encoding="utf-8")

    # 次の回：page 1 には差分が無いが、残した範囲を取り直してランク外の 1001〜1100 も入る
    api.down = False
    hc.forget_memoized(None)
    out, _w2, status2 = B.run(api)
    assert B.ids(api.items[100:200]) <= B.ids(out)
    assert B.ids(out) == B.ids(api.items)
    assert B.read_cache(cache_dir)["failed_ranges"] == [] and status2["complete"] is True
    assert sorted(set(api.score_pages)) == [1, 2, 3]  # 全ページは読まない


# ---------------------------------------------------------------- 3. ★同期の全件取り直しが途中で失敗

def test_ss_bulk_star_sync_failure_does_not_write_back_old_stars(tmp_path):
    # wiki のリウェイトでランク譜面キャッシュは 9.5 / 8.5。スコアキャッシュの★は古い 9.0 / 8.0
    Y._write_ss_cache(tmp_path, {"1": 9.5, "2": 8.5}, {"1": ("Song", 9.0, 100.0), "2": ("Zed", 8.0, 50.0)})

    class Down:
        calls = 0

        def get(self, url, params=None, timeout=None):
            Down.calls += 1
            return Y._Resp({}, status_code=500)

    status: dict = {}
    with mock.patch.object(ss, "CACHE_DIR", tmp_path), \
            mock.patch.object(ss, "_STAR_SYNC_CHECKED", tmp_path / "star_sync_checked.json"), \
            mock.patch.object(ss, "log_api_failure"):
        summary = ss.sync_changed_ranked_scores(Y.PID, Down(), status=status)
    assert summary["mode"] == "bulk" and status["complete"] is False
    assert summary["maps_updated"] == 0 and summary["scores_updated"] == 0
    _scores, ranked = Y._ss_state(tmp_path)
    assert ranked["leaderboards"]["1"]["stars"] == 9.5 and ranked["leaderboards"]["2"]["stars"] == 8.5
    # 次の回は取り直す（印を付けていない）
    api = Y.SsApi({"1": ("Song", 9.5, 104.0), "2": ("Zed", 8.5, 55.0)})
    Y._ss_run(tmp_path, api)
    scores, ranked = Y._ss_state(tmp_path)
    assert scores["1"]["score"]["pp"] == 104.0 and scores["2"]["leaderboard"]["stars"] == 8.5


def test_bl_bulk_star_sync_failure_does_not_write_back_old_stars(tmp_path):
    # ランク譜面キャッシュは 9.0 / 8.0、スコアキャッシュの★は古い 8.5 / 7.5。accgraph の今の値は 9.5 / 8.5
    Y._write_bl_cache(tmp_path, {"A1": 9.0, "B1": 8.0}, {"A1": ("aaa", 8.5, 100.0), "B1": ("bbb", 7.5, 90.0)})
    api = Y.BlApi({"A1": ("aaa", 9.5, 104.0, ""), "B1": ("bbb", 8.5, 95.0, "")})
    real_get = api.get

    def _get(url, params=None, timeout=None):
        if url.endswith(f"/player/{Y.PID}/scores") and "search" not in (params or {}):
            api.calls.append("ranked-failed")
            return Y._Resp({}, status_code=500)
        return real_get(url, params=params, timeout=timeout)

    api.get = _get
    status: dict = {}
    with mock.patch.object(bl, "CACHE_DIR", tmp_path), \
            mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", tmp_path / "traversal.json"), \
            mock.patch.object(bl, "_STAR_SYNC_CHECKED", tmp_path / "star_sync_checked.json"):
        summary = bl.sync_changed_ranked_scores(Y.PID, api, status=status)
    assert summary["mode"] == "bulk" and status["complete"] is False
    assert summary["maps_updated"] == 0 and summary["scores_updated"] == 0
    _scores, lbs, _raw = Y._bl_state(tmp_path)
    assert lbs["A1"]["difficulty"]["stars"] == 9.0 and lbs["B1"]["difficulty"]["stars"] == 8.0
    assert not (tmp_path / "traversal.json").exists()  # 書き換えていないので★のずれの印も付けない
    # 次の回（API が戻った）は取り直してランク譜面キャッシュを今の★にし、★のずれの印も付ける
    api.get = real_get
    Y._bl_run(tmp_path, api)
    _scores, lbs, _raw = Y._bl_state(tmp_path)
    assert lbs["A1"]["difficulty"]["stars"] == 9.5 and lbs["B1"]["difficulty"]["stars"] == 8.5
    assert json.loads((tmp_path / "traversal.json").read_text(encoding="utf-8"))["beatleader_stars_drift"] is True


# ---------------------------------------------------------------- 4. 「Fetch from:」の日時はローカル時刻

def test_local_input_to_utc():
    assert col.local_input_to_utc(datetime(2026, 11, 26, 9, 0), tz=JST) == datetime(2026, 11, 26, 0, 0)
    assert col.local_input_to_utc(datetime(2026, 11, 26, 9, 0, tzinfo=JST)) == datetime(2026, 11, 26, 0, 0)
    local = datetime(2026, 11, 26, 9, 0)
    assert col.local_input_to_utc(local) == local.astimezone(timezone.utc).replace(tzinfo=None)


def test_collectors_compare_in_utc():
    aware = datetime(2026, 11, 26, 9, 0, tzinfo=JST)
    assert ss._until_str(aware) == "2026-11-26T00:00:00Z"
    assert ss._until_str(datetime(2026, 11, 26, 0, 0)) == "2026-11-26T00:00:00Z"  # 無しは UTC
    assert bl._utc_timestamp(aware) == bl._utc_timestamp(datetime(2026, 11, 26, 0, 0))


def test_take_snapshot_dialog_passes_utc(tmp_path):
    from PySide6.QtCore import QDate, QDateTime, QTime
    from PySide6.QtWidgets import QApplication
    from mybeatsaberstats import player_app as pa

    _app = QApplication.instance() or QApplication([])
    with mock.patch.object(pa, "_load_take_snapshot_dialog_state", return_value={}), \
            mock.patch.object(pa, "_save_take_snapshot_dialog_state"):
        dlg = pa.TakeSnapshotDialog(None, "1")
        try:
            dlg._cb_ss_fetch_all.setChecked(False)
            dlg._cb_bl_fetch_all.setChecked(False)
            dlg._cb_ss_until.setChecked(True)
            dlg._cb_bl_until.setChecked(True)
            # 画面には PC のローカル時刻で入れる
            dlg._dt_ss_until.setDateTime(QDateTime(QDate(2026, 11, 26), QTime(9, 0, 0)))
            dlg._dt_bl_until.setDateTime(QDateTime(QDate(2026, 11, 26), QTime(9, 0, 0)))
            opts = dlg.snapshot_options()
        finally:
            dlg.deleteLater()
    expected = datetime(2026, 11, 26, 9, 0).astimezone(timezone.utc).replace(tzinfo=None)
    assert opts.ss_fetch_until == expected and opts.bl_fetch_until == expected
    assert opts.ss_ranked_until == expected and opts.bl_ranked_until == expected
    offset = datetime(2026, 11, 26, 9, 0).astimezone().utcoffset()
    if offset == timedelta(hours=9):  # 日本時間の PC
        assert expected == datetime(2026, 11, 26, 0, 0)


# ---------------------------------------------------------------- 5. 検索しても返らなかった譜面

def _age_mark(path: Path, service: str, key: str, hours: float) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    mark = data[service][Y.PID][key]
    why, _, rest = mark.partition("|not_found:")
    count = rest.split("@")[0]
    when = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data[service][Y.PID][key] = f"{why}|not_found:{count}@{when}"
    path.write_text(json.dumps(data), encoding="utf-8")


def _mark(path: Path, service: str, key: str) -> str:
    return json.loads(path.read_text(encoding="utf-8"))[service][Y.PID][key]


def test_ss_not_found_is_not_confirmed_and_retried_after_wait(tmp_path):
    Y._write_ss_cache(tmp_path, {"1": 9.5}, {"1": ("Song", 9.0, 100.0)})
    api = Y.SsApi({})  # 検索で返らない
    checked = tmp_path / "star_sync_checked.json"
    Y._ss_run(tmp_path, api)
    assert api.calls == ["search:Song"]
    assert _mark(checked, "scoresaber", "1").startswith("stars 9.0->9.5|not_found:1@")  # 確認済みにはしない
    api.calls.clear()
    Y._ss_run(tmp_path, api)
    assert api.calls == []  # 待ち時間の中は同じ検索を繰り返さない
    _age_mark(checked, "scoresaber", "1", hours=25)
    Y._ss_run(tmp_path, api)
    assert api.calls == ["search:Song"]  # 待ち時間の後にもう一度探す
    assert _mark(checked, "scoresaber", "1").startswith("stars 9.0->9.5|not_found:2@")
    api.calls.clear()
    _age_mark(checked, "scoresaber", "1", hours=48)
    Y._ss_run(tmp_path, api)
    assert api.calls == []  # 続けて返らなければ長く待つ（14 日）
    # 見つかれば取り直す
    _age_mark(checked, "scoresaber", "1", hours=15 * 24)
    api.maps = {"1": ("Song", 9.5, 104.0)}
    Y._ss_run(tmp_path, api)
    scores, _ = Y._ss_state(tmp_path)
    assert scores["1"]["score"]["pp"] == 104.0


def test_bl_not_found_is_not_confirmed_and_retried_after_wait(tmp_path):
    Y._write_bl_cache(tmp_path, {"A1": 9.0}, {"A1": ("aaa", 9.0, 100.0)})
    api = Y.BlApi({"A1": ("aaa", 9.0, 104.0, "")})
    real_get = api.get

    def _get(url, params=None, timeout=None):
        if params and "search" in params:
            api.calls.append(f"search:{params['search']}")
            return Y._Resp({"metadata": {"total": 0}, "data": []})
        return real_get(url, params=params, timeout=timeout)

    api.get = _get
    checked = tmp_path / "star_sync_checked.json"
    Y._bl_run(tmp_path, api)
    assert api.calls == ["accgraph", "search:aaa"]
    assert _mark(checked, "beatleader", "A1").startswith("pp 100.000->104.000|not_found:1@")
    api.calls.clear()
    Y._bl_run(tmp_path, api)
    assert api.calls == ["accgraph"]
    _age_mark(checked, "beatleader", "A1", hours=25)
    api.calls.clear()
    Y._bl_run(tmp_path, api)
    assert api.calls == ["accgraph", "search:aaa"]


def test_star_sync_marks():
    """取り直しても食い違いが残った印（理由だけ）は今までどおり取り直さない。見つからなかった印は待ち時間だけ。"""
    why = "stars 9.0->9.5"
    now = datetime(2026, 11, 26, tzinfo=timezone.utc)
    assert rs.star_sync_skip(why, why) is True
    assert rs.star_sync_skip(None, why) is False
    assert rs.star_sync_skip("stars 9.0->9.4", why) is False  # 食い違いが変わった
    first = rs.star_sync_not_found_mark(None, why, now=now)
    assert first == "stars 9.0->9.5|not_found:1@2026-11-26T00:00:00Z"
    assert rs.star_sync_skip(first, why, now=now + timedelta(hours=23)) is True
    assert rs.star_sync_skip(first, why, now=now + timedelta(hours=25)) is False
    second = rs.star_sync_not_found_mark(first, why, now=now)
    assert second.startswith("stars 9.0->9.5|not_found:2@")
    assert rs.star_sync_skip(second, why, now=now + timedelta(days=13)) is True
    assert rs.star_sync_skip(second, why, now=now + timedelta(days=15)) is False
    assert rs.star_sync_skip(first, "stars 9.0->9.6", now=now) is False  # 違う食い違いの印
    assert rs.star_sync_not_found_mark(first, "stars 9.0->9.6", now=now).startswith("stars 9.0->9.6|not_found:1@")


# ---------------------------------------------------------------- 6. BL の打ち切りと 429

def test_bl_aborted_run_does_not_go_recover_previous_ranges(cache_dir):
    api = B.Api(2500, play_count=2000)
    rng = {"page": 12, "last_page": 12, "newer_ts": int(api.items[1099]["timeset"]),
           "older_ts": int(api.items[1200]["timeset"]), "to_end": False}
    B.write_cache(cache_dir, api.items[:1100] + api.items[1200:], failed_ranges=[rng])
    api.add_new(150)
    api.fail_pages = {2, 3}  # page 1 は差分あり → page 2・3 が続けて失敗して打ち切る
    _out, _w, status = B.run(api)
    assert sorted(set(api.score_pages)) == [1, 2, 3], api.score_pages  # 前回の範囲（page 13 付近）は取りに行かない
    saved = B.read_cache(cache_dir)
    assert rng in saved["failed_ranges"]  # 前回の範囲は残す
    assert any(r["page"] == 2 for r in saved["failed_ranges"])  # 今回の範囲も残す
    assert status["complete"] is False


def test_bl_recovery_stops_after_two_failed_ranges(cache_dir):
    api = B.Api(2500, play_count=2000, fail_pages=set(range(2, 26)))
    rngs = [{"page": p, "last_page": p, "newer_ts": int(api.items[(p - 1) * 100 - 1]["timeset"]),
             "older_ts": int(api.items[p * 100]["timeset"]), "to_end": False} for p in (5, 10, 15)]
    B.write_cache(cache_dir, api.items[:], failed_ranges=rngs)
    _out, _w, status = B.run(api)
    assert sorted(set(api.score_pages)) == [1, 4, 9], api.score_pages  # 3 つ目（page 14 付近）は取りに行かない
    assert [r["page"] for r in B.read_cache(cache_dir)["failed_ranges"]] == [5, 10, 15]
    assert status["complete"] is False


def test_bl_per_song_star_sync_stops_on_429(tmp_path):
    # 曲ごとの取り直しになるよう、ランクのスコアを増やしておく（ページ数 > 曲の数）
    maps = {f"Z{i}": (f"z{i:03d}", 5.0, 10.0, "") for i in range(150)}
    maps.update({"A1": ("aaa", 9.5, 104.0, ""), "B1": ("bbb", 8.5, 95.0, "")})
    Y._write_bl_cache(
        tmp_path,
        {k: (9.0 if k == "A1" else 8.0 if k == "B1" else 5.0) for k in maps},
        {k: (h, s, p) for k, (h, s, p, _m) in maps.items() if k not in ("A1", "B1")}
        | {"A1": ("aaa", 9.0, 100.0), "B1": ("bbb", 8.0, 90.0)},
    )
    api = Y.BlApi(maps)
    real_get = api.get

    def _limited(url, params=None, timeout=None):
        if params and "search" in params:
            api.calls.append("search-429")
            return Y._Resp({}, status_code=429)
        return real_get(url, params=params, timeout=timeout)

    api.get = _limited
    status: dict = {}
    with mock.patch.object(bl, "CACHE_DIR", tmp_path), \
            mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", tmp_path / "traversal.json"), \
            mock.patch.object(bl, "_STAR_SYNC_CHECKED", tmp_path / "star_sync_checked.json"):
        summary = bl.sync_changed_ranked_scores(Y.PID, api, status=status)
    assert summary["mode"] == "per_song"
    assert api.calls == ["accgraph", "search-429"]  # 残りの曲は次回に回す
    assert status["complete"] is False
    api.get = real_get
    api.calls.clear()
    Y._bl_run(tmp_path, api)
    assert sorted(c for c in api.calls if c.startswith("search")) == ["search:aaa", "search:bbb"]


# ---------------------------------------------------------------- 7. SS のページ取得の再試行

class FlakyRecentApi(S.RecentApi):
    """ページごとに、最初の何回かだけ失敗させる。fail_plan = {page: [失敗の種類, ...]}。"""

    def __init__(self, items, fail_plan):
        super().__init__(items)
        self.fail_plan = {p: list(v) for p, v in fail_plan.items()}

    def get(self, url, params=None, timeout=None):
        page = int(params["page"])
        plan = self.fail_plan.get(page)
        if plan:
            kind = plan.pop(0)
            self.pages.append(page)
            if kind == "timeout":
                raise requests.exceptions.ReadTimeout("read timed out")
            return S._Resp({}, status_code=kind)
        return super().get(url, params=params, timeout=timeout)


def _ss_run_with_sleep(tmp, api, **kw):
    waits: list[float] = []
    with mock.patch("time.sleep", side_effect=waits.append):
        out = S.run(tmp, api, **kw)
    return out, waits


def test_ss_page_fetch_retries_5xx_and_timeout(tmp_path):
    api = FlakyRecentApi(S.all_items(250), {1: [500], 2: ["timeout", 502]})
    (_out, st), waits = _ss_run_with_sleep(tmp_path, api)
    assert st["complete"] is True
    assert api.pages == [1, 1, 2, 2, 2, 3]
    assert S.ids(tmp_path) == {str(i) for i in range(250)}
    # 待ち方は http_client の指数バックオフ（retry_backoff_seconds：2 秒、4 秒）
    assert waits == [2.0, 2.0, 4.0]


def test_ss_page_fetch_gives_up_after_three_and_keeps_range(tmp_path):
    S.write_cache(tmp_path, S.all_items(1000))
    api = FlakyRecentApi(S.all_items(1000) + [S.item(1000 + j) for j in range(150)], {2: [500, 504, 500]})
    (_out, st), waits = _ss_run_with_sleep(tmp_path, api)
    assert api.pages == [1, 2, 2, 2]
    assert len(waits) == 2
    assert st["complete"] is False
    # 今までどおり、読めなかった範囲を残す
    assert S.cache(tmp_path)["resume_ranges"] == [{"newer_ts": S.ts(1050), "older_ts": S.ts(999)}]


def test_ss_page_fetch_does_not_retry_503(tmp_path):
    """503 / 429 は PoliteSession が Retry-After に従って既にリトライしているので、重ねない。"""
    api = FlakyRecentApi(S.all_items(50), {1: [503]})
    (_out, st), waits = _ss_run_with_sleep(tmp_path, api)
    assert api.pages == [1] and waits == [] and st["complete"] is False


def test_ss_page1_failure_keeps_fetched_at(tmp_path):
    S.write_cache(tmp_path, S.all_items(100))
    before = S.cache(tmp_path)["fetched_at"]
    api = S.RecentApi(S.all_items(150), fail_pages={1})
    _out, st = S.run(tmp_path, api)
    assert st["complete"] is False
    c = S.cache(tmp_path)
    assert c["fetched_at"] == before  # 何も読めていない回は 60 日遡りの基準を進めない
    assert c["resume_ranges"] == [{"newer_ts": None, "older_ts": S.ts(99)}]


# ================================================================ aitask #413 のレビュー（#412 の修正のまとめレビュー）の指摘

def _ss_filler(i):
    """pp の付いたランクのスコア（並びの埋め草。ランク譜面キャッシュ・スコアキャッシュには無い）。"""
    return Y._ss_item(1000 + i, f"Filler{i}", 5.0, 1.0)


def _bl_filler(i):
    return Y._bl_item(f"F{i}", f"f{i:03d}", 5.0, 1.0)


def _marks(path: Path, service: str) -> dict:
    if not path.exists():
        return {}
    return (json.loads(path.read_text(encoding="utf-8")).get(service) or {}).get(Y.PID) or {}


def test_ss_bulk_star_sync_stopped_midway_still_updates_stars_of_read_maps(tmp_path):
    """全部の取り直しが途中で止まっても、今回読めた譜面の★では書き換える。確認済みの印で★を古いまま固めない。"""
    # ランク譜面キャッシュが古い（9.0 / 8.0）。スコアキャッシュは 9.2 / 8.2。今の★は 9.5 / 8.5
    Y._write_ss_cache(tmp_path, {"1": 9.0, "2": 8.0}, {"1": ("Song", 9.2, 100.0), "2": ("Zed", 8.2, 50.0)})

    class Api:
        def get(self, url, params=None, timeout=None):
            if (params or {}).get("page") == "1":  # 100 件（"1" と埋め草 99 件）。"2" は page 2 にあるはずだった
                data = [Y._ss_item(1, "Song", 9.5, 104.0, 600.0)] + [_ss_filler(i) for i in range(99)]
                return Y._Resp({"playerScores": data})
            return Y._Resp({}, status_code=500)

    status: dict = {}
    with mock.patch.object(ss, "CACHE_DIR", tmp_path), \
            mock.patch.object(ss, "_STAR_SYNC_CHECKED", tmp_path / "star_sync_checked.json"), \
            mock.patch.object(ss, "log_api_failure"), mock.patch("time.sleep"):
        summary = ss.sync_changed_ranked_scores(Y.PID, Api(), status=status)
    assert summary["mode"] == "bulk" and status["complete"] is False
    scores, ranked = Y._ss_state(tmp_path)
    assert ranked["leaderboards"]["1"]["stars"] == 9.5  # 読めた譜面は今の★にする
    assert ranked["leaderboards"]["2"]["stars"] == 8.0  # 読めていない譜面は書き換えない
    assert scores["2"]["leaderboard"]["stars"] == 8.2
    marks = _marks(tmp_path / "star_sync_checked.json", "scoresaber")
    assert "1" not in marks and "2" not in marks  # 確認済みにしない（"2" は次の回に取り直す）


def test_bl_bulk_star_sync_stopped_midway_still_updates_stars_of_read_maps(tmp_path):
    Y._write_bl_cache(tmp_path, {"A1": 9.0, "B1": 8.0}, {"A1": ("aaa", 9.0, 100.0), "B1": ("bbb", 8.0, 90.0)})
    api = Y.BlApi({"A1": ("aaa", 9.5, 104.0, ""), "B1": ("bbb", 8.5, 95.0, "")})
    real_get = api.get

    def _get(url, params=None, timeout=None):
        if url.endswith(f"/player/{Y.PID}/scores") and "search" not in (params or {}):
            page = int(params.get("page") or 1)
            api.calls.append(f"ranked:{page}")
            if page == 1:  # 100 件（A1 と埋め草 99 件）。B1 は page 2 にあるはずだった
                data = [Y._bl_item("A1", "aaa", 9.5, 104.0)] + [_bl_filler(i) for i in range(99)]
                return Y._Resp({"metadata": {"total": 101}, "data": data})
            return Y._Resp({}, status_code=500)
        return real_get(url, params=params, timeout=timeout)

    api.get = _get
    status: dict = {}
    with mock.patch.object(bl, "CACHE_DIR", tmp_path), \
            mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", tmp_path / "traversal.json"), \
            mock.patch.object(bl, "_STAR_SYNC_CHECKED", tmp_path / "star_sync_checked.json"), \
            mock.patch("time.sleep"):
        summary = bl.sync_changed_ranked_scores(Y.PID, api, status=status)
    assert summary["mode"] == "bulk" and status["complete"] is False
    _scores, lbs, _raw = Y._bl_state(tmp_path)
    assert lbs["A1"]["difficulty"]["stars"] == 9.5  # 読めた譜面は今の★にする
    assert lbs["B1"]["difficulty"]["stars"] == 8.0  # 読めていない譜面は書き換えない
    marks = _marks(tmp_path / "star_sync_checked.json", "beatleader")
    assert "A1" not in marks and "B1" not in marks
    # 書き換えたので、プレイしていない譜面の★を直す全件取り直しの印も付ける
    assert json.loads((tmp_path / "traversal.json").read_text(encoding="utf-8"))["beatleader_stars_drift"] is True


def test_ss_bulk_star_sync_not_read_to_end_waits_before_next_bulk(tmp_path):
    """全部の取り直しが一覧の終わりまで届かなかった（総数の前の空ページ）回も、返らなかった譜面には待ち時間の印を付け、
    次の Snapshot で全部の取り直しを繰り返さない。"""
    Y._write_ss_cache(tmp_path, {"1": 9.0, "2": 8.0}, {"1": ("Song", 9.2, 100.0), "2": ("Zed", 8.2, 50.0)})
    calls: list = []

    class Api:
        def get(self, url, params=None, timeout=None):
            page = (params or {}).get("page")
            calls.append(page)
            if page == "1":
                data = [Y._ss_item(1, "Song", 9.5, 104.0, 600.0)] + [_ss_filler(i) for i in range(99)]
                return Y._Resp({"playerScores": data})
            return Y._Resp({"playerScores": []})  # "2" が返る前に空ページ

    summary = Y._ss_run(tmp_path, Api())
    assert summary["mode"] == "bulk"
    assert _mark(tmp_path / "star_sync_checked.json", "scoresaber", "2").startswith("stars 8.2->8.0|not_found:1@")
    calls.clear()
    Y._ss_run(tmp_path, Api())
    assert calls == []  # 待ち時間の中は取り直さない


def test_bl_bulk_star_sync_not_read_to_end_waits_before_next_bulk(tmp_path):
    Y._write_bl_cache(tmp_path, {"A1": 9.0, "B1": 8.0}, {"A1": ("aaa", 9.0, 100.0), "B1": ("bbb", 8.0, 90.0)})
    api = Y.BlApi({"A1": ("aaa", 9.5, 104.0, ""), "B1": ("bbb", 8.5, 95.0, "")})
    real_get = api.get

    def _get(url, params=None, timeout=None):
        if url.endswith(f"/player/{Y.PID}/scores") and "search" not in (params or {}):
            page = int(params.get("page") or 1)
            api.calls.append(f"ranked:{page}")
            if page == 1:
                data = [Y._bl_item("A1", "aaa", 9.5, 104.0)] + [_bl_filler(i) for i in range(99)]
                return Y._Resp({"metadata": {"total": 101}, "data": data})
            return Y._Resp({"metadata": {"total": 101}, "data": []})  # B1 が返る前に空ページ
        return real_get(url, params=params, timeout=timeout)

    api.get = _get
    summary = Y._bl_run(tmp_path, api)
    assert summary["mode"] == "bulk"
    assert "|not_found:1@" in _mark(tmp_path / "star_sync_checked.json", "beatleader", "B1")
    api.calls.clear()
    Y._bl_run(tmp_path, api)
    assert api.calls == ["accgraph"]  # 待ち時間の中は取り直さない


def test_star_sync_keeps_score_cache_fetched_at(tmp_path):
    """★同期でスコアを取り直しても、スコアキャッシュの fetched_at（60 日遡りの基準）は変えない（SS・BL、曲ごと・全部）。"""
    # SS：曲ごと
    Y._write_ss_cache(tmp_path, {"1": 9.5}, {"1": ("Song", 9.0, 100.0)})
    Y._ss_run(tmp_path, Y.SsApi({"1": ("Song", 9.5, 104.0)}))
    ss_cache = json.loads((tmp_path / f"scoresaber_player_scores_{Y.PID}.json").read_text(encoding="utf-8"))
    assert ss_cache["scores"]["1"]["score"]["pp"] == 104.0 and ss_cache["fetched_at"] == "2026-11-20T00:00:00Z"
    # SS：全部の取り直し（refresh_scoresaber_ranked_scores の保存）
    Y._write_ss_cache(tmp_path, {"1": 9.5, "2": 8.5}, {"1": ("Song", 9.0, 100.0), "2": ("Zed", 8.0, 50.0)})
    summary = Y._ss_run(tmp_path, Y.SsApi({"1": ("Song", 9.5, 104.0), "2": ("Zed", 8.5, 55.0)}))
    assert summary["mode"] == "bulk"
    ss_cache = json.loads((tmp_path / f"scoresaber_player_scores_{Y.PID}.json").read_text(encoding="utf-8"))
    assert ss_cache["scores"]["2"]["score"]["pp"] == 55.0 and ss_cache["fetched_at"] == "2026-11-20T00:00:00Z"
    # BL：曲ごと
    Y._write_bl_cache(tmp_path, {"A1": 9.0}, {"A1": ("aaa", 9.0, 100.0)})
    Y._bl_run(tmp_path, Y.BlApi({"A1": ("aaa", 9.5, 104.0, "")}))
    bl_cache = json.loads((tmp_path / f"beatleader_player_scores_{Y.PID}.json").read_text(encoding="utf-8"))
    assert bl_cache["scores"]["A1"]["pp"] == 104.0 and bl_cache["fetched_at"] == "2026-11-20T00:00:00Z"
    # BL：全部の取り直し
    Y._write_bl_cache(tmp_path, {"A1": 9.0, "B1": 8.0}, {"A1": ("aaa", 9.0, 100.0), "B1": ("bbb", 8.0, 90.0)})
    summary = Y._bl_run(tmp_path, Y.BlApi({"A1": ("aaa", 9.5, 104.0, ""), "B1": ("bbb", 8.5, 95.0, "")}))
    assert summary["mode"] == "bulk"
    bl_cache = json.loads((tmp_path / f"beatleader_player_scores_{Y.PID}.json").read_text(encoding="utf-8"))
    assert bl_cache["scores"]["B1"]["pp"] == 95.0 and bl_cache["fetched_at"] == "2026-11-20T00:00:00Z"


def test_bl_retry_failed_pages_only_keeps_fetched_at(cache_dir):
    """★統計の取得時の取りこぼしの取り直し（retry_failed_pages_only）でも fetched_at は変えない。"""
    api = B.Api(2500, play_count=2000)
    rng = {"page": 12, "last_page": 12, "newer_ts": int(api.items[1099]["timeset"]),
           "older_ts": int(api.items[1200]["timeset"]), "to_end": False}
    B.write_cache(cache_dir, api.items[:1100] + api.items[1200:], failed_ranges=[rng])
    _out, _w, status = B.run(api, retry_failed_pages_only=True)
    saved = B.read_cache(cache_dir)
    assert saved["failed_ranges"] == [] and status["complete"] is True
    assert saved["fetched_at"] == "2026-11-18T00:00:00Z"


class _Bl429Api(B.Api):
    """指定のページで、PoliteSession の待ちの後も 429 が返る。"""

    def __init__(self, *a, limit_pages=(), **kw):
        super().__init__(*a, **kw)
        self.limit_pages = set(limit_pages)

    def get(self, url, params=None, timeout=None, **kw):
        if url.endswith(f"/player/{B.PID}/scores") and int(params["page"]) in self.limit_pages:
            self.urls.append(url)
            self.score_pages.append(int(params["page"]))
            return B._Resp({}, url, status_code=429)
        return super().get(url, params=params, timeout=timeout, **kw)


def test_bl_score_pages_stop_on_429(cache_dir):
    """BL のスコア一覧で 429 が返ったら、次のページ・前回の取りこぼしを取りに行かずに止める（SS と同じ）。"""
    api = _Bl429Api(2500, play_count=2000, limit_pages={2})
    rng = {"page": 12, "last_page": 12, "newer_ts": int(api.items[1099]["timeset"]),
           "older_ts": int(api.items[1200]["timeset"]), "to_end": False}
    B.write_cache(cache_dir, api.items[:1100] + api.items[1200:], failed_ranges=[rng])
    api.add_new(150)
    _out, warnings, status = B.run(api)
    assert sorted(set(api.score_pages)) == [1, 2], api.score_pages  # page 3・前回の範囲は取りに行かない
    assert api.score_pages.count(2) == 1  # 429 は重ねて試さない（PoliteSession が待って試している）
    saved = B.read_cache(cache_dir)
    assert rng in saved["failed_ranges"]  # 前回の範囲は残す
    assert any(r["page"] == 2 for r in saved["failed_ranges"])  # 今回の範囲も残す
    assert status["complete"] is False
    assert any("rate limited (HTTP 429)" in w for w in warnings)


def test_bl_retry_failed_pages_only_stops_on_429(cache_dir):
    api = _Bl429Api(2500, play_count=2000, limit_pages=set(range(2, 26)))
    rngs = [{"page": p, "last_page": p, "newer_ts": int(api.items[(p - 1) * 100 - 1]["timeset"]),
             "older_ts": int(api.items[p * 100]["timeset"]), "to_end": False} for p in (5, 10)]
    B.write_cache(cache_dir, api.items[:], failed_ranges=rngs)
    _out, _w, status = B.run(api, retry_failed_pages_only=True)
    assert sorted(set(api.score_pages)) == [4], api.score_pages  # 2 つ目の範囲（page 9 付近）は取りに行かない
    assert [r["page"] for r in B.read_cache(cache_dir)["failed_ranges"]] == [5, 10]
    assert status["complete"] is False
