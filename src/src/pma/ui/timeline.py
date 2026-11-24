"""Oś czasu w stylu Audacity: podkład i wokale jako klipy z falą, przesuwane łapką w poziomie.
Przeciąganie zmienia offset ścieżki (dźwięk przelicza się po puszczeniu), kliknięcie w linijkę przewija,
Ctrl + kółko przybliża, Shift + kółko przesuwa widok, ← → przesuwają zaznaczony klip o 1 ms (Shift: 10 ms),
podwójne kliknięcie zeruje offset."""
import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from . import themes  # noqa: E402
from .album_chart import AlbumChart, _minus  # noqa: E402
from ..engine.vocal import PEAK_HOP_S  # noqa: E402

LABEL_W, OFF_W, RULER_H, LANE_H, GAP = 168, 78, 22, 40, 4
KNOB_R = 10
VOL_RANGE = (-24.0, 12.0)
OFFSET_RANGE = (-2000.0, 2000.0)
DISPLAY_GAIN = 1.6     # fala w prawdziwej skali: ścieżki na wspólnym poziomie (−20 LUFS) mają szczyty ok. −4 dBFS


class Lane:
    """Widok jednej ścieżki: obj ma .offset_ms; peaks/duration to fala BEZ przesunięcia."""

    def __init__(self, obj, name, kind):
        self.obj, self.name, self.kind = obj, name, kind     # kind: 'backing' | 'vocal'
        self.peaks = None
        self.duration = 0.0


class Timeline(AlbumChart):
    def __init__(self, on_offset, on_seek, on_action=None, on_volume=None, on_mute=None):
        super().__init__()
        self.on_mute = on_mute
        self.on_offset, self.on_seek, self.on_action, self.on_volume = on_offset, on_seek, on_action, on_volume
        self._knob = None                # (lane, głośność na starcie) – przeciąganie pokrętła
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

        drag = Gtk.GestureDrag(button=0)           # lewy: klipy / zaznaczanie / pokrętło · środkowy: głośność
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

    def _knob_xy(self, i):
        return LABEL_W - KNOB_R - 8, RULER_H + i * (LANE_H + GAP) + LANE_H / 2

    def _mute_rect(self, i):
        kx, ky = self._knob_xy(i)
        return kx - KNOB_R - 30, ky - 1, 20, 17          # pod wartością dB, obok pokrętła

    def _mute_hit(self, x, y):
        lane = self._lane_at(y)
        if not lane or not hasattr(lane.obj, "mute"):
            return None
        rx, ry, rw, rh = self._mute_rect(self.lanes.index(lane))
        return lane if rx - 2 <= x <= rx + rw + 2 and ry - 2 <= y <= ry + rh + 2 else None

    def _knob_hit(self, x, y):
        lane = self._lane_at(y)
        if not lane or not hasattr(lane.obj, "volume_db"):
            return None
        kx, ky = self._knob_xy(self.lanes.index(lane))
        return lane if (x - kx) ** 2 + (y - ky) ** 2 <= (KNOB_R + 4) ** 2 else None

    def _set_volume(self, lane, db):
        db = round(max(VOL_RANGE[0], min(VOL_RANGE[1], db)) * 2) / 2
        if db != lane.obj.volume_db and self.on_volume:
            self.on_volume(lane.obj, db)
        self.queue_draw()

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
        mute = self._mute_hit(x, y)
        if mute:
            if self.on_mute:
                self.on_mute(mute.obj, not mute.obj.mute)
            self.queue_draw()
            return
        knob = self._knob_hit(x, y)
        if knob and n == 2:
            self._set_volume(knob, 0.0)
            return
        if knob:
            return
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
        self._knob = None
        if y < RULER_H:
            return
        btn = g.get_current_button()
        if self._mute_hit(x, y):
            return
        lane = self._knob_hit(x, y) if btn == 1 else self._lane_at(y) if btn == 2 else None
        if btn != 1:
            if lane and hasattr(lane.obj, "volume_db"):
                self._knob = (lane, lane.obj.volume_db)
                self.set_cursor(Gdk.Cursor.new_from_name("ns-resize"))
            return
        if lane:
            self._knob = (lane, lane.obj.volume_db)
            self.set_cursor(Gdk.Cursor.new_from_name("ns-resize"))
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
        if self._knob:
            lane, start = self._knob
            _ok, _x, dy = g.get_offset()
            self._set_volume(lane, start - dy * 0.15)          # w górę = głośniej, ok. 7 px na 1 dB
            return
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
        if self._knob:
            self._knob = None
            self.set_cursor(Gdk.Cursor.new_from_name("default"))
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
        if self._knob_hit(x, y) or self._knob:
            name = "ns-resize"
        if self._mute_hit(x, y):
            name = "pointer"
        if self.tool == "select" and y >= RULER_H and x >= LABEL_W:
            name = "text"
        if self._drag:
            name = "grabbing"
        self.set_cursor(Gdk.Cursor.new_from_name(name))

    def _scroll(self, ctl, dx, dy):
        state = ctl.get_current_event_state()
        ev = ctl.get_current_event()
        ok, px, py = ev.get_position() if ev else (False, 0, 0)
        if ok and px < LABEL_W and not state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
            lane = self._lane_at(py)                             # kółko nad nazwą ścieżki = głośność ±0,5 dB
            if lane and hasattr(lane.obj, "volume_db") and dy:
                self._set_volume(lane, lane.obj.volume_db - 0.5 * (1 if dy > 0 else -1))
                return True
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

    def _knob_draw(self, cr, i, lane):
        """Pokrętło głośności: łuk 270° od −24 do +12 dB, znacznik 0 dB, wartość pod nazwą."""
        if not hasattr(lane.obj, "volume_db"):
            return
        db = lane.obj.volume_db
        mute = getattr(lane.obj, "mute", False)
        kx, ky = self._knob_xy(i)
        y = RULER_H + i * (LANE_H + GAP)
        self._text(cr, 6, y + LANE_H / 2 + 12, _minus(f"{db:+.1f} dB").replace(".", ","), 9, "left",
                   0.35 if mute else 0.85 if abs(db) >= 0.25 else 0.5)
        rx, ry, rw, rh = self._mute_rect(i)                     # przycisk M (wycisz)
        if mute:
            cr.set_source_rgba(0.89, 0.29, 0.29, 0.95)
            self._bar(cr, rx, ry + rh, ry, rw, 4)
            self._text(cr, rx + rw / 2, ry + rh - 4, "M", 10, "center", 1.0, bold=True, rgb=(1, 1, 1))
        else:
            self._fg(cr, 0.35)
            cr.set_line_width(1)
            self._bar(cr, rx + 0.5, ry + rh - 0.5, ry + 0.5, rw - 1, 4, fill=False)
            cr.stroke()
            self._text(cr, rx + rw / 2, ry + rh - 4, "M", 10, "center", 0.6, bold=True)
        a0, a1 = math.radians(135), math.radians(405)
        frac = (db - VOL_RANGE[0]) / (VOL_RANGE[1] - VOL_RANGE[0])
        zero = (0 - VOL_RANGE[0]) / (VOL_RANGE[1] - VOL_RANGE[0])
        cr.set_line_width(3)
        cr.new_path()
        self._fg(cr, 0.15)
        cr.arc(kx, ky, KNOB_R, a0, a1)
        cr.stroke()
        rgb = self._colors(lane.kind)
        light = not Adw.StyleManager.get_default().get_dark()
        ink = tuple(c * 0.55 for c in rgb) if light else rgb
        cr.set_source_rgba(*ink, 0.35 if mute else 0.95)
        lo, hi = sorted((a0 + (a1 - a0) * zero, a0 + (a1 - a0) * frac))
        cr.new_path()
        cr.arc(kx, ky, KNOB_R, lo, max(hi, lo + 0.02))
        cr.stroke()
        ang = a0 + (a1 - a0) * frac                              # wskazówka
        self._fg(cr, 0.8)
        cr.set_line_width(2)
        cr.move_to(kx + math.cos(ang) * 3, ky + math.sin(ang) * 3)
        cr.line_to(kx + math.cos(ang) * (KNOB_R - 3), ky + math.sin(ang) * (KNOB_R - 3))
        cr.stroke()

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
            self._text(cr, 6, y + LANE_H / 2 - 2, lane.name[:22], 10, "left", 0.9 if sel else 0.75, bold=sel)
            self._knob_draw(cr, i, lane)
            off = lane.obj.offset_ms
            self._text(cr, w - 6, y + LANE_H / 2 + 4, _minus(f"{off:+.0f} ms") if abs(off) >= 0.5 else "0 ms", 10,
                       "right", 0.9 if abs(off) >= 0.5 else 0.5)
            if not lane.duration:
                self._text(cr, x0 + 8, y + LANE_H / 2 + 4, "…", 10, "left", 0.5)
                continue
            rgb = self._colors(lane.kind)
            light = not Adw.StyleManager.get_default().get_dark()
            ink = tuple(c * 0.55 for c in rgb) if light else rgb      # jasny tryb: ciemniejsza fala i obrys
            a, b = self._x(off / 1000), self._x(off / 1000 + lane.duration)
            ca, cb = max(a, x0), min(b, x0 + aw)
            if cb <= ca:
                continue
            cr.save()
            cr.rectangle(x0, y, aw, LANE_H)
            cr.clip()
            cr.set_source_rgba(*rgb, (0.16 if sel else 0.08) if light else (0.22 if sel else 0.14))
            self._bar(cr, a, y + LANE_H - 2, y + 2, b - a, min(rr, 5))
            cr.set_source_rgba(*ink, 1.0 if sel else 0.55)
            cr.set_line_width(2 if sel else 1)
            self._bar(cr, a + 0.5, y + LANE_H - 2.5, y + 2.5, b - a - 1, min(rr, 5), fill=False)
            cr.stroke()
            if lane.peaks is not None and len(lane.peaks):     # fala: jedna kreska na piksel, z suwakiem głośności
                mid = y + LANE_H / 2
                amp = (LANE_H / 2 - 5) * DISPLAY_GAIN * 10 ** (getattr(lane.obj, "volume_db", 0.0) / 20)
                if getattr(lane.obj, "mute", False):
                    amp = 0.0
                cr.set_source_rgba(*ink, 0.95)
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
                        light = not Adw.StyleManager.get_default().get_dark()
                        cr.set_source_rgba(*((0.85, 0.35, 0.19, 0.12) if light else (1.0, 1.0, 1.0, 0.28)))
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
