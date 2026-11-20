import sys; sys.path.insert(0,'.'); sys.path.insert(0,'src')
from contextlib import ExitStack
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from test_top_compare_pp import isolate_ui, sv
app=QApplication([])
data=Path('artifacts/fix357/offline')
with ExitStack() as st:
    isolate_ui(st, data)
    d=sv.SnapshotCompareDialog(initial_steam_id='76561198324870685'); d.show(); QTest.qWait(1500)
    d.combo_b.setCurrentIndex(d.combo_b.findData('2026-11-20T11:36:06Z')); QTest.qWait(1500)
    print("RATIO rows", d.table.rowCount(), d.table.columnCount(), [d.table.item(r,1).text() if d.table.item(r,1) else None for r in range(d.table.rowCount())])
    for r in range(d.table.rowCount()):
        for col in (0,1):
            it=d.table.item(r,col)
            if it and ('PP' in it.text()):
                print('RATIO', r, col, it.text(), [(d.table.item(r,c).text(), d.table.item(r,c).data(Qt.ItemDataRole.UserRole)) for c in (2,3)])
    d.close()
