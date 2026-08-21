#!/usr/bin/env python3
"""スミケシのアイコンを作る（生成元。画像だけ残して作り方を失わないため）

    python3 make_icon.py

出力:
    assets/icon.png    1024x1024（元画像）
    assets/icon.icns   macOS用（iconutil がある環境のみ）
    assets/icon.ico    Windows用
    assets/check.png   チェック欄の印（白い✓）

意匠：白い紙の上に、墨で引いた一本の帯。道具の名前（墨消し）そのまま。
帯は紙の左右からわずかにはみ出す——上に重ねた四角ではなく「引いた一本」に見せる。
小さいサイズ（16px）でも形が潰れないよう、要素は3つ（下地・紙・帯）に絞っている。
"""
import struct
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QGuiApplication, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPen)

S = 1024
ASSETS = Path(__file__).resolve().parent / "assets"


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    k = size / S  # 1024基準で描いて縮める

    # 角丸の下地（濃紺→藍のグラデーション）
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(0, 0, size, size), 224 * k, 224 * k)
    g = QLinearGradient(0, 0, size, size)
    g.setColorAt(0.0, QColor("#2b3a55"))
    g.setColorAt(1.0, QColor("#16213a"))
    p.fillPath(bg, QBrush(g))

    # 紙
    paper = QRectF(250 * k, 178 * k, 524 * k, 668 * k)
    p.setPen(Qt.NoPen)
    for i in range(6):                                    # やわらかい影
        p.fillRect(paper.translated(0, (4 + i * 2.4) * k), QColor(0, 0, 0, 14))
    path = QPainterPath()
    path.addRoundedRect(paper, 18 * k, 18 * k)
    p.fillPath(path, QColor("#f7f9fc"))

    # 紙の罫（薄いグレーの本文行）
    p.setPen(QPen(QColor("#c9d1dc"), 13 * k, Qt.SolidLine, Qt.RoundCap))
    for i, (x0, x1) in enumerate([(0.13, 0.87), (0.13, 0.87), (0.13, 0.62)]):
        y = paper.y() + paper.height() * (0.53 + i * 0.145)
        p.drawLine(QPointF(paper.x() + paper.width() * x0, y),
                   QPointF(paper.x() + paper.width() * x1, y))

    # 墨の帯（氏名欄を塗ったところ）。紙の左右から少しはみ出させて「一本引いた」形にする
    over = 34 * k
    bar = QRectF(paper.x() - over, paper.y() + 118 * k,
                 paper.width() + over * 2, 104 * k)
    bp = QPainterPath()
    bp.addRoundedRect(bar, 8 * k, 8 * k)
    p.fillPath(bp, QColor(0, 0, 0, 55))                   # 帯の落ち影
    p.fillPath(bp.translated(0, -5 * k), QColor("#10141c"))
    p.end()
    return img


def draw_check(size: int = 30) -> QImage:
    """チェック欄の印（白い✓）。

    Fusionが描く既定のチェック欄は、白い下地の上だと枠がほとんど見えない
    （2026-08-21に実機で確認）。枠と印はこちらで指定する。
    """
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    k = size / 30
    pen = QPen(QColor("#ffffff"), 4.2 * k)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    path = QPainterPath()
    path.moveTo(7.5 * k, 15.5 * k)
    path.lineTo(12.8 * k, 21 * k)
    path.lineTo(22.5 * k, 9.5 * k)
    p.drawPath(path)
    p.end()
    return img


def write_ico(src: QImage, dest: Path, sizes=(16, 24, 32, 48, 64, 128, 256)):
    """PNGを収めたICOを組み立てる（Windows Vista以降が読める形式）。"""
    blobs = []
    for s in sizes:
        tmp = dest.with_suffix(f".{s}.png")
        draw(s).save(str(tmp), "PNG")
        blobs.append(tmp.read_bytes())
        tmp.unlink()
    n = len(sizes)
    head = struct.pack("<HHH", 0, 1, n)
    offset = 6 + 16 * n
    entries, body = b"", b""
    for s, blob in zip(sizes, blobs):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        body += blob
    dest.write_bytes(head + entries + body)


def main() -> int:
    QGuiApplication(sys.argv[:1])
    ASSETS.mkdir(exist_ok=True)
    png = ASSETS / "icon.png"
    draw(S).save(str(png), "PNG")
    print("書き出し:", png)

    chk = ASSETS / "check.png"
    draw_check(30).save(str(chk), "PNG")
    print("書き出し:", chk)

    write_ico(draw(256), ASSETS / "icon.ico")
    print("書き出し:", ASSETS / "icon.ico")

    if sys.platform == "darwin":
        iconset = ASSETS / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        for s in (16, 32, 128, 256, 512):
            draw(s).save(str(iconset / f"icon_{s}x{s}.png"), "PNG")
            draw(s * 2).save(str(iconset / f"icon_{s}x{s}@2x.png"), "PNG")
        r = subprocess.run(["iconutil", "-c", "icns", str(iconset),
                            "-o", str(ASSETS / "icon.icns")])
        if r.returncode == 0:
            print("書き出し:", ASSETS / "icon.icns")
            for f in iconset.iterdir():
                f.unlink()
            iconset.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
