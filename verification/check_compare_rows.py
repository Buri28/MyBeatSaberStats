import sys, json, os
from pathlib import Path
from contextlib import ExitStack
from datetime import datetime
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_top_compare_pp import isolate_ui, ROOT, sv
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from mybeatsaberstats import theme

out = ROOT / 'verification/compare-rows'
out.mkdir(exist_ok=True)
data = out / 'offline'
(data / 'snapshots').mkdir(parents=True, exist_ok=True)
(data / 'cache').mkdir(exist_ok=True)
for name in ('76561198324870685_20261118-121840.json', '76561198324870685_20261118-122702.json'):
    (data / 'snapshots' / name).write_bytes((ROOT / 'snapshots' / name).read_bytes())
(data / 'compare.json').write_bytes((ROOT / 'cache/snapshot_compare.json').read_bytes())
(out / 'process.json').write_text(json.dumps({'pid':os.getpid(), 'started':datetime.now().isoformat(), 'executable':sys.executable}), encoding='utf-8')
app = QApplication([])
theme.apply_dark(app)
with ExitStack() as stack:
    isolate_ui(stack, data)
    d = sv.SnapshotCompareDialog(initial_steam_id='76561198324870685')
    d.setWindowTitle('Snapshot Compare [row verification]')
    d.resize(1540,887)
    d.show()
    results = {}
    def capture(name):
        results[name] = {n: {'row':getattr(d,n).rowHeight(0), 'font':getattr(d,n).font().toString(), 'scroll':getattr(d,n).verticalScrollBar().maximum()} for n in ('table','table_acc','ss_star_table','bl_star_table')}
        d.grab().save(str(out / (name+'.png')))
    def check():
        capture('normal')
        assert d.ss_star_table.rowHeight(0) == d.bl_star_table.rowHeight(0) == 21
        assert d.table.rowHeight(0) == d.table_acc.rowHeight(0) < 21
        left = d.table.rowHeight(0)
        # #336 applied the left height to the stars too. Reproduce only for
        # comparing left geometry/fonts without changing the source under test.
        for t in (d.ss_star_table,d.bl_star_table): t.verticalHeader().setDefaultSectionSize(left)
        capture('before-right-restoration')
        d._apply_row_height(); app.processEvents()
        assert d.table.rowHeight(0) == left
        d._toggle_bl_acc_details(9); app.processEvents(); capture('details')
        assert d._bl_acc_show_lr
        d._toggle_bl_acc_details(9)
        d.resize(1000,650); QTest.qWait(100)
        for t in (d.table,d.table_acc,d.ss_star_table,d.bl_star_table):
            t.scrollToBottom(); app.processEvents()
            assert t.visualItemRect(t.item(t.rowCount()-1,2)).bottom() <= t.viewport().height()
        capture('small-bottom')
        d._on_row_height_dn()
        assert all(t.rowHeight(0)==20 for t in (d.table,d.table_acc,d.ss_star_table,d.bl_star_table))
        d._on_default_layout(); QTest.qWait(100)
        capture('default')
        assert d.ss_star_table.rowHeight(0)==21
        (out/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
        print('CHECKS PASSED',flush=True)
    QTimer.singleShot(1500, check)
    QTimer.singleShot(90000, d.close)
    app.exec()
    print('NORMAL EXIT',flush=True)
