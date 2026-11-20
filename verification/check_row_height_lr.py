"""比較画面の▲▼を実ボタン(QTest.mouseClick)で操作し、左右の実高さを別々に記録する。"""
import sys, tempfile
from contextlib import ExitStack
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
import test_row_height_restore as T
out = ROOT / 'verification' / 'row-height-lr'
for name, seq in (('up1-dn1', 'ud'), ('dn1-up1', 'du'), ('roundtrips', 'uuuddduuddud')):
    with ExitStack() as st:
        d = T._open(Path(tempfile.mkdtemp()), st, (1540, 887))
        def snap(tag):
            l, r = T._lr(d); QTest.qWait(300)
            sb = T._state(d)[1]
            d.grab().save(str(out / f'{name}-{tag}.png'))
            print(name, tag, 'LEFT', l, 'RIGHT', r, 'vscroll', sb, 'manual', d._manual_row_height)
        snap('0start')
        for i, c in enumerate(seq):
            QTest.mouseClick(d.btn_row_height_up if c == 'u' else d.btn_row_height_dn, Qt.MouseButton.LeftButton)
            if i == 0: snap('1first')
            if name == 'roundtrips' and i == 2: snap('2after-uuu')
        snap('3end')
        d.close()
