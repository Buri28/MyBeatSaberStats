# 比較画面の右側行高復元

- 元の高さ: Git HEAD 0085f19 の snapshot_view.py は _row_height を21で初期化し、_apply_row_height で全表に適用。cache/snapshot_compare.json の保存値も21。
- #336 の保存画像 verification/top-compare/compare-compact.png と compare-details.png を確認。現行の _fit_comparison_tables が左右とも左側用の自動調整値を適用していた。
- 修正: 自動調整の対象を table/table_acc のみに限定。右側SS/BLは基準行高を維持。文字・配置・Acc切替のコードは変更なし。
- 実測: 1540x887で左19/右21、1000x650で左18/右21。フォントは全表 Yu Gothic UI 9 のまま。
- 保存画像: normal.png、details.png、small-bottom.png、default.png。before-right-restoration.png は変更直前の右19を現行UI上で再現した比較用画像（過去画像ではない）。
- 確認: 通常時は全行を表示、縮小時はスクロールで最終行到達。Acc詳細切替、手動行高変更、Default layout復帰も成功。
- テスト: pytest test_top_compare_pp.py test_pp_layout_accsaber.py -q は15成功。check_compare_rows.py の実GUI検査も成功。requests は全て禁止し保存Snapshotのみ使用。
- レビュー: サブエージェントの最終レビュー指摘なし。独立した関連2テストも成功。
- プロセス: process.json/owned-processes.json に記録。確認用PID37724/40464は通常終了を確認。既存アプリは操作・終了していない。
