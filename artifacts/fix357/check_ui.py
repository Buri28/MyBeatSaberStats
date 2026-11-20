"""#357 修正の画面確認。今のソースの PlayerWindow / SnapshotCompareDialog を実際に画面に出し、画面のピクセルを撮る。

- 通信はすべて止める（isolate_ui）。保存済みの Snapshot と設定のコピー（artifacts/fix357/offline）だけを使い、
  本物の cache/・snapshots/・logs/ には書かない。Snapshot 取得のボタンは押さない。
- 実行: .venv/Scripts/python.exe artifacts/fix357/check_ui.py
"""
import json
import os
import shutil
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
os.chdir(ROOT)

from PySide6.QtCore import Qt, QRect  # noqa: E402
from PySide6.QtGui import QTextDocument, QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem  # noqa: E402

from test_top_compare_pp import isolate_ui, pa, sv  # noqa: E402
from mybeatsaberstats import theme, http_client  # noqa: E402

OUT = ROOT / 'artifacts/fix357/screens'
DATA = ROOT / 'artifacts/fix357/offline'
SID = '76561198324870685'
SNAPS = ('76561198324870685_20261118-121840.json', '76561198324870685_20261118-122702.json',
         '76561198324870685_20261120-113606.json')

if DATA.exists():
    shutil.rmtree(DATA)
(DATA / 'snapshots').mkdir(parents=True)
(DATA / 'cache').mkdir()
OUT.mkdir(parents=True, exist_ok=True)
for name in SNAPS:
    shutil.copy2(ROOT / 'snapshots' / name, DATA / 'snapshots' / name)
shutil.copy2(ROOT / 'cache/player_window.json', DATA / 'cache/player_window.json')
shutil.copy2(ROOT / 'cache/snapshot_compare.json', DATA / 'compare.json')
for name in ('accsaber_reloaded_map_counts.json', 'scoresaber_ranked_maps.json', 'beatleader_ranked_maps.json'):
    if (ROOT / 'cache' / name).exists():
        shutil.copy2(ROOT / 'cache' / name, DATA / 'cache' / name)

app = QApplication.instance() or QApplication([])
theme.apply_dark(app)
report = {'pid': os.getpid(), 'started': datetime.now().isoformat(timespec='seconds'),
          'python': sys.executable, 'screens': []}


def grect(w):
    p = w.mapToGlobal(w.rect().topLeft())
    return QRect(p.x(), p.y(), w.width(), w.height())


def _print_window(hwnd):
    """Win32 PrintWindow(PW_RENDERFULLCONTENT) で、OS が描いた窓の中身を撮る（他の窓に覆われていても撮れる）。"""
    import ctypes
    from ctypes import wintypes
    from PySide6.QtGui import QImage
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    ok = user32.PrintWindow(hwnd, mdc, 2)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG), ('biHeight', wintypes.LONG),
                    ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                    ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', wintypes.LONG),
                    ('biYPelsPerMeter', wintypes.LONG), ('biClrUsed', wintypes.DWORD), ('biClrImportant', wintypes.DWORD)]
    bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp); gdi32.DeleteDC(mdc); user32.ReleaseDC(hwnd, hdc)
    img = QImage(buf.raw, w, h, w * 4, QImage.Format.Format_RGB32).copy()
    return img, bool(ok), (w, h)


def shot(win, name, label):
    win.raise_(); win.activateWindow()
    QTest.qWait(600)
    r = grect(win)
    img, ok, size = _print_window(int(win.winId()))
    path = OUT / f'{name}.png'
    img.save(str(path))
    report['screens'].append({'file': str(path), 'label': label, 'rect': r.getRect(), 'printwindow_ok': ok,
                              'physical_size': size})
    return path


def _unused(win, name, label):
    r = grect(win)
    pix = QGuiApplication.primaryScreen().grabWindow(0, r.x(), r.y(), r.width(), r.height())
    path = OUT / f'{name}.png'
    pix.save(str(path))
    report['screens'].append({'file': str(path), 'label': label, 'rect': r.getRect()})
    return path


def text_width(label):
    doc = QTextDocument()
    doc.setDefaultFont(label.font())
    doc.setHtml(label.text())
    return doc.idealWidth()


def check_top(w, tag):
    QTest.qWait(400)
    t = w.bl_pp_table
    rows = []
    for r in range(3):
        item = t.item(r, 1)
        opt = QStyleOptionViewItem()
        t._bars[r].initStyleOption(opt, t.model().index(r, 1))
        rows.append({'label': t.item(r, 0).text(), 'text': item.text(),
                     'text_px': t.fontMetrics().horizontalAdvance(item.text()),
                     'cell_px': t.columnWidth(1), 'drawn_text': opt.text, 'bold': opt.font.bold(),
                     'max': t._bars[r]._max_value, 'tooltip': item.toolTip()})
    hdr = w._acc_header_limit
    btns = [b for b in (w._btn_rl_unplayed, getattr(w, '_btn_rl_ms_progress', None)) if b is not None]
    btn_col = [grect(b) for b in w.findChildren(type(w._btn_rl_unplayed)) if b.isVisible()
               and b.parent() is w._btn_rl_unplayed.parent()]
    pp_r, hdr_r, acc_r = grect(t), grect(hdr), grect(w._acc_header)
    xp = w._acc_rl_xp_label
    info = {
        'tag': tag, 'window': grect(w).getRect(), 'rows': rows,
        'pp_header': (hdr.text(), hdr_r.getRect()), 'pp_table': pp_r.getRect(),
        'acc_header': acc_r.getRect(), 'acc_header_overlaps_pp_header': acc_r.intersects(hdr_r),
        'acc_header_overlaps_pp_table': acc_r.intersects(pp_r),
        'buttons': [(b.text(), b.getRect()) for b in []],
        'button_rects': [r.getRect() for r in btn_col],
        'buttons_overlap_pp': [r.getRect() for r in btn_col if r.intersects(pp_r) or r.intersects(hdr_r)],
        'xp_text': xp.toolTip(), 'xp_text_px': text_width(xp), 'xp_label_px': xp.width(),
        'xp_fits': text_width(xp) <= xp.width(),
    }
    report.setdefault('top', []).append(info)
    return info


with ExitStack() as stack:
    isolate_ui(stack, DATA)
    stack.enter_context(patch.object(theme, '_PREF_PATH', DATA / 'cache/theme.json'))
    # 念のため：http_client 経由のリクエストも止める（通信しない）
    stack.enter_context(patch.object(http_client.PoliteSession, 'request',
                                     side_effect=AssertionError('network forbidden')))
    w = pa.PlayerWindow()
    w.setWindowTitle(w.windowTitle() + ' [#357 offline check]')
    w.show(); QTest.qWait(800)
    idx = w.snapshot_combo.findData('2026-11-20T11:36:06Z')
    if idx >= 0:
        w.snapshot_combo.setCurrentIndex(idx)
    report['default_size'] = w.size().toTuple()
    check_top(w, 'default')
    shot(w, 'top_default', 'トップ画面（既定サイズ）')
    w.resize(max(w.minimumWidth(), 1000), max(w.minimumHeight(), 650))
    QTest.qWait(600)
    check_top(w, 'small')
    shot(w, 'top_small', 'トップ画面（小さめ）')
    w.resize(*report['default_size']); QTest.qWait(400)

    # 比較画面
    d = sv.SnapshotCompareDialog(w, initial_steam_id=SID)
    d.show(); QTest.qWait(800)
    d.combo_b.setCurrentIndex(d.combo_b.findData('2026-11-20T11:36:06Z')) if d.combo_b.findData('2026-11-20T11:36:06Z') >= 0 else None
    QTest.qWait(400)
    pp_rows = {}
    for r in range(d.table.rowCount()):
        labs = [d.table.item(r, cc).text() for cc in range(2) if d.table.item(r, cc)]
        lab = next((x for x in labs if x in ('Acc PP', 'Tech PP', 'Pass PP', '[BL] Acc PP', '[BL] Tech PP', '[BL] Pass PP')), '')
        if lab:
            pp_rows[lab] = [(d.table.item(r, c).text(), d.table.item(r, c).data(Qt.ItemDataRole.UserRole))
                            for c in (2, 3)]
    report['compare_pp_rows'] = pp_rows
    report['compare_default_size'] = d.size().toTuple()
    report['compare_row_height_before'] = d.table.rowHeight(0)
    shot(d, 'compare_default', '比較画面（既定サイズ）')
    d.resize(1000, 650); QTest.qWait(600)
    shot(d, 'compare_small', '比較画面（小さめ）')
    d.resize(*report['compare_default_size']); QTest.qWait(600)
    # 行の高さを ▲ で 21 にする（自動の高さから）
    clicks = 0
    while d.table.rowHeight(0) < 21 and clicks < 10:
        QTest.mouseClick(d.btn_row_height_up, Qt.MouseButton.LeftButton); QTest.qWait(150); clicks += 1
    while d.table.rowHeight(0) > 21 and clicks < 20:
        QTest.mouseClick(d.btn_row_height_dn, Qt.MouseButton.LeftButton); QTest.qWait(150); clicks += 1
    report['compare_row_height_set'] = (d.table.rowHeight(0), d._manual_row_height, clicks)
    shot(d, 'compare_rowheight21_before_reopen', '比較画面（行の高さ 21 にした直後）')
    d.close(); QTest.qWait(300)
    report['compare_saved'] = {k: v for k, v in json.loads((DATA / 'compare.json').read_text(encoding='utf-8')).items()
                               if 'row_height' in k}
    d2 = sv.SnapshotCompareDialog(w, initial_steam_id=SID)
    d2.show(); QTest.qWait(1000)
    report['compare_row_height_after_reopen'] = (d2.table.rowHeight(0), d2._manual_row_height,
                                                 d2.table_acc.rowHeight(0), d2.ss_star_table.rowHeight(0))
    shot(d2, 'compare_rowheight21_after_reopen', '比較画面（開き直した後も 21）')
    d2.close(); w.close(); QTest.qWait(200)

report['finished'] = datetime.now().isoformat(timespec='seconds')
(OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
print('done')
