"""Visible source UI, disposable saved snapshots, HTTP forbidden."""
import sys, os, json
from pathlib import Path
from contextlib import ExitStack
from datetime import datetime
from dataclasses import replace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_top_compare_pp import isolate_ui, ROOT, pa, sv, Snapshot
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from mybeatsaberstats import theme

out = ROOT / 'verification/ratio-layout'
data = out / 'offline'
(data / 'snapshots').mkdir(parents=True, exist_ok=True)
(data / 'cache').mkdir(exist_ok=True)
names = ['76561198324870685_20261118-121840.json', '76561198324870685_20261118-122702.json']
for name in names:
    (data / 'snapshots' / name).write_bytes((ROOT / 'snapshots' / name).read_bytes())
for source, target in [('player_window.json', 'cache/player_window.json'), ('snapshot_compare.json', 'compare.json')]:
    (data / target).write_bytes((ROOT / 'cache' / source).read_bytes())
(out / 'process.json').write_text(json.dumps(dict(pid=os.getpid(), started=datetime.now().isoformat(), executable=sys.executable)), encoding='utf-8')
app = QApplication([])
theme.apply_dark(app)
with ExitStack() as stack:
    isolate_ui(stack, data)
    stack.enter_context(__import__('unittest.mock', fromlist=['patch']).patch.object(theme, '_PREF_PATH', data / 'cache/theme.json'))
    w = pa.PlayerWindow()
    w.setWindowTitle('Stats [ratio verification]')
    w.resize(1211,770)
    w._events_panel._on_fetched({'title':'Accursed: Totality','slug':'totality','startsAt':'2026-09-26T00:00:00Z','endsAt':'2026-11-09T00:00:00Z','active':True}, {'name':'Better Speed!','id':1,'endDate':1795215600})
    w.show()
    d = sv.SnapshotCompareDialog(initial_steam_id='76561198324870685')
    d.setWindowTitle('Compare [ratio verification]')
    d.show()
    results = {}
    def check():
        try:
            QTest.qWait(100)
            w.grab().save(str(out/'top.png'))
            pp = w.bl_pp_table.parentWidget()
            ev = w._events_scroll.parentWidget()
            results['top'] = dict(pp_width=pp.width(), events_width=ev.width(), gap=pp.x()-(w._btn_rl_unplayed.x()+w._btn_rl_unplayed.width()), window=w.size().toTuple())
            assert abs(pp.width()-ev.width()) <= 2, results
            assert all(w.bl_pp_table.fontMetrics().horizontalAdvance(w.bl_pp_table.item(r,1).text())+8 <= w.bl_pp_table.columnWidth(1) for r in range(3))
            d.show(); QTest.qWait(100)
            d.combo_a.setCurrentIndex(1); d.combo_b.setCurrentIndex(0)
            d.grab().save(str(out/'compare-missing.png'))
            a = d._current_snapshot(d.combo_a)
            b = d._current_snapshot(d.combo_b)
            def inspect(sa,sb):
                rows = [r for r in range(d.table.rowCount()) if d.table.item(r,1).text() in ('Pass PP','Tech PP','Acc PP','Ranked Play Count')]
                assert len(rows)==5
                for r in rows:
                    label=d.table.item(r,1).text()
                    for col,snap in ((2,sa),(3,sb)):
                        item=d.table.item(r,col)
                        assert '/' not in item.text()
                        if label=='Ranked Play Count':
                            prefix='scoresaber' if r<5 else 'beatleader'
                            value=getattr(snap,prefix+'_ranked_play_count')
                            total=sum(s.map_count for s in (getattr(snap,'star_stats' if prefix=='scoresaber' else 'beatleader_star_stats') or []))
                        else:
                            part=label.split()[0].lower()
                            value=getattr(snap,'beatleader_'+part+'_pp')
                            total={'pass':3000,'tech':1200,'acc':14500}[part]
                        ratio=item.data(Qt.ItemDataRole.UserRole)
                        if value is None:
                            assert item.text()=='—' and ratio is None
                        elif total>0:
                            assert abs(ratio-max(0,min(1,value/total)))<1e-9
                            assert item.data(Qt.ItemDataRole.UserRole+1) is True
                return rows
            inspect(a,b)
            # Same saved data plus controlled missing/zero components on each side.
            aa=replace(a,beatleader_pass_pp=0,beatleader_tech_pp=600,beatleader_acc_pp=14500,scoresaber_ranked_play_count=None)
            bb=replace(b,beatleader_pass_pp=1500,beatleader_tech_pp=1200,beatleader_acc_pp=None,beatleader_ranked_play_count=0)
            snaps=d._snapshots_by_player[a.steam_id]
            snaps[d.combo_a.currentIndex()]=aa; snaps[d.combo_b.currentIndex()]=bb
            d._update_view2(); QTest.qWait(100)
            rows=inspect(aa,bb)
            results['rows']=[dict(label=d.table.item(r,1).text(),a=d.table.item(r,2).text(),b=d.table.item(r,3).text(),diff=d.table.item(r,4).text()) for r in rows]
            assert d.ss_star_table.rowHeight(0)==d.bl_star_table.rowHeight(0)==21
            results['left_height']=d.table.rowHeight(0)
            d.grab().save(str(out/'compare-zero-missing.png'))
            d._toggle_bl_acc_details(9); QTest.qWait(100)
            assert d._bl_acc_show_lr
            d.grab().save(str(out/'compare-lr.png'))
            results['ok']=True
            print('CHECKS PASSED', flush=True)
        except Exception:
            import traceback; traceback.print_exc()
            results['ok']=False
        (out/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    QTimer.singleShot(1200,check)
    QTimer.singleShot(55000,d.close)
    QTimer.singleShot(55000,w.close)
    QTimer.singleShot(56000,app.quit)
    app.exec()
    print('NORMAL EXIT',flush=True)
