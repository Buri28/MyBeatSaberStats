def edit(p, pairs):
    b=open(p,'rb').read().decode('utf-8'); crlf='\r\n' in b; s=b.replace('\r\n','\n')
    for a,c in pairs:
        assert s.count(a)==1,(p,a,s.count(a)); s=s.replace(a,c)
    if crlf: s=s.replace('\n','\r\n')
    open(p,'wb').write(s.encode('utf-8'))
edit('test_skill_resize.py', [
("""                    assert w.bl_pp_table._bars[row]._max_value == pp_limits_by_field(snap)[field]
                    assert item.text().endswith(f'/ {pp_limits_by_field(snap)[field]:,}')""",
"""                    # 撮影日に関係なく最新の基準。表示は値だけ（上限の数値は出さない）
                    assert w.bl_pp_table._bars[row]._max_value == {'beatleader_pass_pp': 3000,
                        'beatleader_tech_pp': 1200, 'beatleader_acc_pp': 14500}[field]
                    assert '/' not in item.text() and item.text() == f'{getattr(snap, field):,.2f}'"""),
])
