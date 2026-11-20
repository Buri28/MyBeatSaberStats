"""テスト共通の設定：API の失敗ログ・リクエストログを本物の logs/ ではなく一時フォルダに書く。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "src"))


@pytest.fixture(autouse=True, scope="session")
def _isolate_logs(tmp_path_factory):
    from mybeatsaberstats import api_error_log, beatleader, http_client
    from mybeatsaberstats.collector import beatleader as collector_bl

    log_root = tmp_path_factory.mktemp("logs_root")
    log_dir = log_root / "logs"
    mp = pytest.MonkeyPatch()
    # api_error_log は呼ぶたびに BASE_DIR / "logs" を組み立てる
    mp.setattr(api_error_log, "BASE_DIR", log_root)
    mp.setattr(beatleader, "LOG_DIR", log_dir)
    mp.setattr(beatleader, "LOG_PATH", log_dir / "beatleader_api.log")
    mp.setattr(collector_bl, "LOG_DIR", log_dir)
    mp.setattr(collector_bl, "LOG_PATH", log_dir / "beatleader_api.log")
    mp.setattr(http_client, "REQUEST_LOG_PATH", log_dir / "api_requests.log")
    yield log_dir
    mp.undo()
