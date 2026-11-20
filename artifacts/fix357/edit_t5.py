def edit(p, pairs):
    b=open(p,'rb').read().decode('utf-8'); crlf='\r\n' in b; s=b.replace('\r\n','\n')
    for a,c in pairs:
        assert s.count(a)==1,(p,a,s.count(a)); s=s.replace(a,c)
    if crlf: s=s.replace('\n','\r\n')
    open(p,'wb').write(s.encode('utf-8'))
edit('test_player_pp.py', [('''    assert get.call_args.args[1] == "https://api.beatleader.com/player/test"''',
 '''    # 指摘 3：統計・スコア取得と同じ xyz の URL（get_memoized で 1 回にまとまる）
    assert get.call_args.args[1] == "https://api.beatleader.xyz/player/test"''')])
edit('test_top_compare_pp.py', [
("""            assert w.bl_pp_table.item(0, 1).text() == '— / 14,500'""","""            assert w.bl_pp_table.item(0, 1).text() == '—'"""),
("""            assert [w.bl_pp_table.item(r, 1).text() for r in range(3)] == ['10,700.80 / 14,500', '783.72 / 1,200', '1,188.78 / 3,000']""",
 """            assert [w.bl_pp_table.item(r, 1).text() for r in range(3)] == ['10,700.80', '783.72', '1,188.78']"""),
("""@pytest.mark.parametrize('date,limits', [('2026-10-26T00:00:00Z', (6000, 1300, 15000)),
    ('2026-10-27T16:00:00Z', (3100, 2800, 19000)), ('2026-11-05T00:00:00Z', (3000, 1200, 14500))])
def test_scale_history(date, limits):
    assert pp_limits(snapshot(taken_at=date)) == limits""",
"""@pytest.mark.parametrize('date', ['2026-10-26T00:00:00Z', '2026-10-27T16:00:00Z', '2026-11-05T00:00:00Z', None])
def test_scale_is_latest_for_any_date(date):
    # 撮影日で基準を切り替えない。トップ・比較とも最新の基準（Pass / Tech / Acc）
    assert pp_limits(snapshot(taken_at=date) if date else None) == (3000, 1200, 14500)"""),
("""        assert table.item(2, 1).text() == '0.00 / 3,000'
        assert table.item(1, 1).text() == '— / 1,200'""","""        assert table.item(2, 1).text() == '0.00'
        assert table.item(1, 1).text() == '—'"""),
])
