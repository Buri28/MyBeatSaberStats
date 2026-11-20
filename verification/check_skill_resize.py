"""Visible offline source UI; normal window controls, no network or real writes."""
import sys
import os
import json
from datetime import datetime
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_skill_resize import prepare_data, measurements
from test_top_compare_pp import isolate_ui, ROOT, pa, sv
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from mybeatsaberstats import theme

out = ROOT / 'verification/skill-resize'
data = out / 'offline'
prepare_data(data)
(out / 'process.json').write_text(json.dumps(dict(pid=os.getpid(), started=datetime.now().isoformat(),
                                                executable=sys.executable), indent=2), encoding='utf-8')
app = QApplication([])
theme.apply_dark(app)
with ExitStack() as stack:
    isolate_ui(stack, data)
    stack.enter_context(patch.object(theme, '_PREF_PATH', data / 'cache/theme.json'))
    w = pa.PlayerWindow()
    w.setWindowTitle('Stats [skill resize verification]')
    w.resize(1211, 770)
    w.show()
    d = sv.SnapshotCompareDialog(initial_steam_id='76561198324870685')
    d.setWindowTitle('Compare [skill resize verification]')
    d.resize(1540, 887)
    d.show()
    history = []
    last = None
    stable = 0

    def record():
        global last, stable
        if not d.isVisible():
            return
        current = dict(size=d.size().toTuple(), metric_area=d._metric_vsplitter.height(),
                       splitter=d._metric_vsplitter.sizes(), tables=measurements(d),
                       font=d.table.font().toString(), lr=d._bl_acc_show_lr)
        if current != last:
            last = current
            stable = 0
        else:
            stable += 1
        if stable == 2:
            name = f'compare-{len(history):02d}-{d.width()}x{d.height()}'
            current = dict(current, image=name+'.png')
            d.grab().save(str(out / current['image']))
            history.append(current)
            (out / 'geometry.json').write_text(json.dumps(history, indent=2), encoding='utf-8')
            print(name, current['tables']['table']['rows'][0], flush=True)

    timer = QTimer()
    timer.timeout.connect(record)
    timer.start(250)
    def top_capture():
        w.grab().save(str(out / 'top.png'))
        pp = w.bl_pp_table.parentWidget()
        ev = w._events_scroll.parentWidget()
        assert abs(pp.width()-ev.width()) <= 2
        (out / 'top-geometry.json').write_text(json.dumps(dict(size=w.size().toTuple(),
            pp_width=pp.width(), events_width=ev.width(),
            gap=pp.x()-(w._btn_rl_unplayed.x()+w._btn_rl_unplayed.width()),
            rows=[[w.bl_pp_table.item(r,c).text() for c in range(2)] for r in range(3)]), indent=2), encoding='utf-8')
    QTimer.singleShot(1200, top_capture)
    app.exec()
    print('NORMAL EXIT', flush=True)
