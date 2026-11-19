"""PyQt6 desktop GUI. The map is Leaflet inside a QWebEngineView; everything else
(data, routing, timing, notes, undo, save/load) is plain Python in this process."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import (QFile, QIODevice, QObject, QRunnable, Qt, QThreadPool, QTime, QTimer, QUrl,
                          pyqtSignal, pyqtSlot)
from PyQt6.QtGui import QAction, QColor, QIcon, QKeySequence, QPixmap
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QColorDialog, QDoubleSpinBox,
                             QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
                             QPushButton, QSpinBox, QSplitter, QTabWidget, QTimeEdit, QToolBar,
                             QVBoxLayout, QWidget)

from . import routing
from .model import COLORS, Plan, Route, Stop, seed_plan
from .timing import Timeline, fmt_clock, fmt_dur, fmt_km, timeline
from .transit import load_ballarat_routes

DATA_DIR = Path.home() / ".ballarat_bus_planner"
AUTOSAVE = DATA_DIR / "plan.json"
HTML_PATH = Path(__file__).with_name("map_view.html")
ID_ROLE = Qt.ItemDataRole.UserRole


# --------------------------------------------------------------------------- bridge
class Bridge(QObject):
    """Receives events from the Leaflet page (JS -> Python)."""
    ready = pyqtSignal()
    map_clicked = pyqtSignal(float, float)
    stop_clicked = pyqtSignal(str, str)
    route_clicked = pyqtSignal(str)
    stop_moved = pyqtSignal(str, str, float, float)
    stop_drag_started = pyqtSignal()
    line_insert = pyqtSignal(str, int, float, float)
    escape = pyqtSignal()

    @pyqtSlot()
    def jsReady(self): self.ready.emit()

    @pyqtSlot(float, float)
    def jsMapClicked(self, lat, lng): self.map_clicked.emit(lat, lng)

    @pyqtSlot(str, str)
    def jsStopClicked(self, rid, sid): self.stop_clicked.emit(rid, sid)

    @pyqtSlot(str)
    def jsRouteClicked(self, rid): self.route_clicked.emit(rid)

    @pyqtSlot(str, str, float, float)
    def jsStopMoved(self, rid, sid, lat, lng): self.stop_moved.emit(rid, sid, lat, lng)

    @pyqtSlot()
    def jsStopDragStart(self): self.stop_drag_started.emit()

    @pyqtSlot(str, int, float, float)
    def jsLineInsert(self, rid, index, lat, lng): self.line_insert.emit(rid, index, lat, lng)

    @pyqtSlot()
    def jsEscape(self): self.escape.emit()


# --------------------------------------------------------------------------- background routing
class RouteSignals(QObject):
    done = pyqtSignal(str, int, str, object, bool)   # route id, token, signature, legs, approx


class RouteTask(QRunnable):
    def __init__(self, route_id: str, token: int, sig: str, points, signals: RouteSignals):
        super().__init__()
        self.route_id, self.token, self.sig, self.points, self.signals = route_id, token, sig, points, signals

    def run(self):
        try:
            legs, approx = routing.fetch_legs(self.points), False
        except Exception:
            legs, approx = routing.approx_legs(self.points), True
        self.signals.done.emit(self.route_id, self.token, self.sig, legs, approx)


class TransitSignals(QObject):
    progress = pyqtSignal(str)
    done = pyqtSignal(object, str, str)  # routes, feed update time, error


class TransitTask(QRunnable):
    def __init__(self, signals: TransitSignals):
        super().__init__()
        self.signals = signals

    def run(self):
        try:
            routes, updated = load_ballarat_routes(self.signals.progress.emit)
        except Exception as e:
            self.signals.done.emit(None, "", str(e))
        else:
            self.signals.done.emit(routes, updated, "")


def swatch(color: str) -> QIcon:
    pm = QPixmap(14, 14)
    pm.fill(QColor(color))
    return QIcon(pm)


def read_qwebchannel_js() -> str:
    f = QFile(":/qtwebchannel/qwebchannel.js")
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError("Could not find qwebchannel.js in Qt resources (is PyQt6-WebEngine installed?)")
    try:
        return bytes(f.readAll()).decode("utf-8")
    finally:
        f.close()


# --------------------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ballarat Bus Route Planner")
        self.resize(1360, 820)

        self.plan = self._load_autosave()
        self.sel_stop: Optional[str] = None
        self.add_mode = False
        self.undo_stack: List[str] = []
        self.map_ready = False
        self.file_path: Optional[Path] = None
        self._loading = False          # suppress widget -> model feedback while filling widgets

        self.pool = QThreadPool.globalInstance()
        self.route_signals = RouteSignals()
        self.route_signals.done.connect(self.on_route_done)
        self.transit_signals = TransitSignals(self)
        self.transit_signals.progress.connect(self.statusBar().showMessage)
        self.transit_signals.done.connect(self.on_transit_loaded)
        self.transit_routes: Optional[List[dict]] = None
        self.transit_enabled = False
        self.transit_loading = False

        self._map_timer = QTimer(self, singleShot=True, interval=60, timeout=self.push_map)
        self._save_timer = QTimer(self, singleShot=True, interval=500, timeout=self.autosave)

        self._build_ui()
        self._build_map()
        self.refresh_all()
        for r in self.plan.routes:
            if len(r.stops) > 1 and not r.valid_legs():
                self.request_route(r)

    # ------------------------------------------------------------------ small helpers
    def cur(self) -> Optional[Route]:
        return self.plan.route(self.plan.selected)

    def _load_autosave(self) -> Plan:
        try:
            return Plan.load(AUTOSAVE)
        except Exception:
            return seed_plan()

    def autosave(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.plan.save(AUTOSAVE)
        except OSError:
            pass

    def changed(self, map_too: bool = True):
        self._save_timer.start()
        if map_too:
            self._map_timer.start()

    def push_undo(self):
        self.undo_stack.append(self.plan.to_json(include_calc=False, indent=None))
        del self.undo_stack[:-60]

    def undo(self):
        if not self.undo_stack:
            return self.statusBar().showMessage("Nothing to undo", 2000)
        self.plan = Plan.from_json(self.undo_stack.pop())
        self.sel_stop = None
        for r in self.plan.routes:
            self.request_route(r)
        self.refresh_all()
        self.changed()

    # ------------------------------------------------------------------ UI construction
    def _build_ui(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        def act(text, slot, shortcut=None, checkable=False):
            a = QAction(text, self)
            a.setCheckable(checkable)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(slot)
            tb.addAction(a)
            return a

        act("＋ Route", self.new_route, "Ctrl+N")
        self.act_add = act("📍 Add stops", self.toggle_add_mode, "Ctrl+E", checkable=True)
        act("↶ Undo", self.undo, "Ctrl+Z")
        tb.addSeparator()
        act("🔍 Find place…", self.find_place, "Ctrl+F")
        self.act_transit = act("PTV routes", self.toggle_transit, checkable=True)
        tb.addSeparator()
        act("Open…", self.open_file, "Ctrl+O")
        act("Save as…", self.save_as, "Ctrl+S")
        act("Timetable CSV…", self.export_csv)

        # ---- left panel
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(8, 8, 8, 8)

        self.route_list = QListWidget()
        self.route_list.setMaximumHeight(130)
        self.route_list.currentItemChanged.connect(self.on_route_list_changed)
        lv.addWidget(QLabel("<b>Routes</b>"))
        lv.addWidget(self.route_list)
        row = QHBoxLayout()
        for text, slot in (("Duplicate", self.duplicate_route), ("Reverse", self.reverse_route),
                           ("Fit", self.fit_route), ("Delete", self.delete_route)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        lv.addLayout(row)

        self.summary = QLabel()
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("background:#f3f6fb;border:1px solid #d5deee;border-radius:6px;padding:6px;")
        lv.addWidget(self.summary)

        tabs = QTabWidget()
        lv.addWidget(tabs, 1)

        # -- Route settings tab
        t1 = QWidget()
        f = QFormLayout(t1)
        self.f_name = QLineEdit()
        self.f_color = QPushButton()
        self.f_visible = QCheckBox("Visible")
        self.f_loop = QCheckBox("Loop route (separate road back to first stop)")
        self.f_return = QCheckBox("Return via same stops (out-and-back)")
        self.f_return.setToolTip("Route back through these stops in reverse order instead of taking a separate road to the first stop.")
        self.f_start = QTimeEdit()
        self.f_start.setDisplayFormat("HH:mm")
        self.f_factor = QDoubleSpinBox(); self.f_factor.setRange(0.5, 4); self.f_factor.setSingleStep(0.05)
        self.f_factor.setToolTip("Bus time = car time × this. 1.35 allows for bus speed, stops and signals.")
        self.f_dwell = QSpinBox(); self.f_dwell.setRange(0, 600); self.f_dwell.setSuffix(" s"); self.f_dwell.setSingleStep(5)
        self.f_headway = QDoubleSpinBox(); self.f_headway.setRange(1, 240); self.f_headway.setSuffix(" min")
        self.f_layover = QDoubleSpinBox(); self.f_layover.setRange(0, 120); self.f_layover.setSuffix(" min")
        f.addRow("Name", self.f_name)
        f.addRow("Colour", self.f_color)
        f.addRow("", self.f_visible)
        f.addRow("", self.f_loop)
        f.addRow("", self.f_return)
        f.addRow("First departure", self.f_start)
        f.addRow("Bus ÷ car time", self.f_factor)
        f.addRow("Default dwell / stop", self.f_dwell)
        f.addRow("Headway", self.f_headway)
        f.addRow("Layover at terminus", self.f_layover)
        tabs.addTab(t1, "Route")

        self.f_name.textEdited.connect(lambda t: self.set_route_attr("name", t))
        self.f_color.clicked.connect(self.pick_color)
        self.f_visible.toggled.connect(lambda v: self.set_route_attr("visible", v))
        self.f_loop.toggled.connect(self.on_loop_toggled)
        self.f_return.toggled.connect(self.on_return_toggled)
        self.f_start.timeChanged.connect(lambda t: self.set_route_attr("start", t.toString("HH:mm")))
        self.f_factor.valueChanged.connect(lambda v: self.set_route_attr("factor", v))
        self.f_dwell.valueChanged.connect(lambda v: self.set_route_attr("dwell", v))
        self.f_headway.valueChanged.connect(lambda v: self.set_route_attr("headway", v))
        self.f_layover.valueChanged.connect(lambda v: self.set_route_attr("layover", v))

        # -- Stops tab
        t2 = QWidget()
        sv = QVBoxLayout(t2)
        sv.addWidget(QLabel("Drag rows to reorder. Select a stop to edit it."))
        self.stop_list = QListWidget()
        self.stop_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.stop_list.currentItemChanged.connect(self.on_stop_list_changed)
        self.stop_list.model().rowsMoved.connect(self.on_rows_moved)
        sv.addWidget(self.stop_list, 3)
        sf = QFormLayout()
        self.s_name = QLineEdit()
        self.s_dwell = QSpinBox(); self.s_dwell.setRange(-1, 600); self.s_dwell.setSpecialValueText("route default"); self.s_dwell.setSuffix(" s")
        self.s_note = QPlainTextEdit(); self.s_note.setPlaceholderText("Notes about this stop (safety, shelter, nearby destinations…)")
        sf.addRow("Name", self.s_name)
        sf.addRow("Dwell", self.s_dwell)
        sv.addLayout(sf)
        sv.addWidget(self.s_note, 2)
        srow = QHBoxLayout()
        b_zoom = QPushButton("Zoom to stop"); b_zoom.clicked.connect(self.zoom_stop)
        b_del = QPushButton("Delete stop"); b_del.clicked.connect(self.delete_stop)
        srow.addWidget(b_zoom); srow.addWidget(b_del)
        sv.addLayout(srow)
        tabs.addTab(t2, "Stops")
        self.s_name.textEdited.connect(self.on_stop_name)
        self.s_dwell.valueChanged.connect(self.on_stop_dwell)
        self.s_note.textChanged.connect(self.on_stop_note)

        # -- Notes tab
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("Why this route? Timing points, road issues, community feedback, ideas…")
        self.notes.textChanged.connect(lambda: self.set_route_attr("notes", self.notes.toPlainText()))
        tabs.addTab(self.notes, "Notes")
        self.tabs = tabs

        self.splitter = QSplitter()
        self.splitter.addWidget(left)
        self.view = QWebEngineView()
        self.splitter.addWidget(self.view)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([430, 930])
        self.setCentralWidget(self.splitter)
        self.status_lbl = QLabel()
        self.statusBar().addPermanentWidget(self.status_lbl)

    def _build_map(self):
        s = self.view.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.bridge = Bridge()
        self.channel = QWebChannel(self)
        self.channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self.channel)
        b = self.bridge
        b.ready.connect(self.on_map_ready)
        b.map_clicked.connect(self.on_map_clicked)
        b.stop_clicked.connect(self.on_map_stop_clicked)
        b.route_clicked.connect(self.select_route)
        b.stop_moved.connect(self.on_stop_moved)
        b.stop_drag_started.connect(self.push_undo)
        b.line_insert.connect(self.on_line_insert)
        b.escape.connect(lambda: self.act_add.isChecked() and self.act_add.trigger())
        html = HTML_PATH.read_text(encoding="utf-8").replace("/*QWEBCHANNEL*/", read_qwebchannel_js())
        # A real https base URL lets OpenStreetMap tile servers see a valid Referer.
        self.view.setHtml(html, QUrl("https://ballarat-bus-planner.local/"))

    # ------------------------------------------------------------------ map sync
    def on_map_ready(self):
        self.map_ready = True
        self.push_map()
        r = self.cur()
        if r and r.stops:
            self.fit_route()

    def push_map(self):
        if not self.map_ready:
            return
        routes = []
        for r in self.plan.routes:
            routes.append({
                "id": r.id, "color": r.color, "visible": r.visible, "loop": r.loop,
                "returnViaStops": r.return_via_stops,
                "approx": bool(r.approx or not r.valid_legs()),
                "stops": [{"id": s.id, "name": s.name, "lat": s.lat, "lng": s.lng} for s in r.stops],
                "legs": r.leg_coords(),
            })
        state = {
            "routes": routes, "selected": self.plan.selected, "selStop": self.sel_stop,
            "addMode": self.add_mode,
            "transit": self.transit_routes if self.transit_enabled and self.transit_routes else [],
        }
        self.view.page().runJavaScript(f"window.setState({json.dumps(state)});")

    # ------------------------------------------------------------------ refresh widgets
    def refresh_all(self):
        self.refresh_route_list()
        self.refresh_form()
        self.refresh_stops()
        self.refresh_summary()
        self._map_timer.start()

    def refresh_route_list(self):
        self.route_list.blockSignals(True)
        self.route_list.clear()
        for r in self.plan.routes:
            tl = timeline(r)
            extra = f"   {fmt_km(tl.dist)} · {fmt_dur(tl.run)}" if tl else f"   {len(r.stops)} stops"
            it = QListWidgetItem(swatch(r.color), r.name + extra)
            it.setData(ID_ROLE, r.id)
            self.route_list.addItem(it)
            if r.id == self.plan.selected:
                self.route_list.setCurrentItem(it)
        self.route_list.blockSignals(False)

    def refresh_form(self):
        r = self.cur()
        self._loading = True
        self.tabs.setEnabled(r is not None)
        if r:
            self.f_name.setText(r.name)
            self.f_color.setStyleSheet(f"background:{r.color};border:1px solid #888;min-height:20px;")
            self.f_visible.setChecked(r.visible)
            self.f_loop.setChecked(r.loop)
            self.f_return.setChecked(r.return_via_stops)
            self.f_start.setTime(QTime.fromString(r.start, "HH:mm"))
            self.f_factor.setValue(r.factor)
            self.f_dwell.setValue(int(r.dwell))
            self.f_headway.setValue(r.headway)
            self.f_layover.setValue(r.layover)
            if self.notes.toPlainText() != r.notes:
                self.notes.setPlainText(r.notes)
        else:
            self.notes.setPlainText("")
        self._loading = False

    def _stop_text(self, r: Route, s: Stop, i: int, tl: Optional[Timeline]) -> str:
        text = f"{i + 1}. {s.name}"
        if tl and i < len(tl.rows):
            row = tl.rows[i]
            text += f"    {fmt_clock(row.arrive)}"
            if row.depart - row.arrive >= 30:
                text += f" → {fmt_clock(row.depart)}"
            if row.leg_dist is not None:
                text += f"    ↓ {fmt_km(row.leg_dist)} · {fmt_dur(row.leg_sec)}"
            elif r.loop:
                text += "    ↩ returns to stop 1"
            elif r.return_via_stops and i == len(r.stops) - 1:
                text += "    ↩ return via same stops"
        return text

    def refresh_stops(self):
        r = self.cur()
        self._loading = True
        self.stop_list.blockSignals(True)
        self.stop_list.clear()
        if r:
            tl = timeline(r)
            for i, s in enumerate(r.stops):
                it = QListWidgetItem(self._stop_text(r, s, i, tl))
                it.setData(ID_ROLE, s.id)
                self.stop_list.addItem(it)
                if s.id == self.sel_stop:
                    self.stop_list.setCurrentItem(it)
        self.stop_list.blockSignals(False)
        self.load_stop_editor()
        self._loading = False

    def load_stop_editor(self):
        r = self.cur()
        s = next((x for x in r.stops if x.id == self.sel_stop), None) if r else None
        was = self._loading
        self._loading = True
        for w in (self.s_name, self.s_dwell, self.s_note):
            w.setEnabled(s is not None)
        # only touch text widgets when the value differs, so background refreshes never disturb typing
        if self.s_name.text() != (s.name if s else ""):
            self.s_name.setText(s.name if s else "")
        self.s_dwell.setValue(-1 if (not s or s.dwell is None) else int(s.dwell))
        if self.s_note.toPlainText() != (s.note if s else ""):
            self.s_note.setPlainText(s.note if s else "")
        self._loading = was

    def refresh_summary(self):
        r = self.cur()
        if not r:
            self.summary.setText("No route selected.")
            self.status_lbl.setText("")
            return
        tl = timeline(r)
        status = ("routing…" if r.status == "loading" else
                  "rough estimate (routing service unreachable)" if r.approx else "road-routed")
        self.status_lbl.setText(status if len(r.stops) > 1 else "")
        if not tl:
            self.summary.setText("Add at least two stops to see estimates." +
                                 ("<br><i>routing…</i>" if r.status == "loading" else ""))
            return
        buses = f"{tl.buses} bus{'es' if tl.buses != 1 else ''}" if tl.buses else "–"
        self.summary.setText(
            f"<b>{fmt_km(tl.dist)}</b> one-way"
            f"{' (loop)' if r.loop else ' (out-and-back)' if r.return_via_stops else ''} &nbsp;·&nbsp; "
            f"<b>{fmt_dur(tl.run)}</b> end-to-end &nbsp;·&nbsp; "
            f"{'back at start' if r.loop else 'arrive'} <b>{fmt_clock(tl.end_arrive)}</b><br>"
            f"avg <b>{tl.avg_kmh:.1f} km/h</b> &nbsp;·&nbsp; stopped {fmt_dur(tl.dwell_total)} &nbsp;·&nbsp; "
            f"<b>{buses}</b> for a {r.headway:g}-min headway"
            f"{'' if r.loop else ' (return via same stops)' if r.return_via_stops else ' (return trip incl.)'}"
            f"<br><span style='color:#666'>{status}</span>")

    # ------------------------------------------------------------------ routes
    def select_route(self, route_id: str):
        self.plan.selected = route_id
        self.sel_stop = None
        self.refresh_all()
        self.changed(False)

    def on_route_list_changed(self, cur, _prev):
        if cur and cur.data(ID_ROLE) != self.plan.selected:
            rid = cur.data(ID_ROLE)
            QTimer.singleShot(0, lambda: self.select_route(rid))   # don't rebuild the list inside its own signal

    def new_route(self):
        self.push_undo()
        r = Route(f"Route {len(self.plan.routes) + 1}", COLORS[len(self.plan.routes) % len(COLORS)])
        self.plan.routes.append(r)
        self.plan.selected = r.id
        self.sel_stop = None
        if not self.act_add.isChecked():
            self.act_add.trigger()
        self.refresh_all()
        self.changed()

    def duplicate_route(self):
        r = self.cur()
        if not r:
            return
        self.push_undo()
        c = Route.from_dict(r.to_dict(include_calc=False), len(self.plan.routes))
        c.id = Route("", "").id
        for s in c.stops:
            s.id = Stop("", 0, 0).id
        c.name = r.name + " (copy)"
        c.color = COLORS[len(self.plan.routes) % len(COLORS)]
        self.plan.routes.append(c)
        self.plan.selected = c.id
        self.request_route(c)
        self.refresh_all()
        self.changed()

    def reverse_route(self):
        r = self.cur()
        if r:
            self.push_undo()
            r.stops.reverse()
            self.after_structure(r)

    def delete_route(self):
        r = self.cur()
        if not r or QMessageBox.question(self, "Delete route", f'Delete "{r.name}" and its notes?') != QMessageBox.StandardButton.Yes:
            return
        self.push_undo()
        self.plan.routes.remove(r)
        self.plan.selected = self.plan.routes[0].id if self.plan.routes else None
        self.sel_stop = None
        self.refresh_all()
        self.changed()

    def fit_route(self):
        r = self.cur()
        if r and r.stops and self.map_ready:
            pts = json.dumps([[s.lat, s.lng] for s in r.stops])
            self.view.page().runJavaScript(f"window.fitStops({pts});")

    def set_route_attr(self, name: str, value):
        r = self.cur()
        if self._loading or not r:
            return
        setattr(r, name, value)
        if name == "name":
            self.refresh_route_list()
        elif name in ("start", "factor", "dwell", "headway", "layover"):
            self.refresh_stops()
            self.refresh_summary()
            self.refresh_route_list()
        elif name == "visible":
            self._map_timer.start()
        self.changed(name == "visible")

    def on_loop_toggled(self, v: bool):
        r = self.cur()
        if self._loading or not r:
            return
        self.push_undo()
        if v:
            self._loading = True
            self.f_return.setChecked(False)
            self._loading = False
            r.return_via_stops = False
        r.loop = v
        self.after_structure(r)

    def on_return_toggled(self, v: bool):
        r = self.cur()
        if self._loading or not r:
            return
        self.push_undo()
        if v:
            self._loading = True
            self.f_loop.setChecked(False)
            self._loading = False
            r.loop = False
        r.return_via_stops = v
        self.after_structure(r)

    def pick_color(self):
        r = self.cur()
        if not r:
            return
        c = QColorDialog.getColor(QColor(r.color), self, "Route colour")
        if c.isValid():
            r.color = c.name()
            self.refresh_route_list()
            self.refresh_form()
            self.changed()

    # ------------------------------------------------------------------ stops
    def toggle_add_mode(self):
        self.add_mode = self.act_add.isChecked()
        self._map_timer.start()
        self.statusBar().showMessage(
            "Click the map to add stops to the selected route; click the line to insert one." if self.add_mode else "", 4000)

    def after_structure(self, r: Route):
        """Call after stops or the route pattern change."""
        self.request_route(r)
        self.refresh_all()
        self.changed()

    def add_stop(self, lat: float, lng: float, name: Optional[str] = None, index: Optional[int] = None):
        r = self.cur()
        if not r:
            return
        self.push_undo()
        s = Stop(name or f"Stop {len(r.stops) + 1}", lat, lng)
        if index is None:
            r.stops.append(s)
        else:
            r.stops.insert(index, s)
        self.sel_stop = s.id
        self.after_structure(r)

    def on_map_clicked(self, lat, lng):
        if self.add_mode:
            self.add_stop(lat, lng)

    def on_line_insert(self, rid, index, lat, lng):
        if rid == self.plan.selected:
            r = self.plan.route(rid)
            if r and r.return_via_stops:
                index = min(index, 2 * len(r.stops) - 1 - index)
            self.add_stop(lat, lng, index=index)

    def on_map_stop_clicked(self, rid, sid):
        if rid != self.plan.selected:
            return self.select_route(rid)
        self.sel_stop = None if self.sel_stop == sid else sid
        self.tabs.setCurrentIndex(1)
        self.refresh_stops()
        self._map_timer.start()

    def on_stop_moved(self, rid, sid, lat, lng):
        r = self.plan.route(rid)
        s = next((x for x in r.stops if x.id == sid), None) if r else None
        if s:
            s.lat, s.lng = lat, lng
            self.after_structure(r)

    def on_stop_list_changed(self, cur, _prev):
        if self._loading:
            return
        self.sel_stop = cur.data(ID_ROLE) if cur else None
        self.load_stop_editor()
        self._map_timer.start()
        r = self.cur()
        s = next((x for x in r.stops if x.id == self.sel_stop), None) if r else None
        if s and self.map_ready:
            self.view.page().runJavaScript(f"window.panTo({s.lat},{s.lng});")

    def on_rows_moved(self, *_):
        r = self.cur()
        if not r:
            return
        ids = [self.stop_list.item(i).data(ID_ROLE) for i in range(self.stop_list.count())]
        by_id = {s.id: s for s in r.stops}
        if ids == [s.id for s in r.stops] or set(ids) != set(by_id):
            return
        self.push_undo()                       # model still holds the old order here
        r.stops = [by_id[i] for i in ids]
        QTimer.singleShot(0, lambda: self.after_structure(r))   # rebuild list after the drop finishes

    def _sel_stop_obj(self) -> Optional[Stop]:
        r = self.cur()
        return next((x for x in r.stops if x.id == self.sel_stop), None) if r else None

    def _update_stop_row(self, s: Stop):
        r = self.cur()
        tl = timeline(r)
        for i in range(self.stop_list.count()):
            it = self.stop_list.item(i)
            if it.data(ID_ROLE) == s.id:
                it.setText(self._stop_text(r, s, i, tl))

    def on_stop_name(self, text):
        s = self._sel_stop_obj()
        if s and not self._loading:
            s.name = text
            self._update_stop_row(s)
            self.changed()

    def on_stop_dwell(self, v):
        s = self._sel_stop_obj()
        if s and not self._loading:
            s.dwell = None if v < 0 else v
            self.refresh_stops(); self.refresh_summary(); self.refresh_route_list()
            self.changed(False)

    def on_stop_note(self):
        s = self._sel_stop_obj()
        if s and not self._loading:
            s.note = self.s_note.toPlainText()
            self.changed(False)

    def delete_stop(self):
        r, s = self.cur(), self._sel_stop_obj()
        if r and s:
            self.push_undo()
            r.stops.remove(s)
            self.sel_stop = None
            self.after_structure(r)

    def zoom_stop(self):
        s = self._sel_stop_obj()
        if s and self.map_ready:
            self.view.page().runJavaScript(f"window.panTo({s.lat},{s.lng},17);")

    # ------------------------------------------------------------------ transit references
    def toggle_transit(self, enabled: bool):
        self.transit_enabled = enabled
        if not enabled:
            self._map_timer.start()
            return
        if self.transit_routes is not None:
            self._map_timer.start()
            return
        if self.transit_loading:
            return
        self.transit_loading = True
        self.act_transit.setEnabled(False)
        self.statusBar().showMessage("Loading current PTV route references…")
        self.pool.start(TransitTask(self.transit_signals))

    def on_transit_loaded(self, routes, updated: str, error: str):
        self.transit_loading = False
        self.act_transit.setEnabled(True)
        if error:
            self.transit_enabled = False
            self.act_transit.setChecked(False)
            self.statusBar().showMessage("Could not load PTV route references.", 8000)
            QMessageBox.warning(self, "PTV routes unavailable", error)
            return
        self.transit_routes = routes
        self._map_timer.start()
        if routes:
            self.statusBar().showMessage(
                f"Loaded {len(routes)} PTV route shapes · feed updated {updated[:10]}", 8000)
        else:
            self.statusBar().showMessage(
                f"No bus or train route shapes found near Ballarat · feed updated {updated[:10]}", 10000)

    # ------------------------------------------------------------------ routing
    def request_route(self, r: Route):
        r.tok += 1
        tok = r.tok
        if len(r.stops) < 2:
            r.legs, r.legs_sig, r.status, r.approx = None, "", "", False
            return
        r.status = "loading"

        def start():
            rr = self.plan.route(r.id)
            if rr is None or rr.tok != tok:
                return                       # superseded by a newer edit
            self.pool.start(RouteTask(rr.id, tok, rr.signature(), routing.route_points(rr), self.route_signals))
        QTimer.singleShot(350, start)

    def on_route_done(self, rid: str, tok: int, sig: str, legs, approx: bool):
        r = self.plan.route(rid)
        if r is None or r.tok != tok:
            return
        r.legs, r.legs_sig, r.approx, r.status = legs, sig, approx, ("approx" if approx else "ok")
        self.refresh_route_list()
        self.refresh_stops()
        self.refresh_summary()
        self.changed()

    # ------------------------------------------------------------------ search
    def find_place(self):
        q, ok = QInputDialog.getText(self, "Find place", "Search in the Ballarat area:")
        if not ok or not q.strip():
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            results = routing.search_places(q.strip())
        except Exception as e:
            QApplication.restoreOverrideCursor()
            return QMessageBox.warning(self, "Search failed", f"Could not reach the search service.\n{e}")
        QApplication.restoreOverrideCursor()
        if not results:
            return QMessageBox.information(self, "Find place", "No results in the Ballarat area.")
        labels = [", ".join(x["full"].split(",")[:3]) for x in results]
        label, ok = QInputDialog.getItem(self, "Find place", "Choose a result:", labels, 0, False)
        if not ok:
            return
        hit = results[labels.index(label)]
        self.view.page().runJavaScript(f"window.panTo({hit['lat']},{hit['lng']},17);")
        if self.cur() and QMessageBox.question(self, "Add stop", f'Add "{hit["name"]}" as a stop on the current route?') == QMessageBox.StandardButton.Yes:
            self.add_stop(hit["lat"], hit["lng"], hit["name"])

    # ------------------------------------------------------------------ files
    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open plan", str(Path.home()), "Planner JSON (*.json)")
        if not path:
            return
        try:
            plan = Plan.load(path)
        except Exception as e:
            return QMessageBox.warning(self, "Open failed", f"That is not a valid planner file.\n{e}")
        self.push_undo()
        self.plan, self.file_path, self.sel_stop = plan, Path(path), None
        for r in self.plan.routes:
            if len(r.stops) > 1 and not r.valid_legs():
                self.request_route(r)
        self.refresh_all()
        self.changed()

    def save_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save plan", str(self.file_path or Path.home() / "ballarat-bus-plan.json"),
                                              "Planner JSON (*.json)")
        if path:
            self.plan.save(path)
            self.file_path = Path(path)
            self.statusBar().showMessage(f"Saved {path}", 4000)

    def export_csv(self):
        r = self.cur()
        tl = timeline(r) if r else None
        if not tl:
            return QMessageBox.information(self, "Timetable", "Add two or more stops and wait for routing first.")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in r.name)
        path, _ = QFileDialog.getSaveFileName(self, "Export timetable", str(Path.home() / f"{safe}-timetable.csv"), "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Stop #", "Name", "Lat", "Lng", "Arrive", "Depart", "Next leg km", "Next leg min", "Notes"])
            for i, s in enumerate(r.stops):
                row = tl.rows[i]
                w.writerow([i + 1, s.name, f"{s.lat:.6f}", f"{s.lng:.6f}", fmt_clock(row.arrive), fmt_clock(row.depart),
                            f"{row.leg_dist / 1000:.2f}" if row.leg_dist is not None else "",
                            f"{row.leg_sec / 60:.1f}" if row.leg_sec is not None else "", s.note])
            if r.loop:
                w.writerow(["", "Back at start", "", "", fmt_clock(tl.end_arrive), "", "", "", ""])
        self.statusBar().showMessage(f"Exported {path}", 4000)

    def closeEvent(self, e):
        self.autosave()
        super().closeEvent(e)


def main():
    # Needed so Chromium's sandbox/GPU quirks don't block the map on some Linux setups:
    #   QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox"   (only if the map stays blank)
    app = QApplication(sys.argv)
    app.setApplicationName("Ballarat Bus Route Planner")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
