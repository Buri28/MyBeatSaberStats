import json
import os
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_top_compare_pp import isolate_ui, pa
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from mybeatsaberstats import theme

out = ROOT / 'verification' / 'button-alignment'
out.mkdir(exist_ok=True)
data = out / 'offline'
(data / 'snapshots').mkdir(parents=True, exist_ok=True)
(data / 'cache').mkdir(exist_ok=True)
for name in ('76561198324870685_20261118-120800.json',
             '76561198324870685_20261118-121840.json',
             '76561198324870685_20261118-122702.json'):
    (data / 'snapshots' / name).write_bytes((ROOT / 'snapshots' / name).read_bytes())
(data / 'cache/player_window.json').write_bytes((ROOT / 'cache/player_window.json').read_bytes())
(out / 'process.json').write_text(json.dumps({'pid': os.getpid(), 'started': datetime.now().isoformat(),
                                               'executable': sys.executable}), encoding='utf-8')
app = QApplication([])
theme.apply_dark(app)
with ExitStack() as stack:
    isolate_ui(stack, data)
    w = pa.PlayerWindow()
    w._on_default_layout()
    w._events_panel._on_fetched(
        {'title': 'Accursed: Totality', 'slug': 'totality', 'startsAt': '2026-09-26T00:00:00Z', 'endsAt': '2026-11-09T00:00:00Z', 'active': True},
        {'name': 'Better Speed!', 'id': 1, 'endDate': 1795215600})
    w.show()
    def capture():
        buttons = [w._btn_rl_unplayed, w._btn_rl_campaigns, w._btn_rl_ms_progress]
        def rect(widget):
            p = widget.mapTo(w, widget.rect().topLeft())
            return [p.x(), p.y(), widget.width(), widget.height()]
        row = w.bl_pp_table.viewport().mapTo(w, w.bl_pp_table.visualItemRect(w.bl_pp_table.item(0, 0)).topLeft())
        result = {'window': [w.width(), w.height()], 'buttons': [rect(b) for b in buttons],
                  'pp': rect(w.bl_pp_table), 'first_row_top': row.y(),
                  'events': rect(w._events_scroll),
                  'pp_labels': [w.bl_pp_table.item(i, 0).text() for i in range(3)]}
        (out / 'geometry.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        w.grab().save(str(out / 'top-default.png'))
        w.close()
        app.quit()
    QTimer.singleShot(1200, capture)
    app.exec()
