#!/usr/bin/env python3
"""
Item icons for the report, from the public maplestory.io API (GMS v83 item data).

Icons are downloaded once into icons/<id>.png and embedded in the page as data: URIs,
so report.html works offline. Items without an exact GMS name get the icon of the closest
GMS item of the same kind (e.g. "Dark scroll for Cape for DEX 70%" -> the GMS dark scroll
for Cape, "[Mastery Book] Snipe 20" -> "[Mastery Book] Snipe").

    python icons.py <item name> [...]     show which GMS item each name's icon comes from
"""
import base64, json, os, re, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(HERE, "icons")
IDS_JSON = os.path.join(ICON_DIR, "gms_ids.json")
API = "https://maplestory.io/api/GMS/83/item"
UA = {"User-Agent": "fm-price-tracker/1.0 (personal price log)"}


def _get(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


OSM_INDEX = os.path.join(HERE, "osm_item_index.json")   # saved from the server's site (README)


def load_ids():
    """{lowercased name: item id}: the server's own item list first (osm_item_index.json,
    when saved), then GMS v83 names (downloaded once) for anything it lacks."""
    ids = {}
    if os.path.exists(OSM_INDEX):
        with open(OSM_INDEX, encoding="utf-8-sig") as f:
            for x in sorted(json.load(f), key=lambda x: (bool(x.get("cash")), int(x["id"]))):
                n = (x.get("name") or "").strip()
                if n:
                    ids.setdefault(n.casefold(), int(x["id"]))
    for k, v in load_gms_ids().items():
        ids.setdefault(k, v)
    return ids


def load_gms_ids():
    """{lowercased GMS name: item id}, downloaded once."""
    if not os.path.exists(IDS_JSON):
        os.makedirs(ICON_DIR, exist_ok=True)
        raw = json.loads(_get(API, timeout=120))
        ids = {}
        for x in sorted(raw, key=lambda x: (bool(x.get("isCash")), x["id"])):   # prefer non-cash
            n = (x.get("name") or "").strip()
            if n:
                ids.setdefault(n.casefold(), x["id"])
        with open(IDS_JSON, "w", encoding="utf-8") as f:
            json.dump(ids, f, ensure_ascii=False)
    with open(IDS_JSON, encoding="utf-8") as f:
        return json.load(f)


def find_id(name, ids):
    """Return (id, matched GMS name) or (None, None)."""
    n = name.rstrip("…").strip()
    n = re.sub(r"\s*\(\+\d+\)$", "", n)                       # scrolled "(+4)"
    tries = [n, re.sub(r"\s+\d+%$", "", n), re.sub(r"\s+\d+$", "", n)]   # "70%", book level
    for t in tries:
        if t.casefold() in ids:
            return ids[t.casefold()], t
    # same kind of item: drop words from the end until some GMS name starts with the rest
    words = tries[1].split()
    while len(words) >= 2:
        prefix = " ".join(words).casefold()
        hits = sorted((k for k in ids if k.startswith(prefix)), key=len)
        if hits:
            return ids[hits[0]], hits[0]
        words.pop()
    return None, None


MOB_API = "https://maplestory.io/api/GMS/83/mob"
INVENTORY = {1: "equip", 2: "use", 3: "setup", 4: "etc", 5: "cash"}   # first digit of an item id

# Group and type as the server's item site files them (oldschoolmaplegame.com/items),
# from the item id: equipment by id // 10000, everything else by id // 1000.
EQUIP_TYPES = {
    100: ("Armor", "Hat"), 105: ("Armor", "Overall"), 104: ("Armor", "Top"), 106: ("Armor", "Bottom"),
    107: ("Armor", "Shoes"), 108: ("Armor", "Gloves"), 110: ("Armor", "Cape"), 109: ("Armor", "Shield"),
    101: ("Accessories", "Face Accessory"), 102: ("Accessories", "Eye Accessory"),
    103: ("Accessories", "Earring"), 111: ("Accessories", "Ring"), 112: ("Accessories", "Pendant"),
    113: ("Accessories", "Belt"), 114: ("Accessories", "Medal"),
    130: ("Weapons", "One-Handed Sword"), 131: ("Weapons", "One-Handed Axe"),
    132: ("Weapons", "One-Handed Blunt"), 133: ("Weapons", "Dagger"), 137: ("Weapons", "Wand"),
    138: ("Weapons", "Staff"), 140: ("Weapons", "Two-Handed Sword"), 141: ("Weapons", "Two-Handed Axe"),
    142: ("Weapons", "Two-Handed Blunt"), 143: ("Weapons", "Spear"), 144: ("Weapons", "Polearm"),
    145: ("Weapons", "Bow"), 146: ("Weapons", "Crossbow"), 147: ("Weapons", "Claw"),
    148: ("Weapons", "Knuckle"), 149: ("Weapons", "Gun"),
}
OTHER_TYPES = [                     # (first id // 1000, last id // 1000, group, type)
    (2000, 2029, "Use", "Potions"), (2050, 2050, "Use", "Potions"), (2210, 2210, "Use", "Potions"),
    (2030, 2030, "Use", "Return Scrolls"), (2040, 2049, "Use", "Scrolls"),
    (2060, 2061, "Use", "Arrows"), (2070, 2070, "Use", "Throwing Stars"), (2330, 2332, "Use", "Bullets"),
    (2280, 2299, "Use", "Skill & Mastery Books"),
    (2380, 2380, "Monster Cards", "Lv. 1-15"), (2381, 2381, "Monster Cards", "Lv. 16-30"),
    (2382, 2382, "Monster Cards", "Lv. 31-45"), (2383, 2383, "Monster Cards", "Lv. 46-60"),
    (2384, 2384, "Monster Cards", "Lv. 61-75"), (2385, 2385, "Monster Cards", "Lv. 76-90"),
    (2386, 2386, "Monster Cards", "Lv. 91-105"), (2387, 2387, "Monster Cards", "Lv. 106+"),
    (2388, 2388, "Monster Cards", "Bosses"),
    (3010, 3019, "Setup", "Chairs"),
    (4000, 4008, "Etc", "Monster Drops"), (4010, 4011, "Etc", "Ore"), (4020, 4021, "Etc", "Gems"),
    (4130, 4131, "Etc", "Stimulators"), (4030, 4080, "Etc", "Minigame Pieces"), (4161, 4161, "Etc", "Books"),
]
GROUP_OF_INV = {"equip": "Armor", "use": "Use", "setup": "Setup", "etc": "Etc", "cash": "Cash"}
# job bit mask in the server's item index; 0 = every class
CLASSES = [(1, "Warrior"), (2, "Magician"), (4, "Bowman"), (8, "Thief"), (16, "Pirate")]


def category(iid):
    """Item id -> (group, type), e.g. 1472030 -> ("Weapons", "Claw")."""
    if iid // 1_000_000 == 1:
        return EQUIP_TYPES.get(iid // 10000, ("Armor", "Other"))
    k = iid // 1000
    for lo, hi, group, kind in OTHER_TYPES:
        if lo <= k <= hi:
            return group, kind
    return GROUP_OF_INV.get(INVENTORY.get(iid // 1_000_000), "Etc"), "Other"


def load_jobs():
    """{item id: job bit mask} from the server's item index (empty if it is not saved)."""
    if not os.path.exists(OSM_INDEX):
        return {}
    with open(OSM_INDEX, encoding="utf-8-sig") as f:
        return {int(x["id"]): int(x.get("job") or 0) for x in json.load(f)}


def classes(job):
    """Job bit mask -> ["Warrior", ...]; ["Common"] for items every class can use."""
    return [name for bit, name in CLASSES if job & bit] or ["Common"]


def _cached_png(path, url):
    """data: URI of a PNG, downloading it once into icons/. None if unavailable."""
    if not os.path.exists(path):
        try:
            png = _get(url)
        except OSError:
            return None
        with open(path, "wb") as f:
            f.write(png)
        time.sleep(0.1)                                        # be gentle with the free API
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def item_info(names, log=print):
    """{name: {"icon": data URI or None, "inv": "equip"/"use"/"setup"/"etc"/"cash" or None,
    "group", "type": as category(), "cls": classes() for equipment, else None}}.
    Downloads missing icons (1-2 KB each); without internet it uses what is in icons/."""
    try:
        ids = load_ids()
    except OSError as e:
        log(f"icons: item list unavailable ({e}); report without icons")
        return {}
    jobs = load_jobs()
    os.makedirs(ICON_DIR, exist_ok=True)
    out = {}
    for name in names:
        iid, _ = find_id(name, ids)
        if iid is None:
            out[name] = {"icon": None, "inv": None, "group": None, "type": None, "cls": None}
            continue
        uri = _cached_png(os.path.join(ICON_DIR, f"{iid}.png"), f"{API}/{iid}/icon")
        group, kind = category(iid)
        cls = classes(jobs[iid]) if iid // 1_000_000 == 1 and iid in jobs else None
        out[name] = {"icon": uri, "inv": INVENTORY.get(iid // 1_000_000),
                     "group": group, "type": kind, "cls": cls}
    have = sum(1 for v in out.values() if v["icon"])
    log(f"icons: {have}/{len(names)} items have an icon")
    return out


def mob_icon(mob_id):
    """A monster's icon (e.g. 1210102 = Orange Mushroom) as a data: URI, or None."""
    os.makedirs(ICON_DIR, exist_ok=True)
    return _cached_png(os.path.join(ICON_DIR, f"mob_{mob_id}.png"), f"{MOB_API}/{mob_id}/icon")


if __name__ == "__main__":
    ids = load_ids()
    for name in sys.argv[1:]:
        print(f"{name!r:45} -> {find_id(name, ids)}")
