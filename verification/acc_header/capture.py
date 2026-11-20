import sys, json
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
sys.path.insert(0, str(Path.cwd()))
from test_top_compare_pp import isolate_ui, pa, ROOT
from test_skill_resize import prepare_data
from mybeatsaberstats import theme
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
out=ROOT/'verification/acc_header'
data=out/'offline'; prepare_data(data)
app=QApplication([]); theme.apply_dark(app)
with ExitStack() as stack:
    isolate_ui(stack,data)
    stack.enter_context(patch.object(theme,'_PREF_PATH',data/'cache/theme.json'))
    w=pa.PlayerWindow(); w.setWindowTitle('AccSaber layout verification'); w.resize(1211,770); w.show()
    def capture():
        w.activateWindow(); w.raise_(); app.processEvents()
        def rect(x):
            p=x.mapToGlobal(x.rect().topLeft()); return [p.x(),p.y(),x.width(),x.height()]
        from PySide6.QtGui import QTextDocument
        d=QTextDocument(); d.setDefaultFont(w._acc_rl_xp_label.font()); d.setHtml(w._acc_rl_xp_label.text()); d.setDocumentMargin(0); d.adjustSize()
        info={'window':rect(w),'header':rect(w._acc_header),'xp':rect(w._acc_rl_xp_label),'xp_ideal_width':d.idealWidth(),'xp_text':w._acc_rl_xp_label.text(),'skill':rect(w._acc_header_limit),'button':rect(w._btn_rl_unplayed),'table':rect(w.acc_rl_table),'pp':rect(w.bl_pp_table),'dpr':w.devicePixelRatioF()}
        (out/'geometry.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
        app.primaryScreen().grabWindow(int(w.winId())).save(str(out/'default.png'))
        w.resize(1300,850); QTimer.singleShot(800,capture_second)
    def capture_second():
        app.primaryScreen().grabWindow(int(w.winId())).save(str(out/'wide.png'))
        w.close(); app.quit()
    QTimer.singleShot(1600,capture); app.exec()
