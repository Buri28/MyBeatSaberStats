"""ScoreSaber スコア取得の手動確認スクリプト。

pytest の収集対象になるファイル名だが自動テストではないため、
実際の API 呼び出しは __main__ 実行時だけに限定している。
"""

import requests

from mybeatsaberstats.collector.scoresaber import _get_scoresaber_player_scores


def main() -> None:
    session = requests.Session()
    try:
        scores = _get_scoresaber_player_scores("3117609721598571", session)
    finally:
        session.close()
    print("len", len(scores))
    if scores:
        print(list(scores[0].keys()))


if __name__ == "__main__":
    main()
