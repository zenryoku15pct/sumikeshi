#!/usr/bin/env python3
"""旧ブラウザ版（app.py）が出していた結果を「正解」として固定する。

v2.0.0 でマスキング中核を masking.py へ切り出したとき、旧版と一致することを
確かめた。その一致を後々まで保てるよう、旧版の出力から取った値を golden.json に
残す。以後 test_core.py がこれと突き合わせる。

旧版は backup/ へ退避したので、このスクリプトを再実行する必要は通常ない。
（退避先の app.py を import path に置けば再生成できる。）
"""
import hashlib
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# 突き合わせに使う条件。矩形の数・色・ページ指定・境界値・空欄の検出を1本ずつ見る
CASES = [
    ("氏名欄1つ・黒・全ページ",
     [{"x0": .05, "y0": .06, "x1": .55, "y1": .13}], "black", "", "all"),
    ("2つ・黒・全ページ",
     [{"x0": .05, "y0": .06, "x1": .55, "y1": .13},
      {"x0": .60, "y0": .06, "x1": .95, "y1": .13}], "black", "", "all"),
    ("1つ・白・全ページ",
     [{"x0": .05, "y0": .06, "x1": .55, "y1": .13}], "white", "", "all"),
    ("2つ・黒・ページ指定 1-2",
     [{"x0": .05, "y0": .06, "x1": .55, "y1": .13},
      {"x0": .60, "y0": .06, "x1": .95, "y1": .13}], "black", "1-2", "all"),
    ("1つ・黒・奇数ページのみ",
     [{"x0": .05, "y0": .06, "x1": .55, "y1": .13}], "black", "", "odd"),
    ("1つ・黒・偶数ページのみ",
     [{"x0": .05, "y0": .06, "x1": .55, "y1": .13}], "black", "", "even"),
    ("端に接する矩形（境界値）",
     [{"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": .08}], "black", "", "all"),
    ("何もない場所（位置ずれ検出）",
     [{"x0": .40, "y0": .80, "x1": .60, "y1": .88}], "black", "", "all"),
]

# PyMuPDF は保存のたびに新しい文書ID（PDFトレーラの /ID）を書く。中身の違いでは
# ないので、比較の前に伏せる。これを除いた全バイトが一致すれば同一とみなす。
_ID = re.compile(rb"/ID\s*\[\s*<[0-9A-Fa-f]*>\s*<[0-9A-Fa-f]*>\s*\]")


def digest(pdf_bytes: bytes) -> str:
    return hashlib.sha256(_ID.sub(b"/ID[<>x<>]", pdf_bytes)).hexdigest()


def collect(mod, sample: Path) -> list[dict]:
    import fitz
    data = sample.read_bytes()
    with fitz.open(stream=data, filetype="pdf") as d:
        n = d.page_count
    out = []
    for label, rects_raw, color, rng, parity in CASES:
        rects = mod.clean_rects(rects_raw)
        pages = mod.parse_page_spec(rng, parity, n)
        blank = mod.find_blank_rects(data, rects, pages)
        pdf = mod.apply_masks(data, rects, color, pages)
        check = mod.verify_masked(pdf, rects, pages)
        out.append({"label": label, "pages": pages, "blank": blank,
                    "verdict": check["verdict"], "size": len(pdf), "sha256": digest(pdf)})
    return out


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "app.py"
    sys.path.insert(0, str(src.parent))
    mod = __import__(src.stem)
    sample = BASE / "samples/ダミー調査票.pdf"
    golden = {
        "採取元": src.name,
        "素材": sample.name,
        # 素材PDFも保存のたびに文書IDだけが変わる（make_dummy_sample.py で作り直せる）。
        # IDを伏せた指紋なら作り直しても同じになるので、こちらを記録する
        "素材sha256": digest(sample.read_bytes()),
        "結果": collect(mod, sample),
    }
    dest = Path(__file__).resolve().parent / "golden.json"
    dest.write_text(json.dumps(golden, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{src.name} から {len(golden['結果'])} 件を採取して {dest.name} に固定しました")
