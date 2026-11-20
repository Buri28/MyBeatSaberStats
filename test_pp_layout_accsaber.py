import sys
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch, Mock
import pytest
sys.path.insert(0,str(Path(__file__).parent/'src'))
from mybeatsaberstats.collector import collector as c, beatleader as cb
from mybeatsaberstats.snapshot import Snapshot
from mybeatsaberstats.accsaber_status import status_text
from mybeatsaberstats import accsaber_reloaded as ar

@pytest.mark.parametrize('mode,expected',[('off','skipped'),('fail','failed'),('unregistered_tech','ok'),('skillfail','partial'),('ok','ok')])
def test_accsaber_acquisition_status_and_persistence(tmp_path,mode,expected):
    options=c.SnapshotOptions(**{f.name:False for f in fields(c.SnapshotOptions) if f.name.startswith('fetch_')})
    options.fetch_accsaber_reloaded=mode!='off'
    player=ar.AccSaberReloadedPlayer('test','Test','JP',10,.98,3,4,5)
    result={cat:player for cat in ar.CATEGORY_IDS}
    # tech に登録していないだけ（API は成功）。#357 指摘 7：未登録は警告も「一部未取得」も出さない
    if mode=='unregistered_tech':result['tech']=None
    warnings=[]
    def ranks(*args,**kwargs):
        if mode=='fail':raise RuntimeError('HTTP 503 test')
        return result
    with patch.object(c,'CACHE_DIR',tmp_path),patch.object(c,'_fetch_accsaber_reloaded',side_effect=ranks), \
         patch.object(c,'_fetch_accsaber_reloaded_xp',return_value=ar.AccSaberReloadedXP(10,1,1,1)), \
         patch.object(c,'_fetch_accsaber_reloaded_milestones',return_value=(1,2)), \
         patch.object(c,'_fetch_accsaber_reloaded_skill',return_value={cat:None if mode=='skillfail' else 1 for cat in ar.CATEGORY_IDS}), \
         patch.object(c,'_fetch_accsaber_reloaded_level_title',return_value=None), \
         patch.object(c,'_fetch_accsaber_reloaded_title',return_value=None), \
         patch.object(c,'_fetch_and_save_rl_maps'),patch.object(c,'_fetch_and_save_rl_player_scores'), \
         patch.object(c,'_get_rl_map_counts_cached',return_value={}), \
         patch.object(c,'_compute_rl_effective_played_counts',return_value={}):
        # A cached ID permits the normal collector path without network.
        from mybeatsaberstats.beatleader import BeatLeaderPlayer
        c._save_player_index({'test':{'beatleader':BeatLeaderPlayer('test','Test','JP',1,1,1)}})
        snap=c._create_snapshot_for_steam_id('test',options=options,snapshot_dir=tmp_path/'snapshots',on_warning=warnings.append)
    loaded=Snapshot.load(next((tmp_path/'snapshots').glob('*.json')))
    assert snap.accsaber_fetch_status==loaded.accsaber_fetch_status==expected
    assert loaded.accsaber_fetch_errors==snap.accsaber_fetch_errors
    assert bool(warnings)==(mode in ('fail','skillfail'))
    if mode=='fail':assert any('HTTP 503 test' in x for x in warnings)
    if mode in ('off','fail'):assert snap.accsaber_reloaded_overall_ap is None
    assert status_text(loaded)

def test_api_failure_retains_diagnostics():
    session=Mock()
    response=Mock(status_code=503)
    response.raise_for_status.side_effect=RuntimeError('HTTP 503 test')
    session.get.return_value=response
    warnings=[]
    with patch.object(ar,'fetch_user_profile',return_value={}):
        result=ar.fetch_player_all_categories('test',session=session,on_warning=warnings.append)
    assert all(x is None for x in result.values())
    assert len(warnings)==4 and all('HTTP 503 test' in x for x in warnings)

def test_legacy_status_does_not_read_current_settings(tmp_path):
    from test_player_pp import snapshot
    old=snapshot()
    assert '理由の記録なし' in status_text(old)
    path=old.save(tmp_path/'old.json');before=path.read_bytes()
    assert Snapshot.load(path).accsaber_fetch_status is None
    assert path.read_bytes()==before

def test_bl_zero_and_one_buckets_exclude_unranked():
    lbs=[{'id':str(i),'difficulty':{'status':status,'stars':stars}} for i,(status,stars) in enumerate([(3,0),(3,.999),(3,1),(3,1.999),(3,2),(0,0)])]
    stats=cb._build_beatleader_star_stats(lbs,[])
    assert {x.star:x.map_count for x in stats} == {0:2,1:2,2:1}

def test_compare_fit_legacy_missing_resize_and_manual_rows(tmp_path):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from mybeatsaberstats import snapshot_view as sv
    from mybeatsaberstats.snapshot import StarClearStat
    from test_player_pp import snapshot
    app=QApplication.instance() or QApplication([])
    a=snapshot(scoresaber_name='Test',beatleader_name='Test',
               star_stats=[StarClearStat(i,1,1,0) for i in range(15)],
               beatleader_star_stats=[StarClearStat(i,1,1,0) for i in range(2,16)])
    from dataclasses import replace
    b=replace(a,taken_at='2026-11-18T13:00:00Z',beatleader_pass_pp=0.,beatleader_tech_pp=1.234,beatleader_acc_pp=2.345)
    snapshots=tmp_path/'snapshots';snapshots.mkdir();a.save(snapshots/'a.json');b.save(snapshots/'b.json')
    with patch.object(sv,'SNAPSHOT_DIR',snapshots),patch.object(sv.SnapshotCompareDialog,'_settings_path',lambda s:tmp_path/'compare.json'):
        d=sv.SnapshotCompareDialog(initial_steam_id=a.steam_id)
        try:
            d.resize(1540,887);d.show();QTest.qWait(80)
            d.combo_a.setCurrentIndex(1);d.combo_b.setCurrentIndex(0);QTest.qWait(30)
            for t in (d.table,d.table_acc,d.ss_star_table,d.bl_star_table):
                assert t.verticalScrollBar().maximum()==0, (t is d.table, t is d.table_acc, t.rowCount(), t.rowHeight(0), t.viewport().height(), d._metric_vsplitter.sizes())
            assert [d.table.item(13,c).text() for c in (2,3,4)]==['—','0.00','—']
            d.resize(1000,650);QTest.qWait(50)
            for t in (d.table,d.table_acc,d.ss_star_table,d.bl_star_table):
                t.scrollToBottom();app.processEvents()
                assert t.visualItemRect(t.item(t.rowCount()-1,2)).bottom()<=t.viewport().height()
            h0=d.table.rowHeight(0)  # ▲▼は表示中の実際の高さを基準に±1する
            d._on_row_height_dn();assert d.table.rowHeight(0)==h0-1
            d._on_row_height_dn();assert d.table.rowHeight(0)==h0-2
            d._on_row_height_up();assert d.table.rowHeight(0)==h0-1
        finally:d.close()
