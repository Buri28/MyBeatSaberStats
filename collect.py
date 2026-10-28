import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# src を sys.path に足してから読み込む（先に import すると、リポジトリ直下から実行したとき ImportError になる）
from mybeatsaberstats.collector.collector import main  # noqa: E402


if __name__ == "__main__":
    main()
