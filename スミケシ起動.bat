@echo off
rem スミケシ起動（Windows・ソースから使う場合）— ダブルクリックで実行
rem
rem 配布版（sumikeshi.exe）を使う場合はこのファイルは不要。exeを直接ダブルクリックする。
cd /d "%~dp0"
chcp 65001 >nul

if not exist .venv\Scripts\pythonw.exe (
  echo まだセットアップが済んでいません。
  echo 先に「セットアップ.bat」をダブルクリックしてください。
  pause
  exit /b 1
)

rem pythonw.exe は黒い画面を出さない。start で切り離すのでこの窓もすぐ閉じる。
start "" .venv\Scripts\pythonw.exe gui.py
