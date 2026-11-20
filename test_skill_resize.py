"""Offline regressions for skill ordering and reversible comparison sizing."""
from contextlib import ExitStack

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from test_top_compare_pp import app, isolate_ui, ROOT, pa, sv
from test_player_pp import snapshot
from mybeatsaberstats.player_pp import PP_FIELDS, pp_limits_by_field
from mybeatsaberstats import theme


def prepare_data(data):
    (data / 'snapshots').mkdir(parents=True, exist_ok=True)
    (data / 'cache').mkdir(exist_ok=True)
    for name in ('76561198324870685_20261118-121840.json',
                 '76561198324870685_20261118-122702.json'):
        (data / 'snapshots' / name).write_bytes((ROOT / 'snapshots' / name).read_bytes())
    for source, dest in (('snapshot_compare.json', 'compare.json'),
                         ('player_window.json', 'cache/player_window.json')):
        (data / dest).write_bytes((ROOT / 'cache' / source).read_bytes())


def measurements(d):
    return {name: {'rows': [getattr(d, name).rowHeight(r)
                            for r in range(getattr(d, name).rowCount())],
                   'viewport': getattr(d, name).viewport().size().toTuple(),
                   'scroll': getattr(d, name).verticalScrollBar().maximum(),
                   'minimum': getattr(d, name).minimumSize().toTuple()}
            for name in ('table', 'table_acc', 'ss_star_table', 'bl_star_table', 'acc_cmp_table')}


@pytest.mark.parametrize('dark', [True, False])
def test_resize_roundtrip_and_splitter_only(app, tmp_path, dark):
    prepare_data(tmp_path)
    with ExitStack() as stack:
        isolate_ui(stack, tmp_path)
        (theme.apply_dark if dark else theme.apply_light)(app)
        d = sv.SnapshotCompareDialog(initial_steam_id='76561198324870685')
        try:
            d.show()
            font = d.table.font().toString()
            results = []
            for width, height in ((1540, 887), (1540, 1200), (1000, 650),
                                  (1540, 887), (1540, 1200), (1540, 887)):
                d.resize(width, height)
                # 行高の再調整はタイマーでまとめて行うので、2 回続けて同じ値になるまで待つ（80ms 固定だと測り始めが早く不安定）
                m = None
                for _ in range(20):
                    QTest.qWait(80)
                    cur = measurements(d)
                    if cur == m:
                        break
                    m = cur
                for name in ('ss_star_table', 'bl_star_table', 'acc_cmp_table'):
                    assert set(m[name]['rows']) == {21}
                assert d.table.rowHeight(0) == d.table_acc.rowHeight(0)
                assert d.table.font().toString() == font
                assert d.table.rowHeight(0) >= max(18, d.fontMetrics().height() + 2)
                for name in ('table', 'table_acc'):
                    t = getattr(d, name)
                    t.scrollToBottom()
                    app.processEvents()
                    assert t.visualItemRect(t.item(t.rowCount()-1, 2)).bottom() <= t.viewport().height()
                results.append(m)
            assert results[0] == results[3] == results[5]
            assert results[1] == results[4]
            assert results[1]['table']['rows'][0] > results[2]['table']['rows'][0]
            # Change only the available table area, leaving dialog dimensions alone.
            size = d.size()
            d._v_splitter.widget(0).setFixedHeight(300)
            QTest.qWait(80)
            short = measurements(d)
            d._v_splitter.widget(0).setFixedHeight(53)
            QTest.qWait(80)
            tall = measurements(d)
            assert d.size() == size
            assert short['table']['rows'][0] < tall['table']['rows'][0]
            assert set(tall['ss_star_table']['rows']) == {21}
            h0 = d.table.rowHeight(0)  # ▲▼は左右それぞれの実高さを基準に±1する
            r0 = d.ss_star_table.rowHeight(0)
            d._on_row_height_dn()
            QTest.qWait(50)
            assert d.table.rowHeight(0) == h0 - 1
            assert d.ss_star_table.rowHeight(0) == r0 - 1
            d._on_default_layout()
            QTest.qWait(80)
            assert d.ss_star_table.rowHeight(0) == 21
        finally:
            d.close()
            d.deleteLater()
            QTest.qWait(10)
            theme.apply_dark(app)


def test_component_mapping_and_title(app, tmp_path):
    prepare_data(tmp_path)
    with ExitStack() as stack:
        isolate_ui(stack, tmp_path)
        w = pa.PlayerWindow()
        d = sv.SnapshotCompareDialog(initial_steam_id='76561198324870685')
        try:
            assert any(label.text() == 'BeatLeader Skill' for label in w.findChildren(QLabel))
            assert [label for label, _ in PP_FIELDS] == ['Acc PP', 'Tech PP', 'Pass PP']
            for date in ('2026-10-26T00:00:00Z', '2026-10-28T00:00:00Z', '2026-11-18T00:00:00Z'):
                snap = snapshot(taken_at=date, beatleader_acc_pp=10000,
                                beatleader_tech_pp=600, beatleader_pass_pp=1500)
                w.bl_pp_table.set_snapshots(snap)
                for row, (label, field) in enumerate(PP_FIELDS):
                    item = w.bl_pp_table.item(row, 1)
                    assert w.bl_pp_table.item(row, 0).text() == label
                    assert item.data(Qt.ItemDataRole.UserRole) == getattr(snap, field)
                    # 撮影日に関係なく最新の基準。表示は値だけ（上限の数値は出さない）
                    assert w.bl_pp_table._bars[row]._max_value == {'beatleader_pass_pp': 3000,
                        'beatleader_tech_pp': 1200, 'beatleader_acc_pp': 14500}[field]
                    assert '/' not in item.text() and item.text() == f'{getattr(snap, field):,.2f}'
            a = d._current_snapshot(d.combo_a)
            b = d._current_snapshot(d.combo_b)
            from dataclasses import replace
            snaps = d._snapshots_by_player[a.steam_id]
            d.combo_a.setCurrentIndex(1)
            d.combo_b.setCurrentIndex(0)
            snaps[1] = replace(a, beatleader_acc_pp=7250, beatleader_tech_pp=600, beatleader_pass_pp=1500)
            snaps[0] = replace(b, beatleader_acc_pp=14500, beatleader_tech_pp=0, beatleader_pass_pp=None)
            d._update_view2()
            rows = [r for r in range(d.table.rowCount()) if d.table.item(r, 1).text() in dict(PP_FIELDS)]
            assert [d.table.item(r, 1).text() for r in rows] == ['Acc PP', 'Tech PP', 'Pass PP']
            assert [d.table.item(r, 4).text() for r in rows] == ['+7,250.00', '-600.00', '—']
            assert d.table.item(rows[0], 4).background().color() == theme.diff_positive_bg()
            assert d.table.item(rows[1], 4).background().color() == theme.diff_negative_bg()
            for row, ratio_a, ratio_b in zip(rows, (.5, .5, .5), (1., 0., None)):
                assert d.table.item(row, 2).data(Qt.ItemDataRole.UserRole) == ratio_a
                assert d.table.item(row, 3).data(Qt.ItemDataRole.UserRole) == ratio_b
                assert '/' not in d.table.item(row, 2).text()
        finally:
            d.close()
            w.close()
            d.deleteLater()
            w.deleteLater()
            QTest.qWait(10)
