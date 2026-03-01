# -*- mode: python ; coding: utf-8 -*-
#
# --onedir ビルド: mybeatsaberstats の .py は lib/ に分離して差分更新を可能にする
#

a = Analysis(
    ['main.py'],
    # 分析時に src 配下を解決できるようにする
    pathex=['src'],
    binaries=[],
    datas=[
        # resources 配下のアイコン類
        ('resources', 'resources'),
        # mybeatsaberstats の .py を lib/ に配置（PYZ に含めず、更新可能な状態にする）
        ('src/mybeatsaberstats', 'lib/mybeatsaberstats'),
        # バージョン情報
        ('version.json', '.'),
    ],
    # mybeatsaberstats は lib/ から読み込むため hiddenimports に含めない
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['pyi_rth_libpref.py'],
    # excludes を設定しない: PyInstaller が mybeatsaberstats 経由で PySide6/requests 等を正しく追跡できるようにする
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    name='MyBeatSaberRanking',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='MyBeatSaberRanking',
)
