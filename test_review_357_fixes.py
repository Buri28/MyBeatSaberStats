"""#357 のレビュー指摘（項目 2・3・5・7・8・9）の回帰テスト。模擬応答と一時フォルダだけを使い、実 API・実キャッシュに触れない。

項目 1・4・6 は test_ranked_sync.py、項目 5 の既存の外し方は test_prune_missing_scores.py にある。
"""
import json
import sys
from contextlib import ExitStack
from dataclasses import fields
from pathlib import Path
from unittest import mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

sys.path.insert(0, str(Path(__file__).parent / "src"))
from mybeatsaberstats import accsaber_reloaded as ar, beatleader as blp, http_client  # noqa: E402
from mybeatsaberstats import player_app as pa  # noqa: E402
from mybeatsaberstats.accsaber_status import status_text  # noqa: E402
from mybeatsaberstats.collector import collector as c  # noqa: E402
from mybeatsaberstats.collector import scoresaber as ss  # noqa: E402
from mybeatsaberstats.player_pp import PlayerPPPanel, pp_limits_by_field  # noqa: E402
import test_ss_ranked_cases as T  # noqa: E402
from test_player_pp import snapshot  # noqa: E402
from test_prune_missing_scores import SsApi, ss_read, ss_refresh, ss_setup  # noqa: E402


class _Resp:
    def __init__(self, data, status=200, url="x"):
        self._d, self.status_code, self.url, self.headers = data, status, url, {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._d


class CountingSession:
    """URL ごとの呼び出し回数を数える模擬セッション（weakref を張れる普通のクラス）。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls: dict[str, int] = {}

    def get(self, url, params=None, timeout=None, **kw):
        self.calls[url] = self.calls.get(url, 0) + 1
        for key, fn in self.routes.items():
            if key in url:
                return fn(url, params)
        raise AssertionError(f"unexpected url {url}")


# ---------------- 項目 2：SS の一覧が総数に届かないだけなら PP の取り直しは「完了」 ----------------

def test_ss_listing_short_reconcile_recorded_as_complete(tmp_path):
    maps = T.base_maps()

    class ShortApi(T.FakeApi):
        def get(self, url, params=None, timeout=None):
            r = super().get(url, params, timeout)
            r._d["metadata"]["total"] = len(self.maps) + 3  # 普段から総数に届かない（6161/6243 と同じ状況）
            return r

    T.write_cache(tmp_path, maps, fetched_days_ago=1)
    status = {"complete": True}
    with mock.patch.object(ss, "CACHE_DIR", tmp_path), \
            mock.patch.object(ss, "_RANKED_FULL_TRAVERSAL_STATE", tmp_path / "traversal.json"), \
            mock.patch.object(ss, "_is_rate_limited", return_value=False), \
            mock.patch.object(ss, "log_api_failure"):
        ss._get_scoresaber_leaderboards_ranked(ShortApi(maps), fetch_until=T.datetime(2000, 1, 1), status=status)
    assert status.get("ss_ranked_listing_short") is True
    state_path = tmp_path / "score_reconcile.json"
    with mock.patch.object(c, "_SCORE_RECONCILE_STATE_PATH", state_path), mock.patch.object(c, "CACHE_DIR", tmp_path):
        # collector と同じ記録の仕方（complete=bool(status["complete"])）
        c._record_pp_reconcile_result("scoresaber", "1", 100.0, 99.0, complete=bool(status.get("complete")))
        st = json.loads(state_path.read_text(encoding="utf-8"))["scoresaber"]["1"]
    assert "full_scan_at" in st and "pp_reconcile_failed_at" not in st, st
    # 7 日待ちが効く（24 時間ごとに取り直さない）
    assert c._pp_reconcile_skip_reason(st, 100.0, 1.0) is not None


def test_ss_real_failure_still_recorded_as_failure(tmp_path):
    maps = T.base_maps()
    T.write_cache(tmp_path, maps, fetched_days_ago=1)
    status = {"complete": True}
    with mock.patch.object(ss, "CACHE_DIR", tmp_path), \
            mock.patch.object(ss, "_RANKED_FULL_TRAVERSAL_STATE", tmp_path / "traversal.json"), \
            mock.patch.object(ss, "_is_rate_limited", return_value=False), \
            mock.patch.object(ss, "log_api_failure"):
        ss._get_scoresaber_leaderboards_ranked(T.FakeApi(maps, fail_page=2), fetch_until=T.datetime(2000, 1, 1),
                                               status=status)
    assert status["complete"] is False and not status.get("ss_ranked_listing_short")
    state_path = tmp_path / "score_reconcile.json"
    with mock.patch.object(c, "_SCORE_RECONCILE_STATE_PATH", state_path), mock.patch.object(c, "CACHE_DIR", tmp_path):
        c._record_pp_reconcile_result("scoresaber", "1", 100.0, 99.0, complete=bool(status.get("complete")))
        st = json.loads(state_path.read_text(encoding="utf-8"))["scoresaber"]["1"]
    assert "pp_reconcile_failed_at" in st


# ---------------- 項目 3：BL の /player は xyz に 1 回だけ ----------------

def test_bl_player_fetched_once_per_snapshot_session():
    payload = {"id": "7", "name": "x", "country": "JP", "pp": 1.0, "rank": 1, "countryRank": 1,
               "passPp": 1.5, "techPp": 2.5, "accPp": 3.5, "scoreStats": {"rankedPlayCount": 5}}
    session = CountingSession({"/player/7": lambda u, p: _Resp(payload, url=u)})
    player = blp.fetch_player("7", session=session)
    stats = c._get_beatleader_player_stats("7", session)
    assert (player.pass_pp, player.tech_pp, player.acc_pp) == (1.5, 2.5, 3.5)
    assert stats == {"rankedPlayCount": 5}
    assert session.calls == {"https://api.beatleader.com/player/7": 1}, session.calls


def test_bl_player_host_has_rate_policy():
    assert http_client._policy_for("https://api.beatleader.com/player/7") is http_client._POLICIES["api.beatleader.com"]


# ---------------- 項目 5：rankedPlayCount と合わないときは外さない ----------------

def test_ss_prune_requires_matching_ranked_play_count(tmp_path):
    for count, kept in ((None, True), (149, False), (150, True), (148, True)):
        d = tmp_path / str(count)
        d.mkdir()
        ranked = ss_setup(d)
        returned = [x for x in ranked if x["leaderboard"]["id"] != 3]  # 149 件返る
        ss_refresh(d, SsApi(returned), status={"complete": True}, ranked_play_count=count)
        assert ("3" in ss_read(d)["scores"]) is kept, count


def test_ss_page_shift_with_profile_count_keeps_score(tmp_path):
    """ページがずれて 1 件読み飛ばした：読めたのは 149 件、プロフィールは 150 件 → 外さない。"""
    ranked = ss_setup(tmp_path)
    returned = [x for x in ranked if x["leaderboard"]["id"] != 3]
    ss_refresh(tmp_path, SsApi(returned), status={"complete": True}, ranked_play_count=150)
    assert "3" in ss_read(tmp_path)["scores"]


def test_collector_passes_profile_ranked_play_count():
    src = Path(c.__file__).read_text(encoding="utf-8")
    assert "ranked_play_count=scoresaber_ranked_play_count" in src


# ---------------- 項目 7：AccSaber の未登録と失敗を分ける ----------------

def test_accsaber_profile_404_is_not_registered():
    session = CountingSession({"/users/9": lambda u, p: _Resp({}, status=404, url=u)})
    with mock.patch.object(ar, "log_api_failure") as log:
        assert ar.fetch_user_profile("9", session) is None
    assert ar.user_not_registered("9", session) is True
    assert not log.called, "未登録を API の失敗として記録した"
    other = CountingSession({"/users/9": lambda u, p: _Resp({}, status=500, url=u)})
    with mock.patch.object(ar, "log_api_failure"):
        ar.fetch_user_profile("9", other)
    assert ar.user_not_registered("9", other) is False


def test_accsaber_failed_categories_reported():
    def board(url, params):
        if "boom" in url:
            return _Resp({}, status=500, url=url)
        return _Resp({"content": [], "last": True}, url=url)
    ids = dict(ar.CATEGORY_IDS)
    ids["true"] = "boom"
    session = CountingSession({"/users/": lambda u, p: _Resp({"name": "x"}, url=u), "/leaderboards/": board})
    failed: set = set()
    warnings = []
    with mock.patch.object(ar, "CATEGORY_IDS", ids), mock.patch.object(ar, "log_api_failure"), \
            mock.patch.object(ar, "_is_rate_limited", return_value=False):
        res = ar.fetch_player_all_categories("1", session=session, on_warning=warnings.append, failed=failed)
    assert all(v is None for v in res.values())
    assert failed == {"true"} and len(warnings) == 1


def _run_accsaber_snapshot(tmp_path, categories, failed_cats=(), not_registered=False, skill=None, xp=True):
    from mybeatsaberstats.accsaber_reloaded import AccSaberReloadedPlayer, AccSaberReloadedXP

    def player(cat):
        return AccSaberReloadedPlayer("1", "x", "JP", 100.0, 0.95, 10, 5, 2)

    def fetch_all(pid, country=None, session=None, on_warning=None, failed=None):
        out = {}
        for cat in ("overall", "true", "standard", "tech"):
            if cat in failed_cats:
                on_warning(f"AccSaber {cat}: 500 Server Error")
                failed.add(cat)
                out[cat] = None
            else:
                out[cat] = player(cat) if cat in categories else None
        return out

    snaps = tmp_path / "snapshots"
    with ExitStack() as st:
        st.enter_context(mock.patch.object(c, "CACHE_DIR", tmp_path / "cache"))
        st.enter_context(mock.patch.object(c, "_fetch_accsaber_reloaded", side_effect=fetch_all))
        st.enter_context(mock.patch.object(c, "_rl_user_not_registered", return_value=not_registered))
        st.enter_context(mock.patch.object(c, "_fetch_accsaber_reloaded_xp",
                                           return_value=AccSaberReloadedXP(1.0, 3, 1, 1) if xp else None))
        st.enter_context(mock.patch.object(c, "_fetch_accsaber_reloaded_level_title", return_value=None))
        st.enter_context(mock.patch.object(c, "_fetch_accsaber_reloaded_milestones",
                                           return_value=None if not_registered else (1, 2)))
        st.enter_context(mock.patch.object(c, "_fetch_accsaber_reloaded_skill", return_value=skill or {
            cat: (50.0 if cat in categories else None) for cat in ("overall", "true", "standard", "tech")}))
        st.enter_context(mock.patch.object(c, "_fetch_accsaber_reloaded_title", return_value=None))
        st.enter_context(mock.patch.object(c, "_fetch_and_save_rl_maps", return_value=None))
        st.enter_context(mock.patch.object(c, "_get_rl_map_counts_cached", return_value={}))
        st.enter_context(mock.patch.object(c, "_fetch_and_save_rl_player_scores", return_value=None))
        st.enter_context(mock.patch.object(c, "_compute_rl_effective_played_counts", return_value={}))
        st.enter_context(mock.patch("requests.sessions.Session.request", side_effect=AssertionError("network")))
        (tmp_path / "cache").mkdir(exist_ok=True)
        c._save_player_index({"1": {"beatleader": blp.BeatLeaderPlayer("1", "x", "JP", 1, 1, 1)}})
        options = c.SnapshotOptions(**{f.name: False for f in fields(c.SnapshotOptions) if f.name.startswith("fetch_")})
        options.fetch_accsaber_reloaded = True
        warnings: list[str] = []
        snap = c._create_snapshot_for_steam_id("1", options=options, snapshot_dir=snaps, on_warning=warnings.append)
    return snap, warnings


def test_accsaber_partly_unregistered_is_ok(tmp_path):
    snap, warnings = _run_accsaber_snapshot(tmp_path, categories={"overall", "standard"})
    assert snap.accsaber_fetch_status == "ok" and status_text(snap) == "取得済み", snap.accsaber_fetch_status
    assert not [w for w in warnings if "AccSaber" in w], warnings


def test_accsaber_not_registered_anywhere_is_not_failure(tmp_path):
    snap, warnings = _run_accsaber_snapshot(tmp_path, categories=set(), not_registered=True, xp=False)
    assert snap.accsaber_fetch_status == "not_registered" and status_text(snap) == "未登録"
    assert not [w for w in warnings if "AccSaber" in w], warnings


def test_accsaber_category_exception_warns_once(tmp_path):
    # ランキングの取得だけが失敗。スキル API は 4 カテゴリとも返った
    snap, warnings = _run_accsaber_snapshot(tmp_path, categories={"overall", "standard", "tech"},
                                            failed_cats=("true",),
                                            skill={k: 50.0 for k in ("overall", "true", "standard", "tech")})
    acc = [w for w in warnings if "AccSaber" in w]
    assert acc == ["AccSaber true: 500 Server Error"], acc
    assert snap.accsaber_fetch_status == "partial"
    assert snap.accsaber_fetch_errors == acc


# ---------------- 項目 8：行の高さ 21（手動）を開き直しても保つ ----------------

def test_manual_row_height_21_restored(tmp_path):
    from test_row_height_restore import _open, _lr
    from mybeatsaberstats import snapshot_view as sv
    with ExitStack() as stack:
        d = _open(tmp_path, stack, (1540, 887))
        left0, _ = _lr(d)
        assert left0 == [19, 19]
        d._on_row_height_up(); d._on_row_height_up()  # 19 → 21（手動）
        left, right = _lr(d)
        assert left == [21, 21] and d._manual_row_height is True
        d._save_last_selection()
        d.close()
        d2 = sv.SnapshotCompareDialog(initial_steam_id="76561198324870685")
        d2.resize(1540, 887); d2.show()
        left2, right2 = _lr(d2)
        manual = d2._manual_row_height
        d2.close()
    assert left2 == [21, 21] and manual is True, (left2, manual)
    assert right2 == right


# ---------------- 項目 9：トップ画面の BeatLeader Skill は値だけ・🏆 なし ----------------

@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_top_skill_value_only_no_trophy(app):
    table = PlayerPPPanel(None, pa.PercentageBarDelegate)
    try:
        table.set_snapshots(snapshot(beatleader_acc_pp=20000.0, beatleader_tech_pp=1200.0, beatleader_pass_pp=None))
        assert [table.item(r, 1).text() for r in range(3)] == ["20,000.00", "1,200.00", "—"]
        for row in range(2):
            option = QStyleOptionViewItem()
            table._bars[row].initStyleOption(option, table.model().index(row, 1))
            assert "🏆" not in option.text and "🏅" not in option.text, option.text
            assert not option.font.bold()
        assert "上限ではありません" in table.item(0, 1).toolTip()
        # 最小幅は値だけの文字列で計算する（上限の文字列分を取らない）
        fm = table.fontMetrics()
        assert table.minimumWidth() == fm.horizontalAdvance("Pass PP") + fm.horizontalAdvance("99,999.99") + 24
    finally:
        table.close()


def test_other_bars_still_show_trophy(app):
    from PySide6.QtWidgets import QTableWidget, QTableWidgetItem
    t = QTableWidget(1, 1)
    item = QTableWidgetItem("100.00%")
    item.setData(Qt.ItemDataRole.UserRole, 100.0)
    t.setItem(0, 0, item)
    delegate = pa.PercentageBarDelegate(t, max_value=100.0)
    option = QStyleOptionViewItem()
    delegate.initStyleOption(option, t.model().index(0, 0))
    assert option.text.endswith("🏆") and option.font.bold()
    t.close()


def test_top_and_compare_use_same_latest_scale():
    for date in ("2026-10-26T00:00:00Z", "2026-11-18T00:00:00Z"):
        assert pp_limits_by_field(snapshot(taken_at=date)) == pp_limits_by_field(None) == {
            "beatleader_pass_pp": 3000, "beatleader_tech_pp": 1200, "beatleader_acc_pp": 14500}


# ---------------- レビューの指摘：途中で止まった取り直しで印を付けない／総数 0 の空ページ ----------------

def test_newly_ranked_no_mark_when_refresh_stopped_midway(tmp_path):
    from test_ranked_sync import _ss_setup
    from mybeatsaberstats.collector import beatleader as bl

    class Ss404:
        calls = 0

        def get(self, url, params=None, timeout=None):
            self.calls += 1
            r = _Resp({}, status=404)
            return r
    _ss_setup(tmp_path, pp_in_api=0)
    with mock.patch.object(ss, "CACHE_DIR", tmp_path), mock.patch.object(ss, "_is_rate_limited", return_value=False), \
            mock.patch.object(ss, "log_api_failure"):
        assert ss.refresh_scores_of_newly_ranked_maps("1", Ss404()) == 2
    assert not (tmp_path / "newly_ranked_checked.json").exists(), "404 で止まったのに印を付けた"

    d = tmp_path / "bl"
    d.mkdir()
    from test_ranked_sync import bl_write, bl_lb
    bl_write(d, [bl_lb(i, 3) for i in range(150)], 1)

    def item(i, status, pp):
        return {"pp": pp, "timeset": "1700000000",
                "leaderboard": {"id": f"L{i}", "difficulty": {"status": status, "rankedTime": 0}}}
    (d / "beatleader_player_scores_7.json").write_text(json.dumps(
        {"fetched_at": "2026-11-18T00:00:00Z", "total_play_count": 1, "scores": {"L149": item(149, 0, 0)}}),
        encoding="utf-8")

    class BlStop:
        def get(self, url, params=None, timeout=None):
            # 総数 300 のうち 1 ページ目（100 件）だけ返し、2 ページ目が空
            data = [item(i, 3, 10.0) for i in range(100)] if params["page"] == "1" else []
            return _Resp({"metadata": {"total": 300}, "data": data})
    with mock.patch.object(bl, "CACHE_DIR", d), mock.patch.object(bl, "_log_api_failure"):
        assert bl.refresh_scores_of_newly_ranked_maps("7", BlStop()) == 1
    assert not (d / "newly_ranked_checked.json").exists(), "空ページで止まったのに印を付けた"


def test_zero_total_empty_page_is_not_complete(tmp_path):
    from mybeatsaberstats.collector import beatleader as bl
    from test_ranked_sync import bl_write, bl_lb, bl_mark, state

    class SsZero:
        def get(self, url, params=None, timeout=None):
            return T._Resp({"metadata": {"total": 0, "itemsPerPage": 14}, "leaderboards": []})
    maps = T.base_maps()
    T.write_cache(tmp_path, maps, fetched_days_ago=1)
    T.mark_full(tmp_path, days_ago=20)
    st0 = json.loads((tmp_path / "traversal.json").read_text(encoding="utf-8"))
    st0["scoresaber_stars_drift"] = True
    (tmp_path / "traversal.json").write_text(json.dumps(st0), encoding="utf-8")
    status = {"complete": True}
    with mock.patch.object(ss, "CACHE_DIR", tmp_path), \
            mock.patch.object(ss, "_RANKED_FULL_TRAVERSAL_STATE", tmp_path / "traversal.json"), \
            mock.patch.object(ss, "_is_rate_limited", return_value=False):
        ss._get_scoresaber_leaderboards_ranked(SsZero(), fetch_until=T.datetime(2000, 1, 1), status=status)
    st = json.loads((tmp_path / "traversal.json").read_text(encoding="utf-8"))
    assert st.get("scoresaber_stars_drift") is True and status["complete"] is False
    assert not status.get("ss_ranked_listing_short")

    d = tmp_path / "bl"
    d.mkdir()
    maps = [bl_lb(i, 10 + i) for i in range(50)]
    bl_write(d, maps, 1)
    bl_mark(d, 20)
    s0 = state(d)
    s0["beatleader_stars_drift"] = True
    (d / "traversal.json").write_text(json.dumps(s0), encoding="utf-8")

    class BlZero:
        def get(self, url, params=None, timeout=None):
            return T._Resp({"metadata": {"total": 0}, "data": []})
    status = {"complete": True}
    with mock.patch.object(bl, "CACHE_DIR", d), \
            mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", d / "traversal.json"):
        bl._get_beatleader_leaderboards_ranked(BlZero(), fetch_until=T.datetime(2000, 1, 1), status=status)
    assert state(d).get("beatleader_stars_drift") is True and status["complete"] is False
