"""ランク譜面の全件取り直しを、期間（定期）では行わないことの回帰テスト（ScoreSaber・BeatLeader）。

全件取り直しは「全件取得を一度も最後まで終えていない」「★のずれの印がある」ときだけ。
完了の記録から何日経っても、印が無ければ日付の窓の差分取得のまま。失敗直後の待ち時間は守る。
どのテストも state・キャッシュを一時フォルダに向ける（実キャッシュ・実 API には触らない）。
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "src"))
from mybeatsaberstats import ranked_sync as rs  # noqa: E402
import test_ranked_sync as R  # noqa: E402
from test_ss_ranked_cases import FakeApi, base_maps, mark_full, run, write_cache  # noqa: E402

SERVICES = ("scoresaber", "beatleader")


def _stamp(**delta):
    return (datetime.utcnow() - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_state(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def _state(tmp):
    return json.loads((tmp / "traversal.json").read_text(encoding="utf-8"))


# ---------------- 判定そのもの（ranked_sync.full_traversal_due） ----------------

@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("days", [15, 100])
def test_due_false_long_after_complete(tmp_path, service, days):
    p = tmp_path / "t.json"
    _write_state(p, {service: _stamp(days=days), f"{service}_complete": _stamp(days=days), f"{service}_last_ok": True})
    assert not rs.full_traversal_due(p, service)


@pytest.mark.parametrize("service", SERVICES)
def test_due_true_with_star_drift(tmp_path, service):
    p = tmp_path / "t.json"
    _write_state(p, {service: _stamp(days=2), f"{service}_complete": _stamp(days=2), f"{service}_last_ok": True,
                     f"{service}_stars_drift": True})
    assert rs.full_traversal_due(p, service)


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("with_complete", [True, False])
def test_due_waits_after_failure(tmp_path, service, with_complete):
    """失敗直後（1 時間以内）は、★のずれの印・初回でも取り直さない。待ち時間を過ぎたら取り直す。"""
    p = tmp_path / "t.json"
    data = {service: _stamp(minutes=30), f"{service}_last_ok": False, f"{service}_unresolved_total": None,
            f"{service}_stars_drift": True}
    if with_complete:
        data[f"{service}_complete"] = _stamp(days=30)
    _write_state(p, data)
    assert not rs.full_traversal_due(p, service)
    data[service] = _stamp(hours=2)
    _write_state(p, data)
    assert rs.full_traversal_due(p, service)


@pytest.mark.parametrize("service", SERVICES)
def test_due_true_without_complete(tmp_path, service):
    p = tmp_path / "t.json"
    assert rs.full_traversal_due(p, service)  # 記録のファイルが無い
    _write_state(p, {"scoresaber_complete" if service == "beatleader" else "beatleader_complete": _stamp(days=1)})
    assert rs.full_traversal_due(p, service)  # もう一方のサービスの完了は関係ない


# ---------------- ScoreSaber（取得処理を通す） ----------------

def _ss_old_star_api():
    maps = base_maps()
    return maps, FakeApi([dict(m, stars=7.5) if m["id"] == 110 else m for m in maps])  # 110 は窓の外（200 日前）


@pytest.mark.parametrize("days", [15, 100])
def test_ss_no_full_traversal_long_after_complete(tmp_path, days):
    maps, api = _ss_old_star_api()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    mark_full(tmp_path, days_ago=days)
    before = _state(tmp_path)["scoresaber_complete"]
    _, saved = run(tmp_path, api, 1)
    assert saved["leaderboards"]["110"]["stars"] == 5.0, "期間だけで全件取り直した"
    assert api.calls <= 3, f"全件取り直しになった calls={api.calls}"
    assert _state(tmp_path)["scoresaber_complete"] == before


def test_ss_star_drift_flag_forces_full_traversal(tmp_path):
    maps, api = _ss_old_star_api()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    mark_full(tmp_path, days_ago=2, drift=True)
    _, saved = run(tmp_path, api, 1)
    assert saved["leaderboards"]["110"]["stars"] == 7.5
    st = _state(tmp_path)
    assert "scoresaber_stars_drift" not in st
    assert st["scoresaber_complete"] > _stamp(minutes=5)


def test_ss_no_full_traversal_during_failure_backoff(tmp_path):
    maps, api = _ss_old_star_api()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    _write_state(tmp_path / "traversal.json", {
        "scoresaber": _stamp(minutes=30), "scoresaber_complete": _stamp(days=30), "scoresaber_last_ok": False,
        "scoresaber_unresolved_total": None, "scoresaber_stars_drift": True})
    _, saved = run(tmp_path, api, 1)
    assert saved["leaderboards"]["110"]["stars"] == 5.0, "失敗直後に全件取り直した"
    assert api.calls <= 3, f"失敗直後に全件取り直した calls={api.calls}"
    assert _state(tmp_path)["scoresaber_stars_drift"] is True


def test_ss_full_traversal_without_complete(tmp_path):
    maps, api = _ss_old_star_api()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    _write_state(tmp_path / "traversal.json", {})
    _, saved = run(tmp_path, api, 1)
    assert saved["leaderboards"]["110"]["stars"] == 7.5, "完了の記録が無いのに全件取得しなかった"
    assert _state(tmp_path)["scoresaber_complete"] > _stamp(minutes=5)


# ---------------- BeatLeader（取得処理を通す） ----------------

def _bl_old_star_api():
    maps = R.bl_base()
    api_maps = [json.loads(json.dumps(m)) for m in maps]
    api_maps[10]["difficulty"]["stars"] = 8.0  # 窓の外（200 日前）
    return maps, api_maps[10]["id"], R.BlFake(api_maps)


@pytest.mark.parametrize("days", [15, 100])
def test_bl_no_full_traversal_long_after_complete(tmp_path, days):
    maps, lid, api = _bl_old_star_api()
    R.bl_write(tmp_path, maps, 1)
    R.bl_mark(tmp_path, days)
    before = _state(tmp_path)["beatleader_complete"]
    got, _ = R.bl_run(tmp_path, api)
    assert got[lid]["difficulty"]["stars"] == 5.0, "期間だけで全件取り直した"
    assert api.calls <= 2, f"全件取り直しになった calls={api.calls}"
    assert _state(tmp_path)["beatleader_complete"] == before


def test_bl_star_drift_flag_forces_full_traversal(tmp_path):
    maps, lid, api = _bl_old_star_api()
    R.bl_write(tmp_path, maps, 1)
    R.bl_mark(tmp_path, 2, drift=True)
    got, _ = R.bl_run(tmp_path, api)
    assert got[lid]["difficulty"]["stars"] == 8.0
    st = _state(tmp_path)
    assert "beatleader_stars_drift" not in st
    assert st["beatleader_complete"] > _stamp(minutes=5)


def test_bl_no_full_traversal_during_failure_backoff(tmp_path):
    maps, lid, api = _bl_old_star_api()
    R.bl_write(tmp_path, maps, 1)
    _write_state(tmp_path / "traversal.json", {
        "beatleader": _stamp(minutes=30), "beatleader_complete": _stamp(days=30), "beatleader_last_ok": False,
        "beatleader_unresolved_total": None, "beatleader_stars_drift": True})
    got, _ = R.bl_run(tmp_path, api)
    assert got[lid]["difficulty"]["stars"] == 5.0, "失敗直後に全件取り直した"
    assert api.calls <= 2, f"失敗直後に全件取り直した calls={api.calls}"
    assert _state(tmp_path)["beatleader_stars_drift"] is True


def test_bl_full_traversal_without_complete(tmp_path):
    maps, lid, api = _bl_old_star_api()
    R.bl_write(tmp_path, maps, 1)
    _write_state(tmp_path / "traversal.json", {})
    got, _ = R.bl_run(tmp_path, api)
    assert got[lid]["difficulty"]["stars"] == 8.0, "完了の記録が無いのに全件取得しなかった"
    assert _state(tmp_path)["beatleader_complete"] > _stamp(minutes=5)


# ---------------- 総数の食い違いのまま終わった後の待ち時間 ----------------

@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("with_complete", [True, False])
def test_due_waits_after_unresolved_mismatch(tmp_path, service, with_complete):
    """全件取得しても総数が合わなかった後は、★のずれ・初回でも 1 回目は 24 時間、同じ総数で 2 回続いたら 14 日待つ。"""
    p = tmp_path / "t.json"
    data = {f"{service}_last_ok": True, f"{service}_unresolved_total": 50, f"{service}_unresolved_count": 1,
            f"{service}_stars_drift": True}
    if with_complete:
        data[f"{service}_complete"] = _stamp(days=30)
    for hours, count, due in ((3, 1, False), (25, 1, True), (25, 2, False), (24 * 13, 2, False), (24 * 15, 2, True)):
        data[service] = _stamp(hours=hours)
        data[f"{service}_unresolved_count"] = count
        _write_state(p, data)
        assert rs.full_traversal_due(p, service) is due, (hours, count)
