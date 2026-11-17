"""
adsb-top — top-like terminal view of adsb-mon's decoder.

Requirements: pip install textual  (plus adsb-mon's pyrtlsdr + numpy for hardware)

Rows are coloured while fresh, fade to dim grey between 10 s and 5 min since the
aircraft was last heard, and are dropped after an hour.  Keys: q quit, c clear;
click a column header to sort by it (again to reverse).  Every decoded message
is appended as a JSON line to adsb-top.jsonl beside this script.
"""

import importlib.util
import json
import queue
import time
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, RichLog, Static

# The GUI's filename has a hyphen, so it can't be imported by name.
_spec = importlib.util.spec_from_file_location(
    "adsb_mon", Path(__file__).with_name("adsb-mon.py"))
adsb_mon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adsb_mon)

FADE_START = 10
FADE_END   = 300
DROP_AFTER = 3600
DIM        = (0x4A, 0x4A, 0x4A)
EMERGENCY  = {"7500", "7600", "7700"}
LOG_PATH   = Path(__file__).with_name("adsb-top.jsonl")

CYAN, WHITE, GREEN, ORANGE, RED = "#00d4ff", "#e2e8f0", "#4ade80", "#ff9f43", "#ff4d4d"

COLUMNS = [  # (label, key)
    ("ICAO", "icao"), ("Callsign", "callsign"), ("Flight", "flight"),
    ("Alt ft", "altitude"),
    ("V/R", "vrate"), ("Spd kt", "speed"), ("Hdg", "heading"),
    ("Lat", "lat"), ("Lon", "lon"), ("Squawk", "squawk"), ("Msgs", "msgs"),
    ("Age", "age"), ("Type", "type"), ("Route", "route"),
]
RIGHT = {"altitude", "vrate", "speed", "heading", "lat", "lon", "msgs", "age"}


def fade(color: str, age: float) -> str:
    t = min(max((age - FADE_START) / (FADE_END - FADE_START), 0.0), 1.0)
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#{:02x}{:02x}{:02x}".format(
        *(round(c + (d - c) * t) for c, d in zip((r, g, b), DIM)))


def age_of(ac) -> float:
    return (datetime.now() - ac["last_seen"]).total_seconds() if ac["last_seen"] else 1e9


def row_cells(ac, age):
    def cell(key, value, color, bold=False):
        just = "right" if key in RIGHT else "left"
        style = fade(color, age) + (" bold" if bold else "")
        return Text("—" if value is None or value == "" else str(value),
                    style=style, justify=just)

    vr = ac["vrate"]
    sq = ac["squawk"]
    return [
        cell("icao", ac["icao"], CYAN, bold=True),
        cell("callsign", ac["callsign"], CYAN),
        cell("flight", ac.get("flight"), CYAN),
        cell("altitude", f"{ac['altitude']:,}" if ac["altitude"] is not None else None, WHITE),
        cell("vrate", f"{vr:+,}" if vr is not None else None,
             GREEN if (vr or 0) > 0 else ORANGE if (vr or 0) < 0 else WHITE),
        cell("speed", ac["speed"], WHITE),
        cell("heading", ac["heading"], WHITE),
        cell("lat", f"{ac['lat']:.4f}" if ac["lat"] is not None else None, WHITE),
        cell("lon", f"{ac['lon']:.4f}" if ac["lon"] is not None else None, WHITE),
        # An emergency squawk must stay visible however stale the row is.
        Text(sq, style=f"bold {RED}") if sq in EMERGENCY else cell("squawk", sq, WHITE),
        cell("msgs", ac["msgs"], WHITE),
        cell("age", f"{age:.0f}s", GREEN, bold=age < FADE_START),
        cell("type", ac.get("type"), WHITE),
        cell("route", ac.get("route"), WHITE),
    ]


def sort_value(ac, key):
    if key == "age":
        return age_of(ac)
    v = ac.get(key)
    return (v is None or v == "", v if v is not None else 0)


class AircraftTable(DataTable):
    """A hidden cursor means not following; arrows or a click bring it back."""

    def action_cursor_up(self):
        if self.show_cursor:
            super().action_cursor_up()
        self.show_cursor = True

    def action_cursor_down(self):
        if self.show_cursor:
            super().action_cursor_down()
        self.show_cursor = True

    async def _on_click(self, event):
        if event.style.meta.get("row", -1) >= 0:
            self.show_cursor = True
        await super()._on_click(event)


class AdsbTop(App):
    TITLE = "adsb-top"
    CSS = """
    #status { height: 1; background: $boost; color: $text; padding: 0 1; }
    DataTable { height: 1fr; }
    RichLog { height: 6; border-top: solid $primary-darken-2; }
    """
    BINDINGS = [("q", "quit", "Quit"), ("c", "clear", "Clear"),
                ("escape", "unfollow", "Unfollow")]

    def __init__(self):
        super().__init__()
        self.decoder = adsb_mon.ADSBDecoder()
        self.update_q = queue.Queue()
        self.receiver = None
        self.log_file = None
        self.status = "Starting…"
        self.sort_key, self.sort_reverse = "age", False
        self._msgs = 0
        self._rate_mark = (time.monotonic(), 0)
        self._rate = 0.0

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield AircraftTable(cursor_type="row", zebra_stripes=False)
        yield RichLog(markup=False, wrap=False)
        yield Footer()

    def on_mount(self):
        table = self.query_one(DataTable)
        for label, key in COLUMNS:
            table.add_column(label, key=key)
        self.log_file = LOG_PATH.open("a", buffering=1)
        self.receiver = adsb_mon.ReceiverThread(self.decoder, self.update_q)
        self.receiver.start()
        self.set_interval(0.2, self.drain_queue)
        self.set_interval(1.0, self.redraw)
        self.redraw()

    def log_line(self, msg):
        self.query_one(RichLog).write(f"[{datetime.now():%H:%M:%S}]  {msg}")

    def drain_queue(self):
        try:
            while True:
                kind, data = self.update_q.get_nowait()
                if kind == "status":
                    self.status = data
                    self.log_line(data)
                elif kind == "update":
                    self._msgs += 1
                    ac = self.decoder.aircraft.get(data)
                    if ac:
                        adsb_mon.start_enrichment(ac, self.update_q)
                        self.log_file.write(json.dumps(
                            {k: v for k, v in ac.items()
                             if not k.startswith("_") and k != "photo_url"},
                            default=str) + "\n")
                elif kind == "enrich":
                    ac = self.decoder.aircraft.get(data, {})
                    self.log_line(f"Enriched {data}: {ac.get('type') or '?'} / "
                                  f"{ac.get('operator') or '?'}")
        except queue.Empty:
            pass

    def prune(self):
        for icao, ac in list(self.decoder.aircraft.items()):
            if age_of(ac) > DROP_AFTER:
                del self.decoder.aircraft[icao]
                self.decoder._cpr_cache.pop(icao, None)

    def redraw(self):
        self.prune()
        now = time.monotonic()
        t0, m0 = self._rate_mark
        if now - t0 >= 5:
            self._rate = (self._msgs - m0) / (now - t0)
            self._rate_mark = (now, self._msgs)

        table = self.query_one(DataTable)
        selected = None
        if table.row_count and table.show_cursor:
            selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

        rows = sorted(self.decoder.aircraft.values(),
                      key=lambda ac: sort_value(ac, self.sort_key),
                      reverse=self.sort_reverse)
        table.clear()
        for ac in rows:
            table.add_row(*row_cells(ac, age_of(ac)), key=ac["icao"])
        if selected in self.decoder.aircraft:
            table.move_cursor(row=table.get_row_index(selected), animate=False)

        self.query_one("#status", Static).update(
            f"{datetime.now():%H:%M:%S}  aircraft {len(rows)}  "
            f"msgs {self._msgs}  {self._rate:.1f}/s  sort {self.sort_key}"
            f"{' ↓' if self.sort_reverse else ''}  │  {self.status}")

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected):
        key = event.column_key.value
        if key == self.sort_key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key, self.sort_reverse = key, False
        self.redraw()

    def action_unfollow(self):
        self.query_one(DataTable).show_cursor = False

    def action_clear(self):
        self.decoder.aircraft.clear()
        self.decoder._cpr_cache.clear()
        self._msgs = 0
        self._rate_mark = (time.monotonic(), 0)
        self.log_line("Cleared all data.")
        self.redraw()

    def on_unmount(self):
        if self.receiver:
            self.receiver.stop()
        if self.log_file:
            self.log_file.close()


if __name__ == "__main__":
    AdsbTop().run()
