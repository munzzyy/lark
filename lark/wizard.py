"""Scene wizard: work out what a template still needs in OBS, then create only that.

No Qt in here. The Applier talks to OBS through any request(type, data, cb) callable,
which is what ObsBridge.request already is. It never sends a delete or a rename.
"""
import json
import logging
from pathlib import Path

log = logging.getLogger("lark.wizard")

TEMPLATE_DIR = Path(__file__).parent / "templates"

# Every request type the wizard may send. A test pins this so nothing destructive sneaks in.
ALLOWED_REQUESTS = frozenset({
    "GetSceneList", "GetInputList", "GetInputKindList",
    "CreateScene", "CreateInput", "CreateSceneItem", "SetSceneItemTransform",
})


def load_templates(directory=None):
    out = []
    for p in sorted(Path(directory or TEMPLATE_DIR).glob("*.json")):
        try:
            t = json.loads(p.read_text())
        except (OSError, ValueError) as e:
            log.error("template %s unreadable: %s", p.name, e)
            continue
        if isinstance(t, dict) and isinstance(t.get("scenes"), list) and t.get("id") and t.get("name"):
            out.append(t)
        else:
            log.error("template %s is missing id, name or scenes", p.name)
    out.sort(key=lambda t: (t["id"] != "standard", t["name"]))
    return out


def resolve(value, env):
    """Fill {placeholders} in every string of a settings tree. Unknown placeholders stay as written."""
    if isinstance(value, str):
        for k, v in env.items():
            value = value.replace("{" + k + "}", str(v))
        return value
    if isinstance(value, list):
        return [resolve(v, env) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, env) for k, v in value.items()}
    return value


class Plan:
    def __init__(self):
        self.steps = []
        self.notes = []

    @property
    def work(self):
        return [s for s in self.steps if s["op"] in ("create_scene", "create_input", "add_existing")]

    def lines(self):
        out = []
        for s in self.steps:
            op = s["op"]
            if op == "create_scene":
                out.append(f"Create scene {s['scene']}")
            elif op == "create_input":
                out.append(f"    New source {s['name']} ({s['kind']}) in {s['scene']}")
            elif op == "add_existing":
                out.append(f"    Use your existing {s['name']} in {s['scene']}")
            elif op == "keep_scene":
                out.append(f"Keep scene {s['scene']} as it is")
            elif op == "skip":
                out.append(f"    Skip {s['name']} in {s['scene']}: {s['reason']}")
        out.extend(self.notes)
        return out


def build_plan(template, scenes, inputs, kinds, env):
    """scenes: names in OBS. inputs: {name: kind}. kinds: set of input kinds OBS has, or None if unknown."""
    plan = Plan()
    if template.get("note"):
        plan.notes.append(template["note"])
    taken = set(scenes)
    known = dict(inputs)
    unavailable = {}
    for sc in template["scenes"]:
        name = sc["name"]
        if name in taken:
            plan.steps.append({"op": "keep_scene", "scene": name})
            continue
        plan.steps.append({"op": "create_scene", "scene": name})
        taken.add(name)
        for it in sc["items"]:
            iname, kind = it["name"], it["kind"]
            tf = resolve(it.get("transform"), env)
            base = {"scene": name, "name": iname}
            if iname in unavailable:
                plan.steps.append({"op": "skip", **base, "reason": unavailable[iname]})
            elif iname in known:
                plan.steps.append({"op": "add_existing", **base, "transform": tf})
            elif iname in taken:
                unavailable[iname] = "a scene already has that name"
                plan.steps.append({"op": "skip", **base, "reason": unavailable[iname]})
            elif kinds is not None and kind not in kinds:
                unavailable[iname] = f"this OBS has no {kind} source"
                plan.steps.append({"op": "skip", **base, "reason": unavailable[iname]})
            else:
                known[iname] = kind
                plan.steps.append({"op": "create_input", **base, "kind": kind,
                                   "settings": resolve(it.get("settings") or {}, env), "transform": tf})
    return plan


def detect(request, done):
    """Ask OBS what exists. done(ok, {"scenes", "inputs", "kinds"} or error text)."""
    found = {}
    remaining = {"n": 3, "err": None}

    def finish():
        remaining["n"] -= 1
        if remaining["n"]:
            return
        if remaining["err"]:
            done(False, remaining["err"])
        else:
            done(True, found)

    def got_scenes(ok, p):
        if ok:
            found["scenes"] = [s["sceneName"] for s in p.get("scenes", [])]
        else:
            remaining["err"] = remaining["err"] or p
        finish()

    def got_inputs(ok, p):
        if ok:
            found["inputs"] = {i["inputName"]: i.get("inputKind", "") for i in p.get("inputs", [])}
        else:
            remaining["err"] = remaining["err"] or p
        finish()

    def got_kinds(ok, p):
        found["kinds"] = set(p.get("inputKinds", [])) if ok else None
        finish()

    request("GetSceneList", None, got_scenes)
    request("GetInputList", None, got_inputs)
    request("GetInputKindList", {"unversioned": True}, got_kinds)


class Applier:
    """Runs a plan one request at a time. A failed scene skips the rest of that scene's steps."""

    def __init__(self, request, plan, on_step=None, on_done=None):
        self.request = request
        self.steps = list(plan.work)
        self.on_step = on_step or (lambda text, ok: None)
        self.on_done = on_done or (lambda created, errors: None)
        self.created = 0
        self.errors = []
        self._bad_scenes = set()
        self._i = 0

    def start(self):
        self._next()

    def _next(self):
        while self._i < len(self.steps) and self.steps[self._i]["scene"] in self._bad_scenes:
            self._i += 1
        if self._i >= len(self.steps):
            self.on_done(self.created, self.errors)
            return
        s = self.steps[self._i]
        self._i += 1
        if s["op"] == "create_scene":
            self.request("CreateScene", {"sceneName": s["scene"]}, lambda ok, p: self._after(s, ok, p, False))
        elif s["op"] == "create_input":
            self.request("CreateInput", {"sceneName": s["scene"], "inputName": s["name"], "inputKind": s["kind"],
                                         "inputSettings": s["settings"], "sceneItemEnabled": True},
                         lambda ok, p: self._after(s, ok, p, True))
        else:
            self.request("CreateSceneItem", {"sceneName": s["scene"], "sourceName": s["name"],
                                             "sceneItemEnabled": True}, lambda ok, p: self._after(s, ok, p, True))

    def _fail(self, s, text):
        self.errors.append(text)
        self.on_step(text, False)
        if s["op"] == "create_scene":
            self._bad_scenes.add(s["scene"])

    def _after(self, s, ok, p, has_item):
        label = s["scene"] if s["op"] == "create_scene" else f"{s['name']} in {s['scene']}"
        if not ok:
            self._fail(s, f"{label}: {p}")
            self._next()
            return
        self.created += 1
        self.on_step(f"Created {label}", True)
        tf = s.get("transform")
        item_id = p.get("sceneItemId") if has_item else None
        if tf and item_id is not None:
            def placed(ok2, p2):
                if not ok2:
                    self._fail(s, f"{label}: could not place it ({p2})")
                self._next()
            self.request("SetSceneItemTransform", {"sceneName": s["scene"], "sceneItemId": item_id,
                                                   "sceneItemTransform": tf}, placed)
        else:
            self._next()
