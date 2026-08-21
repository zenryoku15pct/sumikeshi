#!/usr/bin/env python3
"""スミケシのマスキング中核（画面から独立した部分）

app.py（ブラウザ版）から移設した。実機テストとセキュリティ点検を通った部分
なので、移設にあたって処理内容は一切変えていない。ここを変えるときは、
同じ入力から同じバイト列のPDFが出ることを毎回確かめること。

- マスキングは PyMuPDF のリダクション機能で画像ピクセル自体を消去する
  （黒い四角を上に重ねるだけの「見た目の黒塗り」ではなく、復元不可能）。
- 原本は読むだけで、変更しない。出力は ./output/ に別名で保存する。

著作権表示（AGPL-3.0 第7条bに基づく保持要求つき）:
    スミケシ（sumikeshi）  Copyright (C) 2026 全力の15%
    zenryoku15@gmail.com  /  ライセンス: GNU AGPL-3.0（LICENSE 参照）
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF

# マスキング直後の自動検査に、検査スクリプト（check_masked.py）の中身を借りる。
# 画面の「✓」を「検査に通った」の意味にするため、同じ判定を通す
from check_masked import NG as CHECK_NG
from check_masked import WARN as CHECK_WARN
from check_masked import check_page

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

RENDER_DPI = 150
MAX_RENDER_WIDTH = 2200  # 表示用レンダリングの最大幅(px)


# ---------------------------------------------------------------- ページ指定
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


# ---------------------------------------------------------------- マスキング
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


def mask_one_file(name: str, data: bytes, rects_raw, color: str,
                  page_range: str, parity: str, n_pages: int) -> dict:
    """1ファイル分の一連の処理。app.py の /api/apply のループ内と同じ順序・同じ判定。

      矩形の整形 → ページ指定の解釈 → 塗る前の位置ずれ検出 → マスキング
      → 書き出したPDFを読み直して検査 → 一時ファイル経由で保存

    戻り値は画面がそのまま表示できる形（ブラウザ版の results と同じ）。
    """
    rects = clean_rects(rects_raw)
    if not rects:  # 矩形未設定のファイルはスキップ（エラーにしない）
        return {"skipped": True, "name": name}
    pages = parse_page_spec(page_range, parity, n_pages)
    if not pages:
        raise ValueError("指定範囲に該当するページがありません")

    # 塗る前に「その場所に中身があるか」を見る（位置ずれの検出）
    blank = find_blank_rects(data, rects, pages)
    out_bytes = apply_masks(data, rects, color, pages)
    # 書き出す前に、本当に消えているかを機械で確かめる
    check = verify_masked(out_bytes, rects, pages)
    if blank:
        nums = "・".join(f"{i}つ目" for i in blank)
        check["reasons"].insert(
            0, f"{nums}の枠は、塗る前から何もありませんでした（位置がずれていませんか）"
        )
        if check["verdict"] == "ok":
            check["verdict"] = "warn"

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = unique_out_path(name)
    # 一時ファイルへ書いてから名前を付け替える。書いている途中で
    # 強制終了されても、中途半端なPDFが完成品の顔で残らない
    tmp_path = out_path.with_suffix(".pdf.part")
    tmp_path.write_bytes(out_bytes)
    os.replace(tmp_path, out_path)
    return {
        "ok": True,
        "name": name,
        "output": out_path.name,
        "pagesMasked": len(pages),
        "check": check,
    }


# ---------------------------------------------------------------- プリセット
def safe_preset_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|.\x00-\x1f]+', "_", (name or "").strip())
    return name[:60] or "preset"


def list_presets() -> list[dict]:
    PRESET_DIR.mkdir(exist_ok=True)
    presets = []
    for p in sorted(PRESET_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            data["name"] = p.stem
            presets.append(data)
        except Exception:
            continue
    return presets


def save_preset(name: str, rects_raw, color: str, page_range: str, parity: str) -> str:
    name = safe_preset_name(name)
    rects = clean_rects(rects_raw)
    if not rects:
        raise ValueError("保存する矩形がありません")
    preset = {
        "rects": rects,
        "color": color or "black",
        "range": page_range or "",
        "parity": parity or "all",
        "saved": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    PRESET_DIR.mkdir(exist_ok=True)
    path = PRESET_DIR / f"{name}.json"
    if path.exists():  # 上書き時は旧ファイルを trash/ に退避
        _to_trash(path)
    path.write_text(json.dumps(preset, ensure_ascii=False, indent=2), encoding="utf-8")
    return name


def delete_preset(name: str) -> None:
    path = PRESET_DIR / f"{safe_preset_name(name)}.json"
    if path.exists():  # 完全削除はせず trash/ へ退避
        _to_trash(path)


def _to_trash(path: Path) -> None:
    TRASH_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.move(str(path), str(TRASH_DIR / f"{path.name}.{ts}.bak"))


# ---------------------------------------------------------------- その他
def open_folder(path: Path) -> None:
    """OSごとのファイル管理アプリでフォルダを開く（macOS / Windows / Linux）。"""
    path.mkdir(exist_ok=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(path)])
