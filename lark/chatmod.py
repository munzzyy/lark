"""Chat moderation rules. Pure functions: a message and the settings in, the first broken rule out."""
import re
import unicodedata

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"))
_TLDS = ("com|net|org|io|gg|tv|co|me|xyz|info|biz|us|uk|de|fr|ru|cn|jp|br|ca|au|nl|se|no|fi|pl|es|it|ly|to|cc|ws|"
         "app|dev|ink|link|live|shop|site|online|store|tech|club|top|vip|win|bet|fun|pro|page|cloud|zip|mov|stream")
_LINK = re.compile(rf"(?:https?://|www\.)[^\s]+|(?<![\w@.-])((?:[a-z0-9-]{{1,63}}\.)+(?:{_TLDS}))(?![\w-])", re.I)
_HOST = re.compile(r"^(?:https?://)?(?:www\.)?([^/\s:?#]+)", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\d)\+?\d(?:[\s().-]{0,2}\d){9,12}(?!\d)")

RULES = ("terms", "contacts", "links", "emotes", "caps")
REASONS = {
    "terms": "That word isn't allowed in this chat.",
    "contacts": "Please don't post emails or phone numbers.",
    "links": "Links need a permit from a mod.",
    "emotes": "Too many emotes in one message.",
    "caps": "Please ease off the caps.",
}


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).casefold().split())


def hosts_in(text):
    out = []
    for m in _LINK.finditer(normalize(text)):
        raw = m.group(0)
        h = _HOST.match(raw)
        host = (h.group(1) if h else raw).lower().rstrip(".,!?)")
        if host:
            out.append(host)
    return out


def host_allowed(host, allow):
    return any(host == a or host.endswith("." + a) for a in allow)


def has_term(text, terms):
    norm = normalize(text)
    for t in terms:
        t = normalize(t).strip()
        if not t:
            continue
        if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", norm):
            return True
        flat = re.sub(r"[\W_]+", "", t)
        if len(flat) >= 5:
            loose = "[._*-]?".join(re.escape(c) for c in flat)
            if re.search(rf"(?<!\w){loose}(?!\w)", norm):
                return True
    return False


def caps_ratio(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0, 0.0
    up = sum(1 for c in letters if c.isupper())
    return len(letters), 100.0 * up / len(letters)


def check(msg, mod, permitted=False):
    """The name of the first rule the message breaks, or None. permitted skips the link rule."""
    text = msg["text"]
    t = mod["terms"]
    if t["on"] and has_term(text, [*t["blocked"], *t["personal"]]):
        return "terms"
    c = mod["contacts"]
    if c["on"] and (_EMAIL.search(text) or _PHONE.search(text)):
        return "contacts"
    lk = mod["links"]
    if lk["on"] and not permitted:
        if any(not host_allowed(h, lk["allow"]) for h in hosts_in(text)):
            return "links"
    e = mod["emotes"]
    if e["on"] and msg.get("emote_count", 0) > e["max"]:
        return "emotes"
    cp = mod["caps"]
    if cp["on"]:
        n, pct = caps_ratio(text)
        if n >= cp["min_letters"] and pct >= cp["percent"]:
            return "caps"
    return None
