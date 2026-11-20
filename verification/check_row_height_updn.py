"""比較画面の▲▼を実ボタンのクリックで操作し、行高・スクロールバー・画像を記録する。
使い方: python verification/check_row_height_updn.py <label>"""
import sys, tempfile
from contextlib import ExitStack
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
import test_row_height_restore as T

label = sys.argv[1]
out = ROOT / 'verification' / 'row-height-updn'; out.mkdir(exist_ok=True)
for name, seq in (('up1-dn1', 'ud'), ('dn1-up1', 'du'), ('roundtrips', 'uuuddduuddud')):
    with ExitStack() as st:
        d = T._open(Path(tempfile.mkdtemp()), st, (1540, 887))
        QTest.qWait(500)
        def snap(tag):
            QTest.qWait(500)
            h, sb = T._state(d)
            d.grab().save(str(out / f'{label}-{name}-{tag}.png'))
            print(label, name, tag, 'row', h['table'], 'scrollbar', sb['table'], sb['table_acc'], 'manual', d._manual_row_height)
        snap('0start')
        for i, c in enumerate(seq):
            b = d.btn_row_height_up if c == 'u' else d.btn_row_height_dn
            QTest.mouseClick(b, Qt.MouseButton.LeftButton)
            if i == 0: snap('1after-first')
        snap('2end')
        d.close()
