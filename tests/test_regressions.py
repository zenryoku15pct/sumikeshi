#!/usr/bin/env python3
"""v2.0.0 の移植で開いた穴を、事故の形のまま試験にしたもの。

    QT_QPA_PLATFORM=offscreen python3 tests/test_regressions.py

2026-08-21 の監査で、旧ブラウザ版には無かった事故が9件見つかった。うち6件は
**氏名が読めるPDFが「✓ 検査済み」で出てくる**という、この道具が唯一防ぎたい形。
どれも「順番が乱れたとき」にだけ起きるので、素直な順番しか試さない
test_gui.py は全項目を通してしまっていた。ここでは順番を乱して当てる。

原因A 処理の最中に矩形が書き換わる
原因B 読み込んだ後に原本が差し替わっても気づかない
原因C マウスの掴みが外から切られると矩形が引きずられる
原因D 置き場所によっては窓が出ないまま終わる
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

import fitz

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.argv = ["gui.py"]

from PySide6.QtCore import QEvent, QPointF, Qt          # noqa: E402
from PySide6.QtGui import QMouseEvent                    # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402

import gui        # noqa: E402
import masking    # noqa: E402
from make_dummy_sample import NAME_BOX                   # noqa: E402

app = QApplication([])
app.setStyle("Fusion")
app.setPalette(gui.light_palette())
app.setStyleSheet(gui.STYLE)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

SRC = BASE / "samples/ダミー調査票.pdf"
if not SRC.exists():
    print(f"素材がありません: {SRC}\n  python3 make_dummy_sample.py で作れます")
    raise SystemExit(2)

TMP = Path(tempfile.mkdtemp(prefix="sumikeshi-regress-"))
masking.BASE_DIR = TMP
masking.OUTPUT_DIR = TMP / "output"
masking.PRESET_DIR = TMP / "presets"
masking.TRASH_DIR = TMP / "trash"

fails = []


def check(label, cond, extra=""):
    print(("  OK  " if cond else "  NG  ") + label + (f"\n        {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(label)


def pump(n=6):
    for _ in range(n):
        app.processEvents()


def wait_worker(w, limit=1200):
    for _ in range(limit):
        app.processEvents()
        if w is not None and not w.isRunning():
            return True
        app.thread().msleep(20)
    return False


def name_box_range(pdf: Path, pno: int) -> int:
    """出力PDFの氏名欄の濃淡の幅。0なら塗られている（＝消えている）。"""
    with fitz.open(pdf) as d:
        page = d[pno - 1]
        pr = page.rect
        clip = fitz.Rect(pr.x0 + NAME_BOX["x0"] * pr.width + 4,
                         pr.y0 + NAME_BOX["y0"] * pr.height + 4,
                         pr.x0 + NAME_BOX["x1"] * pr.width - 4,
                         pr.y0 + NAME_BOX["y1"] * pr.height - 4)
        pix = page.get_pixmap(clip=clip, dpi=100, colorspace=fitz.csGRAY)
        return max(pix.samples) - min(pix.samples)


def fresh_output():
    masking.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for f in masking.OUTPUT_DIR.glob("*.pdf"):
        f.unlink()


# ================================================================ 原因A
print("\n【原因A】処理の最中に矩形を動かしても、順番待ちのファイルに効かない")
win = gui.MainWindow()
win.resize(1200, 820)
win.show()
pump()

work = TMP / "A"
work.mkdir(exist_ok=True)
files = []
for n in ("甲", "乙", "丙", "丁"):
    f = work / f"{n}_答案.pdf"
    shutil.copy(SRC, f)
    files.append(f)
win.add_files(files)
pump()
for e in win.entries:
    e.rects = [dict(NAME_BOX)]
win.after_rects_change()
fresh_output()

# ダミーPDFは4ページと小さく、実際の処理そのものが一瞬で終わる。
# 「処理中であること」を確実に試すため、この試験の間だけ1ファイルごとに
# わずかな間を置く（実処理の中身は変えない・待つだけ）
_real_mask_one_file = masking.mask_one_file
def _slow_mask_one_file(*a, **kw):
    time.sleep(0.15)
    return _real_mask_one_file(*a, **kw)
masking.mask_one_file = _slow_mask_one_file
try:
    win.run_masking()
    # 二度押しの検証は、間に pump() を挟まず「1回目を呼んだ直後」に行う
    w_before = win._mask_worker
    win.run_masking()                      # 走行中の二度押し（のつもり）
    check("処理中に実行を呼んでも二重に走らない（裏方が入れ替わらない）",
          win._mask_worker is w_before)
    pump(2)
    check("この時点でまだ処理中である（試験の前提の確認）",
          win._mask_worker is not None and win._mask_worker.isRunning())
    jobs = win._mask_worker.jobs
    # 処理が走っている最中に、画面の矩形を氏名欄から遠くへ動かす
    for e in win.entries:
        e.rects[0].update({"x0": .05, "y0": .80, "x1": .30, "y1": .88})
    check("裏方へ渡した矩形は写しで、画面をいじっても変わらない",
          all(abs(j["rects"][0]["x0"] - NAME_BOX["x0"]) < 1e-9 for j in jobs),
          str(jobs[0]["rects"][0]))

    prog = win._progress
    prog.reject()                       # Escキーと同じ経路
    prog.close()                        # ×ボタンと同じ経路
    pump()
    check("進捗の窓はEscでも×でも閉じない", prog.isVisible(), f"visible={prog.isVisible()}")

    check("処理が終わるまで待てた", wait_worker(win._mask_worker))
finally:
    masking.mask_one_file = _real_mask_one_file
pump(8)
outs = sorted(masking.OUTPUT_DIR.glob("*_masked.pdf"))
check("4本とも書き出された", len(outs) == 4, str([p.name for p in outs]))
worst = max((name_box_range(p, 1) for p in outs), default=999)
check("全ファイルの氏名欄が塗られている（動かした後の位置ではない）", worst == 0,
      f"濃淡の幅の最大 = {worst}（0なら消えている）")
check("終わったら進捗の窓は閉じている", not win._progress.isVisible())

# ================================================================ 原因B
print("\n【原因B】読み込んだ後に原本が差し替わったら、塗らずに止める")
workb = TMP / "B"
workb.mkdir(exist_ok=True)

# B-1 ページ数が増えた差し替え
f1 = workb / "増えた.pdf"
shutil.copy(SRC, f1)
win.clear_docs()
win.add_files([f1])
pump()
win.entries[0].rects = [dict(NAME_BOX)]
with fitz.open(SRC) as a, fitz.open(SRC) as b:
    a.insert_pdf(b)                       # 4ページ → 8ページ
    a.save(str(f1))
fresh_output()
win.run_masking()
wait_worker(win._mask_worker)
pump(8)
html = win.results.toHtml()
# 差し替えは2つの網で捕まえる。控え（大きさと更新時刻）のほうが先に気づくので、
# 出る文言はそちら。どちらの網で止まっても「塗らずに止める」が守れていればよい
check("ページ数が増えていたら✕で止まる（緑の✓を出さない）",
      "検査済み" not in html
      and ("変わっています" in html or "変わっています" in html.replace("&#22793;", "変")),
      html[-400:].replace("\n", " ")[:260])
check("止まったので出力は残らない", not list(masking.OUTPUT_DIR.glob("*_masked.pdf")))

# ページ数の砦そのものも単独で確かめる（控えの網をすり抜けた場合の最後の守り）
data8 = f1.read_bytes()
with fitz.open(stream=data8, filetype="pdf") as d:
    real = d.page_count
try:
    masking.mask_one_file("試し.pdf", data8, [dict(NAME_BOX)], "black", "", "all", 4)
    check("ページ数の砦が単独でも効く", False, f"止まらなかった（実際は{real}ページ）")
except ValueError as ex:
    check("ページ数の砦が単独でも効く", "ページ" in str(ex), str(ex))
check("砦が止めたときも出力は残らない", not list(masking.OUTPUT_DIR.glob("試し*")))

# B-2 ページ数は同じだが中身が差し替わった
f2 = workb / "同ページで差し替え.pdf"
shutil.copy(SRC, f2)
win.clear_docs()
win.add_files([f2])
pump()
win.entries[0].rects = [dict(NAME_BOX)]
before = gui.DocEntry.stamp_of(f2)
with fitz.open(SRC) as d:
    d.save(str(f2), garbage=4, deflate=True)   # 中身は同じ紙・バイト列は別物
after = gui.DocEntry.stamp_of(f2)
fresh_output()
win.run_masking()
wait_worker(win._mask_worker)
pump(8)
html2 = win.results.toHtml()
check("差し替えを見分けられている（控えが変わっている）", before != after, f"{before} vs {after}")
check("同ページ数の差し替えでも✕で止まる",
      "検査済み" not in html2 and "ファイルが変わっています" in html2,
      html2[-500:].replace("\n", " ")[:300])
check("止まったので出力は残らない", not list(masking.OUTPUT_DIR.glob("*_masked.pdf")))

# B-3 何も起きていなければ普通に通る（止めすぎていないこと）
f3 = workb / "普通.pdf"
shutil.copy(SRC, f3)
win.clear_docs()
win.add_files([f3])
pump()
win.entries[0].rects = [dict(NAME_BOX)]
fresh_output()
win.run_masking()
wait_worker(win._mask_worker)
pump(8)
outs = list(masking.OUTPUT_DIR.glob("*_masked.pdf"))
check("差し替えが無ければ従来どおり通る", len(outs) == 1 and "検査済み" in win.results.toHtml(),
      win.results.toHtml()[-300:].replace("\n", " "))
check("氏名欄が消えている", outs and name_box_range(outs[0], 1) == 0)

# ================================================================ 原因C
print("\n【原因C】マウスの掴みが外から切られたら、矩形を元へ戻す")
cv = win.canvas
win.clear_docs()
fc = TMP / "C.pdf"
shutil.copy(SRC, fc)
win.add_files([fc])
pump()
e = win.entries[0]
e.rects = [dict(NAME_BOX)]
win.sync_inputs_from_cur()
win.after_rects_change()
pump()


def pt(nx, ny):
    pr = cv.page_rect()
    return QPointF(pr.x() + nx * pr.width(), pr.y() + ny * pr.height())


def press(nx, ny):
    cv.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, pt(nx, ny), Qt.LeftButton,
                                   Qt.LeftButton, Qt.NoModifier))


def move(nx, ny, buttons=Qt.LeftButton):
    cv.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, pt(nx, ny), Qt.NoButton,
                                  buttons, Qt.NoModifier))


mid_x = (NAME_BOX["x0"] + NAME_BOX["x1"]) / 2
mid_y = (NAME_BOX["y0"] + NAME_BOX["y1"]) / 2

# C-1 掴んだまま、ボタンを押していない移動が来る（＝掴みが切られた後）
press(mid_x, mid_y)
move(mid_x + .02, mid_y + .02)                 # 普通の移動（ボタン押下あり）
move(mid_x + .30, mid_y + .30, Qt.NoButton)    # 掴みが切れたあとの移動
pump()
check("ボタンを離した移動では矩形が付いてこない",
      abs(cv.rects[0]["x0"] - NAME_BOX["x0"]) < 1e-9, str(cv.rects[0]))
check("操作は取り消されている", cv._action is None)

# C-2 OSからの「掴みが外れた」合図
e.rects[0] = dict(NAME_BOX)
cv.set_rects(e.rects)
press(mid_x, mid_y)
move(mid_x + .05, mid_y + .05)
cv.event(QEvent(QEvent.UngrabMouse))
pump()
check("掴みが外れた合図で元の位置へ戻る",
      abs(cv.rects[0]["x0"] - NAME_BOX["x0"]) < 1e-9, str(cv.rects[0]))

# C-3 幅変更の途中で切られても戻る
e.rects[0] = dict(NAME_BOX)
cv.set_rects(e.rects)
cv.set_selected(0)
press(NAME_BOX["x1"], mid_y)                   # 右辺のハンドルを掴む
move(NAME_BOX["x1"] - .20, mid_y)
cv.event(QEvent(QEvent.UngrabMouse))
pump()
check("幅を変えている途中で切られても元へ戻る",
      abs(cv.rects[0]["x1"] - NAME_BOX["x1"]) < 1e-9, str(cv.rects[0]))

# C-4 普通の操作は今までどおり効く（止めすぎていないこと）
e.rects[0] = dict(NAME_BOX)
cv.set_rects(e.rects)
press(mid_x, mid_y)
move(mid_x + .05, mid_y)
cv.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, pt(mid_x + .05, mid_y),
                                 Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
pump()
check("指を離すまでの普通の移動は今までどおり効く",
      abs(cv.rects[0]["x0"] - (NAME_BOX["x0"] + .05)) < .002, str(cv.rects[0]))

# ================================================================ 原因D
print("\n【原因D】置き場所が無くても、窓が出ないまま終わらない")
deep = TMP / "D" / "まだ無い" / "スミケシ"
masking.BASE_DIR = deep
masking.OUTPUT_DIR = deep / "output"
masking.PRESET_DIR = deep / "presets"
masking.TRASH_DIR = deep / "trash"
try:
    masking.list_presets()
    ok1 = True
    err = ""
except Exception as ex:
    ok1, err = False, f"{type(ex).__name__}: {ex}"
check("親フォルダが無くてもプリセットの読み出しが通る（起動時に必ず通る道）", ok1, err)
try:
    masking.save_preset("試し", [dict(NAME_BOX)], "black", "", "all")
    masking.delete_preset("試し")
    ok2, err2 = True, ""
except Exception as ex:
    ok2, err2 = False, f"{type(ex).__name__}: {ex}"
check("保存と削除（trashへの退避）も通る", ok2, err2)
check("退避先が実際に作られている", (deep / "trash").is_dir())
check("_writable_base の逃がし先は作ってから返す仕様になっている",
      "mkdir(parents=True" in (BASE / "masking.py").read_text(encoding="utf-8")
      and "_can_write(home)" in (BASE / "masking.py").read_text(encoding="utf-8"))

# ================================================================ 原因E
print("\n【原因E】矩形の✕は赤く、消してもすぐ戻せる（2026-08-22の設計判断）")
work_e = TMP / "E"
work_e.mkdir(exist_ok=True)
fe = work_e / "答案.pdf"
shutil.copy(SRC, fe)
win.clear_docs()
win.add_files([fe])
pump()
e = win.entries[0]
e.rects = [dict(NAME_BOX), {"x0": .1, "y0": .5, "x1": .3, "y1": .6}]
win.after_rects_change()
pump()

btn_kind = win.rect_list.itemWidget(win.rect_list.item(0)).findChild(QPushButton).property("kind")
check("矩形一覧の✕は平常時から赤（iconDanger）", btn_kind == "iconDanger", str(btn_kind))

win.delete_rect(0)                      # 氏名欄の矩形を✕で消す
pump()
check("削除した矩形は一覧から消える", len(e.rects) == 1)
check("氏名欄の矩形は消えている（残っているのは2つ目だけ）",
      abs(e.rects[0]["x0"] - .1) < 1e-9, str(e.rects))

win.undo_delete_rect()                  # ⌘Z 相当
pump()
check("⌘Zで直後に元へ戻る", len(e.rects) == 2 and abs(e.rects[0]["x0"] - NAME_BOX["x0"]) < 1e-9,
      str(e.rects))

win.undo_delete_rect()                  # 二度目は戻すものが無い
check("戻すものが無ければ何も壊れない", len(e.rects) == 2)

# 削除→ファイルを切り替え→戻す、を試す（対象が居なくなっている場合の安全確認）
win.delete_rect(0)
pump()
win.clear_docs()
win.undo_delete_rect()
check("削除後にファイルが無くなっても、戻そうとして落ちない（例外を出さない）", True)

# ================================================================ 原因F
print("\n【原因F】二重起動：同じ作業フォルダを2つ目が使えない")
work_f = TMP / "F"
work_f.mkdir(exist_ok=True)

srv1 = gui._claim_single_instance(work_f)
check("1つ目は名前を取れる", srv1 is not None)
srv2 = gui._claim_single_instance(work_f)
check("2つ目は取れない（同じ作業フォルダ）", srv2 is None)
if srv1:
    srv1.close()
srv3 = gui._claim_single_instance(work_f)
check("1つ目が終われば、次はまた取れる", srv3 is not None)
if srv3:
    srv3.close()

work_g = TMP / "G"
work_g.mkdir(exist_ok=True)
srv4 = gui._claim_single_instance(work_f)
srv5 = gui._claim_single_instance(work_g)
check("違う作業フォルダなら、同時に取れる（共有していないので問題ない）",
      srv4 is not None and srv5 is not None)
if srv4:
    srv4.close()
if srv5:
    srv5.close()

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + "=" * 60)
print("すべて通りました。" if not fails else f"通らなかった項目 {len(fails)} 件: {fails}")
sys.exit(1 if fails else 0)
