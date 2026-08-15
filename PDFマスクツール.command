#!/bin/bash
# PDFマスキングツール起動スクリプト（ダブルクリックで起動）
cd "$(dirname "$0")"
echo "PDFマスキングツールを起動します…（終了するにはこのウィンドウで Ctrl+C）"
./.venv/bin/python app.py
