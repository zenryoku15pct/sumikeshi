#!/usr/bin/env python3
"""スミケシ — スキャンPDFの定位置マスキングツール（デスクトップアプリ）

ページを見ながら矩形をドラッグで指定し、全ページ・複数PDFを一括マスキングする。
矩形・対象ページの設定はファイルごとに持ち、全ファイルへのコピーもできる。

マスキングそのものは masking.py が行う（PyMuPDFのリダクションで画像ピクセル
自体を消去する。黒い四角を上に重ねるだけの「見た目の黒塗り」ではなく復元不可能）。
原本は読むだけで変更しない。出力は ./output/ に別名で保存する。

著作権表示（AGPL-3.0 第7条bに基づく保持要求つき）:
    スミケシ（sumikeshi）  Copyright (C) 2026 全力の15%
    zenryoku15@gmail.com  /  ライセンス: GNU AGPL-3.0（LICENSE 参照）
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF

import masking

APP_NAME = "スミケシ"
APP_VERSION = "2.0.0"

# ---------------------------------------------------------------- 自己診断
# CIの動作確認用。Qtを起動せずにマスキング中核だけを走らせて終わる。
# 画面なしの環境でも「中核が生きているか」を確かめられるようにしておく。
def selftest() -> int:
    print(f"{APP_NAME} {APP_VERSION} 自己診断")
    sample = masking.RES_DIR / "samples" / "ダミー調査票.pdf"
    if not sample.exists():
        sample = masking.BASE_DIR / "お試し用PDF" / "ダミー調査票.pdf"
    if not sample.exists():
        print("  素材PDFが見つからないため、処理の確認は省略します")
        return 0
    data = sample.read_bytes()
    with fitz.open(stream=data, filetype="pdf") as d:
        n = d.page_count
    rects = [{"x0": 0.05, "y0": 0.06, "x1": 0.55, "y1": 0.13}]
    pages = masking.parse_page_spec("", "all", n)
    out = masking.apply_masks(data, rects, "black", pages)
    res = masking.verify_masked(out, rects, pages)
    print(f"  素材: {sample.name}（{n}ページ）")
    print(f"  マスキング: {len(data):,} → {len(out):,} バイト")
    print(f"  検査: {res['verdict']}")
    if res["verdict"] == "ng":
        for r in res["reasons"]:
            print(f"    - {r}")
        return 1
    print("  自己診断に合格しました")
    return 0


if "--selftest" in sys.argv:
    raise SystemExit(selftest())

from PySide6.QtCore import (QEvent, QPointF, QRectF, QSize, Qt, QThread,
                            QTimer, Signal)
from PySide6.QtGui import (QAction, QColor, QCursor, QFont, QGuiApplication, QIcon,
                           QImage, QKeySequence, QPainter, QPalette, QPen, QPixmap,
                           QShortcut)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QProgressDialog, QPushButton, QRadioButton,
                               QScrollArea, QSizePolicy, QSplitter, QTextBrowser,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

# 画面の色（ブラウザ版の見た目をそのまま引き継ぐ）
RED = QColor(220, 38, 38)
BLUE = QColor(37, 99, 235)
INK = "#1f2933"
MUTED = "#616e7c"
LINE = "#d7dce3"
BG = "#f5f7fa"

# チェック欄の印。Qtの装飾指定で読むので、区切りは / に揃える（Windowsでも同じ書き方）
CHECK_PNG = (masking.RES_DIR / "assets" / "check.png").as_posix()

MIN_SIZE = 0.005    # これより小さいドラッグは矩形として採用しない
HANDLE_TOL = 8      # ハンドルの当たり判定（画面px）
HANDLE_PX = 8       # ハンドルの一辺（画面px）

CURSORS = {
    "n": Qt.SizeVerCursor, "s": Qt.SizeVerCursor,
    "e": Qt.SizeHorCursor, "w": Qt.SizeHorCursor,
    "ne": Qt.SizeBDiagCursor, "sw": Qt.SizeBDiagCursor,
    "nw": Qt.SizeFDiagCursor, "se": Qt.SizeFDiagCursor,
}


def norm_rect(r: dict) -> dict:
    """x0<x1, y0<y1 に整える（ドラッグ方向によらず同じ形にする）。"""
    return {"x0": min(r["x0"], r["x1"]), "y0": min(r["y0"], r["y1"]),
            "x1": max(r["x0"], r["x1"]), "y1": max(r["y0"], r["y1"])}


def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------- 読み込んだPDF
@dataclass
class DocEntry:
    """読み込んだPDF1本ぶん。矩形・ページ範囲・奇数偶数はファイルごとに持つ。"""
    doc_id: int
    path: Path
    name: str
    pages: int
    rects: list = field(default_factory=list)
    page_range: str = ""
    parity: str = "all"
    _doc: object = None
    # 読み込んだ瞬間のファイルの姿。実行の直前に見比べて、間に差し替えられて
    # いないかを確かめる（差し替わっていると、画面で見た紙と塗る紙が別物になる）
    stamp: tuple = ()

    @staticmethod
    def stamp_of(path: Path) -> tuple:
        try:
            st = path.stat()
            return (st.st_size, st.st_mtime_ns)
        except OSError:
            return ()

    def document(self):
        """表示用。**原本のファイルは掴まない。**

        パスから開くと、そのPDFをアプリが開きっぱなしになる。Windowsでは
        開いている間そのファイルを移動・削除できないため、実行後に結果欄が
        促す「元ファイルの隔離・削除」が、促した当のアプリに塞がれてしまう
        （2026-08-21の監査で判明）。バイト列を写してから開けば掴まない。
        写しは表示中の1本ぶんだけ持ち、切り替えたら手放す（switch_doc）。
        """
        if self._doc is None:
            self._doc = fitz.open(stream=self.path.read_bytes(), filetype="pdf")
        return self._doc

    def close(self):
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:
                pass
            self._doc = None


# ---------------------------------------------------------------- ページ表示
class PageCanvas(QWidget):
    """PDFのページを表示し、その上で矩形を描く・選ぶ・動かす・大きさを変える。

    ブラウザ版の canvas 実装（static/app.js の drawOverlay / hitTest / handleAt と
    ポインタ操作）をそのまま移したもの。矩形は正規化座標(0〜1)のまま保持するので、
    表示倍率が変わっても masking.py へそのまま渡せる。
    """
    rectsChanged = Signal()
    selectionChanged = Signal()
    notify = Signal(str, bool)
    pageStep = Signal(int)

    PLACEHOLDER = ("PDFを読み込むと、ここにページが表示されます。\n"
                   "ページ上をドラッグしてマスクする範囲を指定してください。")

    def __init__(self):
        super().__init__()
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(340, 400)
        self._pix: QPixmap | None = None
        self.rects: list = []
        self.selected = -1
        self.hover = -1
        self.preview = False
        self.mask_color = "black"
        self._action = None
        self._drag = None

    # -------------------------------------------------- 表示の受け渡し
    def set_page(self, pix: QPixmap | None):
        self._pix = pix
        self.update()

    def set_rects(self, rects: list):
        self.rects = rects
        self.update()

    def set_selected(self, i: int):
        self.selected = i
        self.update()

    def set_hover(self, i: int):
        if self.hover != i:
            self.hover = i
            self.update()

    def set_preview(self, on: bool):
        self.preview = on
        self.update()

    def set_mask_color(self, c: str):
        self.mask_color = c
        self.update()

    # -------------------------------------------------- 座標の変換
    def page_rect(self) -> QRectF:
        """ウィジェットの中で、ページを実際に描く矩形（縦横比を保って中央寄せ）。"""
        if self._pix is None or self._pix.isNull():
            return QRectF()
        m = 14
        aw = max(1.0, self.width() - 2 * m)
        ah = max(1.0, self.height() - 2 * m)
        dpr = self._pix.devicePixelRatio() or 1.0
        pw = self._pix.width() / dpr
        ph = self._pix.height() / dpr
        s = min(aw / pw, ah / ph)
        w, h = pw * s, ph * s
        return QRectF(m + (aw - w) / 2, m + (ah - h) / 2, w, h)

    def _to_norm(self, pos) -> dict:
        pr = self.page_rect()
        if pr.isEmpty():
            return {"x": 0.0, "y": 0.0}
        x = (pos.x() - pr.x()) / pr.width()
        y = (pos.y() - pr.y()) / pr.height()
        return {"x": min(1.0, max(0.0, x)), "y": min(1.0, max(0.0, y))}

    # -------------------------------------------------- 描画
    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#e8ecf2"))
        if self._pix is None or self._pix.isNull():
            p.setPen(QColor(MUTED))
            f = p.font()
            f.setPointSize(12)
            p.setFont(f)
            p.drawText(self.rect().adjusted(40, 0, -40, 0),
                       Qt.AlignCenter | Qt.TextWordWrap, self.PLACEHOLDER)
            return
        pr = self.page_rect()
        p.fillRect(pr.translated(0, 2), QColor(15, 23, 42, 40))  # 紙の影
        p.drawPixmap(pr, self._pix, QRectF(self._pix.rect()))
        p.setPen(QPen(QColor(0, 0, 0, 40), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(pr)
        for i, r in enumerate(self.rects):
            self._draw_rect(p, pr, norm_rect(r), i, False)
        if self._drag:
            self._draw_rect(p, pr, norm_rect(self._drag), -1, True)

    def _draw_rect(self, p: QPainter, pr: QRectF, r: dict, i: int, temp: bool):
        box = QRectF(pr.x() + r["x0"] * pr.width(), pr.y() + r["y0"] * pr.height(),
                     (r["x1"] - r["x0"]) * pr.width(), (r["y1"] - r["y0"]) * pr.height())
        if self.preview and not temp:
            # マスク後プレビュー：実際に塗られる色で塗って見せる
            p.fillRect(box, QColor(0, 0, 0) if self.mask_color == "black" else QColor(255, 255, 255))
            p.setPen(QPen(QColor(100, 116, 139, 153), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRect(box)
            return
        sel = (i == self.selected)
        hot = sel or i == self.hover
        p.fillRect(box, QColor(37, 99, 235, 89) if hot else QColor(220, 38, 38, 64))
        pen = QPen(BLUE if hot else RED, 2)
        if temp:
            pen.setStyle(Qt.CustomDashLine)
            pen.setDashPattern([3, 2])
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(box)
        if not temp:
            badge = QRectF(box.x(), box.y(), 20, 18)
            p.fillRect(badge, BLUE if hot else RED)
            f = p.font()
            f.setBold(True)
            f.setPointSize(10)
            p.setFont(f)
            p.setPen(QColor("#ffffff"))
            p.drawText(badge, Qt.AlignCenter, str(i + 1))
            f.setBold(False)
            p.setFont(f)
        if sel and not temp:
            self._draw_handles(p, pr, r)

    def _draw_handles(self, p: QPainter, pr: QRectF, r: dict):
        p.setPen(QPen(BLUE, 1.5))
        p.setBrush(QColor("#ffffff"))
        for hx, hy in self._handle_points(r):
            x = pr.x() + hx * pr.width()
            y = pr.y() + hy * pr.height()
            p.drawRect(QRectF(x - HANDLE_PX / 2, y - HANDLE_PX / 2, HANDLE_PX, HANDLE_PX))
        p.setBrush(Qt.NoBrush)

    @staticmethod
    def _handle_points(r: dict):
        """四隅＋各辺の中央の8点。"""
        mx = (r["x0"] + r["x1"]) / 2
        my = (r["y0"] + r["y1"]) / 2
        return [(r["x0"], r["y0"]), (mx, r["y0"]), (r["x1"], r["y0"]),
                (r["x0"], my), (r["x1"], my),
                (r["x0"], r["y1"]), (mx, r["y1"]), (r["x1"], r["y1"])]

    # -------------------------------------------------- 当たり判定
    def _handle_at(self, r: dict, p: dict):
        """矩形の縁のどのハンドル上か（"nw","n","ne","w","e","sw","s","se" / None）。"""
        pr = self.page_rect()
        if pr.isEmpty():
            return None
        tx = HANDLE_TOL / pr.width()
        ty = HANDLE_TOL / pr.height()
        near_x0 = abs(p["x"] - r["x0"]) <= tx
        near_x1 = abs(p["x"] - r["x1"]) <= tx
        near_y0 = abs(p["y"] - r["y0"]) <= ty
        near_y1 = abs(p["y"] - r["y1"]) <= ty
        in_x = r["x0"] - tx <= p["x"] <= r["x1"] + tx
        in_y = r["y0"] - ty <= p["y"] <= r["y1"] + ty
        h = ""
        if near_y0 and in_x:
            h += "n"
        elif near_y1 and in_x:
            h += "s"
        if near_x0 and in_y:
            h += "w"
        elif near_x1 and in_y:
            h += "e"
        return h or None

    def _hit_test(self, p: dict):
        # 選択中矩形のハンドルを最優先
        if 0 <= self.selected < len(self.rects):
            h = self._handle_at(self.rects[self.selected], p)
            if h:
                return {"type": "handle", "index": self.selected, "handle": h}
        # 重なっている場合は後から追加した（上に描かれる）矩形を優先
        for i in range(len(self.rects) - 1, -1, -1):
            r = self.rects[i]
            if r["x0"] <= p["x"] <= r["x1"] and r["y0"] <= p["y"] <= r["y1"]:
                return {"type": "inside", "index": i}
        return None

    # -------------------------------------------------- マウス操作
    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        self.setFocus(Qt.MouseFocusReason)
        if self._pix is None:
            return
        if self.preview:
            self.notify.emit("プレビュー中は編集できません（プレビューを解除してください）", False)
            return
        p = self._to_norm(ev.position())
        hit = self._hit_test(p)
        if hit and hit["type"] == "handle":
            # 元の位置を控える。掴みが外から切られたときに戻せるようにするため
            self._action = {"type": "resize", "handle": hit["handle"],
                            "orig": dict(self.rects[hit["index"]])}
            self.selected = hit["index"]
        elif hit and hit["type"] == "inside":
            self.selected = hit["index"]
            self._action = {"type": "move", "start": p, "orig": dict(self.rects[hit["index"]])}
        else:
            self.selected = -1
            self._action = {"type": "draw"}
            self._drag = {"x0": p["x"], "y0": p["y"], "x1": p["x"], "y1": p["y"]}
        self.selectionChanged.emit()
        self.update()

    def mouseMoveEvent(self, ev):
        p = self._to_norm(ev.position())
        # 左ボタンを押していないのに操作中になっているのは、掴みが外から
        # 切られた（画面ロック・画面切り替え・他アプリに入力を取られた）とき。
        # 拾わないと、指を離したつもりのカーソル移動だけで氏名欄の矩形が
        # 付いてきて、ずれたまま塗られる（2026-08-21に実機で再現）
        if self._action and not (ev.buttons() & Qt.LeftButton):
            self._cancel_action()
            return
        if not self._action:
            # 操作していない間はカーソル形状でヒントを出す
            if self.preview or self._pix is None:
                self.setCursor(Qt.ArrowCursor)
                return
            hit = self._hit_test(p)
            if not hit:
                self.setCursor(Qt.CrossCursor)
            elif hit["type"] == "handle":
                self.setCursor(CURSORS[hit["handle"]])
            else:
                self.setCursor(Qt.SizeAllCursor)
            return
        t = self._action["type"]
        if t == "draw":
            self._drag["x1"] = p["x"]
            self._drag["y1"] = p["y"]
        elif t == "resize" and 0 <= self.selected < len(self.rects):
            r = self.rects[self.selected]
            h = self._action["handle"]
            if "w" in h:
                r["x0"] = min(max(0.0, p["x"]), r["x1"] - MIN_SIZE)
            if "e" in h:
                r["x1"] = max(min(1.0, p["x"]), r["x0"] + MIN_SIZE)
            if "n" in h:
                r["y0"] = min(max(0.0, p["y"]), r["y1"] - MIN_SIZE)
            if "s" in h:
                r["y1"] = max(min(1.0, p["y"]), r["y0"] + MIN_SIZE)
        elif t == "move" and 0 <= self.selected < len(self.rects):
            a = self._action
            rw = a["orig"]["x1"] - a["orig"]["x0"]
            rh = a["orig"]["y1"] - a["orig"]["y0"]
            nx = min(max(0.0, a["orig"]["x0"] + (p["x"] - a["start"]["x"])), 1.0 - rw)
            ny = min(max(0.0, a["orig"]["y0"] + (p["y"] - a["start"]["y"])), 1.0 - rh)
            r = self.rects[self.selected]
            r["x0"], r["y0"], r["x1"], r["y1"] = nx, ny, nx + rw, ny + rh
        self.update()

    def _cancel_action(self):
        """操作を取り消して、始める前の位置・大きさへ戻す。

        旧版はブラウザの pointercancel が同じ役目をしていた
        （backup/static.20260821-2147/app.js の pointercancel）。
        """
        a = self._action
        self._action = None
        if not a:
            return
        if a["type"] == "draw":
            self._drag = None                       # 描きかけは捨てる
        elif a.get("orig") and 0 <= self.selected < len(self.rects):
            self.rects[self.selected].update(a["orig"])   # 元へ戻す
        self.rectsChanged.emit()
        self.update()

    def event(self, ev):
        # マウスの掴みがOSから外れた合図。上と同じ理由で必ず後始末する
        if ev.type() == QEvent.UngrabMouse and self._action:
            self._cancel_action()
        return super().event(ev)

    def mouseReleaseEvent(self, ev):
        if not self._action:
            return
        if self._action["type"] == "draw" and self._drag:
            r = norm_rect(self._drag)
            self._drag = None
            if r["x1"] - r["x0"] > MIN_SIZE and r["y1"] - r["y0"] > MIN_SIZE:
                self.rects.append(r)
                self.selected = len(self.rects) - 1  # 追加した矩形をそのまま選択
        self._action = None
        self.rectsChanged.emit()
        self.update()

    def leaveEvent(self, ev):
        self.set_hover(-1)

    # -------------------------------------------------- キーボード
    # ←→ページ送り / Delete・Backspace 選択矩形の削除 / Esc 選択解除
    # （⌘C・⌘V はこのウィジェットに付けたショートカットで処理する）
    def keyPressEvent(self, ev):
        k = ev.key()
        if k == Qt.Key_Left:
            self.pageStep.emit(-1)
        elif k == Qt.Key_Right:
            self.pageStep.emit(1)
        elif k in (Qt.Key_Delete, Qt.Key_Backspace) and self.selected >= 0:
            self.delete_selected()
        elif k == Qt.Key_Escape and self.selected >= 0:
            self.selected = -1
            self.selectionChanged.emit()
            self.update()
        else:
            super().keyPressEvent(ev)

    def delete_selected(self):
        i = self.selected
        if not (0 <= i < len(self.rects)):
            return
        self.rects.pop(i)
        self.selected = -1
        self.rectsChanged.emit()
        self.update()


# ---------------------------------------------------------------- 裏方の処理
class BusyDialog(QProgressDialog):
    """処理中に出す窓。**閉じられない**。

    中止ボタンを外しただけでは、Escキーでも右上の×でも閉じてしまう。閉じると
    本体の締め出しが解けて画面が触れる状態に戻り、そこで矩形を動かすと、
    まだ塗っていないファイルが動かした後の位置で塗られる。検査も同じ矩形を
    見るので「✓ 検査済み」のまま氏名が残る（2026-08-21に実機で再現）。
    処理が終わるまで閉じさせない。
    """

    def __init__(self, text: str, total: int, parent=None):
        super().__init__(text, "", 0, total, parent)
        self._closable = False
        self.setWindowTitle("スミケシ")
        self.setCancelButton(None)
        self.setWindowModality(Qt.WindowModal)
        self.setMinimumDuration(0)
        self.setAutoClose(False)
        self.setAutoReset(False)
        self.setValue(0)

    def allow_close(self):
        self._closable = True

    def reject(self):          # Escキー・×ボタンの両方がここへ来る
        if self._closable:
            super().reject()

    def closeEvent(self, ev):
        if self._closable:
            super().closeEvent(ev)
        else:
            ev.ignore()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape and not self._closable:
            ev.ignore()
            return
        super().keyPressEvent(ev)


class MaskWorker(QThread):
    """マスキングを別スレッドで走らせる。画面が固まらないようにするため。"""
    progress = Signal(int, int, str)
    done = Signal(list)

    def __init__(self, jobs: list, color: str):
        super().__init__()
        self.jobs = jobs
        self.color = color

    def run(self):
        results = []
        for i, job in enumerate(self.jobs, 1):
            self.progress.emit(i, len(self.jobs), job["name"])
            try:
                data = job["path"].read_bytes()
                # 読み込んだときと同じファイルか。取り込み直して同じ名前で
                # 書き戻されていると、画面で見た紙と塗る紙が別物になる
                now = DocEntry.stamp_of(job["path"])
                if job.get("stamp") and now and now != job["stamp"]:
                    raise ValueError(
                        "読み込んだときからファイルが変わっています。"
                        "いったん取り除いて、読み込み直してください")
                results.append(masking.mask_one_file(
                    job["name"], data, job["rects"], self.color,
                    job["range"], job["parity"], job["pages"]))
            except Exception as e:  # 1ファイルの失敗で全体を止めない
                results.append({"ok": False, "name": job["name"], "error": str(e)})
        self.done.emit(results)


class CheckWorker(QThread):
    """マスク済みPDFの検査を別スレッドで走らせる。"""
    progress = Signal(int, int, str)
    done = Signal(list, str)

    def __init__(self, files: list, rects_norm, source: str, opt: dict):
        super().__init__()
        self.files = files
        self.rects_norm = rects_norm
        self.source = source
        self.opt = opt

    def run(self):
        import check_masked as ck
        results = []
        for i, f in enumerate(self.files, 1):
            if self.isInterruptionRequested():   # 窓を閉じられた。切り上げる
                return
            self.progress.emit(i, len(self.files), f.name)
            try:
                results.append(ck.check_file(f, self.rects_norm, self.opt, None, True))
            except Exception as e:
                # 1本の異常PDFで他ファイルの結果まで失わない
                results.append({"path": f, "verdict": ck.WARN, "pages": [],
                                "file_reasons": [f"検査不能（{e}）——このファイルは安全と確認できていない"]})
        self.done.emit(results, self.source)


# ---------------------------------------------------------------- 部品
class ElidedLabel(QLabel):
    """幅に収まらない文字列を「…」で省略して表示する（長いファイル名対策）。"""

    def __init__(self, text=""):
        super().__init__()
        self._full = text
        self.setToolTip(text)
        self.setMinimumWidth(40)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def setFullText(self, text):
        self._full = text
        self.setToolTip(text)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        fm = p.fontMetrics()
        p.setPen(self.palette().windowText().color())
        p.drawText(self.rect(), Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(self._full, Qt.ElideMiddle, self.width()))


class HoverRow(QWidget):
    """マウスが乗ったことを知らせる行。

    一覧の項目にウィジェットを敷くと、その下（viewport）までマウスの動きが
    届かず itemEntered が飛ばない（2026-08-21に実測）。旧版は一覧の行に
    マウスを乗せるとページ上の該当矩形が青くなったので、行そのものに
    知らせてもらってその挙動を戻す。
    """
    hovered = Signal(bool)

    def enterEvent(self, ev):
        self.hovered.emit(True)
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        # 子（✕ボタン）へ移っただけのときは離れたことにしない
        if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            self.hovered.emit(False)
        super().leaveEvent(ev)


def small_btn(text: str, tip: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setProperty("kind", "icon")
    b.setFixedSize(QSize(22, 22))
    b.setCursor(Qt.PointingHandCursor)
    if tip:
        b.setToolTip(tip)
    return b


def card(title: str, step: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    """サイドバーの1区画。ブラウザ版の .card と同じ見た目。"""
    f = QFrame()
    f.setProperty("kind", "card")
    v = QVBoxLayout(f)
    v.setContentsMargins(14, 12, 14, 14)
    v.setSpacing(8)
    head = QHBoxLayout()
    head.setSpacing(7)
    if step:
        s = QLabel(step)
        s.setProperty("kind", "step")
        s.setFixedSize(QSize(20, 20))
        s.setAlignment(Qt.AlignCenter)
        head.addWidget(s)
    t = QLabel(title)
    t.setProperty("kind", "cardTitle")
    head.addWidget(t)
    head.addStretch(1)
    v.addLayout(head)
    return f, v


def fit_list(lw: QListWidget, max_h: int):
    """一覧の高さを中身に合わせる。空なら隠す（下に空白が居座らないように）。"""
    n = lw.count()
    if n == 0:
        lw.setVisible(False)
        return
    lw.setVisible(True)
    h = sum(lw.sizeHintForRow(i) for i in range(n)) + 2 * lw.frameWidth() + 4
    lw.setFixedHeight(min(max_h, max(30, h)))


def hint(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setProperty("kind", "hint")
    lb.setWordWrap(True)
    return lb


# ---------------------------------------------------------------- 検査の窓
class CheckDialog(QDialog):
    """マスク済みPDFの全ページ自動検査（コンソール版 sumikeshi-check と同じ判定）。

    判定そのものは check_masked.py をそのまま呼ぶ。ここは表示だけを受け持つ。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("マスク済みPDFの検査")
        self.resize(760, 560)
        self._worker = None
        self._closing = False
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)

        v.addWidget(hint(
            "できあがったPDFを読み直し、マスクした範囲が本当に消えているかを全ページ調べます。"))

        row = QHBoxLayout()
        row.addWidget(QLabel("対象フォルダ:"))
        self.folder = QLineEdit(str(masking.OUTPUT_DIR))
        row.addWidget(self.folder, 1)
        pick = QPushButton("参照…")
        pick.clicked.connect(self._pick_folder)
        row.addWidget(pick)
        v.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("マスク範囲:"))
        self.rect_src = QComboBox()
        self.rect_src.addItem("自動検出（塗りつぶしを探す）", None)
        for p in masking.list_presets():
            self.rect_src.addItem(f"プリセット「{p['name']}」（矩形{len(p.get('rects', []))}件）",
                                  p.get("rects", []))
        self.rect_src.setToolTip("白で塗った場合は自動検出できないため、プリセットで範囲を指定してください")
        row2.addWidget(self.rect_src, 1)
        self.run_btn = QPushButton("検査する")
        self.run_btn.setProperty("kind", "primary")
        self.run_btn.clicked.connect(self._run)
        row2.addWidget(self.run_btn)
        v.addLayout(row2)

        self.summary = QLabel("")
        self.summary.setProperty("kind", "hint")
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["ファイル / ページ", "判定", "内容"])
        self.tree.setColumnWidth(0, 300)
        self.tree.setColumnWidth(1, 70)
        self.tree.setAlternatingRowColors(True)
        v.addWidget(self.tree, 1)

        v.addWidget(hint(
            "⚠ 検査が見ているのは<b>塗った範囲の中だけ</b>です。押さえるべき欄がすべて入っているかは、"
            "できあがったPDFを目で確かめてください。"))

        bottom = QHBoxLayout()
        self.report_lb = QLabel("")
        self.report_lb.setProperty("kind", "hint")
        bottom.addWidget(self.report_lb, 1)
        close = QPushButton("閉じる")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        v.addLayout(bottom)

    def closeEvent(self, ev):
        """検査の最中に閉じられたときの後始末。

        裏方を走らせたまま窓を捨てると、アプリ終了時に異常終了する
        （QThread: Destroyed while thread is still running・2026-08-21に実測）。
        切り上げを頼んで、終わるのを待ってから閉じる。
        """
        w = self._worker
        if w is not None and w.isRunning():
            self._closing = True
            w.requestInterruption()
            w.wait(5000)
        super().closeEvent(ev)

    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "検査するフォルダを選ぶ", self.folder.text())
        if d:
            self.folder.setText(d)

    def _run(self):
        folder = Path(self.folder.text().strip())
        if not folder.is_dir():
            QMessageBox.warning(self, "検査", "フォルダが見つかりません。")
            return
        files = sorted(folder.glob("*.pdf"))
        if not files:
            QMessageBox.information(self, "検査", "このフォルダにPDFがありません。")
            return
        rects = self.rect_src.currentData()
        source = "自動検出" if rects is None else self.rect_src.currentText()
        opt = {"tolerance": 8, "inset": 2, "detect_white": False, "allow_text_outside": False}
        self.tree.clear()
        self.summary.setText("")
        self.report_lb.setText("")
        self.run_btn.setEnabled(False)
        self.run_btn.setText("検査中…")
        self._worker = CheckWorker(files, rects, source, opt)
        self._worker.progress.connect(
            lambda i, n, name: self.summary.setText(f"検査中… {i}/{n}　{name}"))
        self._worker.done.connect(self._show)
        self._worker.start()

    def _show(self, results, source):
        import check_masked as ck
        if self._closing:
            # 窓は閉じられている。見てもらえない結果を画面に描いても、
            # 本人が見ていないレポートを output/ に増やしても意味がない
            return
        self.run_btn.setEnabled(True)
        self.run_btn.setText("検査する")
        marks = {ck.OK: ("✓ 合格", "#0b7a44"), ck.NG: ("✕ 不合格", "#b91c1c"),
                 ck.WARN: ("⚠ 要確認", "#a16207")}
        n_ok = n_ng = n_warn = 0
        for r in results:
            label, color = marks.get(r["verdict"], ("?", INK))
            ng = sum(1 for p in r.get("pages", []) if p["verdict"] == ck.NG)
            wn = sum(1 for p in r.get("pages", []) if p["verdict"] == ck.WARN)
            top = QTreeWidgetItem([
                r["path"].name, label,
                f"{r.get('n_pages', '?')}ページ中　不合格{ng} / 要確認{wn}"])
            top.setForeground(1, QColor(color))
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            self.tree.addTopLevelItem(top)
            for x in r.get("file_reasons", []):
                top.addChild(QTreeWidgetItem(["", "", x]))
            for p in r.get("pages", []):
                if p["verdict"] == ck.OK:
                    continue
                lab, col = marks[p["verdict"]]
                child = QTreeWidgetItem([f"{p['page']}ページ", lab, "／".join(p["reasons"])])
                child.setForeground(1, QColor(col))
                top.addChild(child)
            if r["verdict"] == ck.OK:
                n_ok += 1
            elif r["verdict"] == ck.NG:
                n_ng += 1
                top.setExpanded(True)
            else:
                n_warn += 1
                top.setExpanded(True)
        self.summary.setText(
            f"検査済み {len(results)} ファイル　—　合格 {n_ok} / 要確認 {n_warn} / <b>不合格 {n_ng}</b>"
            f"　（マスク領域＝{esc(source)}）")
        try:
            masking.OUTPUT_DIR.mkdir(exist_ok=True)
            from datetime import datetime as _dt
            out = masking.OUTPUT_DIR / f"検査レポート_{_dt.now():%Y%m%d-%H%M%S}.md"
            out.write_text(ck.build_report(results, 8, source), encoding="utf-8")
            self.report_lb.setText(f"レポート: output/{out.name}")
        except Exception as e:
            self.report_lb.setText(f"レポートを書けませんでした: {e}")


# ---------------------------------------------------------------- 本体の窓
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} — PDFマスキングツール")
        self.resize(1220, 830)
        self.setMinimumSize(980, 640)
        self.setAcceptDrops(True)

        self.entries: list[DocEntry] = []
        self.ref_id: int | None = None
        self.page = 1
        self._next_id = 1
        self.clipboard = None      # 矩形のコピー＆貼り付け用
        self._mask_worker = None
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)

        root = QWidget()
        root.setObjectName("root")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_sidebar())
        split.addWidget(self._build_viewer())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([410, 810])
        split.setChildrenCollapsible(False)
        outer.addWidget(split, 1)
        self.setCentralWidget(root)

        self._toast = QLabel("", root)
        self._toast.setObjectName("toast")
        self._toast.setAlignment(Qt.AlignCenter)
        self._toast.hide()
        self._toast_timer.timeout.connect(self._toast.hide)

        self._build_menu()
        self.refresh_presets()
        self.render_all()
        self.update_paste_button()

    # ============================================================ 組み立て
    def _build_header(self) -> QWidget:
        f = QFrame()
        f.setObjectName("appHeader")
        h = QHBoxLayout(f)
        h.setContentsMargins(18, 12, 18, 12)
        left = QVBoxLayout()
        left.setSpacing(2)
        t = QLabel(APP_NAME)
        t.setObjectName("appTitle")
        left.addWidget(t)
        s = QLabel("スキャンPDFの同じ位置を全ページ一括マスキング（画像データごと消去・復元不可）")
        s.setProperty("kind", "hint")
        left.addWidget(s)
        h.addLayout(left)
        h.addStretch(1)
        c = QLabel("© 2026 全力の15%")
        c.setProperty("kind", "hint")
        h.addWidget(c, 0, Qt.AlignBottom)
        return f

    def _build_menu(self):
        # macOSでは「について」「終了」がロール指定によりアプリメニューへ自動で移る。
        # Windows・Linuxでは窓の中にこのまま出るので、両方で自然な構成にしておく。
        f = self.menuBar().addMenu("ファイル")
        op = QAction("PDFを開く…", self)
        op.setShortcut(QKeySequence.Open)
        op.triggered.connect(self.pick_files)
        f.addAction(op)
        oo = QAction("出力フォルダを開く", self)
        oo.triggered.connect(lambda: masking.open_folder(masking.OUTPUT_DIR))
        f.addAction(oo)
        f.addSeparator()
        quit_a = QAction("終了", self)
        quit_a.setMenuRole(QAction.QuitRole)
        quit_a.setShortcut(QKeySequence.Quit)
        quit_a.triggered.connect(self.close)
        f.addAction(quit_a)

        h = self.menuBar().addMenu("ヘルプ")
        about = QAction(f"{APP_NAME}について", self)
        about.setMenuRole(QAction.AboutRole)
        about.triggered.connect(self._about)
        h.addAction(about)

    def _about(self):
        QMessageBox.about(self, f"{APP_NAME}について",
                          f"<b>{APP_NAME}（sumikeshi）</b> {APP_VERSION}<br><br>"
                          "スキャンPDFの同じ位置を全ページ一括マスキングします。<br>"
                          "PyMuPDFのリダクションで画像ピクセル自体を消去するため、"
                          "塗った部分は復元できません。<br><br>"
                          "Copyright © 2026 全力の15%<br>"
                          "zenryoku15@gmail.com<br>"
                          "ライセンス: GNU AGPL-3.0（同梱の LICENSE を参照）<br>"
                          "画面まわりに Qt for Python (PySide6, LGPL-3.0) を使用")

    def _build_sidebar(self) -> QWidget:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setContentsMargins(14, 14, 10, 14)
        v.setSpacing(12)

        # ---- 1 読み込み
        c1, l1 = card("PDFを読み込む", "1")
        drop = QFrame()
        drop.setProperty("kind", "dropzone")
        dl = QVBoxLayout(drop)
        dl.setContentsMargins(12, 16, 12, 16)
        dl.setSpacing(6)
        d1 = QLabel("ここにPDFをドラッグ＆ドロップ")
        d1.setAlignment(Qt.AlignCenter)
        dl.addWidget(d1)
        d2 = QLabel("または")
        d2.setProperty("kind", "hint")
        d2.setAlignment(Qt.AlignCenter)
        dl.addWidget(d2)
        pick = QPushButton("ファイルを選択（複数可）")
        pick.clicked.connect(self.pick_files)
        dl.addWidget(pick, 0, Qt.AlignCenter)
        l1.addWidget(drop)
        self.doc_list = QListWidget()
        self.doc_list.setMaximumHeight(200)
        self.doc_list.currentItemChanged.connect(self._doc_row_selected)
        l1.addWidget(self.doc_list)
        self.clear_docs_btn = QPushButton("すべて取り除く")
        self.clear_docs_btn.setProperty("kind", "link")
        self.clear_docs_btn.clicked.connect(self.clear_docs)
        l1.addWidget(self.clear_docs_btn, 0, Qt.AlignLeft)
        v.addWidget(c1)

        # ---- 2 マスク位置
        c2, l2 = card("マスク位置を指定", "2")
        l2.addWidget(hint(
            "右のページ上をドラッグして矩形を追加（複数可）。<br>"
            "矩形をクリックで選択 → ■ハンドルでサイズ変更・内側ドラッグで移動・Deleteキーで削除<br>"
            "<b>矩形はファイルごとに記録</b>されます（ファイル一覧のクリックで切替）<br>"
            "「コピー」→ファイル切替→「貼り付け」で同じ位置・サイズに複製（⌘C / ⌘V も可）"))
        self.rect_list = QListWidget()
        self.rect_list.setMaximumHeight(170)
        self.rect_list.setMouseTracking(True)
        self.rect_list.currentRowChanged.connect(self._rect_row_selected)
        self.rect_list.itemEntered.connect(self._rect_row_hover)
        l2.addWidget(self.rect_list)
        rb = QHBoxLayout()
        self.rect_copy_btn = QPushButton("矩形をコピー")
        self.rect_copy_btn.setToolTip("選択中の矩形（未選択なら全矩形）をコピー")
        self.rect_copy_btn.clicked.connect(self.copy_rects)
        rb.addWidget(self.rect_copy_btn)
        self.rect_paste_btn = QPushButton("貼り付け")
        self.rect_paste_btn.setToolTip("コピーした矩形をこのファイルの同じ位置に追加")
        self.rect_paste_btn.clicked.connect(self.paste_rects)
        rb.addWidget(self.rect_paste_btn)
        l2.addLayout(rb)
        self.clear_rects_btn = QPushButton("このファイルの矩形をすべて削除")
        self.clear_rects_btn.setProperty("kind", "link")
        self.clear_rects_btn.clicked.connect(self.clear_rects)
        l2.addWidget(self.clear_rects_btn, 0, Qt.AlignLeft)
        v.addWidget(c2)

        # ---- 3 設定
        c3, l3 = card("設定", "3")
        l3.addWidget(hint("ページ範囲・奇数偶数は<b>表示中のファイルごと</b>の設定。色は全ファイル共通。"))
        l3.addWidget(QLabel("塗りつぶし色（全ファイル共通）"))
        cr = QHBoxLayout()
        self.rb_black = QRadioButton("黒")
        self.rb_black.setChecked(True)
        self.rb_white = QRadioButton("白")
        self.color_group = QButtonGroup(self)
        self.color_group.addButton(self.rb_black)
        self.color_group.addButton(self.rb_white)
        self.color_group.buttonToggled.connect(self._color_changed)
        cr.addWidget(self.rb_black)
        cr.addWidget(self.rb_white)
        cr.addStretch(1)
        l3.addLayout(cr)
        l3.addWidget(QLabel("適用ページ（空欄＝全ページ）"))
        self.range_edit = QLineEdit()
        self.range_edit.setPlaceholderText("例: 2-30, 35")
        self.range_edit.textChanged.connect(self._range_changed)
        l3.addWidget(self.range_edit)
        l3.addWidget(QLabel("対象ページの絞り込み"))
        self.parity_box = QComboBox()
        for label, val in (("すべてのページ", "all"),
                           ("奇数ページのみ（両面スキャンの表）", "odd"),
                           ("偶数ページのみ（両面スキャンの裏）", "even")):
            self.parity_box.addItem(label, val)
        self.parity_box.currentIndexChanged.connect(self._parity_changed)
        l3.addWidget(self.parity_box)
        cp = QPushButton("この設定を全ファイルへコピー")
        cp.clicked.connect(self.copy_to_all)
        l3.addWidget(cp)
        v.addWidget(c3)

        # ---- 4 プリセット
        c4, l4 = card("プリセット（設定の保存・再利用）")
        pr = QHBoxLayout()
        self.preset_box = QComboBox()
        pr.addWidget(self.preset_box, 1)
        pdel = QPushButton("削除")
        pdel.clicked.connect(self.preset_delete)
        pr.addWidget(pdel)
        l4.addLayout(pr)
        pr2 = QHBoxLayout()
        b1 = QPushButton("このファイルへ読込")
        b1.clicked.connect(lambda: self.preset_load(False))
        b2 = QPushButton("全ファイルへ読込")
        b2.clicked.connect(lambda: self.preset_load(True))
        pr2.addWidget(b1)
        pr2.addWidget(b2)
        l4.addLayout(pr2)
        pr3 = QHBoxLayout()
        self.preset_name = QLineEdit()
        self.preset_name.setPlaceholderText("プリセット名（例: 調査票の氏名欄）")
        pr3.addWidget(self.preset_name, 1)
        psave = QPushButton("保存")
        psave.clicked.connect(self.preset_save)
        pr3.addWidget(psave)
        l4.addLayout(pr3)
        v.addWidget(c4)

        # ---- 5 実行
        c5, l5 = card("実行", "4")
        self.apply_btn = QPushButton("マスキング実行（読み込んだ全PDF）")
        self.apply_btn.setProperty("kind", "primary")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self.run_masking)
        l5.addWidget(self.apply_btn)
        self.results = QTextBrowser()
        self.results.setMinimumHeight(170)
        self.results.setOpenExternalLinks(False)
        l5.addWidget(self.results)
        oo = QPushButton("出力フォルダ（output）を開く")
        oo.setProperty("kind", "link")
        oo.setToolTip(str(masking.OUTPUT_DIR))
        oo.clicked.connect(lambda: masking.open_folder(masking.OUTPUT_DIR))
        l5.addWidget(oo, 0, Qt.AlignLeft)
        v.addWidget(c5)

        # ---- 6 検査
        c6, l6 = card("検査（できあがったPDFを調べる）", "5")
        l6.addWidget(hint("実行時にも自動で検査していますが、あとからフォルダごと調べ直せます。"))
        chk = QPushButton("マスク済みPDFを検査する…")
        chk.clicked.connect(self.open_check)
        l6.addWidget(chk)
        v.addWidget(c6)

        v.addStretch(1)
        area.setWidget(inner)
        area.setMinimumWidth(360)
        self.sidebar = area
        return area

    def _build_viewer(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QFrame()
        bar.setObjectName("toolbar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 8, 14, 8)
        h.setSpacing(10)
        h.addWidget(QLabel("表示・編集中のPDF:"))
        self.ref_box = QComboBox()
        self.ref_box.setMinimumWidth(240)
        self.ref_box.activated.connect(self._ref_box_changed)
        h.addWidget(self.ref_box, 1)
        self.prev_btn = QPushButton("◀")
        self.prev_btn.setFixedWidth(38)
        self.prev_btn.clicked.connect(lambda: self.step_page(-1))
        h.addWidget(self.prev_btn)
        self.page_lb = QLabel("– / –")
        self.page_lb.setMinimumWidth(72)
        self.page_lb.setAlignment(Qt.AlignCenter)
        h.addWidget(self.page_lb)
        self.next_btn = QPushButton("▶")
        self.next_btn.setFixedWidth(38)
        self.next_btn.clicked.connect(lambda: self.step_page(1))
        h.addWidget(self.next_btn)
        self.preview_cb = QCheckBox("マスク後プレビュー")
        self.preview_cb.toggled.connect(self._preview_toggled)
        h.addWidget(self.preview_cb)
        v.addWidget(bar)

        self.canvas = PageCanvas()
        self.canvas.rectsChanged.connect(self.after_rects_change)
        self.canvas.selectionChanged.connect(self.render_rect_list)
        self.canvas.notify.connect(self.toast)
        self.canvas.pageStep.connect(self.step_page)
        v.addWidget(self.canvas, 1)

        # ⌘C / ⌘V はページ表示に触れているときだけ効かせる
        # （文字入力欄での通常のコピー＆ペーストを奪わないため）
        sc_c = QShortcut(QKeySequence.Copy, self.canvas)
        sc_c.setContext(Qt.WidgetWithChildrenShortcut)
        sc_c.activated.connect(self.copy_rects)
        sc_v = QShortcut(QKeySequence.Paste, self.canvas)
        sc_v.setContext(Qt.WidgetWithChildrenShortcut)
        sc_v.activated.connect(self.paste_rects)
        return w

    # ============================================================ 状態
    def find(self, doc_id) -> DocEntry | None:
        return next((e for e in self.entries if e.doc_id == doc_id), None)

    def current(self) -> DocEntry | None:
        return self.find(self.ref_id) if self.ref_id is not None else None

    def _color_changed(self, *_):
        if hasattr(self, "canvas"):
            self.canvas.set_mask_color(self.mask_color())

    def mask_color(self) -> str:
        return "black" if self.rb_black.isChecked() else "white"

    # ============================================================ PDFの読み込み
    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "PDFを選ぶ（複数可）", str(Path.home()), "PDF (*.pdf)")
        if paths:
            self.add_files([Path(p) for p in paths])

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [Path(u.toLocalFile()) for u in ev.mimeData().urls() if u.isLocalFile()]
        paths = [p for p in paths if p.suffix.lower() == ".pdf"]
        if paths:
            self.add_files(paths)
        else:
            self.toast("PDFファイルをドロップしてください", True)

    def add_files(self, paths: list[Path]):
        added, errors = 0, []
        for p in paths:
            try:
                with fitz.open(p) as d:
                    n = d.page_count
                if n == 0:
                    raise ValueError("0ページ")
            except Exception:
                errors.append(p.name)
                continue
            self.entries.append(DocEntry(self._next_id, p, p.name, n,
                                         stamp=DocEntry.stamp_of(p)))
            self._next_id += 1
            added += 1
        if self.ref_id is None and self.entries:
            self.ref_id = self.entries[0].doc_id
            self.page = 1
        self.sync_inputs_from_cur()
        self.render_all()
        self.load_page()
        if errors:
            self.toast(f"読み込めないファイル: {'、'.join(errors)}", True)
        elif added:
            self.toast(f"{added} ファイルを読み込みました")

    def remove_doc(self, doc_id):
        e = self.find(doc_id)
        if e is None:
            return
        e.close()
        self.entries.remove(e)
        if self.ref_id == doc_id:
            self.ref_id = self.entries[0].doc_id if self.entries else None
            self.page = 1
            self.canvas.set_selected(-1)
        self.sync_inputs_from_cur()
        self.render_all()
        self.load_page()

    def clear_docs(self):
        for e in self.entries:
            e.close()
        self.entries.clear()
        self.ref_id = None
        self.page = 1
        self.canvas.set_selected(-1)
        self.sync_inputs_from_cur()
        self.render_all()
        self.load_page()

    def switch_doc(self, doc_id):
        if doc_id == self.ref_id:
            return
        prev = self.current()
        if prev is not None:
            prev.close()          # 表示に使っていた写しを手放す（覚え続けない）
        self.ref_id = doc_id
        self.page = 1
        self.canvas.set_selected(-1)
        self.canvas.set_hover(-1)
        self.sync_inputs_from_cur()
        self.render_all()
        self.load_page()

    def _doc_row_selected(self, cur, _prev):
        if self._syncing or cur is None:
            return
        self.switch_doc(cur.data(Qt.UserRole))

    def _ref_box_changed(self, idx):
        if self._syncing:
            return
        doc_id = self.ref_box.itemData(idx)
        if doc_id is not None:
            self.switch_doc(doc_id)

    # ============================================================ 表示の更新
    _syncing = False

    def render_all(self):
        self.render_doc_list()
        self.render_rect_list()
        self.render_toolbar()
        self.update_apply_button()

    def after_rects_change(self):
        self.render_doc_list()
        self.render_rect_list()
        self.update_apply_button()

    def render_doc_list(self):
        self._syncing = True
        self.doc_list.clear()
        for e in self.entries:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(8, 5, 6, 5)
            h.setSpacing(6)
            name = ElidedLabel(e.name)
            h.addWidget(name, 1)
            n = len(e.rects)
            badge = QLabel(f"矩形{n}" if n else "未設定")
            badge.setProperty("kind", "badge" if n else "badgeZero")
            h.addWidget(badge)
            pg = QLabel(f"{e.pages}p")
            pg.setProperty("kind", "hint")
            h.addWidget(pg)
            rm = small_btn("✕", "一覧から取り除く（ファイル自体は削除されません）")
            rm.clicked.connect(lambda _=False, i=e.doc_id: self.remove_doc(i))
            h.addWidget(rm)
            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            item.setData(Qt.UserRole, e.doc_id)
            item.setToolTip("クリックで表示・編集対象にする")
            self.doc_list.addItem(item)
            self.doc_list.setItemWidget(item, row)
            if e.doc_id == self.ref_id:
                self.doc_list.setCurrentItem(item)
        self.clear_docs_btn.setVisible(bool(self.entries))
        fit_list(self.doc_list, 200)
        self._syncing = False

    def render_rect_list(self):
        self._syncing = True
        self.rect_list.clear()
        e = self.current()
        rects = e.rects if e else []
        for i, r in enumerate(rects):
            row = HoverRow()
            row.hovered.connect(lambda on, k=i: self.canvas.set_hover(k if on else -1))
            h = QHBoxLayout(row)
            h.setContentsMargins(8, 4, 6, 4)
            h.setSpacing(6)
            sw = QLabel()
            sw.setFixedSize(QSize(12, 12))
            sw.setProperty("kind", "swatch")
            sw.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            h.addWidget(sw)
            w = (r["x1"] - r["x0"]) * 100
            ht = (r["y1"] - r["y0"]) * 100
            lb = QLabel(f"矩形{i + 1}（幅{w:.1f}% × 高さ{ht:.1f}%）")
            lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)   # 行に通す
            h.addWidget(lb, 1)
            rm = small_btn("✕", "この矩形を削除")
            rm.clicked.connect(lambda _=False, k=i: self.delete_rect(k))
            h.addWidget(rm)
            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            self.rect_list.addItem(item)
            self.rect_list.setItemWidget(item, row)
        if 0 <= self.canvas.selected < len(rects):
            self.rect_list.setCurrentRow(self.canvas.selected)
        else:
            self.rect_list.setCurrentRow(-1)
        self.clear_rects_btn.setVisible(bool(rects))
        fit_list(self.rect_list, 170)
        self._syncing = False

    def render_toolbar(self):
        self._syncing = True
        self.ref_box.clear()
        for e in self.entries:
            self.ref_box.addItem(f"{e.name}（{e.pages}ページ）", e.doc_id)
        e = self.current()
        if e:
            self.ref_box.setCurrentIndex(self.ref_box.findData(e.doc_id))
        self.page_lb.setText(f"{self.page} / {e.pages}" if e else "– / –")
        self.prev_btn.setEnabled(bool(e) and self.page > 1)
        self.next_btn.setEnabled(bool(e) and self.page < e.pages)
        self._syncing = False

    def update_apply_button(self):
        self.apply_btn.setEnabled(any(e.rects for e in self.entries))

    def sync_inputs_from_cur(self):
        self._syncing = True
        e = self.current()
        self.range_edit.setText(e.page_range if e else "")
        idx = self.parity_box.findData(e.parity if e else "all")
        self.parity_box.setCurrentIndex(max(0, idx))
        self.range_edit.setEnabled(e is not None)
        self.parity_box.setEnabled(e is not None)
        self.canvas.set_rects(e.rects if e else [])
        self._syncing = False

    def _range_changed(self, text):
        if self._syncing:
            return
        e = self.current()
        if e:
            e.page_range = text

    def _parity_changed(self, _idx):
        if self._syncing:
            return
        e = self.current()
        if e:
            e.parity = self.parity_box.currentData()

    # ============================================================ ページ表示
    def load_page(self):
        e = self.current()
        if e is None:
            self.canvas.set_page(None)
            self.canvas.set_rects([])
            return
        self.page = max(1, min(self.page, e.pages))
        try:
            page = e.document()[self.page - 1]
            zoom = masking.RENDER_DPI / 72
            if page.rect.width * zoom > masking.MAX_RENDER_WIDTH:
                zoom = masking.MAX_RENDER_WIDTH / page.rect.width
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888)
            self.canvas.set_page(QPixmap.fromImage(img.copy()))  # copy: 元の領域が消えても平気にする
        except Exception as ex:
            self.canvas.set_page(None)
            self.toast(f"ページを表示できません: {ex}", True)
        self.canvas.set_rects(e.rects)
        self.render_toolbar()

    def step_page(self, delta: int):
        e = self.current()
        if not e:
            return
        n = max(1, min(e.pages, self.page + delta))
        if n != self.page:
            self.page = n
            self.load_page()

    def _preview_toggled(self, on):
        self.canvas.set_preview(on)

    # ============================================================ 矩形の操作
    def _rect_row_selected(self, row):
        if self._syncing:
            return
        self.canvas.set_selected(row)

    def _rect_row_hover(self, item):
        self.canvas.set_hover(self.rect_list.row(item))

    def eventFilter(self, obj, ev):
        if obj is self.rect_list.viewport() and ev.type() == ev.Type.Leave:
            self.canvas.set_hover(-1)
        elif ev.type() == QEvent.KeyPress and self._global_key(ev):
            return True
        return super().eventFilter(obj, ev)

    def _global_key(self, ev) -> bool:
        """画面のどこを触っていてもキーが効くようにする。

        旧版はブラウザ全体でキーを拾い、文字の入力欄とプルダウンにいるときだけ
        素通りさせていた。新版は受け皿がページ表示部だけだったため、サイドバーの
        ボタンや一覧を一度触ると Delete・←→・Esc・⌘C・⌘V が丸ごと効かなくなり、
        画面に書いてある「Deleteキーで削除」が嘘になっていた（2026-08-21に実測）。
        """
        if QApplication.activeWindow() is not self:
            return False                      # 別の窓が前にいるときは触らない
        w = QApplication.focusWidget()
        if isinstance(w, (QLineEdit, QComboBox)):
            return False                      # 文字を打っている最中は奪わない
        k = ev.key()
        if ev.matches(QKeySequence.Copy):
            # 画面の文字を選んでいるときは、そちらのコピーを優先する（旧版と同じ）
            if isinstance(w, QTextBrowser) and w.textCursor().hasSelection():
                return False
            self.copy_rects()
        elif ev.matches(QKeySequence.Paste):
            self.paste_rects()
        elif k == Qt.Key_Left:
            self.step_page(-1)
        elif k == Qt.Key_Right:
            self.step_page(1)
        elif k in (Qt.Key_Delete, Qt.Key_Backspace) and self.canvas.selected >= 0:
            self.canvas.delete_selected()
        elif k == Qt.Key_Escape and self.canvas.selected >= 0:
            self.canvas.selected = -1
            self.canvas.selectionChanged.emit()
            self.canvas.update()
        else:
            return False
        return True

    def delete_rect(self, i):
        e = self.current()
        if not e or not (0 <= i < len(e.rects)):
            return
        e.rects.pop(i)
        if self.canvas.selected == i:
            self.canvas.set_selected(-1)
        elif self.canvas.selected > i:
            self.canvas.set_selected(self.canvas.selected - 1)
        self.after_rects_change()
        self.canvas.update()

    def clear_rects(self):
        e = self.current()
        if not e:
            return
        e.rects.clear()
        self.canvas.set_selected(-1)
        self.after_rects_change()
        self.canvas.update()

    def update_paste_button(self):
        n = len(self.clipboard["rects"]) if self.clipboard else 0
        self.rect_paste_btn.setEnabled(bool(n))
        self.rect_paste_btn.setText(f"貼り付け（{n}件）" if n else "貼り付け")

    def copy_rects(self):
        e = self.current()
        if not e or not e.rects:
            self.toast("コピーする矩形がありません", True)
            return
        i = self.canvas.selected
        one = 0 <= i < len(e.rects)
        src = [e.rects[i]] if one else e.rects
        self.clipboard = {"rects": [dict(r) for r in src], "source_id": e.doc_id}
        self.update_paste_button()
        self.toast(
            f"選択中の矩形{i + 1} をコピーしました（別ファイルへ切り替えて貼り付け）" if one
            else f"このファイルの矩形{len(src)}件をコピーしました（別ファイルへ切り替えて貼り付け）")

    def paste_rects(self):
        e = self.current()
        if not e:
            self.toast("貼り付け先のファイルがありません", True)
            return
        if not self.clipboard or not self.clipboard["rects"]:
            self.toast("コピーされた矩形がありません（先に「コピー」してください）", True)
            return
        # 同じファイルへの貼り付けは複製として少しずらす（重なり防止）
        same = self.clipboard["source_id"] == e.doc_id
        OFF = 0.02
        for r in self.clipboard["rects"]:
            w, h = r["x1"] - r["x0"], r["y1"] - r["y0"]
            x0, y0 = r["x0"], r["y0"]
            if same:
                x0 = min(max(0.0, x0 + OFF), 1.0 - w)
                y0 = min(max(0.0, y0 + OFF), 1.0 - h)
            e.rects.append({"x0": x0, "y0": y0, "x1": x0 + w, "y1": y0 + h})
        self.canvas.set_selected(len(e.rects) - 1)
        self.after_rects_change()
        self.canvas.update()
        n = len(self.clipboard["rects"])
        self.toast(f"矩形{n}件を複製しました（少しずらして配置）" if same
                   else f"矩形{n}件を同じ位置に貼り付けました")

    def copy_to_all(self):
        e = self.current()
        if not e:
            self.toast("コピー元のファイルがありません", True)
            return
        others = [d for d in self.entries if d.doc_id != e.doc_id]
        if not others:
            self.toast("コピー先のファイルがありません（読み込みが1ファイルのみ）", True)
            return
        ans = QMessageBox.question(
            self, "設定を全ファイルへコピー",
            f"「{e.name}」の設定（矩形{len(e.rects)}件・ページ範囲・奇数偶数）を\n"
            f"他の {len(others)} ファイルへコピーします。\n"
            "各ファイルの既存の設定は置き換えられます。よろしいですか？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        for d in others:
            d.rects = [dict(r) for r in e.rects]
            d.page_range = e.page_range
            d.parity = e.parity
        self.after_rects_change()
        self.toast(f"{len(others)} ファイルへ設定をコピーしました")

    # ============================================================ プリセット
    def refresh_presets(self, select: str | None = None):
        self._syncing = True
        self.preset_box.clear()
        self.preset_box.addItem("（保存済みプリセット）", None)
        for p in masking.list_presets():
            self.preset_box.addItem(
                f"{p['name']}（矩形{len(p.get('rects', []))}件・{p.get('saved', '')}）", p)
        if select:
            for i in range(self.preset_box.count()):
                d = self.preset_box.itemData(i)
                if d and d.get("name") == select:
                    self.preset_box.setCurrentIndex(i)
                    break
        self._syncing = False

    def _selected_preset(self):
        p = self.preset_box.currentData()
        if not p:
            self.toast("読み込むプリセットを選択してください", True)
            return None
        return p

    def preset_save(self):
        name = self.preset_name.text().strip()
        e = self.current()
        if not name:
            self.toast("プリセット名を入力してください", True)
            return
        if not e or not e.rects:
            self.toast("保存する矩形がありません（表示中のファイルに矩形を作成してください）", True)
            return
        try:
            saved = masking.save_preset(name, e.rects, self.mask_color(),
                                        e.page_range, e.parity)
        except Exception as ex:
            self.toast(str(ex), True)
            return
        self.preset_name.clear()
        self.refresh_presets(saved)
        self.toast(f"プリセット「{saved}」を保存しました")

    def preset_load(self, to_all: bool):
        p = self._selected_preset()
        if not p:
            return
        if not self.entries:
            self.toast("PDFを読み込んでから実行してください", True)
            return
        targets = self.entries if to_all else [self.current()]
        for e in targets:
            if e is None:
                continue
            e.rects = [dict(r) for r in p.get("rects", [])]
            e.page_range = p.get("range", "")
            e.parity = p.get("parity", "all")
        if p.get("color", "black") == "white":
            self.rb_white.setChecked(True)
        else:
            self.rb_black.setChecked(True)
        self.canvas.set_selected(-1)
        self.sync_inputs_from_cur()
        self.after_rects_change()
        self.canvas.update()
        self.toast(f"プリセット「{p['name']}」を全 {len(self.entries)} ファイルへ読み込みました"
                   if to_all else f"プリセット「{p['name']}」を表示中のファイルへ読み込みました")

    def preset_delete(self):
        p = self.preset_box.currentData()
        if not p:
            self.toast("削除するプリセットを選択してください", True)
            return
        ans = QMessageBox.question(
            self, "プリセットの削除",
            f"プリセット「{p['name']}」を削除しますか？\n（trash フォルダに退避されます）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        masking.delete_preset(p["name"])
        self.refresh_presets()
        self.toast("削除しました（trash/ に退避）")

    # ============================================================ 実行
    def run_masking(self):
        # 走っている最中に呼ばれたら何もしない。進捗の窓が閉じられた場合の保険
        if self._mask_worker is not None and self._mask_worker.isRunning():
            return
        # 矩形は「写し」を渡す。画面の矩形そのものを渡すと、処理の最中に
        # 動かした結果が、まだ塗っていないファイルへそのまま効いてしまう。
        # 検査も同じ動いた矩形を見るので「✓ 検査済み」のまま氏名が残る
        # （2026-08-21に実機で再現）。旧版は押した瞬間にJSONへ固めていた
        jobs = [{"name": e.name, "path": e.path,
                 "rects": [dict(r) for r in e.rects],
                 "range": e.page_range, "parity": e.parity, "pages": e.pages,
                 "stamp": e.stamp}
                for e in self.entries]
        if not jobs:
            self.toast("PDFが読み込まれていません", True)
            return
        self.results.clear()
        self.apply_btn.setEnabled(False)
        self.apply_btn.setText("処理中…")
        self._progress = BusyDialog("マスキング中…", len(jobs), self)
        self._mask_worker = MaskWorker(jobs, self.mask_color())
        self._mask_worker.progress.connect(self._mask_progress)
        self._mask_worker.done.connect(self._mask_done)
        self._mask_worker.start()

    def _mask_progress(self, i, n, name):
        self._progress.setLabelText(f"マスキング中… {i}/{n}\n{name}")
        self._progress.setValue(i - 1)

    def _mask_done(self, results):
        self._progress.setValue(self._progress.maximum())
        self._progress.allow_close()
        self._progress.close()
        self.apply_btn.setText("マスキング実行（読み込んだ全PDF）")
        self.update_apply_button()
        self.results.setHtml(self._results_html(results))
        # 結果はサイドバーの下のほうにある。実行直後に見えていないと
        # 「消え残りを検出」を見落とすので、そこまで自動で送る
        self.sidebar.ensureWidgetVisible(self.results, 0, 180)
        ng = sum(1 for r in results if r.get("ok") and (r.get("check") or {}).get("verdict") == "ng")
        ng += sum(1 for r in results if r.get("ok") is False)
        warn = sum(1 for r in results if r.get("ok") and (r.get("check") or {}).get("verdict") == "warn")
        ok = sum(1 for r in results if r.get("ok") and (r.get("check") or {}).get("verdict") == "ok")
        skip = sum(1 for r in results if r.get("skipped"))
        if ng:
            self.toast(f"消え残りを検出: {ng} ファイル。結果欄を確認してください", True)
        else:
            self.toast(f"完了: {ok} ファイルを検査まで通しました"
                       + (f"（要確認 {warn} 件）" if warn else "")
                       + (f"（矩形未設定 {skip} 件はスキップ）" if skip else ""))

    @staticmethod
    def _results_html(results) -> str:
        """ブラウザ版の結果欄と同じ内容・同じ言い回しで書き出す。"""
        rows, any_done = [], False
        for r in results:
            if r.get("skipped"):
                rows.append(f'<div style="color:#7b8794;margin:3px 0">'
                            f'− {esc(r["name"])}: 矩形未設定のためスキップ</div>')
            elif r.get("ok"):
                any_done = True
                c = r.get("check") or {}
                out = (f'<div style="color:#7b8794;margin-left:1.1em">'
                       f'→ output/{esc(r["output"])}</div>')
                reasons = c.get("reasons") or []
                detail = ("".join(f'<div style="color:#a16207;margin-left:1.1em">{esc(x)}</div>'
                                  for x in reasons) if reasons else "")
                v = c.get("verdict")
                if v == "ng":
                    rows.append(f'<div style="color:#b91c1c;margin:3px 0">✕ {esc(r["name"])}'
                                f'（{r["pagesMasked"]}ページ処理・<b>検査で消え残りを検出</b>）'
                                f'</div>{out}{detail}')
                elif v == "warn":
                    rows.append(f'<div style="color:#a16207;margin:3px 0">⚠ {esc(r["name"])}'
                                f'（{r["pagesMasked"]}ページ処理・要確認）</div>{out}{detail}')
                else:
                    rows.append(f'<div style="color:#0b7a44;margin:3px 0">✓ {esc(r["name"])}'
                                f'（{r["pagesMasked"]}ページ・検査済み）</div>{out}')
            else:
                any_done = True
                rows.append(f'<div style="color:#b91c1c;margin:3px 0">'
                            f'✕ {esc(r["name"])}: {esc(r.get("error", ""))}</div>')
        if any_done:
            # 機械が見ているのは「指定した範囲の中が消えたか」だけ。範囲の外にある
            # 氏名（別欄の保護者署名など）や、そもそも置き場所を間違えた矩形は
            # 判定できない。ここを黙っていると「✓＝安全」と誤解される
            rows.append(
                '<div style="margin-top:8px;padding:8px;background:#fff7ed;'
                'border:1px solid #fed7aa;color:#7c2d12">'
                '⚠ 検査は<b>指定した範囲の中だけ</b>を見ています。押さえるべき欄がすべて'
                '入っているかは、できあがったPDFを目で確かめてください。<br>'
                '⚠ マスク前の元ファイルはこのツールでは移動・削除されません。'
                '所定の場所への隔離、または不要なら削除を忘れずに。</div>')
        return f'<div style="font-size:12px;line-height:1.5">{"".join(rows)}</div>'

    def open_check(self):
        CheckDialog(self).exec()

    # ============================================================ 通知
    def toast(self, msg: str, error: bool = False):
        self._toast.setText(msg)
        self._toast.setProperty("err", "1" if error else "0")
        self._toast.style().unpolish(self._toast)
        self._toast.style().polish(self._toast)
        self._toast.adjustSize()
        self._place_toast()
        self._toast.show()
        self._toast.raise_()
        self._toast_timer.start(3500)

    def _place_toast(self):
        p = self.centralWidget()
        w = self._toast.width()
        self._toast.move(max(10, (p.width() - w) // 2), p.height() - self._toast.height() - 22)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._toast.isVisible():
            self._place_toast()

    def showEvent(self, ev):
        super().showEvent(ev)
        self.rect_list.viewport().installEventFilter(self)
        QApplication.instance().installEventFilter(self)

    def closeEvent(self, ev):
        if self._mask_worker is not None and self._mask_worker.isRunning():
            QMessageBox.information(self, APP_NAME,
                                    "マスキング処理中です。終わるまでお待ちください。")
            ev.ignore()
            return
        for e in self.entries:
            e.close()
        super().closeEvent(ev)


# ---------------------------------------------------------------- 見た目
STYLE = f"""
QWidget {{ color: {INK}; font-size: 13px; }}
#root, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {BG}; }}
#appHeader {{ background: #ffffff; border-bottom: 1px solid {LINE}; }}
#appTitle {{ font-size: 19px; font-weight: 700; letter-spacing: 1px; }}
#toolbar {{ background: #ffffff; border-bottom: 1px solid {LINE}; }}
QFrame[kind="card"] {{ background: #ffffff; border: 1px solid {LINE}; border-radius: 10px; }}
QFrame[kind="dropzone"] {{ background: #f8fafc; border: 2px dashed #b8c1cc; border-radius: 9px; }}
QLabel[kind="cardTitle"] {{ font-size: 14px; font-weight: 700; }}
QLabel[kind="step"] {{ background: {BLUE.name()}; color: #ffffff; border-radius: 10px;
                       font-weight: 700; font-size: 12px; }}
QLabel[kind="hint"] {{ color: {MUTED}; font-size: 12px; }}
QLabel[kind="badge"] {{ background: #e0edff; color: #1d4ed8; border-radius: 8px;
                        padding: 1px 7px; font-size: 11px; font-weight: 700; }}
QLabel[kind="badgeZero"] {{ background: #eef1f5; color: #8b95a1; border-radius: 8px;
                            padding: 1px 7px; font-size: 11px; }}
QLabel[kind="swatch"] {{ background: rgba(220,38,38,.35); border: 1px solid {RED.name()};
                         border-radius: 3px; }}
QPushButton {{ background: #ffffff; border: 1px solid #cbd2d9; border-radius: 6px;
               padding: 6px 11px; }}
QPushButton:hover {{ background: #f0f4f8; }}
QPushButton:pressed {{ background: #e4e9ef; }}
QPushButton:disabled {{ color: #a7b0ba; background: #f6f8fa; border-color: #e1e6eb; }}
QPushButton[kind="primary"] {{ background: {BLUE.name()}; color: #ffffff;
                               border: 1px solid #1d4ed8; font-weight: 700; padding: 10px 12px; }}
QPushButton[kind="primary"]:hover {{ background: #1d4ed8; }}
QPushButton[kind="primary"]:disabled {{ background: #c3cbd5; border-color: #c3cbd5; color: #ffffff; }}
QPushButton[kind="icon"] {{ border: none; background: transparent; color: #9aa5b1; padding: 0; }}
QPushButton[kind="icon"]:hover {{ color: #b91c1c; background: #fdecec; border-radius: 11px; }}
QPushButton[kind="link"] {{ border: none; background: transparent; color: {BLUE.name()};
                            padding: 2px 0; text-align: left; }}
QPushButton[kind="link"]:hover {{ color: #1d4ed8; text-decoration: underline; }}
QLineEdit, QComboBox {{ background: #ffffff; border: 1px solid #cbd2d9; border-radius: 6px;
                        padding: 5px 7px; }}
QLineEdit:focus, QComboBox:focus {{ border-color: {BLUE.name()}; }}
QLineEdit:disabled {{ background: #f6f8fa; color: #a7b0ba; }}
QListWidget, QTextBrowser, QTreeWidget {{ background: #fbfcfd; border: 1px solid #e2e6ec;
                                          border-radius: 8px; }}
QListWidget::item {{ border-bottom: 1px solid #eef1f5; }}
QListWidget::item:selected {{ background: #e6efff; color: {INK}; }}
QTreeWidget::item {{ padding: 2px 0; }}
QHeaderView::section {{ background: #f1f4f8; border: none; border-right: 1px solid #e2e6ec;
                        border-bottom: 1px solid #e2e6ec; padding: 4px 6px; font-weight: 700; }}
QSplitter::handle {{ background: {LINE}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
#toast {{ background: #1f2933; color: #ffffff; border-radius: 8px; padding: 10px 18px; }}
#toast[err="1"] {{ background: #b91c1c; }}
/* ラジオの印には手を出さない。Fusionが描くものが自然で、自前で描くと丸が四角くなる。
   チェック欄だけは、白い下地の上で枠がほとんど見えないので指定する（実機で確認）。 */
QRadioButton, QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid #98a3b0;
                        border-radius: 3px; background: #ffffff; }}
QCheckBox::indicator:hover {{ border-color: {BLUE.name()}; }}
QCheckBox::indicator:checked {{ border-color: {BLUE.name()}; background: {BLUE.name()};
                                image: url("{CHECK_PNG}"); }}
QCheckBox::indicator:disabled {{ border-color: #d3d9e0; background: #f6f8fa; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #c3cbd5; border-radius: 5px; min-height: 26px; }}
QScrollBar::handle:vertical:hover {{ background: #a7b0ba; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


def light_palette() -> QPalette:
    """明るい配色に固定する。

    macOSやWindowsのダークモードでは、色を指定していない部品（ラジオ・チェックの印
    など）だけが暗い配色のまま描かれ、明るい下地の上で黒く潰れる。この道具の画面は
    白い紙を見るためのものなので、OSの設定によらず明るい側に揃える。
    """
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(BG))
    pal.setColor(QPalette.WindowText, QColor(INK))
    pal.setColor(QPalette.Base, QColor("#ffffff"))
    pal.setColor(QPalette.AlternateBase, QColor("#f4f6f9"))
    pal.setColor(QPalette.Text, QColor(INK))
    pal.setColor(QPalette.PlaceholderText, QColor("#a7b0ba"))
    pal.setColor(QPalette.Button, QColor("#ffffff"))
    pal.setColor(QPalette.ButtonText, QColor(INK))
    pal.setColor(QPalette.ToolTipBase, QColor("#ffffff"))
    pal.setColor(QPalette.ToolTipText, QColor(INK))
    pal.setColor(QPalette.Highlight, BLUE)
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.Link, BLUE)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor("#a7b0ba"))
    return pal


def main() -> int:
    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationDisplayName(APP_NAME)
    QApplication.setOrganizationName("zenryoku15pct")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setPalette(light_palette())
    app.setStyleSheet(STYLE)
    # Dock・タスクバー・窓のアイコン（実行ファイル版では同梱先から読む）
    icon = masking.RES_DIR / "assets" / "icon.png"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    # console=False にしたので、起動に失敗しても画面には何も出ない。
    # 「ダブルクリックしても反応しない」で終わらせず、理由を必ず見せる
    try:
        win = MainWindow()
    except Exception:
        import traceback
        detail = traceback.format_exc()
        try:
            (masking.BASE_DIR / "起動エラー.txt").write_text(detail, encoding="utf-8")
        except Exception:
            pass
        QMessageBox.critical(
            None, f"{APP_NAME}を起動できません",
            f"{APP_NAME}の起動に失敗しました。\n\n"
            f"作業フォルダ: {masking.BASE_DIR}\n\n"
            "アプリを書き込めない場所（アプリケーションフォルダなど）に置いている場合は、"
            "書類フォルダなどへ移してからもう一度お試しください。\n\n"
            f"詳しい内容:\n{detail[-1200:]}")
        return 1
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
