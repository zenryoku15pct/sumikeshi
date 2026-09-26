#!/usr/bin/env python3
"""検査スクリプトの動作確認用に「わざとマスクを漏らしたPDF」を作る

check_masked.py が漏れを本当に捕まえられるかを確かめるための負の対照サンプル。
samples/ダミー調査票.pdf（make_dummy_sample.py で生成）から7本を生成する。

  01 正常                  … スミケシ本体で処理（印つき）    → 枠内クリアになるべき
  02 黒四角を重ねただけ    … 見た目は黒塗り・画像は無傷      → [1]で落ちるべき
  03 テキスト層が残る      … OCR文字がマスク領域に残存        → [2]で落ちるべき
  04 注釈で黒塗り          … 注釈は剥がせる                   → [1][3]で落ちるべき
  05 一部ページだけ未処理  … 3ページ目の取りこぼし            → ページ整合で落ちるべき
  06 多層の1層に残存       … MRCの上層に氏名が残る            → [1]の全層走査で落ちるべき
  07 メタデータ残存        … 処理を通っていない疑い            → [0]で落ちるべき
  08 枠の外にはみ出し      … 欄どおりに塗ったが字が欄より長い  → [1.5]で要確認になるべき
                             （合格でも不合格でもない。枠の内側は消えている）

使い方: python3 make_leak_samples.py
出力先: samples/leak-check/
"""
import io
import sys
from pathlib import Path

import fitz

BASE_DIR = Path(__file__).resolve().parent
SRC = BASE_DIR / "samples" / "ダミー調査票.pdf"
OUT_DIR = BASE_DIR / "samples" / "leak-check"

# ダミー調査票の氏名欄の位置（make_dummy_sample.py の NAME_BOX と一致）
RECT = {"x0": 0.5418, "y0": 0.1080, "x1": 0.9273, "y1": 0.1825}


def page_rect(page, r=RECT) -> fitz.Rect:
    pr = page.rect
    return fitz.Rect(pr.x0 + r["x0"] * pr.width, pr.y0 + r["y0"] * pr.height,
                     pr.x0 + r["x1"] * pr.width, pr.y0 + r["y1"] * pr.height)


def redact(doc, pages=None, color=(0, 0, 0)):
    """app.py の apply_masks と同じ処理（画像ピクセルごと消去）。"""
    for pno in (pages if pages is not None else range(doc.page_count)):
        page = doc[pno]
        rect = page_rect(page)
        page.add_redact_annot(rect * page.derotation_matrix, fill=color)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)


def save(doc, name: str, clear_meta=True):
    if clear_meta:
        doc.set_metadata({})
        doc.del_xml_metadata()
    path = OUT_DIR / name
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    print(f"  作成: {path.name}")


def make_01_ok():
    # 正常品は本体と同じ処理で作る（処理済みの印つき）。自前の redact で作ると
    # 印が無く、新しい検査[0]が「未処理の疑い」を出すのが正しい動きになる
    import masking
    data = SRC.read_bytes()
    with fitz.open(stream=data, filetype="pdf") as d:
        pages = list(range(1, d.page_count + 1))
    out = masking.apply_masks(data, [RECT], "black", pages)
    (OUT_DIR / "01_正常_リダクション済み.pdf").write_bytes(out)
    print("  作成: 01_正常_リダクション済み.pdf")


def make_02_overlay_only():
    """黒い四角を上に重ねただけ。見た目は同じでも画像には氏名が残る。"""
    doc = fitz.open(SRC)
    for page in doc:
        page.draw_rect(page_rect(page), color=None, fill=(0, 0, 0))
    save(doc, "02_漏れ_黒四角を重ねただけ.pdf")


def make_03_text_layer():
    """正しくリダクションした上で、マスク領域に不可視のOCR文字を残す。"""
    doc = fitz.open(SRC)
    redact(doc)
    for page in doc:
        r = page_rect(page)
        # render_mode=3 は画面に出ない文字（スキャナのOCR層と同じ状態）
        page.insert_text((r.x0 + 4, r.y0 + 18), "架空 太郎",
                         fontname="japan", fontsize=12, render_mode=3)
    save(doc, "03_漏れ_テキスト層が残る.pdf")


def make_04_annot():
    """注釈（Square）で黒塗り。剥がせば中身が見える。"""
    doc = fitz.open(SRC)
    for page in doc:
        a = page.add_rect_annot(page_rect(page))
        a.set_colors(stroke=(0, 0, 0), fill=(0, 0, 0))
        a.set_border(width=0)
        a.update()
    save(doc, "04_漏れ_注釈で黒塗り.pdf")


def make_05_missed_page():
    """3ページ目だけ処理し忘れ。"""
    doc = fitz.open(SRC)
    redact(doc, pages=[p for p in range(doc.page_count) if p != 2])
    save(doc, "05_漏れ_3ページ目だけ未処理.pdf")


def make_06_multilayer():
    """MRC想定。背景層は消えているが、上に重なった層に氏名が残る。"""
    src = fitz.open(SRC)
    out = fitz.open()
    for spage in src:
        pr = spage.rect
        mr = page_rect(spage)
        # 背景層：氏名欄を白で潰した全面画像
        bg = spage.get_pixmap(matrix=fitz.Matrix(2, 2))
        sx, sy = bg.width / pr.width, bg.height / pr.height
        bg.set_rect(fitz.IRect(int(mr.x0 * sx), int(mr.y0 * sy),
                               int(mr.x1 * sx) + 1, int(mr.y1 * sy) + 1), (255, 255, 255))
        # 上層：氏名欄だけを切り出した画像（ここに氏名が残る）
        name = spage.get_pixmap(matrix=fitz.Matrix(2, 2), clip=mr)

        page = out.new_page(width=pr.width, height=pr.height)
        page.insert_image(pr, stream=io.BytesIO(bg.tobytes("png")))
        page.insert_image(mr, stream=io.BytesIO(name.tobytes("png")))
        page.draw_rect(mr, color=None, fill=(0, 0, 0))   # 見た目だけ黒塗り
    src.close()
    save(out, "06_漏れ_多層の1層に残存.pdf")


def make_07_metadata():
    """正しくリダクションしたが、メタデータを消していない。"""
    doc = fitz.open(SRC)
    redact(doc)
    doc.set_metadata({"title": "3年1組 生活調査票", "author": "架空 太郎",
                      "creator": "ExampleScanner PDF"})
    save(doc, "07_漏れ_メタデータ残存.pdf", clear_meta=False)


def make_08_overflow():
    """氏名が欄から右へはみ出した紙を、欄の枠どおりに本体で塗る。枠の内側は消えるが
    最後の1字が枠の外に残る。2026-08-21〜09-26のお試し用PDFは意図せずこの形で、
    「枠内クリア」が出るのを作者本人が実機テストで見つけた。同じ素通りを二度と
    起こさないよう、事故の形のまま見本に残す。[1.5]が要確認に落とすべき。"""
    import masking
    import make_dummy_sample
    data = make_dummy_sample.build_bytes(overflow=True)
    with fitz.open(stream=data, filetype="pdf") as d:
        pages = list(range(1, d.page_count + 1))
    out = masking.apply_masks(data, [RECT], "black", pages)
    (OUT_DIR / "08_はみ出し_枠の外に氏名の一部.pdf").write_bytes(out)
    print("  作成: 08_はみ出し_枠の外に氏名の一部.pdf")


def main():
    if not SRC.exists():
        print(f"元になるダミーPDFがありません: {SRC}", file=sys.stderr)
        print("先に python3 make_dummy_sample.py を実行してください", file=sys.stderr)
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"検査用サンプルを作成します（元: {SRC.name}）")
    for fn in (make_01_ok, make_02_overlay_only, make_03_text_layer, make_04_annot,
               make_05_missed_page, make_06_multilayer, make_07_metadata,
               make_08_overflow):
        fn()
    print(f"\n出力先: {OUT_DIR}")
    print("検査: python3 check_masked.py samples/leak-check/ --no-report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
