"""Guess a shop's FM room (1-22) from the title its owner typed, e.g. "1-14 top" (channel 1,
FM 14), "CH1 FM18 ...", "Fm 20 Ch1", "C1R7", "CH 2/10". The game doesn't show the room in
the shop list, so this is only what the title says; None when it doesn't say."""
import re

ROOMS = range(1, 23)
CH = r"(?:c{1,2}h?|ch\.?|channel\s*)\s*[1il|]"                # "C1", "CH1", "Chl" (l = misread 1)
NUM = r"(\d{1,2})(?!\d)"


def _ok(n):
    n = int(n)
    return n if n in ROOMS else None


def fm_room(title):
    t = (title or "").lower()
    if not t:
        return None
    # "FM 1-14": channel-room after FM; "FM3-1": room-channel
    m = re.search(r"\bfm\s*\.?\s*([1-3])\s*[-/]\s*" + NUM, t)
    if m and int(m[2]) > 3:
        return _ok(m[2])
    # "FM18", "Fm 20", "FM.12", "FM2:", "f14", "rm13", "room5", "R10" / "r7" after a channel
    m = (re.search(r"(?:\bfm|fm(?=\d)|\brm|\broom)\s*[.:]?\s*" + NUM, t)
         or re.search(r"(?:\bf|(?<=\d)f)\s*" + NUM, t)
         or (re.search(r"(?:\b|(?<=\d))r\s*" + NUM, t) if re.search(CH, t) else None))
    if m:
        return _ok(m[1])
    # "[13]Ch.1"
    m = re.search(r"\[(\d{1,2})\]\s*ch", t)
    if m:
        return _ok(m[1])
    # "CH2-10", "C1-14", "CH 2/10", "ch2~1": channel then room
    m = re.search(r"(?:^|[^a-z])" + CH + r"\s*[-/~]+\s*" + NUM, t)
    if m:
        return _ok(m[1])
    # bare "1-14", "1/15", "2 13": channel 1-3 then room; "14-1", "15-1": room then channel
    m = re.search(r"(?:^|[^\d/.-])([1-3])\s*[-/ ]\s*" + NUM + r"(?![\d/-])", t)
    if m and (" " not in m[0].strip() or int(m[2]) > 3):
        return _ok(m[2])
    m = re.search(r"(?:^|[^\d/.-])(\d{1,2})\s*-\s*([1-3])(?![\d/-])", t)
    if m and int(m[1]) > 3:
        return _ok(m[1])
    return None
