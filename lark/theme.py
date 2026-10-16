from pathlib import Path

from PySide6.QtGui import QColor

TOKENS = {
    "bg": "#0e1014", "surface": "#161920", "surface2": "#1d212a", "border": "#2a2f3b",
    "text": "#e8eaf0", "muted": "#8a92a3", "disabled": "#5a6172", "disabled_bar": "#3a4050",
    "hover": "#262b37", "pressed": "#1a1e27", "border_hover": "#3d4558", "scroll": "#2f3542", "scroll_hover": "#3d4454",
    "good": "#3ddc97", "bad": "#ff5d6c", "warn": "#ffb454",
}


def _mix(hex_color, alpha, base=TOKENS["surface"]):
    c, b = QColor(hex_color), QColor(base)
    mixed = [round(c.red() * alpha + b.red() * (1 - alpha)),
             round(c.green() * alpha + b.green() * (1 - alpha)),
             round(c.blue() * alpha + b.blue() * (1 - alpha))]
    return QColor(*mixed).name()


def build_stylesheet(accent="#7c5cff"):
    t = dict(TOKENS)
    t["accent"] = accent
    t["accent_hover"] = QColor(accent).lighter(115).name()
    t["accent_soft"] = _mix(accent, 0.18)
    t["good_soft"] = _mix(t["good"], 0.16)
    t["bad_soft"] = _mix(t["bad"], 0.16)
    t["warn_soft"] = _mix(t["warn"], 0.16)
    css = (Path(__file__).parent / "theme.qss").read_text()
    for k, v in t.items():
        css = css.replace(f"@{k}@", v)
    return css
