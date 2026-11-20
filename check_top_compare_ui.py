"""Launch the actual source UI against disposable offline fixtures.

Run: .venv/Scripts/python.exe check_top_compare_ui.py
The app uses normal UI controls; all HTTP requests are blocked.
"""
import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_top_compare_pp import isolate_ui, pa, sv, ROOT, Snapshot
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from mybeatsaberstats import theme

out = ROOT / 'verification/top-compare'
out.mkdir(parents=True, exist_ok=True)
data = out / 'offline'
(data / 'snapshots').mkdir(parents=True, exist_ok=True)
(data / 'cache').mkdir(exist_ok=True)
for filename in ('76561198324870685_20261118-120800.json',
                 '76561198324870685_20261118-121840.json',
                 '76561198324870685_20261118-122702.json'):
    (data / 'snapshots' / filename).write_bytes((ROOT / 'snapshots' / filename).read_bytes())
for filename in ('player_window.json', 'snapshot_compare.json'):
    dest = data / ('compare.json' if filename == 'snapshot_compare.json' else 'cache/player_window.json')
    dest.write_bytes((ROOT / 'cache' / filename).read_bytes())

app = QApplication([])
theme.apply_dark(app)
with ExitStack() as stack:
    isolate_ui(stack, data)
    stack.enter_context(patch.object(theme, '_PREF_PATH', data / 'cache/theme.json'))
    window = pa.PlayerWindow()
    window.resize(1211, 770)
    window.setWindowTitle(window.windowTitle() + ' [offline verification]')
    window._events_panel._on_fetched(
        {'title': 'Accursed: Totality', 'slug': 'totality', 'startsAt': '2026-09-26T00:00:00Z', 'endsAt': '2026-11-09T00:00:00Z', 'active': True},
        {'name': 'Better Speed!', 'id': 1, 'endDate': 1795215600})
    window.show()

    def report():
        def rect(w):
            return [*w.mapToGlobal(w.rect().topLeft()).toTuple(), *w.size().toTuple()]
        state = {'window': rect(window), 'dpr': window.devicePixelRatioF(),
                 'top_pp': [window.bl_pp_table.item(r, 1).text() for r in range(3)],
                 'pp': rect(window.bl_pp_table), 'buttons': rect(window._btn_rl_unplayed),
                 'events': rect(window._events_scroll),
                 'ss_star': rect(window.star_table), 'bl_star': rect(window.bl_star_table),
                 'pp_text_widths': [window.bl_pp_table.fontMetrics().horizontalAdvance(window.bl_pp_table.item(r, 1).text()) for r in range(3)],
                 'pp_value_width': window.bl_pp_table.columnWidth(1)}
        for w in app.topLevelWidgets():
            if isinstance(w, sv.SnapshotCompareDialog) and w.isVisible():
                state['compare'] = {'rect': rect(w), 'lr': w._bl_acc_show_lr,
                                    'acc_width': w.bl_star_table.columnWidth(9),
                                    'acc_header': rect(w.bl_star_table.horizontalHeader()),
                                    'acc_header_x': w.bl_star_table.horizontalHeader().sectionViewportPosition(9),
                                    'value': w.bl_star_table.item(0, 9).text()}
        (out / 'geometry.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
    timer = QTimer(); timer.timeout.connect(report); timer.start(700)
    app.exec()
