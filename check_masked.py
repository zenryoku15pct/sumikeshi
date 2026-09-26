#!/usr/bin/env python3
"""マスク済みPDFの全ページ自動検査

マスキング実行後のPDFを機械で全ページ検査し、個人情報が本当に消えているかを判定する。
目視確認の代わりに、外部（AIサービス等）へ渡す前の関門として使う。

検査項目
  [0] 処理痕跡   … メタデータ・XMPが消去されているか（ツールを通った証拠）
  [1] 画像レイヤー … 埋め込み画像すべてのマスク領域が塗りつぶされているか
                     （MRC圧縮は1ページに複数レイヤーがあるため、全レイヤーを抽出して
                       ピクセルが均一かを判定する。1層でも残っていれば不合格）
  [2] テキスト層 … マスク領域内の文字が0であること（ページ全体の文字数も報告）
  [3] 注釈       … 注釈オーバーレイが0件であること（注釈の黒塗りは剥がせるため）
  [4] レポート   … 上記をページごとの合否表として出力

マスク領域の決め方（3段構え）
  1. --preset / --rects で明示指定（最も確実）
  2. 指定がなければ自動検出（黒または白の均一な矩形ブロックを探す）
  3. どちらでも見つからなければ「マスク痕跡なし」として要確認にする

使い方
  python3 check_masked.py output/xxx_masked.pdf
  python3 check_masked.py output/*.pdf --preset 氏名欄
  python3 check_masked.py output/ --report output/検査レポート.md

終了コード: 0=全ファイル合格 / 1=不合格または要確認あり / 2=実行エラー

著作権表示（AGPL-3.0 第7条bに基づく保持要求つき）:
    スミケシ（sumikeshi）  Copyright (C) 2026 全力の15%
    zenryoku15@gmail.com  /  ライセンス: GNU AGPL-3.0（LICENSE 参照）
"""
import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF

# Windowsで出力先の文字コードが日本語非対応（cp1252等）のとき、
# 日本語の印字でクラッシュしないよう「表せない文字は?に置換」に切り替える
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

# 実行ファイル版（PyInstaller）では、実行ファイルの隣を基準にする
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = (Path(sys.executable) if FROZEN else Path(__file__)).resolve().parent
PRESET_DIR = BASE_DIR / "presets"
OUTPUT_DIR = BASE_DIR / "output"
# レポートは output/（人に渡すフォルダ）には書かない。レポートにはファイル名
# （氏名が入り得る）と元メタデータの値が平文で載るため、渡す物と分けて置く
REPORT_DIR = BASE_DIR / "検査レポート"


def _mark(sym: str, alt: str) -> str:
    """コンソールが絵文字を表示できない環境（Windowsの一部）では代替表記にする。"""
    try:
        sym.encode(sys.stdout.encoding or "utf-8")
        return sym
    except (UnicodeEncodeError, LookupError):
        return alt

# 判定
#   合格   … 指定した領域について、漏れの兆候が見つからなかった
#   要確認 … 安全と言い切れない（検査不能・マスク痕跡なし等）。合格ではない
#   不合格 … 漏れを検出した
# 要確認・不合格はどちらも終了コード1。「確認できない」を合格に数えない。
OK, NG, WARN = "合格", "不合格", "要確認"
RANK = {OK: 0, WARN: 1, NG: 2}

# 既定値
DEFAULT_TOLERANCE = 8      # 均一判定の許容幅（0〜255）。再圧縮のにじみを見込む
DEFAULT_INSET = 2          # 判定領域を内側へ縮める画素数。マスク境界のにじみを外す
DETECT_ZOOM = 1.0          # 自動検出時のレンダリング倍率（72dpi相当）
MIN_MASK_W_PT = 10.0       # マスクとみなす最小の幅(pt)
MIN_MASK_H_PT = 5.0        # マスクとみなす最小の高さ(pt)
MIN_REGION_PX = 4          # これ未満の交差画素数はレイヤー判定から除く

# [1.5] 枠の縁：枠のすぐ外側の帯を見て、縁を横切って外へ続く字を見つける
EDGE_BAND_PT = 12          # 見る帯の幅（pt）。1文字ぶんより少し広い
EDGE_ZOOM = 2.0            # 帯を描く倍率（72dpi×2）
EDGE_ROW_FRAC = 0.15       # 行（列）の幅のこれ以上が暗ければ「字の行」とみなす
                           # （0.25では白塗りで細い末尾の字を見逃した・2026-09-26実測）
EDGE_MIN_RUN_PT = 3.0      # 字の行がこれ以上続けば字とみなす（縁を横切る罫線は1〜2pt）
EDGE_DARK = 150            # これより暗い画素をインクとみなす（0〜255）


# ---------------------------------------------------------------- 矩形の入力
def load_preset_rects(name: str) -> list[dict]:
    path = PRESET_DIR / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"プリセットが見つかりません: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    rects = data.get("rects") or []
    if not rects:
        raise SystemExit(f"プリセットに矩形がありません: {path}")
    return [{k: float(r[k]) for k in ("x0", "y0", "x1", "y1")} for r in rects]


def parse_rects_arg(spec: str) -> list[dict]:
    rects = []
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        v = [float(x) for x in part.split(",")]
        if len(v) != 4:
            raise SystemExit(f"矩形の指定は x0,y0,x1,y1 の4値です: {part}")
        rects.append({"x0": min(v[0], v[2]), "y0": min(v[1], v[3]),
                      "x1": max(v[0], v[2]), "y1": max(v[1], v[3])})
    if not rects:
        raise SystemExit("矩形を解釈できませんでした")
    return rects


def norm_to_page(page, r: dict) -> fitz.Rect:
    """正規化座標(0〜1)を、そのページの表示座標へ変換する。"""
    pr = page.rect
    return fitz.Rect(pr.x0 + r["x0"] * pr.width, pr.y0 + r["y0"] * pr.height,
                     pr.x0 + r["x1"] * pr.width, pr.y0 + r["y1"] * pr.height)


# ---------------------------------------------------------------- 自動検出
def _runs_of_color(row: bytes, n: int, width: int, unit: bytes, min_run: int):
    """1行の中から、同一色 unit が min_run 画素以上続く区間を列挙する。"""
    pat = unit * min_run
    out, start = [], 0
    while True:
        i = row.find(pat, start)
        if i < 0:
            break
        if i % n:                       # 画素境界に乗っていない一致は捨てる
            start = i + 1
            continue
        x0 = i // n
        x1 = x0 + min_run
        while x1 < width and row[x1 * n:(x1 + 1) * n] == unit:
            x1 += 1
        while x0 > 0 and row[(x0 - 1) * n:x0 * n] == unit:
            x0 -= 1
        out.append((x0, x1))
        start = x1 * n
    return out


def _find_blocks(pix, unit: bytes, min_w: int, min_h: int):
    """単色 unit の矩形ブロックを検出する。"""
    W, H, n, st, s = pix.width, pix.height, pix.n, pix.stride, pix.samples
    blocks, active = [], []
    for y in range(H):
        row = s[y * st:y * st + W * n]
        segs = _runs_of_color(row, n, W, unit, min_w) if row.find(unit * min_w) >= 0 else []
        new = []
        for (x0, x1) in segs:
            hit = None
            for a in active:
                if a["y1"] == y and abs(a["x0"] - x0) <= 1 and abs(a["x1"] - x1) <= 1:
                    hit = a
                    break
            if hit:
                hit["y1"] = y + 1
                new.append(hit)
            else:
                new.append({"x0": x0, "x1": x1, "y0": y, "y1": y + 1})
        for a in active:
            if a not in new and a["y1"] - a["y0"] >= min_h:
                blocks.append(a)
        active = new
    blocks += [a for a in active if a["y1"] - a["y0"] >= min_h]
    return blocks


def _is_enclosed(pix, b, margin=3) -> bool:
    """ブロックの外周に別の内容があるか（白マスクを紙の余白と区別するため）。"""
    W, H, n, st, s = pix.width, pix.height, pix.n, pix.stride, pix.samples
    sides = 0
    for (yr, xr) in (
        (range(max(0, b["y0"] - margin), b["y0"]), range(b["x0"], b["x1"])),
        (range(b["y1"], min(H, b["y1"] + margin)), range(b["x0"], b["x1"])),
        (range(b["y0"], b["y1"]), range(max(0, b["x0"] - margin), b["x0"])),
        (range(b["y0"], b["y1"]), range(b["x1"], min(W, b["x1"] + margin))),
    ):
        found = False
        for y in yr:
            row = s[y * st:y * st + W * n]
            for x in xr:
                if min(row[x * n:(x + 1) * n]) < 200:
                    found = True
                    break
            if found:
                break
        sides += 1 if found else 0
    return sides >= 3


def detect_mask_rects(page, detect_white=False) -> list[dict]:
    """ページ画像から、均一な矩形ブロックをマスクとして検出する。戻り値は正規化座標(0〜1)。

    既定は黒マスクのみ。白マスクは紙の余白・記入欄の空白と原理的に区別できず、
    誤検出だらけになるため自動検出しない（白で塗った場合は --preset / --rects で
    領域を渡すか、--detect-white を明示する）。"""
    pix = page.get_pixmap(matrix=fitz.Matrix(DETECT_ZOOM, DETECT_ZOOM))
    W, H = pix.width, pix.height
    if not W or not H:
        return []
    sx, sy = W / page.rect.width, H / page.rect.height
    min_w = max(3, int(MIN_MASK_W_PT * sx))
    min_h = max(3, int(MIN_MASK_H_PT * sy))
    n = pix.n
    targets = [(b"\x00" * n, False)]
    if detect_white:
        targets.append((b"\xff" * n, True))
    found = []
    for unit, enclosed_only in targets:
        for b in _find_blocks(pix, unit, min_w, min_h):
            if enclosed_only and not _is_enclosed(pix, b):
                continue
            found.append({"x0": b["x0"] / W, "y0": b["y0"] / H,
                          "x1": b["x1"] / W, "y1": b["y1"] / H})
    # 入れ子・重複を除く（大きい方を残す）
    found.sort(key=lambda r: -((r["x1"] - r["x0"]) * (r["y1"] - r["y0"])))
    kept = []
    for r in found:
        if any(r["x0"] >= k["x0"] - 1e-6 and r["y0"] >= k["y0"] - 1e-6
               and r["x1"] <= k["x1"] + 1e-6 and r["y1"] <= k["y1"] + 1e-6 for k in kept):
            continue
        kept.append(r)
    return kept


# ---------------------------------------------------------------- 画素の統計
def region_stats(pix, c0: int, r0: int, c1: int, r1: int):
    """画像の指定領域について (チャンネル別最小値, 最大値, 画素数) を返す。"""
    n, st, s = pix.n, pix.stride, pix.samples
    c0, r0 = max(0, c0), max(0, r0)
    c1, r1 = min(pix.width, c1), min(pix.height, r1)
    if c1 <= c0 or r1 <= r0:
        return None
    buf = bytearray()
    for row in range(r0, r1):
        base = row * st
        buf += s[base + c0 * n:base + c1 * n]
    lo = [min(buf[ch::n]) for ch in range(n)]
    hi = [max(buf[ch::n]) for ch in range(n)]
    return lo, hi, (c1 - c0) * (r1 - r0)


def mask_to_image_pixels(mask_unrot: fitz.Rect, info: dict, inset: int = 0):
    """回転前ページ座標のマスク矩形を、その画像のピクセル座標へ写す。

    inset は判定領域を内側へ縮める画素数。マスクの境界には再圧縮のにじみ
    （JPEGのリンギング等）が出るため、そこを判定から外すために使う。"""
    W, H = info["width"], info["height"]
    try:
        inv = ~fitz.Matrix(info["transform"])
        us = [fitz.Point(x, y) * inv
              for (x, y) in ((mask_unrot.x0, mask_unrot.y0), (mask_unrot.x1, mask_unrot.y0),
                             (mask_unrot.x0, mask_unrot.y1), (mask_unrot.x1, mask_unrot.y1))]
        u0, u1 = min(p.x for p in us), max(p.x for p in us)
        v0, v1 = min(p.y for p in us), max(p.y for p in us)
    except Exception:                                    # 退化行列は bbox で近似
        bb = fitz.Rect(info["bbox"])
        if bb.is_empty:
            return None
        u0, u1 = (mask_unrot.x0 - bb.x0) / bb.width, (mask_unrot.x1 - bb.x0) / bb.width
        v0, v1 = (mask_unrot.y0 - bb.y0) / bb.height, (mask_unrot.y1 - bb.y0) / bb.height
    c0, c1 = int(u0 * W) + inset, int(u1 * W) + 1 - inset
    r0, r1 = int(v0 * H) + inset, int(v1 * H) + 1 - inset
    if c1 <= c0 or r1 <= r0:                             # 縮めすぎたら内側1画素だけ見る
        c0, c1 = int((u0 + u1) / 2 * W), int((u0 + u1) / 2 * W) + 1
        r0, r1 = int((v0 + v1) / 2 * H), int((v0 + v1) / 2 * H) + 1
    return c0, r0, c1, r1


# ---------------------------------------------------------------- 枠の縁
def edge_overflow(page, disp_rects) -> list[str]:
    """[1.5] 枠の縁を横切って外へ続く字を探し、見つかった辺の名前を返す。

    枠の四辺それぞれのすぐ外側の帯（幅 EDGE_BAND_PT）を描画し、帯の幅の
    EDGE_ROW_FRAC 以上が暗い行が EDGE_MIN_RUN_PT 以上続く塊を「縁を横切る字」と
    みなす。縁に沿って走る罫線（欄の枠線・用紙の外枠）は各行に1〜2画素しか載らず、
    縁を横切る罫線（下線・表の横線）は1〜2行しか続かないので、どちらも見つけない。

    合格を出す仕組みではなく、要確認にするための網。氏名が欄より長く書かれて
    いても、枠の内側だけを見る[1]〜[3]は「枠内クリア」を出す。2026-09-26、お試し用
    PDFの氏名が欄から13ptはみ出したまま1か月気づかれず、作者本人が実機テストで
    「OKが出た」と読んだ。マスク枠から離れた別欄の氏名は、この網でも見えない。
    """
    found = []
    pr = page.rect
    rects = [fitz.Rect(r) for r in disp_rects]
    B, Z = EDGE_BAND_PT, EDGE_ZOOM
    for i, r in enumerate(rects):
        # 帯は辺に沿った向きに両端へ B だけ延ばして描く（延長部は「縁に沿う線」の判定
        # だけに使う）。罫線は枠の外まで続くが、字の画は枠の高さで終わる。延ばさないと、
        # 枠を字にぴったり合わせたとき、はみ出した字の縦画が縁に沿う線と区別できず
        # 丸ごと除外されていた（2026-09-26 Codex校閲2回目・指摘2）
        bands = (("右", fitz.Rect(r.x1, r.y0 - B, r.x1 + B, r.y1 + B), "y", False),
                 ("左", fitz.Rect(r.x0 - B, r.y0 - B, r.x0, r.y1 + B), "y", True),
                 ("上", fitz.Rect(r.x0 - B, r.y0 - B, r.x1 + B, r.y0), "x", True),
                 ("下", fitz.Rect(r.x0 - B, r.y1, r.x1 + B, r.y1 + B), "x", False))
        for name, clip, along, edge_at_end in bands:
            clip = clip & pr                      # 用紙の外へ出た帯は切り落とす
            if clip.is_empty:
                continue
            pix = page.get_pixmap(matrix=fitz.Matrix(Z, Z), clip=clip,
                                  colorspace=fitz.csGRAY, alpha=False)
            w, h, st, s = pix.width, pix.height, pix.stride, pix.samples
            # 縦の辺は行ごと・横の辺は列ごとに見る。以下「行」と書くのは走る向きの1本、
            # 「位置」は縁から遠ざかる向きの添字（edge_at_end なら末尾が縁側）
            n_lines, n_across = (h, w) if along == "y" else (w, h)
            if n_across == 0 or n_lines == 0:
                continue

            def dark(line, k):
                y, x = (line, k) if along == "y" else (k, line)
                return s[y * st + x] < EDGE_DARK

            # 数えない画素：隣り合う別の枠の内側（黒塗りが帯に入り、字と区別できない。
            # クラス欄と氏名欄を並べて塗ると互いの縁が警告が出た・Codex校閲1回目 2-2）。
            # 始点は切り捨て・終点は切り上げで、黒塗りの画素を数え残さない側に丸める
            skip = [bytearray(n_across) for _ in range(n_lines)]
            for j, o in enumerate(rects):
                if j == i:
                    continue
                ov = o & clip
                if ov.is_empty:
                    continue
                ox0 = max(0, int((ov.x0 - clip.x0) * Z))
                oy0 = max(0, int((ov.y0 - clip.y0) * Z))
                ox1 = min(w, int(math.ceil((ov.x1 - clip.x0) * Z)))
                oy1 = min(h, int(math.ceil((ov.y1 - clip.y0) * Z)))
                for y in range(oy0, oy1):
                    for x in range(ox0, ox1):
                        line, k = (y, x) if along == "y" else (x, y)
                        skip[line][k] = 1
            # 縁に沿って走る線（欄の枠線・用紙の外枠・黒塗りの縁のにじみ）は、延長部を
            # 含む帯の同じ位置がほぼ全行で暗い。太さや本数に関わらず、その位置ごと数えない
            along_line = set()
            for k in range(n_across):
                counted = [line for line in range(n_lines) if not skip[line][k]]
                if counted and sum(1 for line in counted if dark(line, k)) >= 0.9 * len(counted):
                    along_line.add(k)
            # 縁に近い側の画素（帯の手前2pt）。塊が縁のすぐ外側にあるかをここで見る。
            # 左・上の帯は末尾が縁側（Codex校閲2回目・指摘1：右辺と同じ先頭を見ていた）
            near_idx = range(min(n_across, int(2 * Z)))
            if edge_at_end:
                near_idx = [n_across - 1 - k for k in near_idx]
            near = [k for k in near_idx if k not in along_line]
            # 走る向きの範囲は枠の辺の長さぶんだけ（延長部は判定に入れない）
            lo = (r.y0 if along == "y" else r.x0) - (clip.y0 if along == "y" else clip.x0)
            hi = (r.y1 if along == "y" else r.x1) - (clip.y0 if along == "y" else clip.x0)
            l0, l1 = max(0, int(lo * Z)), min(n_lines, int(math.ceil(hi * Z)))
            run = 0
            for line in range(l0, l1 + 1):
                if line < l1:
                    cols = [k for k in range(n_across)
                            if not skip[line][k] and k not in along_line]
                    v = (sum(1 for k in cols if dark(line, k)) / len(cols)
                         if cols and (n_across < 4 or len(cols) >= 4) else 0.0)
                else:
                    v = 0.0
                if v >= EDGE_ROW_FRAC:
                    run += 1
                    continue
                if run / Z >= EDGE_MIN_RUN_PT:
                    # 塊が縁のすぐ外側に無ければ字のはみ出しではない（枠のすぐ下の見出し・
                    # 隣欄の印字・ハンコ。縁から離れた黒い塊で警告が出た・Codex校閲1回目 2-1）
                    touching = any(dark(l, k) for l in range(line - run, line)
                                   for k in near if not skip[l][k])
                    if touching:
                        found.append(f"枠{i + 1}の{name}")
                        break
                run = 0
    return found


# ---------------------------------------------------------------- ページ検査
def check_page(doc, page, rects_norm, opt):
    """1ページを検査して結果の辞書を返す。"""
    tol, allow_text_outside = opt["tolerance"], opt["allow_text_outside"]
    res = {"verdict": OK, "reasons": [], "layers": [], "detected": False}

    # マスク矩形の決定
    if rects_norm:
        rects = rects_norm
    else:
        rects = detect_mask_rects(page, opt["detect_white"])
        res["detected"] = True
    res["rects"] = rects
    res["n_rects"] = len(rects)

    disp = [norm_to_page(page, r) for r in rects]
    unrot = [fitz.Rect(d) * page.derotation_matrix for d in disp]

    # ---- [3] 注釈
    # page.annots() は Link・Popup・Widget（フォーム入力欄）と独自の種類を列挙しない
    # （2026-08-22に実測。独自サブタイプの注釈が氏名を抱えたまま「注釈0件」で合格した）。
    # 数はPDFの生の /Annots 配列から取り、種類名は列挙できた分だけ添える
    n_annots = 0
    val = doc.xref_get_key(page.xref, "Annots")
    if val[0] == "array":
        n_annots = val[1].count("R")          # "12 0 R 15 0 R" の参照数
        if n_annots == 0 and val[1].strip("[] \n"):
            n_annots = 1                       # 直接埋め込まれた辞書など
    elif val[0] not in ("null", ""):
        n_annots = 1
    res["n_annots"] = n_annots
    if n_annots:
        kinds = sorted({a.type[1] for a in (page.annots() or [])})
        detail = f"（{', '.join(kinds)}）" if kinds else "（列挙できない種類を含む）"
        res["verdict"] = NG
        res["reasons"].append(f"注釈が{n_annots}件残っている{detail}")

    # ---- [2] テキスト層
    page_text = page.get_text("text").strip()
    res["n_chars"] = len(page_text)
    in_mask = 0
    if page_text and unrot:
        # get_text("words") は /Rotate に関わらず非回転座標を返す（2026-08-22に実測で
        # 確定）。表示座標(disp)と比べると回転ページで見逃しと誤警報の両方が起きる
        for w in page.get_text("words"):
            wr = fitz.Rect(w[:4])
            if any(not (wr & u).is_empty for u in unrot):
                in_mask += len(w[4])
    res["n_chars_in_mask"] = in_mask
    if in_mask:
        res["verdict"] = NG
        res["reasons"].append(f"マスク領域内にテキストが{in_mask}文字残っている")

    # ---- [2.5] ベクター描画（線で書かれた筆跡）
    # タブレット記入・描画ツールの筆跡は線の座標として残り、黒く塗っても
    # 画像の検査[1]には映らない（2026-08-22に実測。枠から20pt離れるまで復元できた）。
    # マスク領域に掛かる描画のうち、「塗りとして置かれた矩形」（fillのみ・領域を
    # ほぼ覆う）以外が残っていれば不合格にする
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = None
        if res["verdict"] == OK:
            res["verdict"] = WARN
            res["reasons"].append("線画を調べられなかった（確認できない＝合格にしない）")
    if drawings:
        n_ink = 0
        for d in drawings:
            drect = fitz.Rect(d["rect"])
            hit = [u for u in unrot if not (drect & u).is_empty]
            if not hit:
                continue
            is_mask_fill = (
                d.get("fill") is not None
                and all(it[0] == "re" for it in d.get("items", []))
                and any(abs((drect & u).get_area() - u.get_area()) < u.get_area() * 0.2
                        for u in hit))
            if not is_mask_fill:
                n_ink += 1
        if n_ink:
            res["verdict"] = NG
            res["reasons"].append(
                f"マスク領域に線画（ベクターの筆跡・図形）が{n_ink}本掛かっている"
                "（黒塗りの下に座標が残っている疑い）")
    elif res["n_chars"] and not allow_text_outside:
        if RANK[WARN] > RANK[res["verdict"]]:
            res["verdict"] = WARN
        res["reasons"].append(f"ページにテキスト層が{res['n_chars']}文字ある（領域外）")

    # ---- マスクが1つも無いページ
    if not rects:
        if RANK[WARN] > RANK[res["verdict"]]:
            res["verdict"] = WARN
        res["reasons"].append(
            "マスクの痕跡が見つからない"
            + ("（白で塗った場合は --preset か --rects で領域を指定）" if res["detected"] else ""))

        res["n_layers"] = len(page.get_image_info())
        return res

    # ---- [1.5] 枠の縁（縁を横切って外へ続く字）
    try:
        edges = edge_overflow(page, disp)
    except Exception as e:
        edges = []
        if RANK[WARN] > RANK[res["verdict"]]:
            res["verdict"] = WARN
        res["reasons"].append(f"枠の縁を調べられなかった（確認できない＝合格にしない）: {e}")
    res["edges"] = edges
    if edges:
        if RANK[WARN] > RANK[res["verdict"]]:
            res["verdict"] = WARN
        res["reasons"].append(
            f"枠の縁のすぐ外側（2pt以内）に暗い部分がある（{'・'.join(edges)}）"
            "——字が枠より長い疑い。枠を広げて塗り直してください")

    # ---- [1] 画像レイヤー
    infos = page.get_image_info(xrefs=True)
    res["n_layers"] = len(infos)
    cache = {}
    for idx, info in enumerate(infos):
        bb = fitz.Rect(info["bbox"])
        touching = [u for u in unrot if not (fitz.Rect(u) & bb).is_empty]
        if not touching:
            continue
        xref = info.get("xref", 0)
        try:
            if xref not in cache:
                cache[xref] = fitz.Pixmap(doc, xref) if xref else None
            pix = cache[xref]
            if pix is None:
                raise ValueError("xrefなし")
        except Exception as e:
            res["verdict"] = NG
            res["reasons"].append(f"レイヤー{idx + 1}(xref={xref})を展開できず検査不能: {e}")
            res["layers"].append({"idx": idx + 1, "xref": xref, "ok": None, "note": str(e)})
            continue
        for u in touching:
            box = mask_to_image_pixels(fitz.Rect(u), info, opt["inset"])
            if box is None:
                continue
            st = region_stats(pix, *box)
            if st is None:
                continue
            lo, hi, npx = st
            if npx < MIN_REGION_PX:
                # 解像度の低い層（MRCの背景層など）ではマスク枠が数画素に潰れて
                # 判定できない。以前は記録も残さず外していたが、「確認できない」を
                # 合格に数えない原則（本ファイル冒頭）に合わせて要確認にする
                res["layers"].append({"idx": idx + 1, "xref": xref, "ok": None,
                                      "px": npx, "spread": None, "lo": None, "hi": None})
                if res["verdict"] == OK:
                    res["verdict"] = WARN
                    res["reasons"].append(
                        f"レイヤー{idx + 1}(xref={xref})は解像度が低く判定できない（{npx}画素）")
                continue
            spread = max(hi[c] - lo[c] for c in range(len(lo)))
            ok = spread <= tol
            res["layers"].append({"idx": idx + 1, "xref": xref, "ok": ok,
                                  "px": npx, "spread": spread, "lo": lo, "hi": hi})
            if not ok:
                res["verdict"] = NG
                res["reasons"].append(
                    f"レイヤー{idx + 1}(xref={xref}) のマスク領域が均一でない"
                    f"（濃淡の幅{spread} > 許容{tol}・{npx}画素）")
    cache.clear()
    if not res["layers"]:
        if RANK[WARN] > RANK[res["verdict"]]:
            res["verdict"] = WARN
        res["reasons"].append("マスク領域に重なる画像レイヤーが無い")

    # 自動検出は「自分で見つけた黒を自分で確かめる」形なので、素の判定では必ず
    # 合格が出る（見出しに黒帯のある未処理スキャンが全ページ✅になるのを実測・
    # 2026-08-22）。自動検出で判定したページは、良くても「要確認」までとする。
    # ※この格下げは全項目の検査が終わった後に行うこと。途中に置くと後続の
    #   検査を飛ばす（[1]を飛ばして黒四角重ねを見逃しかけた・2026-08-23）
    if res["detected"] and res["verdict"] == OK:
        res["verdict"] = WARN
        res["reasons"].append(
            "マスク領域を自動検出で判定（合格の保証にはならない。"
            "枠を指定して検査し直してください）")
    return res


# ---------------------------------------------------------------- ファイル検査
def check_file(path: Path, rects_norm, opt, pages_spec, quiet):
    out = {"path": path, "verdict": OK, "pages": [], "file_reasons": []}
    try:
        doc = fitz.open(path)
    except Exception as e:
        out["verdict"] = NG
        out["file_reasons"].append(f"PDFを開けない: {e}")
        return out

    try:
        # ---- [0] 処理痕跡（メタデータ・XMP・処理済みの印）
        md = doc.metadata or {}
        left = {k: v for k, v in md.items()
                if k in ("title", "author", "subject", "keywords", "creator", "producer") and v}
        # スミケシが出力に残す処理済みの印（producer・個人情報を含まない）は
        # 「残存メタデータ」に数えない。むしろ無いことが未処理の合図になる
        # （以前はメタデータが空の未処理PDF——束の分割・抽出で普通にできる——が
        # ここを素通りしていた。2026-08-22の総ざらい）
        mark = left.pop("producer", "") if str(left.get("producer", "")).startswith("sumikeshi ") else ""
        xmp = doc.xref_xml_metadata()
        has_xmp = bool(xmp) if not isinstance(xmp, int) else xmp > 0
        out["meta_left"] = left
        out["has_xmp"] = has_xmp
        out["tool_mark"] = mark
        if not left and not has_xmp and not mark:
            if RANK[out["verdict"]] < RANK[WARN]:
                out["verdict"] = WARN
            out["file_reasons"].append(
                "処理済みの印が無い（このツールを通っていない疑い。"
                "v2.0.0より前の出力にも印はありません）")
        if left or has_xmp:
            out["verdict"] = NG
            detail = "・".join(f"{k}={v[:40]!r}" for k, v in left.items()) or ""
            out["file_reasons"].append(
                "メタデータが消去されていない＝マスキング処理を通っていない疑い"
                + (f"（{detail}{'・XMPあり' if has_xmp else ''}）" if detail or has_xmp else ""))

        total = doc.page_count
        targets = [p for p in (pages_spec or range(1, total + 1)) if 1 <= p <= total]
        for i, pno in enumerate(targets, 1):
            try:
                r = check_page(doc, doc[pno - 1], rects_norm, opt)
            except Exception as e:
                # 壊れたページでバッチ全体を止めない。検査できない＝合格ではない
                r = {"verdict": WARN, "reasons": [f"検査不能（{e}）"], "layers": [],
                     "detected": False, "rects": [], "n_rects": 0, "n_annots": 0,
                     "n_chars": 0, "n_chars_in_mask": 0, "n_layers": 0}
            r["page"] = pno
            out["pages"].append(r)
            if RANK[r["verdict"]] > RANK[out["verdict"]]:
                out["verdict"] = r["verdict"]
            if not quiet and (i % 20 == 0 or i == len(targets)):
                print(f"  … {i}/{len(targets)}ページ", file=sys.stderr)
        out["n_pages"] = total

        # ページ間の整合：大半のページにマスクがあるのに無いページは取りこぼしとみなす
        # （ページ範囲を絞って正当にマスクした場合もここに出る。--pages で範囲を渡せば消える）
        # 逆にマスクが見つかったページがごく少数なら、それ自体が誤検出の疑い。
        # 1件の誤検出で残り全ページに「取りこぼし」の札を付けないよう、割合で線を引く。
        if out["pages"]:
            n_masked = sum(1 for p in out["pages"] if p["n_rects"])
            ratio = n_masked / len(out["pages"])
            if n_masked and ratio >= 0.2:
                for p in out["pages"]:
                    if not p["n_rects"]:
                        p["verdict"] = NG
                        p["reasons"].append(
                            "他のページにはマスクがあるのに、このページには無い（取りこぼし）")
            elif n_masked:
                out["file_reasons"].append(
                    f"マスクを検出できたのは{len(out['pages'])}ページ中{n_masked}ページだけ"
                    "（取りこぼしか、自動検出できない塗り色＝白の可能性。"
                    "--preset / --rects で領域を指定して再検査を）")
        out["verdict"] = max([out["verdict"]] + [p["verdict"] for p in out["pages"]],
                             key=lambda v: RANK[v])
    finally:
        doc.close()
    return out


# ---------------------------------------------------------------- レポート
def build_report(results, tol, rects_source) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    L = [f"# マスク済みPDF 自動検査レポート", "",
         f"- 検査日時：{now}", f"- マスク領域：{rects_source}",
         f"- 均一判定の許容幅：{tol}（0〜255。再圧縮のにじみを見込んだ値）", "",
         "> **このレポートが保証するのは、指定したマスク枠の内側だけです。**",
         "> 用紙全体の安全証明ではなく、外部に出してよいかの判断にも、事故調査の",
         "> 根拠にも使えません。枠の縁を横切る字は⚠で知らせますが、枠から離れた場所",
         "> （別欄の氏名・保護者署名・緊急連絡先・自由記述の中の名前）と**ファイル名**は",
         "> 検査していません。",
         "> 押さえるべき欄がすべて枠に入っているかは、用紙の現物を見て人が判断してください。", ""]

    L += ["## 総括", "", "|ファイル|ページ数|判定|不合格|要確認|", "|-|-|-|-|-|"]
    for r in results:
        ng = sum(1 for p in r["pages"] if p["verdict"] == NG)
        wn = sum(1 for p in r["pages"] if p["verdict"] == WARN)
        mark = {OK: "✅ 合格", NG: "❌ 不合格", WARN: "⚠️ 要確認"}[r["verdict"]]
        L.append(f"|{r['path'].name}|{r.get('n_pages', '-')}|{mark}|{ng}|{wn}|")
    L.append("")

    for r in results:
        L += [f"## {r['path'].name}", ""]
        mark = {OK: "✅ 合格", NG: "❌ 不合格", WARN: "⚠️ 要確認"}[r["verdict"]]
        L.append(f"**総合判定：{mark}**")
        L.append("")
        if r["file_reasons"]:
            L.append("ファイル全体の指摘：")
            L += [f"- {x}" for x in r["file_reasons"]] + [""]
        if not r["pages"]:
            L.append("（ページ検査なし）")
            L.append("")
            continue

        L += ["|ページ|判定|画像層|検査した層|マスク数|文字(領域内/全体)|注釈|指摘|",
              "|-|-|-|-|-|-|-|-|"]
        for p in r["pages"]:
            m = {OK: "✅", NG: "❌", WARN: "⚠️"}[p["verdict"]]
            checked = len(p["layers"])
            bad = sum(1 for x in p["layers"] if x["ok"] is False)
            layer_txt = f"{checked}層" + (f"（不合格{bad}）" if bad else "")
            note = "；".join(p["reasons"]) if p["reasons"] else ""
            L.append(f"|{p['page']}|{m}|{p.get('n_layers', 0)}|{layer_txt}|{p['n_rects']}|"
                     f"{p['n_chars_in_mask']}/{p['n_chars']}|{p['n_annots']}|{note}|")
        L.append("")

        bad_pages = [p for p in r["pages"] if p["verdict"] == NG]
        if bad_pages:
            L += ["### 不合格ページの詳細", ""]
            for p in bad_pages[:40]:
                L.append(f"- **{p['page']}ページ**")
                for x in p["layers"]:
                    if x["ok"] is False:
                        L.append(f"    - レイヤー{x['idx']}(xref={x['xref']}) "
                                 f"{x['px']}画素 濃淡の幅={x['spread']} min={x['lo']} max={x['hi']}")
                for reason in p["reasons"]:
                    L.append(f"    - {reason}")
            if len(bad_pages) > 40:
                L.append(f"- （ほか{len(bad_pages) - 40}ページ）")
            L.append("")
    L += ["---", "",
          "> 再掲：このレポートは指定したマスク枠の内側だけの判定です。用紙全体の",
          "> 安全証明ではありません。被害範囲の認定・提出可否の判断には、必ず現物との",
          "> 照合を併せてください。", "",
          f"（sumikeshi 検査レポート）"]
    return "\n".join(L)


# ---------------------------------------------------------------- main
def parse_pages(spec: str):
    if not spec:
        return None
    pages = set()
    for part in spec.replace("、", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            # 「2-300000000」のような指定で数億要素の集合を作らない。
            # 実ページ数は check_file 側で絞られるので結果は変わらない
            pages.update(range(max(a, 1), min(b, 99999) + 1))
        else:
            pages.add(int(part))
    return sorted(pages)


def main():
    ap = argparse.ArgumentParser(
        description="マスク済みPDFを全ページ自動検査する",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="PDFファイルまたはフォルダ")
    ap.add_argument("--preset", help="presets/ のプリセット名で矩形を指定")
    ap.add_argument("--rects", help='正規化座標 "x0,y0,x1,y1;..." で矩形を指定')
    ap.add_argument("--tolerance", type=int, default=DEFAULT_TOLERANCE,
                    help=f"均一判定の許容幅（既定 {DEFAULT_TOLERANCE}）")
    ap.add_argument("--inset", type=int, default=DEFAULT_INSET,
                    help=f"判定領域を内側へ縮める画素数（既定 {DEFAULT_INSET}。境界のにじみ対策）")
    ap.add_argument("--detect-white", action="store_true",
                    help="白マスクも自動検出する（紙の余白を見つけやすいので通常は使わない）")
    ap.add_argument("--pages", help="検査するページ（例 1-10,15）。既定は全ページ")
    ap.add_argument("--report", help="レポートの出力先（既定 output/検査レポート_日時.md）")
    ap.add_argument("--no-report", action="store_true", help="レポートを書き出さない")
    ap.add_argument("--allow-text-outside", action="store_true",
                    help="マスク領域外のテキスト層を要確認にしない")
    ap.add_argument("--quiet", action="store_true", help="進捗を出さない")
    args = ap.parse_args()

    if args.preset and args.rects:
        raise SystemExit("--preset と --rects は同時に指定できません")
    rects_norm, source = None, "自動検出"
    if args.preset:
        rects_norm = load_preset_rects(args.preset)
        source = f"プリセット「{args.preset}」（{len(rects_norm)}個）"
    elif args.rects:
        rects_norm = parse_rects_arg(args.rects)
        source = f"コマンドライン指定（{len(rects_norm)}個）"

    files = []
    for p in args.paths:
        path = Path(p)
        if path.is_dir():
            # glob("*.pdf") は .PDF（複合機の既定名に多い）を落とし、そのファイルは
            # 「検査できなかった」ではなく最初から存在しない扱いになる（2026-08-22実測）。
            # 読み込み側（suffix.lower()）と同じ判定にそろえる
            files += sorted(q for q in path.iterdir()
                            if q.is_file() and q.suffix.lower() == ".pdf")
        elif path.exists():
            files.append(path)
        else:
            print(f"見つかりません: {p}", file=sys.stderr)
    if not files:
        raise SystemExit("検査するPDFがありません")

    pages_spec = parse_pages(args.pages)
    opt = {"tolerance": args.tolerance, "inset": args.inset,
           "detect_white": args.detect_white,
           "allow_text_outside": args.allow_text_outside}
    results = []
    for f in files:
        if not args.quiet:
            print(f"検査中: {f.name}", file=sys.stderr)
        try:
            results.append(check_file(f, rects_norm, opt, pages_spec, args.quiet))
        except Exception as e:
            # 1本の異常PDFで他ファイルの結果まで失わない
            results.append({"path": f, "verdict": WARN, "pages": [],
                            "file_reasons": [f"検査不能（{e}）——このファイルは安全と確認できていない"]})

    # 画面向けの要約
    print()
    print("=" * 64)
    print(f"検査結果（許容幅={args.tolerance} / マスク領域={source}）")
    print("※「合格」＝指定領域の内側のみの判定。枠の縁を横切る字は⚠で知らせるが、")
    print("  枠から離れた別欄の氏名（保護者署名等）は見ていない。欄の網羅は人が現物で確認する")
    print("=" * 64)
    worst = OK
    console_marks = {OK: _mark("✅", "[OK]") + " 合格",
                     NG: _mark("❌", "[NG]") + " 不合格",
                     WARN: _mark("⚠️", "[!]") + " 要確認"}
    for r in results:
        ng = sum(1 for p in r["pages"] if p["verdict"] == NG)
        wn = sum(1 for p in r["pages"] if p["verdict"] == WARN)
        mark = console_marks[r["verdict"]]
        print(f"{mark}  {r['path'].name}  "
              f"（{r.get('n_pages', '?')}ページ中 不合格{ng} / 要確認{wn}）")
        for x in r["file_reasons"]:
            print(f"       ! {x}")
        seen = set()
        for p in r["pages"]:
            if p["verdict"] != OK:
                for reason in p["reasons"]:
                    key = reason.split("（")[0]
                    if key not in seen:
                        seen.add(key)
                        print(f"       - {p['page']}ページ: {reason}")
        if RANK[r["verdict"]] > RANK[worst]:
            worst = r["verdict"]
    print("=" * 64)
    print("※「合格」＝枠内クリア。枠の外とファイル名は見ていません")

    if not args.no_report:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = Path(args.report) if args.report else (
            REPORT_DIR / f"検査レポート_{datetime.now():%Y%m%d-%H%M%S}.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(build_report(results, args.tolerance, source), encoding="utf-8")
        print(f"レポート: {out}")

    return 0 if worst == OK else 1


if __name__ == "__main__":
    # 実行ファイル版をダブルクリック（引数なし）で起動した場合は、
    # 隣の output/ を検査し、結果を読めるよう Enter 待ちで閉じる
    interactive = FROZEN and len(sys.argv) == 1
    if interactive:
        sys.argv.append(str(OUTPUT_DIR))
    try:
        code = main()
    except SystemExit as e:
        if isinstance(e.code, int):
            code = e.code
        else:  # raise SystemExit("メッセージ") の形。捕まえたので自前で表示する
            if e.code is not None:
                print(e.code, file=sys.stderr)
            code = 2
    except Exception as e:
        print(f"実行エラー: {e}", file=sys.stderr)
        code = 2
    if interactive:
        try:
            input("\nEnterキーを押すと閉じます…")
        except EOFError:
            pass
    sys.exit(code)
