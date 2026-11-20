import sys
from contextlib import ExitStack
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from test_top_compare_pp import isolate_ui, pa, sv
from PySide6.QtWidgets import QApplication
app = QApplication([])
data = Path(__file__).resolve().parent / 'offline'
with ExitStack() as st:
    isolate_ui(st, data)
    d = sv.SnapshotCompareDialog(None, initial_steam_id='76561198324870685'); d.show(); app.processEvents()
    d.ss_star_table.horizontalHeader().resizeSection(1, 130); app.processEvents()
    print('drag SS col1 -> BL', d.bl_star_table.columnWidth(1))
    d.bl_star_table.horizontalHeader().resizeSection(12, 70); app.processEvents()
    print('drag BL col12 -> SS', d.ss_star_table.columnWidth(12))
    d.chk_col_acc.setChecked(False); d.chk_col_acc.setChecked(True); app.processEvents()
    print('acc toggle col9', d.ss_star_table.columnWidth(9), d.bl_star_table.columnWidth(9))
    d.close()
    d = sv.SnapshotCompareDialog(None, initial_steam_id='76561198324870685'); d.show(); app.processEvents()
    print('stacked', d._star_tables_stacked(), 'col9', d.ss_star_table.columnWidth(9), d.bl_star_table.columnWidth(9))
    d._on_toggle_bl_below(); app.processEvents()
    print('side', d._star_tables_stacked(), 'col9', d.ss_star_table.columnWidth(9), d.bl_star_table.columnWidth(9))
    d.ss_star_table.horizontalHeader().resizeSection(2, 120); app.processEvents()
    print('side drag SS col2 -> BL', d.bl_star_table.columnWidth(2))
    d._on_toggle_bl_below(); app.processEvents()
    print('stacked again col9', d.ss_star_table.columnWidth(9), d.bl_star_table.columnWidth(9))
    d.btn_toggle_ss.setChecked(False); app.processEvents()
    print('ss hidden', d._star_tables_stacked(), 'col9', d.ss_star_table.columnWidth(9), d.bl_star_table.columnWidth(9))
    d.btn_toggle_ss.setChecked(True); app.processEvents()
    print('ss shown col9', d.ss_star_table.columnWidth(9), d.bl_star_table.columnWidth(9))
    d.close()
