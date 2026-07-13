"""アプリの予期せぬクラッシュを診断可能にするための共通フック。

PySide6 ではシグナル接続されたスロット内で未捕捉例外が発生すると、トレースが
表示されないままプロセスごと終了（クラッシュ）してしまう。ここで sys.excepthook を
差し替えて例外をログへ残し、ネイティブな致命的エラーは faulthandler で捕捉する。
"""

import sys
import faulthandler
import logging
import traceback
from pathlib import Path
from typing import Optional, TextIO

_installed = False


def install_crash_guard() -> None:
    """未捕捉例外・致命的エラーをログへ残し、アプリの即死を可能な限り防ぐ。"""
    global _installed
    if _installed:
        return
    _installed = True

    log_dir = Path.home() / ".mybeatsaberstats"
    crash_log: Optional[TextIO] = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        crash_log = open(log_dir / "crash.log", "a", encoding="utf-8")
        faulthandler.enable(crash_log)
    except Exception:
        crash_log = None

    logger = logging.getLogger("mybeatsaberstats.crash")

    def _hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.error("Unhandled exception:\n%s", text)
        if crash_log is not None:
            try:
                crash_log.write("Unhandled exception:\n" + text + "\n")
                crash_log.flush()
            except Exception:
                pass
        sys.stderr.write(text)

    sys.excepthook = _hook
