#!/usr/bin/env python3
"""
Eyeball check for fm_ocr.py output.

    python verify_sample.py <video> --out <dir> [-n 20] [--seed 1]

Picks N random rows of <dir>/items.csv, finds a frame in <dir>/frames_log.json where
that row was read, and writes the row's crop next to the OCR result into <dir>/verify/
(one PNG per row plus sheet.png with all of them stacked).
"""
import argparse, csv, json, os, random, sys
import cv2
import numpy as np
from fm_ocr import LIST_Y0, ROW_H

CROP_X = (440, 880)        # icon + text column of the item list
PANEL_W = 620
SCALE = 2


def ascii_text(s):
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in str(s))


def grab_frames(video, wanted):
    """Yield (index, frame) for the wanted source-frame indices, in order.
    Streams the file because seeking by timestamp is not frame-accurate on these recordings."""
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        sys.exit(f"cannot open video: {video}")
    last = max(wanted, default=-1)
    i = 0
    while i <= last and cap.grab():
        if i in wanted:
            ok, fr = cap.retrieve()
            if ok:
                if fr.shape[1] != 1920:
                    fr = cv2.resize(fr, (1920, 1080))
                yield i, fr
        i += 1
    cap.release()


def find_reading(row, log):
    """Return (frame_index, slot) of a frame where this row was read, or None."""
    name, price = row["list_name"], int(row["price"])
    exact, loose = [], []
    for e in log:
        if e["shop"] != row["shop_owner"]:
            continue
        for (n, p), k in zip(e["rows"], e["slots"]):
            # fm_ocr canonicalises "Name 1" -> "Name", so accept short suffixes too
            if n == name or (n.startswith(name) and len(n) - len(name) <= 4):
                (exact if p == price else loose).append((e["f"], k))
    hits = exact or loose
    return hits[len(hits) // 2] if hits else None


def render(row, crop, frame_idx, slot):
    crop = cv2.resize(crop, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_NEAREST)
    panel = np.full((crop.shape[0], PANEL_W, 3), 255, np.uint8)
    lines = [f"shop : {row['shop_owner']}  ({row['shop_title']})",
             f"list : {row['list_name']}",
             f"item : {row['item']}",
             f"price: {int(row['price']):,}"]
    if "qty" in row:
        unit = row.get("unit_price") or "-"
        lines.append(f"qty  : {row['qty']}   unit: {unit}")
    lines.append(f"seen : {row['frames_seen']} frames   (frame {frame_idx}, row {slot + 1})")
    for i, l in enumerate(lines):
        cv2.putText(panel, ascii_text(l)[:60], (10, 24 + i * 24), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (0, 0, 0) if i else (90, 90, 90), 1, cv2.LINE_AA)
    img = np.hstack([crop, panel])
    return cv2.copyMakeBorder(img, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=(0, 0, 200))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", default="out", help="fm_ocr.py output dir")
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()

    with open(os.path.join(a.out, "items.csv"), encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with open(os.path.join(a.out, "frames_log.json")) as f:
        log = json.load(f)
    if log and "slots" not in log[0]:
        sys.exit("frames_log.json is from an older fm_ocr.py; re-run fm_ocr.py first")

    rng = random.Random(a.seed)
    sample = rng.sample(range(len(rows)), min(a.n, len(rows)))
    want = {}                                    # frame index -> [(sample no, row, slot)]
    missing = 0
    for no, ri in enumerate(sample):
        hit = find_reading(rows[ri], log)
        if hit is None:
            missing += 1
            continue
        want.setdefault(hit[0], []).append((no, rows[ri], hit[1]))

    vdir = os.path.join(a.out, "verify")
    os.makedirs(vdir, exist_ok=True)
    for old in os.listdir(vdir):
        if old.endswith(".png"):
            os.remove(os.path.join(vdir, old))

    tiles = {}
    for idx, fr in grab_frames(a.video, want):
        for no, row, slot in want[idx]:
            top = LIST_Y0 + slot * ROW_H
            crop = fr[top - 4:top + ROW_H - 4, CROP_X[0]:CROP_X[1]]
            img = render(row, crop, idx, slot)
            tiles[no] = img
            name = ascii_text(f"{no + 1:02d}_{row['shop_owner']}_{row['list_name']}")
            name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60]
            cv2.imwrite(os.path.join(vdir, name + ".png"), img)

    if tiles:
        cv2.imwrite(os.path.join(vdir, "sheet.png"), np.vstack([tiles[k] for k in sorted(tiles)]))
    print(f"sampled={len(sample)} written={len(tiles)} no_frame_found={missing}")
    print(f"saved -> {vdir}")


if __name__ == "__main__":
    main()
