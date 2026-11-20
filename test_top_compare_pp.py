"""Offline regressions for the agreed top/compare layout."""
import json
import sys
from contextlib import ExitStack
from dataclasses import fields, replace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).parent / 'src'))
from mybeatsaberstats import player_app as pa, snapshot_view as sv, beatleader as bl
from mybeatsaberstats.collector import collector as c
from mybeatsaberstats.player_pp import PlayerPPPanel, pp_limits
from mybeatsaberstats.snapshot import Snapshot, StarClearStat
from test_player_pp import snapshot

ROOT = Path(__file__).parent


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def isolate_ui(stack, tmp_path):
    stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('network forbidden')))
    stack.enter_context(patch.object(pa, 'SNAPSHOT_DIR', tmp_path / 'snapshots'))
    stack.enter_context(patch.object(sv, 'SNAPSHOT_DIR', tmp_path / 'snapshots'))
    stack.enter_context(patch.object(pa.PlayerWindow, '_cache_dir', lambda s: tmp_path / 'cache'))
    stack.enter_context(patch.object(sv.SnapshotCompareDialog, '_settings_path', lambda s: tmp_path / 'compare.json'))
    stack.enter_context(patch.object(pa.EventsPanel, '_fetch', lambda s: None))
    stack.enter_context(patch.object(pa.StartupUpdateChecker, 'start', lambda s: None))
    stack.enter_context(patch.object(pa.PlayerAvatarWidget, 'set_ids', lambda *a, **k: None))
    stack.enter_context(patch.object(pa.RemotePixmapLabel, 'set_image_url', lambda *a, **k: None))
    stack.enter_context(patch.object(pa.PlayerWindow, '_start_title_icon_download', lambda *a: None))


def test_saved_response_collector_reload_top(app, tmp_path):
    """Actual parser -> collector -> JSON -> reload -> top panel, no network."""
    # Minimal subset of the saved 2026-11-18 player API response from #334.
    payload = json.loads((ROOT / 'tests/fixtures/beatleader-player-pp.json').read_text(encoding='utf-8'))
    response = Mock(status_code=200)
    response.json.return_value = payload
    with ExitStack() as stack:
        isolate_ui(stack, tmp_path)
        stack.enter_context(patch.object(c, 'CACHE_DIR', tmp_path / 'cache'))
        stack.enter_context(patch.object(bl, 'get_memoized', return_value=response))
        stack.enter_context(patch.object(bl, '_resolve_prestige_icon_url', return_value=None))
        stack.enter_context(patch.object(c, '_get_beatleader_player_scores', return_value=[]))
        stack.enter_context(patch.object(c, '_get_beatleader_player_stats', return_value={}))
        c._save_player_index({payload['id']: {'beatleader': bl.BeatLeaderPlayer(payload['id'], 'Buri', 'JP', 1, 1, 1)}})
        legacy = snapshot(beatleader_name='Buri', taken_at='2026-11-17T00:00:00Z')
        path = legacy.save(tmp_path / 'snapshots/legacy.json')
        before = path.read_bytes()
        w = pa.PlayerWindow()
        try:
            assert w.bl_pp_table.item(0, 1).text() == '—'
            options = c.SnapshotOptions(**{f.name: False for f in fields(c.SnapshotOptions) if f.name.startswith('fetch_')})
            options.fetch_beatleader = True
            created = c._create_snapshot_for_steam_id(payload['id'], options=options, snapshot_dir=tmp_path / 'snapshots')
            loaded = Snapshot.load(next(p for p in (tmp_path / 'snapshots').glob('*.json') if p != path))
            assert [getattr(loaded, f'beatleader_{name}_pp') for name in ('pass', 'tech', 'acc')] == [payload['passPp'], payload['techPp'], payload['accPp']]
            w.reload_snapshots()
            assert w.snapshot_combo.currentData() == created.taken_at
            assert [w.bl_pp_table.item(r, 1).text() for r in range(3)] == ['10,700.80', '783.72', '1,188.78']
            w.snapshot_combo.setCurrentIndex(w.snapshot_combo.findData(legacy.taken_at))
            assert w.bl_pp_table.item(0, 1).data(Qt.ItemDataRole.UserRole) is None
            assert path.read_bytes() == before
        finally:
            w.close()


@pytest.mark.parametrize('date', ['2026-10-26T00:00:00Z', '2026-10-27T16:00:00Z', '2026-11-05T00:00:00Z', None])
def test_scale_is_latest_for_any_date(date):
    # 撮影日で基準を切り替えない。トップ・比較とも最新の基準（Pass / Tech / Acc）
    assert pp_limits(snapshot(taken_at=date) if date else None) == (3000, 1200, 14500)


def test_panel_missing_zero_limits_and_colors(app):
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem
    from mybeatsaberstats import theme
    original_palette, original_stylesheet, original_dark = app.palette(), app.styleSheet(), theme._dark_mode
    table = PlayerPPPanel(None, pa.PercentageBarDelegate)
    table.resize(280, table.height())
    table.show()
    try:
        for dark in (True, False):
            (theme.apply_dark if dark else theme.apply_light)(app)
            for ratio, rgb in ((0, (255, 0, 0)), (.5, (255, 255, 0)), (.8, (0, 255, 0)), (1, (0, 255, 127)), (1.2, (0, 255, 127))):
                table.set_snapshots(snapshot(beatleader_pass_pp=3000*ratio, beatleader_tech_pp=1200*ratio, beatleader_acc_pp=14500*ratio))
                for row in range(3):
                    bar = table._bars[row]
                    index = table.model().index(row, 1)
                    assert bar._bar_value(index) / bar._max_value == ratio
                    canvas = QImage(220, 24, QImage.Format.Format_ARGB32)
                    canvas.fill(Qt.GlobalColor.white)
                    painter = QPainter(canvas)
                    option = QStyleOptionViewItem()
                    option.rect = canvas.rect()
                    option.palette = table.palette()
                    bar.paint(painter, option, index)
                    painter.end()
                    if ratio > 0:
                        # At x=2 the translucent bar is over the white background.
                        color = canvas.pixelColor(2, 2)
                        expected = tuple(round(v*180/255 + 75) for v in rgb)
                        assert all(abs(a-b) <= 2 for a,b in zip(color.getRgb()[:3], expected))
        table.set_snapshots(snapshot(beatleader_pass_pp=0))
        assert table.item(2, 1).text() == '0.00'
        assert table.item(1, 1).text() == '—'
        assert table.item(1, 1).data(Qt.ItemDataRole.UserRole) is None
    finally:
        table.close()
        app.setPalette(original_palette)
        app.setStyleSheet(original_stylesheet)
        theme._dark_mode = original_dark


def test_compare_click_preserves_values_missing_and_width(app, tmp_path):
    stats = [StarClearStat(i, 1, 1, 0, average_acc=90, avg_acc_left=89, avg_acc_right=91) for i in range(2, 16)]
    stats[1] = replace(stats[1], avg_acc_right=None)
    stats[2] = replace(stats[2], average_acc=None)
    a = snapshot(beatleader_name='Test', beatleader_star_stats=stats)
    b = replace(a, taken_at='2026-11-18T13:00:00Z', beatleader_star_stats=[replace(s, average_acc=None if s.average_acc is None else s.average_acc+1, avg_acc_left=90, avg_acc_right=93) for s in stats])
    a.save(tmp_path / 'snapshots/a.json'); b.save(tmp_path / 'snapshots/b.json')
    with ExitStack() as stack:
        isolate_ui(stack, tmp_path)
        d = sv.SnapshotCompareDialog(initial_steam_id=a.steam_id)
        try:
            d.resize(1540, 887); d.show(); QTest.qWait(60)
            d.combo_a.setCurrentIndex(1); d.combo_b.setCurrentIndex(0)
            original_a, original_b = a.beatleader_star_stats.copy(), b.beatleader_star_stats.copy()
            header = d.bl_star_table.horizontalHeader()
            compact_width = header.sectionSize(9)
            before = [(d.bl_star_table.item(r, 9).text() if d.bl_star_table.item(r, 9) else None) for r in range(d.bl_star_table.rowCount())]
            colors = [d.bl_star_table.item(r, 9).background() if d.bl_star_table.item(r, 9) else None for r in range(d.bl_star_table.rowCount())]
            # Existing star summaries substitute zero for missing overall averages.
            assert before[:3] == ['+1.00%', '+1.00%', '+0.00%']
            # Real header input, same as the user's click.
            d.bl_star_table.scrollToItem(d.bl_star_table.item(0, 9))
            QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton,
                             pos=QPoint(header.sectionViewportPosition(9)+5, 10))
            assert d._bl_acc_show_lr
            assert d.bl_star_table.item(0, 9).text() == '+1.00%(+1.00/+2.00)'
            assert d.bl_star_table.item(1, 9).text() == '+1.00%'
            assert header.sectionSize(9) > compact_width
            d._toggle_bl_acc_details(8)
            assert d._bl_acc_show_lr
            d._toggle_bl_acc_details(9)
            assert header.sectionSize(9) == compact_width
            assert before == [(d.bl_star_table.item(r, 9).text() if d.bl_star_table.item(r, 9) else None) for r in range(d.bl_star_table.rowCount())]
            assert colors == [d.bl_star_table.item(r, 9).background() if d.bl_star_table.item(r, 9) else None for r in range(d.bl_star_table.rowCount())]
            assert a.beatleader_star_stats == original_a and b.beatleader_star_stats == original_b
            d._toggle_bl_acc_details(9)
            d.combo_a.setCurrentIndex(0)
            assert d._bl_acc_show_lr and d.bl_star_table.item(0, 9).text() == '+0.00%(+0.00/+0.00)'
        finally:
            d.close()


def test_ranked_counts_denominators_bars_and_missing(app, tmp_path):
    stats_a = [StarClearStat(5, 6201, 1, 0)]
    stats_b = [StarClearStat(5, 6243, 1, 0)]
    a = snapshot(scoresaber_ranked_play_count=5606, beatleader_ranked_play_count=2913,
                 star_stats=stats_a, beatleader_star_stats=[StarClearStat(5, 3972, 1, 0)],
                 beatleader_acc_pp=10700.8, beatleader_tech_pp=783.72, beatleader_pass_pp=1188.78)
    b = replace(a, taken_at='2026-11-18T13:00:00Z', scoresaber_ranked_play_count=5639,
                beatleader_ranked_play_count=2914, star_stats=stats_b)
    a.save(tmp_path / 'snapshots/a.json'); b.save(tmp_path / 'snapshots/b.json')
    with ExitStack() as stack:
        isolate_ui(stack, tmp_path)
        d = sv.SnapshotCompareDialog(initial_steam_id=a.steam_id)
        try:
            d.resize(1540, 887); d.show(); QTest.qWait(60)
            d.combo_a.setCurrentIndex(next(i for i, s in enumerate(d._snapshots_by_player[a.steam_id]) if s.taken_at == a.taken_at))
            d.combo_b.setCurrentIndex(next(i for i, s in enumerate(d._snapshots_by_player[a.steam_id]) if s.taken_at == b.taken_at))
            def row(label):
                return next(r for r in range(d.table.rowCount()) if d.table.item(r, 1).text() == label)
            ss, bl = [r for r in range(d.table.rowCount()) if d.table.item(r, 1).text() == 'Ranked Play Count']
            for r, texts, ratios, diff in ((ss, ('5,606/6,201', '5,639/6,243'), (5606/6201, 5639/6243), '+33'),
                                             (bl, ('2,913/3,972', '2,914/3,972'), (2913/3972, 2914/3972), '+1')):
                for col, expected, ratio in zip((2, 3), texts, ratios):
                    item = d.table.item(r, col)
                    assert item.text() == expected
                    assert item.data(Qt.ItemDataRole.UserRole) == pytest.approx(ratio)
                    assert item.data(Qt.ItemDataRole.UserRole + 1) is True
                    assert d.table.columnWidth(col) >= d.table.fontMetrics().horizontalAdvance(expected) + 8
                assert d.table.item(r, 4).text() == diff
            pp_rows = [row(name) for name in ('Acc PP', 'Tech PP', 'Pass PP')]
            assert pp_rows == sorted(pp_rows)
            for r in pp_rows:
                assert '/' not in d.table.item(r, 2).text()
                assert '/' not in d.table.item(r, 3).text()
            # Missing count and absent denominator retain their established forms.
            missing = replace(a, scoresaber_ranked_play_count=None, beatleader_star_stats=[],
                              beatleader_ranked_play_count=0)
            d._snapshots_by_player[a.steam_id][d.combo_a.currentIndex()] = missing
            d._update_view2()
            assert d.table.item(ss, 2).text() == '—'
            assert d.table.item(ss, 2).data(Qt.ItemDataRole.UserRole) is None
            assert d.table.item(ss, 4).text() == '—'
            assert d.table.item(bl, 2).text() == '0'
            assert d.table.item(bl, 2).data(Qt.ItemDataRole.UserRole) is None
            d._snapshots_by_player[a.steam_id][d.combo_a.currentIndex()] = a
            for col, combo in ((2, d.combo_a), (3, d.combo_b)):
                for attr, r in (('scoresaber_ranked_play_count', ss), ('beatleader_ranked_play_count', bl)):
                    original = d._snapshots_by_player[a.steam_id][combo.currentIndex()]
                    d._snapshots_by_player[a.steam_id][combo.currentIndex()] = replace(original, **{attr: None})
                    d._update_view2()
                    assert d.table.item(r, col).text() == '—'
                    assert d.table.item(r, col).data(Qt.ItemDataRole.UserRole) is None
                    assert d.table.item(r, 4).text() == '—'
                    d._snapshots_by_player[a.steam_id][combo.currentIndex()] = original
        finally:
            d.close()
