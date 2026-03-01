"""PyInstaller runtime hook: _internal/lib/ の mybeatsaberstats を PYZ より優先して読み込む。

PYZ アーカイブ (FrozenImporter) より先に sys.meta_path に挿入することで、
GitHub 差分更新で書き換えた lib/ の .py が即座に有効になる。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


class _LibDirFinder:
    """_internal/lib/mybeatsaberstats/ を優先する sys.meta_path ファインダー。"""

    def __init__(self, lib_path: Path) -> None:
        self._lib = lib_path

    def find_spec(self, fullname: str, path, target=None):  # type: ignore[override]
        if not fullname.startswith("mybeatsaberstats"):
            return None

        parts = fullname.split(".")

        if len(parts) == 1:
            # トップレベルパッケージ: lib/mybeatsaberstats/__init__.py
            pkg_dir = self._lib / parts[0]
            init = pkg_dir / "__init__.py"
            if init.exists():
                return importlib.util.spec_from_file_location(
                    fullname,
                    init,
                    submodule_search_locations=[str(pkg_dir)],
                )
        else:
            # サブモジュール: lib/mybeatsaberstats/xxx.py or lib/mybeatsaberstats/xxx/__init__.py
            sub_py  = self._lib.joinpath(*parts[1:]).with_suffix(".py")
            sub_pkg = self._lib.joinpath(*parts[1:]) / "__init__.py"
            if sub_py.exists():
                return importlib.util.spec_from_file_location(fullname, sub_py)
            if sub_pkg.exists():
                return importlib.util.spec_from_file_location(
                    fullname,
                    sub_pkg,
                    submodule_search_locations=[str(sub_pkg.parent)],
                )
        return None


if getattr(sys, "frozen", False):
    _meipass = Path(getattr(sys, "_MEIPASS", ""))
    _lib = _meipass / "lib"
    if _lib.exists():
        # FrozenImporter (PYZ) より前に挿入
        sys.meta_path.insert(0, _LibDirFinder(_lib))
