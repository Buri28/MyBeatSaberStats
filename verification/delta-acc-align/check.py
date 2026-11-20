"""比較画面の SS / BL ★表の列境界を測り、画面を撮る（データは複製、ネットワーク遮断）。
使い方: .venv/Scripts/python.exe verification/delta-acc-align/check.py <tag>
"""
import json, sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from test_top_compare_pp import isolate_ui, pa, sv
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from mybeatsaberstats import theme

tag = sys.argv[1]
out = Path(__file__).resolve().parent
data = out / 'offline'
(data / 'snapshots').mkdir(parents=True, exist_ok=True)
(data / 'cache').mkdir(exist_ok=True)
for fn in ('76561198324870685_20261118-121840.json', '76561198324870685_20261119-114733.json'):
    (data / 'snapshots' / fn).write_bytes((ROOT / 'snapshots' / fn).read_bytes())
(data / 'compare.json').write_bytes((ROOT / 'cache/snapshot_compare.json').read_bytes())
(data / 'cache/player_window.json').write_bytes((ROOT / 'cache/player_window.json').read_bytes())

app = QApplication([])
theme.apply_dark(app)
result = {}

def edges(t):
    h = t.horizontalHeader()
    x0 = t.viewport().mapTo(t.window(), t.viewport().rect().topLeft()).x()
    return [(c, x0 + h.sectionViewportPosition(c), x0 + h.sectionViewportPosition(c) + h.sectionSize(c))
            for c in range(t.columnCount()) if not t.isColumnHidden(c)]

def clipped(t):
    """見出し・値の文字幅が列幅を超える列を返す。"""
    bad = []
    fm = t.fontMetrics(); hfm = t.horizontalHeader().fontMetrics()
    for c in range(t.columnCount()):
        w = t.columnWidth(c)
        hi = t.horizontalHeaderItem(c)
        need = hfm.horizontalAdvance(hi.text()) + (20 if not hi.icon().isNull() else 0) if hi else 0
        for r in range(t.rowCount()):
            it = t.item(r, c)
            if it: need = max(need, fm.horizontalAdvance(it.text()))
        if need + 6 > w: bad.append((c, need, w))
    return bad

def step(dlg, name, width=None, lr=False):
    if width: dlg.resize(width, dlg.height())
    if lr != dlg._bl_acc_show_lr:
        dlg._toggle_bl_acc_details(9)
    app.processEvents()
    def shoot():
        ss, bl = edges(dlg.ss_star_table), edges(dlg.bl_star_table)
        mism = [(a, b) for a, b in zip(ss, bl) if a[1:] != b[1:]]
        png = out / f'{tag}_{name}.png'
        dlg.screen().grabWindow(dlg.winId()).save(str(png))
        result[name] = {'size': dlg.size().toTuple(), 'ss': ss, 'bl': bl, 'mismatch': mism,
                        'clip_ss': clipped(dlg.ss_star_table), 'clip_bl': clipped(dlg.bl_star_table), 'png': str(png)}
    return shoot

with ExitStack() as stack:
    isolate_ui(stack, data)
    stack.enter_context(patch.object(theme, '_PREF_PATH', data / 'cache/theme.json'))
    win = pa.PlayerWindow(); win.show()
    plan = [('default', None, False), ('narrow', 1250, False), ('wide', 1800, False), ('lr', 1540, True), ('back', 1540, False)]
    def run():
        dlgs = [w for w in app.topLevelWidgets() if isinstance(w, sv.SnapshotCompareDialog) and w.isVisible()]
        if not dlgs:
            QTimer.singleShot(300, run); return
        dlg = dlgs[0]
        def seq(i=0):
            if i == len(plan):
                (out / f'{tag}_geometry.json').write_text(json.dumps(result, indent=1), encoding='utf-8')
                dlg.close(); win.close(); app.quit(); return
            name, w, lr = plan[i]
            shoot = step(dlg, name, w, lr)
            QTimer.singleShot(800, lambda: (shoot(), seq(i + 1)))
        QTimer.singleShot(1500, seq)
    QTimer.singleShot(500, run)
    QTimer.singleShot(0, win.compare_button.click)
    app.exec()
