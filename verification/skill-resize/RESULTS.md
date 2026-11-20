# BeatLeader Skill・比較行高の確認結果

- 対象: src/mybeatsaberstats/player_app.py、player_pp.py、snapshot_view.py。
- 今回分の差分: changes.patch。作業前の3ソースは *.before.py に保存。
- タイトルは BeatLeader Skill、内訳は Acc PP / Tech PP / Pass PP。合計PP名は保持。
- 表示順と独立した項目キーで上限を対応させ、トップの履歴別上限と比較のバー基準を維持。
- 左側の Resize / Show / Hide / LayoutRequest を集約し、Qtレイアウト更新後に再計算。
- 以前の親・表の実寸差を使う計算を、表示中のレイアウト要素・ヘッダー・枠・スクロールバーに必要な高さの計算へ変更。
- 最小行高とフォント、右側の基準21、手動▲▼操作を維持。最小サイズを大きく固定する変更はなし。

## テスト

`QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest test_player_pp.py test_top_compare_pp.py test_pp_layout_accsaber.py test_skill_resize.py -q`

- 30件成功、終了コード0（tests-final.log）。既存の datetime.utcnow 非推奨警告16件。
- 新規テスト: 明暗両テーマで大→小→大→同サイズ復帰、外側ウィンドウのサイズが変わらない領域更新、可読性の下限、最終行へのスクロール、右21、手動調整・初期化。
- 順序、歴史別上限、ゼロ・欠損、差分符号・色、バー比率、L/R切替は保存データ・模擬応答で確認。
- テスト終了時のQt異常終了は新規テストが閉じたQtウィンドウを明示削除することで解消。最終実行は正常終了。
- 一括サブエージェントレビュー: 今回分に具体的な不具合指摘なし。タイマー収束、表示切替、最小サイズからの復元、Qt削除も追加確認。

## 実画面

検証コマンド: `.venv/Scripts/python.exe verification/check_skill_resize.py`
HTTP要求を禁止し、Snapshot・設定は verification/skill-resize/offline に複製。実Snapshot取得・大量API取得なし。

|状態|クライアントサイズ|左2表の行高|右表の行高|画像|
|---|---|---|---|---|
|通常|1540×887|19|21|compare-00-1540x887.png|
|拡大|1707×889|19|21|compare-02-1707x889.png|
|縮小|1540×425|18|21|compare-06-1540x425.png|
|再拡大|1707×889|19|21|compare-08-1707x889.png|
|再縮小|1540×425|18|21|compare-10-1540x425.png|
|再拡大・L/R|1707×889|19|21|compare-13-1707x889.png|

- geometry.json に全行高・viewport寸法・スクロール範囲・最小サイズを記録。同サイズの可視4表は復帰前後で一致。
- 縮小時は左上をPass PP、左下をSkill Level/Techまで実際にスクロールし、末尾への到達を画面確認。
- トップ画像: top.png。top-geometry.json に内訳とEventsが各241、ボタンとの間隔10を記録。
- 代表PNG3枚は save_screen.py で stats_skill / compare_small / compare_restored として登録。履歴添付先は attachments.json。

## プロセス

- 起動前一覧: processes-before.json。確認用PID44316、起動2026-11-19T01:57:27.534301、実行ファイル C:/work/tool/MyBeatSaberStats/.venv/Scripts/python.exe（process.json）。ランチャー記録: launcher-process.json。
- 比較・トップをAlt+F4で通常終了。gui.log の NORMAL EXIT、対象PIDとランチャーPIDの消滅、ウィンドウ一覧から確認用2窓の消滅を確認。
- 起動前から存在した My Beat Saber Stats β のウィンドウは保持。

残作業なし。ユーザーの操作不要。
