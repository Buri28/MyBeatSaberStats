import sys, json, os
from pathlib import Path
from contextlib import ExitStack
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_top_compare_pp import isolate_ui, ROOT, sv
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from mybeatsaberstats import theme
out = ROOT / 'verification/row-restore'; out.mkdir(exist_ok=True)
data = out / 'offline'
(data / 'snapshots').mkdir(parents=True, exist_ok=True); (data / 'cache').mkdir(exist_ok=True)
for name in ('76561198324870685_20261118-121840.json', '76561198324870685_20261118-122702.json'):
    (data / 'snapshots' / name).write_bytes((ROOT / 'snapshots' / name).read_bytes())
(data / 'compare.json').write_bytes((ROOT / 'cache/snapshot_compare.json').read_bytes())
app = QApplication([]); theme.apply_dark(app)
with ExitStack() as stack:
    isolate_ui(stack, data)
    d = sv.SnapshotCompareDialog(initial_steam_id='76561198324870685')
    d.resize(1540, 887); d.show()
    def rows(): return {n: getattr(d, n).rowHeight(0) for n in ('table','table_acc','ss_star_table','bl_star_table','acc_cmp_table')}
    def shot(n): d.grab().save(str(out / (n + '.png')))
    def check():
        log = {}
        QTest.qWait(300); log['initial'] = rows(); log['initial_fit'] = log['initial']['table']; shot('initial')
        for _ in range(8): d._on_row_height_up()
        QTest.qWait(100); log['up8'] = rows(); shot('up8')
        for _ in range(8): d._on_row_height_dn()
        QTest.qWait(100); log['up8_dn8'] = rows(); shot('up8_dn8'); assert log['up8_dn8']['table'] == log['initial_fit'], log
        d._on_default_layout(); QTest.qWait(100)
        d.resize(1540, 1200); QTest.qWait(200); log['big'] = rows()
        d.resize(1540, 887); QTest.qWait(200); log['big_back'] = rows()
        d.resize(1000, 600); QTest.qWait(200); log['small'] = rows()
        d.resize(1540, 887); QTest.qWait(200); log['small_back'] = rows(); shot('small_back')
        print(json.dumps(log, indent=1), flush=True); d.close()
    QTimer.singleShot(1500, check); QTimer.singleShot(60000, d.close)
    app.exec()
