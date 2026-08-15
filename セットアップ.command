#!/bin/bash
# 初回セットアップ（macOS）— ダブルクリックで実行
# Python の実行環境（.venv）を作り、必要なライブラリを入れて、お試し用ダミーPDFを生成します。
cd "$(dirname "$0")"
set -e

echo "=== PDFマスキングツール セットアップ（macOS）==="

if ! command -v python3 >/dev/null 2>&1; then
  echo ""
  echo "Python 3 が見つかりません。"
  echo "ターミナルで次の1行を実行してから、もう一度このファイルをダブルクリックしてください。"
  echo ""
  echo "    xcode-select --install"
  echo ""
  exit 1
fi

echo "1/3 実行環境を作成しています…"
python3 -m venv .venv
echo "2/3 ライブラリを導入しています…"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
echo "3/3 お試し用ダミーPDFを生成しています…"
./.venv/bin/python make_dummy_sample.py

echo ""
echo "セットアップ完了。次からは「PDFマスクツール.command」をダブルクリックで起動できます。"
