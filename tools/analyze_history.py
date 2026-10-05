#!/usr/bin/env python3
"""
Deterministic correlation of a Home Assistant history CSV export with the
Heat Orchestrator AppDaemon log.

Every number in the report is computed from the two input files; nothing is
inferred. Room config (floors, climate entities, overshoot) is loaded from the
real app module, so it cannot drift from production code.

Input
-----
* CSV: History panel → ⋮ → Download data, with the entities from the
  dashboard's "All Data" tab. Columns: entity_id,state,last_changed and, for
  climate rows, current_temperature,hvac_action,...,temperature.
  `last_changed` is UTC ("...Z"); input_datetime *values* are local time.
* Log: the AppDaemon log (local time, "YYYY-MM-DD HH:MM:SS.ffffff LEVEL app: msg").

Usage
-----
    python tools/analyze_history.py HISTORY.csv APPDAEMON.log [--out report.md]
        [--utc-offset HOURS] [--from YYYY-MM-DD] [--grid-second 30]

All times in the report are local (UTC + offset). The offset is detected from
input_datetime values vs their last_changed stamps unless --utc-offset is given.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import importlib.util
import re
import sys
import types
from collections import Counter, defaultdict
from pathlib import Path

APP_PATH = Path(__file__).resolve().parents[1] / "apps" / "heat_orchestrator" / "heat_orchestrator.py"

# Correlation tolerances
MATCH_WINDOW_S = 120      # a logged command must show up in the CSV within this
GRID_SECOND = 30          # replay grid sample second (ticks write at ~:55)


# ---------------------------------------------------------------------------
# App config (loaded from the real module with a stubbed hassapi)
# ---------------------------------------------------------------------------
def load_app():
    stub = types.ModuleType("hassapi")

    class _Hass:  # noqa: D401 - minimal base class
        pass

    stub.Hass = _Hass
    sys.modules.setdefault("hassapi", stub)
    spec = importlib.util.spec_from_file_location("heat_orchestrator_cfg", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


APP = load_app()
ROOMS = {k: (r.floor, r.climate) for k, r in APP.ROOMS.items()}
OVERSHOOT = APP.HeatOrchestrator.THERMOSTAT_OVERSHOOT
PUMP_SWITCH = APP.PUMP_SWITCH
PUMP_POWER = APP.PUMP_POWER_SENSOR
HEALTH_MIN_W = APP.PUMP_HEALTH_MIN_WATTS
HEALTH_GRACE = APP.PUMP_HEALTH_GRACE_MIN

# Defaults mirror the app's _param() fallbacks.
PARAM_DEFAULTS = {
    "room_off_setpoint": 7.0,
    "heating_hyst_on": 0.3,
    "heating_hyst_off": 0.2,
    "min_state_duration_min": 25.0,
    "min_pump_on_min": 40.0,
    "min_pump_off_min": 25.0,
    "dhw_min_run_hours": 3.5,
    "dhw_exclusive_max_run_min": 45.0,
    "dhw_exclusive_pause_min": 90.0,
    "max_continuous_heating_min": 120.0,
    "max_continuous_heating_solo_min": 240.0,
}
HEAT_STATES = {"HEAT_GF": "GF", "HEAT_FF": "FF"}
# [DECISION] reasons logged every N ticks while the state is steady.
PERIODIC_REASONS = {"no_demand_no_quota", "pump_cooldown", "dhw_exclusive_pause"}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def parse_utc(s: str) -> dt.datetime:
    return dt.datetime.strptime(s.rstrip("Z")[:23], "%Y-%m-%dT%H:%M:%S.%f")


def fmt(t: dt.datetime | None) -> str:
    return "—" if t is None else t.strftime("%m-%d %H:%M:%S")


def mins(a: dt.datetime, b: dt.datetime) -> float:
    return (b - a).total_seconds() / 60.0


def fnum(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
class Series:
    """Step function of an entity's state (and attributes) over local time."""

    def __init__(self):
        self.t: list[dt.datetime] = []
        self.s: list[str] = []
        self.a: list[dict] = []

    def add(self, t, s, a):
        self.t.append(t)
        self.s.append(s)
        self.a.append(a)

    def sort(self):
        order = sorted(range(len(self.t)), key=lambda i: self.t[i])
        self.t = [self.t[i] for i in order]
        self.s = [self.s[i] for i in order]
        self.a = [self.a[i] for i in order]

    def idx(self, t):
        return bisect.bisect_right(self.t, t) - 1

    def at(self, t, attr: str | None = None):
        i = self.idx(t)
        if i < 0:
            return None
        return self.s[i] if attr is None else self.a[i].get(attr)

    def transitions(self):
        """(time, old, new) for every state change (attribute-only rows skipped)."""
        out = []
        for i in range(1, len(self.t)):
            if self.s[i] != self.s[i - 1]:
                out.append((self.t[i], self.s[i - 1], self.s[i]))
        return out

    def attr_transitions(self, attr):
        out = []
        prev = None
        for i in range(len(self.t)):
            v = self.a[i].get(attr)
            if i > 0 and v != prev:
                out.append((self.t[i], prev, v))
            prev = v
        return out


def load_csv(path: str):
    raw = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            attrs = {
                k: row[k]
                for k in ("current_temperature", "hvac_action", "temperature")
                if row.get(k) not in (None, "")
            }
            raw.append((row["entity_id"], row["state"], parse_utc(row["last_changed"]), attrs))
    return raw


def detect_offset(raw) -> float:
    """Hours between input_datetime local values and their UTC last_changed."""
    votes = Counter()
    for ent, state, t_utc, _ in raw:
        if not ent.startswith("input_datetime.") or len(state) != 19:
            continue
        try:
            local = dt.datetime.strptime(state, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        delta_h = (local - t_utc).total_seconds() / 3600.0
        q = round(delta_h * 4) / 4
        if abs(delta_h - q) < 0.02:  # written in the same second → real stamp
            votes[q] += 1
    if not votes:
        raise SystemExit("cannot detect UTC offset; pass --utc-offset")
    return votes.most_common(1)[0][0]


def build_series(raw, offset_h):
    off = dt.timedelta(hours=offset_h)
    series: dict[str, Series] = defaultdict(Series)
    firsts = {}
    for ent, state, t_utc, attrs in raw:
        t = t_utc + off
        series[ent].add(t, state, attrs)
        firsts[ent] = min(firsts.get(ent, t), t)
    for s in series.values():
        s.sort()
    start = Counter(firsts.values()).most_common(1)[0][0]
    end = max(s.t[-1] for s in series.values())
    return series, start, end


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------
LOG_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?) (\w+) ([\w.]+): (.*)$")
RX = {
    "PUMP_ON": re.compile(r"^\[PUMP\] ON"),
    "PUMP_OFF": re.compile(r"^\[PUMP\] OFF"),
    "ROOM_ENABLE": re.compile(r"^\[ROOM\] enable (\w+) \D*?([\d.]+)\D*?C \(user_sp=([\d.]+)"),
    "ROOM_DISABLE": re.compile(r"^\[ROOM\] disable (\w+) \D*?([\d.]+)"),
    "COOLDOWN": re.compile(
        r"^\[ROOM\] (\w+) forced cooldown after (\d+)min continuous heating "
        r"\(cap=(\d+)min, contended=(\w+)\)"
    ),
    "RESET": re.compile(r"^\[RESET\]"),
    "FLOOR_SWITCH": re.compile(r"^\[DECISION\] switching floor (\w\w)\W+(\w\w) \((.*)\)|^\[DECISION\] switching floor (\w\w)\W+(\w\w) reason=(.*)"),
    "DECISION": re.compile(r"^\[DECISION\] state=(\w+)(.*)$"),
    "USER": re.compile(r"^\[USER\]"),
    "HEALTH": re.compile(r"^\[HEALTH\]"),
}


class LogEvent:
    __slots__ = ("t", "level", "kind", "msg", "m")

    def __init__(self, t, level, kind, msg, m):
        self.t, self.level, self.kind, self.msg, self.m = t, level, kind, msg, m


def load_log(path: str) -> list[LogEvent]:
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LOG_RE.match(line.rstrip("\n"))
            if not m:
                continue
            ts, level, app, msg = m.groups()
            t = dt.datetime.strptime(ts[:26], "%Y-%m-%d %H:%M:%S.%f" if "." in ts else "%Y-%m-%d %H:%M:%S")
            kind, mm = "OTHER", None
            if app == "heat_orchestrator":
                for k, rx in RX.items():
                    mm = rx.match(msg)
                    if mm:
                        kind = k
                        break
            else:
                kind = "SYSTEM"
            out.append(LogEvent(t, level, kind, msg, mm))
    return out


def decision_fields(ev: LogEvent) -> dict:
    """state=/reason=/floor=/rooms= parsed out of a [DECISION] line."""
    d = {"state": ev.m.group(1)}
    rest = ev.m.group(2)
    m = re.search(r"reason=(\w+)", rest)
    if m:
        d["reason"] = m.group(1)
    m = re.search(r"rooms=\[(.*?)\]", rest)
    if m:
        d["rooms"] = sorted(x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip())
    m = re.search(r"floor=(\w\w)\b", rest)
    if m:
        d["floor"] = m.group(1)
    d["pump_off"] = "pump_off" in rest
    return d


# ---------------------------------------------------------------------------
# Report plumbing
# ---------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.lines: list[str] = []
        self.findings: list[tuple[str, str, str]] = []  # (severity, code, text)

    def h(self, text, level=2):
        self.lines += ["", "#" * level + " " + text, ""]

    def p(self, text=""):
        self.lines.append(text)

    def table(self, header, rows, limit=None):
        if not rows:
            self.p("_none_")
            return
        self.p("| " + " | ".join(header) + " |")
        self.p("|" + "---|" * len(header))
        shown = rows if limit is None else rows[:limit]
        for r in shown:
            self.p("| " + " | ".join(str(c) for c in r) + " |")
        if limit is not None and len(rows) > limit:
            self.p(f"| … {len(rows) - limit} more | " + " | " * (len(header) - 1))

    def finding(self, sev, code, text):
        self.findings.append((sev, code, text))

    def render(self, title):
        out = [f"# {title}", ""]
        order = {"BUG": 0, "SUSPECT": 1, "INFO": 2}
        out.append("## Findings summary")
        out.append("")
        if not self.findings:
            out.append("_no findings_")
        for sev, code, text in sorted(self.findings, key=lambda f: (order.get(f[0], 9), f[1])):
            out.append(f"- **{sev}** `{code}` — {text}")
        return "\n".join(out + self.lines) + "\n"


# ---------------------------------------------------------------------------
# Analysis context
# ---------------------------------------------------------------------------
class Ctx:
    def __init__(self, series, start, end, log, offset):
        self.S = series
        self.start, self.end = start, end
        self.log = log
        self.offset = offset
        self.log_start = log[0].t if log else None
        self.log_end = log[-1].t if log else None

    def ser(self, ent) -> Series:
        return self.S.get(ent) or Series()

    def param(self, key, t):
        v = fnum(self.ser(f"input_number.{key}").at(t))
        return PARAM_DEFAULTS.get(key) if v is None else v

    def time_param(self, key, t, default):
        v = self.ser(f"input_datetime.{key}").at(t)
        try:
            return dt.datetime.strptime(v, "%H:%M:%S").time()
        except (TypeError, ValueError):
            return default

    def in_off_window(self, t):
        start = self.time_param("off_window_start", t, dt.time(1, 0))
        end = self.time_param("off_window_end", t, dt.time(6, 0))
        c = t.time()
        return (start <= c < end) if start <= end else (c >= start or c < end)

    def in_log(self, t):
        return self.log_start is not None and self.log_start <= t <= self.log_end

    def day_of(self, t):
        reset = self.time_param("day_reset_time", t, dt.time(0, 0))
        d = t.date()
        if t.time() < reset:
            d -= dt.timedelta(days=1)
        return d

    def room_snapshot(self, room, t):
        floor, climate = ROOMS[room]
        c = self.ser(climate)
        return {
            "flag": self.ser(f"input_boolean.heating_{room}").at(t),
            "t_cur": fnum(c.at(t, "current_temperature")),
            "sp": fnum(c.at(t, "temperature")),
            "hvac": c.at(t, "hvac_action"),
            "user_sp": fnum(self.ser(f"input_number.user_sp_{room}").at(t)),
            "minutes": fnum(self.ser(f"input_number.heating_minutes_{room}").at(t)),
        }

    def logs_near(self, t, seconds=90, kinds=None):
        lo, hi = t - dt.timedelta(seconds=seconds), t + dt.timedelta(seconds=seconds)
        return [e for e in self.log if lo <= e.t <= hi and (kinds is None or e.kind in kinds)]


def first_change_in(series: Series, lo, hi, pred):
    """First sample in [lo, hi] whose state/attrs satisfy pred(state, attrs)."""
    i = max(0, bisect.bisect_left(series.t, lo))
    while i < len(series.t) and series.t[i] <= hi:
        if pred(series.s[i], series.a[i]):
            return series.t[i]
        i += 1
    return None


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def sec_coverage(ctx: Ctx, R: Report):
    R.h("1. Coverage")
    R.table(
        ["source", "from", "to"],
        [
            ["CSV (local)", fmt(ctx.start), fmt(ctx.end)],
            ["log", fmt(ctx.log_start), fmt(ctx.log_end)],
        ],
    )
    R.p()
    R.p(f"UTC offset used: +{ctx.offset:g} h. Correlation window: {MATCH_WINDOW_S}s.")
    kinds = Counter(e.kind for e in ctx.log)
    R.p()
    R.table(["log event kind", "count"], sorted(kinds.items(), key=lambda kv: -kv[1]))
    t0 = ctx.start
    R.p()
    R.p("Parameters at CSV start:")
    R.table(["param", "value"], [[k, ctx.param(k, t0)] for k in PARAM_DEFAULTS])
    warn = Counter(e.msg[:110] for e in ctx.log if e.level in ("WARNING", "ERROR"))
    if warn:
        R.p()
        R.p("Warnings / errors in log:")
        R.table(["count", "message (truncated)"], [[c, m.replace("|", "/")] for m, c in warn.most_common()])


def sec_log_to_csv(ctx: Ctx, R: Report):
    R.h("2. Log commands → CSV effects")
    win = dt.timedelta(seconds=MATCH_WINDOW_S)
    early = dt.timedelta(seconds=2)
    rows, miss = [], Counter()
    sw = ctx.ser(PUMP_SWITCH)
    for e in ctx.log:
        if not (ctx.start <= e.t <= ctx.end):
            continue
        ok, detail = None, ""
        if e.kind in ("PUMP_ON", "PUMP_OFF"):
            want = "on" if e.kind == "PUMP_ON" else "off"
            hit = first_change_in(sw, e.t - early, e.t + win, lambda s, a: s == want)
            ok = hit is not None
            detail = f"switch→{want} after {(hit - e.t).total_seconds():.0f}s" if ok else f"switch never read {want}"
        elif e.kind in ("ROOM_ENABLE", "ROOM_DISABLE"):
            room, target = e.m.group(1), float(e.m.group(2))
            if room not in ROOMS:
                continue
            c = ctx.ser(ROOMS[room][1])
            hit = first_change_in(
                c, e.t - early, e.t + win, lambda s, a: fnum(a.get("temperature")) is not None and abs(fnum(a["temperature"]) - target) < 0.05
            )
            flag = ctx.ser(f"input_boolean.heating_{room}").at(e.t + win)
            want_flag = "on" if e.kind == "ROOM_ENABLE" else "off"
            ok = hit is not None and flag == want_flag
            detail = (
                f"TRV sp={target} {'after %.0fs' % (hit - e.t).total_seconds() if hit else 'NOT seen'}; "
                f"flag={flag}"
            )
        elif e.kind == "COOLDOWN":
            room = e.m.group(1)
            v = fnum(ctx.ser(f"input_number.heating_minutes_{room}").at(e.t + dt.timedelta(seconds=30)))
            ok = v == 0.0
            detail = f"heating_minutes_{room}={v} 30s later"
        elif e.kind == "RESET":
            v = fnum(ctx.ser("input_number.pump_on_minutes_today").at(e.t + dt.timedelta(seconds=30)))
            ok = v is not None and v <= 1
            detail = f"pump_on_minutes_today={v}"
        elif e.kind == "DECISION":
            d = decision_fields(e)
            hs = ctx.ser("input_text.heat_state").at(e.t + dt.timedelta(seconds=30))
            ok = hs == d["state"]
            detail = f"heat_state={hs}"
            if ok and d.get("rooms") is not None and d["state"] in HEAT_STATES:
                t2 = e.t + dt.timedelta(seconds=30)
                on = sorted(r for r in ROOMS if ctx.ser(f"input_boolean.heating_{r}").at(t2) == "on")
                if on != d["rooms"]:
                    ok = False
                    detail += f"; logged rooms={d['rooms']} but flags on={on}"
                else:
                    continue  # periodic line that matches: not worth a row
            elif ok and ("reason" not in d or (d["reason"] in PERIODIC_REASONS and not d["pump_off"])):
                continue  # periodic status line that agrees with the CSV
        else:
            continue
        if not ok:
            miss[e.kind] += 1
        rows.append([fmt(e.t), e.kind, e.msg[:70].replace("|", "/"), "✔" if ok else "✘", detail])
    R.table(["time", "kind", "log", "ok", "CSV evidence"], rows)
    for k, n in miss.items():
        R.finding("SUSPECT", "log-csv-mismatch", f"{n}× `{k}` log line not reflected in CSV within {MATCH_WINDOW_S}s (see §2)")


def sec_csv_to_log(ctx: Ctx, R: Report):
    R.h("3. CSV changes → log explanation")
    R.p("Relay and FSM transitions inside the log window, with the log line that explains them.")
    rows = []
    sw = ctx.ser(PUMP_SWITCH)
    for t, old, new in sw.transitions():
        if not ctx.in_log(t):
            continue
        kind = "PUMP_ON" if new == "on" else "PUMP_OFF"
        near = [e for e in ctx.logs_near(t, MATCH_WINDOW_S, {kind}) if e.t <= t + dt.timedelta(seconds=2)]
        rows.append([fmt(t), f"switch {old}→{new}", "✔ " + fmt(near[-1].t) if near else "✘ none"])
        if not near and new in ("on", "off"):
            R.finding("SUSPECT", "unexplained-relay", f"pump relay {old}→{new} at {fmt(t)} with no [PUMP] log line (manual/external/HA restart?)")
    hs = ctx.ser("input_text.heat_state")
    for t, old, new in hs.transitions():
        if not ctx.in_log(t):
            continue
        near = ctx.logs_near(t, 60, {"DECISION", "FLOOR_SWITCH"})
        # Prefer the line that names the new state (or the floor switch) over
        # a periodic status line that happens to be in the same window.
        near.sort(key=lambda e: (
            0 if e.kind == "FLOOR_SWITCH" or (e.kind == "DECISION" and e.m.group(1) == new) else 1,
            abs((e.t - t).total_seconds()),
        ))
        expl = near[0].msg[:80].replace("|", "/") if near else "(silent transition)"
        rows.append([fmt(t), f"heat_state {old}→{new}", expl])
    rows.sort()
    R.table(["time", "change", "log"], rows)


def intervals_on(series: Series, lo, hi, value="on"):
    """[(start, end)] where state == value, clipped to [lo, hi]."""
    out, cur = [], None
    for i, t in enumerate(series.t):
        s = series.s[i]
        if s == value and cur is None:
            cur = max(t, lo)
        elif s != value and cur is not None:
            if t > lo:
                out.append((cur, min(t, hi)))
            cur = None
    if cur is not None:
        out.append((cur, hi))
    return [(a, b) for a, b in out if b > a]


def sec_pump(ctx: Ctx, R: Report):
    R.h("4. Pump runs")
    sw = ctx.ser(PUMP_SWITCH)
    runs = intervals_on(sw, ctx.start, ctx.end)
    rows = []
    prev_end = None
    hs = ctx.ser("input_text.heat_state")
    for a, b in runs:
        dur = mins(a, b)
        gap = mins(prev_end, a) if prev_end else None
        states = []
        for t, _, new in hs.transitions():
            if a - dt.timedelta(seconds=60) <= t <= b:
                states.append(new)
        first = hs.at(a + dt.timedelta(seconds=30))
        seq = [first] + [s for s in states if s != first]
        flags = []
        if b < ctx.end and dur < ctx.param("min_pump_on_min", a) - 1:
            flags.append("short run")
        if gap is not None and gap < ctx.param("min_pump_off_min", a) - 1:
            flags.append("short off gap")
        for f in flags:
            R.finding("BUG", "pump-min-time", f"{f}: run {fmt(a)}–{fmt(b)} ({dur:.0f} min, gap before {gap if gap is None else round(gap)} min)")
        rows.append([fmt(a), fmt(b) if b < ctx.end else "(ongoing)", f"{dur:.0f}", "—" if gap is None else f"{gap:.0f}", "→".join(dict.fromkeys(seq)), ", ".join(flags)])
        prev_end = b
    R.table(["on", "off", "min", "off gap before", "FSM states during run", "flags"], rows)

    # Daily accounting: relay minutes vs counters
    R.p()
    R.p("Daily accounting (day boundary = day_reset_time):")
    per_day = defaultdict(float)
    starts = Counter()
    for a, b in runs:
        t = a
        while t < b:
            d = ctx.day_of(t)
            nxt = dt.datetime.combine(d + dt.timedelta(days=1), ctx.time_param("day_reset_time", t, dt.time(0, 0)))
            seg_end = min(b, nxt)
            per_day[d] += mins(t, seg_end)
            t = seg_end
        if a > ctx.start:
            starts[ctx.day_of(a)] += 1
    on_ctr = ctx.ser("input_number.pump_on_minutes_today")
    st_ctr = ctx.ser("input_number.pump_starts_today")
    rows = []
    for d in sorted(per_day):
        lo = dt.datetime.combine(d, dt.time(0, 0))
        hi = min(lo + dt.timedelta(days=1), ctx.end) - dt.timedelta(seconds=1)
        c_on = fnum(on_ctr.at(hi))
        c_st = fnum(st_ctr.at(hi))
        quota = ctx.param("dhw_min_run_hours", hi) * 60
        partial = lo < ctx.start or hi < lo + dt.timedelta(days=1) - dt.timedelta(seconds=2)
        rows.append([d, f"{per_day[d]:.0f}", c_on, starts[d], c_st, f"{quota:.0f}", "partial day" if partial else ""])
        if not partial and c_on is not None and abs(c_on - per_day[d]) > 5:
            R.finding("SUSPECT", "pump-counter-drift", f"{d}: pump_on_minutes_today={c_on} but relay was on {per_day[d]:.0f} min")
    R.table(["day", "relay-on min", "pump_on_minutes_today (end)", "starts (relay)", "pump_starts_today (end)", "quota min", "note"], rows)


def sec_fsm(ctx: Ctx, R: Report):
    R.h("5. FSM timeline")
    hs = ctx.ser("input_text.heat_state")
    rows = []
    trans = [(ctx.start, None, hs.at(ctx.start))] + [x for x in hs.transitions() if x[0] > ctx.start]
    for i, (t, _, new) in enumerate(trans):
        t_next = trans[i + 1][0] if i + 1 < len(trans) else ctx.end
        rows.append([fmt(t), new, f"{mins(t, t_next):.0f}"])
    R.table(["from", "state", "min"], rows)

    # Floor switches vs min_state_duration
    for i in range(1, len(trans)):
        t, _, new = trans[i]
        prev_t, prev = trans[i - 1][0], trans[i - 1][2]
        if prev in HEAT_STATES and new in HEAT_STATES and prev != new:
            held = mins(prev_t, t)
            need = ctx.param("min_state_duration_min", t)
            if held < need - 1:
                R.finding("BUG", "floor-switch-early", f"{prev}→{new} at {fmt(t)} after {held:.0f} min (< min_state_duration {need:.0f})")

    # DHW-only runs vs duty cycle
    sw = ctx.ser(PUMP_SWITCH)
    for i, (t, _, new) in enumerate(trans):
        if new != "DHW_QUOTA":
            continue
        t_next = trans[i + 1][0] if i + 1 < len(trans) else ctx.end
        dur = mins(t, t_next)
        max_run = ctx.param("dhw_exclusive_max_run_min", t)
        min_on = ctx.param("min_pump_on_min", t)
        nxt_state = trans[i + 1][2] if i + 1 < len(trans) else None
        if max_run > 0 and nxt_state == "OFF" and dur > max(max_run, min_on) + 2:
            R.finding("SUSPECT", "dhw-run-long", f"DHW_QUOTA {fmt(t)} lasted {dur:.0f} min (max_run {max_run:.0f})")
        # pause before a DHW-only start (entered with pump off)
        if sw.at(t - dt.timedelta(seconds=5)) == "off":
            offs = [x for x in sw.transitions() if x[2] == "off" and x[0] < t]
            if offs:
                gap = mins(offs[-1][0], t)
                need = max(ctx.param("min_pump_off_min", t), ctx.param("dhw_exclusive_pause_min", t))
                if gap < need - 1:
                    R.finding("BUG", "dhw-pause-short", f"DHW-only start {fmt(t)} after {gap:.0f} min off (< {need:.0f})")


def sec_heating_minutes(ctx: Ctx, R: Report):
    R.h("6. Per-room heating: real time vs `heating_minutes_*` helper")
    R.p(
        "`heating_minutes_*` is written by `_tick` (+1/min while the flag is on) and zeroed by "
        "`_reset_heating_minutes` (forced cooldown, room's floor inactive, `_disable_all_rooms`, daily reset). "
        "Real heating time below is measured from the `input_boolean.heating_*` flag."
    )
    days = sorted({ctx.day_of(ctx.start + dt.timedelta(minutes=m)) for m in range(0, int(mins(ctx.start, ctx.end)) + 1, 60)})
    # 6a. Real minutes per day
    rows = []
    real = {}
    for room in ROOMS:
        ivs = intervals_on(ctx.ser(f"input_boolean.heating_{room}"), ctx.start, ctx.end)
        per = defaultdict(float)
        for a, b in ivs:
            t = a
            while t < b:
                d = ctx.day_of(t)
                nxt = dt.datetime.combine(d + dt.timedelta(days=1), dt.time(0, 0))
                e = min(b, nxt)
                per[d] += mins(t, e)
                t = e
        real[room] = (ivs, per)
        end_val = fnum(ctx.ser(f"input_number.heating_minutes_{room}").at(ctx.end))
        rows.append([room, ROOMS[room][0]] + [f"{per.get(d, 0):.0f}" for d in days] + [end_val])
    R.table(["room", "floor"] + [str(d) for d in days] + ["helper value at CSV end"], rows)

    # 6b. Resets of the helper, with cause
    R.p()
    R.p("Helper drops to 0 and their cause (evidence within ±90 s):")
    rows = []
    causes = Counter()
    hs = ctx.ser("input_text.heat_state")
    for room in ROOMS:
        s = ctx.ser(f"input_number.heating_minutes_{room}")
        for i in range(1, len(s.t)):
            a, b = fnum(s.s[i - 1]), fnum(s.s[i])
            if a is None or b is None or not (a > 0 and b == 0):
                continue
            t = s.t[i]
            near = ctx.logs_near(t, 90, {"COOLDOWN", "RESET"})
            st_after = hs.at(t + dt.timedelta(seconds=5))
            floor = ROOMS[room][0]
            if any(e.kind == "COOLDOWN" and e.m.group(1) == room for e in near):
                cause = "forced cooldown"
            elif any(e.kind == "RESET" for e in near) or (t.time() < dt.time(0, 2)):
                cause = "daily reset"
            elif st_after in HEAT_STATES and HEAT_STATES[st_after] != floor:
                cause = f"floor inactive ({st_after})"
            elif st_after not in HEAT_STATES:
                cause = f"disable_all ({st_after})"
            else:
                cause = "UNKNOWN"
            causes[cause.split(" (")[0]] += 1
            rows.append([fmt(t), room, a, cause])
    rows.sort()
    R.table(["time", "room", "value before", "cause"], rows)

    # 6c. Carry-over: counter not reset between two heating sessions
    R.p()
    R.p("Heating sessions that started with a non-zero helper (counter carried over from an earlier session):")
    rows = []
    for room in ROOMS:
        ivs, _ = real[room]
        s = ctx.ser(f"input_number.heating_minutes_{room}")
        prev_end = None
        for a, b in ivs:
            if a <= ctx.start:
                prev_end = b
                continue
            carried = fnum(s.at(a - dt.timedelta(seconds=1)))
            if carried and carried > 0 and prev_end is not None:
                rows.append([room, fmt(prev_end), fmt(a), f"{mins(prev_end, a):.0f}", carried])
            prev_end = b
    R.table(["room", "prev session ended", "new session started", "gap min", "carried minutes"], rows)
    if rows:
        worst = max(rows, key=lambda r: r[4])
        R.finding(
            "BUG",
            "heating-minutes-carryover",
            f"{len(rows)} heating session(s) started with a non-zero continuous-heating counter "
            f"(worst: {worst[0]} carried {worst[4]:.0f} min over a {worst[3]} min gap). "
            "A room disabled on the *active* floor keeps its counter, so the rotation cap fires early on its next session.",
        )

    # 6d. Cooldowns: was the cap reached by *continuous* heating?
    R.p()
    R.p("Forced cooldowns: logged minutes vs the actual length of the session that was cut:")
    rows = []
    for e in ctx.log:
        if e.kind != "COOLDOWN" or not (ctx.start <= e.t <= ctx.end):
            continue
        room, logged, cap, contended = e.m.group(1), int(e.m.group(2)), int(e.m.group(3)), e.m.group(4)
        ivs, _ = real.get(room, ([], None))
        sess = [(a, b) for a, b in ivs if a <= e.t <= b + dt.timedelta(seconds=90)]
        actual = mins(sess[0][0], e.t) if sess else None
        other = "FF" if ROOMS[room][0] == "GF" else "GF"
        other_demand = [r for r in ROOMS if ROOMS[r][0] == other and _need_heat(ctx, r, e.t)]
        rows.append([fmt(e.t), room, logged, cap, contended, "—" if actual is None else f"{actual:.0f}", ",".join(other_demand) or "none"])
        if actual is not None and actual < logged - 3:
            R.finding("BUG", "cooldown-early", f"{room} forced into cooldown at {fmt(e.t)}: counter {logged} min but session only {actual:.0f} min long")
    R.table(["time", "room", "logged min", "cap", "contended", "actual session min", "other-floor rooms at onset"], rows)

    # 6e. What the dashboard shows
    today = ctx.day_of(ctx.end)
    shown = {r: fnum(ctx.ser(f"input_number.heating_minutes_{r}").at(ctx.end)) for r in ROOMS}
    real_today = {r: real[r][1].get(today, 0.0) for r in ROOMS}
    diff = [r for r in ROOMS if real_today[r] > 5 and (shown[r] or 0) < real_today[r] - 5]
    if diff:
        R.finding(
            "BUG",
            "heating-minutes-not-daily",
            "Dashboard card 'Heating minutes today' shows a continuous-run counter, not a daily total. "
            + ", ".join(f"{r}: shows {shown[r]:.0f}, actually heated {real_today[r]:.0f} min today" for r in diff),
        )


def _need_heat(ctx, room, t):
    snap = ctx.room_snapshot(room, t)
    if snap["t_cur"] is None or snap["user_sp"] is None:
        return False
    return snap["t_cur"] < snap["user_sp"] - ctx.param("heating_hyst_on", t)


class Tracker:
    """Collect intervals during which a named condition held."""

    def __init__(self):
        self.open: dict[tuple, tuple] = {}
        self.done: list[tuple] = []

    def update(self, key, t, cond, detail=""):
        if cond and key not in self.open:
            self.open[key] = (t, detail)
        elif not cond and key in self.open:
            a, d = self.open.pop(key)
            self.done.append((key, a, t, d))

    def close(self, t):
        for key, (a, d) in list(self.open.items()):
            self.done.append((key, a, t, d + " (ongoing)"))
        self.open.clear()

    def by_name(self, name, min_minutes=0):
        return [x for x in self.done if x[0][0] == name and mins(x[1], x[2]) >= min_minutes]


def sec_replay(ctx: Ctx, R: Report, grid_second: int):
    R.h("7. Minute-grid replay: invariants and demand")
    R.p(f"State sampled every minute at :{grid_second:02d} (between ticks). Intervals shorter than the stated minimum are ignored as tick latency.")
    T = Tracker()
    t = ctx.start.replace(second=grid_second, microsecond=0) + dt.timedelta(minutes=1)
    sw = ctx.ser(PUMP_SWITCH)
    hs = ctx.ser("input_text.heat_state")
    pw = ctx.ser(PUMP_POWER)
    af = ctx.ser("input_text.active_floor")
    demand_minutes = defaultdict(Counter)
    while t <= ctx.end:
        pump = sw.at(t) == "on"
        state = hs.at(t)
        off_win = ctx.in_off_window(t)
        hyst_on, hyst_off = ctx.param("heating_hyst_on", t), ctx.param("heating_hyst_off", t)
        off_sp = ctx.param("room_off_setpoint", t)
        snaps = {r: ctx.room_snapshot(r, t) for r in ROOMS}
        heating = [r for r, s in snaps.items() if s["flag"] == "on"]
        active_floor = HEAT_STATES.get(state)

        T.update(("pump-on-closed-valves",), t, pump and state in HEAT_STATES and not heating, state)
        T.update(("fsm-on-pump-off",), t, (state in HEAT_STATES or state == "DHW_QUOTA") and not pump, state)
        T.update(("fsm-off-pump-on",), t, state in ("OFF", "OFF_LOCKOUT") and pump, state)
        if pump:
            on_since = [x for x in sw.transitions() if x[2] == "on" and x[0] <= t]
            since = mins(on_since[-1][0], t) if on_since else 999
            p = fnum(pw.at(t))
            T.update(("pump-no-power",), t, since >= HEALTH_GRACE and p is not None and p < HEALTH_MIN_W, f"{p} W")
            T.update(
                ("pump-in-off-window",), t, off_win and since >= ctx.param("min_pump_on_min", t) + 2, state
            )
        else:
            T.update(("pump-no-power",), t, False)
            T.update(("pump-in-off-window",), t, False)
        a = af.at(t)
        T.update(("active-floor-text",), t, (active_floor or "none") != a and a is not None, f"state={state} text={a}")

        for r, s in snaps.items():
            floor = ROOMS[r][0]
            on = s["flag"] == "on"
            T.update(("flag-on-wrong-floor", r), t, on and active_floor != floor, f"state={state}")
            if on and s["user_sp"] is not None and s["sp"] is not None:
                want = min(30.0, s["user_sp"] + OVERSHOOT)
                T.update(("trv-sp-mismatch", r), t, abs(s["sp"] - want) > 0.05, f"TRV {s['sp']} want {want}")
            elif s["flag"] == "off" and s["sp"] is not None:
                T.update(("trv-sp-mismatch", r), t, abs(s["sp"] - off_sp) > 0.05, f"TRV {s['sp']} want {off_sp}")
            if s["t_cur"] is None or s["user_sp"] is None:
                continue
            need = s["t_cur"] < s["user_sp"] - hyst_on
            sat = s["t_cur"] >= s["user_sp"] + hyst_off
            T.update(("heating-while-satisfied", r), t, on and sat, f"{s['t_cur']} ≥ {s['user_sp']}+{hyst_off}")
            T.update(("hvac-idle-while-flag-on", r), t, on and s["hvac"] not in ("heating", None), f"hvac={s['hvac']}")
            if need and not on:
                if off_win:
                    why = "off window"
                elif not pump:
                    why = "pump off"
                elif active_floor and active_floor != floor:
                    why = "other floor active"
                elif active_floor == floor:
                    why = "same floor, not selected"
                else:
                    why = f"state {state}"
                demand_minutes[r][why] += 1
                T.update(("demand-unserved", r), t, True, why)
            else:
                T.update(("demand-unserved", r), t, False)
            if need or on:
                demand_minutes[r]["heating" if on else ""] += 0  # keep key order stable
            if on:
                demand_minutes[r]["heating"] += 1
        t += dt.timedelta(minutes=1)
    T.close(ctx.end)

    checks = [
        ("pump-on-closed-valves", 3, "BUG", "pump running in HEAT_* with every room flag off"),
        ("fsm-on-pump-off", 3, "BUG", "FSM says heating/DHW but relay is off"),
        ("fsm-off-pump-on", 3, "BUG", "FSM says OFF but relay is on"),
        ("pump-no-power", 3, "SUSPECT", "relay on but power below health threshold"),
        ("pump-in-off-window", 3, "BUG", "pump on inside off window past min_pump_on"),
        ("active-floor-text", 3, "SUSPECT", "input_text.active_floor disagrees with heat_state"),
        ("flag-on-wrong-floor", 2, "BUG", "room flag on while its floor is not the active floor"),
        ("trv-sp-mismatch", 3, "SUSPECT", "TRV setpoint differs from commanded value"),
        ("heating-while-satisfied", 3, "BUG", "room kept heating past user_sp + hyst_off"),
        ("hvac-idle-while-flag-on", 15, "INFO", "TRV reports idle while orchestrator heats the room"),
    ]
    for name, min_m, sev, desc in checks:
        items = T.by_name(name, min_m)
        R.p()
        R.p(f"**{name}** (≥{min_m} min) — {desc}")
        rows = [[(k[1] if len(k) > 1 else ""), fmt(a), fmt(b), f"{mins(a, b):.0f}", d.replace("|", "/")] for k, a, b, d in items]
        R.table(["room", "from", "to", "min", "detail"], rows, limit=40)
        if items:
            total = sum(mins(a, b) for _, a, b, _ in items)
            R.finding(sev, name, f"{len(items)} interval(s), {total:.0f} min total — {desc} (see §7)")

    R.p()
    R.p("Demand minutes per room (grid minutes where `t_cur < user_sp − hyst_on` and the room was not heating, by reason; `heating` = minutes flag on):")
    reasons = sorted({k for c in demand_minutes.values() for k in c if k})
    R.table(["room"] + reasons, [[r] + [demand_minutes[r].get(k, 0) for k in reasons] for r in ROOMS])

    R.p()
    R.p("Longest unserved-demand intervals (≥30 min):")
    items = sorted(T.by_name("demand-unserved", 30), key=lambda x: -mins(x[1], x[2]))
    R.table(["room", "from", "to", "min", "reason at onset"], [[k[1], fmt(a), fmt(b), f"{mins(a, b):.0f}", d] for k, a, b, d in items], limit=25)
    long_pump_off = [x for x in items if x[3] == "pump off" and mins(x[1], x[2]) >= ctx.param("min_pump_off_min", x[1]) + 10]
    for k, a, b, d in long_pump_off:
        R.finding("SUSPECT", "demand-while-pump-off", f"{k[1]} had demand for {mins(a, b):.0f} min from {fmt(a)} with the pump off (outside off window)")


def sec_trv(ctx: Ctx, R: Report):
    R.h("8. Temperature trace per heating session")
    R.p("For every session (flag on→off): temperatures at start/end, peak, and whether the stop was satisfaction.")
    rows = []
    for room in ROOMS:
        c = ctx.ser(ROOMS[room][1])
        for a, b in intervals_on(ctx.ser(f"input_boolean.heating_{room}"), ctx.start, ctx.end):
            t0, t1 = fnum(c.at(a, "current_temperature")), fnum(c.at(b, "current_temperature"))
            i0, i1 = c.idx(a), c.idx(b)
            temps = [fnum(c.a[i].get("current_temperature")) for i in range(max(0, i0), i1 + 1)]
            temps = [x for x in temps if x is not None]
            peak = max(temps) if temps else None
            usp = fnum(ctx.ser(f"input_number.user_sp_{room}").at(b))
            hyst_off = ctx.param("heating_hyst_off", b)
            if b >= ctx.end:
                why = "(ongoing)"
            elif t1 is not None and usp is not None and t1 >= usp + hyst_off:
                why = "satisfied"
            else:
                near = ctx.logs_near(b, 90, {"COOLDOWN", "FLOOR_SWITCH", "DECISION"})
                labels = []
                for e in near:
                    if e.kind == "COOLDOWN" and e.m.group(1) == room:
                        labels.append("cooldown")
                    elif e.kind == "FLOOR_SWITCH":
                        labels.append("floor switch")
                    elif e.kind == "DECISION" and "reason" in decision_fields(e):
                        labels.append(decision_fields(e)["reason"])
                st = ctx.ser("input_text.heat_state").at(b + dt.timedelta(seconds=5))
                why = "/".join(dict.fromkeys(labels)) or f"interrupted (state→{st})"
            rows.append([room, fmt(a), fmt(b), f"{mins(a, b):.0f}", t0, t1, peak, usp, why])
    rows.sort(key=lambda r: r[1])
    R.table(["room", "on", "off", "min", "T start", "T end", "T peak", "user_sp", "stop reason"], rows)


SWITCH_SCORE_RE = re.compile(r"\((\w\w)_score=([\d.]+) > (\w\w)_score=([\d.]+)\)")


def sec_rotation(ctx: Ctx, R: Report):
    R.h("9. Floor rotation")
    R.p(
        "A cap-driven switch (`reason=no_selectable_rooms`) hands the slot to the waiting floor. "
        "A score-based switch back soon after takes it away again. Scores are deficit × priority, "
        "with the deficit measured to user_sp (not to user_sp + hyst_off)."
    )
    sw = [e for e in ctx.log if e.kind == "FLOOR_SWITCH" and ctx.start <= e.t <= ctx.end]
    rows = []
    for i, e in enumerate(sw):
        m = SWITCH_SCORE_RE.search(e.msg)
        kind = "score" if m else ("cap/no_selectable" if "no_selectable_rooms" in e.msg else "other")
        held = mins(sw[i - 1].t, e.t) if i > 0 else None
        lost = ""
        if m:
            winner, ws, loser, ls = m.group(1), float(m.group(2)), m.group(3), float(m.group(4))
            heating_losers = [
                r for r in ROOMS
                if ROOMS[r][0] == loser and ctx.ser(f"input_boolean.heating_{r}").at(e.t - dt.timedelta(seconds=5)) == "on"
            ]
            if ls == 0.0 and heating_losers:
                lost = f"{loser} score 0 while heating {','.join(heating_losers)}"
                R.finding(
                    "SUSPECT", "score-zero-preemption",
                    f"{fmt(e.t)} {loser}→{winner}: {loser} rooms {heating_losers} still had demand "
                    f"(below user_sp+hyst_off) but scored 0 because the deficit is measured to user_sp",
                )
            prev = sw[i - 1] if i > 0 else None
            need = ctx.param("min_state_duration_min", e.t)
            if prev is not None and "no_selectable_rooms" in prev.msg and held is not None and held <= need + 10:
                lost = (lost + "; " if lost else "") + f"gave back a cap-driven slot after {held:.0f} min"
                R.finding(
                    "SUSPECT", "rotation-giveback",
                    f"{fmt(prev.t)} cap handed the slot to {loser}; "
                    f"{fmt(e.t)} score switch took it back after {held:.0f} min (min_state_duration {need:.0f})",
                )
        rows.append([fmt(e.t), kind, e.msg[25:95].replace("|", "/"), "—" if held is None else f"{held:.0f}", lost])
    R.table(["time", "kind", "log", "min since prev switch", "note"], rows)

    # Sensor resolution vs hysteresis
    vals = []
    for room in ROOMS:
        c = ctx.ser(ROOMS[room][1])
        vals += [fnum(a.get("current_temperature")) for a in c.a]
    vals = [v for v in vals if v is not None]
    if vals:
        steps = sorted({round(abs(v * 10)) % 10 for v in vals})
        if set(steps) <= {0, 5}:
            h_on = ctx.param("heating_hyst_on", ctx.end)
            h_off = ctx.param("heating_hyst_off", ctx.end)
            R.p()
            R.p(
                f"All {len(vals)} TRV `current_temperature` samples are multiples of 0.5 °C. "
                f"With hyst_on={h_on} / hyst_off={h_off} the effective thresholds are "
                f"`need_heat` at ≤ user_sp − 0.5 and `satisfied` at ≥ user_sp + 0.5: a 1.0 °C band, "
                f"and a room sitting exactly at user_sp has demand (if heating/latched) but a score of 0."
            )
            R.finding(
                "INFO", "sensor-resolution",
                f"TRV temperatures are quantised to 0.5 °C; hyst_on {h_on}/hyst_off {h_off} act as ±0.5 °C (see §9)",
            )

    # Weather fallback
    bad = [e for e in ctx.log if e.kind == "SYSTEM" and "return_result" in e.msg]
    if bad:
        R.finding(
            "BUG", "weather-fallback-broken",
            f"{len(bad)}× weather.get_forecasts rejected ('return_result' is not a valid service field), "
            f"{fmt(bad[0].t)}–{fmt(bad[-1].t)}; the app fell back to the last known outdoor temp",
        )


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("log")
    ap.add_argument("--out")
    ap.add_argument("--utc-offset", type=float)
    ap.add_argument("--grid-second", type=int, default=GRID_SECOND)
    args = ap.parse_args(argv)

    raw = load_csv(args.csv)
    offset = args.utc_offset if args.utc_offset is not None else detect_offset(raw)
    series, start, end = build_series(raw, offset)
    log = load_log(args.log)
    ctx = Ctx(series, start, end, log, offset)

    R = Report()
    sec_coverage(ctx, R)
    sec_log_to_csv(ctx, R)
    sec_csv_to_log(ctx, R)
    sec_pump(ctx, R)
    sec_fsm(ctx, R)
    sec_heating_minutes(ctx, R)
    sec_replay(ctx, R, args.grid_second)
    sec_trv(ctx, R)
    sec_rotation(ctx, R)

    text = R.render(f"Heat Orchestrator history analysis ({fmt(start)} → {fmt(end)})")
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"report written to {args.out} ({len(R.findings)} findings)")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main()
