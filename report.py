#!/usr/bin/env python3
"""
Static HTML price report from fm.db (no server; open the file in a browser).

    python report.py [--db fm.db] [--out report.html] [--placeholder-min 100000000] [--fragment]

Per item (names normalised with items.json, see normalize.py), on the latest capture date:
average asking price of active listings (prices are per piece), average price of sold-out
listings, price range, number of listings, and the 7-day change of the daily average.
Tabs: High volume (most listings) and Most expensive.
Placeholder listings at/above --placeholder-min (sellers park e.g. one potion at 1000m so
the shop never sells out and closes) are left out unless the page's switch is on.
Item icons and Equip/Use/Setup/Etc come from GMS v83 data (icons.py); item stats only from
tooltips read in the recordings. The page text is English.
--fragment writes the page without <html>/<head>/<body> (for publishing as a web page).
"""
import argparse, collections, json, os, pathlib, re, sqlite3, sys
from normalize import Normalizer
from icons import item_info, mob_icon, EQUIP_TYPES, OTHER_TYPES

HERE = os.path.dirname(os.path.abspath(__file__))
ELLIPSIS = "…"
# the mushroom family: Mushmom is the logo, one mushroom per tab
MOBS = {"logo": 6130101, "volume": 1210102, "expensive": 2220100}


def load(db_path):
    if not os.path.exists(db_path):
        sys.exit(f"no database at {db_path}; run ingest.py first")
    db = sqlite3.connect(db_path)
    # A shop seen in several clips on one day counts once, from the clip ingested last:
    # its listings replace the earlier ones instead of being added to them.
    rows = db.execute("""
        SELECT s.captured_at, l.shop_owner, l.item, l.list_name, l.price, l.qty, l.stats, l.shop_title,
               COALESCE(l.hovered, 0)
        FROM listings l JOIN snapshots s ON s.id = l.snapshot_id
        WHERE s.id = (SELECT MAX(s2.id) FROM listings l2 JOIN snapshots s2 ON s2.id = l2.snapshot_id
                      WHERE s2.captured_at = s.captured_at AND l2.shop_owner = l.shop_owner)""").fetchall()
    n_snap = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    return rows, n_snap


# Stats as the game's tooltip names them, in its order. This server's stats differ from
# GMS data (e.g. Blue Sauna Robe), so only stats read from tooltips in the recordings are shown.
STAT_ORDER = ["STR", "DEX", "INT", "LUK", "MAX HP", "MAX MP", "WEAPON ATTACK", "MAGIC ATTACK",
              "WEAPON DEF", "MAGIC DEF", "ACCURACY", "AVOIDABILITY", "SPEED", "JUMP", "ATTACK SPEED"]
UPGRADES = ("NUMBER OF UPGRADES AVAILABLE", "REMAINING ENHANCEMENTS")
SLOTS = "UPGRADES LEFT"                  # how the page labels the upgrade slots, shown first


def piece_stats(st):
    """parse_stats() result -> the stats line of one piece: upgrade slots first, then stats."""
    if not st:
        return []
    return ([[SLOTS, st["slots"]]] if st.get("slots") is not None else []) + st["st"]


def parse_stats(text):
    """'CATEGORY: CAPE; INT: +8; I REQLEV: 41; ...' (OCR of the item tooltip) ->
    {"cat": "CAPE", "st": [["INT", "+8"], ...], "slots": "0"}; OCR junk keys are dropped."""
    out = {"st": []}
    found = {}
    for part in (text or "").split(";"):
        if ":" not in part:
            continue
        k, v = (s.strip() for s in part.split(":", 1))
        k = re.sub(r"\s+", " ", k.upper())
        if k.endswith("CATEGORY"):
            words = []
            for w in v.split():
                if not re.fullmatch(r"[A-Za-z][A-Za-z-]*", w) or w.upper() in ("MDEF", "ALL", "ATT"):
                    break                                  # set-effect text bleeding in
                words.append(w.upper())
            if words:
                out["cat"] = " ".join(words)
        elif any(k.endswith(u) for u in UPGRADES):
            m = re.match(r"\d+", v)
            if m:
                out["slots"] = m.group(0)
        elif "REQ" not in k:                               # "PA REQ INT: 0" is a requirement, not a stat
            # longest name first, so "ATTACK SPEED" is not read as "SPEED"
            key = next((s for s in sorted(STAT_ORDER, key=len, reverse=True)
                        if k == s or k.endswith(" " + s)), None)
            m = re.match(r"[+-]?\d+|FASTER|FAST|NORMAL|SLOWER|SLOW", v.upper())
            if key and m:
                found[key] = m.group(0)
    out["st"] = [[k, found[k]] for k in STAT_ORDER if k in found]
    return out if (out["st"] or "cat" in out) else None


def stat_range(reads):
    """Stats of every hovered piece of one item -> [[stat, "29–36"], ...]: an estimate for the
    pieces nobody hovered (unscrolled pieces of one item roll within a small range)."""
    vals = collections.defaultdict(list)
    for st in reads:
        for k, v in st:
            vals[k].append(v)
    out = []
    for k in [SLOTS] + STAT_ORDER:
        vs = vals.get(k)
        if not vs:
            continue
        nums = [int(v) for v in vs if re.fullmatch(r"[+-]?\d+", v)]
        if len(nums) == len(vs):
            sign = "+" if vs[0].startswith("+") else ""
            lo, hi = min(nums), max(nums)
            out.append([k, f"{sign}{lo}" if lo == hi else f"{sign}{lo}–{hi}"])
        else:
            out.append([k, "/".join(sorted(set(vs)))])
    return out


def build(rows, norm):
    dates = sorted({r[0] for r in rows})
    latest = dates[-1] if dates else None
    items = collections.defaultdict(lambda: {"days": collections.defaultdict(list),
                                             "today": [], "sold": [], "names": set(),
                                             "status": "exact"})
    unresolved = {}
    shops_today = set()
    seen = {}                                          # name -> latest tooltip read for it
    reads = collections.defaultdict(list)              # name -> stats of every hovered piece
    for date, shop, item, list_name, price, qty, stats, title, hov in rows:
        found, status, _ = norm.match_keep_upgrades(item)
        st = parse_stats(stats)
        name = found or item
        if status == "ambiguous":
            name = item + ELLIPSIS                     # truncated and could be several items
        if status in ("ambiguous", "unmatched"):
            unresolved[item] = status
        it = items[name]
        it["names"].add(list_name)
        it["status"] = status
        if date == latest:
            shops_today.add(shop)
        if st and (name not in seen or date >= seen[name]["date"]):
            seen[name] = dict(st, shop=shop, date=date)
        line = piece_stats(st)
        if line:
            reads[name].append(line)
        if not qty:                                    # sold out: what it sold for
            if date == latest:
                it["sold"].append({"shop": shop, "title": title or "", "price": price, "hov": hov})
            continue
        it["days"][date].append((price, hov))
        if date == latest:
            listing = {"shop": shop, "title": title or "", "price": price, "qty": qty, "hov": hov}
            if line:
                listing["st"] = line
            it["today"].append(listing)

    info = item_info(list(items))
    out = []
    for name, it in items.items():
        meta = info.get(name, {})
        plus = (re.search(r"\(\+(\d+)\)$", name) or [None, None])[1]
        # scrolled pieces differ piece by piece, so no estimate for them
        est = {"st": stat_range(reads[name]), "n": len(reads[name])} if reads[name] and not plus else None
        out.append({
            "name": name,
            "inv": meta.get("inv") or "etc",
            "grp": meta.get("group") or "Etc",
            "type": meta.get("type") or "Other",
            "cls": meta.get("cls"),
            "icon": meta.get("icon"),
            "unsure": it["status"] in ("ambiguous", "unmatched"),
            "aka": sorted(n for n in it["names"] if n != name and n + ELLIPSIS != name),
            "days": [[d, sorted(v)] for d, v in sorted(it["days"].items())],
            "listings": sorted(it["today"], key=lambda l: l["price"]),
            "sold": sorted(it["sold"], key=lambda l: l["price"]),
            "seen": seen.get(name),
            "plus": plus,
            "est": est,
        })
    out.sort(key=lambda r: r["name"].casefold())
    mobs = {k: mob_icon(v) for k, v in MOBS.items()}
    logo = os.path.join(HERE, "logo.png")                # the site's own logo, if saved
    if os.path.exists(logo):
        import base64
        with open(logo, "rb") as f:
            mobs["logo"] = "data:image/png;base64," + base64.b64encode(f.read()).decode()
    banner = os.path.join(HERE, "bg.jpg")                # picture behind the page header, if saved
    if os.path.exists(banner):
        import base64
        with open(banner, "rb") as f:
            mobs["bg"] = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
    return out, dates, unresolved, len(shops_today), mobs


# Layout: a mushroom-cap strip, a quiet header (logo, name, search), then one white market
# card: two tabs, inventory filters, and a ranked table with a light header and orange rule.
# Rows become cards on phones.
PAGE = r"""<title>Walking in the Free Market</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fredoka:wght@500;600&family=Nunito:wght@400;600;700&display=swap">
<style>
/* Minimal maple: warm white paper, hairline lines, maple orange only as the accent */
:root {
  --bg: #faf8f5;
  --card: #ffffff;
  --soft: #f7f4ef;
  --line: #ece7df;
  --line-2: #f3efe9;
  --ink: #2a2622;
  --ink-2: #6b635a;
  --ink-3: #a59d93;
  --cap: #f07b22;             /* maple orange, the one accent */
  --cap-deep: #c75f10;
  --cap-soft: #fff4ea;
  --sold: #8e4f86;
  --up: #2c8a55;
  --down: #c8452c;
  --coin: #f3c43a;
  --coin-edge: #b07d0e;
  --slot: #f4f1ec;
  --shadow: 0 1px 2px rgba(60, 40, 20, 0.04), 0 4px 16px rgba(60, 40, 20, 0.04);
  --font-display: "Fredoka", "Nunito", "Segoe UI", system-ui, sans-serif;
  --font-body: "Nunito", "Segoe UI", system-ui, "Leelawadee UI", "Microsoft YaHei", sans-serif;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #141210; --card: #1c1916; --soft: #221e1a; --line: #2f2a24; --line-2: #26221d; --ink: #f3ece3;
  --ink-2: #c7bba9; --ink-3: #8b8174; --cap: #f58a38; --cap-deep: #f9a464; --cap-soft: #2d2017;
  --sold: #d993c9; --up: #6fcf94; --down: #ef866d; --coin: #f0c13c; --coin-edge: #9c6f0c; --slot: #e3ddd4;
  --shadow: 0 1px 2px rgba(0, 0, 0, 0.3), 0 4px 16px rgba(0, 0, 0, 0.2); color-scheme: dark;
}}
:root[data-theme="dark"] {
  --bg: #141210; --card: #1c1916; --soft: #221e1a; --line: #2f2a24; --line-2: #26221d; --ink: #f3ece3;
  --ink-2: #c7bba9; --ink-3: #8b8174; --cap: #f58a38; --cap-deep: #f9a464; --cap-soft: #2d2017;
  --sold: #d993c9; --up: #6fcf94; --down: #ef866d; --coin: #f0c13c; --coin-edge: #9c6f0c; --slot: #e3ddd4;
  --shadow: 0 1px 2px rgba(0, 0, 0, 0.3), 0 4px 16px rgba(0, 0, 0, 0.2); color-scheme: dark;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 var(--font-body);
  -webkit-font-smoothing: antialiased; }
img.px { image-rendering: pixelated; }
button { font: inherit; }

/* a thin maple line across the top */
.cap { height: 3px; background: var(--cap); }

main { max-width: 1120px; margin: 0 auto; padding-block: 32px 56px;
  padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; grid-template-columns: minmax(0, 1fr); gap: 24px; }
main > * { min-width: 0; }

.top { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 16px 32px; }
/* banner picture (bg.jpg beside report.py, a wide scene) across the top, edge to edge;
   its bottom fades into the page under the frosted header panel */
:root { --hero-h: clamp(400px, 29vw, 560px); }      /* taller on wide screens, so less is cropped */
.hero { display: none; position: absolute; top: 3px; left: 0; right: 0; height: var(--hero-h); z-index: 0; pointer-events: none;
  background: var(--hero) center 50% / cover no-repeat;
  -webkit-mask-image: linear-gradient(to bottom, #000 70%, transparent); mask-image: linear-gradient(to bottom, #000 70%, transparent); }
.has-hero .hero { display: block; }
.has-hero main { position: relative; z-index: 1; padding-top: calc(var(--hero-h) - 88px); }
.has-hero .top { padding: 12px 18px; border-radius: 16px; background: rgba(255, 255, 255, 0.86);
  -webkit-backdrop-filter: blur(8px); backdrop-filter: blur(8px); box-shadow: var(--shadow); }
.has-hero .top .search input { background: rgba(255, 255, 255, 0.9); }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) .has-hero .top { background: rgba(28, 25, 22, 0.82); }
  :root:not([data-theme="light"]) .hero { filter: brightness(0.7); } }
:root[data-theme="dark"] .has-hero .top { background: rgba(28, 25, 22, 0.82); }
:root[data-theme="dark"] .hero { filter: brightness(0.7); }
.brand { display: flex; align-items: center; gap: 14px; }
.brand img { width: 46px; height: 60px; object-fit: contain; }
.brand h1 { margin: 0; font: 600 26px/1.1 var(--font-display); letter-spacing: -0.005em; }
.brand h1 b { color: var(--cap); font-weight: 600; }
.brand p { margin: 4px 0 0; font-size: 13px; color: var(--ink-3); }
.search { position: relative; flex: 1 1 300px; max-width: 420px; }
.search input { width: 100%; padding: 10px 40px 10px 40px; font: inherit; font-size: 14px; color: var(--ink);
  background: var(--card); border: 1px solid var(--line); border-radius: 12px; transition: border-color .15s, box-shadow .15s; }
.search input::placeholder { color: var(--ink-3); }
.search input:focus { outline: none; border-color: var(--cap); box-shadow: 0 0 0 3px var(--cap-soft); }
.search svg { position: absolute; left: 14px; top: 50%; translate: 0 -50%; width: 16px; height: 16px;
  stroke: var(--ink-3); fill: none; stroke-width: 2.2; }
.search kbd { position: absolute; right: 12px; top: 50%; translate: 0 -50%; font: 600 11px/1 var(--font-body);
  color: var(--ink-3); background: var(--soft); border: 1px solid var(--line); border-radius: 5px; padding: 3px 6px; }

.market { background: var(--card); border: 1px solid var(--line); border-radius: 16px; box-shadow: var(--shadow); overflow: hidden; }
.head { display: grid; gap: 14px; padding: 18px 20px 0; }
/* tabs: one quiet segmented control */
.tabs { display: inline-flex; flex-wrap: wrap; gap: 2px; padding: 3px; background: var(--soft);
  border: 1px solid var(--line-2); border-radius: 12px; justify-self: start; }
.tab { display: inline-flex; align-items: center; gap: 6px; padding: 5px 14px 5px 8px; border-radius: 9px;
  font: 600 14px/1 var(--font-body); color: var(--ink-2); background: none; border: 0; cursor: pointer;
  transition: background .15s, color .15s; }
.tab img { width: 24px; height: 24px; object-fit: contain; }
.tab[aria-selected="true"] { background: var(--card); color: var(--ink); box-shadow: 0 1px 3px rgba(60, 40, 20, 0.1); }
.tab:not([aria-selected="true"]):hover { color: var(--ink); }
.sub { display: flex; flex-wrap: wrap; gap: 8px 18px; align-items: flex-end; justify-content: flex-end;
  border-bottom: 1px solid var(--line); }
/* Group / Type / Class chip rows, like the server's item site */
.filters { display: grid; gap: 8px; }
.frow { display: grid; grid-template-columns: 52px minmax(0, 1fr); gap: 10px; align-items: start; }
.flabel { font: 700 11px/1.2 var(--font-body); letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-3); padding-top: 8px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 11px; border-radius: 999px; cursor: pointer;
  font: 600 13px/1.2 var(--font-body); color: var(--ink-2); background: var(--soft); border: 1px solid var(--line-2);
  transition: background .15s, color .15s, border-color .15s; }
.chip:hover { color: var(--ink); border-color: var(--line); }
.chip .n { font-size: 11px; font-weight: 600; color: var(--ink-3); background: var(--card); border-radius: 999px; padding: 1px 6px; }
.chip[aria-pressed="true"] { background: var(--cap); border-color: var(--cap); color: #fff; }
.chip[aria-pressed="true"] .n { color: var(--cap-deep); }
.chip:focus-visible { outline: 2px solid var(--cap); outline-offset: 2px; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.dot.common { background: #6b635a; } .dot.warrior { background: #c8452c; } .dot.magician { background: #3d6fd6; }
.dot.bowman { background: #2c8a55; } .dot.thief { background: #8e4fa6; } .dot.pirate { background: #1f8ea3; }
.chip[aria-pressed="true"] .dot { box-shadow: 0 0 0 1.5px #fff; }
.inv { display: flex; flex-wrap: wrap; gap: 4px; }
.inv button { font: 600 13.5px/1 var(--font-body); color: var(--ink-3); background: none; border: 0;
  padding: 10px 8px 11px; border-bottom: 2px solid transparent; margin-bottom: -1px; cursor: pointer; transition: color .15s; }
.inv button:hover { color: var(--ink); }
.inv button .n { color: var(--ink-3); font-size: 11.5px; font-weight: 400; margin-left: 4px; }
.inv button[aria-pressed="true"] { color: var(--ink); border-bottom-color: var(--cap); }
.opts { display: flex; flex-wrap: wrap; gap: 6px 18px; align-items: center; font-size: 12.5px; color: var(--ink-2); padding-bottom: 10px; }
.switch { display: inline-flex; gap: 8px; align-items: center; cursor: pointer; }
.switch { position: relative; }
.switch input { position: absolute; left: 0; top: 0; width: 1px; height: 1px; margin: 0; opacity: 0; pointer-events: none; }
.switch i { width: 28px; height: 16px; border-radius: 8px; background: var(--line); position: relative; flex: none; transition: background .15s; }
.switch i::after { content: ""; position: absolute; top: 2px; left: 2px; width: 12px; height: 12px; border-radius: 50%;
  background: var(--card); box-shadow: 0 1px 2px rgba(0, 0, 0, 0.15); transition: translate .15s; }
.switch input:checked + i { background: var(--cap); }
.switch input:checked + i::after { translate: 12px 0; }
.tab:focus-visible, .inv button:focus-visible, .switch input:focus-visible + i, tr.item:focus-visible, thead th:focus-visible {
  outline: 2px solid var(--cap); outline-offset: 2px; }

.scroll { overflow-x: auto; }
/* fixed column widths, so filtering or toggling never makes the columns jump */
table { border-collapse: collapse; width: 100%; table-layout: fixed; min-width: 860px; }
thead th:nth-child(1) { width: 56px; }
thead th:nth-child(3), thead th:nth-child(4) { width: 150px; }
thead th:nth-child(5) { width: 140px; }
thead th:nth-child(6) { width: 100px; }
thead th:nth-child(7) { width: 96px; }
th, td { padding: 12px 16px; text-align: left; vertical-align: middle; }
thead th { color: var(--ink-3); font: 700 11px/1.2 var(--font-body); letter-spacing: 0.06em; text-transform: uppercase;
  white-space: nowrap; cursor: pointer; user-select: none; border-bottom: 1px solid var(--line); padding-block: 12px 10px; }
thead th:hover { color: var(--ink-2); }
thead th.rank { cursor: default; text-align: center; width: 56px; }
thead th .arr { display: inline-block; width: 8px; margin-right: 5px; opacity: 0.35; vertical-align: -1px; }
thead th .arr svg { display: block; width: 8px; height: 11px; fill: currentColor; }
thead th[aria-sort] { color: var(--cap-deep); }
thead th[aria-sort] .arr { opacity: 1; }
tbody tr.item { cursor: pointer; border-bottom: 1px solid var(--line-2); transition: background .12s; }
tbody tr.item:hover { background: var(--soft); }
tbody tr.item[aria-expanded="true"] { background: var(--soft); border-bottom-color: transparent; }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.rank { text-align: center; color: var(--ink-3); font-size: 13px; font-variant-numeric: tabular-nums; }
.medal { display: inline-grid; place-items: center; width: 24px; height: 24px; border-radius: 50%;
  font: 700 12px/1 var(--font-body); color: var(--cap-deep); background: var(--cap-soft); }
.medal.m1 { color: #fff; background: var(--cap); }
.who { display: flex; align-items: center; gap: 12px; min-width: 0; }
.who > div { min-width: 0; }
.slot { flex: none; width: 40px; height: 40px; display: grid; place-items: center; border-radius: 10px; background: var(--slot); }
.slot img { max-width: 34px; max-height: 34px; }
.slot .ph { font: 600 14px/1 var(--font-display); color: #8c7a60; }
.name { font-weight: 700; }
.name small { display: block; font-weight: 400; color: var(--ink-3); font-size: 12px; }
.tag { display: inline-block; margin-left: 6px; padding: 1px 7px; border-radius: 6px; font-size: 11px;
  font-weight: 600; color: var(--ink-2); background: var(--soft); border: 1px solid var(--line); vertical-align: 1px; }
.tag.warn { color: var(--cap-deep); background: var(--cap-soft); border-color: transparent; }
.tag.plus { color: #8a5a00; background: #fff1c2; border-color: transparent; }
.mesos { font-weight: 700; }
.mesos.sold { color: var(--sold); }
.coin { display: inline-block; width: 14px; height: 14px; margin-left: 5px; vertical-align: -2px; }
.coin circle:first-child { fill: var(--coin); stroke: var(--coin-edge); stroke-width: 1.5; }
.coin circle:last-child { fill: none; stroke: var(--coin-edge); stroke-width: 1; opacity: 0.6; }
.range { color: var(--ink-2); }
.none { color: var(--ink-3); font-weight: 400; }
.up { color: var(--up); font-weight: 700; }
.down { color: var(--down); font-weight: 700; }
small.cnt { color: var(--ink-3); font-weight: 400; margin-left: 3px; }

/* expanded row: a panel under the item, a maple line on its left */
tr.detail > td { background: var(--soft); padding: 4px 20px 22px 72px; border-bottom: 1px solid var(--line); }
.dwrap { display: flex; flex-wrap: wrap; gap: 20px 48px; align-items: flex-start;
  padding: 16px 20px; background: var(--card); border: 1px solid var(--line); border-left: 3px solid var(--cap); border-radius: 12px; }
.dwrap > div { min-width: 0; }
.dwrap h3 { margin: 0 0 8px; font: 700 11px/1.2 var(--font-body); color: var(--ink-2); letter-spacing: 0.06em; text-transform: uppercase; }
.shops { display: grid; min-width: min(100%, 380px); }
.shop { display: grid; grid-template-columns: 1fr auto auto; gap: 20px; padding: 8px 0; border-bottom: 1px solid var(--line-2); }
.shop:last-child { border-bottom: 0; }
.shop.ph { color: var(--ink-3); text-decoration: line-through; }
.shop .q { color: var(--ink-3); min-width: 56px; text-align: right; font-size: 13px; font-variant-numeric: tabular-nums; }
.shop.sh { font-size: 10.5px; font-weight: 700; color: var(--ink-3); letter-spacing: 0.06em; text-transform: uppercase;
  border-bottom: 1px solid var(--line); padding: 0 0 6px; }
.shop.sh .q { font-size: inherit; }
.owner { font-weight: 700; }
.st-line { display: block; font-size: 12px; color: var(--ink-3); font-weight: 400; margin-top: 1px; }
.lbl { font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; font-size: 10px; margin-right: 5px; color: var(--ink-2); opacity: 0.7; white-space: nowrap; }
.st-line.missing { font-style: italic; opacity: 0.85; }
.est-n { font-size: 10.5px; }
svg.spark { display: block; overflow: visible; }
svg.spark .area { fill: var(--cap); opacity: 0.12; }
svg.spark .line { fill: none; stroke: var(--cap); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
svg.spark .dot { fill: var(--cap); stroke: var(--card); stroke-width: 2; }
svg.spark .hit { fill: transparent; }
svg.spark text { fill: var(--ink-3); font: 11px var(--font-body); }
.empty { padding: 40px 16px; text-align: center; color: var(--ink-2); }
.more { display: flex; justify-content: center; }
.more button { margin: 16px 0 18px; font: 700 14px/1 var(--font-body); color: var(--cap-deep); background: var(--cap-soft);
  border: 1px solid transparent; border-radius: 999px; padding: 10px 22px; cursor: pointer; transition: border-color .15s; }
.more button:hover { border-color: var(--cap); }
.more button:focus-visible { outline: 2px solid var(--cap); outline-offset: 2px; }
.more button[hidden] { display: none; }
.brand p a { color: var(--cap-deep); font-weight: 700; text-decoration: none; }
.brand p a:hover { text-decoration: underline; }
/* support card: the owner's QR code (support_qr.png beside report.py) beside a feedback form */
.support { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 340px), 1fr)); gap: 20px 40px;
  padding: 22px 24px; background: var(--card); border: 1px solid var(--line); border-top: 3px solid var(--cap);
  border-radius: 16px; box-shadow: var(--shadow); scroll-margin-top: 16px; }
.support h2 { margin: 0 0 4px; font: 600 18px/1.3 var(--font-display); }
.support p { margin: 0; color: var(--ink-2); font-size: 14px; }
.give { display: flex; flex-direction: column; gap: 14px; }
.give[hidden] { display: none; }
.qrs { display: flex; flex-wrap: wrap; gap: 14px; }
.qrs figure { margin: 0; display: grid; gap: 6px; justify-items: center; }
.qrs img { width: 148px; height: 148px; object-fit: contain; padding: 6px; background: #fff;
  border: 1px solid var(--line); border-radius: 12px; }
.qrs figcaption { font-size: 12px; font-weight: 700; color: var(--ink-2); }
.fb { display: grid; gap: 8px; align-content: start; }
.fb p { margin-bottom: 2px; }
.fb textarea, .fb input { width: 100%; font: inherit; font-size: 14px; color: var(--ink); background: var(--bg);
  border: 1px solid var(--line); border-radius: 10px; padding: 9px 12px; resize: vertical; }
.fb textarea:focus, .fb input:focus { outline: none; border-color: var(--cap); box-shadow: 0 0 0 3px var(--cap-soft); }
.fb-row { display: flex; gap: 8px; }
.fb button { flex: none; font: 700 14px/1 var(--font-body); color: #fff; background: var(--cap); border: 0;
  border-radius: 10px; padding: 0 20px; cursor: pointer; }
.fb button:hover { background: var(--cap-deep); }
.fb button:disabled { opacity: 0.6; cursor: default; }
.fb button:focus-visible { outline: 2px solid var(--cap); outline-offset: 2px; }
.fb-status { font-size: 12.5px; min-height: 1em; }
.foot { color: var(--ink-3); font-size: 12px; margin: 0; text-align: center; }
#tip { position: fixed; pointer-events: none; background: var(--ink); color: var(--bg); padding: 5px 9px;
  border-radius: 6px; font-size: 12px; white-space: nowrap; z-index: 9; }

/* item card on hover, in the look of the game's own item tooltip (same in both themes) */
:root { --tt-bg: rgba(28, 32, 58, 0.96); --tt-line: #5b6390; --tt-title: #8fd3ff; --tt-ink: #f2f4ff;
  --tt-dim: #a7aed6; --tt-up: #ffd45c; }
#card { position: fixed; z-index: 10; pointer-events: none; width: 270px; padding: 12px 14px;
  background: var(--tt-bg); color: var(--tt-ink); border: 1px solid var(--tt-line); border-radius: 8px;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.35); font: 12.5px/1.55 var(--font-body); }
#card h4 { margin: 0 0 8px; text-align: center; color: var(--tt-title); font: 700 15px/1.25 var(--font-body); }
#card h4 .plus { color: var(--tt-up); }
#card .tt-top { display: flex; gap: 12px; align-items: center; padding-bottom: 8px; border-bottom: 1px dashed var(--tt-line); }
#card .tt-top .slot { width: 52px; height: 52px; background: #e9e6f2; }
#card .tt-top .slot img { max-width: 44px; max-height: 44px; }
#card .tt-cat { font-weight: 700; letter-spacing: 0.02em; }
#card ul { list-style: none; margin: 8px 0 0; padding: 0; text-transform: uppercase; letter-spacing: 0.02em; }
#card li b { font-weight: 700; }
#card .tt-note { margin: 8px 0 0; padding-top: 7px; border-top: 1px dashed var(--tt-line); color: var(--tt-dim); font-size: 11.5px; line-height: 1.45; }
@media (hover: hover) { .only-touch { display: none; } }   /* mouse users get the hover card */

@media (max-width: 680px) {
  main { padding-top: 20px; gap: 18px; }
  .hero { height: 260px; background-position: 47% 50%; }
  .has-hero main { padding-top: 196px; }
  .has-hero .top { padding: 12px; }
  .search { max-width: none; }
  .search kbd { display: none; }
  .head { padding: 14px 14px 0; }
  .opts { padding-bottom: 8px; }
  .frow { grid-template-columns: minmax(0, 1fr); gap: 4px; }
  .flabel { padding-top: 0; }
  .chips { flex-wrap: nowrap; overflow-x: auto; padding-bottom: 2px; scrollbar-width: none; }
  .chip { flex: none; }
  .support { padding: 20px 16px; }
  .give { text-align: center; }
  .qrs { justify-content: center; }
  thead { display: none; }
  table, tbody { display: block; min-width: 0; }
  tr.item { display: grid; grid-template-columns: 28px 1fr auto; gap: 4px 10px; padding: 14px; align-items: center; }
  tr.item td { display: block; padding: 0; }
  tr.item td.rank { grid-row: 1; }
  tr.item td.c-name { grid-column: 2 / -1; grid-row: 1; }
  tr.item td.num { text-align: left; }
  tr.item td.c-ask, tr.item td.c-sold, tr.item td.c-range { grid-column: 2; }
  tr.item td.c-n { grid-column: 3; grid-row: 2; text-align: right; }
  tr.item td.c-chg { grid-column: 3; grid-row: 3; text-align: right; }
  tr.item td.c-ask::before { content: "Avg. asking "; color: var(--ink-3); font-weight: 400; }
  tr.item td.c-sold::before { content: "Avg. sold "; color: var(--ink-3); font-weight: 400; }
  tr.item td.c-range::before { content: "Range "; color: var(--ink-3); }
  tr.item td.c-sold:has(.none), tr.item td.c-range:has(.none) { display: none; }
  tr.item td.c-n::after { content: " listed"; color: var(--ink-3); }
  tr.item td.c-chg .none { display: none; }
  tr.detail { display: block; }
  tr.detail > td { display: block; padding: 0 10px 12px; }
  .dwrap { padding: 12px 14px; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>

<div class="cap" aria-hidden="true"></div>
<div class="hero" id="hero" aria-hidden="true"></div>
<main>
  <header class="top">
    <div class="brand">
      <img id="logo" alt="" hidden>
      <div>
        <h1>Walking in the <b>Free Market</b></h1>
        <p>Old School Maple prices &middot; updated <span id="latest"></span> &middot; <span id="stats"></span><span id="supportLink" hidden> &middot; <a href="#support">&#9829; Support this site</a></span></p>
      </div>
    </div>
    <label class="search">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="m20 20-4-4"></path></svg>
      <input type="search" id="q" placeholder="Search items, shop owners or shop titles" autocomplete="off" aria-label="Search items, shop owners or shop titles">
      <kbd>/</kbd>
    </label>
  </header>

  <section class="market" aria-label="Item prices">
    <div class="head">
      <div class="tabs" id="tabs" role="tablist"></div>
      <div class="filters" id="filters"></div>
      <div class="sub">
        <div class="opts">
          <label class="switch"><input type="checkbox" id="todayOnly" checked><i></i>Latest day only</label>
          <label class="switch" title="Some sellers list one item at an absurd price so their shop never sells out and closes">
            <input type="checkbox" id="withPh"><i></i>Include placeholder prices</label>
        </div>
      </div>
    </div>
    <div class="scroll">
      <table>
        <thead><tr>
          <th class="rank">#</th>
          <th data-k="name" tabindex="0">__ARR__Item</th>
          <th data-k="avg" class="num" tabindex="0" title="Average asking price of active listings">__ARR__Avg. asking</th>
          <th data-k="soldAvg" class="num" tabindex="0" title="Average price of listings that sold out">__ARR__Avg. sold</th>
          <th data-k="low" class="num" tabindex="0" title="Cheapest to most expensive active listing">__ARR__Price range</th>
          <th data-k="n" class="num" tabindex="0">__ARR__Listings</th>
          <th data-k="chg" class="num" tabindex="0" title="Change of the daily average over the last 7 days">__ARR__7d</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
    <div class="more"><button type="button" id="more" hidden></button></div>
    <div class="empty" id="empty" hidden>No items match your search. Try a shorter word, or pick "All".</div>
  </section>
  <section class="support" id="support" aria-label="Support and feedback">
    <div class="give" id="give" hidden>
      <div>
        <h2>Enjoying this site?</h2>
        <p>It's free and always will be. Keeping prices fresh takes hours of recording and checking,
          so if it helps you, a small tip keeps it going. Thank you!</p>
      </div>
      <div class="qrs" id="qrs"></div>
    </div>
    <form class="fb" id="fb">
      <h2>Feedback</h2>
      <p>Wrong price, missing item, or an idea? Let me know.</p>
      <textarea id="fbMsg" name="message" rows="3" maxlength="2000" required placeholder="Your feedback" aria-label="Your feedback"></textarea>
      <div class="fb-row">
        <input id="fbWho" name="contact" maxlength="100" placeholder="IGN or Discord (optional)" aria-label="IGN or Discord (optional)">
        <button type="submit">Send</button>
      </div>
      <p class="fb-status" id="fbStatus" role="status"></p>
    </form>
  </section>
  <p class="foot" id="count"></p>
</main>
<div id="tip" hidden></div>
<div id="card" hidden></div>

<script>
const DATA = __DATA__;
const META = __META__;
const PH = META.placeholderMin;
// filters in the server item site's order (oldschoolmaplegame.com/items)
const GROUPS = ["Weapons", "Armor", "Accessories", "Use", "Monster Cards", "Etc", "Setup", "Cash"];
const TYPE_ORDER = META.types;
const CLASSES = ["Common", "Warrior", "Magician", "Bowman", "Thief", "Pirate"];
const TABS = [
  {id: "volume", label: "High volume", sort: ["n", -1]},
  {id: "expensive", label: "Most expensive", sort: ["avg", -1]},
];
const $ = id => document.getElementById(id);
const fmt = v => v == null ? "—" : Math.round(v).toLocaleString("en-US");
// 999,999 -> "1m" (not "1000k"): switch units where rounding would reach 1000
const short = v => v >= 999.5e6 ? +(v / 1e9).toFixed(2) + "b" : v >= 999.5e3 ? +(v / 1e6).toFixed(2) + "m" : v >= 1e3 ? +(v / 1e3).toFixed(1) + "k" : String(v);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const mean = a => a.length ? a.reduce((s, v) => s + v, 0) / a.length : null;
const COIN = '<svg class="coin" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.5"></circle><circle cx="8" cy="8" r="3.6"></circle></svg>';
const dayNum = d => Date.parse(d + "T00:00:00Z") / 864e5;
let tab = TABS[0], grp = null, typ = null, cls = null, [sortK, sortDir] = tab.sort;
// rows drawn at once; "Show more" adds a page. Any new filter, search or sort starts over.
const PAGE_ROWS = 100;
let limit = PAGE_ROWS;
const refilter = () => { limit = PAGE_ROWS; render(); };
const open = new Set();

const median = a => { const s = [...a].sort((x, y) => x - y), n = s.length;
  return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2; };
// Placeholder price (a seller parks one item at an absurd price so the shop never sells
// out and closes), any of:
//   at/above PH (near the game's maximum), even if hovered;
//   at/above SOFT and not hovered while recording (hovering marks a high price as real);
//   RATIO x the median of the other prices for the same item that day.
const RATIO = META.placeholderRatio, SOFT = META.placeholderUnhovered;
const phMask = (prices, hovs) => prices.map((p, i) => {
  if (p >= PH) return true;
  if (p >= SOFT && !hovs[i]) return true;
  const others = prices.filter((_, j) => j !== i);
  return others.length > 0 && p >= RATIO * median(others);
});

function stats(r, withPh) {
  const all = r.listings.concat(r.sold);
  const today = phMask(all.map(l => l.price), all.map(l => l.hov));
  const phL = today.slice(0, r.listings.length), phS = today.slice(r.listings.length);
  const live = r.listings.filter((l, i) => withPh || !phL[i]);
  const prices = live.map(l => l.price);
  const sold = r.sold.filter((l, i) => withPh || !phS[i]).map(l => l.price);
  const series = [];
  for (const [d, ps] of r.days) {                 // ps: [[price, hovered], ...] sorted by price
    const m = phMask(ps.map(x => x[0]), ps.map(x => x[1]));
    const v = ps.filter((x, i) => withPh || !m[i]).map(x => x[0]);
    if (v.length) series.push([d, mean(v), v[0], v.length]);
  }
  let chg = null;                         // latest daily average vs the oldest one in the 7 days before
  if (series.length > 1 && series[series.length - 1][0] === META.latest) {
    const now = series[series.length - 1], t = dayNum(now[0]);
    const base = series.find(p => p !== now && t - dayNum(p[0]) <= 7);
    if (base) chg = (now[1] - base[1]) / base[1] * 100;
  }
  return { avg: mean(prices), low: prices.length ? prices[0] : null, high: prices.length ? prices[prices.length - 1] : null,
    n: live.length, soldAvg: mean(sold), soldN: sold.length, series, chg, phL, phS,
    nph: phL.filter(Boolean).length + phS.filter(Boolean).length };
}

function spark(points) {
  if (!points.length) return "";
  const W = 220, H = 56, P = 6;
  const vals = points.map(p => p[1]);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const x = i => points.length === 1 ? W / 2 : P + i * (W - 2 * P) / (points.length - 1);
  const y = v => hi === lo ? H / 2 - 6 : H - 16 - (v - lo) * (H - 24) / (hi - lo);
  const pts = points.map((p, i) => [x(i), y(p[1])]);
  const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join("");
  const area = points.length > 1 ? `<path class="area" d="${line}L${pts[pts.length - 1][0]},${H - 12}L${pts[0][0]},${H - 12}Z"></path>` : "";
  const hits = points.map((p, i) => `<rect class="hit" x="${x(i) - 10}" y="0" width="20" height="${H}"
      data-tip="${esc(p[0])}: avg ${fmt(p[1])} · low ${fmt(p[2])} (${p[3]} listing${p[3] === 1 ? "" : "s"})"></rect>`).join("");
  const last = pts[pts.length - 1];
  const label = points.length > 1 ? `<text x="${P}" y="${H}">${esc(points[0][0])}</text><text x="${W - P}" y="${H}" text-anchor="end">${esc(points[points.length - 1][0])}</text>`
    : `<text x="${W / 2}" y="${H}" text-anchor="middle">${esc(points[0][0])} (one day so far)</text>`;
  return `<svg class="spark" width="${W}" height="${H}" role="img" aria-label="Daily average price, ${points.length} day(s)">` +
    area + (points.length > 1 ? `<path class="line" d="${line}"></path>` : "") +
    `<circle class="dot" cx="${last[0]}" cy="${last[1]}" r="3.5"></circle>` + label + hits + `</svg>`;
}

const slot = r => `<span class="slot">${r.icon ? `<img class="px" src="${r.icon}" alt="" loading="lazy">`
  : `<span class="ph">${esc(r.name.replace(/^\W+/, "").charAt(0) || "?")}</span>`}</span>`;
const money = (v, cls = "") => v == null ? `<span class="none">—</span>` : `<span class="mesos ${cls}">${fmt(v)}</span>${COIN}`;
const statText = st => st.map(([k, v]) => `${k} ${v}`).join(" · ");
const statList = st => st.map(([k, v]) => `<li>${esc(k)} : <b>${esc(v)}</b></li>`).join("");

// Card shown on hover (and inside the expanded row on phones). Only stats read from the
// game's tooltip in the recordings are shown: this server's items differ from GMS data.
function cardHTML(r) {
  const s = r.seen;
  const title = `${esc(r.name.replace(/\s*\(\+\d+\)$/, ""))}${r.plus ? ` <span class="plus">(+${r.plus})</span>` : ""}`;
  const top = `<div class="tt-top">${slot(r)}<div><div class="tt-cat">${esc(s && s.cat ? s.cat : r.inv.toUpperCase())}</div>` +
    (s && s.slots != null ? `<div>UPGRADES LEFT : ${esc(s.slots)}</div>` : "") + `</div></div>`;
  let body = "", note;
  if (s && s.st.length) {
    body = `<ul>${statList(s.st)}</ul>`;
    note = `Stats of the piece in ${esc(s.shop)}'s shop (${esc(s.date)})` +
      (r.plus ? ". Scrolled items differ piece by piece." : r.inv === "equip" ? ". Equipment stats can vary slightly between pieces." : ".");
  } else {
    note = r.inv === "equip" ? "No stats recorded for this item yet." : "No stats for this item.";
  }
  return `<h4>${title}</h4>${top}${body}<p class="tt-note">${note}</p>`;
}

// owner name, then the shop's own title (often its channel / FM room, e.g. "C1 FM2 ...")
const owner = l => `<span class="owner">${esc(l.shop)}</span>` +
  (l.title ? `<span class="st-line"><span class="lbl">Shop name</span>“${esc(l.title)}”</span>` : "");
const shopHead = (price, last) => `<div class="shop sh"><span>Character (shop owner)</span><span class="num">${price}</span><span class="q">${last}</span></div>`;

function detail(r, withPh) {
  const shops = r.listings.length ? shopHead("Price each", "Stock") + r.listings.map((l, i) => {
    const ph = r.phL[i];
    const why = l.price >= PH ? `${short(PH)}+` : l.price >= SOFT && !l.hov ? `${short(SOFT)}+, not hovered` : `${RATIO}× other shops`;
    return `<div class="shop${ph && !withPh ? " ph" : ""}"><span>${owner(l)}${ph ? `<span class="tag warn" title="Counted as a placeholder: ${why}">placeholder?</span>` : ""}` +
      `${l.st ? `<span class="st-line"><span class="lbl">Stats</span>${esc(statText(l.st))}</span>`
        : r.est ? `<span class="st-line missing" title="Not hovered in the recording. Range of the ${r.est.n} hovered piece${r.est.n === 1 ? "" : "s"} of this item, not this piece's own stats."><span class="lbl">Stats (est.)</span>${esc(statText(r.est.st))}` +
          `<span class="est-n"> · range of ${r.est.n} hovered piece${r.est.n === 1 ? "" : "s"}</span></span>`
        : r.inv === "equip" ? `<span class="st-line missing"><span class="lbl">Stats</span>not hovered in the recording</span>` : ""}</span>
      <span class="num mesos">${fmt(l.price)}</span><span class="q">${l.qty} left</span></div>`;
  }).join("") : `<div class="none">Not listed on the latest day.</div>`;
  const sold = r.sold.length ? `<div><h3>Sold out (${r.sold.length})</h3><div class="shops">${shopHead("Sold for", "")}${r.sold.map(l =>
    `<div class="shop"><span>${owner(l)}</span><span class="num mesos sold">${fmt(l.price)}</span><span class="q">sold</span></div>`).join("")}</div></div>` : "";
  const card = r.seen && r.seen.st.length
    ? `<div class="only-touch"><h3>Stats (from ${esc(r.seen.shop)}'s shop)</h3><div class="st-line">${esc(statText(r.seen.st))}</div></div>` : "";
  return `<tr class="detail"><td colspan="7"><div class="dwrap">
    <div><h3>Where to buy · ${esc(META.latest)}</h3><div class="shops">${shops}</div></div>
    ${sold}${card}<div><h3>Price history</h3>${spark(r.series)}</div></div></td></tr>`;
}

function render() {
  const terms = $("q").value.toLowerCase().split(/\s+/).filter(Boolean);
  const todayOnly = $("todayOnly").checked, withPh = $("withPh").checked;
  const all = DATA.map(r => Object.assign({}, r, stats(r, withPh)))
    .filter(r => !todayOnly || r.listings.length || r.sold.length)
    .filter(r => {
      const hay = (r.name + " " + r.aka.join(" ") + " " + r.listings.map(l => l.shop + " " + l.title).join(" ")).toLowerCase();
      return terms.every(t => hay.includes(t));
    });
  $("tabs").innerHTML = TABS.map(t => `<button type="button" class="tab" role="tab" data-tab="${t.id}" aria-selected="${t === tab}">` +
    (META.mobs[t.id] ? `<img class="px" src="${META.mobs[t.id]}" alt="">` : "") + `${t.label}</button>`).join("");
  // Group -> Type -> Class; each row counts what the rows above it leave
  const tally = (list, key) => list.reduce((m, r) => { for (const v of [].concat(r[key] || [])) m[v] = (m[v] || 0) + 1; return m; }, {});
  const chip = (kind, val, label, n, on, dot = "") => `<button type="button" class="chip" data-f="${kind}" data-v="${esc(val)}" aria-pressed="${on}">` +
    `${dot ? `<i class="dot ${dot}"></i>` : ""}${esc(label)}${n != null ? `<span class="n">${n}</span>` : ""}</button>`;
  const gN = tally(all, "grp");
  const inGrp = all.filter(r => !grp || r.grp === grp);
  const tN = tally(inGrp, "type");
  const inType = inGrp.filter(r => !typ || r.type === typ);
  const cN = tally(inType, "cls");
  const types = grp ? Object.keys(tN).sort((a, b) => TYPE_ORDER.indexOf(a) - TYPE_ORDER.indexOf(b)) : [];
  const classes = CLASSES.filter(c => cN[c] || c === cls);
  $("filters").innerHTML =
    `<div class="frow"><span class="flabel">Group</span><div class="chips">${chip("grp", "", "All", all.length, !grp)}` +
      GROUPS.filter(g => gN[g] || g === grp).map(g => chip("grp", g, g, gN[g] || 0, g === grp)).join("") + `</div></div>` +
    (types.length ? `<div class="frow"><span class="flabel">Type</span><div class="chips">${chip("typ", "", "All", inGrp.length, !typ)}` +
      types.map(t => chip("typ", t, t, tN[t], t === typ)).join("") + `</div></div>` : "") +
    (classes.length ? `<div class="frow"><span class="flabel">Class</span><div class="chips">${chip("cls", "", "All", null, !cls)}` +
      classes.map(c => chip("cls", c, c, cN[c] || 0, c === cls, c.toLowerCase())).join("") + `</div></div>` : "");
  const rows = inType.filter(r => !cls || (r.cls || []).includes(cls));
  rows.sort((a, b) => {
    const va = a[sortK], vb = b[sortK];
    if (va == null && vb == null) return a.name.localeCompare(b.name);
    if (va == null) return 1;
    if (vb == null) return -1;
    const d = (typeof va === "string" ? va.localeCompare(vb, undefined, {sensitivity: "base"}) : va - vb) * sortDir;
    return d || (b.soldN - a.soldN) || (b.n - a.n) || a.name.localeCompare(b.name);
  });
  $("rows").innerHTML = rows.slice(0, limit).map((r, i) => {
    const tags = (r.plus ? `<span class="tag plus" title="Scrolled item: stats differ piece by piece, so it is priced separately">+${r.plus}</span>` : "") +
                 (r.nph && !$("withPh").checked ? `<span class="tag" title="Hidden from the prices: ${short(PH)}+, or ${short(SOFT)}+ and not hovered while recording, or ${RATIO}x the other shops">${r.nph} placeholder hidden</span>` : "") +
                 (r.unsure ? `<span class="tag warn" title="The shop list cut this name short, so the exact item is not certain">name cut off</span>` : "");
    const aka = r.aka.length ? `<small>Listed as: ${esc(r.aka.join(" / "))}</small>` : "";
    const rank = i < 3 ? `<span class="medal m${i + 1}">${i + 1}</span>` : i + 1;
    const range = r.low == null ? `<span class="none">—</span>` : r.low === r.high ? short(r.low) : `${short(r.low)} – ${short(r.high)}`;
    const chg = r.chg == null ? '<span class="none">—</span>' : `<span class="${r.chg >= 0 ? "up" : "down"}">${r.chg >= 0 ? "+" : ""}${r.chg.toFixed(2)}%</span>`;
    const isOpen = open.has(r.name);
    return `<tr class="item" tabindex="0" data-name="${esc(r.name)}" aria-expanded="${isOpen}">
      <td class="rank">${rank}</td>
      <td class="c-name name"><div class="who">${slot(r)}<div>${esc(r.name)}${tags}${aka}</div></div></td>
      <td class="num c-ask">${money(r.avg)}</td>
      <td class="num c-sold">${money(r.soldAvg, "sold")}${r.soldN > 1 ? `<small class="cnt">×${r.soldN}</small>` : ""}</td>
      <td class="num c-range range">${range}</td>
      <td class="num c-n">${r.n}</td>
      <td class="num c-chg">${chg}</td></tr>` + (isOpen ? detail(r, withPh) : "");
  }).join("");
  $("empty").hidden = rows.length > 0;
  const left = rows.length - limit;
  $("more").hidden = left <= 0;
  if (left > 0) $("more").textContent = `Show more (${fmt(left)} more)`;
  $("count").textContent = (left > 0 ? `Showing ${fmt(limit)} of ${fmt(rows.length)} items` : `${rows.length} item${rows.length === 1 ? "" : "s"}`) +
    " · click a row for where to buy · prices are mesos per piece";
  document.querySelectorAll("thead th[data-k]").forEach(th => {
    if (th.dataset.k === sortK) th.setAttribute("aria-sort", sortDir > 0 ? "ascending" : "descending");
    else th.removeAttribute("aria-sort");
  });
}

function header() {
  $("latest").textContent = META.latest;
  if (META.mobs.logo) { $("logo").src = META.mobs.logo; $("logo").hidden = false; }
  if (META.mobs.bg) { $("hero").style.setProperty("--hero", `url(${META.mobs.bg})`); document.body.classList.add("has-hero"); }
  if (META.support.length) {
    $("qrs").innerHTML = META.support.map(q => `<figure><img src="${q.src}" alt="QR code: ${esc(q.label || "support")}">` +
      (q.label ? `<figcaption>${esc(q.label)}</figcaption>` : "") + `</figure>`).join("");
    $("give").hidden = $("supportLink").hidden = false;
  }
  const live = DATA.reduce((s, r) => s + r.listings.length, 0);
  $("stats").textContent = `${META.shops} shops · ${fmt(live)} listings`;
}

// feedback goes to a form service (Formspree-style JSON POST) set in site.json as feedback_url
$("fb").addEventListener("submit", async e => {
  e.preventDefault();
  const status = $("fbStatus"), btn = $("fb").querySelector("button");
  if (!META.feedbackUrl) { status.textContent = "Feedback isn't connected yet. Please try again later."; return; }
  btn.disabled = true; status.textContent = "Sending…";
  try {
    const res = await fetch(META.feedbackUrl, { method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ message: $("fbMsg").value, contact: $("fbWho").value, page: location.href }) });
    if (!res.ok) throw new Error(res.status);
    $("fb").reset(); status.textContent = "Thanks! Your feedback was sent.";
  } catch (err) {
    status.textContent = "Couldn't send right now. Please try again later.";
  } finally { btn.disabled = false; }
});
$("q").addEventListener("input", refilter);
$("todayOnly").addEventListener("change", refilter);
$("withPh").addEventListener("change", refilter);
$("more").addEventListener("click", () => { limit += PAGE_ROWS; render(); });
$("filters").addEventListener("click", e => {
  const b = e.target.closest("button.chip"); if (!b) return;
  const v = b.dataset.v || null;
  if (b.dataset.f === "grp") { grp = v; typ = null; }    // a new group starts with all its types
  else if (b.dataset.f === "typ") typ = v;
  else cls = v;
  refilter();
});
$("tabs").addEventListener("click", e => {
  const b = e.target.closest(".tab"); if (!b) return;
  tab = TABS.find(t => t.id === b.dataset.tab); [sortK, sortDir] = tab.sort;
  refilter();
});
function sortBy(th) {
  const k = th.dataset.k;
  // first click: names A-Z, everything else largest first
  sortDir = sortK === k ? -sortDir : (k === "name" ? 1 : -1); sortK = k;
  refilter();
}
document.querySelectorAll("thead th[data-k]").forEach(th => {
  th.addEventListener("click", () => sortBy(th));
  th.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sortBy(th); } });
});
function toggle(tr) { const n = tr.dataset.name; open.has(n) ? open.delete(n) : open.add(n); render(); }
$("rows").addEventListener("click", e => { const tr = e.target.closest("tr.item"); if (tr) toggle(tr); });
$("rows").addEventListener("keydown", e => {
  const tr = e.target.closest("tr.item");
  if (tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(tr); }
});
document.addEventListener("keydown", e => {
  if (e.key === "/" && document.activeElement !== $("q")) { e.preventDefault(); $("q").focus(); }
});

const tip = $("tip"), card = $("card");
const byName = new Map(DATA.map(r => [r.name, r]));
let cardFor = null;
document.addEventListener("pointerover", e => {
  const t = e.target.closest("[data-tip]");
  tip.hidden = !t;
  if (t) tip.textContent = t.dataset.tip;
  // item card: mouse only, on the item rows, not while the small chart tooltip is showing
  const row = e.pointerType === "mouse" && !t ? e.target.closest("tr.item") : null;
  if (!row) { card.hidden = true; cardFor = null; return; }
  if (row.dataset.name !== cardFor) {
    cardFor = row.dataset.name;
    card.innerHTML = cardHTML(byName.get(cardFor));
  }
  card.hidden = false;
});
document.addEventListener("pointermove", e => {
  if (!tip.hidden) {
    tip.style.left = Math.max(8, Math.min(e.clientX + 12, innerWidth - tip.offsetWidth - 8)) + "px";
    tip.style.top = (e.clientY + 16) + "px";
  }
  if (!card.hidden) {                     // right of the cursor; flip left / up near the edges
    const w = card.offsetWidth, h = card.offsetHeight;
    const x = e.clientX + 18 + w > innerWidth - 8 ? e.clientX - 18 - w : e.clientX + 18;
    const y = Math.max(8, Math.min(e.clientY + 12, innerHeight - h - 8));
    card.style.left = Math.max(8, x) + "px";
    card.style.top = y + "px";
  }
});
document.addEventListener("scroll", () => { card.hidden = true; cardFor = null; }, {passive: true});
header();
render();
</script>
"""

# up/down sort arrows in front of each sortable header, like a spinner
ARROWS = ('<span class="arr" aria-hidden="true"><svg viewBox="0 0 9 12">'
          '<path d="M4.5 0 9 5H0z"></path><path d="M4.5 12 0 7h9z"></path></svg></span>')

DOC_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
"""


def support_qr():
    """The owner's donation QR codes as [{"label", "src": data URI}], from site.json's
    "support" list ({"file", "label"}), else support_qr.png/.jpg beside this script.
    Empty hides the page's support half."""
    import base64
    entries = site_config().get("support") or [{"file": f"support_qr.{e}", "label": ""} for e in ("png", "jpg")]
    out = []
    for e in entries:
        path = os.path.join(HERE, e["file"])
        if os.path.exists(path):
            mime = "png" if path.lower().endswith(".png") else "jpeg"
            with open(path, "rb") as f:
                out.append({"label": e.get("label", ""),
                            "src": f"data:image/{mime};base64," + base64.b64encode(f.read()).decode()})
    return out


def site_config():
    """site.json beside this script: {"feedback_url": form endpoint for the feedback box}."""
    path = os.path.join(HERE, "site.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def placeholders(listings, pmin, soft, ratio):
    """How many of one item's listings on one day are placeholders (same rule as the page)."""
    prices = [l["price"] for l in listings]
    n = 0
    for i, l in enumerate(listings):
        p = l["price"]
        others = sorted(prices[:i] + prices[i + 1:])
        med = (others[(len(others) - 1) // 2] + others[len(others) // 2]) / 2 if others else None
        n += p >= pmin or (p >= soft and not l["hov"]) or (med is not None and p >= ratio * med)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "fm.db"))
    ap.add_argument("--out", default=os.path.join(HERE, "report.html"))
    ap.add_argument("--placeholder-min", type=int, default=999_000_000,
                    help="prices at/above this are keep-the-shop-open placeholders even if "
                         "hovered (left out of the stats unless the page's switch is on)")
    ap.add_argument("--placeholder-unhovered", type=int, default=100_000_000,
                    help="prices at/above this are placeholders unless the item was hovered "
                         "while recording")
    ap.add_argument("--placeholder-ratio", type=float, default=10,
                    help="also a placeholder: this many times the median of the other "
                         "prices for the same item that day")
    ap.add_argument("--fragment", action="store_true", help="omit <html>/<head>/<body>")
    a = ap.parse_args()

    rows, n_snap = load(a.db)
    data, dates, unresolved, n_shops, mobs = build(rows, Normalizer())
    latest = dates[-1] if dates else "-"
    meta = {"latest": latest, "days": len(dates), "snapshots": n_snap, "shops": n_shops,
            "placeholderMin": a.placeholder_min, "placeholderUnhovered": a.placeholder_unhovered,
            "placeholderRatio": a.placeholder_ratio, "mobs": mobs,
            "types": list(dict.fromkeys([t for _, t in EQUIP_TYPES.values()] +
                                        [t for *_, t in OTHER_TYPES] + ["Other"])),
            "support": support_qr(), "feedbackUrl": site_config().get("feedback_url") or None}
    js = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    page = (PAGE.replace("__ARR__", ARROWS)
                .replace("__DATA__", js(data)).replace("__META__", js(meta)))
    if not a.fragment:
        # the <title>/<link>/<style> block goes into <head>, the rest into <body>
        head, body = page.split('<div class="cap"', 1)
        page = DOC_HEAD + head + '</head>\n<body>\n<div class="cap"' + body + "</body></html>\n"
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(page)
    today = sum(1 for d in data if d["listings"])
    n_ph = sum(placeholders(d["listings"] + d["sold"], a.placeholder_min,
                            a.placeholder_unhovered, a.placeholder_ratio) for d in data)
    print(f"items={len(data)} listed_on_{latest}={today} shops={n_shops} snapshots={n_snap} "
          f"days={len(dates)} placeholder_listings={n_ph} unresolved_names={len(unresolved)}")
    print(f"saved -> {a.out}")
    print(f"open the report: {pathlib.Path(a.out).resolve().as_uri()}")


if __name__ == "__main__":
    main()
