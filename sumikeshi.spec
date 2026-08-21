# -*- mode: python ; coding: utf-8 -*-
# スミケシのビルド設計図（PyInstaller）
#
# 実行ファイルは1本だけ（sumikeshi）。マスキングも検査も、この1本の中の画面で行う。
# v1系にあったコンソール版 sumikeshi-check は、アプリの「検査」画面に取り込んだ。
#
# console=False にしているのは配布物の体裁のため。黒い画面を背面に出さない。
# そのぶん起動に失敗しても何も表示されないので、中核の生死は `sumikeshi --selftest`
# （Qtを起動せずに走る自己診断）で確かめる。CIの動作確認もこれを使う。
#
# ビルド: pyinstaller --noconfirm --clean sumikeshi.spec
# 出力  : dist/sumikeshi/            （Windows・Linux）
#         dist/スミケシ.app          （macOS）

import sys

APP_VERSION = "2.0.0"

# Windowsのexeに製品名・バージョン等の情報を埋め込む。
# この情報が空のexeはウイルス対策ソフトの疑い度が上がる（誤検知対策の一環）。
def _win_version_info(description, original_filename):
    if sys.platform != "win32":
        return None
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
        StringStruct, VarFileInfo, VarStruct,
    )
    ver_tuple = tuple(int(x) for x in APP_VERSION.split(".")) + (0,)
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=ver_tuple, prodvers=ver_tuple),
        kids=[
            StringFileInfo([StringTable("041104b0", [
                StringStruct("ProductName", "sumikeshi"),
                StringStruct("FileDescription", description),
                StringStruct("FileVersion", APP_VERSION),
                StringStruct("ProductVersion", APP_VERSION),
                StringStruct("OriginalFilename", original_filename),
                StringStruct("CompanyName", "sumikeshi project (zenryoku15pct)"),
                StringStruct("LegalCopyright", "AGPL-3.0"),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0411, 1200])]),
        ],
    )

# 使わないQtの部品を落として配布サイズを抑える。
# PySide6-Essentials の時点で付録（Addons）は入っていないので、ここは残りの整理。
EXCLUDES = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtWebSockets", "PySide6.QtWebChannel", "PySide6.QtSerialPort",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSensors", "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech",
    "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtUiTools",
    "PySide6.QtOpenGLWidgets", "PySide6.Qt3DCore",
    "tkinter", "unittest", "pydoc", "doctest", "email", "http", "xmlrpc",
]

a = Analysis(
    ["gui.py"],
    datas=[
        ("assets/icon.png", "assets"),
        ("assets/check.png", "assets"),
        # --selftest がどこでも走れるよう、お試し用PDFを同梱する
        ("samples/ダミー調査票.pdf", "samples"),
    ],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="sumikeshi",
    console=False,                      # 背面に黒い画面を出さない
    disable_windowed_traceback=False,
    icon="assets/icon.ico" if sys.platform == "win32" else None,
    version=_win_version_info(
        "sumikeshi - PDF masking tool with verification", "sumikeshi.exe"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="sumikeshi",
)

# macOSは .app にする。これで初めて、Finderのダブルクリックで
# ターミナルが開かずに立ち上がる。
# LGPL（PySide6）の要件との関係でも、1ファイルにまとめる形（onefile）ではなく
# Qtのライブラリが個別に置かれるこの形（onedir）を使う。差し替えの余地を残すため。
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="スミケシ.app",
        icon="assets/icon.icns",
        bundle_identifier="jp.zenryoku15pct.sumikeshi",
        version=APP_VERSION,
        info_plist={
            "CFBundleName": "スミケシ",
            "CFBundleDisplayName": "スミケシ",
            "CFBundleShortVersionString": APP_VERSION,
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.productivity",
            "NSHumanReadableCopyright": "Copyright (C) 2026 全力の15%  /  AGPL-3.0",
        },
    )
