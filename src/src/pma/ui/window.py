"""Okno główne: dwie zakładki – WOKAL (mastering i scalanie ścieżek) i ALBUM (normalizacja)."""
import json
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import APP_NAME, VERSION, i18n  # noqa: E402
from .. import log as L  # noqa: E402
from .terminal import MiniTerminal  # noqa: E402
from ..i18n import tr  # noqa: E402
from .album_page import AlbumPage  # noqa: E402
from .vocal_page import VocalPage  # noqa: E402


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, executor):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.project_path = None
        self.set_default_size(1280, 900)
        self.set_size_request(780, 620)

        self.vocal = VocalPage(self, executor)
        self.album = AlbumPage(self, executor)
        self.views = Adw.ViewStack()
        self.views.add_titled_with_icon(self.vocal, "vocal", tr("tab_vocal"), "audio-input-microphone-symbolic")
        self.views.add_titled_with_icon(self.album, "album", tr("tab_album"), "media-optical-symbolic")

        hb = Adw.HeaderBar()
        hb.set_title_widget(Adw.ViewSwitcher(stack=self.views, policy=Adw.ViewSwitcherPolicy.WIDE))
        menu = Gio.Menu()
        proj = Gio.Menu()
        proj.append(tr("project_open"), "app.project-open")
        proj.append(tr("project_save"), "app.project-save")
        menu.append_section(None, proj)
        lang = Gio.Menu()
        lang.append("Polski", "app.language::pl")
        lang.append("English", "app.language::en")
        menu.append_submenu(tr("language"), lang)
        theme = Gio.Menu()
        for k in ("system", "aurora", "neon", "holo", "analog", "pro"):
            theme.append(tr("theme_" + k), f"app.theme::{k}")
        menu.append_submenu(tr("theme"), theme)
        menu.append(tr("about"), "app.about")
        menu.append(tr("quit"), "app.quit")
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True))
        for icon, tip, cb in (("document-save-symbolic", tr("project_save") + " (Ctrl+S)",
                               lambda *_: self.save_project(self.project_path)),
                              ("folder-open-symbolic", tr("project_open") + " (Ctrl+O)", lambda *_: self.open_project()),
                              ("edit-clear-all-symbolic", tr("project_clear"), lambda *_: self.clear_project())):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.connect("clicked", cb)
            hb.pack_start(b)
        self.term_btn = Gtk.ToggleButton(icon_name="utilities-terminal-symbolic", tooltip_text=tr("terminal"))
        hb.pack_end(self.term_btn)

        tv = Adw.ToolbarView()
        tv.add_top_bar(hb)
        overlay = Gtk.Overlay(child=self.views)
        self.term = MiniTerminal(on_close=lambda: self.term_btn.set_active(False))
        self.term.set_halign(Gtk.Align.END)
        self.term.set_valign(Gtk.Align.START)
        for m in ("top", "end"):
            getattr(self.term, f"set_margin_{m}")(12)
        overlay.add_overlay(self.term)
        self.term_btn.connect("toggled", self._on_term)
        self.term.set_visible(False)          # schowany przy starcie – otwiera się dopiero po kliknięciu
        self.toasts = Adw.ToastOverlay(child=overlay)
        tv.set_content(self.toasts)
        self.set_content(tv)
        # wąskie okno: zakładka Album w jednej kolumnie
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 960sp"))
        bp.add_setter(self.album.columns, "orientation", Gtk.Orientation.VERTICAL)
        bp.add_setter(self.album.columns, "homogeneous", False)
        self.add_breakpoint(bp)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        if os.environ.get("PMA_START_TAB") == "album":
            self.views.set_visible_child_name("album")

    def _on_key(self, _c, keyval, _code, state):
        """Spacja = start / stop odtwarzania w bieżącej zakładce (poza polami do wpisywania tekstu)."""
        if keyval != Gdk.KEY_space or state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        focus = self.get_focus()
        w = focus
        while w is not None:
            if isinstance(w, (Gtk.Editable, Gtk.Text, Gtk.TextView)):
                return False
            w = w.get_parent()
        if self.views.get_visible_child_name() == "album":
            if self.album.ab.pipe:
                self.album.ab.toggle()
                return True
            return False
        if self.vocal.mixer.pipe:
            self.vocal.mixer.toggle()
            return True
        return False

    def _on_term(self, btn):
        on = btn.get_active()
        self.term.set_visible(on)

    @property
    def busy(self):
        return self.vocal.busy or self.album.busy

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def add_files(self, paths):
        proj = [p for p in paths if p.endswith(self.PROJECT_EXT)]
        if proj:
            self.open_project(proj[0])
            paths = [p for p in paths if not p.endswith(self.PROJECT_EXT)]
            if not paths:
                return
        page = self.views.get_visible_child()
        page.add_files(paths)

    # ------------------------------------------------------------------ projekt
    PROJECT_EXT = ".pmaproj"

    def save_project(self, path=None):
        if path is None:
            dlg = Gtk.FileDialog(title=tr("project_save"), initial_name=os.path.basename(
                self.project_path or tr("project_default") + self.PROJECT_EXT))
            if self.project_path:
                dlg.set_initial_folder(Gio.File.new_for_path(os.path.dirname(self.project_path)))
            dlg.set_filters(self._project_filters())

            def done(d, res):
                try:
                    p = d.save_finish(res).get_path()
                except GLib.Error:
                    return
                if not p.endswith(self.PROJECT_EXT):
                    p += self.PROJECT_EXT
                self.save_project(p)
            dlg.save(self, None, done)
            return
        data = dict(app="linux-project-mastering-audio", format=1, version=VERSION,
                    tab=self.views.get_visible_child_name(),
                    vocal=self.vocal.get_state(), album=self.album.get_state())
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(path + ".tmp", path)
        except OSError as e:
            self.toast(f"{tr('error')}: {e}")
            return
        self.project_path = path
        self.set_title(f"{os.path.basename(path)} – {APP_NAME}")
        L.log(f"projekt zapisany: {path}", "ok", tag="project")
        self.toast(tr("project_saved", name=os.path.basename(path)))

    def open_project(self, path=None):
        if path is None:
            dlg = Gtk.FileDialog(title=tr("project_open"))
            dlg.set_filters(self._project_filters())

            def done(d, res):
                try:
                    self.open_project(d.open_finish(res).get_path())
                except GLib.Error:
                    return
            dlg.open(self, None, done)
            return
        if self.busy:
            self.toast(tr("project_busy"))
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("app") != "linux-project-mastering-audio":
                raise ValueError(tr("project_bad"))
        except (OSError, ValueError) as e:
            self.toast(f"{tr('error')}: {e}")
            return
        missing = []
        self.vocal.set_state(data.get("vocal") or {}, missing)
        self.album.set_state(data.get("album") or {}, missing)
        if data.get("tab") in ("vocal", "album"):
            self.views.set_visible_child_name(data["tab"])
        self.project_path = path
        self.set_title(f"{os.path.basename(path)} – {APP_NAME}")
        L.log(f"projekt otwarty: {path}", "ok", tag="project")
        if missing:
            d = Adw.AlertDialog(heading=tr("project_missing_title"),
                                body=tr("project_missing_body") + "\n\n" + "\n".join(missing[:12]))
            d.add_response("close", tr("close"))
            d.present(self)
        else:
            self.toast(tr("project_opened", name=os.path.basename(path)))

    def clear_project(self):
        """Nowy, pusty projekt: czyści obie zakładki (z pytaniem, bo zmiany nie są zapisywane same)."""
        if self.busy:
            self.toast(tr("project_busy"))
            return
        d = Adw.AlertDialog(heading=tr("project_clear_title"), body=tr("project_clear_body"))
        d.add_response("cancel", tr("cancel"))
        d.add_response("save", tr("project_save"))
        d.add_response("clear", tr("project_clear"))
        d.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("cancel")

        def resp(_d, r):
            if r == "save":
                self.save_project(self.project_path)
                if not self.project_path:
                    return
            if r in ("save", "clear"):
                self.vocal.clear()
                self.album.clear()
                self.album.set_reference(None)
                self.project_path = None
                self.set_title(APP_NAME)
                L.log("nowy pusty projekt", tag="project")
        d.connect("response", resp)
        d.present(self)

    def _project_filters(self):
        flt = Gtk.FileFilter(name=tr("project_filter"))
        flt.add_suffix(self.PROJECT_EXT[1:])
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        return store

    def shutdown(self):
        self.vocal.mixer.stop()
        self.album.ab.stop()
