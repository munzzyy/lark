"""Small vector icons drawn with QPainter so there are no asset files."""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

SIZE = 20


def _pix(draw, color):
    pm = QPixmap(SIZE * 2, SIZE * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), 1.7)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    draw(p)
    p.end()
    return pm


def _live(p):
    p.drawEllipse(QPointF(10, 10), 2.4, 2.4)
    p.drawArc(QRectF(5, 5, 10, 10), 40 * 16, 100 * 16)
    p.drawArc(QRectF(5, 5, 10, 10), 220 * 16, 100 * 16)
    p.drawArc(QRectF(2, 2, 16, 16), 40 * 16, 100 * 16)
    p.drawArc(QRectF(2, 2, 16, 16), 220 * 16, 100 * 16)


def _scenes(p):
    for x in (3, 11):
        for y in (3, 11):
            p.drawRoundedRect(QRectF(x, y, 6, 6), 1.5, 1.5)


def _audio(p):
    for x, h in ((4, 5), (8, 11), (12, 8), (16, 13)):
        p.drawLine(QPointF(x, 10 - h / 2), QPointF(x, 10 + h / 2))


def _camera(p):
    p.drawRoundedRect(QRectF(2.5, 5, 15, 11), 2.5, 2.5)
    p.drawEllipse(QPointF(10, 10.5), 3, 3)
    p.drawLine(QPointF(7, 3.5), QPointF(13, 3.5))


def _overlays(p):
    p.drawRoundedRect(QRectF(2.5, 7, 11, 9), 2, 2)
    path = QPainterPath()
    path.moveTo(6, 4.5)
    path.lineTo(15.5, 4.5)
    path.quadTo(17.5, 4.5, 17.5, 6.5)
    path.lineTo(17.5, 12.5)
    p.drawPath(path)


def _chat(p):
    path = QPainterPath()
    path.addRoundedRect(QRectF(2.5, 3.5, 15, 10.5), 3, 3)
    p.drawPath(path)
    p.drawLine(QPointF(7, 14), QPointF(6, 17.5))
    p.drawLine(QPointF(6, 17.5), QPointF(10, 14))


def _events(p):
    path = QPainterPath()
    path.moveTo(10, 2.5)
    path.lineTo(12.2, 7.6)
    path.lineTo(17.5, 8)
    path.lineTo(13.5, 11.6)
    path.lineTo(14.8, 17)
    path.lineTo(10, 14.1)
    path.lineTo(5.2, 17)
    path.lineTo(6.5, 11.6)
    path.lineTo(2.5, 8)
    path.lineTo(7.8, 7.6)
    path.closeSubpath()
    p.drawPath(path)


def _highlights(p):
    path = QPainterPath()
    path.moveTo(5.5, 2.8)
    path.lineTo(14.5, 2.8)
    path.lineTo(14.5, 17.2)
    path.lineTo(10, 13.2)
    path.lineTo(5.5, 17.2)
    path.closeSubpath()
    p.drawPath(path)


def _settings(p):
    for y, x in ((5, 7), (10, 13), (15, 8)):
        p.drawLine(QPointF(3, y), QPointF(x - 2.2, y))
        p.drawLine(QPointF(x + 2.2, y), QPointF(17, y))
        p.drawEllipse(QPointF(x, y), 2.2, 2.2)


DRAW = {"live": _live, "scenes": _scenes, "audio": _audio, "camera": _camera,
        "overlays": _overlays, "chat": _chat, "events": _events, "highlights": _highlights,
        "settings": _settings}


def _deck(p):
    for x in (3, 8, 13):
        for y in (3, 8, 13):
            p.drawEllipse(QRectF(x, y, 4, 4))


DRAW["deck"] = _deck


def nav_icon(name, off="#8a92a3", on="#ffffff"):
    ic = QIcon()
    ic.addPixmap(_pix(DRAW[name], off), QIcon.Normal, QIcon.Off)
    ic.addPixmap(_pix(DRAW[name], on), QIcon.Normal, QIcon.On)
    return ic
