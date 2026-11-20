def edit(p, pairs):
    b=open(p,'rb').read().decode('utf-8'); crlf='\r\n' in b; s=b.replace('\r\n','\n')
    for a,c in pairs:
        assert s.count(a)==1,(p,a,s.count(a)); s=s.replace(a,c)
    if crlf: s=s.replace('\n','\r\n')
    open(p,'wb').write(s.encode('utf-8'))

edit('src/mybeatsaberstats/collector/scoresaber.py', [
("""    status: Optional[dict] = None,
    prune_missing: bool = True,
) -> int:
    \"\"\"ランク譜面のスコアだけを pp の高い順（sort=top）に取り直し""","""    status: Optional[dict] = None,
    prune_missing: bool = True,
    ranked_play_count: Optional[int] = None,
) -> int:
    \"\"\"ランク譜面のスコアだけを pp の高い順（sort=top）に取り直し"""),
("""    キャッシュから外す（prune_missing=False なら外さない。条件は _prune_missing_ranked_scores）。
    戻り値は取り直したスコアの件数。
    \"\"\"
    # 呼ぶ前に別の取得（ランク譜面の取り直しなど）が失敗していたら""","""    キャッシュから外す（prune_missing=False なら外さない。条件は _prune_missing_ranked_scores）。
    ranked_play_count は同じ Snapshot で取った /full の rankedPlayCount。読んだ pp > 0 のスコアの件数と合うときだけ外す
    （取得中にページがずれて正常なスコアを読み飛ばしたときは件数が足りない）。None（取れていない）なら外さない。
    戻り値は取り直したスコアの件数。
    \"\"\"
    # 呼ぶ前に別の取得（ランク譜面の取り直しなど）が失敗していたら"""),
("""            print("ScoreSaber: 取り直しの途中で並びが動いた（重複あり）ため、返らなかったスコアは外しません")
        else:""","""            print("ScoreSaber: 取り直しの途中で並びが動いた（重複あり）ため、返らなかったスコアは外しません")
        elif ranked_play_count is None or ranked_play_count != len(fetched):
            # 読み飛ばし（ページのずれ）があると件数が合わない。プロフィールの件数が無いときも確かめられないので外さない
            print(
                f"ScoreSaber: 読んだ pp 付きスコア {len(fetched)} 件がプロフィールの rankedPlayCount "
                f"（{ranked_play_count}）と合わないため、返らなかったスコアは外しません"
            )
        else:"""),
])
edit('src/mybeatsaberstats/collector/collector.py', [
("""                _ss_refresh_ranked_scores(
                    scoresaber_id,
                    session,
                    progress=_ss_reconcile_progress,
                    status=ss_status,
                )""","""                _ss_refresh_ranked_scores(
                    scoresaber_id,
                    session,
                    progress=_ss_reconcile_progress,
                    status=ss_status,
                    # 返らなかったスコアを外す前に、件数を照らし合わせる（この Snapshot で取った /full の値。API は増やさない）
                    ranked_play_count=scoresaber_ranked_play_count,
                )"""),
])
