@echo off
rem PDFマスキングツール起動スクリプト（Windows）— ダブルクリックで起動
cd /d "%~dp0"
chcp 65001 >nul

if not exist .venv\Scripts\python.exe (
  echo まだセットアップが済んでいません。
  echo 先に「セットアップ.bat」をダブルクリックしてください。
  pause
  exit /b 1
)

echo PDFマスキングツールを起動します…（終了するにはこのウィンドウで Ctrl+C）
.venv\Scripts\python app.py
pause
