"""Zakładka WOKAL: 1–20 ścieżek, 10 opcji masteringu + MANUAL (kafle z suwakami), suwaki głośności i pogłosu/echa
działające na żywo, przycisk SCAL WSZYSTKIE ŚCIEŻKI W JEDNĄ."""
import os
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

import soundfile as sf  # noqa: E402

from ..engine import scratch as SC  # noqa: E402
from ..engine import vocal as V  # noqa: E402
from ..i18n import count, num, plural, tr  # noqa: E402
from . import common as C  # noqa: E402
from .. import i18n  # noqa: E402
from .. import log as L  # noqa: E402
from .mixer import LiveMixer  # noqa: E402
from .timeline import Lane, Timeline  # noqa: E402

MAX_TRACKS = 20
LETTERS = "ABCDEFGHIJKLMNOPQRST"


class VTrack:
    def __init__(self, path, preset):
        self.path = path
        self.preset = preset
        self.volume_db = 0.0
        self.fx = V.get_preset(preset).fx_default
        self.mute = False
        self.solo = False
        self.offset_ms = 0.0
        self.ops = []                       # edycje z osi czasu (wyciszenia, wklejenia) – czas ścieżki, bez offsetu
        self.clips = []                     # osobne klipy (wklejki) na torze tej ścieżki
        self.dry = self.wet = None          # pliki dla miksera (już przesunięte o offset_ms)
        self.raw = None                     # (dry, wet) prosto z renderu – przed przesunięciem
        self.duration = 0.0
        self.token = 0
        self.shift_token = 0
        self.error = ""
        self.w = {}


class Clip(VTrack):
    """Osobny klip na torze ścieżki (wklejony fragment): własne przesunięcie i przycięcie, a głośność, pogłos,
    M i S – po torze (ścieżce-rodzicu). Dźwięk: przetworzony plik źródłowy, przycięty do fragmentu."""
    _INHERIT = ("volume_db", "fx", "mute", "solo")

    def __init__(self, parent, path, preset):
        object.__setattr__(self, "parent", parent)
        super().__init__(path, preset)

    def __getattribute__(self, name):
        if name in Clip._INHERIT:
            return getattr(object.__getattribute__(self, "parent"), name)
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        if name in Clip._INHERIT:
            return                                          # ustawienia toru – klip ich nie nadpisuje
        object.__setattr__(self, name, value)


class Backing:
    """Podkład (instrumental): tylko do odsłuchu albo także do scalonego pliku."""
    def __init__(self, path):
        self.path = path
        self.volume_db = 0.0
        self.mute = False
        self.solo = False
        self.in_mix = False
        self.fx = 0
        self.offset_ms = 0.0
        self.raw = None
        self.shift_token = 0
        self.dry = None
        self.wet = None
        self.duration = 0.0
        self.error = ""


class VocalPage(Gtk.Box):
    def __init__(self, win, executor):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.ex = executor
        self.tracks = []
        self.backing = None
        self._offset_src = {}
        self.clipboard = None              # (ścieżka źródłowa, od, do) – sekundy czasu ścieżki
        self._quiet = 0                    # >0: zmiana z osi czasu / cofania – suwak nie zapisuje jej drugi raz
        self._pend = {}                    # (rodzaj, id) -> (obiekt, wartość przed zmianą, timer)
        self._undo = []                    # (rodzaj, obiekt, poprzednia wartość) – Ctrl+Z cofa ostatnią zmianę
        self.fs = None
        self.album_preset = "studio"
        self.busy = False
        self.peak_hold = (-90.0, 0.0)
        self.manual = {**V.MANUAL_DEFAULT, **i18n.load_settings().get("vocal_manual", {})}
        self._manual_src = 0
        self.manual_w = {}
        self.mixer = LiveMixer(on_state=self._sync_play, on_level=self._on_level)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.stack.add_named(C.empty_page(tr("v_drop_title"), tr("v_drop_sub"), self.choose_files), "empty")
        self.stack.add_named(self._main(), "main")
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar.add_css_class("tl-topbar")
        self.tl_bar.set_margin_start(18)
        self.tl_bar.set_margin_end(18)
        self.tl_bar.set_margin_top(4)
        self.tl_bar.set_margin_bottom(4)
        bar.append(self.tl_bar)
        bar.append(Gtk.Separator())
        self.tl_topbar = bar
        self.append(bar)
        self.append(self.stack)
        self.bottom, self.status, self.progress, self.merge_btn = C.bottom_bar(tr("merge"), self.start_merge)
        self.merge_bk_btn = Gtk.Button(label=tr("merge_bk_btn").upper(), tooltip_text=tr("merge_with_bk"))
        self.merge_bk_btn.add_css_class("master-btn")
        self.merge_bk_btn.add_css_class("merge-bk-btn")
        self.merge_bk_btn.connect("clicked", lambda *_: self.start_merge(True))
        self.bottom.insert_child_after(self.merge_bk_btn, self.bottom.get_first_child())
        self.merge_btn.set_label(tr("merge_vocals_btn").upper())
        self.merge_btn.set_tooltip_text(tr("merge_vocals"))
        self.player_bar = self._player_bar()
        self.append(Gtk.Separator())
        self.append(self.player_bar)
        self.append(self.bottom)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", lambda _t, v, *_: (self.add_files(C.collect_audio(v.get_files())[0]), True)[1])
        self.add_controller(drop)
        GLib.timeout_add(150, self._tick)
        self._update()

    # ================================================================ UI
    def _main(self):
        # szersza strona niż standardowe 600 px Adwaity – kafle MANUAL mieszczą się po 3–4 w rzędzie
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28)
        for m in ("top", "bottom", "start", "end"):
            getattr(col, f"set_margin_{m}")(28 if m in ("start", "end") else 24)
        page = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                  child=Adw.Clamp(maximum_size=3000, tightening_threshold=2600, child=col))
        C.wheel_scrolls_page(page)

        g = Adw.PreferencesGroup(title=tr("v_style"), description=tr("v_style_sub"))
        cards = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                            max_children_per_line=12, min_children_per_line=2, column_spacing=8, row_spacing=8)
        self.cards = {}
        first = None
        for key in V.PRESET_ORDER:
            b = Gtk.ToggleButton()
            b.add_css_class("preset-card")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            n = Gtk.Label(label=tr("vp_" + key), xalign=0)
            n.add_css_class("preset-name")
            d = Gtk.Label(label=tr(f"vp_{key}_d"), xalign=0, wrap=True, max_width_chars=18)
            d.add_css_class("preset-desc")
            box.append(n)
            box.append(d)
            b.set_child(box)
            if first:
                b.set_group(first)
            else:
                first = b
            b.connect("toggled", self._on_card, key)
            self.cards[key] = b
            cards.append(b)
        self.cards["studio"].set_active(True)
        g.add(cards)
        col.append(g)
        col.append(self._timeline_group())

        tg = Adw.PreferencesGroup(title=tr("v_tracks"))
        hb = Gtk.Box(spacing=4)
        lv = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text=tr("level_all_tip"))
        lvb = Gtk.Box(spacing=6)
        lvb.append(Gtk.Image(icon_name="object-flip-vertical-symbolic"))
        lvb.append(Gtk.Label(label=tr("level_all")))
        lv.set_child(lvb)
        lv.add_css_class("flat")
        lv.connect("clicked", lambda *_: self.level_all())
        hb.append(lv)
        for icon, tip, cb in (("list-add-symbolic", tr("add_files"), self.choose_files),
                              ("edit-clear-all-symbolic", tr("clear"), self.clear)):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.connect("clicked", lambda *_, cb=cb: cb())
            hb.append(b)
        tg.set_header_suffix(hb)
        self.list = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, max_children_per_line=3,
                                min_children_per_line=1, column_spacing=12, row_spacing=12)
        tg.add(self.list)
        col.append(self._backing_group())      # podkład nad wokalami – jeden, wokali zwykle kilka
        col.append(tg)
        col.append(self._manual_group())
        return page

    # ---------------------------------------------------------------- MANUAL: kafle z suwakami
    TILES = (("mt_tone", ("lowcut", "body", "mud", "presence", "air")),
             ("mt_dyn", ("comp_thr", "comp_ratio", "attack", "release")),
             ("mt_clean", ("deess", "saturation")),
             ("mt_space", ("reverb_size", "predelay", "echo_ms", "echo_fb")))

    def _manual_group(self):
        g = Adw.PreferencesGroup(title=tr("v_manual"), description=tr("v_manual_sub"), visible=False)
        self.manual_group = g
        reset = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text=tr("mk_reset"), valign=Gtk.Align.CENTER)
        reset.add_css_class("flat")
        reset.connect("clicked", lambda *_: self._manual_reset())
        g.set_header_suffix(reset)
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, max_children_per_line=4,
                           min_children_per_line=2, column_spacing=16, row_spacing=16)
        for title, keys in self.TILES:
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            tile.add_css_class("card")
            tile.add_css_class("manual-tile")
            h = Gtk.Label(label=tr(title), xalign=0)
            h.add_css_class("heading")
            tile.append(h)
            for k in keys:
                lo, hi, step = V.MANUAL_RANGES[k]
                row = Gtk.Box(spacing=8)
                name = Gtk.Label(label=tr("mk_" + k), xalign=0, hexpand=True)
                name.add_css_class("caption")
                val = Gtk.Label(xalign=1)
                val.add_css_class("caption")
                val.add_css_class("numeric")
                row.append(name)
                row.append(val)
                sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
                sc.set_draw_value(False)
                sc.set_value(self.manual[k])
                sc.connect("value-changed", self._on_manual, k)
                tile.append(row)
                tile.append(sc)
                self.manual_w[k] = (sc, val)
                self._manual_label(k)
            flow.append(tile)
        g.add(flow)
        return g

    def _manual_label(self, k):
        v = self.manual[k]
        if k == "lowcut":
            t = f"{v:.0f} Hz"
        elif k in ("body", "mud", "presence", "air"):
            t = f"{num(v, '+.1f')} dB"
        elif k == "comp_thr":
            t = f"{num(v, '.0f')} dB"
        elif k == "comp_ratio":
            t = f"{num(v, '.1f')}:1"
        elif k in ("attack", "release", "predelay"):
            t = f"{v:.0f} ms"
        elif k == "echo_ms":
            t = tr("off") if v < 20 else f"{v:.0f} ms"
        elif k == "reverb_size":
            t = tr("off") if v < 0.2 else f"{num(v, '.1f')} s"
        elif k == "echo_fb":
            t = f"{v * 100:.0f}%"
        else:                                   # deess, saturation
            t = tr("off") if v <= 0 else f"{v:.0f}%"
        self.manual_w[k][1].set_text(t)

    def _on_manual(self, sc, k):
        lo, hi, step = V.MANUAL_RANGES[k]
        self.manual[k] = round(sc.get_value() / step) * step
        self._manual_label(k)
        if self._manual_src:
            GLib.source_remove(self._manual_src)
        self._manual_src = GLib.timeout_add(700, self._manual_apply)   # render dopiero po puszczeniu suwaka

    def _manual_reset(self):
        for k, v in V.MANUAL_DEFAULT.items():
            self.manual_w[k][0].set_value(v)

    def _manual_apply(self):
        self._manual_src = 0
        s = i18n.load_settings()
        s["vocal_manual"] = self.manual
        i18n.save_settings(s)
        for t in self.tracks:
            if t.preset == "manual":
                self._render(t)
        return False

    def _manual_visible(self):
        if not hasattr(self, "manual_group"):          # wołane już przy budowie kart, zanim powstały kafle
            return
        self.manual_group.set_visible(self.album_preset == "manual" or any(t.preset == "manual" for t in self.tracks))

    # ---------------------------------------------------------------- oś czasu
    def _timeline_group(self):
        g = Adw.PreferencesGroup(title=tr("tl_title"), description=tr("tl_sub"), visible=False)
        self.tl_group = g
        snap = Gtk.CheckButton(label=tr("tl_snap"), active=True, valign=Gtk.Align.CENTER)
        snap.connect("toggled", lambda b: setattr(self.timeline, "snap", b.get_active()))
        g.set_header_suffix(snap)
        self.timeline = Timeline(on_offset=self._tl_offset, on_seek=lambda t: self.mixer.seek(t),
                                 on_action=self.tl_action, on_volume=self._tl_volume,
                                 on_mute=self._tl_mute, on_solo=self._tl_solo)
        self.timeline.on_commit = lambda kind, obj, old: self._push_undo((kind, obj, old))
        self.timeline.on_context = self._tl_context
        self.timeline.on_trim = lambda t, a, b: t in self._all() and self._edit(t, ["trim", a, b])
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("card")
        # pasek narzędzi osi czasu jest przypięty na górze zakładki (zawsze widoczny), patrz __init__
        self.tl_bar = self._tl_toolbar()
        for m in ("bottom", "start", "end"):
            getattr(self.timeline, f"set_margin_{m}")(10)
        card.append(self.timeline)
        # suwak przewijania widoku – widoczny po przybliżeniu (Ctrl + kółko / lupa)
        self.tl_adj = Gtk.Adjustment(lower=0, upper=1, page_size=1, step_increment=1, page_increment=10)
        self._tl_adj_h = self.tl_adj.connect("value-changed", self._tl_scrolled)
        self.tl_scroll = Gtk.Scrollbar(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.tl_adj, visible=False)
        from .timeline import LABEL_W, OFF_W
        self.tl_scroll.set_margin_start(10 + LABEL_W)
        self.tl_scroll.set_margin_end(10 + OFF_W)
        self.tl_scroll.set_margin_bottom(8)
        card.append(self.tl_scroll)
        GLib.timeout_add(100, self._tl_sync_scroll)
        g.add(card)
        return g

    def _tl_sync_scroll(self):
        """Suwak = widok osi czasu (początek, długość); po przybliżeniu pokazuje, którą część ścieżki widać."""
        tl = self.timeline
        start, length = tl._win()
        total = tl._total()
        zoomed = tl.view[1] > 0 and length < total - 1e-3
        if zoomed and self.mixer.playing and not (start <= tl.position <= start + length):
            start = max(0.0, min(total - length, tl.position - length * 0.1))   # widok podąża za odtwarzaniem
            tl.view = (start, length)
            tl.queue_draw()
        self.tl_scroll.set_visible(zoomed)
        if zoomed:
            a = self.tl_adj
            if (abs(a.get_upper() - total) > 1e-3 or abs(a.get_page_size() - length) > 1e-3
                    or abs(a.get_value() - start) > 1e-3):
                a.handler_block(self._tl_adj_h)
                a.configure(start, 0, total, length / 20, length * 0.9, length)
                a.handler_unblock(self._tl_adj_h)
        return True

    def _tl_scrolled(self, adj):
        tl = self.timeline
        if tl.view[1] > 0:
            tl.view = (adj.get_value(), tl.view[1])
            tl.queue_draw()

    def _tl_context(self, obj, x, y):
        """Menu pod prawym przyciskiem na osi czasu."""
        ag = Gio.SimpleActionGroup()

        def add(name, cb, enabled=True):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: cb())
            a.set_enabled(enabled)
            ag.add_action(a)
        has_sel = bool(self.timeline.selection)
        add("cut", lambda: self.tl_action("cut"), has_sel)
        add("copy", lambda: self.tl_action("copy"), has_sel)
        add("paste", lambda: self.tl_action("paste"), bool(self.clipboard))
        add("silence", lambda: self.tl_action("silence"), has_sel)
        add("scratch", self.scratch_dialog)
        add("delete", lambda: self._ctx_delete(obj), obj is not self.backing)
        add("delete_bk", lambda: self.set_backing(None), obj is self.backing)
        menu = Gio.Menu()
        edit = Gio.Menu()
        for key, act in (("tl_cut", "cut"), ("tl_copy", "copy"), ("tl_paste", "paste"), ("tl_silence", "silence")):
            edit.append(tr(key), "tl." + act)
        menu.append_section(None, edit)
        sc = Gio.Menu()
        sc.append(tr("sc_btn") + "…", "tl.scratch")
        menu.append_section(None, sc)
        rm = Gio.Menu()
        if obj is self.backing:
            rm.append(tr("ctx_delete_bk"), "tl.delete_bk")
        else:
            rm.append(tr("ctx_delete_clip") if isinstance(obj, Clip) else tr("ctx_delete_track"), "tl.delete")
        menu.append_section(None, rm)
        pop = Gtk.PopoverMenu.new_from_model(menu)
        pop.insert_action_group("tl", ag)
        pop.set_parent(self.timeline)
        r = Gdk.Rectangle()
        r.x, r.y, r.width, r.height = int(x), int(y), 1, 1
        pop.set_pointing_to(r)
        pop.set_has_arrow(False)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def _ctx_delete(self, obj):
        if isinstance(obj, Clip):
            if obj in obj.parent.clips:
                obj.parent.clips.remove(obj)
                self._push_undo(("clip_del", (obj,), None))
                self._clips_changed()
                self.win.toast(tr("tl_clip_deleted"))
        elif obj in self.tracks:
            self.remove(obj)
            self.win.toast(tr("ctx_track_deleted", name=os.path.basename(obj.path)))

    def _tl_toolbar(self):
        bar = Gtk.Box(spacing=6)
        for m in ("top", "start", "end"):
            getattr(bar, f"set_margin_{m}")(10)
        bar.set_margin_bottom(4)

        def btn(icon, tip, cb, label=None):
            b = Gtk.Button(tooltip_text=tip)
            if label:
                box = Gtk.Box(spacing=6)
                box.append(Gtk.Image(icon_name=icon))
                box.append(Gtk.Label(label=label))
                b.set_child(box)
            else:
                b.set_icon_name(icon)
            b.add_css_class("flat")
            b.connect("clicked", lambda *_: cb())
            return b
        self.tl_play = btn("media-playback-start-symbolic", tr("play") + " (Space)", lambda: self.mixer.toggle())
        self.tl_play.add_css_class("circular")
        home = btn("media-skip-backward-symbolic", tr("to_start") + " (Home)", lambda: self.to_start())
        home.add_css_class("circular")
        bar.append(home)
        bar.append(self.tl_play)
        tools = Gtk.Box()
        tools.add_css_class("linked")
        mv = Gtk.ToggleButton(tooltip_text=tr("tl_move_tip"), active=True)
        mv.set_child(self._icon_label("view-pan-symbolic", tr("tl_move")))
        sel = Gtk.ToggleButton(tooltip_text=tr("tl_select_tip"), group=mv)
        sel.set_child(self._icon_label("edit-select-symbolic", tr("tl_select")))
        mv.connect("toggled", lambda b: b.get_active() and self.timeline.set_tool("move"))
        sel.connect("toggled", lambda b: b.get_active() and self.timeline.set_tool("select"))
        tools.append(mv)
        tools.append(sel)
        bar.append(tools)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        for icon, key, act in (("edit-cut-symbolic", "tl_cut", "cut"), ("edit-copy-symbolic", "tl_copy", "copy"),
                               ("edit-paste-symbolic", "tl_paste", "paste"),
                               ("audio-volume-muted-symbolic", "tl_silence", "silence"),
                               ("edit-undo-symbolic", "tl_undo", "undo")):
            bar.append(btn(icon, tr(key + "_tip"), lambda act=act: self.tl_action(act), tr(key)))
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        bar.append(btn("media-playlist-shuffle-symbolic", tr("sc_tip"), lambda: self.scratch_dialog(), tr("sc_btn")))
        sp = Gtk.Box(hexpand=True)
        bar.append(sp)
        for icon, tip, f in (("zoom-out-symbolic", tr("tl_zoom_out"), 1.25), ("zoom-in-symbolic", tr("tl_zoom_in"), 0.8),
                             ("zoom-fit-best-symbolic", tr("tl_zoom_fit"), 0)):
            bar.append(btn(icon, tip, lambda f=f: self._tl_zoom(f)))
        return bar

    @staticmethod
    def _icon_label(icon, text):
        box = Gtk.Box(spacing=6)
        box.append(Gtk.Image(icon_name=icon))
        box.append(Gtk.Label(label=text))
        return box

    def _tl_zoom(self, f):
        tl = self.timeline
        if not f:
            tl.view = (0.0, 0.0)
        else:
            start, length = tl._win()
            total = tl._total()
            new = max(0.5, min(total, length * f))
            c = tl.position if start <= tl.position <= start + length else start + length / 2
            start = c - (c - start) * new / length
            tl.view = (0.0, 0.0) if new >= total - 1e-6 else (max(0.0, min(total - new, start)), new)
        tl.queue_draw()

    # ---------------------------------------------------------------- edycja: wytnij / kopiuj / wklej / wycisz
    def _sel_tracks(self):
        """Zaznaczone ścieżki: [(ścieżka, od, do)] w czasie ścieżki (bez offsetu) + (od, do) na osi czasu."""
        sel = self.timeline.selection
        if not sel:
            return [], None
        objs, a, b = sel
        out = []
        for t in self.tracks:
            if t in objs:
                off = t.offset_ms / 1000
                out.append((t, max(0.0, a - off), max(0.0, b - off)))
                for c in t.clips:                     # klipy (wklejki) na tym torze, które zachodzą na zaznaczenie
                    cs, ce = self._clip_span(c)
                    if ce > a and cs < b:
                        coff = c.offset_ms / 1000
                        out.append((c, max(0.0, max(a, cs) - coff), max(0.0, min(b, ce) - coff)))
        return out, (a, b)

    @staticmethod
    def _clip_span(c):
        """Początek i koniec klipu na osi czasu (z przycięcia)."""
        s_, e_ = 0.0, c.duration or 1e9
        for op in c.ops:
            if op[0] == "trim":
                s_, e_ = max(s_, op[1]), min(e_, op[2])
        off = c.offset_ms / 1000
        return off + s_, off + e_

    def tl_action(self, act):
        if act == "play":
            self.mixer.toggle()
            return
        if self.busy:
            return
        if act == "undo":
            self._flush_pending()
            while self._undo:
                kind, obj, old = self._undo.pop()
                if kind == "offsets":                                     # przesunięcie całej grupy
                    for o, prev in obj:
                        if o is self.backing or o in self._all():
                            self._tl_offset(o, prev)
                    self.timeline.selection = None
                    self.win.toast(tr("tl_undone_offset"))
                    self.timeline.queue_draw()
                    return
                if kind == "ops":
                    tracks = [t for t in (obj if isinstance(obj, tuple) else (obj,)) if t in self._all() and t.ops]
                    if not tracks:
                        continue
                    for t in tracks:
                        t.ops.pop()
                    for t in dict.fromkeys(tracks):
                        self._post(t)
                    L.log(f"cofnięto edycję ({len(tracks)} {plural(len(tracks), 'stem', 'pl')})", tag="edit")
                    self.win.toast(tr("tl_undone_ops"))
                    self.timeline.queue_draw()
                    return
                if kind == "track_add":                                  # cofnij dodanie ścieżki (skrecz z presetu)
                    if obj in self.tracks:
                        self.remove(obj, undo=False)
                    self.win.toast(tr("tl_undone_track_add"))
                    return
                if kind == "track_del":                                  # przywróć usuniętą ścieżkę
                    if obj not in self.tracks:
                        self.tracks.insert(min(old, len(self.tracks)), obj)
                        self._rebuild()
                        self._remix()
                        self._update()
                    self.win.toast(tr("tl_undone_track_del"))
                    return
                if kind == "clip_add":                                   # cofnij wklejenie klipów
                    for c in obj:
                        if c in c.parent.clips:
                            c.parent.clips.remove(c)
                    self._clips_changed()
                    self.win.toast(tr("tl_undone_ops"))
                    return
                if kind == "clip_del":
                    for c in obj:
                        if c.parent in self.tracks and c not in c.parent.clips:
                            c.parent.clips.append(c)
                    self._clips_changed()
                    self.win.toast(tr("tl_undone_ops"))
                    return
                if obj is not self.backing and obj not in self._all():
                    continue                                    # ścieżkę usunięto – pomiń
                if kind == "offset":
                    self._tl_offset(obj, old)
                elif kind == "volume":
                    self._tl_volume(obj, old)
                elif kind == "fx":
                    self._quiet += 1
                    obj.w["fx"].set_value(old)
                    self._quiet -= 1
                elif kind == "mute":
                    self._tl_mute(obj, old)
                elif kind == "solo":
                    self._tl_solo(obj, old)
                L.log(f"{os.path.basename(obj.path)}: cofnięto ({kind})", tag="edit")
                self.win.toast(tr("tl_undone_" + kind))
                self.timeline.queue_draw()
                return
            self.win.toast(tr("tl_nothing_undo"))
            return
        if act == "silence" and not self.timeline.selection and isinstance(self.timeline.selected, Clip):
            c = self.timeline.selected                                   # Delete na klipie = usuń klip
            c.parent.clips.remove(c)
            self._push_undo(("clip_del", (c,), None))
            self.timeline.selected = c.parent
            self._clips_changed()
            self.win.toast(tr("tl_clip_deleted"))
            return
        if act in ("copy", "cut", "silence"):
            items, span = self._sel_tracks()
            if not items:
                self.win.toast(tr("tl_need_sel"))
                return
            if act in ("copy", "cut"):
                # schowek: fragment każdej ścieżki + jej przesunięcie względem początku zaznaczenia na osi czasu
                # razem z edycjami źródła: klip ma dokładnie to, co słychać w zaznaczeniu (wklejki, skrecze…)
                self.clipboard = [(t.path, a, b, (t.offset_ms / 1000 + a) - span[0], [list(o) for o in t.ops],
                                   t.preset) for t, a, b in items]
                L.log(f"skopiowano {span[1] - span[0]:.2f} s z {len(items)} {plural(len(items), 'stem', 'pl')}",
                      tag="edit")
            if act in ("cut", "silence"):
                self._edit_many([(t, ["silence", a, b]) for t, a, b in items])
            d = span[1] - span[0]
            self.win.toast(tr("tl_" + act + "_done", d=d) + (f" · {len(items)}×" if len(items) > 1 else ""))
            return
        if act == "paste":
            if not self.clipboard:
                self.win.toast(tr("tl_empty_clip"))
                return
            tl = self.timeline
            items, span = self._sel_tracks()
            at = span[0] if span else tl.position                  # początek wklejania na osi czasu
            by_path = {t.path: t for t in self.tracks}
            single = len(self.clipboard) == 1
            sel_row = tl.selected.parent if isinstance(tl.selected, Clip) else tl.selected
            new = []
            for path, a, b, rel, src_ops, preset in self.clipboard:
                src = by_path.get(path)
                if src is None:
                    continue
                # jedna ścieżka: na tor zaznaczonej ścieżki; kilka: każda na swój tor, w tym samym ułożeniu.
                # Wklejka to OSOBNY klip – przesuwa się niezależnie od reszty toru.
                row = sel_row if single and sel_row in self.tracks else src
                c = Clip(row, path, preset)
                c.ops = [list(o) for o in src_ops if o[0] != "trim"] + [["trim", a, b]]
                c.offset_ms = round((at + rel - a) * 1000)
                row.clips.append(c)
                new.append(c)
            if not new:
                self.win.toast(tr("tl_src_missing"))
                return
            self._push_undo(("clip_add", tuple(new), None))
            self._clips_changed(render=new)
            tl.selected = new[0]
            d = max(it[2] - it[1] for it in self.clipboard)
            self.win.toast(tr("tl_paste_done", d=d, at=C.fmt_dur(at)) + (f" · {len(new)}×" if len(new) > 1 else ""))

    def to_start(self):
        """Powrót na początek ścieżki (przycisk ⏮ obok Play albo klawisz Home)."""
        self.mixer.seek(0, now=True)
        tl = self.timeline
        tl.position = 0.0
        if tl.view[1] > 0:
            tl.view = (0.0, tl.view[1])
        tl.queue_draw()

    # ---------------------------------------------------------------- skrecze
    def scratch_dialog(self):
        """Skrecze: źródło (zaznaczony wokal albo gotowy dźwięk), gatunek i wzorzec na siatce bitu.
        Tempo, faza uderzeń i „raz” wykrywane z podkładu; preset ląduje na najbliższym takcie."""
        items, span = self._sel_tracks()
        d = Adw.AlertDialog(heading=tr("sc_title"), body=tr("sc_body"))
        grp = Adw.PreferencesGroup()
        src_row = Adw.ComboRow(title=tr("sc_source"),
                               model=Gtk.StringList.new([tr("sc_src_sel")] + [tr("sc_src_" + k) for k in SC.SOURCES]))
        src_row.set_selected(0 if items else 1)
        src_row.set_subtitle(tr("sc_src_sub"))
        genres = list(SC.GENRES)
        genre_row = Adw.ComboRow(title=tr("sc_genre"), model=Gtk.StringList.new([tr("sc_g_" + g) for g in genres]))
        genre_row.set_selected(genres.index("rap"))
        pat_row = Adw.ComboRow(title=tr("sc_pattern"))
        bpm_row = Adw.SpinRow.new_with_range(60, 200, 0.5)
        bpm_row.set_title(tr("sc_bpm"))
        bpm_row.set_digits(1)
        bpm_row.set_value(float(getattr(self, "_bpm", None) or 90.0))
        bars_opts = [2, 4, 8, 16, 32]                      # w uderzeniach: ½ taktu, 1, 2, 4, 8 taktów
        bars = Adw.ComboRow(title=tr("sc_len"), model=Gtk.StringList.new([tr("sc_len_" + str(b)) for b in bars_opts]))
        bars.set_selected(1)
        if span:
            bars.set_subtitle(tr("sc_beats_sub", s=span[1] - span[0]))
        rng = Adw.ComboRow(title=tr("sc_range"), model=Gtk.StringList.new([tr("sc_range_start"), tr("sc_range_all")]))
        for r in (src_row, genre_row, pat_row, bpm_row, bars, rng):
            grp.add(r)

        def fill_patterns(*_):
            g = genres[genre_row.get_selected()]
            pat_row.set_model(Gtk.StringList.new([tr("sc_p_" + k) for k in SC.GENRES[g]]))
            pat_row.set_selected(0)
            detect()

        def sync_rows(*_):
            rng.set_visible(src_row.get_selected() == 0)
        src_row.connect("notify::selected", sync_rows)
        genre_row.connect("notify::selected", fill_patterns)
        d.set_extra_child(grp)
        d.add_response("cancel", tr("cancel"))
        d.add_response("ok", tr("sc_apply"))
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("ok")
        grid = {"g": None}

        def detect():
            b = self.backing
            if not (b and b.raw):
                bpm_row.set_subtitle(tr("sc_no_bk"))
                return
            g = genres[genre_row.get_selected()]
            bpm_row.set_subtitle(tr("sc_detecting"))
            fut = self.ex.submit(SC.detect_grid, b.raw, g)

            def got(f):
                try:
                    bpm, beat0, bar0 = f.result()
                    off = b.offset_ms / 1000                  # siatka w czasie osi (podkład może być przesunięty)
                    grid["g"] = (bpm, beat0 + off, bar0 + off)
                    self._bpm = bpm
                    bpm_row.set_value(bpm)
                    bpm_row.set_subtitle(tr("sc_detected_grid", bar=bar0 + off))
                except Exception:  # noqa: BLE001
                    bpm_row.set_subtitle(tr("sc_manual"))
                return False
            fut.add_done_callback(lambda f: GLib.idle_add(got, f))
        fill_patterns()
        sync_rows()

        def resp(_d, r):
            if r != "ok":
                return
            bpm = bpm_row.get_value()
            self._bpm = bpm
            key = SC.GENRES[genres[genre_row.get_selected()]][pat_row.get_selected()]
            n = bars_opts[bars.get_selected()]
            if src_row.get_selected() > 0:                       # gotowy dźwięk -> nowa ścieżka Sample na siatce
                kind = SC.SOURCES[src_row.get_selected() - 1]
                at = span[0] if span else self.mixer.position()
                g = grid["g"]
                if g and abs(g[0] - bpm) < 0.01:                 # na najbliższy „raz” (wzorce) / uderzenie (style)
                    at = SC.snap_to_grid(at, bpm, g[1], g[2], "bar" if key in SC.PATTERNS else "beat")
                fut = self.ex.submit(SC.render_preset, kind, key, bpm, n, self.fs or 48000)
                fut.add_done_callback(lambda f: GLib.idle_add(self._preset_ready, f, at, kind))
                return
            if not items:
                self.win.toast(tr("sc_need_sel"))
                return
            whole = "all" if rng.get_selected() == 1 else "start"
            self._edit_many([(t, ["scratch", a, bb, bpm, key, n, whole]) for t, a, bb in items])
            self.win.toast(tr("sc_done", n=n, bpm=bpm, style=tr("sc_p_" + key)))
        d.connect("response", resp)
        d.present(self.win)

    def _clips_changed(self, render=()):
        for c in render:
            self._render(c)
        self._tl_refresh()
        self._remix()

    def _preset_ready(self, fut, at, kind):
        try:
            path = fut.result()
        except Exception as e:  # noqa: BLE001
            self.win.toast(str(e))
            return False
        if len(self.tracks) >= MAX_TRACKS:
            self.win.toast(f"Max {count(MAX_TRACKS, 'stem')}")
            return False
        if self.fs is None:
            self.fs = 48000
        t = VTrack(path, "sample")
        t.offset_ms = round(at * 1000)
        self.tracks.append(t)
        self._push_undo(("track_add", t, None))
        self._rebuild()
        self._render(t)
        self._update()
        self.win.toast(tr("sc_preset_added", name=tr("sc_src_" + kind), at=C.fmt_dur(at)))
        return False

    def _edit(self, t, op):
        self._edit_many([(t, op)])

    def _edit_many(self, edits):
        """Edycje kilku ścieżek naraz – jeden wpis do cofania (Ctrl+Z cofa cały blok)."""
        tracks = []
        for t, op in edits:
            t.ops.append(op)
            tracks.append(t)
            L.log(f"{os.path.basename(t.path)}: edycja {op[0]}", tag="edit")
        self._push_undo(("ops", tuple(tracks), None))
        for t in dict.fromkeys(tracks):
            self._post(t)

    def _tl_refresh(self):
        lanes = []
        b = self.backing
        if b:
            lanes.append(Lane(b, tr("bk_short"), "backing"))
        clips = []
        for n, t in enumerate(self.tracks):
            row = Lane(t, f"{LETTERS[n]} · {os.path.splitext(os.path.basename(t.path))[0]}", "vocal")
            lanes.append(row)
            clips += [Lane(c, row.name, "vocal", parent=row) for c in t.clips]
        self.timeline.set_lanes(lanes, clips)
        self.tl_group.set_visible(bool(lanes))

    def _tl_peaks(self, obj, path):
        fut = self.ex.submit(V.peaks, path)

        def done(f):
            try:
                pk, dur = f.result()
            except Exception:  # noqa: BLE001
                return False
            self.timeline.set_peaks(obj, pk, dur)
            return False
        fut.add_done_callback(lambda f: GLib.idle_add(done, f))

    def _flush_pending(self):
        """Zmiany suwaków czekające 600 ms -> do historii teraz (zanim trafi tam coś nowszego)."""
        for key, (o, old, src) in list(self._pend.items()):
            GLib.source_remove(src)
            self._pend.pop(key)
            cur = {"volume": "volume_db", "fx": "fx", "offset": "offset_ms"}[key[0]]
            if getattr(o, cur) != old:
                self._undo.append((key[0], o, old))

    def _push_undo(self, entry):
        self._flush_pending()
        self._undo.append(entry)

    def _note(self, kind, obj, old):
        """Zmiana suwakiem na karcie -> do cofania (Ctrl+Z). Przeciąganie suwaka to jedna zmiana:
        zapis 600 ms po ostatnim ruchu, z wartością sprzed pierwszego ruchu."""
        if self._quiet:
            return
        key = (kind, id(obj))
        o, first, src = self._pend.get(key, (obj, old, 0))
        if src:
            GLib.source_remove(src)

        def commit():
            self._pend.pop(key, None)
            cur = {"volume": "volume_db", "fx": "fx", "offset": "offset_ms"}[kind]
            if getattr(obj, cur) != first:
                self._undo.append((kind, obj, first))
            return False
        self._pend[key] = (obj, first, GLib.timeout_add(600, commit))

    def _tl_volume(self, obj, db):
        """Pokrętło na osi czasu -> ten sam suwak „Głośność” (on zmienia mikser na żywo)."""
        self._quiet += 1
        try:
            if obj is self.backing:
                self.bk_vol.set_value(db)
            elif obj in self.tracks and obj.w:
                obj.w["vol"].set_value(db)
        finally:
            self._quiet -= 1

    def _tl_mute(self, obj, on):
        """Przycisk M na osi czasu -> ten sam przycisk wyciszenia co na karcie ścieżki / podkładu."""
        if obj is self.backing:
            self.bk_mute.set_active(on)
        elif obj in self.tracks and obj.w:
            obj.w["mute"].set_active(on)

    def _tl_solo(self, obj, on):
        """Przycisk S na osi czasu: odsłuch tylko ścieżek z włączonym solo (scalony plik się nie zmienia)."""
        obj.solo = on
        n = sum(1 for o in self.tracks + ([self.backing] if self.backing else []) if o.solo)
        L.log(f"solo: {n} {plural(n, 'stem', 'pl')}" if n else "solo wyłączone", tag="ui")
        self.mixer.update_volumes()
        self.timeline.queue_draw()

    def _tl_offset(self, obj, ms):
        """Klip przeciągnięty na osi czasu -> to samo pole co suwak „Przesunięcie” (ono przelicza dźwięk)."""
        self._quiet += 1
        try:
            if obj is self.backing:
                self.bk_of_spin.set_value(ms)
            elif obj in self.tracks and obj.w:
                obj.w["of_spin"].set_value(ms)
            elif isinstance(obj, Clip):                           # klip: bez pola na karcie – od razu przelicz
                obj.offset_ms = ms
                self._post(obj)
                self.timeline.queue_draw()
        finally:
            self._quiet -= 1

    # ---------------------------------------------------------------- podkład
    def _backing_group(self):
        g = Adw.PreferencesGroup(title=tr("bk_title"), description=tr("bk_sub"))
        self.bk_row = Adw.ActionRow(title=tr("bk_none"), activatable=True)
        self.bk_row.add_prefix(Gtk.Image(icon_name="audio-x-generic-symbolic"))
        self.bk_spin = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.bk_mute = Gtk.ToggleButton(icon_name="audio-volume-muted-symbolic", tooltip_text=tr("mute"),
                                        valign=Gtk.Align.CENTER, visible=False)
        self.bk_mute.add_css_class("flat")
        self.bk_mute.connect("toggled", self._on_bk_mute)
        self.bk_rm = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=tr("remove"), valign=Gtk.Align.CENTER,
                                visible=False)
        self.bk_rm.add_css_class("flat")
        self.bk_rm.connect("clicked", lambda *_: self.set_backing(None))
        self.bk_pick = Gtk.Button(label=tr("bk_choose"), valign=Gtk.Align.CENTER)
        self.bk_pick.connect("clicked", lambda *_: self.choose_backing())
        for w in (self.bk_spin, self.bk_mute, self.bk_rm, self.bk_pick):
            self.bk_row.add_suffix(w)
        self.bk_row.connect("activated", lambda *_: self.choose_backing())
        g.add(self.bk_row)
        self.bk_vol_row = Adw.ActionRow(title=tr("volume"), visible=False)
        self.bk_vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -30, 12, 0.5)
        self.bk_vol.set_hexpand(True)
        self.bk_vol.set_draw_value(False)
        self.bk_vol.set_size_request(220, -1)
        self.bk_vol.add_mark(0, Gtk.PositionType.BOTTOM, None)
        self.bk_vol.connect("value-changed", self._on_bk_volume)
        self.bk_vol_v = Gtk.Label(label=f"{num(0.0, '+.1f')} dB", width_chars=8, xalign=1)
        self.bk_vol_v.add_css_class("numeric")
        self.bk_vol_row.add_suffix(self.bk_vol)
        self.bk_vol_row.add_suffix(self.bk_vol_v)
        g.add(self.bk_vol_row)
        self.bk_of_row = Adw.ActionRow(title=tr("offset"), subtitle=tr("offset_tip"), visible=False)
        self.bk_of = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -30000, 30000, 1)
        self.bk_of.set_round_digits(0)
        self.bk_of.set_hexpand(True)
        self.bk_of.set_draw_value(False)
        self.bk_of.set_size_request(220, -1)
        self.bk_of.add_mark(0, Gtk.PositionType.BOTTOM, None)
        self.bk_of_spin = Gtk.SpinButton.new_with_range(-600000, 600000, 1)
        self.bk_of_spin.set_valign(Gtk.Align.CENTER)
        self.bk_of_spin.set_width_chars(5)
        self.bk_of.connect("value-changed", lambda sc: abs(self.bk_of_spin.get_value()) < 30000
                           and self.bk_of_spin.get_value() != round(sc.get_value()) and self.bk_of_spin.set_value(round(sc.get_value())))
        self.bk_of_spin.connect("value-changed", self._on_bk_offset)
        ms = Gtk.Label(label="ms")
        ms.add_css_class("caption")
        for w in (self.bk_of, self.bk_of_spin, ms):
            self.bk_of_row.add_suffix(w)
        g.add(self.bk_of_row)
        return g

    def choose_backing(self):
        if self.busy:
            return
        C.open_audio_dialog(self.win, lambda paths: paths and self.set_backing(paths[0]))

    def _on_bk_offset(self, sp):
        b = self.backing
        if not b:
            return
        self._note("offset", b, b.offset_ms)
        b.offset_ms = round(sp.get_value())
        if round(self.bk_of.get_value()) != b.offset_ms:
            self.bk_of.set_value(b.offset_ms)
        self.timeline.queue_draw()
        src = self._offset_src.pop("bk", 0)
        if src:
            GLib.source_remove(src)

        def go():
            self._offset_src.pop("bk", None)
            self._bk_shift()
            return False
        self._offset_src["bk"] = GLib.timeout_add(450, go)

    def _bk_shift(self):
        b = self.backing
        if not b or not b.raw:
            return
        b.shift_token += 1
        tok = b.shift_token
        if abs(b.offset_ms) < 0.5:
            b.dry = b.raw
            self._remix()
            return
        fut = self.ex.submit(V.shift_file, b.raw, b.offset_ms)

        def done(f):
            if b is self.backing and tok == b.shift_token:
                try:
                    b.dry = f.result()
                except Exception as e:  # noqa: BLE001
                    b.error = str(e)
                self._bk_fill()
                self._remix()
            return False
        fut.add_done_callback(lambda f: GLib.idle_add(done, f))

    def set_backing(self, path, volume_db=0.0, mute=False, in_mix=False, offset_ms=0.0):
        if self.busy:
            return
        self.backing = Backing(path) if path else None
        if self.backing:
            b = self.backing
            b.volume_db, b.mute, b.in_mix, b.offset_ms = volume_db, mute, in_mix, offset_ms
            L.log(f"podkład: {os.path.basename(path)}", tag="ui")
            if self.fs is None:
                try:
                    self.fs = sf.info(path).samplerate
                except Exception:  # noqa: BLE001
                    self.fs = 48000
            fut = self.ex.submit(V.prepare_backing, path, self.fs)
            fut.add_done_callback(lambda f, b=b: GLib.idle_add(self._backing_ready, b, f))
        elif not self.tracks:
            self.fs = None
        self._bk_fill()
        self._tl_refresh()
        self._remix()

    def _backing_ready(self, b, fut):
        if b is not self.backing:
            return False
        try:
            b.raw, b.duration = fut.result()
        except Exception as e:  # noqa: BLE001
            b.error = str(e)
            self._bk_fill()
            return False
        self._bk_fill()
        self._tl_peaks(b, b.raw)
        self._bk_shift()
        return False

    def _bk_fill(self):
        b = self.backing
        has = b is not None
        for w in (self.bk_mute, self.bk_rm, self.bk_vol_row, self.bk_of_row):
            w.set_visible(has)
        self.bk_pick.set_label(tr("bk_change") if has else tr("bk_choose"))
        busy = has and b.raw is None and not b.error
        self.bk_spin.set_visible(busy)
        self.bk_spin.set_spinning(busy)
        if not has:
            self.bk_row.set_title(tr("bk_none"))
            self.bk_row.set_subtitle("")
            return
        self.bk_row.set_title(C.esc(os.path.basename(b.path)))
        if b.error:
            self.bk_row.set_subtitle(f"⚠ {tr('analysis_failed')}: {C.esc(b.error[:70])}")
        elif busy:
            self.bk_row.set_subtitle(tr("rendering"))
        else:
            self.bk_row.set_subtitle(tr("bk_meta", dur=C.fmt_dur(b.duration)))
        self.bk_vol.set_value(b.volume_db)
        self.bk_of_spin.set_value(b.offset_ms)
        self.bk_mute.set_active(b.mute)
        self.bk_vol_v.set_text(f"{num(b.volume_db, '+.1f')} dB")

    def _on_bk_volume(self, sc):
        if self.backing:
            self._note("volume", self.backing, self.backing.volume_db)
            self.backing.volume_db = round(sc.get_value() * 2) / 2
            self.bk_vol_v.set_text(f"{num(self.backing.volume_db, '+.1f')} dB")
            self.mixer.update_volumes()
            self.timeline.queue_draw()

    def _on_bk_mute(self, btn):
        if self.backing:
            self.backing.mute = btn.get_active()
            self.timeline.queue_draw()
            self.mixer.update_volumes()
            self._update()

    def _on_bk_mix(self, row, _p):
        if self.backing:
            self.backing.in_mix = row.get_active()
            self._update()

    def _player_bar(self):
        lg = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for m in ("top", "bottom", "start", "end"):
            getattr(lg, f"set_margin_{m}")(8 if m in ("top", "bottom") else 18)
        row = Gtk.Box(spacing=10)
        self.play_btn = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text=tr("play"),
                                   valign=Gtk.Align.CENTER)
        self.play_btn.add_css_class("circular")
        self.play_btn.add_css_class("suggested-action")
        self.play_btn.connect("clicked", lambda *_: self.mixer.toggle())
        self.pos = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.1)
        self.pos.set_hexpand(True)
        self.pos.set_draw_value(False)
        self._pos_h = self.pos.connect("change-value", lambda _s, _c, v: (self.mixer.seek(v), False)[1])
        self.time_lbl = Gtk.Label(label="0:00 / 0:00")
        self.time_lbl.add_css_class("numeric")
        home = Gtk.Button(icon_name="media-skip-backward-symbolic", tooltip_text=tr("to_start") + " (Home)",
                          valign=Gtk.Align.CENTER)
        home.add_css_class("circular")
        home.add_css_class("flat")
        home.connect("clicked", lambda *_: self.to_start())
        row.append(home)
        row.append(self.play_btn)
        row.append(self.pos)
        row.append(self.time_lbl)
        lg.append(row)
        mrow = Gtk.Box(spacing=10)
        mrow.set_margin_top(8)
        self.meter = Gtk.LevelBar(min_value=0, max_value=1, hexpand=True, valign=Gtk.Align.CENTER)
        self.meter.add_css_class("peak-meter")
        self.meter.add_offset_value("hot", 0.95)
        self.meter_lbl = Gtk.Label(label="", xalign=0, ellipsize=Pango.EllipsizeMode.END, width_chars=10)
        self.meter_lbl.add_css_class("caption")
        mrow.append(self.meter)
        self.meter_db = Gtk.Label(label="−∞ dB", width_chars=8)
        self.meter_db.add_css_class("numeric")
        mrow.append(self.meter_db)
        lg.append(mrow)
        self.meter_msg = Gtk.Label(xalign=0, wrap=True)
        self.meter_msg.add_css_class("caption")
        self.meter_msg.set_margin_top(4)
        lg.append(self.meter_msg)
        return lg

    def _track_row(self, n, t):
        row = Gtk.Box()
        row.add_css_class("card")
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("top", "bottom"):
            getattr(outer, f"set_margin_{m}")(10)
        outer.set_margin_start(12)
        outer.set_margin_end(8)

        top = Gtk.Box(spacing=10)
        badge = Gtk.Label(label=LETTERS[n])
        badge.add_css_class("track-badge")
        badge.set_valign(Gtk.Align.CENTER)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        title = Gtk.Label(label=os.path.basename(t.path), xalign=0, wrap=True, lines=2,
                          wrap_mode=Pango.WrapMode.WORD_CHAR, ellipsize=Pango.EllipsizeMode.END)
        title.add_css_class("heading")
        sub = Gtk.Label(xalign=0)
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        names.append(title)
        names.append(sub)
        spin = Gtk.Spinner(valign=Gtk.Align.CENTER)
        dd = Gtk.DropDown.new_from_strings([tr("vp_" + k) for k in V.PRESET_ORDER])
        dd.set_selected(V.PRESET_ORDER.index(t.preset))
        dd.set_valign(Gtk.Align.CENTER)
        dd.connect("notify::selected", self._on_track_preset, t)
        mute = Gtk.ToggleButton(icon_name="audio-volume-muted-symbolic", tooltip_text=tr("mute"),
                                active=t.mute, valign=Gtk.Align.CENTER)
        mute.add_css_class("flat")
        mute.connect("toggled", self._on_mute, t)
        rm = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=tr("remove"), valign=Gtk.Align.CENTER)
        rm.add_css_class("flat")
        rm.connect("clicked", lambda *_: self.remove(t))
        for w in (badge, names, spin, dd, mute, rm):
            top.append(w)
        outer.append(top)

        sl = Gtk.Grid(column_spacing=10, row_spacing=2)
        sl.set_margin_start(38)
        vol_l = Gtk.Label(label=tr("volume"), xalign=0)
        vol_l.add_css_class("caption")
        vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -24, 12, 0.5)
        vol.set_value(t.volume_db)
        vol.set_hexpand(True)
        vol.set_draw_value(False)
        vol.add_mark(0, Gtk.PositionType.BOTTOM, None)
        vol_v = Gtk.Label(width_chars=8, xalign=1)
        vol_v.add_css_class("numeric")
        vol_v.add_css_class("caption")
        vol.connect("value-changed", self._on_volume, t)
        fx_l = Gtk.Label(label=tr("fx"), xalign=0)
        fx_l.add_css_class("caption")
        fx = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        fx.set_value(t.fx)
        fx.set_hexpand(True)
        fx.set_draw_value(False)
        fx_v = Gtk.Label(width_chars=8, xalign=1)
        fx_v.add_css_class("numeric")
        fx_v.add_css_class("caption")
        fx.connect("value-changed", self._on_fx, t)
        sl.attach(vol_l, 0, 0, 1, 1)
        sl.attach(vol, 1, 0, 1, 1)
        sl.attach(vol_v, 2, 0, 1, 1)
        sl.attach(fx_l, 0, 1, 1, 1)
        sl.attach(fx, 1, 1, 1, 1)
        sl.attach(fx_v, 2, 1, 1, 1)
        of_l = Gtk.Label(label=tr("offset"), xalign=0, tooltip_text=tr("offset_tip"))
        of_l.add_css_class("caption")
        of = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -30000, 30000, 1)
        of.set_round_digits(0)
        of.set_value(t.offset_ms)
        of.set_hexpand(True)
        of.set_draw_value(False)
        of.add_mark(0, Gtk.PositionType.BOTTOM, None)
        of.set_tooltip_text(tr("offset_tip"))
        of_v = Gtk.Box(spacing=2, halign=Gtk.Align.END)
        of_spin = Gtk.SpinButton.new_with_range(-600000, 600000, 1)
        of_spin.set_value(t.offset_ms)
        of_spin.set_width_chars(5)
        of_spin.add_css_class("caption")
        of_ms = Gtk.Label(label="ms")
        of_ms.add_css_class("caption")
        of_v.append(of_spin)
        of_v.append(of_ms)
        of.connect("value-changed", lambda sc: abs(of_spin.get_value()) < 30000
                   and of_spin.get_value() != round(sc.get_value()) and of_spin.set_value(round(sc.get_value())))
        of_spin.connect("value-changed", self._on_offset, t)
        sl.attach(of_l, 0, 2, 1, 1)
        sl.attach(of, 1, 2, 1, 1)
        sl.attach(of_v, 2, 2, 1, 1)
        outer.append(sl)
        outer.set_hexpand(True)
        row.append(outer)
        t.w = dict(sub=sub, spin=spin, dd=dd, vol=vol, vol_v=vol_v, fx=fx, fx_v=fx_v, mute=mute, of=of,
                   of_spin=of_spin)
        self._label_values(t)
        self._fill_sub(t)
        return row

    # ================================================================ pliki
    def choose_files(self):
        C.open_audio_dialog(self.win, self.add_files)

    def add_files(self, paths):
        if self.busy or not paths:
            return
        known = {t.path for t in self.tracks}
        paths = sorted([p for p in paths if p not in known], key=lambda p: os.path.basename(p).lower())
        room = MAX_TRACKS - len(self.tracks)
        if len(paths) > room:
            self.win.toast(f"Max {count(MAX_TRACKS, 'stem')}")
            paths = paths[:room]
        if not paths:
            return
        if self.fs is None:
            try:
                self.fs = sf.info(paths[0]).samplerate
            except Exception:  # noqa: BLE001
                self.fs = 48000
        new = [VTrack(p, self.album_preset) for p in paths]
        for p in paths:
            L.log(f"dodano ścieżkę {os.path.basename(p)}", tag="ui")
        self.tracks = sorted(self.tracks + new, key=lambda t: os.path.basename(t.path).lower())
        self._rebuild()
        for t in new:
            self._render(t)
        self._update()

    def remove(self, t, undo=True):
        if self.busy:
            return
        if undo:
            self._push_undo(("track_del", t, self.tracks.index(t)))
        self.tracks.remove(t)
        if not self.tracks and not self.backing:
            self.fs = None
        self._rebuild()
        self._remix()
        self._update()

    def clear(self):
        if self.busy:
            return
        self.tracks = []
        self.fs = None
        self.backing = None
        self._bk_fill()
        self.mixer.stop()
        self._rebuild()
        self._update()

    def _rebuild(self):
        self._manual_visible()
        self.list.remove_all()
        for n, t in enumerate(self.tracks):
            self.list.append(self._track_row(n, t))
        self._tl_refresh()

    def _fill_sub(self, t):
        if not t.w:
            return
        rendering = t.dry is None and not t.error
        t.w["spin"].set_spinning(rendering)
        t.w["spin"].set_visible(rendering)
        if t.error:
            t.w["sub"].set_text(f"⚠ {tr('analysis_failed')}: {t.error[:70]}")
        elif rendering:
            t.w["sub"].set_text(tr("rendering"))
        else:
            off = f" · {num(t.offset_ms, '+.0f')} ms" if abs(t.offset_ms) >= 0.5 else ""
            t.w["sub"].set_text(f"{C.fmt_dur(t.duration)} · {tr('vp_' + t.preset)}{off}")

    def _label_values(self, t):
        t.w["vol_v"].set_text(f"{num(t.volume_db, '+.1f')} dB")
        t.w["fx_v"].set_text(f"{int(round(t.fx))}%")

    # ================================================================ render
    def _render(self, t):
        t.token += 1
        tok = t.token
        t.dry = t.wet = t.raw = None
        t.error = ""
        self._fill_sub(t)
        fut = self.ex.submit(V.render_track, t.path, t.preset, self.fs,
                             dict(self.manual) if t.preset == "manual" else None)
        fut.add_done_callback(lambda f: GLib.idle_add(self._rendered, t, tok, f))
        self._update()

    def _rendered(self, t, tok, fut):
        if tok != t.token or t not in self._all():
            return False
        try:
            dry, wet, t.duration = fut.result()
            t.raw = (dry, wet)
        except Exception as e:  # noqa: BLE001
            t.error = str(e)
            self._fill_sub(t)
            self._remix()
            return False
        self._post(t)
        for o in ([] if isinstance(t, Clip) else self.tracks):   # ścieżki z wklejkami z tej ścieżki – źródło się zmieniło
            if o is not t and o.raw and any(op[0] == "paste" and op[1] == t.path for op in o.ops):
                self._post(o)
        return False

    def _post(self, t):
        """Edycje z osi czasu + przesunięcie: szybkie operacje na gotowych plikach, w tle."""
        if t.raw is None:
            return
        t.shift_token += 1
        tok = t.shift_token
        sources = {o.path: o.raw for o in self.tracks if o.raw}
        fut = self.ex.submit(V.prepare_track, *t.raw, [list(op) for op in t.ops], sources, t.offset_ms,
                             os.path.basename(t.path))
        fut.add_done_callback(lambda f: GLib.idle_add(self._posted, t, tok, f))

    def _posted(self, t, tok, fut):
        if tok != t.shift_token or t not in self._all():
            return False
        try:
            r = fut.result()
            t.dry, t.wet = r["dry"], r["wet"]
            self.timeline.set_peaks(t, r["peaks"], r["duration"])
        except Exception as e:  # noqa: BLE001
            t.error = str(e)
        self._fill_sub(t)
        self._remix()
        return False

    def _on_offset(self, sp, t):
        self._note("offset", t, t.offset_ms)
        t.offset_ms = round(sp.get_value())
        if round(t.w["of"].get_value()) != t.offset_ms:
            t.w["of"].set_value(t.offset_ms)
        self.timeline.queue_draw()
        src = self._offset_src.pop(id(t), 0)
        if src:
            GLib.source_remove(src)

        def go():
            self._offset_src.pop(id(t), None)
            L.log(f"{os.path.basename(t.path)}: przesunięcie -> {t.offset_ms:+.0f} ms", tag="ui")
            self._post(t)
            return False
        self._offset_src[id(t)] = GLib.timeout_add(450, go)

    def _remix(self):
        """Przebudowa potoku GStreamer – zebrana w jedną, gdy kilka ścieżek kończy się naraz (14 ścieżek =
        14 przebudów 28 źródeł; teraz jedna po 150 ms)."""
        if getattr(self, "_remix_src", 0):
            GLib.source_remove(self._remix_src)
        self._remix_src = GLib.timeout_add(150, self._remix_now)
        self._update()

    def _remix_now(self):
        self._remix_src = 0
        extra = [self.backing] if self.backing and self.backing.dry else []
        self.mixer.set_tracks(self._all() + extra)
        n = len([t for t in self.tracks if t.dry])
        if n:
            L.log(f"mikser GStreamer: {n} {plural(n, 'stem', 'pl')} × (sucha + efekt) -> audiomixer", tag="mixer")
        self._update()

    # ================================================================ zdarzenia
    def _on_card(self, btn, key):
        if not btn.get_active():
            return
        self.album_preset = key
        self._manual_visible()
        for t in self.tracks:
            if t.preset != key:
                t.w["dd"].set_selected(V.PRESET_ORDER.index(key))   # wywoła _on_track_preset

    def _on_track_preset(self, dd, _p, t):
        key = V.PRESET_ORDER[dd.get_selected()]
        if key == t.preset:
            return
        t.preset = key
        L.log(f"{os.path.basename(t.path)}: opcja -> {key}", tag="ui")
        t.fx = V.get_preset(key, self.manual).fx_default
        t.w["fx"].set_value(t.fx)
        self._manual_visible()
        self._render(t)

    def _on_volume(self, s, t):
        self._note("volume", t, t.volume_db)
        t.volume_db = round(s.get_value() * 2) / 2
        self._label_values(t)
        self.mixer.update_volumes()
        self.timeline.queue_draw()

    def level_all(self):
        """Wszystkie wokale na wspólny poziom (−20 LUFS po masteringu = suwak 0 dB); dalej każdy suwakiem osobno."""
        for t in self.tracks:
            if t.w:
                t.w["vol"].set_value(0.0)
        L.log("wyrównano głośność wokali: wszystkie suwaki 0 dB (−20 LUFS)", tag="ui")
        self.win.toast(tr("level_all_done"))

    def _on_fx(self, s, t):
        self._note("fx", t, t.fx)
        t.fx = s.get_value()
        self._label_values(t)
        self.mixer.update_volumes()

    def _on_mute(self, b, t):
        t.mute = b.get_active()
        self.timeline.queue_draw()
        L.log(f"{os.path.basename(t.path)}: {'wyciszona' if t.mute else 'włączona'}", tag="ui")
        self.mixer.update_volumes()

    # ================================================================ odsłuch
    def _sync_play(self):
        p = self.mixer.playing
        self.play_btn.set_icon_name("media-playback-pause-symbolic" if p else "media-playback-start-symbolic")
        self.play_btn.set_tooltip_text(tr("pause") if p else tr("play"))
        if hasattr(self, "tl_play"):
            self.tl_play.set_icon_name("media-playback-pause-symbolic" if p else "media-playback-start-symbolic")

    def _on_level(self, db):
        now = time.monotonic()
        if db > self.peak_hold[0] or now - self.peak_hold[1] > 1.5:
            self.peak_hold = (db, now)
        self.meter.set_value(max(0.0, min(1.0, (db + 60) / 60)))
        self.meter_db.set_text(f"{num(db, '+.1f')} dB" if db > -90 else "−∞ dB")
        hot = self.peak_hold[0] > -1.0
        self.meter_msg.set_text(tr("meter_hot") if hot else tr("meter_ok"))
        self.meter_msg.remove_css_class("error" if not hot else "success")
        self.meter_msg.add_css_class("error" if hot else "success")

    def _tick(self):
        if self.mixer.pipe:
            self.mixer.check_stall()
            d = self.mixer.duration()
            pend = self.mixer.pending_position()
            p = pend if pend is not None else self.mixer.position()
            self.pos.handler_block(self._pos_h)
            self.pos.set_range(0, max(d, 0.1))
            self.pos.set_value(p)
            self.pos.handler_unblock(self._pos_h)
            self.time_lbl.set_text(f"{C.fmt_dur(p)} / {C.fmt_dur(d)}")
            self.timeline.set_position(p)
        if not self.mixer.playing:
            self.meter.set_value(0)
        return True

    # ================================================================ scalanie
    def _all(self):
        """Ścieżki i ich klipy (do miksera, scalania, cofania)."""
        return [x for t in self.tracks for x in [t] + t.clips]

    def _ready(self):
        return [t for t in self._all() if t.dry and not t.error]

    def _update(self):
        has_any = bool(self.tracks or self.backing)
        self.stack.set_visible_child_name("main" if has_any else "empty")
        self.bottom.set_visible(has_any)
        self.player_bar.set_visible(has_any)
        self.tl_topbar.set_visible(has_any)
        rendering = any(t.dry is None and not t.error for t in self.tracks)
        ok = self._ready()
        self.merge_btn.set_sensitive(bool(ok) and not rendering and not self.busy)
        self.merge_bk_btn.set_visible(bool(self.backing))
        self.merge_bk_btn.set_sensitive(bool(ok) and not rendering and not self.busy and bool(self.backing and
                                        self.backing.dry and not self.backing.mute))
        has = self.mixer.pipe is not None
        self.play_btn.set_sensitive(has)
        self.pos.set_sensitive(has)
        if not self.busy:
            if rendering:
                self.status.set_text(tr("rendering"))
            elif ok:
                self.status.set_text(tr("count_dur", count=count(len(ok), "stem"), dur=C.fmt_dur(max(t.duration for t in ok))))
            else:
                self.status.set_text("")

    def _default_name(self):
        stems = [os.path.splitext(os.path.basename(t.path))[0] for t in self.tracks]
        pre = os.path.commonprefix(stems).strip(" -_.") if len(stems) > 1 else stems[0]
        if len(pre) >= 3:
            return f"{pre} - {tr('merged_suffix')}.wav"
        return f"{tr('merged_suffix')}.wav"

    def start_merge(self, with_bk=False):
        tracks = [t for t in self._ready() if not t.mute]
        if not tracks:
            return
        if with_bk and not (self.backing and self.backing.dry and not self.backing.mute):
            self.win.toast(tr("merge_no_bk"))
            return
        st = i18n.load_settings()
        d = Adw.AlertDialog(heading=tr("merge_with_bk" if with_bk else "merge_vocals"), body=tr("merge_dlg_body"))
        grp = Adw.PreferencesGroup()
        fmt = Adw.ComboRow(title=tr("merge_fmt"), model=Gtk.StringList.new([tr("mf_" + k) for k in V.MERGE_FORMATS]))
        last = st.get("merge_fmt", "wav24")
        fmt.set_selected(V.MERGE_FORMATS.index(last) if last in V.MERGE_FORMATS else 0)
        grp.add(fmt)
        d.set_extra_child(grp)
        d.add_response("cancel", tr("cancel"))
        d.add_response("save", tr("merge_choose_file"))
        d.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("save")

        def resp(_d, r):
            if r != "save":
                return
            key = V.MERGE_FORMATS[fmt.get_selected()]
            st2 = i18n.load_settings()
            st2["merge_fmt"] = key
            i18n.save_settings(st2)
            if self.backing:
                self.backing.in_mix = with_bk
            self._merge_file(tracks, with_bk, key)
        d.connect("response", resp)
        d.present(self.win)

    def _merge_file(self, tracks, with_bk, key):
        ext = ".mp3" if key.startswith("mp3") else ".wav"
        name = os.path.splitext(self._default_name())[0]
        if with_bk:
            name += " + " + tr("bk_short").lower()
        dlg = Gtk.FileDialog(title=tr("merge_save_title"), initial_name=name + ext)
        dlg.set_initial_folder(Gio.File.new_for_path(os.path.dirname(tracks[0].path)))

        def done(d, res):
            try:
                out = d.save_finish(res).get_path()
            except GLib.Error:
                return
            if not out.lower().endswith(ext):
                out += ext
            self._do_merge(tracks, out, with_bk, key)
        dlg.save(self.win, None, done)

    def _do_merge(self, tracks, out, with_bk=False, key="wav24"):
        self.mixer.pause()
        for t in tracks:
            L.log(f"{os.path.basename(t.path)}: {t.preset}, głośność {t.volume_db:+.1f} dB, efekt {int(t.fx)}%",
                  tag="merge")
        self.busy = True
        self.progress.set_visible(True)
        self.progress.pulse()
        self.status.set_text(tr("merging"))
        self._update()
        spec = [dict(dry=t.dry, wet=t.wet, volume_db=t.volume_db, fx=t.fx) for t in tracks]
        if with_bk and self.backing and self.backing.dry:
            b = self.backing
            spec.append(dict(dry=b.dry, wet=None, volume_db=b.volume_db, fx=0))
            L.log(f"podkład w miksie: {os.path.basename(b.path)}, głośność {b.volume_db:+.1f} dB", tag="merge")
        fut = self.ex.submit(V.merge, spec, out, self.fs, key)
        pulse = GLib.timeout_add(120, lambda: (self.progress.pulse(), True)[1])
        fut.add_done_callback(lambda f: GLib.idle_add(self._merged, f, pulse))

    def _merged(self, fut, pulse):
        GLib.source_remove(pulse)
        self.busy = False
        self.progress.set_visible(False)
        self._update()
        try:
            r = fut.result()
        except Exception as e:  # noqa: BLE001
            d = Adw.AlertDialog(heading=tr("error"), body=str(e))
            d.add_response("close", tr("close"))
            d.present(self.win)
            return False
        body = tr("merge_done_body", name=os.path.basename(r["path"]), lufs=r["lufs"], tp=r["true_peak"])
        if r["limited_db"] > 0.05:
            body += tr("merge_limited", db=r["limited_db"])
        self.win.app.notify(tr("notif_merge"), os.path.basename(r["path"]))
        d = Adw.AlertDialog(heading=tr("merge_done_title"), body=body)
        d.add_response("close", tr("close"))
        d.add_response("open", tr("open_folder"))
        d.set_response_appearance("open", Adw.ResponseAppearance.SUGGESTED)
        d.connect("response", lambda _d, resp: resp == "open" and C.open_folder(self.win, os.path.dirname(r["path"])))
        d.present(self.win)
        return False

    # ================================================================ projekt
    def get_state(self):
        b = self.backing
        return dict(preset=self.album_preset, manual=dict(self.manual),
                    tracks=[dict(path=t.path, preset=t.preset, volume_db=t.volume_db, fx=t.fx, mute=t.mute,
                                 offset_ms=t.offset_ms, ops=[list(op) for op in t.ops],
                                 clips=[dict(path=c.path, preset=c.preset, offset_ms=c.offset_ms,
                                             ops=[list(op) for op in c.ops]) for c in t.clips]) for t in self.tracks],
                    backing=dict(path=b.path, volume_db=b.volume_db, mute=b.mute, in_mix=b.in_mix,
                                 offset_ms=b.offset_ms) if b else None)

    def set_state(self, d, missing):
        if self.busy:
            return
        self.clear()
        self.manual.update({k: v for k, v in (d.get("manual") or {}).items() if k in V.MANUAL_DEFAULT})
        for k, (sc, _v) in self.manual_w.items():
            sc.set_value(self.manual[k])
        if self._manual_src:                     # set_value zaplanował render – nie ma jeszcze czego renderować
            GLib.source_remove(self._manual_src)
            self._manual_src = 0
        key = d.get("preset") if d.get("preset") in self.cards else "studio"
        self.cards[key].set_active(True)
        self.album_preset = key
        items = [t for t in d.get("tracks", []) if os.path.exists(t.get("path", ""))]
        missing += [t.get("path", "?") for t in d.get("tracks", []) if not os.path.exists(t.get("path", ""))]
        bk = d.get("backing")
        if bk and os.path.exists(bk.get("path", "")):
            self.set_backing(bk["path"], float(bk.get("volume_db", 0)), bool(bk.get("mute")), bool(bk.get("in_mix")),
                             float(bk.get("offset_ms", 0.0)))
        elif bk:
            missing.append(bk.get("path", "?"))
        if not items:
            self._update()
            return
        if self.fs is None:
            try:
                self.fs = sf.info(sorted(i["path"] for i in items)[0]).samplerate
            except Exception:  # noqa: BLE001
                self.fs = 48000
        new = []
        for it in items:
            t = VTrack(it["path"], it.get("preset") if it.get("preset") in V.PRESET_ORDER else key)
            t.volume_db = float(it.get("volume_db", 0.0))
            t.fx = float(it.get("fx", V.get_preset(t.preset, self.manual).fx_default))
            t.mute = bool(it.get("mute"))
            t.offset_ms = float(it.get("offset_ms", 0.0))
            t.ops = [list(op) for op in it.get("ops", []) if op and op[0] in ("silence", "paste", "scratch", "trim")]
            for cd in it.get("clips", []):
                if os.path.exists(cd.get("path", "")):
                    c = Clip(t, cd["path"], cd.get("preset") if cd.get("preset") in V.PRESET_ORDER else t.preset)
                    c.offset_ms = float(cd.get("offset_ms", 0.0))
                    c.ops = [list(op) for op in cd.get("ops", [])]
                    t.clips.append(c)
            new.append(t)
        self.tracks = sorted(new, key=lambda t: os.path.basename(t.path).lower())[:MAX_TRACKS]
        self._rebuild()
        for t in self._all():
            self._render(t)
        self._update()
