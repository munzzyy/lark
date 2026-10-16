"""Camera looks: colour and sharpness filters on the OBS camera input, all named "Lark: ..."."""
import logging

log = logging.getLogger("lark.looks")

PREFIX = "Lark: "
COLOR_FILTER, SHARP_FILTER, LUT_FILTER = PREFIX + "Color", PREFIX + "Sharpness", PREFIX + "LUT"
KINDS = {COLOR_FILTER: "color_filter_v2", SHARP_FILTER: "sharpness_filter_v2", LUT_FILTER: "clut_filter"}

# key -> (filter, obs setting, low, high, default)
NUMERIC = {
    "gamma": (COLOR_FILTER, "gamma", -3.0, 3.0, 0.0),
    "contrast": (COLOR_FILTER, "contrast", -4.0, 4.0, 0.0),
    "brightness": (COLOR_FILTER, "brightness", -4.0, 4.0, 0.0),
    "saturation": (COLOR_FILTER, "saturation", -1.0, 5.0, 0.0),
    "hue_shift": (COLOR_FILTER, "hue_shift", -180.0, 180.0, 0.0),
    "opacity": (COLOR_FILTER, "opacity", 0.0, 100.0, 100.0),
    "sharpness": (SHARP_FILTER, "sharpness", 0.0, 1.0, 0.0),
    "lut_amount": (LUT_FILTER, "clut_amount", 0.0, 1.0, 1.0),
}
LUT_EXTS = (".png", ".jpg", ".jpeg", ".bmp")
MAX_USER_LOOKS = 50
NEUTRAL = "Neutral"

BUILTIN = {
    NEUTRAL: {},
    "Warm": {"hue_shift": -5.0, "saturation": 0.12, "brightness": 0.02, "gamma": 0.04},
    "Punchy": {"contrast": 0.25, "saturation": 0.35, "gamma": -0.05, "sharpness": 0.25},
    "Soft": {"contrast": -0.12, "saturation": -0.05, "brightness": 0.03, "gamma": 0.08},
}


def defaults():
    p = {k: v[4] for k, v in NUMERIC.items()}
    p["lut_path"] = ""
    return p


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def valid_lut_path(v):
    return isinstance(v, str) and len(v) <= 512 and "\x00" not in v and (v == "" or v.lower().endswith(LUT_EXTS))


def valid_params(p):
    if not isinstance(p, dict):
        return False
    for k, v in p.items():
        if k == "lut_path":
            if not valid_lut_path(v):
                return False
        elif k in NUMERIC:
            if not _number(v) or not NUMERIC[k][2] <= v <= NUMERIC[k][3]:
                return False
        else:
            return False
    return True


def valid_name(n):
    return isinstance(n, str) and 1 <= len(n) <= 40 and n == n.strip() and n.isprintable()


def valid_looks(v):
    return (isinstance(v, dict) and len(v) <= MAX_USER_LOOKS
            and all(valid_name(k) and k not in BUILTIN and valid_params(p) for k, p in v.items()))


def full_params(p):
    out = defaults()
    out.update(p)
    return out


def desired(params):
    """{filter name: (settings, enabled)} for a parameter set."""
    d = full_params(params)
    out = {}
    for fname in (COLOR_FILTER, SHARP_FILTER, LUT_FILTER):
        out[fname] = {obs: float(d[k]) for k, (f, obs, _, _, _) in NUMERIC.items() if f == fname}
    color_on = any(d[k] != v[4] for k, v in NUMERIC.items() if v[0] == COLOR_FILTER)
    out[LUT_FILTER]["image_path"] = d["lut_path"]
    return {COLOR_FILTER: (out[COLOR_FILTER], color_on),
            SHARP_FILTER: (out[SHARP_FILTER], d["sharpness"] > 0),
            LUT_FILTER: (out[LUT_FILTER], bool(d["lut_path"]))}


class Looks:
    """Owns the working look and keeps the OBS filters in step with it.

    bridge.request(type, data, cb) and notify(text, is_error) are the only outside calls.
    """

    def __init__(self, bridge, config, notify):
        self.bridge, self.config, self.notify = bridge, config, notify
        self.active = config.get("look_active")
        self.other = config.get("look_other")
        if self.active not in self.names():
            self.active = NEUTRAL
        if self.other not in self.names() or self.other == self.active:
            self.other = NEUTRAL if self.active != NEUTRAL else ""
        self.params = full_params(self.stored(self.active))
        self._exists = set()
        self._sent = {}
        self._busy = False
        self._dirty = False
        self._left = 0
        self._failed = None

    def source(self):
        return self.config.get("camera_input")

    def names(self):
        return list(BUILTIN) + sorted(self.config.get("looks"))

    def stored(self, name):
        return BUILTIN[name] if name in BUILTIN else self.config.get("looks").get(name, {})

    def modified(self):
        return self.params != full_params(self.stored(self.active))

    def select(self, name):
        if name not in self.names():
            raise KeyError(name)
        if name != self.active:
            self.other = self.active
        self.active = name
        self.params = full_params(self.stored(name))
        self._remember()
        self.sync()

    def toggle_ab(self):
        if not self.other or self.other not in self.names():
            return False
        self.select(self.other)
        return True

    def set_param(self, key, value):
        if key == "lut_path":
            if not valid_lut_path(value):
                raise ValueError("A LUT must be a png, jpg or bmp image")
            self.params[key] = value
        elif key in NUMERIC:
            if not _number(value):
                raise ValueError(f"{key} must be a number")
            _, _, lo, hi, _ = NUMERIC[key]
            self.params[key] = min(hi, max(lo, float(value)))
        else:
            raise KeyError(key)
        self.sync()

    def save_as(self, name):
        if not valid_name(name):
            raise ValueError("Pick a name of up to 40 characters")
        if name in BUILTIN:
            raise ValueError(f"{name} is a built-in look")
        looks = dict(self.config.get("looks"))
        if name not in looks and len(looks) >= MAX_USER_LOOKS:
            raise ValueError("Too many saved looks")
        base = defaults()
        looks[name] = {k: v for k, v in self.params.items() if v != base[k]}
        self.config.set("looks", looks)
        if name != self.active:
            self.other = self.active
        self.active = name
        self._remember()

    def delete(self, name):
        if name in BUILTIN:
            raise ValueError("Built-in looks stay")
        looks = dict(self.config.get("looks"))
        if looks.pop(name, None) is None:
            raise KeyError(name)
        self.config.set("looks", looks)
        if self.active == name:
            self.active = NEUTRAL
            self.params = defaults()
            self.sync()
        if self.other == name:
            self.other = ""
        self._remember()

    def _remember(self):
        self.config.set("look_active", self.active)
        self.config.set("look_other", self.other)
        try:
            self.config.save()
        except OSError as e:
            log.error("could not save looks: %s", e)
            self.notify(f"Could not save looks ({e.strerror})", True)

    def forget_remote(self):
        """OBS reconnected or the source changed, so what was last sent no longer counts."""
        self._exists.clear()
        self._sent.clear()

    @staticmethod
    def _check(name):
        if not name.startswith(PREFIX) or name not in KINDS:
            raise ValueError(f"refusing to touch filter {name!r}")
        return name

    def sync(self):
        if self._busy:
            self._dirty = True
            return
        self._busy = True
        self._dirty = False
        self.bridge.request("GetSourceFilterList", {"sourceName": self.source()}, self._listed)

    def _listed(self, ok, payload):
        if not ok:
            self._finish()
            self.notify(f"Looks: {payload}", True)
            return
        names = {f.get("filterName") for f in payload.get("filters", [])}
        self._exists = {n for n in names if n in KINDS}
        for gone in set(self._sent) - self._exists:
            del self._sent[gone]
        src = self.source()
        jobs = []
        for fname, (settings, on) in desired(self.params).items():
            self._check(fname)
            if fname not in self._exists:
                if on:
                    jobs.append(("CreateSourceFilter", {"sourceName": src, "filterName": fname,
                                                        "filterKind": KINDS[fname], "filterSettings": settings},
                                 fname, (settings, True)))
                continue
            last = self._sent.get(fname)
            if last is None or last[0] != settings:
                jobs.append(("SetSourceFilterSettings", {"sourceName": src, "filterName": fname,
                                                         "filterSettings": settings}, fname, (settings, None)))
            if last is None or last[1] != on:
                jobs.append(("SetSourceFilterEnabled", {"sourceName": src, "filterName": fname,
                                                        "filterEnabled": on}, fname, (None, on)))
        if not jobs:
            self._finish()
            return
        self._left = len(jobs)
        self._failed = None
        for rtype, data, fname, sent in jobs:
            self.bridge.request(rtype, data, lambda ok, p, f=fname, s=sent: self._done(ok, p, f, s))

    def _done(self, ok, payload, fname, sent):
        if ok:
            old = self._sent.get(fname, (None, None))
            self._sent[fname] = (old[0] if sent[0] is None else sent[0], old[1] if sent[1] is None else sent[1])
        elif self._failed is None:
            self._failed = payload
        self._left -= 1
        if self._left == 0:
            if self._failed:
                self.notify(f"Looks: {self._failed}", True)
            self._finish()

    def _finish(self):
        self._busy = False
        if self._dirty:
            self.sync()
