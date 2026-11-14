# -*- mode: python ; coding: utf-8 -*-
#
# --onedir ビルド: mybeatsaberstats の .py は lib/ に分離して差分更新を可能にする
#

a = Analysis(
    ['player_main.py'],
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

# 同梱しない Qt のファイル。
# - Qt Virtual Keyboard: Qt の無償版では GPLv3 のみ。同梱するとアプリ全体を GPLv3 にする必要がある。
#   PyInstaller が platforminputcontexts のプラグインとして自動で拾う。
# - QML / Quick（Virtual Keyboard が使う）、PDF（画像形式プラグイン qpdf が使う）、
#   opengl32sw（Mesa のソフトウェア OpenGL）: アプリは QtCore / QtGui / QtSvg / QtWidgets だけを使う。
# 外したものは make_third_party_notices.py の通知にも載らない。
_EXCLUDED_QT_PREFIXES = (
    "qt6virtualkeyboard", "qtvirtualkeyboardplugin",
    "qt6qml", "qt6quick",
    "qt6pdf", "qpdf.",
    "opengl32sw.",
)


def _keep_entry(entry):
    name = entry[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    return not name.startswith(_EXCLUDED_QT_PREFIXES)


a.binaries = [entry for entry in a.binaries if _keep_entry(entry)]
a.datas = [entry for entry in a.datas if _keep_entry(entry)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    name='MyBeatSaberStats',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='resources/app_icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    # UPX で圧縮すると Qt の DLL が書き換わり、通知の「改変していない」と合わなくなるので使わない。
    upx=False,
    upx_exclude=[],
    name='MyBeatSaberStats',
)
