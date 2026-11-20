import sys,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,'src')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from mybeatsaberstats import player_app as pa,snapshot_view as sv,theme
root=Path.cwd();out=root/'artifacts/pp-layout-fix';trial=out/'dist/MyBeatSaberStats'
app=QApplication([]);theme.apply_dark(app)
patches=[patch.object(pa,'SNAPSHOT_DIR',trial/'snapshots'),patch.object(sv,'SNAPSHOT_DIR',trial/'snapshots'),patch.object(pa.PlayerWindow,'_settings_path',lambda s:out/'harness-top.json'),patch.object(sv.SnapshotCompareDialog,'_settings_path',lambda s:out/'harness-compare.json'),patch.object(pa.EventsPanel,'_fetch',lambda s:None),patch.object(pa.StartupUpdateChecker,'start',lambda s:None)]
for p in patches:p.start()
c=sv.SnapshotCompareDialog(initial_steam_id='76561198324870685');c.resize(1540,887);c.show()
def report():
 print('window',c.size().toTuple(),'font',c.fontMetrics().height(),'split',c._metric_vsplitter.height(),c._metric_vsplitter.sizes())
 for name in ('table','table_acc','ss_star_table','bl_star_table'):
  t=getattr(c,name);print(name,'rows',t.rowCount(),'rowheight',t.rowHeight(0),'viewport',t.viewport().size().toTuple(),'size',t.size().toTuple(),'parent',t.parentWidget().size().toTuple(),'hdr',t.horizontalHeader().height(),'vscroll',t.verticalScrollBar().maximum(),'hscroll',t.horizontalScrollBar().isVisible(),t.horizontalScrollBar().height())
 for r in range(c.table.rowCount()-3,c.table.rowCount()):print(r,[c.table.item(r,i).text() if c.table.item(r,i) else None for i in range(1,5)])
 c.grab().save(str(out/'harness-compare-normal.png'));c.close();app.quit()
QTimer.singleShot(1200,report);app.exec()
