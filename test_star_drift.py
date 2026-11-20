"""窓の外の★のずれ（実例：1819224 の 14.15→14.2、702820 の 14.25→14.2）を、最近のスコアから検知して全件取り直す。"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent / "src"))
from mybeatsaberstats.collector import beatleader as bl  # noqa: E402
from mybeatsaberstats.collector import scoresaber as ss  # noqa: E402
import test_ranked_sync as R  # noqa: E402
import test_ss_ranked_cases as T  # noqa: E402
from test_ss_ranked_cases import FakeApi, base_maps, mark_full, run, write_cache  # noqa: E402


def _state(tmp):
    return json.loads((tmp / "traversal.json").read_text(encoding="utf-8"))


def _ss_score_cache(tmp, lid, stars, played):
    item = {"score": {"id": lid, "pp": 1.0, "timeSet": played.strftime("%Y-%m-%dT%H:%M:%S.000Z")},
            "leaderboard": {"id": lid, "ranked": True, "stars": stars}}
    (tmp / "scoresaber_player_scores_1.json").write_text(
        json.dumps({"fetched_at": "2026-11-18T00:00:00Z", "total_play_count": 1, "scores": {str(lid): item}}),
        encoding="utf-8")


def _ss_refresh(tmp):
    with mock.patch.object(ss, "CACHE_DIR", tmp), mock.patch.object(ss, "_RANKED_FULL_TRAVERSAL_STATE", tmp / "traversal.json"):
        return ss.refresh_scores_of_newly_ranked_maps("1", None)


def test_ss_star_drift_triggers_full_traversal(tmp_path):
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    mark_full(tmp_path, days_ago=2)  # ★のずれの印が付くまでは全件取り直さない
    api_maps = [dict(m, stars=14.2) if m["id"] == 110 else m for m in maps]
    _ss_score_cache(tmp_path, 110, 14.2, datetime.now(timezone.utc))  # 最近のプレイ：最新の★
    _, saved = run(tmp_path, FakeApi(api_maps), 1)
    assert saved["leaderboards"]["110"]["stars"] == 5.0  # 検知前は窓外なので古いまま（限界）
    _ss_refresh(tmp_path)
    assert _state(tmp_path)["scoresaber_stars_drift"] is True
    _, saved = run(tmp_path, FakeApi(api_maps), 1)
    assert saved["leaderboards"]["110"]["stars"] == 14.2, "★のずれで全件取り直さなかった"
    assert "scoresaber_stars_drift" not in _state(tmp_path), "取り直し後もフラグが残った"


def test_ss_star_same_or_old_play_no_flag(tmp_path):
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    mark_full(tmp_path, days_ago=2)
    _ss_score_cache(tmp_path, 110, 5.0, datetime.now(timezone.utc))  # 同じ★
    _ss_refresh(tmp_path)
    assert "scoresaber_stars_drift" not in _state(tmp_path)
    _ss_score_cache(tmp_path, 110, 9.0, datetime.now(timezone.utc) - timedelta(days=10))  # 全件取得より前のプレイ（スコア側が古いだけ）
    _ss_refresh(tmp_path)
    assert "scoresaber_stars_drift" not in _state(tmp_path)


def test_bl_star_drift_triggers_full_traversal(tmp_path):
    maps = R.bl_base()
    R.bl_write(tmp_path, maps, 1)
    R.bl_mark(tmp_path, 2)
    api_maps = [json.loads(json.dumps(m)) for m in maps]
    api_maps[10]["difficulty"]["stars"] = 14.2
    lid = api_maps[10]["id"]
    score = {"timeset": str(int(datetime.now(timezone.utc).timestamp())),
             "leaderboard": {"id": lid, "difficulty": {"status": 3, "stars": 14.2}}}
    (tmp_path / "beatleader_player_scores_1.json").write_text(
        json.dumps({"fetched_at": "2026-11-18T00:00:00Z", "scores": {lid: score}}), encoding="utf-8")
    got, _ = R.bl_run(tmp_path, R.BlFake(api_maps))
    assert got[lid]["difficulty"]["stars"] == 5.0
    with mock.patch.object(bl, "CACHE_DIR", tmp_path), mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", tmp_path / "traversal.json"):
        bl.refresh_scores_of_newly_ranked_maps("1", None)
    assert _state(tmp_path)["beatleader_stars_drift"] is True
    got, _ = R.bl_run(tmp_path, R.BlFake(api_maps))
    assert got[lid]["difficulty"]["stars"] == 14.2
    assert "beatleader_stars_drift" not in _state(tmp_path)
