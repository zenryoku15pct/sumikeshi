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
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF

# マスキング直後の自動検査に、検査スクリプト（check_masked.py）の中身を借りる。
# 画面の「✓」を「検査に通った」の意味にするため、同じ判定を通す
from check_masked import NG as CHECK_NG
from check_masked import WARN as CHECK_WARN
from check_masked import check_page

# 実行ファイル版（PyInstaller）では、同梱リソースは展開先（_MEIPASS）に入る。
# 利用者が触るフォルダ（output/presets/trash）は _writable_base() が決める。
FROZEN = getattr(sys, "frozen", False)
RES_DIR = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent  # noqa: SLF001


def _can_write(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".sumikeshi-write-test"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except Exception:
        return False


def _writable_base() -> Path:
    """output/presets/trash を置く場所を決める。

    原則は「アプリの隣」。フォルダごと持ち運ぶ使い方（USBに入れて別のPCで使う）が
    そのまま通り、出力の在り処が一目で分かる。

    ただしmacOSの .app は実行ファイルが スミケシ.app/Contents/MacOS/ の中にあるため、
    素直に隣を取るとアプリの内部に書いてしまう。中に書くと、アプリを /Applications へ
    入れた人は出力を見つけられず、書き込む権限も無い。そこで .app の外側を基準にし、
    そこにも書けなければ書類フォルダへ逃がす。
    """
    if not FROZEN:
        return Path(__file__).resolve().parent
    exe = Path(sys.executable).resolve()
    parts = exe.parts
    if sys.platform == "darwin" and len(parts) >= 4 and parts[-2] == "MacOS" \
            and parts[-3] == "Contents" and parts[-4].endswith(".app"):
        near = exe.parents[3]
    else:
        near = exe.parent
    if _can_write(near):
        return near
    # ここへ来るのは「アプリを書き込めない場所に置いた」場合。逃がし先は
    # **作ってから**返す。作らずに返すと、起動時に必ず通る presets/ の作成が
    # 親フォルダ不在で失敗し、画面が出ないまま終わる（黒い画面が無いので
    # 利用者には「反応しない」としか見えない。2026-08-21に実機で再現）
    home = Path.home() / "Documents" / "スミケシ"
    if _can_write(home):
        return home
    # 書類フォルダにも書けない環境（読み取り専用の持ち出し用プロファイル等）。
    # ここまで来たら一時フォルダで動かす。出力の場所は画面から辿れる
    tmp = Path(tempfile.gettempdir()) / "sumikeshi"
    _can_write(tmp)
    return tmp


BASE_DIR = _writable_base()
OUTPUT_DIR = BASE_DIR / "output"
PRESET_DIR = BASE_DIR / "presets"
TRASH_DIR = BASE_DIR / "trash"
# 検査レポートは output/ には置かない。output/ は「人に渡すフォルダ」であり、
# レポートにはファイル名とスキャナが埋めた作成者名などがそのまま載るため、
# フォルダごと渡す運用だと気づかないまま同梱されてしまう（2026-08-21の監査）
REPORT_DIR = BASE_DIR / "検査レポート"

# check_masked.py は単体のコマンドとしても動くので、自前で置き場所を決めている。
# アプリの中から使うときは、こちらが決めた場所に揃えておく（.app の内部を指したまま
# レポートを書きに行かないように）。
import check_masked as _ck  # noqa: E402
_ck.BASE_DIR = BASE_DIR
_ck.OUTPUT_DIR = OUTPUT_DIR
_ck.PRESET_DIR = PRESET_DIR
_ck.REPORT_DIR = REPORT_DIR

# 出力PDFに残す「処理済みの印」。個人情報を含まない1項目（producer）で、
# 検査が「印が無い＝このツールを通っていない疑い」を確実に見つけられるようにする。
# src: は原本のSHA-256の先頭16桁（中身は復元できない指紋）。同じ原本を塗り直した
# ときに、古い出力を見分けて退避するのに使う
TOOL_MARK = "sumikeshi 2.0.0"

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
                # 先にページ数へ収めてから展開する。「2-300000000」のような指定で
                # 数億要素の集合を作りに行かないため（結果は従来と完全に同じ）
                a, b = max(a, 1), min(b, n_pages)
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


# ---------------------------------------------------------------- 紙の形
def page_shape(page) -> tuple:
    """そのページの紙の形＝(縦横比, 回転, 切り取りの有無)。

    矩形は正規化座標(0〜1)で持つため、束の中に形の違うページが混じると、
    そのページだけ別の場所を塗り、検査も同じ誤った場所を見て「✓」になる
    （2026-08-22の総ざらいで実測）。塗る前にこの形を突き合わせて止める。
    """
    r = page.rect
    hidden = (abs(page.cropbox.width - page.mediabox.width) > 1
              or abs(page.cropbox.height - page.mediabox.height) > 1)
    return (round(r.width / r.height, 3), page.rotation, hidden)


def check_page_shapes(doc, pages: list[int], base_shape=None) -> None:
    """対象ページの紙の形を照合し、危ないものは塗らずに止める。

    - 束の中で形（縦横比・回転）が食い違う → 中断（ページ数違いと同じ扱い）
    - CropBox で切り取っただけのページ → 中断。切り取りは表示範囲を変えるだけで
      中身を捨てない。隠れた領域は塗りにも検査にも入らず、受け取った側が
      切り取りを外すだけで氏名が復活する（実測済み）
    - /UserUnit ≠ 1 → 中断（塗る場所が実寸でずれる）
    - base_shape（枠を引いたときに画面に出ていた紙の形）が渡されていれば、
      それとも照合する。「この設定を全ファイルへコピー」の2本目以降の食い違いは、
      これが無いと原理的に検出できない
    誤警報を出さないことは9場面で確認済み——B5への相似縮小・束ごとの回転・
    白紙裏面つきの両面スキャンは止めない。
    """
    shapes = {p: page_shape(doc[p - 1]) for p in pages}
    cropped = [p for p, s in shapes.items() if s[2]]
    if cropped:
        nums = "・".join(str(p) for p in cropped[:5])
        raise ValueError(
            f"{nums}ページ目に切り取り（トリミング）があります。切り取りは中身を残したまま"
            "隠すだけで、このツールでは塗れません。切り取りを解除したPDFで読み込み直してください")
    for p in pages:
        uu = doc.xref_get_key(doc[p - 1].xref, "UserUnit")
        if uu and uu[0] not in ("null", "") and uu[1] not in ("1", "null"):
            raise ValueError(
                f"{p}ページ目が特殊な縮尺（UserUnit）を持っています。"
                "塗る場所がずれるため、このPDFは処理できません")
    base = base_shape if base_shape else shapes[pages[0]][:2]
    diff = [p for p in pages if shapes[p][:2] != tuple(base)]
    if diff:
        nums = "・".join(str(p) for p in diff[:5])
        raise ValueError(
            f"{nums}ページ目の紙の形（向き・大きさ）が、枠を引いたときの紙と違います。"
            "複合機の自動回転や、別々に取り込んだPDFの結合が原因のことが多いです。"
            "向きをそろえてから読み込み直してください")


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
            # 注釈の座標（annot.rect）は非回転座標系なので、比較は非回転側でそろえる。
            # 表示座標のまま比べると、回転ページで枠の真下の付箋が消えず、
            # 無関係な場所の注釈が消える（2026-08-22に実測）。
            targets_unrot = [tr * page.derotation_matrix for tr in targets]
            for annot in list(page.annots() or []):
                if annot.type[0] == fitz.PDF_ANNOT_REDACT:
                    continue  # いま自分で足したリダクション指定は消さない
                if any(not (annot.rect & t).is_empty for t in targets_unrot):
                    page.delete_annot(annot)
            # フォームの入力欄（Widget）は page.annots() の列挙に載らないので別に消す。
            # 入力欄の値（氏名など）は見た目を塗っても値として残る（2026-08-22に実測）
            for w in list(page.widgets() or []):
                if any(not (w.rect & t).is_empty for t in targets_unrot):
                    page.delete_widget(w)

            # graphics=REMOVE_IF_TOUCHED: 線で書かれた筆跡（タブレット記入・描画ツール）は
            # 黒く塗っても座標が残り、枠から20pt以上離れていないと既定では消えない
            # （2026-08-22に実測。「復元不可能」の約束が破れていた）。枠に触れた線画は消す
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED)
        # メタデータの除去: スキャナが埋め込むタイトル・作成者等に
        # 個人情報が含まれる場合があるため、標準メタデータとXMPを消去する。
        # そのうえで処理済みの印だけを残す（個人情報を含まない・検査[0]が
        # 「印なし＝未処理の疑い」を出せるようにする・2026-08-23の設計判断）
        doc.set_metadata({})
        doc.del_xml_metadata()
        src16 = hashlib.sha256(data).hexdigest()[:16]
        doc.set_metadata({"producer": f"{TOOL_MARK} src:{src16}"})
        # ページの絵として描かれない持ち物も落とす。生徒別のPDFを結合した束は
        # 元のファイル名（＝氏名）がしおりの見出しになり、どのビューアでも
        # サイドバーに出る（実在の142ページ束で人名114件が素通りした・2026-08-22実測）。
        # 添付・フォーム定義・ページ縮小画像・構造ツリーも同様に、塗りも検査も届かない
        _strip_baggage(doc)
        buf = io.BytesIO()
        doc.save(buf, garbage=4, deflate=True)
        return buf.getvalue()
    finally:
        doc.close()


def _strip_baggage(doc) -> None:
    """ページの絵として描かれない持ち物を落とす（しおり・添付・入力欄の定義・
    ページ縮小画像・構造ツリー・XMP残骸）。save(garbage=4) が実体を回収する。"""
    root = doc.pdf_catalog()
    for key in ("Outlines", "Names", "AcroForm", "StructTreeRoot", "OpenAction", "AF"):
        if doc.xref_get_key(root, key)[0] != "null":
            doc.xref_set_key(root, key, "null")
    for page in doc:
        for key in ("Thumb", "Metadata", "PieceInfo", "AF"):
            if doc.xref_get_key(page.xref, key)[0] != "null":
                doc.xref_set_key(page.xref, key, "null")


def _rect_on_page(page, r: dict) -> "fitz.Rect":
    pr = page.rect
    return fitz.Rect(
        pr.x0 + r["x0"] * pr.width, pr.y0 + r["y0"] * pr.height,
        pr.x0 + r["x1"] * pr.width, pr.y0 + r["y1"] * pr.height,
    )


def probe_rects(data: bytes, rects: list[dict], page_numbers: list[int]) -> dict:
    """塗る前の原本を、矩形×ページの粒度で調べる。

    塗ったあとの検査では、白紙を黒く塗っても「きれいに消えた」と判定されてしまう
    （指定範囲は確かに均一になっているため）。位置を間違えた矩形に気づくには、
    塗る前に中身があったかを見るしかない。

    戻り値:
      blank_rects   … 全対象ページで枠の下が空白だった矩形の番号（1始まり）
      shifted_pages … ページ自体には中身があるのに、枠の下だけ空白のページ番号
                      （＝そのページだけ枠がずれている疑い。白紙の裏面では鳴らない）
      unknown_pages … 描画できず判定できなかったページ番号（合格に数えない）

    以前は「1ページでも中身があれば正常」と打ち切っていたが、束の一部だけ枠が
    ずれている場面（最も助けが要る場面）で必ず沈黙していた（2026-08-22に実測。
    0.7mm角の薄い点1つで警告が消えた）。ページごとに判定する。
    """
    n = len(rects)
    rect_hit = [set() for _ in range(n)]     # 矩形ごとの「中身があったページ」
    shifted, unknown = set(), set()
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        for pno in page_numbers:
            page = doc[pno - 1]
            # ページ自体に中身があるか（白紙の裏面と、ずれたページを区別する）
            page_has = False
            try:
                whole = page.get_pixmap(dpi=25, colorspace=fitz.csGRAY)
                page_has = bool(whole.samples) and (max(whole.samples) - min(whole.samples)) > 24
            except Exception:
                unknown.add(pno)
                continue
            words = page.get_text("words")
            if words:
                page_has = True
            derot = page.derotation_matrix
            page_rect_hit = False
            for idx, r in enumerate(rects):
                rect = _rect_on_page(page, r)
                # 文字があるか。get_text("words") は回転に関わらず非回転座標を
                # 返すので、比較は非回転側でそろえる（2026-08-22に実測で確定）
                rect_unrot = rect * derot
                hit = any(not (fitz.Rect(w[:4]) & rect_unrot).is_empty for w in words)
                if not hit:
                    # 絵柄に濃淡があるか（スキャン画像）。狭い範囲を粗く描くので軽い
                    try:
                        pix = page.get_pixmap(clip=rect, dpi=50, colorspace=fitz.csGRAY)
                    except Exception:
                        unknown.add(pno)   # 「判定できない」を中身ありに数えない
                        continue
                    hit = bool(pix.samples) and (max(pix.samples) - min(pix.samples)) > 24
                if hit:
                    rect_hit[idx].add(pno)
                    page_rect_hit = True
            if page_has and not page_rect_hit and rects:
                shifted.add(pno)
    finally:
        doc.close()
    return {
        "blank_rects": [i + 1 for i in range(n)
                        if not rect_hit[i] and any(p not in unknown for p in page_numbers)],
        "shifted_pages": sorted(shifted),
        "unknown_pages": sorted(unknown),
    }


def find_blank_rects(data: bytes, rects: list[dict], page_numbers: list[int]) -> list[int]:
    """互換の殻：全対象ページで枠の下が空白だった矩形の番号を返す。"""
    return probe_rects(data, rects, page_numbers)["blank_rects"]


def _page_has_content(data: bytes, pno: int) -> bool:
    """そのページの紙に中身（文字か絵柄）があるか。粗く見るだけなので軽い。"""
    with fitz.open(stream=data, filetype="pdf") as doc:
        page = doc[pno - 1]
        if page.get_text("words"):
            return True
        try:
            pix = page.get_pixmap(dpi=25, colorspace=fitz.csGRAY)
        except Exception:
            return True   # 判定できないときは「残っているかもしれない」側へ
        return bool(pix.samples) and (max(pix.samples) - min(pix.samples)) > 24


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


def _mark_of(path: Path) -> str:
    """出力PDFの処理済みの印（producer）を読む。読めなければ空文字。"""
    try:
        with fitz.open(path) as d:
            return (d.metadata or {}).get("producer") or ""
    except Exception:
        return ""


def unique_out_path(name: str, verdict: str = "ok", src16: str = "") -> Path:
    """出力の置き場を決める。output/ の中身は「枠内クリアの最新版だけ」にする
    （2026-08-23の設計判断）。

    - 枠内クリア（ok）は output/ 直下の「元名_masked.pdf」
    - 要確認・消え残り（warn/ng）は output/要確認/ の「元名_masked_要確認.pdf」。
      合格品と同じ顔で並ぶと、判定が画面と一緒に消えたあとで見分けられない
      （処理した人と持ち出す人が別人の共有PCでは、道具が見つけた消え残りが
      そのまま出て行く・2026-08-22の総ざらい）
    - 同じ原本を塗り直したときは、古い出力（印の src: が一致するもの）を
      trash/ へ退避して素の名前を最新版に譲る。以前は古い失敗版が正規の名前を
      占有し続け、直した方が長い名前になっていた
    """
    if verdict == "ok":
        base_dir, suffix = OUTPUT_DIR, "_masked"
    else:
        base_dir, suffix = OUTPUT_DIR / "要確認", "_masked_要確認"
    base_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(name).stem
    path = base_dir / f"{stem}{suffix}.pdf"
    if path.exists():
        old_mark = _mark_of(path)
        if src16 and f"src:{src16}" in old_mark:
            _to_trash(path)          # 同じ原本の古い出力＝退避して名前を譲る
        else:
            # 別の原本の出力（別フォルダの同名PDF）か、印の無い旧版の出力。
            # 同一と確認できないものは消さず、従来どおり日時つきの名前で並べる
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = base_dir / f"{stem}{suffix}_{ts}.pdf"
            i = 1
            while path.exists():
                path = base_dir / f"{stem}{suffix}_{ts}_{i}.pdf"
                i += 1
    return path


def mask_one_file(name: str, data: bytes, rects_raw, color: str,
                  page_range: str, parity: str, n_pages: int,
                  base_shape=None) -> dict:
    """1ファイル分の一連の処理。app.py の /api/apply のループ内と同じ順序・同じ判定。

      矩形の整形 → ページ指定の解釈 → 塗る前の位置ずれ検出 → マスキング
      → 書き出したPDFを読み直して検査 → 一時ファイル経由で保存

    戻り値は画面がそのまま表示できる形（ブラウザ版の results と同じ）。
    """
    n_raw = len(list(rects_raw or []))
    if n_raw == 0:  # 矩形未設定のファイルはスキップ（エラーにしない）
        return {"skipped": True, "name": name}
    rects = clean_rects(rects_raw)
    # 整形で矩形が減っていたら止める。範囲外の座標を黙って捨てると、画面は
    # 「矩形2」なのに実際に塗るのは1件、という食い違いが起きる（2026-08-22に実測）。
    # 全部が不正だった場合も「未設定スキップ」ではなくエラーとして知らせる
    if len(rects) != n_raw:
        raise ValueError(
            f"矩形{n_raw}件のうち{n_raw - len(rects)}件が不正な座標です（範囲外・大きさゼロ）。"
            "プリセットや設定を確かめて、枠を引き直してください")

    # 塗る紙（data）と、呼び出し側が数えたページ数（n_pages）が食い違っていないか。
    # 食い違うのは「読み込んだあと、実行するまでの間に原本が差し替わった」とき。
    # 見逃すと、増えたページは塗られず検査もされないまま「✓ 検査済み」で出てしまう
    # （2026-08-21に実機で再現。4→8ページの差し替えで5〜8ページの氏名が残った）。
    # 位置合わせの前提も崩れているので、続けずに止めるのが安全。
    pages = parse_page_spec(page_range, parity, n_pages)
    if not pages:
        raise ValueError("指定範囲に該当するページがありません")

    with fitz.open(stream=data, filetype="pdf") as _d:
        real_pages = _d.page_count
        if real_pages != n_pages:
            raise ValueError(
                f"読み込んだときから中身が変わっています（{n_pages}ページ→{real_pages}ページ）。"
                "いったん取り除いて、読み込み直してください")
        # 紙の形の門番。束の中に形の違うページ・切り取り・特殊縮尺が混じっていたら
        # 塗らずに止める（塗る側と検査側が同じだけずれ、✓のまま氏名が残るため）
        check_page_shapes(_d, pages, base_shape)

    # 塗る前に「その場所に中身があるか」を見る（位置ずれの検出）
    probe = probe_rects(data, rects, pages)
    out_bytes = apply_masks(data, rects, color, pages)
    # 書き出す前に、本当に消えているかを機械で確かめる
    check = verify_masked(out_bytes, rects, pages)
    warn_msgs = []
    if probe["blank_rects"]:
        nums = "・".join(f"{i}つ目" for i in probe["blank_rects"])
        warn_msgs.append(f"{nums}の枠は、塗る前から何もありませんでした（位置がずれていませんか）")
    if probe["shifted_pages"]:
        nums = "・".join(str(p) for p in probe["shifted_pages"][:8])
        warn_msgs.append(
            f"{nums}ページ目は、紙に中身があるのに枠の下だけ空白でした（そのページだけ枠がずれていませんか）")
    if probe["unknown_pages"]:
        nums = "・".join(str(p) for p in probe["unknown_pages"][:8])
        warn_msgs.append(f"{nums}ページ目は塗る前の確認ができませんでした（現物を確かめてください）")
    # ページ範囲・奇数偶数で外したページは、塗られても検査されてもいない。
    # 枠の下に中身が残ったままなら知らせる（正当な範囲絞りを誤警報にしないため、
    # 「枠の下に中身があるページ」だけを挙げる）
    excluded = [p for p in range(1, n_pages + 1) if p not in set(pages)]
    if excluded:
        left = probe_rects(data, rects, excluded)
        # 「枠の下に中身が残っているページ」＝紙に中身があり、枠が空白でも判定不能でもない
        skip = set(left["shifted_pages"]) | set(left["unknown_pages"])
        remain = [p for p in excluded
                  if p not in skip and _page_has_content(data, p)]
        if remain:
            nums = "・".join(str(p) for p in remain[:8])
            warn_msgs.append(
                f"対象外にした{nums}ページ目には、枠の場所に中身が残ったままです（塗っても検査してもいません）")
    if warn_msgs:
        for m in reversed(warn_msgs):
            check["reasons"].insert(0, m)
        if check["verdict"] == "ok":
            check["verdict"] = "warn"

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    src16 = hashlib.sha256(data).hexdigest()[:16]
    out_path = unique_out_path(name, check["verdict"], src16)
    # 一時ファイルへ書いてから名前を付け替える。書いている途中で
    # 強制終了されても、中途半端なPDFが完成品の顔で残らない
    tmp_path = out_path.with_suffix(".pdf.part")
    tmp_path.write_bytes(out_bytes)
    os.replace(tmp_path, out_path)
    rel_out = str(out_path.relative_to(OUTPUT_DIR)) if out_path.is_relative_to(OUTPUT_DIR) \
        else out_path.name
    return {
        "ok": True,
        "name": name,
        "output": rel_out,
        "pagesMasked": len(pages),
        "totalPages": n_pages,
        "excludedPages": len(excluded),
        "check": check,
    }


# ---------------------------------------------------------------- プリセット
def safe_preset_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|.\x00-\x1f]+', "_", (name or "").strip())
    return name[:60] or "preset"


def list_presets() -> list[dict]:
    PRESET_DIR.mkdir(parents=True, exist_ok=True)
    presets = []
    for p in sorted(PRESET_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            data["name"] = p.stem
            presets.append(data)
        except Exception:
            continue
    return presets


def save_preset(name: str, rects_raw, color: str, page_range: str, parity: str,
                shape=None) -> str:
    name = safe_preset_name(name)
    rects = clean_rects(rects_raw)
    if not rects:
        raise ValueError("保存する矩形がありません")
    if len(rects) != len(list(rects_raw or [])):
        raise ValueError("不正な座標の矩形が含まれています。枠を引き直してください")
    preset = {
        "rects": rects,
        "color": color or "black",
        "range": page_range or "",
        "parity": parity or "all",
        # 枠を引いたときの紙の形（縦横比・回転）。読み込んだ先で照合する。
        # 古いプリセット（この項目が無いもの）はそのまま読める
        "shape": list(shape) if shape else [],
        "saved": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    PRESET_DIR.mkdir(parents=True, exist_ok=True)
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
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.move(str(path), str(TRASH_DIR / f"{path.name}.{ts}.bak"))


# ---------------------------------------------------------------- その他
def open_folder(path: Path) -> None:
    """OSごとのファイル管理アプリでフォルダを開く（macOS / Windows / Linux）。"""
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(path)])
