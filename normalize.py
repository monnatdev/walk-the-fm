#!/usr/bin/env python3
"""
Item-name normalisation against items.json.

items.json
    {"items":   [...],   canonical names you trust (imported item list, hand-added)
     "auto":    [...],   names learned from your own runs (ingest.py adds them; audit freely)
     "aliases": {"Rayen's Beak": "Raven's Beak", ...}}       manual fixes, OCR variant -> name

Matching (case-insensitive; a "(+N)" upgrade suffix is ignored for matching and,
via match_keep_upgrades(), put back on the result since scrolled items price differently):
    exact / alias            -> that name
    truncated list name      -> the unique canonical name it is a (fuzzy) prefix of
    OCR typo                 -> the unique canonical name within ~10% edit distance
    several prefix matches   -> "ambiguous" (e.g. "Dark scroll for Overall Armor for")
    nothing                  -> "unmatched"
Ambiguous and unmatched names are kept as-is and listed in review_names.csv.

    python normalize.py <name> [<name> ...]          show how names resolve
    python normalize.py --import <file> [...]        add item names from a saved item list
                                                     (.txt one per line, .csv, .json, or .html)
    python normalize.py --import-gms                 (re)load the GMS v83 reference list
                                                     from maplestory.io into "gms"

items.json also has "gms": a reference list from GMS v83 (the classic version this
server is based on). Names this server spells differently are filtered out of it.
"""
import csv, difflib, html, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ITEMS_JSON = os.path.join(HERE, "items.json")
REVIEW_CSV = os.path.join(HERE, "review_names.csv")

PREFIX_MIN = 0.90       # similarity of a list name to the same-length prefix of a canonical name
FULL_MIN = 0.90         # similarity for whole-name OCR typos
TIE = 0.02              # candidates this close to the best one make the match ambiguous

UPGRADES = re.compile(r"\s*\(\+\d+\)\s*$")


def key(name):
    name = UPGRADES.sub("", name)                          # "Work Gloves (+4)" -> "Work Gloves"
    return re.sub(r"\s+", " ", name).strip().casefold()


def ratio(a, b, floor):
    m = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if m.real_quick_ratio() < floor or m.quick_ratio() < floor:   # cheap upper bounds first
        return 0.0
    return m.ratio()


LISTS = ("items", "auto", "gms")


def load_json(path=ITEMS_JSON):
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:        # tolerate a BOM from Notepad
            data = json.load(f)
    for k in LISTS:
        data.setdefault(k, [])
    data.setdefault("aliases", {})
    return data


def save_json(data, path=ITEMS_JSON):
    out = {"aliases": data["aliases"]}
    for k in LISTS:
        out[k] = sorted(set(data[k]), key=str.casefold)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)


# Words that change which item it is. A one-letter OCR slip may not turn
# "STR" into "INT", "60%" into "10%", "Dark scroll" into "Scroll" or "Helmet" into "Belts".
KEY_WORDS = {"str", "dex", "int", "luk", "att", "hp", "mp", "def", "dark", "magic",
             "jump", "speed", "accuracy", "avoidability", "avoid"}
TOKEN_MIN = 0.75          # other words may differ this much (OCR slips like "Rayen's" vs "Raven's")


def _is_key(t):
    return any(c.isdigit() for c in t) or t.strip(".") in KEY_WORDS or len(t) <= 3


def tokens_agree(q, c, prefix):
    """Word-by-word check on top of the character similarity.
    prefix=True: q may be c cut short, so q's last word only has to start c's word."""
    qt, ct = q.split(), c.split()
    if len(qt) > len(ct) or (not prefix and len(qt) != len(ct)):
        return False
    for i, (a, b) in enumerate(zip(qt, ct)):
        if prefix and i == len(qt) - 1:
            b = b[:len(a)]                      # truncated last word: "Accur" vs "Accuracy"
        if a == b:
            continue
        if _is_key(a) or _is_key(b):
            return False
        if difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() < TOKEN_MIN:
            return False
    return True


class Normalizer:
    def __init__(self, path=ITEMS_JSON):
        data = load_json(path)
        # later lists win on the same name: GMS reference < learned from runs < your own
        self.items = {}
        for k in ("gms", "auto", "items"):
            self.items.update({key(n): n for n in data[k]})
        self.aliases = {key(k): v for k, v in data["aliases"].items()}
        self._cache = {}

    def match(self, name):
        """Return (canonical name or None, status, [candidates])."""
        k = key(name)
        if k not in self._cache:
            self._cache[k] = self._match(k)
        return self._cache[k]

    def match_keep_upgrades(self, name):
        """Like match(), but "(+N)" (a scrolled item, priced differently) stays on the result."""
        found, status, cands = self.match(name)
        m = UPGRADES.search(name)
        if found and m:
            found = found + " " + m.group(0).strip()
        return found, status, cands

    def _match(self, k):
        if k in self.items:
            return self.items[k], "exact", []
        if k in self.aliases:
            return self.aliases[k], "alias", []
        scored = []
        for ck, canon in self.items.items():
            full = ratio(k, ck, FULL_MIN)
            # truncated in the list: compare against the canonical name cut to the same length
            pre = ratio(k, ck[:len(k)], PREFIX_MIN) if len(ck) > len(k) else 0.0
            if pre >= PREFIX_MIN and pre >= full and tokens_agree(k, ck, prefix=True):
                scored.append((pre, "prefix", canon))
            elif full >= FULL_MIN and tokens_agree(k, ck, prefix=False):
                scored.append((full, "fuzzy", canon))
        if not scored:
            return None, "unmatched", []
        scored.sort(reverse=True)
        best = scored[0]
        close = [c for s, _, c in scored if s >= best[0] - TIE]
        if len(close) > 1:
            return None, "ambiguous", close
        return best[2], best[1], []


# ---- learning from runs ----------------------------------------------------------------

TRUNCATED_TAIL = re.compile(r"\b(for|of|the|and|to|with|Att|ATT|Magic|Acc|Accur|Accuracy)$")


def clean_tooltip_title(t):
    """Tooltip titles carry OCR noise: 'i Angry Mask', 'Dragon Claymore | creeonwerriorset'."""
    t = t.split("|")[0]
    t = re.sub(r"^[a-z]\s+", "", t.strip())                 # stray lowercase char before the name
    return UPGRADES.sub("", t).strip()


def complete_name(name):
    """The full name if this list name is evidently not truncated, else None.
    List names are cut at the column edge (~32+ chars); short ones are whole names."""
    if not re.match(r"[A-Z0-9\[]", name):
        return None
    if re.search(r"\b\d{1,3}%$", name):                     # scroll with its success rate
        return name
    if re.match(r"(dark )?scroll for ", name, re.I) and re.search(r" (15|30|60|65|70|100)$", name):
        return name + "%"                                  # cut right before the "%"
        # (" 10" is left out: it may be the start of "100%")
    if re.match(r"dark scroll for ", name, re.I) and re.search(r" [37]$", name):
        return name + "0%"                                 # dark scrolls come only in 30% and 70%
    if re.match(r"\[(Mastery Book|Storybook)\] .+ (10|20|30)$", name):
        return name                                        # server appends the skill level
    if 3 <= len(name) <= 26 and not TRUNCATED_TAIL.search(name):
        return name
    return None


def learn(rows, source, path=ITEMS_JSON, min_frames=2):
    """Add confidently-complete names from an items.csv to items.json["auto"] and
    log names that still don't resolve to review_names.csv. Returns (added, review)."""
    data = load_json(path)
    norm = Normalizer(path)
    added = []
    # most-seen spelling first, so a rarer OCR variant ("Rayen's Beak") matches it instead of being added
    for r in sorted(rows, key=lambda r: -int(r.get("frames_seen") or 0)):
        cands = []
        if r["item"] != r["list_name"]:                     # tooltip confirmed the full name
            cands.append(clean_tooltip_title(r["item"]))
        full = complete_name(r["list_name"])
        if full and int(r.get("frames_seen") or 0) >= min_frames:
            cands.append(full)
        for c in cands:
            if len(c) < 3:
                continue
            found, status, _ = norm.match(c)
            if status == "unmatched":                       # new, and not a variant of a known name
                data["auto"].append(c)
                norm.items[key(c)] = c
                norm._cache.clear()
                added.append(c)
    save_json(data, path)

    review = {}
    for r in rows:
        found, status, cands = norm.match(r["item"])
        if status in ("ambiguous", "unmatched"):
            e = review.setdefault(r["item"], [status, cands, 0, source])
            e[2] += 1
    write_review(review)
    return added, review


def write_review(entries, path=REVIEW_CSV):
    """Merge {name: [status, candidates, count, source]} into review_names.csv;
    names that now resolve are dropped."""
    norm = Normalizer()
    old = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            old = {r["name"]: r for r in csv.DictReader(f)}
    for name, (status, cands, count, source) in entries.items():
        r = old.setdefault(name, {"name": name, "count": 0, "first_seen": source})
        r["count"] = int(r["count"]) + count
        r["status"], r["candidates"] = status, " | ".join(cands[:5])
    keep = [r for r in old.values() if norm.match(r["name"])[1] in ("ambiguous", "unmatched")]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:   # BOM so Excel shows UTF-8
        w = csv.DictWriter(f, fieldnames=["name", "status", "count", "candidates", "first_seen"])
        w.writeheader()
        w.writerows(sorted(keep, key=lambda r: (r["status"], r["name"])))


# ---- importing an item list ------------------------------------------------------------

def names_from_file(path):
    ext = os.path.splitext(path)[1].lower()
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    if ext == ".json":
        data = json.loads(text)
        if isinstance(data, list) and data and isinstance(data[0], dict) and "id" in data[0]:
            # oldschoolmaplegame.com /api/item-index: skip cash items (not sold in shops)
            return [x["name"] for x in data if x.get("name") and not x.get("cash")]
        out = []
        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k.lower() in ("name", "item_name", "itemname", "title") and isinstance(v, str):
                        out.append(v)
                    else:
                        walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(data)
        return out
    if ext == ".csv":
        rows = list(csv.DictReader(text.splitlines()))
        col = next((c for c in (rows[0] if rows else {}) if c.lower() in ("name", "item", "item_name")), None)
        return [r[col] for r in rows] if col else []
    if ext in (".html", ".htm"):
        # a saved oldschoolmaplegame.com list page: the item rows' name elements
        rows = re.findall(r'class="[^"]*rowName[^"]*"[^>]*>([^<]{3,80})<', text)
        if rows:
            return [html.unescape(r) for r in rows]
        # other pages: text of table cells / links / headings; filtered below
        cells = re.findall(r"<(?:td|a|h\d|span|div)[^>]*>([^<]{3,80})<", text)
        return [html.unescape(c) for c in cells]
    return text.splitlines()


def import_names(paths, path=ITEMS_JSON, into="items"):
    data = load_json(path)
    before = {key(n) for n in data[into]}
    new = []
    for p in paths:
        for n in names_from_file(p):
            n = re.sub(r"\s+", " ", n).strip()
            if 3 <= len(n) <= 80 and re.search(r"[A-Za-z]", n) and key(n) not in before:
                before.add(key(n))
                new.append(n)
    data[into] += new
    save_json(data, path)
    return new


GMS_URL = "https://maplestory.io/api/GMS/83/item"


def gms_names(raw):
    """Tradeable GMS v83 names, minus the kinds this server names differently:
    scrolls without a "%" (server always shows the rate) and mastery/story books
    (server adds the skill level)."""
    keep = set()
    for x in raw:
        n = (x.get("name") or "").strip()
        if not n or x.get("isCash"):
            continue
        if re.match(r"(dark )?scroll for ", n, re.I) and not n.endswith("%"):
            continue                                       # server scrolls always carry a "%"
        if n.startswith(("[Mastery Book]", "[Storybook]")):
            continue
        keep.add(n)
    return keep


def import_gms(path=ITEMS_JSON):
    import urllib.request
    req = urllib.request.Request(GMS_URL, headers={"User-Agent": "fm-price-tracker/1.0 (personal price log)"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = json.load(r)
    data = load_json(path)
    data["gms"] = sorted(gms_names(raw))
    save_json(data, path)
    return len(data["gms"])


if __name__ == "__main__":
    if sys.argv[1:2] == ["--import"]:
        new = import_names(sys.argv[2:])
        print(f"imported {len(new)} new names into {ITEMS_JSON}; first few: {new[:10]}")
        sys.exit()
    if sys.argv[1:2] == ["--import-gms"]:
        print(f"items.json: gms list = {import_gms()} names from {GMS_URL}")
        sys.exit()
    n = Normalizer()
    print(f"items.json: {len(n.items)} names, {len(n.aliases)} aliases")
    for name in sys.argv[1:]:
        found, status, cands = n.match(name)
        print(f"{name!r:45} -> {status:9} {found or ''}{'  ' + ' | '.join(cands) if cands else ''}")
