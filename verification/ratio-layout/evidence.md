# 比較バー・トップ幅の確認

- 対象: src の実GUI。exe配布物は変更せず、既存venvのpython.exeで保存済みSnapshotを読み込んだ。
- HTTPはisolate_uiで拒否。Snapshot新規取得は行っていない。検証データと設定はoffline配下のコピー。
- 関連pytest: 27 passed。git diff --check成功（既存ファイルの改行警告のみ）。
- 比較: PP上限3000/1200/14500、SS/BLの各Snapshotのmap_count合計で割合を検査。5項目のA/B、欠損、0、差分表示を検査。
- 実画面: compare-missing.png、compare-zero-missing.png、compare-lr.pngで背景バー、欠損と0、L/R表示を確認。右行高21を検査。左行高の既存自動調整は変更していない。
- トップ: top.pngで1211×770（DPI 150%）の文字とイベント表示を確認。PP/Events各241論理px、ボタンとの間隔10px（従来6px）。PP3行・現在PP/上限を維持。
- レビュー: サブエージェントで今回の変更全体を確認、修正必須指摘なし。初回の比較画像が空との指摘は、検証用ダイアログをイベントループ開始前に表示して再撮影し解消。
- 起動記録: ../ratio-process.json、../ratio-process2.json、../ratio-process3.json。検証用のみ通常イベントループ終了で閉じ、既存アプリは終了していない。
- 画像4種類をsave_screen.pyで保存、代表3枚をaitask uploadsへ保存した。
