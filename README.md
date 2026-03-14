# MyBeatSaberStats

Beat Saber の SteamIdに紐づく Statsをデスクトップから閲覧するための Python + PySide6 アプリです。  
β版準備中…

ほぼAI生成でソースコードが確認できてないのでβ版としています。   
おそらく動作があやしい部分があります。  
おかしな点があったらXなどで教えていただけると助かります。  

※開発中のイメージ  
## Stats
<img width="50%" height="50%" alt="image" src="https://github.com/user-attachments/assets/e764094b-5fe2-4691-8951-5c5917f6af31" />

Take Snapshotで取得した最新のスナップショットを表示します。(※1)  
Acc Saberの国別ランキングのみFetch Ranking Dataで取得したindex(プレイヤーの国を紐づける)ファイルを使用します。  

<sup>$\color{green}{\text{※1 デフォルトでは前回取得時からの差分を取得、ランクマップは増えた場合は前回取得時の2か月前から取得します。}}$</sup>    
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
<img width="50%" height="50%" alt="image" src="https://github.com/user-attachments/assets/b1194ea1-ce56-4e97-b8c6-a91dffcdb2f3" />

取得したスナップショットを比較します。  

## ランキング
<img width="50%" height="50%" alt="image" src="https://github.com/user-attachments/assets/3766d373-66dc-4e1b-918c-5e1edacd26e6" />

stats画面のFetch Ranking DataおよびFull Sync(index)で取得したデータをSocreSaber、BeatLeader、AccSaberのランキングを表示します。  
※ScoreSaberは4000PP以上、BeatLeaderは5000PP以上を対象としています。※変わる可能性あり


| 機能       | 概要                     | 
|------------|--------------------------|
| Reload ScoreSaber | ScoreSaberの国別ランキングを取得します。(PP制限なし ※変わる可能性あり)  | 
| Reload BeatLeader| BeatLeaderの国別ランキングを取得します。(PP制限なし ※変わる可能性あり)         | 
| Reload AccSaber | AccSaberのランキングを取得します。         | 
| Full Sync(index) | SocreSaber、BeatLeader、AccSaberのランキングデータを取得します。        | 
| Dark / Light | ダークモードとライトモードを切り替えます。        | 
| Update | プログラムをアップデートします。(動作確認できてないので動くかわかりません)        | 

## グラフ
指定した期間のスナップショットを項目ごとにグラフ表示します。※あまり作りこんでいません
