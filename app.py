#!/usr/bin/env python3
"""手書きスキャンPDFの定位置マスキングツール（ローカルWebアプリ）

- ブラウザ上でページを見ながら矩形をドラッグ指定し、
  全ページ・複数PDFを一括マスキングする。
  矩形・対象ページの設定はファイルごとに持ち、全ファイルへのコピーもできる。
- マスキングは PyMuPDF のリダクション機能で画像ピクセル自体を消去する
  （黒い四角を上に重ねるだけの「見た目の黒塗り」ではなく、復元不可能）。
- アップロードされたPDFはメモリ上のみで保持し、原本は変更しない。
  出力は ./output/ に別名で保存する（既存ファイルは上書きしない）。

著作権表示（AGPL-3.0 第7条bに基づく保持要求つき）:
    スミケシ（sumikeshi）  Copyright (C) 2026 全力の15%
    zenryoku15@gmail.com  /  ライセンス: GNU AGPL-3.0（LICENSE 参照）
"""
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF
from flask import Flask, abort, jsonify, request, send_file

# マスキング直後の自動検査に、検査スクリプト（sumikeshi-check）の中身を借りる。
# 画面の「✓」を「検査に通った」の意味にするため、同じ判定を通す
from check_masked import NG as CHECK_NG
from check_masked import WARN as CHECK_WARN
from check_masked import check_page

# Windowsで出力先の文字コードが日本語非対応（cp1252等）のとき、
# 日本語の印字でクラッシュしないよう「表せない文字は?に置換」に切り替える
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

# 実行ファイル版（PyInstaller）では、同梱リソースは展開先（_MEIPASS）、
# 利用者が触るフォルダ（output/presets）は実行ファイルの隣に置く
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    RES_DIR = Path(sys._MEIPASS)  # noqa: SLF001
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    RES_DIR = BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
PRESET_DIR = BASE_DIR / "presets"
TRASH_DIR = BASE_DIR / "trash"

HOST = "127.0.0.1"
PORT = 8765
RENDER_DPI = 150
MAX_RENDER_WIDTH = 2200  # 表示用レンダリングの最大幅(px)

app = Flask(__name__, static_folder=str(RES_DIR / "static"), static_url_path="/static")

# ------------------------------------------------------- 外からの横取りを防ぐ
# このサーバーは自分のPCの中（127.0.0.1）だけで動くので、同じネットワークの
# 他人からは見えない。ただしブラウザ経由の抜け道が2つあるため、そこを塞ぐ。
#
#  1. DNSリバインディング … 攻撃者が自分のドメインの向き先を127.0.0.1へ書き換え、
#     ブラウザに「このアプリ＝攻撃者のサイト」と誤認させる手口。成立すると読み込み
#     中のPDFの画像を外部へ送れてしまう。→ Hostが自分自身でなければ拒否する
#  2. 別サイトからの操作（CSRF）… 悪意あるページを開いているだけで、裏でこのアプリの
#     APIを叩かれる。→ 他サイトから来た書き込み要求は拒否する
ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}


@app.before_request
def _block_foreign_access():
    if request.host not in ALLOWED_HOSTS:
        abort(403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            abort(403)
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
            targets = []
            for r in rects:
                rect = fitz.Rect(
                    pr.x0 + r["x0"] * pr.width,
                    pr.y0 + r["y0"] * pr.height,
                    pr.x0 + r["x1"] * pr.width,
                    pr.y0 + r["y1"] * pr.height,
                )
                targets.append(rect)
                # リダクション矩形は非回転座標系で渡す必要がある
                # （回転0では単位行列なので常に変換してよい・実験で確認済み）
                page.add_redact_annot(rect * page.derotation_matrix, fill=rgb)

            # 塗る範囲に重なる注釈を先に取り除く。
            # apply_redactions は文字を直接描く注釈（FreeText）なら消してくれるが、
            # 付箋やハイライトは残り、そのコメント欄に書かれた氏名も残ってしまう
            # （2026-08-18に実測で確認）。塗ると決めた範囲の情報は種類を問わず消す。
            for annot in list(page.annots() or []):
                if annot.type[0] == fitz.PDF_ANNOT_REDACT:
                    continue  # いま自分で足したリダクション指定は消さない
                if any(not (annot.rect & t).is_empty for t in targets):
                    page.delete_annot(annot)

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


def find_blank_rects(data: bytes, rects: list[dict], page_numbers: list[int]) -> list[int]:
    """塗る前の原本を見て、「元から何も無かった矩形」の番号を返す。

    塗ったあとの検査では、白紙を黒く塗っても「きれいに消えた」と判定されてしまう
    （指定範囲は確かに均一になっているため）。位置を間違えた矩形に気づくには、
    塗る前に中身があったかを見るしかない。1ページでも中身があれば正常とみなす。
    """
    blank = []
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        for idx, r in enumerate(rects):
            has_content = False
            for pno in page_numbers:
                page = doc[pno - 1]
                pr = page.rect
                rect = fitz.Rect(
                    pr.x0 + r["x0"] * pr.width, pr.y0 + r["y0"] * pr.height,
                    pr.x0 + r["x1"] * pr.width, pr.y0 + r["y1"] * pr.height,
                )
                # 文字があるか（テキスト層つきPDF）
                if any(not (fitz.Rect(w[:4]) & rect).is_empty for w in page.get_text("words")):
                    has_content = True
                    break
                # 絵柄に濃淡があるか（スキャン画像）。狭い範囲を粗く描くので軽い
                try:
                    pix = page.get_pixmap(clip=rect, dpi=50, colorspace=fitz.csGRAY)
                except Exception:
                    has_content = True  # 判定できないときは疑わない
                    break
                if pix.samples and (max(pix.samples) - min(pix.samples)) > 24:
                    has_content = True
                    break
            if not has_content:
                blank.append(idx + 1)
    finally:
        doc.close()
    return blank


def verify_masked(out_bytes: bytes, rects: list[dict], page_numbers: list[int]) -> dict:
    """書き出したPDFを読み直し、指定した範囲が本当に消えているか検査する。

    「処理した」と「消えた」は別物なので、実行のたびに機械で確かめる。
    検査そのものは check_masked.py（sumikeshi-check と同じ中身）を使う。
    """
    opt = {"tolerance": 8, "allow_text_outside": True, "detect_white": False, "inset": 2}
    ng_pages, warn_pages, reasons = [], [], []
    doc = fitz.open(stream=out_bytes, filetype="pdf")
    try:
        for pno in page_numbers:
            res = check_page(doc, doc[pno - 1], rects, opt)
            if res["verdict"] == CHECK_NG:
                ng_pages.append(pno)
                reasons.extend(f"{pno}ページ: {r}" for r in res["reasons"])
            elif res["verdict"] == CHECK_WARN:
                warn_pages.append(pno)
                reasons.extend(f"{pno}ページ: {r}" for r in res["reasons"])
    finally:
        doc.close()
    verdict = "ng" if ng_pages else ("warn" if warn_pages else "ok")
    return {
        "verdict": verdict,
        "ngPages": ng_pages,
        "warnPages": warn_pages,
        "reasons": reasons[:6],  # 画面に出すのは先頭だけ。詳細は検査レポートで
    }


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
    global _busy
    payload = request.get_json(force=True)
    color = payload.get("color", "black")
    entries = payload.get("files")
    if not isinstance(entries, list) or not entries:
        return jsonify({"error": "ファイルごとの設定がありません"}), 400
    # 処理中に自動終了しないよう印をつける（書きかけのPDFを残さない）
    with _life_lock:
        _busy += 1
    try:
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
                # 塗る前に「その場所に中身があるか」を見る（位置ずれの検出）
                blank = find_blank_rects(d["data"], rects, pages)
                out_bytes = apply_masks(d["data"], rects, color, pages)
                # 書き出す前に、本当に消えているかを機械で確かめる
                check = verify_masked(out_bytes, rects, pages)
                if blank:
                    nums = "・".join(f"{i}つ目" for i in blank)
                    check["reasons"].insert(
                        0, f"{nums}の枠は、塗る前から何もありませんでした（位置がずれていませんか）"
                    )
                    if check["verdict"] == "ok":
                        check["verdict"] = "warn"
                out_path = unique_out_path(name)
                # 一時ファイルへ書いてから名前を付け替える。書いている途中で
                # 強制終了されても、中途半端なPDFが完成品の顔で残らない
                tmp_path = out_path.with_suffix(".pdf.part")
                tmp_path.write_bytes(out_bytes)
                os.replace(tmp_path, out_path)
                results.append({
                    "ok": True,
                    "name": name,
                    "output": out_path.name,
                    "pagesMasked": len(pages),
                    "check": check,
                })
            except Exception as e:  # 1ファイルの失敗で全体を止めない
                results.append({"ok": False, "name": name, "error": str(e)})
        return jsonify({"results": results, "outputDir": str(OUTPUT_DIR)})
    finally:
        with _life_lock:
            _busy -= 1


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


# ------------------------------------------------- ブラウザを閉じたら終了する
#
# この道具は「ブラウザが操作画面」なので、画面を閉じたらアプリも終わるのが自然。
# 放っておくとサーバーだけ動き続け、本人は閉じたつもりでいるうえ、次回起動時に
# ポートが埋まっていて失敗する。そこで次の2本立てで自動終了する。
#
#   1. タブを閉じた合図（/api/bye）を受け取る … 通常はこれで即座に終わる
#   2. 心拍が途絶えたら終わる（保険）        … 合図が届かない閉じ方への備え
#
# ただし「再読み込み」もタブを閉じたのと同じ合図を出すため、合図の直後に数秒待ち、
# その間に心拍が戻れば終了を取り消す。マスキング処理中も終了しない（書きかけの
# PDFを残さないため）。
# 心拍が途絶えたときの終了は「合図が届かなかった場合の保険」なので、長めに取る。
# ブラウザは裏に回ったタブの定期処理を大幅に間引く（数十秒〜1分に1回まで落ちる）ため、
# 短くすると「別のウィンドウを見ていただけ」でアプリが落ちる。通常の終了は、タブを
# 閉じたときの合図（/api/bye）が即座に効くので、こちらを急ぐ必要はない
BEAT_TIMEOUT = 300.0  # 心拍がこれだけ途絶えたら、ブラウザは閉じたとみなす（秒）
BYE_GRACE = 5.0       # 閉じる合図のあと、再読み込みを待つ猶予（秒）
STARTUP_GRACE = 180.0  # 起動直後、ブラウザが開くまで待つ猶予（秒）

_life_lock = threading.Lock()
_clients: set[str] = set()      # 画面を開いているタブ
_last_beat = 0.0                # 最後に心拍が届いた時刻（0 = まだ一度も来ていない）
_exit_at: float | None = None   # この時刻を過ぎたら終了する
_busy = 0                       # 実行中のマスキング処理の数


def _touch(client_id: str | None) -> None:
    """タブから連絡が来た。生存時刻を更新し、予約済みの終了を取り消す。"""
    global _last_beat, _exit_at
    with _life_lock:
        _last_beat = time.monotonic()
        if client_id:
            _clients.add(client_id)
        _exit_at = None


@app.post("/api/heartbeat")
def heartbeat():
    _touch((request.get_json(silent=True) or {}).get("client_id"))
    return jsonify({"ok": True})


@app.post("/api/bye")
def bye():
    """タブが閉じられた（または再読み込みされた）合図。"""
    global _exit_at
    # sendBeaconはContent-Typeが付かないことがあるため、緩く読む
    payload = request.get_json(silent=True) or {}
    if not payload and request.data:
        try:
            payload = json.loads(request.data.decode("utf-8"))
        except Exception:
            payload = {}
    with _life_lock:
        _clients.discard(payload.get("client_id"))
        if not _clients:
            _exit_at = time.monotonic() + BYE_GRACE
    return jsonify({"ok": True})


@app.post("/api/quit")
def quit_app():
    """画面の「終了」ボタン。処理中でなければ、返事をしてから終わる。"""
    with _life_lock:
        if _busy:
            return jsonify({"error": "マスキング処理中です。終わるまでお待ちください"}), 409

    def _bye_soon():
        time.sleep(0.5)  # この返事がブラウザに届くまで待つ
        print("終了ボタンが押されました。終了します。")
        sys.stdout.flush()
        os._exit(0)  # noqa: S606

    threading.Thread(target=_bye_soon, daemon=True).start()
    return jsonify({"ok": True})


def _watchdog() -> None:
    global _last_beat
    started = time.monotonic()
    last_check = started
    while True:
        time.sleep(1.0)
        now = time.monotonic()
        # ノートPCを閉じている間は心拍も止まる。復帰した瞬間に「通信が途絶えた」と
        # 誤判定して終了すると、読み込み済みのPDFと設定した矩形（サーバーのメモリ上）
        # がまとめて消える。1秒待ちのはずが大きく飛んでいたらスリープとみなし、
        # その回は判定を見送って次の心拍を待つ。
        if now - last_check > 5.0:
            last_check = now
            with _life_lock:
                if _last_beat:
                    _last_beat = now
            continue
        last_check = now
        with _life_lock:
            if _busy:                     # 処理中は終了しない
                continue
            if _exit_at and now >= _exit_at and not _clients:
                reason = "画面が閉じられました"
            elif _last_beat and now - _last_beat > BEAT_TIMEOUT:
                reason = "画面との通信が途絶えました"
            elif not _last_beat and now - started > STARTUP_GRACE:
                reason = "画面が開かれませんでした"
            else:
                continue
        print(f"{reason}。終了します。")
        sys.stdout.flush()
        os._exit(0)  # noqa: S606  Flaskの開発サーバーを確実に止める


@app.post("/api/open-output")
def open_output():
    OUTPUT_DIR.mkdir(exist_ok=True)
    # OSごとのフォルダを開くコマンド（macOS / Windows / Linux）
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(OUTPUT_DIR)])
    elif sys.platform == "win32":
        os.startfile(str(OUTPUT_DIR))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(OUTPUT_DIR)])
    return jsonify({"ok": True})


def _already_running() -> bool:
    """すでに起動しているかを調べる（ポートが埋まっているか）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, PORT)) == 0


if __name__ == "__main__":
    # 二重起動：エラーで落とさず、既に開いている画面を出してやる
    if _already_running():
        # ここでブラウザを開くと同じ画面のタブが2枚になる。2枚目には読み込み済みの
        # PDFが映らないので「ファイルが消えた」と誤解させる。タブは開かず案内だけ出す
        print("スミケシはすでに起動しています。")
        print("開いているブラウザのタブに戻ってください。")
        print(f"（タブが見当たらないときは、ブラウザで {HOST}:{PORT} を開いてください）")
        time.sleep(5)  # 黒い画面が一瞬で消えると読めないため
        sys.exit(0)

    OUTPUT_DIR.mkdir(exist_ok=True)
    PRESET_DIR.mkdir(exist_ok=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}/")).start()
    threading.Thread(target=_watchdog, daemon=True).start()
    print(f"PDFマスキングツール: http://{HOST}:{PORT}/ で起動中")
    print("終了するときは、画面の「終了」ボタンか、ブラウザのタブを閉じてください。")
    app.run(host=HOST, port=PORT, threaded=True)
