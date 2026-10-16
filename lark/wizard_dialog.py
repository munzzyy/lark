from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout

from .wizard import Applier, build_plan, detect, load_templates
from .widgets import label


class WizardDialog(QDialog):
    def __init__(self, ctx, parent=None, templates=None):
        super().__init__(parent)
        self.setWindowTitle("Set up scenes")
        self.resize(640, 520)
        self.ctx = ctx
        self.templates = templates if templates is not None else load_templates()
        self.found = None
        self.plan = None
        self.busy = False
        lay = QVBoxLayout(self)
        lay.addWidget(label("Pick a layout. Lark checks what OBS already has, shows what it would add, and only "
                            "creates what is missing. Nothing is deleted or renamed.", "muted", wrap=True))
        self.combo = QComboBox()
        for t in self.templates:
            self.combo.addItem(t["name"], t["id"])
        self.combo.currentIndexChanged.connect(self.preview)
        lay.addWidget(self.combo)
        self.desc = label("", "small", wrap=True)
        lay.addWidget(self.desc)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        lay.addWidget(self.out, 1)
        row = QHBoxLayout()
        self.recheck = QPushButton("Check OBS again")
        self.recheck.clicked.connect(self.check)
        self.apply = QPushButton("Apply")
        self.apply.setObjectName("primary")
        self.apply.setEnabled(False)
        self.apply.clicked.connect(self.run_apply)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(self.recheck)
        row.addStretch(1)
        row.addWidget(close)
        row.addWidget(self.apply)
        lay.addLayout(row)
        self.check()

    def template(self):
        i = self.combo.currentIndex()
        return self.templates[i] if 0 <= i < len(self.templates) else None

    def env(self):
        svc, cfg = self.ctx.services, self.ctx.config
        port = cfg.get("overlay_port")
        return {"camera_device": cfg.get("camera_device"),
                "overlay_chat": svc.url("/chat") or f"http://127.0.0.1:{port}/chat",
                "overlay_starting": svc.url("/starting") or f"http://127.0.0.1:{port}/starting"}

    def check(self):
        self.found = self.plan = None
        self.apply.setEnabled(False)
        if not self.ctx.store.connected:
            self.out.setPlainText("OBS is not connected. Start OBS, then press Check OBS again.")
            return
        self.out.setPlainText("Asking OBS what it has...")
        detect(self.ctx.bridge.request, self._detected)

    def _detected(self, ok, found):
        if not ok:
            self.out.setPlainText(f"Could not read OBS: {found}")
            return
        self.found = found
        self.preview()

    def preview(self):
        t = self.template()
        self.desc.setText(t["description"] if t else "")
        if t is None or self.found is None:
            return
        f = self.found
        self.plan = build_plan(t, f["scenes"], f["inputs"], f["kinds"], self.env())
        lines = self.plan.lines()
        if self.plan.work:
            head = "Preview, nothing has been changed yet:"
        else:
            head = "Nothing to add."
        self.out.setPlainText("\n".join([head, ""] + lines))
        self.apply.setEnabled(bool(self.plan.work) and not self.busy)

    def run_apply(self):
        if not self.plan or not self.plan.work or self.busy:
            return
        self.busy = True
        self.apply.setEnabled(False)
        self.out.setPlainText("")

        def step(text, ok):
            self.out.appendPlainText(("" if ok else "Problem: ") + text)

        def done(created, errors):
            self.busy = False
            self.out.appendPlainText(f"\nDone. {created} created, {len(errors)} problem(s).")
            if created:
                self.ctx.config.set("wizard_done", True)
                try:
                    self.ctx.config.save()
                except OSError as e:
                    self.out.appendPlainText(f"Could not save settings: {e}")
            self.ctx.store.refresh_scenes()
            self.ctx.store.refresh_inputs()
            detect(self.ctx.bridge.request, self._after_apply)

        Applier(self.ctx.bridge.request, self.plan, step, done).start()

    def _after_apply(self, ok, found):
        if ok:
            self.found = found
            t = self.template()
            plan = build_plan(t, found["scenes"], found["inputs"], found["kinds"], self.env())
            self.out.appendPlainText("\nStill missing: " + (", ".join(
                s.get("scene", "") + ("/" + s["name"] if "name" in s else "") for s in plan.work) or "nothing"))
