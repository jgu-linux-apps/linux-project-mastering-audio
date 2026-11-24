"""Wspólne elementy UI."""
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID, paths  # noqa: E402
from ..i18n import tr  # noqa: E402

AUDIO_EXT = (".wav", ".wave", ".flac", ".aif", ".aiff", ".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".oga", ".opus")


def fmt_dur(sec):
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def esc(s):
    return GLib.markup_escape_text(s)


def collect_audio(gfiles):
    """Pliki audio z listy (foldery rozwijane), osobno ewentualny PNG."""
    audio, png = [], None
    for f in gfiles:
        p = f.get_path()
        if not p:
            continue
        if os.path.isdir(p):
            audio += [os.path.join(p, n) for n in sorted(os.listdir(p)) if n.lower().endswith(AUDIO_EXT)]
        elif p.lower().endswith(AUDIO_EXT):
            audio.append(p)
        elif p.lower().endswith(".png"):
            png = p
    return audio, png


def open_audio_dialog(parent, cb):
    dlg = Gtk.FileDialog(title=tr("choose_files"))
    flt = Gtk.FileFilter(name=tr("files_filter"))
    for e in AUDIO_EXT:
        flt.add_suffix(e[1:])
    store = Gio.ListStore.new(Gtk.FileFilter)
    store.append(flt)
    dlg.set_filters(store)

    def done(d, res):
        try:
            files = d.open_multiple_finish(res)
        except GLib.Error:
            return
        cb([files.get_item(i).get_path() for i in range(files.get_n_items())])
    dlg.open_multiple(parent, None, done)


def empty_page(title, sub, on_click):
    sp = Adw.StatusPage(title=title, description=sub)
    sm = Adw.StyleManager.get_default()

    def set_logo(*_):
        v = "dark" if sm.get_dark() else "light"
        try:
            sp.set_paintable(Gdk.Texture.new_from_filename(paths.data("icons", f"app-{v}.svg")))
        except GLib.Error:
            sp.set_icon_name(APP_ID)
    sm.connect("notify::dark", set_logo)
    set_logo()
    b = Gtk.Button(label=tr("choose_files"), halign=Gtk.Align.CENTER)
    b.add_css_class("pill")
    b.add_css_class("suggested-action")
    b.connect("clicked", lambda *_: on_click())
    sp.set_child(b)
    return sp


def bottom_bar(button_label, on_click):
    bar = Gtk.Box(spacing=16)
    for m in ("top", "bottom", "start", "end"):
        getattr(bar, f"set_margin_{m}")(10 if m in ("top",) else 12 if m == "bottom" else 18)
    col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
    status = Gtk.Label(xalign=0)
    status.add_css_class("dim-label")
    progress = Gtk.ProgressBar(visible=False)
    progress.add_css_class("master-progress")
    col.append(status)
    col.append(progress)
    btn = Gtk.Button(label=button_label.upper())
    btn.add_css_class("master-btn")
    btn.connect("clicked", lambda *_: on_click())
    bar.append(col)
    bar.append(btn)
    return bar, status, progress, btn


def open_folder(parent, path):
    Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(parent, None, None)


def wheel_scrolls_page(scrolled):
    """Kółko myszy nad suwakiem / polem liczbowym przewija stronę zamiast zmieniać wartość
    (przy przewijaniu strony z dołu na górę łatwo było niechcący przestawić ustawienia ścieżek)."""
    ctl = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL,
                                    propagation_phase=Gtk.PropagationPhase.CAPTURE)

    pos = [None]
    motion = Gtk.EventControllerMotion(propagation_phase=Gtk.PropagationPhase.CAPTURE)
    motion.connect("motion", lambda _c, x, y: pos.__setitem__(0, (x, y)))
    motion.connect("leave", lambda _c: pos.__setitem__(0, None))
    scrolled.add_controller(motion)

    def on_scroll(c, _dx, dy):
        if pos[0] is None:
            return False
        w = scrolled.pick(*pos[0], Gtk.PickFlags.DEFAULT)
        while w is not None and w is not scrolled:
            if isinstance(w, (Gtk.Scale, Gtk.SpinButton)):
                adj = scrolled.get_vadjustment()
                step = 60 if c.get_unit() == Gdk.ScrollUnit.WHEEL else 1
                adj.set_value(max(adj.get_lower(), min(adj.get_upper() - adj.get_page_size(),
                                                       adj.get_value() + dy * step)))
                return True
            w = w.get_parent()
        return False
    ctl.connect("scroll", on_scroll)
    scrolled.add_controller(ctl)
