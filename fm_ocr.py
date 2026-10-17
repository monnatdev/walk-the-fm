#!/usr/bin/env python3
"""
FM price POC for Old School Maple (MapleStory Worlds).

Input : a screen recording of walking through Hired Merchant shops in FM.
Output: items.csv  (shop, item, price, qty, unit_price, stats) + frames_log.json, tooltips.json.

Pipeline
  1. stream frames from the video (OpenCV), skip frames with no shop window

  2. per frame: detect an open Hired Merchant window (title bar OCR)
  3. read the shop's item list (name + price rows) where not covered by a tooltip,
     plus the stack quantity drawn on each icon (fm_qty.py; 0 = sold out)
  4. detect the purple item tooltip, OCR full name + stat lines
  5. merge across frames: the scrollbar thumb gives each row's position in the shop's
     list, majority vote per (shop, position), attach tooltip stats
     by matching the (possibly truncated) list name as a prefix of the tooltip name
Only reads pixels from a recording; it never touches the game.
"""
import argparse, collections, csv, hashlib, json, os, re, sys
from concurrent.futures import ThreadPoolExecutor
import cv2
import numpy as np
import pytesseract
from fm_qty import read_qty, BAND_X0, BAND_DY, BAND_W, BAND_H

# Windows: point pytesseract at the default install location if it's not on PATH
_WIN_TESS = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.name == "nt" and os.path.exists(_WIN_TESS):
    pytesseract.pytesseract.tesseract_cmd = _WIN_TESS

# ---- layout (1920x1080, default window position) -------------------------
TITLE_BOX = (480, 185, 1360, 222)          # x1,y1,x2,y2  "X's Hired Merchant : title"
LIST_X = (527, 848)                        # text column of item list
LIST_Y0, ROW_H, N_ROWS = 485, 80, 5        # first row top, row height, visible rows
NAME_DY, PRICE_DY = (0, 30), (38, 70)      # offsets inside a row
PRICE_X0 = 556                             # price text starts after the coin icon
ICON_X = (455, 515)                        # item icon column of the list
NAME_CUT_X = 305                          # name text reaching past this (in the column) was clipped
CARRY_SHOP_S = 3.0                         # a hidden title still counts as the shop read this recently
THUMB_X = (862, 880)                       # the item list's scrollbar column
THUMB_Y0, THUMB_TRAVEL, THUMB_H = 514, 286, 42   # thumb top when scrolled to the top, its travel, height


# Speed: each tesseract call is a ~0.1-0.3s process launch. The same rows stay on screen
# for many frames, so results are cached by the exact binarised image tesseract would see
# (identical input -> identical output), and one frame's calls run in parallel threads.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")   # one core per tesseract process
POOL = ThreadPoolExecutor(max_workers=os.cpu_count() or 4)
_ocr_cache = {}
OCR_STATS = collections.Counter()


def ocr(img, psm=7, whitelist=None, invert=False, scale=3):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    if invert:
        g = 255 - g
    g = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    _, g = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    g = cv2.copyMakeBorder(g, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
    cfg = f"--psm {psm}"
    if whitelist:
        cfg += f" -c tessedit_char_whitelist={whitelist}"
    key = (cfg, g.shape, hashlib.blake2b(np.packbits(g < 128).tobytes(), digest_size=16).digest())
    if key in _ocr_cache:
        OCR_STATS["cached"] += 1
        return _ocr_cache[key]
    OCR_STATS["tesseract"] += 1
    text = pytesseract.image_to_string(g, config=cfg).strip()
    _ocr_cache[key] = text
    return text


def clean_name(t, truncated=False):
    """truncated: the name ran into the column edge, so a short last word is the start
    of a real word ("...Axe for AT") rather than mouse-cursor junk."""
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^[^A-Za-z0-9\[]+", "", t)         # leading junk like '* ' '~ ' (keep "[Mastery Book]")
    toks = t.split(" ")
    while toks and len(toks[-1]) <= 2 and not re.search(r"[0-9%]", toks[-1]) and toks[-1] not in ("of", "to"):
        if truncated and re.fullmatch(r"[A-Za-z]{1,2}", toks[-1]):
            break                                   # clipped word, keep it
        toks.pop()                                  # trailing junk from the mouse cursor
    return " ".join(toks).strip(" |_-'\"`")


def tooltip_mask(frame):
    b, g, r = [frame[:, :, i].astype(int) for i in range(3)]
    m = ((abs(g - r) < 22) & (b - g > 28) & (b - g < 75) & (b > 80) & (b < 140)).astype(np.uint8)
    return cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))


def find_tooltip(frame):
    m = tooltip_mask(frame)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m)
    best = None
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if w > 200 and h > 100 and area > 0.5 * w * h:
            if best is None or area > best[4]:
                best = (x, y, w, h, area)
    return None if best is None else best[:4]


def row_covered(frame, y1, y2, tip):
    if tip is None:
        return False
    x, y, w, h = tip
    # tooltip overlaps the row's text column?
    return not (y + h < y1 or y > y2 or x + w < LIST_X[0] or x > LIST_X[1])


PRICE_RE = re.compile(r"([\d][\d,\.]{2,})\s*meso", re.I)


def parse_price(txt):
    m = PRICE_RE.search(txt.replace(" ", ""))
    if not m:
        m = re.search(r"([\d][\d,\.]{2,})", txt)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def read_title(frame):
    x1, y1, x2, y2 = TITLE_BOX
    t = ocr(frame[y1:y2, x1:x2])
    m = re.search(r"(.+?)['’`]s\s*Hired\s*Merchant\s*[:;]?\s*(.*)", t, re.I)
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def read_list(frame, tip):
    """Return [(name, price, slot, qty, rate)] for the visible rows not covered by the tooltip.
    qty: stack size on the icon (1 when no number is drawn, 0 = sold out),
    None when the tooltip hides the icon. rate: a scroll's success rate from its icon."""
    slots, crops = [], []
    for k in range(N_ROWS):
        top = LIST_Y0 + k * ROW_H
        y1, y2 = top + NAME_DY[0], top + PRICE_DY[1]
        if row_covered(frame, y1, y2, tip):
            continue
        slots.append(k)
        crops.append(frame[top + NAME_DY[0]:top + NAME_DY[1], LIST_X[0]:LIST_X[1]])
        crops.append(frame[top + PRICE_DY[0]:top + PRICE_DY[1], PRICE_X0:LIST_X[1]])
    texts = list(POOL.map(ocr, crops))                 # all rows of the frame at once

    rows = []
    for i, k in enumerate(slots):
        top = LIST_Y0 + k * ROW_H
        name_img = crops[2 * i]
        price = parse_price(texts[2 * i + 1])
        name = clean_name(texts[2 * i], truncated=name_cut_off(name_img))
        if len(name) >= 3 and price:
            covered = icon_covered(top, tip)
            if name_faded(frame, top):
                qty = 0                             # sold out: the whole row is greyed out
            elif covered:
                qty = None
            else:
                qty = read_qty(frame, top)
                qty = 1 if qty is None else qty
            rate = None if covered else scroll_rate(frame, top, name)
            rows.append((name, price, k, qty, rate))
    return rows


def scroll_rate(frame, top, name):
    """Success rate of a scroll from its icon colour, or None. The game draws every scroll
    by rate: 10% yellow, 60% red, 100% blue; dark scrolls 30% purple, 70% grey. This
    completes names the list cut off ("Scroll for Overall Armor for INT 1" -> 10%)."""
    low = name.lower()
    dark = low.startswith("dark scroll")
    if not (dark or low.startswith("scroll for")):
        return None
    icon = frame[top + 2:top + 62, ICON_X[0]:ICON_X[1]]
    hsv = cv2.cvtColor(icon, cv2.COLOR_BGR2HSV).reshape(-1, 3)
    coloured = hsv[(hsv[:, 1] > 60) & (hsv[:, 2] > 60)]
    if len(coloured) < 400:                      # barely any colour: the grey dark-scroll icon
        rate = 70
    else:
        hue = float(np.median(coloured[:, 0]))
        rate = (10 if 14 <= hue <= 32 else 60 if hue < 14 or hue >= 165
                else 100 if 85 <= hue <= 110 else 30 if 110 < hue <= 150 else None)
    ok = (30, 70) if dark else (10, 60, 100)
    return rate if rate in ok else None


def name_cut_off(name_img):
    """Long names are clipped at the column edge: their text reaches x >= 311 of 321,
    while whole names end by ~298."""
    dark = (name_img.mean(axis=2) < 80).any(axis=0)
    return bool(dark[NAME_CUT_X:].any())


def name_faded(frame, top):
    """Sold-out rows draw the name in grey (darkest ~125) instead of black (~0)."""
    g = frame[top + NAME_DY[0]:top + NAME_DY[1], LIST_X[0]:LIST_X[1]].mean(axis=2)
    return np.percentile(g, 1) > 60


def icon_covered(top, tip):
    if tip is None:
        return False
    x, y, w, h = tip
    return not (y + h < top + BAND_DY or y > top + BAND_DY + BAND_H
                or x + w < BAND_X0 or x > BAND_X0 + BAND_W)


def read_tooltip(frame, tip):
    x, y, w, h = tip
    crop = frame[y:y + h, x:x + w]
    # brightest channel, not grey: a purple title on the purple box is near-invisible in grey
    title = ocr(crop[20:62, 10:w - 10].max(axis=2), invert=True)
    body = ocr(crop[62:, 10:w - 10], psm=6, invert=True)
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    stats = {}
    for l in lines:
        m = re.match(r"([A-Z][A-Z \.]+?)\s*[:;]\s*(.+)$", l)
        if m:
            k = m.group(1).strip()
            if "REQ" in k or k.startswith("ITEM"):         # also OCR'd "ER REQ DEX"
                continue
            stats[k] = m.group(2).strip()
    # the tooltip is see-through: the cursor or a list icon behind its corner can read as
    # a stray leading letter or two ("B Stonetooth Sword (+4)", "bi Black Snowshoes")
    title = re.sub(r"^\W*([A-Za-z0-9]|[a-z]{2})\W*\s+(?=[A-Z\[])", "", title.strip())
    return clean_name(title), stats


def iter_frames(video, fps):
    """Yield (index, timestamp_s, frame) at roughly `fps`, without writing files."""
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        sys.exit(f"cannot open video: {video}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(1, round(src_fps / fps))
    i = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if i % step == 0:
            ok, fr = cap.retrieve()
            if ok:
                if fr.shape[1] != 1920:
                    fr = cv2.resize(fr, (1920, 1080))
                yield i, i / src_fps, fr
        i += 1
    cap.release()


def shop_window_open(fr, tip=None):
    """Cheap pre-check: the shop title bar is a near-white strip.
    With a tooltip given, only the part of the bar it does not cover is checked
    (tooltips of the top rows reach up over the title bar)."""
    x1, y1, x2, y2 = TITLE_BOX
    white = (fr[y1:y2, x1:x2].mean(axis=2) > 225)
    if tip is not None:
        tx, ty, tw, th = tip
        if ty < y2 and ty + th > y1:
            keep = np.ones(x2 - x1, bool)
            keep[max(0, tx - x1):max(0, tx + tw - x1)] = False
            if keep.sum() < 150:
                return False                    # too little of the bar left to judge
            white = white[:, keep]
    return white.mean() > 0.6


def read_thumb(fr):
    """How far the item list is scrolled, 0..1, from the scrollbar thumb's position;
    None when the cursor or a tooltip hides the thumb. Shops often list the same item
    many times (8 stacks of Wizard Elixir), so the rows alone can't tell how far it moved."""
    y0 = THUMB_Y0 - 9
    band = fr[y0:THUMB_Y0 + THUMB_TRAVEL + THUMB_H + 9, THUMB_X[0]:THUMB_X[1]].astype(int)
    blue = ((band[..., 0] - band[..., 2] > 40) & (band[..., 0] < 200)).mean(axis=1) > 0.5
    ys = np.flatnonzero(blue)
    start = None
    for k, y in enumerate(ys):
        start = y if start is None else start
        if k + 1 == len(ys) or ys[k + 1] != y + 1:          # end of a run of thumb-blue rows
            top, h = y0 + start, y - start + 1
            if abs(h - THUMB_H) <= 3 and -3 <= top - THUMB_Y0 <= THUMB_TRAVEL + 3:
                return min(1.0, max(0.0, (top - THUMB_Y0) / THUMB_TRAVEL))
            start = None
    return None


def place_rows(frames):
    """frames: [(scroll 0..1 or None, [(slot, row, qty)])] of one visit to a shop.
    Returns ({list position: [(row, qty)]}, one entry per frame that saw that position;
    [list position of slot 0, per frame]).
    The thumb gives the scrolled fraction but not the list length; the length is the one
    under which the frames agree best about what sits at each position. While scrolling,
    the thumb is sometimes drawn a row behind or ahead of the list, so each frame may sit
    one row off the thumb's reading when its rows fit there better."""
    def consensus(pos):
        return {p: collections.Counter(r for r, _ in v).most_common(1)[0][0] for p, v in pos.items()}

    def fit(rows, top, best, near):
        hit = sum(best.get(top + k) == row for k, row, _ in rows)
        miss = sum(top + k in best and best[top + k] != row for k, row, _ in rows)
        return hit - 2 * miss, -abs(top - near)

    def build(tops):
        pos = collections.defaultdict(list)
        for (f, rows), top in zip(frames, tops):
            if top is not None:
                for k, row, q in rows:
                    pos[top + k].append((row, q))
        return pos

    def layout(steps):
        est = [None if f is None else round(f * steps) for f, _ in frames]
        tops = est
        for _ in range(2):
            best = consensus(build(tops))
            tops = []
            for (f, rows), t in zip(frames, est):
                if t is not None and rows:
                    t = max({max(t - 1, 0), t, t + 1}, key=lambda top: fit(rows, top, best, t))
                tops.append(t)
        return build(tops), tops, est

    def score(steps):
        pos, tops, est = layout(steps)
        clash = sum(len(v) - collections.Counter(r for r, _ in v).most_common(1)[0][1]
                    for v in pos.values())
        gaps = (max(pos) + 1 - len(pos)) if pos else 0
        # moving frames off the thumb's reading costs too, or runs of identical rows
        # (6 stacks of Wizard Elixir) would fold into a shorter list for free
        moved = sum(t != e for t, e in zip(tops, est) if t is not None)
        return clash + 2 * gaps + 0.5 * moved

    if all(f is None for f, _ in frames):
        frames = [(0.0, rows) for _, rows in frames]   # no thumb seen: a list too short to scroll
    scrolled = any(f for f, _ in frames if f is not None)
    steps = min(range(1, 101), key=score) if scrolled else 0
    pos, known, _ = layout(steps)

    # frames whose thumb was hidden (tooltip, cursor): put them where their rows fit best,
    # nearest to the last known scroll position
    best = consensus(pos)
    last, tops = 0, []
    for (f, rows), top in zip(frames, known):
        if top is not None:
            last = top
        elif rows:
            top = max(range(steps + 2), key=lambda t: fit(rows, t, best, last))
            if fit(rows, top, best, last)[0] > 0:
                for k, row, q in rows:
                    pos[top + k].append((row, q))
                last = top
        tops.append(last)          # a frame with every row hidden: the list hasn't moved
    return pos, tops


def hovered_slot(tip_box, title, on_screen):
    """Which visible row (slot) the tooltip belongs to, or None.
    The tooltip opens below the cursor (its top just under the hovered row) or, with no
    room below, above it (its bottom on the hovered row); near the bottom of the list the
    two cases overlap, so rows whose name matches the title decide, nearest the estimate.
    on_screen: {slot: list name}."""
    x, y, w, h = tip_box
    edge = y if y > LIST_Y0 else y + h
    est = (edge - 10 - LIST_Y0) // ROW_H
    named = [k for k, n in on_screen.items() if title and title_matches(title, n)]
    if named:
        return min(named, key=lambda k: abs(k - est))
    return None


def title_matches(title, name):
    return title.lower().startswith(name.lower()[:max(8, len(name) - 2)])


def item_name(title, name):
    """The full name from the tooltip, unless OCR cut the title shorter than the list name
    ("Dark Require" for "Dark Requiem")."""
    base = re.sub(r"\s*\(\+\d+\)$", "", title)
    return title if len(base) >= len(name) else name + title[len(base):]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--fps", type=float, default=4)
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    tips = collections.defaultdict(collections.Counter)          # shop -> Counter(json(title,stats))
    shop_titles = {}
    log = []

    n_frames = 0
    last_shop = None                                             # (shop, timestamp) of last title read
    for idx, ts, fr in iter_frames(a.video, a.fps):
        n_frames += 1
        if n_frames % 120 == 0:
            print(f"  {ts/60:5.1f} min of video processed", flush=True)
        t = read_title(fr) if shop_window_open(fr) else None
        if t:
            owner, stitle = t
            shop = owner
            shop_titles.setdefault(shop, collections.Counter())[stitle] += 1
            last_shop = (shop, ts)
            tip = find_tooltip(fr)
        else:
            # A tooltip over the title bar hides the shop name. If the rest of the window is
            # still there and a shop was read moments ago, this is that shop with an item hovered.
            tip = find_tooltip(fr)
            if not (tip and last_shop and ts - last_shop[1] <= CARRY_SHOP_S
                    and shop_window_open(fr, tip)):
                continue
            shop = last_shop[0]
            last_shop = (shop, ts)                  # a long hover keeps the shop
        tip_job = POOL.submit(read_tooltip, fr, tip) if tip else None   # runs alongside the rows
        read = read_list(fr, tip)
        rows = [(n, p) for n, p, _, _, _ in read]
        title, stats = tip_job.result() if tip_job else (None, None)
        if title:
            tips[shop][json.dumps([title, stats], sort_keys=True)] += 1
        log.append({"t": round(ts, 2), "f": idx, "shop": shop, "rows": rows,
                    "slots": [r[2] for r in read], "qty": [r[3] for r in read],
                    "rate": [r[4] for r in read],
                    "scroll": read_thumb(fr), "tooltip": bool(tip),
                    "tip_box": [int(v) for v in tip] if tip else None, "tip_title": title or None,
                    "tip_stats": stats if title else None})

    # canonicalise OCR variants per shop, place every row at its position in the shop's list
    # (scrollbar), then vote per position
    out_rows = []
    for shop in dict.fromkeys(e["shop"] for e in log):
        entries = [e for e in log if e["shop"] == shop]
        names = {n for e in entries for n, _ in e["rows"]}
        def canon(n):
            parts = n.rsplit(" ", 1)
            if len(parts) == 2 and len(parts[1]) <= 3 and parts[0] in names:
                return parts[0]                    # "Maple Nishada 1" -> "Maple Nishada"
            for m in names:                        # "...ATT 709" -> "...ATT 70"
                if m != n and n.startswith(m) and 0 < len(n) - len(m) <= 2:
                    return m
            return n
        name_price = collections.defaultdict(collections.Counter)
        for e in entries:
            for n, p in e["rows"]:
                name_price[canon(n)][p] += 1
        best_price = {n: pc.most_common(1)[0][0] for n, pc in name_price.items()}

        # one visit = an unbroken run of frames of this shop; a shop opened twice is read
        # from the visit that saw the most of its list (no double counting)
        visits, prev = [], None
        for e in log:
            if e["shop"] != shop:
                prev = None
                continue
            if prev is None or e["t"] - prev["t"] > CARRY_SHOP_S:
                visits.append([])
            visits[-1].append(e)
            prev = e

        def frames_of(visit):          # per row: (slot, (name, price), (qty, scroll rate))
            return [(e.get("scroll"), [(k, (canon(n), best_price[canon(n)]), (q, r))
                                       for (n, _), k, q, r in zip(e["rows"], e["slots"], e["qty"],
                                                                  e.get("rate") or [None] * len(e["rows"]))])
                    for e in visit]

        def winners(pos):
            return {p: collections.Counter(r for r, _ in v).most_common(1)[0] for p, v in pos.items()}

        placed = [place_rows(frames_of(v)) for v in visits]
        pos = max(reversed([p for p, _ in placed]),
                  key=lambda pos: sum(c >= 2 for _, c in winners(pos).values()))
        win = winners(pos)

        # tooltips: bind each to the row it was hovered on, so two copies of an item
        # (Red Sauna Robe +9 and +4) keep their own stats
        pos_tips = collections.defaultdict(collections.Counter)
        unbound = collections.Counter()
        for visit, (vpos, tops) in zip(visits, placed):
            vwin = winners(vpos)
            for e, top in zip(visit, tops):
                if not e.get("tip_title"):
                    continue
                key = json.dumps([e["tip_title"], e["tip_stats"]], sort_keys=True)
                on_screen = {k: vwin[top + k][0][0] for k in range(N_ROWS) if top + k in vwin}
                k = hovered_slot(e["tip_box"], e["tip_title"], on_screen)
                if k is None:
                    unbound[key] += 1
                else:
                    pos_tips[top + k][key] += 1
        names_at = collections.Counter(n for (n, _), _ in win.values())

        for p in sorted(pos):
            (name, price), c = win[p]
            full, stats, confirmed = name, {}, False
            if pos_tips[p]:
                title, stats = json.loads(pos_tips[p].most_common(1)[0][0])
                full, confirmed = item_name(title, name), True
            elif names_at[name] == 1:
                # a tooltip whose row couldn't be told (list position unknown): only safe
                # when the shop has a single row of that name
                for js, _ in unbound.most_common():
                    title, st = json.loads(js)
                    if title_matches(title, name):
                        full, stats, confirmed = item_name(title, name), st, True
                        break
            # keep a position read in >=2 frames, or once if a tooltip confirms the name
            if c < 2 and not confirmed:
                continue
            mine = [q for r, q in pos[p] if r == (name, price)]
            qtys = collections.Counter(q for q, _ in mine if q is not None)
            qty = qtys.most_common(1)[0][0] if qtys else 1     # icon always hidden: count 1
            rates = collections.Counter(rt for _, rt in mine if rt is not None)
            rate = rates.most_common(1)[0][0] if rates else ""
            out_rows.append({
                "shop_owner": shop,
                "shop_title": shop_titles[shop].most_common(1)[0][0],
                "list_name": name, "item": full, "price": price,
                # listed prices are per piece; qty is the stock left (0 = sold out)
                "qty": qty, "unit_price": price if qty else "",
                "stats": "; ".join(f"{k}: {v}" for k, v in stats.items()),
                # the recorder hovered this item (its tooltip was read): the report
                # trusts a high price of a hovered item as real, not a placeholder
                "hovered": int(confirmed),
                # a scroll's success rate read from its icon colour (completes cut-off names)
                "scroll_rate": rate,
                "frames_seen": c})

    with open(os.path.join(a.out, "items.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["shop_owner", "shop_title", "list_name", "item",
                                          "price", "qty", "unit_price", "stats", "hovered",
                                          "scroll_rate", "frames_seen"])
        w.writeheader()
        w.writerows(sorted(out_rows, key=lambda r: (r["shop_owner"], -r["price"])))
    with open(os.path.join(a.out, "frames_log.json"), "w") as f:
        json.dump(log, f, indent=1)
    with open(os.path.join(a.out, "tooltips.json"), "w", encoding="utf-8") as f:
        json.dump({s: [json.loads(k) + [v] for k, v in c.most_common()] for s, c in tips.items()},
                  f, indent=1, ensure_ascii=False)
    print(f"frames={n_frames} shop_frames={len(log)} items={len(out_rows)}")
    print(f"ocr calls: tesseract={OCR_STATS['tesseract']} cached={OCR_STATS['cached']}")
    print(f"saved -> {os.path.join(a.out, 'items.csv')}")


if __name__ == "__main__":
    main()
