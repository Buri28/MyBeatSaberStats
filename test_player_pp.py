import json
import sys
from dataclasses import MISSING, asdict, fields, replace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).parent / "src"))
from mybeatsaberstats import beatleader as bl
from mybeatsaberstats.collector import collector as c
from mybeatsaberstats.player_pp import PlayerPPTable
from mybeatsaberstats.snapshot import Snapshot
from mybeatsaberstats.theme import diff_positive_bg, diff_negative_bg, diff_neutral_bg


def snapshot(**updates):
    required = {f.name: None for f in fields(Snapshot) if f.default is MISSING
                and f.default_factory is MISSING}
    required.update(taken_at="2026-11-18T12:00:00Z", steam_id="76561198324870685")
    required.update(updates)
    return Snapshot(**required)


@pytest.mark.parametrize("value,expected", [(0, 0.0), (12.345, 12.345), (None, None),
    ("", None), ("invalid", None), (float('nan'), None), (float('inf'), None), (True, None)])
def test_api_top_level(value, expected):
    response = Mock(status_code=200)
    response.json.return_value = {"passPp": value, "techPp": 25.678, "accPp": 0,
                                  "scoreStats": {"passPp": 999}, "rating": {"techPp": 999}}
    with patch.object(bl, "get_memoized", return_value=response) as get:
        player = bl.fetch_player("test", session=Mock())
    assert player.pass_pp == expected
    assert player.tech_pp == 25.678
    assert player.acc_pp == 0.0
    # 指摘 3：統計・スコア取得と同じ URL（get_memoized で 1 回にまとまる）
    assert get.call_args.args[1] == "https://api.beatleader.com/player/test"
    response.json.return_value = {"scoreStats": {"passPp": 999}}
    with patch.object(bl, "get_memoized", return_value=response):
        assert bl.fetch_player("test", session=Mock()).pass_pp is None


def test_snapshot_roundtrip_and_legacy(tmp_path):
    snap = snapshot(beatleader_pass_pp=0.0, beatleader_tech_pp=12.3456)
    path = snap.save(tmp_path / "new.json")
    loaded = Snapshot.load(path)
    assert loaded.beatleader_pass_pp == 0.0
    assert loaded.beatleader_tech_pp == 12.3456
    assert loaded.beatleader_acc_pp is None
    raw = asdict(snap)
    for key in ("beatleader_pass_pp", "beatleader_tech_pp", "beatleader_acc_pp"):
        raw.pop(key)
    old = tmp_path / "old.json"
    old.write_text(json.dumps(raw), encoding="utf-8")
    before = old.read_bytes()
    legacy = Snapshot.load(old)
    assert legacy.beatleader_pass_pp is None
    assert legacy.beatleader_tech_pp is None
    assert old.read_bytes() == before


def test_fresh_profile_missing_does_not_use_cached_component(tmp_path):
    cached = bl.BeatLeaderPlayer("test", "Test", "JP", 100, 1, 1,
                                 pass_pp=50.0, tech_pp=60.0, acc_pp=70.0)
    fresh = replace(cached, pass_pp=None, tech_pp=0.0, acc_pp=80.12345)
    with patch.object(c, "CACHE_DIR", tmp_path), \
         patch.object(c, "fetch_bl_player", return_value=fresh), \
         patch.object(c, "_get_beatleader_player_scores", return_value=[]), \
         patch.object(c, "_get_beatleader_player_stats", return_value={}):
        c._save_player_index({"test": {"beatleader": cached}})
        options = c.SnapshotOptions(**{f.name: False for f in fields(c.SnapshotOptions)
                                      if f.name.startswith("fetch_")})
        options.fetch_beatleader = True
        result = c._create_snapshot_for_steam_id("test", options=options,
                                                 snapshot_dir=tmp_path / "snapshots")
        assert c._load_player_index()["test"]["beatleader"] == fresh
    loaded = Snapshot.load(next((tmp_path / "snapshots").glob("*.json")))
    assert loaded.beatleader_pass_pp is None
    assert loaded.beatleader_tech_pp == result.beatleader_tech_pp == 0.0
    assert loaded.beatleader_acc_pp == 80.12345


def test_player_index_and_collector_roundtrip(tmp_path):
    player = bl.BeatLeaderPlayer("test", "Test", "JP", 100, 1, 1,
                                 pass_pp=0.0, tech_pp=12.3456, acc_pp=None)
    with patch.object(c, "CACHE_DIR", tmp_path):
        c._save_player_index({"test": {"beatleader": player}})
        assert c._load_player_index()["test"]["beatleader"] == player
        options = c.SnapshotOptions(**{f.name: False for f in fields(c.SnapshotOptions)
                                      if f.name.startswith("fetch_")})
        result = c._create_snapshot_for_steam_id("test", options=options,
                                                 snapshot_dir=tmp_path / "snapshots")
    loaded = Snapshot.load(next((tmp_path / "snapshots").glob("*.json")))
    assert loaded.beatleader_pass_pp == result.beatleader_pass_pp == 0.0
    assert loaded.beatleader_tech_pp == 12.3456
    assert loaded.beatleader_acc_pp is None


def test_display_missing_zero_diff_and_refresh():
    app = QApplication.instance() or QApplication([])
    a = snapshot(beatleader_pass_pp=0.0, beatleader_tech_pp=25.0)
    b = snapshot(beatleader_pass_pp=10.125, beatleader_tech_pp=20.25, beatleader_acc_pp=3.0)
    top = PlayerPPTable()
    top.set_snapshots(a)
    assert [top.item(0, i).text() for i in range(3)] == ["—", "25.00", "0.00"]
    table = PlayerPPTable(compare=True)
    table.set_snapshots(a, b)
    assert table.item(2, 3).text() == "+10.12"
    assert table.item(1, 3).text() == "-4.75"
    assert table.item(0, 3).text() == "—"
    assert table.item(2, 3).background().color() == diff_positive_bg()
    assert table.item(1, 3).background().color() == diff_negative_bg()
    table.set_snapshots(b, a)
    assert table.item(0, 3).text() == "—"
    table.set_snapshots(snapshot(beatleader_pass_pp=1.001), snapshot(beatleader_pass_pp=1.009))
    assert table.item(2, 3).text() == "+0.00"
    assert table.item(2, 3).background().color() == diff_neutral_bg()
    table.set_snapshots(None)
    assert table.item(0, 1).text() == table.item(0, 3).text() == "—"
    top.close()
    table.close()
