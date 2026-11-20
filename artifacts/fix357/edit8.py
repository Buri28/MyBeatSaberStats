def edit(p, pairs):
    b=open(p,'rb').read().decode('utf-8'); crlf='\r\n' in b; s=b.replace('\r\n','\n')
    for a,c in pairs:
        assert s.count(a)==1,(p,a,s.count(a)); s=s.replace(a,c)
    if crlf: s=s.replace('\n','\r\n')
    open(p,'wb').write(s.encode('utf-8'))
edit('src/mybeatsaberstats/snapshot_view.py', [(
"""        self._manual_row_height = bool(data.get("ui_manual_row_height", self._row_height != 21) and self._row_height != 21)""",
"""        # 手動で 21 にした場合も手動のまま戻す（21 は自動フィットの上限と同じ値だが、保存した印を優先する）。
        # 印の無い古い設定は、既定の 21 以外なら手動とみなす。
        self._manual_row_height = bool(data.get("ui_manual_row_height", self._row_height != 21))""")])
