import sys, json, traceback
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
for name in ("player_window.json", "compare.json"):
    (OUT / name).unlink(missing_ok=True)
sys.path.insert(0, str(ROOT / "src"))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from mybeatsaberstats import player_app as pa, snapshot_view as sv, theme
from mybeatsaberstats.snapshot import Snapshot

app = QApplication([])
app.setQuitOnLastWindowClosed(False)
theme.apply_dark(app)
sid = "76561198324870685"
fixture = OUT / "snapshots"
fixture.mkdir(exist_ok=True)
old = Snapshot.load(sorted((ROOT / "snapshots").glob(sid + "*"))[-1])
live = json.loads((OUT / "player-api.json").read_text(encoding="utf-8"))
new = replace(old, taken_at="2026-11-18T12:00:00Z", beatleader_pp=live["pp"],
              beatleader_pass_pp=live["passPp"], beatleader_tech_pp=live["techPp"],
              beatleader_acc_pp=live["accPp"])
old.save(fixture / "old.json")
new.save(fixture / "new.json")
patches = [patch.object(pa, "SNAPSHOT_DIR", fixture), patch.object(sv, "SNAPSHOT_DIR", fixture),
           patch.object(pa.PlayerWindow, "_settings_path", lambda s: OUT / "player_window.json"),
           patch.object(sv.SnapshotCompareDialog, "_settings_path", lambda s: OUT / "compare.json"),
           patch.object(pa.EventsPanel, "_fetch", lambda s: None),
           patch.object(pa.PlayerAvatarWidget, "set_ids", lambda *a, **k: None),
           patch.object(pa.RemotePixmapLabel, "set_image_url", lambda *a, **k: None),
           patch.object(pa.StartupUpdateChecker, "start", lambda *a: None)]
for p in patches: p.start()
w = pa.PlayerWindow()
w.setWindowTitle("PP Trial — Stats")
w.resize(1450, 920)
w.show()
compare = None
checks = []

def capture(window, name):
    app.processEvents()
    window.grab().save(str(OUT / (name + ".png")))
    checks.append({"screen": name, "size": [window.width(), window.height()]})

def run_step(fn):
    try:
        fn()
    except Exception:
        traceback.print_exc()
        cleanup(1)

def top():
    w.snapshot_combo.setCurrentIndex(w.snapshot_combo.findData(new.taken_at))
    w._update_view()
    assert w.bl_pp_table.item(0, 0).text() == "1,188.78"
    capture(w, "top-live")
    w.snapshot_combo.setCurrentIndex(w.snapshot_combo.findData(old.taken_at))
    w._update_view()
    assert w.bl_pp_table.item(0, 0).text() == "—"
    capture(w, "top-legacy")
    w.snapshot_combo.setCurrentIndex(w.snapshot_combo.findData(new.taken_at))
    w.resize(1000, 650)
    QTimer.singleShot(300, lambda: run_step(top_small))

def top_small():
    capture(w, "top-small")
    assert w.bl_pp_table.isVisible()
    for col in range(3):
        assert w.bl_pp_table.visualItemRect(w.bl_pp_table.item(0, col)).right() <= w.bl_pp_table.viewport().width()
    w.hide()
    global compare
    compare = sv.SnapshotCompareDialog(initial_steam_id=sid)
    compare.setWindowTitle("PP Trial — Compare")
    compare.resize(1540, 950)
    compare.show()
    QTimer.singleShot(500, lambda: run_step(cmp_normal))

def cmp_normal():
    compare.combo_a.setCurrentIndex(1)
    compare.combo_b.setCurrentIndex(0)
    compare._update_view2()
    assert compare.bl_pp_table.item(0, 1).text() == "—"
    assert compare.bl_pp_table.item(0, 3).text() == "—"
    compare._bl_detail_scroll.verticalScrollBar().setValue(compare._bl_detail_scroll.verticalScrollBar().maximum())
    capture(compare, "compare-legacy-live")
    a = replace(new, beatleader_pass_pp=new.beatleader_pass_pp-10,
                beatleader_tech_pp=new.beatleader_tech_pp+5, beatleader_acc_pp=0.0)
    compare._snapshots_by_player[sid][1] = a
    compare._update_view2()
    capture(compare, "compare-diff")
    assert compare.bl_pp_table.item(0, 3).text() == "+10.00"
    assert compare.bl_pp_table.item(1, 3).text() == "-5.00"
    compare.resize(1000, 650)
    QTimer.singleShot(300, lambda: run_step(cmp_small))

def cmp_small():
    compare._bl_detail_scroll.verticalScrollBar().setValue(compare._bl_detail_scroll.verticalScrollBar().maximum())
    capture(compare, "compare-small")
    assert compare._bl_detail_scroll.verticalScrollBar().maximum() > 0
    rect = compare.bl_pp_table.geometry()
    assert rect.bottom() <= compare._bl_detail_scroll.widget().height()
    compare._row_height = 40
    compare._apply_row_height()
    compare._bl_detail_scroll.verticalScrollBar().setValue(compare._bl_detail_scroll.verticalScrollBar().maximum())
    capture(compare, "compare-large-rows")
    assert compare.bl_star_table.height() >= sum(compare.bl_star_table.rowHeight(r) for r in range(compare.bl_star_table.rowCount()))
    theme.apply_light(app)
    compare._update_view2()
    capture(compare, "compare-light")
    cleanup(0)

def cleanup(code):
    (OUT / "gui-checks.json").write_text(json.dumps({"exit_code": code, "checks": checks}, indent=2), encoding="utf-8")
    if compare: compare.close()
    w.close()
    app.exit(code)

QTimer.singleShot(700, lambda: run_step(top))
sys.exit(app.exec())
