"""Oś czasu w stylu Audacity: podkład i wokale jako klipy z falą, przesuwane łapką w poziomie.
Przeciąganie zmienia offset ścieżki (dźwięk przelicza się po puszczeniu), kliknięcie w linijkę przewija,
Ctrl + kółko przybliża, Shift + kółko przesuwa widok, ← → przesuwają zaznaczony klip o 1 ms (Shift: 10 ms),
podwójne kliknięcie zeruje offset."""
import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402
import cairo  # noqa: E402

from . import themes  # noqa: E402
from .album_chart import AlbumChart, _minus  # noqa: E402
from ..engine.vocal import PEAK_HOP_S  # noqa: E402

LABEL_W, OFF_W, RULER_H, LANE_H, GAP = 168, 78, 22, 40, 4
KNOB_R = 10
VOL_RANGE = (-24.0, 12.0)
OFFSET_RANGE = (-600000.0, 600000.0)   # ±10 min – klip w dowolne miejsce toru
DISPLAY_GAIN = 1.6     # fala w prawdziwej skali: ścieżki na wspólnym poziomie (−20 LUFS) mają szczyty ok. −4 dBFS


class Lane:
    """Widok jednej ścieżki: obj ma .offset_ms; peaks/duration to fala BEZ przesunięcia."""

    def __init__(self, obj, name, kind, parent=None):
        self.obj, self.name, self.kind = obj, name, kind     # kind: 'backing' | 'vocal'
        self.parent = parent                                 # klip: tor (Lane), na którym leży
        self.peaks = None
        self.duration = 0.0


class Timeline(AlbumChart):
    def __init__(self, on_offset, on_seek, on_action=None, on_volume=None, on_mute=None, on_solo=None):
        super().__init__()
        self.on_mute, self.on_solo = on_mute, on_solo
        self.on_trim = None              # (ścieżka, od, do) – przycięcie klipu za krawędź
        self._trim = None
        self.on_commit = None            # (rodzaj, obiekt, poprzednia wartość) – do cofania Ctrl+Z
        self.on_offset, self.on_seek, self.on_action, self.on_volume = on_offset, on_seek, on_action, on_volume
        self._knob = None                # (lane, głośność na starcie) – przeciąganie pokrętła
        self.tool = "move"               # move: łapka przesuwa klipy · select: zaznaczanie fragmentu
        self.selection = None            # (krotka ścieżek, początek, koniec) w sekundach osi czasu
        self._sel_drag = None
        self.lanes = []
        self.clips = []
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
        rclick = Gtk.GestureClick(button=3)                      # prawy przycisk: menu (Usuń, Wytnij…)
        rclick.connect("pressed", self._context)
        self.add_controller(rclick)
        self.on_context = None
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
    def set_lanes(self, lanes, clips=()):
        """lanes: tory (ścieżki); clips: osobne klipy (wklejki) leżące na torach – Lane z parent=tor."""
        keep = {id(lane.obj): lane for lane in list(self.lanes) + list(getattr(self, "clips", []))}
        for lane in list(lanes) + list(clips):
            old = keep.get(id(lane.obj))
            if old and lane.peaks is None:
                lane.peaks, lane.duration = old.peaks, old.duration
        self.lanes = list(lanes)
        self.clips = list(clips)
        if self.selected not in [lane.obj for lane in self.lanes + self.clips]:
            self.selected = None
        self._resize()
        self.queue_draw()

    def set_peaks(self, obj, peaks, duration):
        for lane in self.lanes + self.clips:
            if lane.obj is obj:
                lane.peaks, lane.duration = peaks, duration
        self.queue_draw()

    def set_position(self, p):
        _x0, aw = self._area()
        if abs(p - self.position) * aw / max(0.01, self._win()[1]) >= 0.5:   # ruch o mniej niż pół piksela – bez rysowania
            self.position = p
            self.queue_draw()

    def _resize(self):
        self.set_content_height(RULER_H + max(1, len(self.lanes)) * (LANE_H + GAP) + 4)

    # ---------------------------------------------------------------- geometria
    def _total(self):
        ends = [lane.obj.offset_ms / 1000 + lane.duration for lane in self.lanes + self.clips if lane.duration]
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

    def _solo_rect(self, i):
        rx, ry, rw, rh = self._mute_rect(i)
        return rx - rw - 4, ry, rw, rh                          # S na lewo od M

    def _btn_hit(self, x, y, rect, attr):
        lane = self._lane_at(y)
        if not lane or not hasattr(lane.obj, attr):
            return None
        rx, ry, rw, rh = rect(self.lanes.index(lane))
        return lane if rx - 1 <= x <= rx + rw + 1 and ry - 2 <= y <= ry + rh + 2 else None

    def _mute_hit(self, x, y):
        return self._btn_hit(x, y, self._mute_rect, "mute")

    def _solo_hit(self, x, y):
        return self._btn_hit(x, y, self._solo_rect, "solo")

    def _any_solo(self):
        return any(getattr(lane.obj, "solo", False) for lane in self.lanes)

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

    def _extent(self, lane):
        """Ramka klipu w czasie ścieżki (bez offsetu): cały plik, zawężony przycięciami, poszerzony wklejkami."""
        tr = self._trim
        if tr and tr["lane"] is lane:
            return tr["s"], tr["e"]
        s_, e_ = 0.0, lane.duration
        for op in getattr(lane.obj, "ops", []):
            if op[0] == "trim":
                s_, e_ = max(s_, op[1]), min(e_, op[2])
            elif op[0] == "paste":
                s_, e_ = min(s_, op[4]), max(e_, op[4] + op[3] - op[2])
        return s_, max(s_, min(e_, lane.duration))

    def _row_items(self, row):
        """Klipy toru (od wierzchu) i sama ścieżka."""
        return [c for c in reversed(self.clips) if c.parent is row] + [row]

    def _clip_hit(self, x, y):
        row = self._lane_at(y)
        if not row:
            return None
        for lane in self._row_items(row):
            if not lane.duration:
                continue
            s_, e_ = self._extent(lane)
            a = self._x(lane.obj.offset_ms / 1000 + s_)
            b = self._x(lane.obj.offset_ms / 1000 + e_)
            if a <= x <= b:
                return lane
        return None

    def _edge_hit(self, x, y):
        """Lewa / prawa krawędź klipu ścieżki wokalu (±6 px) -> (lane, 'l'|'r')."""
        row = self._lane_at(y)
        if not row or row.kind != "vocal" or x < LABEL_W:
            return None
        for lane in self._row_items(row):
            if not lane.duration:
                continue
            s_, e_ = self._extent(lane)
            off = lane.obj.offset_ms / 1000
            a, b = self._x(off + s_), self._x(off + e_)
            if abs(x - a) <= 6:
                return lane, "l"
            if abs(x - b) <= 6:
                return lane, "r"
        return None

    # ---------------------------------------------------------------- zdarzenia
    def _context(self, _g, _n, x, y):
        self.grab_focus()
        lane = self._clip_hit(x, y) or self._lane_at(y)
        if lane is None or not self.on_context:
            return
        sel = self.selection
        if not (sel and lane.obj in sel[0]):                     # klik poza zaznaczeniem -> wybierz ten klip / tor
            self.selected = lane.obj
            self.queue_draw()
        self.on_context(lane.obj, x, y)

    def set_tool(self, tool):
        self.tool = tool
        self.queue_draw()

    def _selecting(self, g=None):
        st = g.get_current_event_state() if g else 0
        return self.tool == "select" or bool(st & Gdk.ModifierType.SHIFT_MASK)

    def _pressed(self, gesture, n, x, y):
        self.grab_focus()
        solo = self._solo_hit(x, y)
        if solo:
            if self.on_solo:
                self._commit("solo", solo.obj, solo.obj.solo)
                self.on_solo(solo.obj, not solo.obj.solo)
            self.queue_draw()
            return
        mute = self._mute_hit(x, y)
        if mute:
            if self.on_mute:
                self._commit("mute", mute.obj, mute.obj.mute)
                self.on_mute(mute.obj, not mute.obj.mute)
            self.queue_draw()
            return
        knob = self._knob_hit(x, y)
        if knob and n == 2:
            if knob.obj.volume_db != 0.0:
                self._commit("volume", knob.obj, knob.obj.volume_db)
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
        if self._mute_hit(x, y) or self._solo_hit(x, y):
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
            self._sel_drag = (self.lanes.index(lane), self._t(x)) if lane and lane.kind == "vocal" and x >= LABEL_W else None
            self._drag = None
            return
        edge = self._edge_hit(x, y)
        if edge and self.on_trim:                                # przycinanie klipu za krawędź
            ln, side = edge
            s_, e_ = self._extent(ln)
            self._trim = dict(lane=ln, side=side, s0=s_, e0=e_, s=s_, e=e_)
            self._drag = None
            return
        lane = self._clip_hit(x, y)
        self._drag = (lane, lane.obj.offset_ms) if lane else None
        # klip należy do zaznaczenia kilku ścieżek -> przesuwa się cała zaznaczona grupa
        self._group = None
        if lane and self.selection and lane.obj in self.selection[0] and len(self.selection[0]) > 1:
            self._group = ([(ln, ln.obj.offset_ms) for ln in self.lanes if ln.obj in self.selection[0]],
                           self.selection)
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
        if self._trim:
            tr = self._trim
            t = self._t(sx + dx) - tr["lane"].obj.offset_ms / 1000
            if self.snap:
                t = round(t * 100) / 100
            if tr["side"] == "l":
                tr["s"] = max(0.0, min(tr["e0"] - 0.05, t))
            else:
                tr["e"] = max(tr["s0"] + 0.05, min(tr["lane"].duration, t))
            self.queue_draw()
            return
        if self._sel_drag:
            i0, t0 = self._sel_drag                              # przeciąganie w poprzek torów = kilka ścieżek
            _ok, _sx, sy = g.get_start_point()
            _ok, _dx, dy = g.get_offset()
            i1 = int((sy + dy - RULER_H) // (LANE_H + GAP))
            i1 = max(0, min(len(self.lanes) - 1, i1))
            lo, hi = sorted((i0, i1))
            objs = tuple(ln.obj for ln in self.lanes[lo:hi + 1] if ln.kind == "vocal")
            t1 = self._t(sx + dx)
            a, b = sorted((max(0.0, t0), max(0.0, t1)))
            self.selection = (objs, a, b) if b - a > 0.01 and objs else None
            self.selected = self.lanes[i0].obj
            self.queue_draw()
            return
        if not self._drag:
            return
        lane, start = self._drag
        _x0, w = self._area()
        ms = start + dx / w * self._win()[1] * 1000
        self._set(lane, ms, final=False)
        if getattr(self, "_group", None):
            delta = lane.obj.offset_ms - start                      # po przyciąganiu – to samo dla wszystkich
            members, (objs, a, b) = self._group
            for ln, st in members:
                if ln is not lane:
                    ln.obj.offset_ms = float(max(OFFSET_RANGE[0], min(OFFSET_RANGE[1], st + delta)))
            self.selection = (objs, max(0.0, a + delta / 1000), max(0.0, b + delta / 1000))
            self.queue_draw()

    def _end(self, _g, _dx, _dy):
        if self._trim:
            tr, self._trim = self._trim, None
            if abs(tr["s"] - tr["s0"]) > 1e-3 or abs(tr["e"] - tr["e0"]) > 1e-3:
                self.on_trim(tr["lane"].obj, round(tr["s"], 3), round(tr["e"], 3))
            self.queue_draw()
            return
        if self._knob:
            lane, start = self._knob
            if lane.obj.volume_db != start:
                self._commit("volume", lane.obj, start)
            self._knob = None
            self.set_cursor(Gdk.Cursor.new_from_name("default"))
        self._ruler_drag = False
        self._sel_drag = None
        if self._drag:
            lane, start = self._drag
            self._drag = None
            self.set_cursor(Gdk.Cursor.new_from_name("grab"))
            group = getattr(self, "_group", None)
            self._group = None
            if group and lane.obj.offset_ms != start:
                members, _sel = group
                self._commit("offsets", tuple((ln.obj, st) for ln, st in members), None)
                for ln, _st in members:
                    self.on_offset(ln.obj, ln.obj.offset_ms)
            elif lane.obj.offset_ms != start:
                self._commit("offset", lane.obj, start)
                self.on_offset(lane.obj, lane.obj.offset_ms)

    def _set(self, lane, ms, final):
        if self.snap:
            ms = round(ms / 10) * 10
        ms = float(round(max(OFFSET_RANGE[0], min(OFFSET_RANGE[1], ms))))
        old = lane.obj.offset_ms
        lane.obj.offset_ms = ms
        self.queue_draw()
        if final:
            if ms != old:
                self._commit("offset", lane.obj, old)
            self.on_offset(lane.obj, ms)

    def _commit(self, kind, obj, old):
        if self.on_commit:
            self.on_commit(kind, obj, old)

    def _hover(self, _c, x, y):
        name = "grab" if self._clip_hit(x, y) else "pointer" if (y < RULER_H and x >= LABEL_W) else "default"
        if self.tool == "move" and (self._edge_hit(x, y) or self._trim):
            name = "ew-resize"
        if self._knob_hit(x, y) or self._knob:
            name = "ns-resize"
        if self._mute_hit(x, y) or self._solo_hit(x, y):
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
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if self.selection and shift and keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            objs, a0, b0 = self.selection                        # precyzyjne zaznaczanie klawiaturą
            step = (0.001 if state & Gdk.ModifierType.ALT_MASK else 0.01) * (1 if keyval == Gdk.KEY_Right else -1)
            if ctrl:
                a0 = max(0.0, min(b0 - 0.01, a0 + step))         # Ctrl+Shift: początek
            else:
                b0 = max(a0 + 0.01, b0 + step)                   # Shift: koniec
            self.selection = (objs, round(a0, 3), round(b0, 3))
            self.queue_draw()
            return True
        lane = next((lane for lane in self.lanes + self.clips if lane.obj is self.selected), None)
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

    def _toggle_btn(self, cr, rect, label, on, rgb, fg):
        rx, ry, rw, rh = rect
        if on:
            cr.set_source_rgba(*rgb, 0.95)
            self._bar(cr, rx, ry + rh, ry, rw, 4)
            self._text(cr, rx + rw / 2, ry + rh - 4, label, 10, "center", 1.0, bold=True, rgb=fg)
        else:
            self._fg(cr, 0.35)
            cr.set_line_width(1)
            self._bar(cr, rx + 0.5, ry + rh - 0.5, ry + 0.5, rw - 1, 4, fill=False)
            cr.stroke()
            self._text(cr, rx + rw / 2, ry + rh - 4, label, 10, "center", 0.6, bold=True)

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
        self._toggle_btn(cr, self._solo_rect(i), "S", getattr(lane.obj, "solo", False), (0.96, 0.75, 0.15),
                         (0.17, 0.12, 0.0))
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

    def _static_key(self, w, h):
        """Wszystko, od czego zależy warstwa fal i opisów (bez kursora odtwarzania i zaznaczenia)."""
        lanes = tuple((id(ln.obj), id(ln.peaks), ln.duration, ln.name, ln.obj.offset_ms, self._extent(ln),
                       getattr(ln.obj, "volume_db", 0), getattr(ln.obj, "mute", False),
                       getattr(ln.obj, "solo", False), ln.obj is self.selected) for ln in self.lanes + self.clips)
        return (w, h, self._win(), lanes, themes.CURRENT, Adw.StyleManager.get_default().get_dark(),
                self.get_scale_factor(), str(self.get_color()))

    def _draw(self, _area, cr, w, h):
        """Fale 14+ ścieżek rysowane piksel po pikselu są drogie – warstwa statyczna jest zapamiętywana,
        a przy odtwarzaniu (10× na sekundę) dorysowuje się tylko kursor i zaznaczenie."""
        key = self._static_key(w, h)
        if getattr(self, "_cache_key", None) != key:
            sf = cr.get_target().create_similar(cairo.CONTENT_COLOR_ALPHA, w, h)
            self._draw_static(cairo.Context(sf), w, h)
            self._cache, self._cache_key = sf, key
        cr.set_source_surface(self._cache, 0, 0)
        cr.paint()
        self._draw_overlay(cr, w, h)

    def _draw_static(self, cr, w, h):
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
            for lane in [lane] + [c for c in self.clips if c.parent is lane]:   # tor: ścieżka + jej klipy
                sel = lane.obj is self.selected
                off = lane.obj.offset_ms
                if not lane.duration:
                    continue
                rgb = self._colors(lane.kind)
                light = not Adw.StyleManager.get_default().get_dark()
                ink = tuple(c * 0.55 for c in rgb) if light else rgb      # jasny tryb: ciemniejsza fala i obrys
                ext_s, ext_e = self._extent(lane)
                a, b = self._x(off / 1000 + ext_s), self._x(off / 1000 + ext_e)
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
                    elif self._any_solo() and not getattr(lane.obj, "solo", False):
                        amp *= 0.25                                  # zagłuszona przez solo innej ścieżki
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
    def _draw_overlay(self, cr, w, h):
        x0, aw = self._area()
        # zaznaczenie
        if self.selection:
            objs, a, b = self.selection
            first = True
            for i, lane in enumerate(self.lanes):
                if lane.obj in objs:
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
                        if first:
                            lab = f"{int(a // 60)}:{a % 60:06.3f} – {int(b // 60)}:{b % 60:06.3f} · {b - a:.3f} s"
                            if len(objs) > 1:
                                lab += f" · {len(objs)}×"
                            self._text(cr, xa + 4, y + 12, lab.replace(".", ","), 9, "left", 0.9)
                            first = False
        # pozycja odtwarzania
        px = self._x(self.position)
        if x0 <= px <= x0 + aw:
            cr.set_source_rgba(0.85, 0.35, 0.19, 0.95)
            cr.set_line_width(2)
            cr.move_to(px, 4)
            cr.line_to(px, h)
            cr.stroke()
