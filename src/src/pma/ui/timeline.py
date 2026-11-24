"""Oś czasu w stylu Audacity: podkład i wokale jako klipy z falą, przesuwane łapką w poziomie.
Przeciąganie zmienia offset ścieżki (dźwięk przelicza się po puszczeniu), kliknięcie w linijkę przewija,
Ctrl + kółko przybliża, Shift + kółko przesuwa widok, ← → przesuwają zaznaczony klip o 1 ms (Shift: 10 ms),
podwójne kliknięcie zeruje offset."""
import math

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from . import themes  # noqa: E402
from .album_chart import AlbumChart, _minus  # noqa: E402
from ..engine.vocal import PEAK_HOP_S  # noqa: E402

LABEL_W, OFF_W, RULER_H, LANE_H, GAP = 118, 78, 22, 40, 4
OFFSET_RANGE = (-2000.0, 2000.0)
DISPLAY_GAIN = 1.6     # fala w prawdziwej skali: ścieżki na wspólnym poziomie (−20 LUFS) mają szczyty ok. −4 dBFS


class Lane:
    """Widok jednej ścieżki: obj ma .offset_ms; peaks/duration to fala BEZ przesunięcia."""

    def __init__(self, obj, name, kind):
        self.obj, self.name, self.kind = obj, name, kind     # kind: 'backing' | 'vocal'
        self.peaks = None
        self.duration = 0.0


class Timeline(AlbumChart):
    def __init__(self, on_offset, on_seek, on_action=None):
        super().__init__()
        self.on_offset, self.on_seek, self.on_action = on_offset, on_seek, on_action
        self.tool = "move"               # move: łapka przesuwa klipy · select: zaznaczanie fragmentu
        self.selection = None            # (obj, początek, koniec) w sekundach osi czasu
        self._sel_drag = None
        self.lanes = []
        self.items = [1]
        self.position = 0.0
        self.view = (0.0, 0.0)           # (początek, długość) widoku w sekundach; długość 0 = całość
        self.snap = True
        self.selected = None
        self._drag = None                # (lane, offset na starcie)
        self._ruler_drag = False
        self.set_focusable(True)
        self.set_can_focus(True)
        self.set_cursor(Gdk.Cursor.new_from_name("default"))

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.add_controller(drag)
        click = Gtk.GestureClick()
        click.connect("pressed", self._pressed)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._hover)
        self.add_controller(motion)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self._resize()

    # ---------------------------------------------------------------- dane
    def set_lanes(self, lanes):
        keep = {id(lane.obj): lane for lane in self.lanes}
        new = []
        for lane in lanes:
            old = keep.get(id(lane.obj))
            if old and lane.peaks is None:
                lane.peaks, lane.duration = old.peaks, old.duration
            new.append(lane)
        self.lanes = new
        if self.selected not in [lane.obj for lane in self.lanes]:
            self.selected = None
        self._resize()
        self.queue_draw()

    def set_peaks(self, obj, peaks, duration):
        for lane in self.lanes:
            if lane.obj is obj:
                lane.peaks, lane.duration = peaks, duration
        self.queue_draw()

    def set_position(self, p):
        if abs(p - self.position) > 0.005:
            self.position = p
            self.queue_draw()

    def _resize(self):
        self.set_content_height(RULER_H + max(1, len(self.lanes)) * (LANE_H + GAP) + 4)

    # ---------------------------------------------------------------- geometria
    def _total(self):
        ends = [lane.obj.offset_ms / 1000 + lane.duration for lane in self.lanes if lane.duration]
        return max([10.0] + ends) + 0.5

    def _win(self):
        start, length = self.view
        if length <= 0:
            return 0.0, self._total()
        return start, length

    def _area(self):
        return LABEL_W, max(40, self.get_width() - LABEL_W - OFF_W)

    def _x(self, t):
        x0, w = self._area()
        start, length = self._win()
        return x0 + (t - start) / length * w

    def _t(self, x):
        x0, w = self._area()
        start, length = self._win()
        return start + (x - x0) / w * length

    def _lane_at(self, y):
        i = int((y - RULER_H) // (LANE_H + GAP))
        return self.lanes[i] if y >= RULER_H and 0 <= i < len(self.lanes) else None

    def _clip_hit(self, x, y):
        lane = self._lane_at(y)
        if not lane or not lane.duration:
            return None
        a = self._x(lane.obj.offset_ms / 1000)
        b = self._x(lane.obj.offset_ms / 1000 + lane.duration)
        return lane if a <= x <= b else None

    # ---------------------------------------------------------------- zdarzenia
    def set_tool(self, tool):
        self.tool = tool
        self.queue_draw()

    def _selecting(self, g=None):
        st = g.get_current_event_state() if g else 0
        return self.tool == "select" or bool(st & Gdk.ModifierType.SHIFT_MASK)

    def _pressed(self, gesture, n, x, y):
        self.grab_focus()
        if y < RULER_H and x >= LABEL_W:
            self._ruler_drag = True
            self.on_seek(max(0.0, self._t(x)))
            return
        if self._selecting(gesture) and x >= LABEL_W:
            lane = self._lane_at(y)
            if lane and lane.kind == "vocal":
                self.selected = lane.obj
                self.selection = None
                self.on_seek(max(0.0, self._t(x)))           # klik = kursor wklejania w tym miejscu
            self.queue_draw()
            return
        lane = self._clip_hit(x, y)
        self.selected = lane.obj if lane else None
        if lane and n == 2:
            self._set(lane, 0.0, final=True)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.queue_draw()

    def _begin(self, g, x, y):
        if y < RULER_H:
            return
        if self._selecting(g):
            lane = self._lane_at(y)
            self._sel_drag = (lane, self._t(x)) if lane and lane.kind == "vocal" and x >= LABEL_W else None
            self._drag = None
            return
        lane = self._clip_hit(x, y)
        self._drag = (lane, lane.obj.offset_ms) if lane else None
        if lane:
            self.set_cursor(Gdk.Cursor.new_from_name("grabbing"))

    def _update(self, g, dx, _dy):
        ok, sx, _sy = g.get_start_point()
        if self._ruler_drag:
            self.on_seek(max(0.0, self._t(sx + dx)))
            return
        if self._sel_drag:
            lane, t0 = self._sel_drag
            t1 = self._t(sx + dx)
            a, b = sorted((max(0.0, t0), max(0.0, t1)))
            self.selection = (lane.obj, a, b) if b - a > 0.01 else None
            self.selected = lane.obj
            self.queue_draw()
            return
        if not self._drag:
            return
        lane, start = self._drag
        _x0, w = self._area()
        ms = start + dx / w * self._win()[1] * 1000
        self._set(lane, ms, final=False)

    def _end(self, _g, _dx, _dy):
        self._ruler_drag = False
        self._sel_drag = None
        if self._drag:
            lane, start = self._drag
            self._drag = None
            self.set_cursor(Gdk.Cursor.new_from_name("grab"))
            if lane.obj.offset_ms != start:
                self.on_offset(lane.obj, lane.obj.offset_ms)

    def _set(self, lane, ms, final):
        if self.snap:
            ms = round(ms / 10) * 10
        ms = float(round(max(OFFSET_RANGE[0], min(OFFSET_RANGE[1], ms))))
        lane.obj.offset_ms = ms
        self.queue_draw()
        if final:
            self.on_offset(lane.obj, ms)

    def _hover(self, _c, x, y):
        name = "grab" if self._clip_hit(x, y) else "pointer" if (y < RULER_H and x >= LABEL_W) else "default"
        if self.tool == "select" and y >= RULER_H and x >= LABEL_W:
            name = "text"
        if self._drag:
            name = "grabbing"
        self.set_cursor(Gdk.Cursor.new_from_name(name))

    def _scroll(self, ctl, dx, dy):
        state = ctl.get_current_event_state()
        start, length = self._win()
        total = self._total()
        if state & Gdk.ModifierType.CONTROL_MASK:              # zoom wokół pozycji odtwarzania
            new = max(0.5, min(total, length * (1.25 if dy > 0 else 0.8)))
            c = self.position if start <= self.position <= start + length else start + length / 2
            start = c - (c - start) * new / length
            length = new
        elif state & Gdk.ModifierType.SHIFT_MASK or dx:
            start += (dy or dx) * length * 0.1
        else:
            return False                                         # zwykłe kółko przewija stronę
        if length >= total - 1e-6:
            self.view = (0.0, 0.0)
        else:
            self.view = (max(0.0, min(total - length, start)), length)
        self.queue_draw()
        return True

    def _key(self, _c, keyval, _code, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        act = None
        if ctrl and keyval in (Gdk.KEY_c, Gdk.KEY_C):
            act = "copy"
        elif ctrl and keyval in (Gdk.KEY_x, Gdk.KEY_X):
            act = "cut"
        elif ctrl and keyval in (Gdk.KEY_v, Gdk.KEY_V):
            act = "paste"
        elif ctrl and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
            act = "undo"
        elif keyval in (Gdk.KEY_Delete, Gdk.KEY_m, Gdk.KEY_M):
            act = "silence"
        elif keyval == Gdk.KEY_space:
            act = "play"
        elif keyval == Gdk.KEY_Escape:
            self.selection = None
            self.queue_draw()
            return True
        if act and self.on_action:
            self.on_action(act)
            return True
        lane = next((lane for lane in self.lanes if lane.obj is self.selected), None)
        if not lane or keyval not in (Gdk.KEY_Left, Gdk.KEY_Right):
            return False
        step = 10 if state & Gdk.ModifierType.SHIFT_MASK else 1
        snap, self.snap = self.snap, False
        self._set(lane, lane.obj.offset_ms + (step if keyval == Gdk.KEY_Right else -step), final=True)
        self.snap = snap
        return True

    # ---------------------------------------------------------------- rysowanie
    def _colors(self, kind):
        pal = themes.palette()
        if kind == "backing":
            return pal["target"]
        return pal["after"][0]

    def _draw(self, _area, cr, w, h):
        x0, aw = self._area()
        start, length = self._win()
        # linijka
        step = next(s for s in (0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120) if aw / (length / s) >= 70)
        t = math.floor(start / step) * step
        cr.set_line_width(1)
        while t <= start + length:
            x = self._x(t)
            if x >= x0 - 1:
                self._fg(cr, 0.18)
                cr.move_to(int(x) + 0.5, RULER_H - 6)
                cr.line_to(int(x) + 0.5, h)
                cr.stroke()
                lab = f"{int(t // 60)}:{t % 60:04.1f}" if step < 1 else f"{int(t // 60)}:{int(round(t % 60)):02d}"
                self._text(cr, x + 3, RULER_H - 8, lab.replace(".", ","), 9, "left", 0.55)
            t += step
        self._text(cr, w - 6, RULER_H - 8, "offset", 9, "right", 0.45)
        rr = themes.palette()["radius"]
        for i, lane in enumerate(self.lanes):
            y = RULER_H + i * (LANE_H + GAP)
            self._fg(cr, 0.05)
            self._bar(cr, x0, y + LANE_H, y, aw, 4)
            sel = lane.obj is self.selected
            self._text(cr, 6, y + LANE_H / 2 + 4, lane.name[:16], 10, "left", 0.9 if sel else 0.7, bold=sel)
            off = lane.obj.offset_ms
            self._text(cr, w - 6, y + LANE_H / 2 + 4, _minus(f"{off:+.0f} ms") if abs(off) >= 0.5 else "0 ms", 10,
                       "right", 0.9 if abs(off) >= 0.5 else 0.5)
            if not lane.duration:
                self._text(cr, x0 + 8, y + LANE_H / 2 + 4, "…", 10, "left", 0.5)
                continue
            rgb = self._colors(lane.kind)
            a, b = self._x(off / 1000), self._x(off / 1000 + lane.duration)
            ca, cb = max(a, x0), min(b, x0 + aw)
            if cb <= ca:
                continue
            cr.save()
            cr.rectangle(x0, y, aw, LANE_H)
            cr.clip()
            cr.set_source_rgba(*rgb, 0.22 if sel else 0.14)
            self._bar(cr, a, y + LANE_H - 2, y + 2, b - a, min(rr, 5))
            cr.set_source_rgba(*rgb, 1.0 if sel else 0.55)
            cr.set_line_width(2 if sel else 1)
            self._bar(cr, a + 0.5, y + LANE_H - 2.5, y + 2.5, b - a - 1, min(rr, 5), fill=False)
            cr.stroke()
            if lane.peaks is not None and len(lane.peaks):     # fala: jedna kreska na piksel, z suwakiem głośności
                mid = y + LANE_H / 2
                amp = (LANE_H / 2 - 5) * DISPLAY_GAIN * 10 ** (getattr(lane.obj, "volume_db", 0.0) / 20)
                if getattr(lane.obj, "mute", False):
                    amp = 0.0
                cr.set_source_rgba(*rgb, 0.9)
                cr.set_line_width(1)
                n = len(lane.peaks)
                for px in range(int(ca), int(cb)):
                    t0 = self._t(px) - off / 1000
                    t1 = self._t(px + 1) - off / 1000
                    i0, i1 = int(t0 / PEAK_HOP_S), int(math.ceil(t1 / PEAK_HOP_S))
                    if i1 <= 0 or i0 >= n:
                        continue
                    v = min(LANE_H / 2 - 3, float(lane.peaks[max(0, i0):max(i0 + 1, min(n, i1))].max()) * amp)
                    cr.move_to(px + 0.5, mid - max(0.5, v))
                    cr.line_to(px + 0.5, mid + max(0.5, v))
                cr.stroke()
            cr.restore()
        # zaznaczenie
        if self.selection:
            obj, a, b = self.selection
            for i, lane in enumerate(self.lanes):
                if lane.obj is obj:
                    y = RULER_H + i * (LANE_H + GAP)
                    xa, xb = max(x0, self._x(a)), min(x0 + aw, self._x(b))
                    if xb > xa:
                        cr.set_source_rgba(1.0, 1.0, 1.0, 0.28)
                        cr.rectangle(xa, y, xb - xa, LANE_H)
                        cr.fill()
                        cr.set_source_rgba(0.85, 0.35, 0.19, 0.9)
                        cr.set_line_width(1)
                        cr.rectangle(xa + 0.5, y + 0.5, xb - xa - 1, LANE_H - 1)
                        cr.stroke()
                        self._text(cr, xa + 4, y + 12, f"{b - a:.2f} s".replace(".", ","), 9, "left", 0.9)
        # pozycja odtwarzania
        px = self._x(self.position)
        if x0 <= px <= x0 + aw:
            cr.set_source_rgba(0.85, 0.35, 0.19, 0.95)
            cr.set_line_width(2)
            cr.move_to(px, 4)
            cr.line_to(px, h)
            cr.stroke()
