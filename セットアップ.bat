@echo off
rem 初回セットアップ（Windows）— ダブルクリックで実行
rem Python の実行環境（.venv）を作り、必要なライブラリを入れて、お試し用ダミーPDFを生成します。
cd /d "%~dp0"
chcp 65001 >nul

echo === スミケシ セットアップ（Windows）===

where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    set "PY=python"
  ) else (
    echo.
    echo Python 3 が見つかりません。
    echo https://www.python.org/downloads/ からインストールしてください。
    echo ※インストール時に「Add python.exe to PATH」に必ずチェックを入れること
    echo.
    pause
    exit /b 1
  )
)

echo 1/3 実行環境を作成しています…
%PY% -m venv .venv
if errorlevel 1 ( echo 実行環境の作成に失敗しました & pause & exit /b 1 )

echo 2/3 ライブラリを導入しています…
.venv\Scripts\python -m pip install --quiet --upgrade pip
.venv\Scripts\pip install --quiet -r requirements.txt
if errorlevel 1 ( echo ライブラリの導入に失敗しました & pause & exit /b 1 )

echo 3/3 お試し用ダミーPDFを生成しています…
.venv\Scripts\python make_dummy_sample.py

echo.
echo セットアップ完了。次からは「スミケシ起動.bat」をダブルクリックで起動できます。
pause
