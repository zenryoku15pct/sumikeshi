#!/usr/bin/env python3
"""お試し用のダミーPDF（架空の氏名入り）を生成する

手書き書類のスキャンPDFを模した、画像ベースのダミーを samples/ に作る。
氏名はすべて架空。マスキングの練習・検査の動作確認（make_leak_samples.py）に使う。

使い方: python3 make_dummy_sample.py
出力  : samples/ダミー調査票.pdf（4ページ）
"""
import io
import sys
from pathlib import Path

import fitz

# Windowsで出力先の文字コードが日本語非対応（cp1252等）のとき、
# 日本語の印字でクラッシュしないよう「表せない文字は?に置換」に切り替える
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
OUT = BASE_DIR / "samples" / "ダミー調査票.pdf"

# 氏名欄の位置（正規化座標）。make_leak_samples.py の RECT と一致させてある
NAME_BOX = {"x0": 0.5418, "y0": 0.1080, "x1": 0.9273, "y1": 0.1825}

STUDENTS = ["架空 太郎", "架空 花子", "見本 次郎", "試験 三子"]
GOALS = [
    "英単語を毎日おぼえて、リスニングもがんばりたいです。",
    "宿題を早めに終わらせる習慣をつけたいです。",
    "部活と勉強を両立できるようにしたいです。",
    "苦手な数学の復習を週3回やります。",
]


def draw_page(doc, idx: int, overflow: bool = False) -> fitz.Page:
    """1ページぶんのダミー調査票をベクターで描く。

    overflow=True は「氏名が欄から右へはみ出した紙」。2026-08-21〜09-26のお試し用PDFは
    意図せずこの形で（13ptで欄より13pt長い）、欄どおりに塗ると「郎」が枠の外に残るのに
    「枠内クリア」が出た。いまは漏れ見本08（make_leak_samples.py）だけがこの形を使う。
    """
    page = doc.new_page(width=595, height=842)  # A4縦
    pr = page.rect
    black = (0, 0, 0)

    # 外枠・タイトル（黒帯は使わない——README「用紙設計の前提条件」のとおり）
    page.draw_rect(fitz.Rect(42, 42, 553, 797), color=black, width=1.2)
    page.insert_text((70, 90), "生活調査票（ダミー）", fontname="japan", fontsize=18)

    # 氏名欄（NAME_BOX の位置に枠を描く）
    nb = fitz.Rect(NAME_BOX["x0"] * pr.width, NAME_BOX["y0"] * pr.height,
                   NAME_BOX["x1"] * pr.width, NAME_BOX["y1"] * pr.height)
    page.draw_rect(nb, color=black, width=0.8)
    page.insert_text((nb.x0 + 8, nb.y0 + 16), "クラス・番号・氏名", fontname="japan", fontsize=8)
    # 11ptで欄の幅（約229pt）に収まる（文字列の幅 約198pt）。13ptだと234ptで欄と外枠を越える
    page.insert_text((nb.x0 + 8, nb.y0 + 40),
                     f"3 年 1 組　{idx + 1} 番　　{STUDENTS[idx % len(STUDENTS)]}",
                     fontname="japan", fontsize=13 if overflow else 11)

    # 記入欄
    page.insert_text((70, 230), "1．　今学期の目標について書いてください。",
                     fontname="japan", fontsize=11)
    page.insert_text((75, 268), GOALS[idx % len(GOALS)], fontname="japan", fontsize=11)
    for y in (275, 315, 355):
        page.draw_line(fitz.Point(70, y), fitz.Point(545, y), color=black, width=0.5)

    page.insert_text((70, 400), "2．　学校生活で心配なことがあれば書いてください。",
                     fontname="japan", fontsize=11)
    for y in (445, 485):
        page.draw_line(fitz.Point(70, y), fitz.Point(545, y), color=black, width=0.5)
    return page


def build_bytes(overflow: bool = False) -> bytes:
    """4ページのダミー調査票をPDFのバイト列で返す。"""
    vec = fitz.open()
    for i in range(4):
        draw_page(vec, i, overflow)

    # スキャンPDFを模すため、各ページを画像化して画像だけのページに置き換える
    out = fitz.open()
    for page in vec:
        pix = page.get_pixmap(matrix=fitz.Matrix(150 / 72, 150 / 72))
        p = out.new_page(width=page.rect.width, height=page.rect.height)
        p.insert_image(p.rect, stream=io.BytesIO(pix.tobytes("png")))
    vec.close()
    buf = io.BytesIO()
    out.save(buf, garbage=4, deflate=True)
    out.close()
    return buf.getvalue()


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(build_bytes())
    print(f"作成: {OUT}（4ページ・架空の氏名入り）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
