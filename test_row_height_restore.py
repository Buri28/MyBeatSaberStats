"""比較画面: 行の高さを ▲ で広げて ▼ で戻すと、初期の自動フィット高さに戻ること。"""
import sys
from contextlib import ExitStack
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from test_top_compare_pp import isolate_ui, ROOT, sv
from mybeatsaberstats import theme


def _open(tmp_path, stack, size=(1700, 1000)):
    data = tmp_path
    (data / 'snapshots').mkdir(); (data / 'cache').mkdir()
    for name in ('76561198324870685_20261118-121840.json', '76561198324870685_20261118-122702.json'):
        (data / 'snapshots' / name).write_bytes((ROOT / 'snapshots' / name).read_bytes())
    (data / 'compare.json').write_bytes((ROOT / 'cache/snapshot_compare.json').read_bytes())
    app = QApplication.instance() or QApplication([])
    theme.apply_dark(app)
    isolate_ui(stack, data)
    if True:
        d = sv.SnapshotCompareDialog(initial_steam_id='76561198324870685')
        d.resize(*size); d.show(); QTest.qWait(300)
        return d


NAMES = ('table', 'table_acc', 'ss_star_table', 'bl_star_table', 'acc_cmp_table')


def _state(d):
    QTest.qWait(400)
    return ({n: getattr(d, n).rowHeight(0) for n in NAMES},
            {n: getattr(d, n).verticalScrollBar().isVisible() for n in NAMES})


def _click(d, seq):
    for c in seq:
        (d._on_row_height_up if c == 'u' else d._on_row_height_dn)()
        QTest.qWait(80)


def _run(tmp_path, seq, name=None):
    with ExitStack() as stack:
        d = _open(tmp_path, stack)
        h0, sb0 = _state(d)
        print('initial', seq, h0, sb0)
        _click(d, seq)
        h1, sb1 = _state(d)
        print('after  ', seq, h1, sb1, d._manual_row_height)
        if name:
            d.grab().save(str(ROOT / 'verification' / 'row-height-updn' / name))
        d.close()
    assert not sb0['table'] and not sb0['table_acc'], sb0
    assert h1 == h0, (seq, h0, h1)
    assert sb1 == sb0, (seq, sb1)
    assert d._manual_row_height is False


def test_return_at_autofit_19(tmp_path):
    for i, seq in enumerate(('ud', 'du', 'uuddud')):
        with ExitStack() as stack:
            (tmp_path / str(i)).mkdir()
            d = _open(tmp_path / str(i), stack, (1540, 887))
            h0 = d.table.rowHeight(0)
            _click(d, seq)
            h1 = d.table.rowHeight(0); m = d._manual_row_height
            d.grab().save(str(ROOT / 'verification' / 'row-height-updn' / f'size1540-{seq}.png'))
            d.close()
        assert h0 == 19 and h1 == h0 and m is False, (seq, h0, h1, m)


def test_up_then_down(tmp_path):
    _run(tmp_path, 'ud', 'after-up-down.png')


def test_down_then_up(tmp_path):
    _run(tmp_path, 'du', 'after-down-up.png')


def test_round_trips(tmp_path):
    _run(tmp_path, 'uuuddduuddud', 'after-round-trips.png')


def test_eight_up_eight_down(tmp_path):
    _run(tmp_path, 'u' * 8 + 'd' * 8)


def test_resize_after_return_keeps_autofit(tmp_path):
    results = []
    for clicks in ('', 'ud'):
        with ExitStack() as stack:
            (tmp_path / ('r' + clicks)).mkdir()
            d = _open(tmp_path / ('r' + clicks), stack)
            _click(d, clicks)
            for size in ((1300, 760), (1700, 1000)):
                d.resize(*size); QTest.qWait(300)
                results.append((clicks, size, d._manual_row_height, _state(d)))
            d.close()
    base, test = results[:2], results[2:]
    for (_, size, _, st0), (_, _, manual, st1) in zip(base, test):
        assert manual is False
        assert st1 == st0, (size, st0, st1)


def test_single_step_changes_actual_height_by_one(tmp_path):
    for c in 'ud':
        with ExitStack() as stack:
            d = _open(tmp_path / c, stack, (1540, 887)) if (tmp_path / c).mkdir() is None else None
            h0 = d.table.rowHeight(0)
            _click(d, c)
            h1 = d.table.rowHeight(0)
            d.close()
        assert h1 == h0 + (1 if c == 'u' else -1), (c, h0, h1)


def test_manual_then_widen_window_down_goes_down(tmp_path):
    with ExitStack() as stack:
        d = _open(tmp_path, stack, (1540, 887))
        _click(d, 'uu')
        h = d.table.rowHeight(0)
        d.resize(1700, 1000); QTest.qWait(400)
        assert d.table.rowHeight(0) == h  # 手動なので変わらない
        _click(d, 'd')
        h1 = d.table.rowHeight(0)
        d.close()
    assert h1 == h - 1, (h, h1)


def test_loaded_manual_height_returns_to_auto(tmp_path):
    # 設定読込直後の手動高さ（保存値 22）から▼で自動の 21 に戻ること。
    with ExitStack() as stack:
        d = _open(tmp_path, stack)
        d._row_height = 22; d._manual_row_height = True
        d._apply_row_height(); QTest.qWait(400)
        assert d.table.rowHeight(0) == 22
        _click(d, 'd')
        h = d.table.rowHeight(0); m = d._manual_row_height
        d.close()
    assert h == 21 and m is False, (h, m)


RIGHT = ('ss_star_table', 'bl_star_table', 'acc_cmp_table')
LEFT = ('table', 'table_acc')


def _lr(d):
    QTest.qWait(300)
    return ([getattr(d, n).rowHeight(0) for n in LEFT], [getattr(d, n).rowHeight(0) for n in RIGHT])


def test_left_right_independent(tmp_path):
    with ExitStack() as stack:
        d = _open(tmp_path, stack, (1540, 887))
        l0, r0 = _lr(d)
        assert l0 == [19, 19] and r0 == [21, 21, 21], (l0, r0)
        for c, dl in (('u', 1), ('u', 1), ('d', -1), ('d', -1), ('d', -1), ('u', 1)):
            before = _lr(d)
            _click(d, c)
            after = _lr(d)
            assert after[0] == [x + dl for x in before[0]], (c, before, after)
            assert after[1] == [x + dl for x in before[1]], (c, before, after)
        # 往復後は左右とも元の高さ（左は自動へ戻り、右は 21 のまま）
        assert _lr(d) == (l0, r0) and d._manual_row_height is False
        d.close()


def test_right_does_not_follow_left_at_limit(tmp_path):
    with ExitStack() as stack:
        d = _open(tmp_path, stack, (1540, 887))
        d._row_height = 8; d._manual_row_height = True
        d._apply_row_height(); QTest.qWait(300)
        _click(d, 'd')
        l, r = _lr(d)
        d.close()
    assert l == [8, 8] and r == [20, 20, 20], (l, r)


def test_save_restore_separately_and_legacy(tmp_path):
    with ExitStack() as stack:
        d = _open(tmp_path, stack, (1540, 887))
        _click(d, 'uuu'); _click(d, 'ddddd')
        saved = _lr(d); d._save_last_selection()
        import json
        data = json.loads(d._settings_path().read_text(encoding='utf-8'))
        assert data['ui_right_row_height'] == saved[1][0] and data['ui_row_height'] == saved[0][0], data
        d.close()
        d2 = sv.SnapshotCompareDialog(initial_steam_id='76561198324870685')
        d2.resize(1540, 887); d2.show(); QTest.qWait(500)
        assert _lr(d2) == saved, (saved, _lr(d2))
        d2.close()
        # 旧形式: ui_row_height だけ
        data.pop('ui_right_row_height'); data['ui_row_height'] = 23; data['ui_manual_row_height'] = True
        d2._settings_path().write_text(json.dumps(data), encoding='utf-8')
        d3 = sv.SnapshotCompareDialog(initial_steam_id='76561198324870685')
        d3.resize(1540, 887); d3.show(); QTest.qWait(500)
        l, r = _lr(d3)
        d3.close()
    assert l == [23, 23] and r == [23, 23, 23], (l, r)


def test_default_layout_resets_both(tmp_path):
    with ExitStack() as stack:
        d = _open(tmp_path, stack, (1540, 887))
        _click(d, 'uuuu')
        d._on_default_layout(); QTest.qWait(500)
        l, r = _lr(d)
        d.close()
    assert d._manual_row_height is False and r == [21, 21, 21] and l == [19, 19], (l, r)
