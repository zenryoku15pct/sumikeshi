#!/usr/bin/env python3
"""手書きスキャンPDFの定位置マスキングツール（ローカルWebアプリ）

- ブラウザ上でページを見ながら矩形をドラッグ指定し、
  全ページ・複数PDFを一括マスキングする。
  矩形・対象ページの設定はファイルごとに持ち、全ファイルへのコピーもできる。
- マスキングは PyMuPDF のリダクション機能で画像ピクセル自体を消去する
  （黒い四角を上に重ねるだけの「見た目の黒塗り」ではなく、復元不可能）。
- アップロードされたPDFはメモリ上のみで保持し、原本は変更しない。
  出力は ./output/ に別名で保存する（既存ファイルは上書きしない）。
"""
import io
import json
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF
from flask import Flask, abort, jsonify, request, send_file

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
PRESET_DIR = BASE_DIR / "presets"
TRASH_DIR = BASE_DIR / "trash"

HOST = "127.0.0.1"
PORT = 8765
RENDER_DPI = 150
MAX_RENDER_WIDTH = 2200  # 表示用レンダリングの最大幅(px)

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024**3

# アップロード済みPDF（メモリ保持のみ・ディスクに書かない）
_docs: dict[int, dict] = {}  # id -> {"name": str, "data": bytes, "pages": int}
_docs_lock = threading.Lock()
_next_id = 1


# ---------------------------------------------------------------- ユーティリティ
def doc_list() -> list[dict]:
    with _docs_lock:
        return [
            {"id": did, "name": d["name"], "pages": d["pages"]}
            for did, d in sorted(_docs.items())
        ]


def parse_page_spec(spec: str, parity: str, n_pages: int) -> list[int]:
    """'2-30, 35' のようなページ指定を1始まりのページ番号リストに変換する。
    空欄は全ページ。parity は all / odd / even。"""
    spec = (spec or "").strip()
    if not spec:
        pages = set(range(1, n_pages + 1))
    else:
        pages = set()
        for part in re.split(r"[,、\s]+", spec):
            if not part:
                continue
            m = re.fullmatch(r"(\d+)\s*[-−〜~]\s*(\d+)", part)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                if a > b:
                    a, b = b, a
                pages.update(range(a, b + 1))
            elif part.isdigit():
                pages.add(int(part))
            else:
                raise ValueError(f"ページ指定を解釈できません: {part}")
        pages = {p for p in pages if 1 <= p <= n_pages}
    if parity == "odd":
        pages = {p for p in pages if p % 2 == 1}
    elif parity == "even":
        pages = {p for p in pages if p % 2 == 0}
    return sorted(pages)


def clean_rects(raw) -> list[dict]:
    """正規化座標(0〜1)の矩形リストを検証・整形する。"""
    rects = []
    for r in raw or []:
        try:
            x0 = min(float(r["x0"]), float(r["x1"]))
            x1 = max(float(r["x0"]), float(r["x1"]))
            y0 = min(float(r["y0"]), float(r["y1"]))
            y1 = max(float(r["y0"]), float(r["y1"]))
        except (KeyError, TypeError, ValueError):
            raise ValueError("矩形データが不正です")
        x0, y0 = max(0.0, x0), max(0.0, y0)
        x1, y1 = min(1.0, x1), min(1.0, y1)
        if x1 - x0 > 0.001 and y1 - y0 > 0.001:
            rects.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1})
    return rects


def apply_masks(data: bytes, rects: list[dict], color: str, page_numbers: list[int]) -> bytes:
    """リダクションを適用したPDFのバイト列を返す。画像ピクセルも消去する。"""
    rgb = (0, 0, 0) if color == "black" else (1, 1, 1)
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        for pno in page_numbers:
            page = doc[pno - 1]
            pr = page.rect  # 回転適用後の見た目のページ矩形
            for r in rects:
                rect = fitz.Rect(
                    pr.x0 + r["x0"] * pr.width,
                    pr.y0 + r["y0"] * pr.height,
                    pr.x0 + r["x1"] * pr.width,
                    pr.y0 + r["y1"] * pr.height,
                )
                # リダクション矩形は非回転座標系で渡す必要がある
                # （回転0では単位行列なので常に変換してよい・実験で確認済み）
                page.add_redact_annot(rect * page.derotation_matrix, fill=rgb)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
        # メタデータの除去: スキャナが埋め込むタイトル・作成者等に
        # 個人情報が含まれる場合があるため、標準メタデータとXMPを消去する
        doc.set_metadata({})
        doc.del_xml_metadata()
        buf = io.BytesIO()
        doc.save(buf, garbage=4, deflate=True)
        return buf.getvalue()
    finally:
        doc.close()


def unique_out_path(name: str) -> Path:
    stem = Path(name).stem
    path = OUTPUT_DIR / f"{stem}_masked.pdf"
    if not path.exists():
        return path
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = OUTPUT_DIR / f"{stem}_masked_{ts}.pdf"
    i = 1
    while path.exists():
        path = OUTPUT_DIR / f"{stem}_masked_{ts}_{i}.pdf"
        i += 1
    return path


def safe_preset_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|.\x00-\x1f]+', "_", (name or "").strip())
    return name[:60] or "preset"


# ---------------------------------------------------------------- ルーティング
@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.post("/api/upload")
def upload():
    global _next_id
    files = request.files.getlist("files")
    added, errors = [], []
    for f in files:
        name = Path(f.filename or "無題.pdf").name
        data = f.read()
        try:
            with fitz.open(stream=data, filetype="pdf") as doc:
                pages = doc.page_count
            if pages == 0:
                raise ValueError("0ページ")
        except Exception:
            errors.append(name)
            continue
        with _docs_lock:
            did = _next_id
            _next_id += 1
            _docs[did] = {"name": name, "data": data, "pages": pages}
        added.append({"id": did, "name": name, "pages": pages})
    return jsonify({"added": added, "errors": errors, "docs": doc_list()})


@app.get("/api/docs")
def get_docs():
    return jsonify({"docs": doc_list()})


@app.delete("/api/docs/<int:doc_id>")
def remove_doc(doc_id):
    with _docs_lock:
        _docs.pop(doc_id, None)
    return jsonify({"docs": doc_list()})


@app.post("/api/docs/clear")
def clear_docs():
    with _docs_lock:
        _docs.clear()
    return jsonify({"docs": []})


@app.get("/api/page/<int:doc_id>/<int:pno>")
def render_page(doc_id, pno):
    with _docs_lock:
        entry = _docs.get(doc_id)
    if entry is None:
        abort(404)
    doc = fitz.open(stream=entry["data"], filetype="pdf")
    try:
        if not (1 <= pno <= doc.page_count):
            abort(404)
        page = doc[pno - 1]
        zoom = RENDER_DPI / 72
        if page.rect.width * zoom > MAX_RENDER_WIDTH:
            zoom = MAX_RENDER_WIDTH / page.rect.width
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        png = pix.tobytes("png")
    finally:
        doc.close()
    return send_file(io.BytesIO(png), mimetype="image/png")


@app.post("/api/apply")
def apply_endpoint():
    """ファイルごとの設定 {id, rects, range, parity} のリストを受け取り、
    各PDFへ個別にマスキングを適用する。色のみ全ファイル共通。"""
    payload = request.get_json(force=True)
    color = payload.get("color", "black")
    entries = payload.get("files")
    if not isinstance(entries, list) or not entries:
        return jsonify({"error": "ファイルごとの設定がありません"}), 400

    with _docs_lock:
        docs = dict(_docs)
    if not docs:
        return jsonify({"error": "PDFが読み込まれていません"}), 400

    OUTPUT_DIR.mkdir(exist_ok=True)
    results = []
    for ent in entries:
        try:
            did = int(ent.get("id"))
        except (TypeError, ValueError):
            continue
        d = docs.get(did)
        if d is None:  # 既に取り除かれたファイルは無視
            continue
        name = d["name"]
        try:
            rects = clean_rects(ent.get("rects"))
            if not rects:  # 矩形未設定のファイルはスキップ（エラーにしない）
                results.append({"skipped": True, "name": name})
                continue
            pages = parse_page_spec(ent.get("range", ""), ent.get("parity", "all"), d["pages"])
            if not pages:
                raise ValueError("指定範囲に該当するページがありません")
            out_bytes = apply_masks(d["data"], rects, color, pages)
            out_path = unique_out_path(name)
            out_path.write_bytes(out_bytes)
            results.append(
                {"ok": True, "name": name, "output": out_path.name, "pagesMasked": len(pages)}
            )
        except Exception as e:  # 1ファイルの失敗で全体を止めない
            results.append({"ok": False, "name": name, "error": str(e)})
    return jsonify({"results": results, "outputDir": str(OUTPUT_DIR)})


# ---------------------------------------------------------------- プリセット
@app.get("/api/presets")
def list_presets():
    PRESET_DIR.mkdir(exist_ok=True)
    presets = []
    for p in sorted(PRESET_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            data["name"] = p.stem
            presets.append(data)
        except Exception:
            continue
    return jsonify({"presets": presets})


@app.post("/api/presets")
def save_preset():
    payload = request.get_json(force=True)
    name = safe_preset_name(payload.get("name", ""))
    try:
        rects = clean_rects(payload.get("rects"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not rects:
        return jsonify({"error": "保存する矩形がありません"}), 400
    preset = {
        "rects": rects,
        "color": payload.get("color", "black"),
        "range": payload.get("range", ""),
        "parity": payload.get("parity", "all"),
        "saved": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    PRESET_DIR.mkdir(exist_ok=True)
    path = PRESET_DIR / f"{name}.json"
    if path.exists():  # 上書き時は旧ファイルを trash/ に退避
        TRASH_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.move(str(path), str(TRASH_DIR / f"{name}.json.{ts}.bak"))
    path.write_text(json.dumps(preset, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "name": name})


@app.delete("/api/presets/<name>")
def delete_preset(name):
    path = PRESET_DIR / f"{safe_preset_name(name)}.json"
    if path.exists():  # 完全削除はせず trash/ へ退避
        TRASH_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.move(str(path), str(TRASH_DIR / f"{path.name}.{ts}.bak"))
    return jsonify({"ok": True})


@app.post("/api/open-output")
def open_output():
    OUTPUT_DIR.mkdir(exist_ok=True)
    # OSごとのフォルダを開くコマンド（macOS / Windows / Linux）
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(OUTPUT_DIR)])
    elif sys.platform == "win32":
        import os
        os.startfile(str(OUTPUT_DIR))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(OUTPUT_DIR)])
    return jsonify({"ok": True})


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(exist_ok=True)
    PRESET_DIR.mkdir(exist_ok=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}/")).start()
    print(f"PDFマスキングツール: http://{HOST}:{PORT}/ で起動中（Ctrl+C で終了）")
    app.run(host=HOST, port=PORT, threaded=True)
