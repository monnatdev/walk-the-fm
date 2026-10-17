#!/usr/bin/env python3
"""
Store one fm_ocr.py result in the price history database.

    python ingest.py <items.csv> [--date YYYY-MM-DD] [--video NAME] [--db fm.db]

One snapshot per video: ingesting the same video again replaces its listings,
so re-running OCR on a recording and re-ingesting is safe.
--video defaults to the result folder name without "_result" (run.bat's naming);
--date defaults to a YYYY-MM-DD found in the video name, else today.
"""
import argparse, csv, datetime, json, os, re, sqlite3, sys
from normalize import learn, load_json, ITEMS_JSON

HERE = os.path.dirname(os.path.abspath(__file__))

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY,
    captured_at TEXT NOT NULL,            -- YYYY-MM-DD
    video_name  TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS listings (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    shop_owner  TEXT,
    shop_title  TEXT,
    item        TEXT,
    list_name   TEXT,
    price       INTEGER,
    qty         INTEGER,                  -- stock left in the shop; 0 = sold out
    unit_price  INTEGER,                  -- = price (prices are per piece); NULL when sold out
    stats       TEXT,
    hovered     INTEGER DEFAULT 0         -- 1: the recorder hovered it (tooltip read)
);
CREATE INDEX IF NOT EXISTS listings_item ON listings(item);
CREATE INDEX IF NOT EXISTS listings_snapshot ON listings(snapshot_id);
"""


def connect(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(listings)")}
    if "hovered" not in cols:                 # databases made before the column existed
        db.execute("ALTER TABLE listings ADD COLUMN hovered INTEGER DEFAULT 0")
    return db


def hovered_from_tooltips(csv_path):
    """For items.csv files written before the 'hovered' column: {(shop, list_name)} of rows
    whose tooltip was read, rebuilt from tooltips.json beside the CSV (same rule as fm_ocr)."""
    path = os.path.join(os.path.dirname(os.path.abspath(csv_path)), "tooltips.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        tips = json.load(f)
    return {shop: [t[0].lower() for t in entries] for shop, entries in tips.items()}


def was_hovered(row, tips):
    if "hovered" in row and row["hovered"] not in ("", None):
        return int(row["hovered"])
    if not tips:
        return 0
    name = row["list_name"].lower()
    return int(any(t.startswith(name[:max(8, len(name) - 2)]) for t in tips.get(row["shop_owner"], [])))


def guess_video(csv_path):
    folder = os.path.basename(os.path.dirname(os.path.abspath(csv_path)))
    return re.sub(r"_result$", "", folder)


def guess_date(video):
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", video)
    if m:
        y, mo, d = map(int, m.groups())
        if y > 2400:                  # Thai Windows names files in the Buddhist era (2569 = 2026)
            y -= 543
        try:
            return datetime.date(y, mo, d).isoformat()
        except ValueError:
            pass
    return datetime.date.today().isoformat()


def complete_by_rate(name, rate, known):
    """A scroll name the list cut off, completed with the success rate read from its icon
    colour: "Scroll for Overall Armor for INT 1" + 10 -> "...INT 10%". Unchanged unless
    exactly one known item fits."""
    if not rate or re.search(r"\d+%$", name):
        return name
    start, end = name.casefold(), f" {rate}%"
    hits = {k for k in known if k.casefold().startswith(start) and k.endswith(end)}
    return hits.pop() if len(hits) == 1 else name


def to_int(v):
    return int(v) if str(v).strip() not in ("", "None") else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--date", help="capture date YYYY-MM-DD")
    ap.add_argument("--video", help="video name (snapshot key)")
    ap.add_argument("--db", default=os.path.join(HERE, "fm.db"))
    a = ap.parse_args()

    video = a.video or guess_video(a.csv)
    date = a.date or guess_date(video)
    try:
        when = datetime.date.fromisoformat(date)
    except ValueError:
        sys.exit(f"--date must be YYYY-MM-DD, got {date!r}")
    if when.year > 2400:                      # a Buddhist-era date typed by hand
        date = when.replace(year=when.year - 543).isoformat()

    with open(a.csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"no shop listings were read from {video!r}; nothing saved. "
                 "Was the game window in focus while recording, at 1920x1080?")
    if rows and "qty" not in rows[0]:
        print("note: items.csv has no qty column (older fm_ocr.py); assuming qty=1")

    db = connect(a.db)
    with db:                                  # one transaction: all or nothing
        old = db.execute("SELECT id FROM snapshots WHERE video_name = ?", (video,)).fetchone()
        if old:
            db.execute("DELETE FROM listings WHERE snapshot_id = ?", (old[0],))
            db.execute("UPDATE snapshots SET captured_at = ? WHERE id = ?", (date, old[0]))
            sid = old[0]
        else:
            sid = db.execute("INSERT INTO snapshots (captured_at, video_name) VALUES (?, ?)",
                             (date, video)).lastrowid
        recs = []
        tips = None if "hovered" in rows[0] else hovered_from_tooltips(a.csv)
        known = [n for k in ("items", "auto") for n in load_json(ITEMS_JSON).get(k, [])]
        for r in rows:
            r["item"] = complete_by_rate(r["item"], r.get("scroll_rate"), known)
            price = to_int(r["price"])
            qty = to_int(r.get("qty", 1))
            qty = 1 if qty is None else qty
            # FM prices are per piece (confirmed in game); qty is the stock left in the shop.
            # Computed here rather than read from the CSV so older CSVs ingest the same way.
            recs.append((sid, r["shop_owner"], r["shop_title"], r["item"], r["list_name"],
                         price, qty, price if qty else None, r.get("stats", ""), was_hovered(r, tips)))
        db.executemany("INSERT INTO listings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", recs)

    added, review = learn(rows, source=video)

    n_snap, n_list = db.execute(
        "SELECT (SELECT COUNT(*) FROM snapshots), (SELECT COUNT(*) FROM listings)").fetchone()
    print(f"{'replaced' if old else 'added'} snapshot {sid}: video={video!r} date={date} "
          f"listings={len(recs)} (sold out: {sum(1 for r in recs if r[6] == 0)}, "
          f"hovered: {sum(r[9] for r in recs)})")
    print(f"db {a.db}: snapshots={n_snap} listings={n_list}")
    print(f"items.json: learned {len(added)} new names; {len(review)} names need review "
          f"-> review_names.csv")


if __name__ == "__main__":
    main()
