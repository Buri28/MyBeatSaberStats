# MyBeatSaberStats β

Beat Saber の SteamIdに紐づく Statsをデスクトップから閲覧するための Python + PySide6 アプリです。  
β版準備中…

現状は、ほぼAI生成であまりソースコードが確認できてないのでβ版としています。   
まだ動作があやしい部分があるかと思いますので  
何かおかしな点がありましたらXなどで教えていただけると助かります。  

※開発中のイメージ  
## Stats
<img width="70%" height="70%" alt="Image" src="https://github.com/user-attachments/assets/8f94d241-ac93-475d-9130-d9bc9db834b7" />

Take Snapshotで取得した最新のスナップショットを表示します。(※1)  
Acc Saberの国別ランキングのみFetch Ranking Dataで取得したindex(プレイヤーの国を紐づける)ファイルを使用します。  

<sup>$\color{green}{\text{※1 デフォルトでは前回取得時からの差分を取得、ランクマップが増えた場合は前回取得時の60日前のマップから再取得します。}}$</sup>    
<sup>$\color{red}{\text{うまくランクが反映されない場合は、Score Fetch ModeでFetch ALLをチェックしてスナップショットを取得してください。}}$</sup>  

| 機能       | 概要                     | 
|------------|--------------------------|
| Take Snapshot | 現在のスナップショットを取得します。  | 
| Snapshot Compare | 取得したスナップショットを比較します。         | 
| Snapshot Graph | スナップショットの各項目をグラフ表示します。         | 
| Ranking | Fetch Ranking Dataで取得したSocreSaber、BeatLeader、AccSaberのランキング画面を開きます。        | 
| Fetch Ranking Data | SocreSaber、BeatLeader、AccSaberのランキングデータを取得します。        | 
| Dark / Light | ダークモードとライトモードを切り替えます。        | 
| Update | プログラムをアップデートします。(動作確認できてないので動くかわかりません)        | 

## スナップショット比較
<img width="70%" height="70%" alt="Image" src="https://github.com/user-attachments/assets/4070f851-cccd-4578-bdc5-64ff91943323" />

取得したスナップショットを比較します。  

## ランキング
<img width="70%" height="70%" alt="Image" src="https://github.com/user-attachments/assets/e242952e-f53c-4932-b217-665192db1f4c" />

stats画面のFetch Ranking DataおよびFull Sync(index)で取得したデータをSocreSaber、BeatLeader、AccSaberのランキングを表示します。  
※ScoreSaberは4000PP以上、BeatLeaderは5000PP以上を対象としています。※変わる可能性あり  
<sup>$\color{red}{\text{若干挙動があやしい気がしています…}}$</sup>  

| 機能       | 概要                     | 
|------------|--------------------------|
| Reload ScoreSaber | ScoreSaberの国別ランキングを取得します。(PP制限なし ※変わる可能性あり)  | 
| Reload BeatLeader| BeatLeaderの国別ランキングを取得します。(PP制限なし ※変わる可能性あり)         | 
| Reload AccSaber | AccSaberのランキングを取得します。         | 
| Full Sync(index) | SocreSaber、BeatLeader、AccSaberのランキングデータを取得します。        | 
| Dark / Light | ダークモードとライトモードを切り替えます。        | 
| Update | プログラムをアップデートします。(動作確認できてないので動くかわかりません)        | 
| ヘッダ | ヘッダを押した列でソートします。        | 

## グラフ
<img width="70%" height="70%" alt="Image" src="https://github.com/user-attachments/assets/b4c687e7-e980-4a5f-bc8c-8e60d9220d3b" />

指定した期間のスナップショットを項目ごとにグラフ表示します。※あまり作りこんでいません

