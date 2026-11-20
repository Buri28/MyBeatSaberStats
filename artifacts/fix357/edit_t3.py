def edit(p, pairs):
    b=open(p,'rb').read().decode('utf-8'); crlf='\r\n' in b; s=b.replace('\r\n','\n')
    for a,c in pairs:
        assert s.count(a)==1,(p,a,s.count(a)); s=s.replace(a,c)
    if crlf: s=s.replace('\n','\r\n')
    open(p,'wb').write(s.encode('utf-8'))
edit('test_ranked_sync.py', [
('''        # pp が付かなかった 301 も譜面情報はランクに差し替わり、再検知しない
        assert saved["301"]["leaderboard"]["ranked"] is True
        assert saved["999"]["leaderboard"]["ranked"] is False
        calls = api.calls
        assert ss.refresh_scores_of_newly_ranked_maps("1", api) == 0
        assert api.calls == calls, "検知済みなのに API を呼んだ"
''','''        # 指摘 4：pp が付かなかった 301 の譜面情報は書き換えない（ranked=True にしない）。検知済みの印だけ残し、再検知しない
        assert saved["301"]["leaderboard"]["ranked"] is False, "ランクのスコアにならなかったのに譜面情報を書き換えた"
        assert saved["999"]["leaderboard"]["ranked"] is False
        marks = json.loads((tmp_path / "newly_ranked_checked.json").read_text(encoding="utf-8"))
        assert set(marks["scoresaber"]["1"]) == {"301"}
        calls = api.calls
        assert ss.refresh_scores_of_newly_ranked_maps("1", api) == 0
        assert api.calls == calls, "検知済みなのに API を呼んだ"
        # プレイし直して、スコアの譜面情報が（ランク外のまま）新しくなっても、取り直さない
        p = tmp_path / "scoresaber_player_scores_1.json"
        raw = json.loads(p.read_text(encoding="utf-8"))
        raw["scores"]["301"] = _ss_score(301, False, 0)
        raw["scores"]["301"]["score"]["timeSet"] = "2026-11-19T00:00:00.000Z"
        p.write_text(json.dumps(raw), encoding="utf-8")
        assert ss.refresh_scores_of_newly_ranked_maps("1", api) == 0
        assert api.calls == calls, "プレイし直すたびに取り直している"
'''),
('''        assert saved["L1"]["pp"] == 0.05 and saved["L1"]["leaderboard"]["difficulty"]["status"] == 3
        assert saved["L2"]["leaderboard"]["difficulty"]["status"] == 3
        assert saved["L9"]["leaderboard"]["difficulty"]["status"] == 0
        calls = api.calls
        assert bl.refresh_scores_of_newly_ranked_maps("7", api) == 0
        assert api.calls == calls
''','''        assert saved["L1"]["pp"] == 0.05 and saved["L1"]["leaderboard"]["difficulty"]["status"] == 3
        # 指摘 4：取り直してもランクのスコアにならなかった L2 は書き換えず、検知済みの印だけ残す
        assert saved["L2"]["leaderboard"]["difficulty"]["status"] == 0, "譜面情報を書き換えた"
        assert saved["L9"]["leaderboard"]["difficulty"]["status"] == 0
        marks = json.loads((tmp_path / "newly_ranked_checked.json").read_text(encoding="utf-8"))
        assert set(marks["beatleader"]["7"]) == {"L2"}
        calls = api.calls
        assert bl.refresh_scores_of_newly_ranked_maps("7", api) == 0
        assert api.calls == calls
        # プレイし直しても取り直さない
        p = tmp_path / "beatleader_player_scores_7.json"
        raw = json.loads(p.read_text(encoding="utf-8"))
        raw["scores"]["L2"] = dict(item(2, 0, 0), timeset="1800000000")
        p.write_text(json.dumps(raw), encoding="utf-8")
        assert bl.refresh_scores_of_newly_ranked_maps("7", api) == 0
        assert api.calls == calls, "プレイし直すたびに取り直している"
        # ランクになり直した（rankedTime が変わった）ら、もう一度検知する
        lbs = [bl_lb(1, 3), dict(bl_lb(2, 1))]
        lbs[1]["difficulty"] = dict(lbs[1]["difficulty"], rankedTime=lbs[1]["difficulty"]["rankedTime"] + 5)
        bl_write(tmp_path, lbs, 1)
        assert bl.refresh_scores_of_newly_ranked_maps("7", api) == 1
'''),
])
