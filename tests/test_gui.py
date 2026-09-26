#!/usr/bin/env python3
"""画面の動きを1項目ずつ機械で確かめる（見た目ではなく挙動の確認）。

    QT_QPA_PLATFORM=offscreen python3 tests/test_gui.py

v2.0.0 でブラウザ版から画面を作り直したときの移植チェックリストが、そのまま
この項目になっている。機能を落としていないことを、ここで担保する。
画面を出さずに走るので、CIでも実機でも同じように使える。
"""
import shutil
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.argv = ["gui.py"]

from PySide6.QtCore import QEvent, QPointF, Qt          # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent        # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import gui        # noqa: E402
import masking    # noqa: E402

app = QApplication([])
app.setStyle("Fusion")
app.setStyleSheet(gui.STYLE)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)   # 確認は常に「はい」


def pump(n=4):
    for _ in range(n):
        app.processEvents()


fails = []


def check(label, cond, extra=""):
    print(("  OK  " if cond else "  NG  ") + label + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        fails.append(label)


win = gui.MainWindow()
win.resize(1240, 850)
win.show()
pump()
cv = win.canvas


def pt(nx, ny):
    pr = cv.page_rect()
    return QPointF(pr.x() + nx * pr.width(), pr.y() + ny * pr.height())


def drag(a, b):
    cv.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, pt(*a), Qt.LeftButton,
                                   Qt.LeftButton, Qt.NoModifier))
    cv.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, pt(*b), Qt.NoButton,
                                  Qt.LeftButton, Qt.NoModifier))
    cv.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, pt(*b), Qt.LeftButton,
                                     Qt.NoButton, Qt.NoModifier))
    pump()


def click(nx, ny):
    cv.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, pt(nx, ny), Qt.LeftButton,
                                   Qt.LeftButton, Qt.NoModifier))
    cv.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, pt(nx, ny), Qt.LeftButton,
                                     Qt.NoButton, Qt.NoModifier))
    pump()


def key(k):
    cv.keyPressEvent(QKeyEvent(QEvent.KeyPress, k, Qt.NoModifier))
    pump()


src = BASE / "samples/ダミー調査票.pdf"
if not src.exists():
    print(f"素材がありません: {src}\n  python3 make_dummy_sample.py で作れます")
    raise SystemExit(2)
tmp = Path(tempfile.mkdtemp(prefix="sumikeshi-test-"))
a, b = tmp / "甲_調査票.pdf", tmp / "乙_調査票.pdf"
shutil.copy(src, a)
shutil.copy(src, b)

print("\n【1】PDFの読み込み")
win.add_files([a, b]); pump()
check("2ファイル読み込めた", len(win.entries) == 2)
check("1本目が表示対象", win.ref_id == win.entries[0].doc_id)
check("ページ数を取れている", win.entries[0].pages == 4, str(win.entries[0].pages))
win.add_files([tmp / "存在しないファイル.pdf"])
check("壊れた指定を足しても増えない", len(win.entries) == 2)

print("\n【2】矩形をドラッグで作る")
drag((.10, .07), (.50, .12))
check("矩形が1つできた", len(cv.rects) == 1, str(len(cv.rects)))
check("作った矩形が選択される", cv.selected == 0)
r0 = dict(cv.rects[0])
check("座標がドラッグ通り", abs(r0["x0"] - .10) < .01 and abs(r0["y1"] - .12) < .01, str(r0))
drag((.20, .30), (.10, .20))    # 逆向きドラッグ
check("逆向きでも x0<x1 に整う", cv.rects[1]["x0"] < cv.rects[1]["x1"])
drag((.60, .60), (.6005, .6005))  # 極小
check("極小ドラッグは矩形にしない", len(cv.rects) == 2, str(len(cv.rects)))

print("\n【3】選択・移動・サイズ変更・削除")
click(.30, .09)
check("矩形の内側クリックで選択", cv.selected == 0, str(cv.selected))
before = dict(cv.rects[0])
drag((.30, .09), (.35, .14))       # 内側ドラッグ＝移動
moved = cv.rects[0]
check("内側ドラッグで移動する",
      abs(moved["x0"] - (before["x0"] + .05)) < .01 and
      abs((moved["x1"] - moved["x0"]) - (before["x1"] - before["x0"])) < .001, str(moved))
cv.set_selected(0)
w_before = cv.rects[0]["x1"] - cv.rects[0]["x0"]
drag((cv.rects[0]["x1"], (cv.rects[0]["y0"] + cv.rects[0]["y1"]) / 2), (.70, (cv.rects[0]["y0"] + cv.rects[0]["y1"]) / 2))
check("右辺ハンドルで幅が変わる", abs((cv.rects[0]["x1"] - cv.rects[0]["x0"]) - w_before) > .05,
      f'{w_before:.3f} -> {cv.rects[0]["x1"] - cv.rects[0]["x0"]:.3f}')
cv.set_selected(0); key(Qt.Key_Delete)
check("Deleteキーで削除", len(cv.rects) == 1)
check("削除後は選択解除", cv.selected == -1)
cv.set_selected(0); key(Qt.Key_Escape)
check("Escで選択解除", cv.selected == -1)

print("\n【4】ページ送り")
p0 = win.page; key(Qt.Key_Right)
check("→で次のページ", win.page == p0 + 1, str(win.page))
key(Qt.Key_Left)
check("←で前のページ", win.page == p0)
for _ in range(9): key(Qt.Key_Left)
check("1ページ目より前へは行かない", win.page == 1)
for _ in range(9): key(Qt.Key_Right)
check("最終ページより先へは行かない", win.page == 4, str(win.page))

print("\n【5】矩形のコピー・貼り付け")
e0, e1 = win.entries
e0.rects = [{"x0": .1, "y0": .07, "x1": .5, "y1": .12}, {"x0": .6, "y0": .07, "x1": .9, "y1": .12}]
cv.set_rects(e0.rects); cv.set_selected(-1); win.render_rect_list()
win.copy_rects()
check("全矩形をコピーできる", len(win.clipboard["rects"]) == 2)
win.switch_doc(e1.doc_id); win.paste_rects(); pump()
check("別ファイルへ2件貼り付く", len(e1.rects) == 2, str(len(e1.rects)))
check("同じ位置に貼り付く", abs(e1.rects[0]["x0"] - .1) < 1e-9, str(e1.rects[0]))
win.paste_rects()
check("コピー元が別ファイルなら2回目もずらさない（旧版と同じ）",
      abs(e1.rects[2]["x0"] - .1) < 1e-9, str(e1.rects[2]))
e1.rects = [{"x0": .1, "y0": .07, "x1": .5, "y1": .12}]   # 1件だけにして条件を単純にする
cv.set_rects(e1.rects); cv.set_selected(-1)
win.copy_rects(); win.paste_rects()          # 同じファイルでコピー→貼り付け
check("同じファイル内での複製は少しずらす",
      len(e1.rects) == 2 and abs(e1.rects[1]["x0"] - .12) < 1e-9
      and abs((e1.rects[1]["x1"] - e1.rects[1]["x0"]) - .4) < 1e-9,
      str(e1.rects))
win.switch_doc(e0.doc_id); cv.set_selected(1)
# 複数件あるときは「全件か選択中だけか」を確かめる窓が出る。「いいえ」＝選択中だけ
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
win.copy_rects()
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
check("選択中のみコピーできる", len(win.clipboard["rects"]) == 1)

print("\n【6】ファイルごとの設定")
win.switch_doc(e0.doc_id); win.range_edit.setText("2-3"); pump()
check("ページ範囲がファイルに入る", e0.page_range == "2-3", e0.page_range)
win.parity_box.setCurrentIndex(1); pump()
check("奇数偶数がファイルに入る", e0.parity == "odd", e0.parity)
win.switch_doc(e1.doc_id); pump()
check("切替で入力欄も入れ替わる", win.range_edit.text() == "" and win.parity_box.currentData() == "all",
      f'{win.range_edit.text()!r} {win.parity_box.currentData()}')
win.switch_doc(e0.doc_id); pump()
check("戻すと元の設定が出る", win.range_edit.text() == "2-3" and win.parity_box.currentData() == "odd")

print("\n【7】設定を全ファイルへコピー")
win.copy_to_all(); pump()
check("矩形が他ファイルへ渡る", len(e1.rects) == len(e0.rects), f"{len(e1.rects)} vs {len(e0.rects)}")
check("ページ範囲も渡る", e1.page_range == "2-3" and e1.parity == "odd")
check("値の共有ではなく複製", e1.rects[0] is not e0.rects[0])

print("\n【8】プリセット")
name = "テスト用_氏名欄"
for p in (masking.PRESET_DIR / f"{name}.json",):
    if p.exists(): p.unlink()
win.preset_name.setText(name); win.preset_save(); pump()
check("保存できる", (masking.PRESET_DIR / f"{name}.json").exists())
e1.rects = []; win.switch_doc(e1.doc_id)
idx = next(i for i in range(win.preset_box.count())
           if (win.preset_box.itemData(i) or {}).get("name") == name)
win.preset_box.setCurrentIndex(idx)
win.preset_load(False); pump()
check("このファイルへ読み込める", len(e1.rects) == len(e0.rects), str(len(e1.rects)))
check("ページ範囲も復元される", e1.page_range == "2-3" and e1.parity == "odd")
for e in win.entries: e.rects = []
win.preset_box.setCurrentIndex(idx); win.preset_load(True); pump()
check("全ファイルへ読み込める", all(e.rects for e in win.entries))
win.preset_box.setCurrentIndex(idx); win.preset_delete(); pump()
check("削除するとファイルが消える", not (masking.PRESET_DIR / f"{name}.json").exists())
check("消さずtrashへ退避している", any(p.name.startswith(f"{name}.json.") for p in masking.TRASH_DIR.glob("*")))

print("\n【9】実行と検査")
for e in win.entries:
    e.rects = [{"x0": .085, "y0": .073, "x1": .52, "y1": .108}]
    e.page_range = ""; e.parity = "all"
win.entries[1].rects = []          # 片方は未設定＝スキップされるはず
for f in masking.OUTPUT_DIR.glob("*_masked*.pdf"): f.unlink()
check("実行ボタンが押せる", win.apply_btn.isEnabled())
win.run_masking()
for _ in range(900):
    app.processEvents()
    if win._mask_worker and not win._mask_worker.isRunning(): break
    app.thread().msleep(20)
pump(8)
html = win.results.toHtml()
outs = list(masking.OUTPUT_DIR.glob("*_masked*.pdf"))
check("PDFが1本書き出された", len(outs) == 1, str([p.name for p in outs]))
check("結果欄に枠内クリアと出る（✓に「検査済み」の顔をさせない）", "枠内クリア" in html and "検査済み" not in html)
check("矩形未設定はスキップと出る", "スキップ" in html)
# 2026-08-22：この2つの注意書きは結果欄（高さの決まった箱）から、実行カードの
# 常設ラベルへ移した。複数ファイル処理時に箱の下へ隠れる問題への対処（監査⑤）。
# 常に表示されるので毎回のトースト等は不要——ラベルの中身を直接見る
warn_text = win.warn.text()
check("範囲外の注意書きが出る（常設ラベル）", "塗った範囲の中だけ" in warn_text)
check("元ファイルの注意書きが出る（常設ラベル）", "移動・削除されません" in warn_text)
check("原本を書き換えていない", a.read_bytes() == src.read_bytes())

print("\n【10】ファイルの取り除き")
n = len(win.entries); win.remove_doc(win.entries[1].doc_id); pump()
check("1本取り除ける", len(win.entries) == n - 1)
win.clear_docs(); pump()
check("すべて取り除ける", len(win.entries) == 0 and win.ref_id is None)
check("実行ボタンが押せなくなる", not win.apply_btn.isEnabled())


print("\n【11】漏れサンプルの検査（見逃していないか）")
leak = BASE / "samples/leak-check"
RECT_LEAK = {"x0": 0.5418, "y0": 0.1080, "x1": 0.9273, "y1": 0.1825}
if not leak.is_dir():
    print("  --  漏れサンプルが無いので省略（python3 make_leak_samples.py で作れます）")
else:
    import check_masked as ck
    d = gui.CheckDialog()
    d.folder.setText(str(leak))
    d.show(); pump()
    d._run()
    for _ in range(1500):
        app.processEvents()
        if d._worker and not d._worker.isRunning():
            break
        app.thread().msleep(20)
    pump(8)
    txt = d.summary.text()
    n_files = len(list(leak.glob("*.pdf")))
    def count(label):
        return sum(1 for i in range(d.tree.topLevelItemCount())
                   if label in d.tree.topLevelItem(i).text(1))
    check("全ファイルを検査した", d.tree.topLevelItemCount() == n_files, txt)
    # 自動検出は「自分で見つけた黒を自分で確かめる」形なので、正常品でも
    # 枠内クリアを出さない（要確認どまり・2026-08-23の設計判断）
    check("自動検出では枠内クリアを出さない", count("枠内クリア") == 0,
          f'枠内クリア {count("枠内クリア")} 件')
    # 08（枠の外にはみ出し）は枠の内側が消えているので消え残りではなく要確認
    check("漏れのある6本は消え残りありになる", count("消え残り") == n_files - 2,
          f'消え残り {count("消え残り")} 件')
    def item_named(prefix):
        return next((d.tree.topLevelItem(i) for i in range(d.tree.topLevelItemCount())
                     if d.tree.topLevelItem(i).text(0).startswith(prefix)), None)
    it08 = item_named("08_")
    check("08（枠の外にはみ出し）は要確認になる", it08 is not None and "要確認" in it08.text(1),
          it08.text(1) if it08 else "08が無い")
    d.close()
    # 枠を指定すれば、正常な1本だけが枠内クリアになる
    d2 = gui.CheckDialog(screen_rects=[dict(RECT_LEAK)])
    d2.folder.setText(str(leak))
    d2.show(); pump()
    d2._run()
    for _ in range(1500):
        app.processEvents()
        if d2._worker and not d2._worker.isRunning():
            break
        app.thread().msleep(20)
    pump(8)
    def count2(label):
        return sum(1 for i in range(d2.tree.topLevelItemCount())
                   if label in d2.tree.topLevelItem(i).text(1))
    check("枠を指定すれば正常な1本だけ枠内クリア", count2("枠内クリア") == 1,
          f'枠内クリア {count2("枠内クリア")} 件')
    d2.close()

shutil.rmtree(tmp, ignore_errors=True)
for f in masking.OUTPUT_DIR.glob("*_masked*.pdf"):
    f.unlink()
for f in masking.OUTPUT_DIR.glob("検査レポート_*.md"):
    f.unlink()
for f in masking.TRASH_DIR.glob("テスト用_氏名欄.json.*"):
    f.unlink()

print("\n" + "=" * 56)
print("すべて通りました。" if not fails else f"通らなかった項目 {len(fails)} 件: {fails}")
sys.exit(1 if fails else 0)
