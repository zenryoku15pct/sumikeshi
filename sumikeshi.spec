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
)
exe_chk = EXE(
    pyz_chk,
    a_chk.scripts,
    exclude_binaries=True,
    name="sumikeshi-check",
    console=True,
    disable_windowed_traceback=False,
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
