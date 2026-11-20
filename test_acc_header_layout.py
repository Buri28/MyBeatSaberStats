from contextlib import ExitStack
from PySide6.QtTest import QTest
from PySide6.QtGui import QTextDocument
from test_top_compare_pp import app, isolate_ui, pa
from test_skill_resize import prepare_data

def test_header_fits_saved_data_and_tracks_splitter(app, tmp_path):
    prepare_data(tmp_path)
    with ExitStack() as stack:
        isolate_ui(stack, tmp_path)
        w = pa.PlayerWindow()
        try:
            w.show()
            for width, height in ((1211, 770), (1300, 850)):
                w.resize(width, height)
                QTest.qWait(150)
                assert '145/267' in w._acc_rl_xp_label.text()
                doc = QTextDocument()
                doc.setDefaultFont(w._acc_rl_xp_label.font())
                doc.setDocumentMargin(0)
                doc.setHtml(w._acc_rl_xp_label.text())
                doc.setTextWidth(-1)
                assert doc.idealWidth() + 4 <= w._acc_rl_xp_label.width()
                header = w._acc_header.geometry()
                skill = w._acc_header_limit.mapTo(w._bottom_container, w._acc_header_limit.rect().topLeft())
                button = w._btn_rl_unplayed.mapTo(w._bottom_container, w._btn_rl_unplayed.rect().topLeft())
                assert header.right() < skill.x()
                assert header.bottom() < button.y()
                assert abs(button.y() - (w.bl_pp_table.mapTo(w._bottom_container, w.bl_pp_table.rect().topLeft()).y() + w.bl_pp_table.frameWidth())) <= 1
            w._bottom_h_splitter.moveSplitter(600, 1)
            QTest.qWait(100)
            assert w._acc_header.geometry().right() < w._acc_header_limit.mapTo(w._bottom_container, w._acc_header_limit.rect().topLeft()).x()
        finally:
            w.close()
