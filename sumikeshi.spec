# -*- mode: python ; coding: utf-8 -*-
# スミケシのビルド設計図（PyInstaller）
#
# 1つの配布フォルダに2つの実行ファイルを同居させる：
#   sumikeshi       … マスキング本体（起動するとブラウザが開く）
#   sumikeshi-check … マスク後の全ページ自動検査（ダブルクリックで隣の output/ を検査）
# ライブラリ（_internal）は共有するため、フォルダサイズは1本分＋α で済む。
#
# ビルド: pyinstaller --noconfirm --clean sumikeshi.spec
# 出力  : dist/sumikeshi/

import sys

APP_VERSION = "1.0.0"

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

a_app = Analysis(
    ["app.py"],
    datas=[("static", "static")],
    noarchive=False,
)
a_chk = Analysis(
    ["check_masked.py"],
    noarchive=False,
)

pyz_app = PYZ(a_app.pure)
pyz_chk = PYZ(a_chk.pure)

exe_app = EXE(
    pyz_app,
    a_app.scripts,
    exclude_binaries=True,
    name="sumikeshi",
    console=True,
    disable_windowed_traceback=False,
    version=_win_version_info(
        "sumikeshi - PDF masking tool with verification", "sumikeshi.exe"),
)
exe_chk = EXE(
    pyz_chk,
    a_chk.scripts,
    exclude_binaries=True,
    name="sumikeshi-check",
    console=True,
    disable_windowed_traceback=False,
    version=_win_version_info(
        "sumikeshi-check - masked PDF verification", "sumikeshi-check.exe"),
)

coll = COLLECT(
    exe_app,
    a_app.binaries,
    a_app.datas,
    exe_chk,
    a_chk.binaries,
    a_chk.datas,
    name="sumikeshi",
)
