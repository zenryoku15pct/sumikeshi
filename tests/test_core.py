#!/usr/bin/env python3
"""マスキング中核が、旧ブラウザ版と同じ結果を出し続けているかを確かめる。

    python3 tests/test_core.py

golden.json は v2.0.0 で画面を作り直したとき、旧 app.py（ブラウザ版）から採取した
値。ここが崩れたら、実機テストとセキュリティ点検を通った挙動から外れたということ。
処理の中身を変えたつもりがないのに落ちたら、変更を疑う。
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import masking                                    # noqa: E402
from make_golden import CASES, collect, digest    # noqa: E402


def main() -> int:
    golden = json.loads((Path(__file__).resolve().parent / "golden.json")
                        .read_text(encoding="utf-8"))
    sample = BASE / "samples" / golden["素材"]
    if not sample.exists():
        print(f"素材が見つかりません: {sample}")
        print("  python3 make_dummy_sample.py で作れます")
        return 2

    fails = []
    got_sample = digest(sample.read_bytes())
    if got_sample != golden["素材sha256"]:
        print(f"NG  素材PDFが違います（{golden['素材']}）")
        print(f"    期待 {golden['素材sha256'][:16]} / 実際 {got_sample[:16]}")
        return 2

    print(f"素材: {golden['素材']}（{golden['採取元']} から採取した正解と突き合わせ）\n")
    now = collect(masking, sample)
    for want, got in zip(golden["結果"], now):
        ok = all(want[k] == got[k] for k in ("pages", "blank", "verdict", "size", "sha256"))
        print(("  OK  " if ok else "  NG  ") + want["label"])
        if not ok:
            fails.append(want["label"])
            for k in ("pages", "blank", "verdict", "size", "sha256"):
                if want[k] != got[k]:
                    print(f"        {k}: 期待 {want[k]} / 実際 {got[k]}")

    # ページ指定の解釈（記法の総当たり）と、不正入力での振る舞い
    print()
    specs = {"": 4, "2-30, 35": 3, "1": 1, "3-1": 3, "1、2 3": 3, "0": 0, "999": 0,
             "2−4": 3, "2〜4": 3, "2~4": 3, "  ": 4}
    bad = [s for s, n in specs.items() if len(masking.parse_page_spec(s, "all", 4)) != n]
    print(("  OK  " if not bad else "  NG  ") + f"ページ指定の解釈 {len(specs)} 通り"
          + (f"　外れ: {bad}" if bad else ""))
    fails += bad
    for s in ("abc", "1-a", "@"):
        try:
            masking.parse_page_spec(s, "all", 4)
            print(f"  NG  不正なページ指定 {s!r} が素通りした")
            fails.append(s)
        except ValueError:
            pass
    try:
        masking.clean_rects([{"x0": "a"}])
        print("  NG  不正な矩形が素通りした")
        fails.append("矩形")
    except ValueError:
        pass
    print("  OK  不正な入力はきちんと弾く" if not fails else "")

    print("\n" + "=" * 56)
    print("中核は固定した正解と同じ結果を出しています。" if not fails
          else f"外れた項目 {len(fails)} 件: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
