from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from test_top_compare_pp import isolate_ui, sv, ROOT
from mybeatsaberstats import theme
out = ROOT / 'verification/ranked-count-338'
out.mkdir(parents=True, exist_ok=True)
data = out / 'offline'
(data / 'snapshots').mkdir(parents=True, exist_ok=True)
for name in ('76561198324870685_20261118-121840.json', '76561198324870685_20261118-122702.json'):
    (data / 'snapshots' / name).write_bytes((ROOT / 'snapshots' / name).read_bytes())
(data / 'compare.json').write_bytes((ROOT / 'cache/snapshot_compare.json').read_bytes())
app = QApplication([])
theme.apply_dark(app)
with ExitStack() as stack:
    isolate_ui(stack, data)
    dialog = sv.SnapshotCompareDialog(initial_steam_id='76561198324870685')
    dialog.resize(1540, 887)
    dialog.show()
    def capture():
        dialog.grab().save(str(out / 'compare-saved.png'))
        dialog.close()
        app.quit()
    QTimer.singleShot(1500, capture)
    app.exec()
