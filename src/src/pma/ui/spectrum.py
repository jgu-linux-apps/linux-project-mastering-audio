"""Analizator widma (pasma 1/3 oktawy) w stylu motywu: na żywo w odsłuchu A/B (obie wersje naraz,
słyszana wyróżniona) i jako porównanie średniej barwy albumu z utworem referencyjnym."""
import math

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from ..engine.analysis import BANDS  # noqa: E402
from . import themes  # noqa: E402
from .album_chart import AlbumChart, _minus  # noqa: E402

LABELS = {31.5: "31", 63: "63", 125: "125", 250: "250", 500: "500", 1000: "1k", 2000: "2k", 4000: "4k",
          8000: "8k", 16000: "16k"}


class SpectrumView(AlbumChart):
    """curves: lista dict(values, role 'a'|'b'|'ref'|'album', heard bool, label)."""

    def __init__(self, height=180):
        super().__init__()
        self.set_content_height(height)
        self.curves = []
        self.mode = "abs"          # abs: dBFS (odsłuch na żywo), rel: barwa wyrównana do 200 Hz–5 kHz
        self._top = None
        self.items = [1]           # AlbumChart rysuje tylko, gdy są dane

    def reset(self):
        self._top = None
        self.curves = []
        self.queue_draw()

    def set_curves(self, curves, mode="abs"):
        self.curves, self.mode = curves, mode
        self.queue_draw()

    def _color(self, role):
        pal = themes.palette()
        after = pal["after"]
        if role in ("b", "album"):
            return after[0]
        if role == "ref":
            return pal["target"]
        b = pal["before"]
        if b:
            return b[:3]
        c = self.get_color()
        return (c.red, c.green, c.blue)

    def _draw(self, _area, cr, w, h):
        left, right, top, bottom = 46, 14, 14, 26
        pw, ph = w - left - right, h - top - bottom
        if pw < 40 or ph < 30:
            return
        vals = [v for c in self.curves for v in c["values"] if v > -110]
        if self.mode == "abs":
            hi = math.ceil((max(vals) if vals else -12) / 6) * 6 + 6
            self._top = hi if self._top is None else max(self._top, hi)
            hi = min(self._top, 6)
            lo, step = hi - 72, 12
        else:
            hi, lo, step = 12, -24, 6
        fmin, fmax = math.log10(22), math.log10(22000)

        def X(f):
            return left + (math.log10(f) - fmin) / (fmax - fmin) * pw

        def Y(v):
            return top + (hi - max(lo, min(hi, v))) / (hi - lo) * ph

        # siatka
        cr.set_line_width(1)
        for v in range(int(lo), int(hi) + 1, step):
            self._fg(cr, 0.08 if v != 0 or self.mode == "abs" else 0.22)
            cr.move_to(left, Y(v) + 0.5)
            cr.line_to(left + pw, Y(v) + 0.5)
            cr.stroke()
            self._text(cr, left - 6, Y(v) + 4, _minus(f"{v:+d}" if self.mode == "rel" else f"{v:d}"), 9,
                       "right", 0.55)
        for f, lab in LABELS.items():
            self._fg(cr, 0.06)
            cr.move_to(X(f) + 0.5, top)
            cr.line_to(X(f) + 0.5, top + ph)
            cr.stroke()
            self._text(cr, X(f), h - 8, lab, 9, "center", 0.55)
        self._text(cr, left - 6, top - 2, "dB" if self.mode == "rel" else "dBFS", 8, "right", 0.45)
        # krzywe: najpierw niesłyszana, na wierzchu słyszana
        pal = themes.palette()
        for c in sorted(self.curves, key=lambda c: c.get("heard", False)):
            rgb = self._color(c["role"])
            pts = [(X(f), Y(v)) for f, v in zip(BANDS, c["values"]) if v > -110 and f <= 20000]
            if len(pts) < 2:
                continue
            heard = c.get("heard", True)
            # wypełnienie pod krzywą
            cr.move_to(pts[0][0], top + ph)
            for x, y in pts:
                cr.line_to(x, y)
            cr.line_to(pts[-1][0], top + ph)
            cr.close_path()
            cr.set_source_rgba(*rgb, 0.16 if heard else 0.05)
            cr.fill()
            if pal.get("glow") and heard:
                for lw, a in ((7, 0.08), (4, 0.14)):
                    self._poly(cr, pts)
                    cr.set_source_rgba(*rgb, a)
                    cr.set_line_width(lw)
                    cr.stroke()
            self._poly(cr, pts)
            cr.set_source_rgba(*rgb, 0.95 if heard else 0.45)
            cr.set_line_width(2.2 if heard else 1.4)
            if c.get("dash"):
                cr.set_dash([5, 4])
            cr.stroke()
            cr.set_dash([])
        # legenda
        x = left + 8
        for c in self.curves:
            if not c.get("label"):
                continue
            rgb = self._color(c["role"])
            cr.set_source_rgba(*rgb, 0.95 if c.get("heard", True) else 0.5)
            cr.rectangle(x, top + 4, 10, 3)
            cr.fill()
            x += 14 + self._text(cr, x + 14, top + 10, c["label"], 9, "left", 0.8) + 12

    @staticmethod
    def _poly(cr, pts):
        """Gładka krzywa (Catmull-Rom -> Bézier) przez punkty pasm."""
        cr.move_to(*pts[0])
        for i in range(len(pts) - 1):
            p0 = pts[i - 1] if i else pts[i]
            p1, p2 = pts[i], pts[i + 1]
            p3 = pts[i + 2] if i + 2 < len(pts) else p2
            cr.curve_to(p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6,
                        p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6, p2[0], p2[1])
