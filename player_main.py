"""Stats 画面ランチャー。

EXE (ビルド時): dist/MyBeatSaberStatsPlayer/_internal/lib/mybeatsaberstats/ の .py を読み込む。
開発時:         src/mybeatsaberstats/ の .py を読み込む。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def _setup_lib_path() -> None:
    """EXE 実行時は _internal/lib/、開発時は src/ を sys.path に追加する。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 6.x: sys._MEIPASS = <exeの隣>/_internal/
        meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        lib = meipass / "lib"
    else:
        lib = _ROOT / "src"
    path_str = str(lib)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)


_setup_lib_path()

# 静的インポートにすることで PyInstaller が PySide6/requests 等の依存を追跡できる
from mybeatsaberstats.player_app import run  # noqa: E402

if __name__ == "__main__":
    run()
