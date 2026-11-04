#!/usr/bin/env python3
"""Energy-oriented analysis of a Home Assistant history CSV export (read-only).

Complements tools/analyze_history.py (which correlates the CSV with the
AppDaemon log). This one needs the CSV alone and reports: pump relay runs,
compressor starts/cycles from the Sonoff power sensor, kWh per day vs outdoor
temperature, FSM time share and transitions, per-room heating sessions with
overshoot and response lag.

Usage: python3 -I tools/analyze_history_energy.py HISTORY.csv
"""
import csv, sys, datetime as dt, statistics, collections, bisect
from zoneinfo import ZoneInfo

F = sys.argv[1]
TZ = ZoneInfo("Europe/Warsaw")
START = dt.datetime(2026, 10, 25, 0, 0, tzinfo=TZ)   # detailed data from here
POWER = "sensor.zasilanie_pompy_sonoff_10017fadeb_power"
SWITCH = "switch.zasilanie_pompy_sonoff_10017fadeb_1"
OUT = "sensor.komfovent_outdoor_temperature"
ROOMS = ["gabinet_ani", "lazienka_parter", "salon", "garaz",
         "sypialnia", "lazienka_pietro", "pokoj_narozny", "pokoj_z_garazem"]
FLOOR = {r: ("GF" if r in ROOMS[:4] else "FF") for r in ROOMS}

def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ)

series = collections.defaultdict(list)   # entity -> [(t, state, row)]
with open(F, newline="") as fh:
    for row in csv.DictReader(fh):
        if not row["last_changed"]:
            continue
        t = ts(row["last_changed"])
        if t < START:
            continue
        series[row["entity_id"]].append((t, row["state"], row))
for k in series:
    series[k].sort(key=lambda x: x[0])

END = max(t for v in series.values() for t, _, _ in v)
print(f"window: {START:%Y-%m-%d %H:%M} .. {END:%Y-%m-%d %H:%M} local\n")

def fl(x):
    try:
        return float(x)
    except Exception:
        return None

# ---------------------------------------------------------------- outdoor
out = [(t, fl(s)) for t, s, _ in series[OUT] if fl(s) is not None]
out_t = [t for t, _ in out]
def outdoor_at(t):
    i = bisect.bisect_right(out_t, t) - 1
    return out[i][1] if i >= 0 else None

# ---------------------------------------------------------------- pump runs
sw = [(t, s) for t, s, _ in series[SWITCH]]
runs = []   # (on_t, off_t)
cur_on = None
for t, s in sw:
    if s == "on" and cur_on is None:
        cur_on = t
    elif s == "off" and cur_on is not None:
        runs.append((cur_on, t)); cur_on = None
if cur_on is not None:
    runs.append((cur_on, END))
print("== pump runs (relay) ==")
for a, b in runs:
    print(f"  {a:%m-%d %H:%M} -> {b:%m-%d %H:%M}  {(b-a).total_seconds()/3600:5.1f} h  Tout_start={outdoor_at(a)}")
offs = [(runs[i][1], runs[i+1][0]) for i in range(len(runs)-1)]
print("  off gaps [h]:", [round((b-a).total_seconds()/3600, 1) for a, b in offs])
print()

# ---------------------------------------------------------------- power -> compressor cycles
pw = [(t, fl(s)) for t, s, _ in series[POWER] if fl(s) is not None]
print(f"== power samples: {len(pw)}, median gap "
      f"{statistics.median([(pw[i+1][0]-pw[i][0]).total_seconds() for i in range(len(pw)-1)]):.0f}s ==")
# distribution of power while relay on
def in_run(t):
    for a, b in runs:
        if a <= t <= b:
            return True
    return False
vals_on = [p for t, p in pw if in_run(t)]
qs = statistics.quantiles(vals_on, n=20)
print("  power while relay on, 5%..95% quantiles:", [round(q) for q in qs])
hist = collections.Counter(int(p // 250) * 250 for p in vals_on)
print("  histogram (W bucket: share):", {k: f"{v/len(vals_on):.0%}" for k, v in sorted(hist.items()) if v/len(vals_on) >= 0.01})

THR = 600.0   # compressor considered running above this
def cycles_for(a, b):
    seg = [(t, p) for t, p in pw if a <= t <= b]
    state = None; last_t = None
    ons = []; offs_ = []; starts = []
    seg_on_start = None; seg_off_start = None
    for t, p in seg:
        s = p > THR
        if state is None:
            state = s; last_t = t
            if s: seg_on_start = t
            else: seg_off_start = t
            continue
        if s != state:
            if s:   # off -> on
                starts.append(t)
                if seg_off_start: offs_.append((t - seg_off_start).total_seconds()/60)
                seg_on_start = t
            else:   # on -> off
                if seg_on_start: ons.append((t - seg_on_start).total_seconds()/60)
                seg_off_start = t
            state = s
    return starts, ons, offs_

# ---------------------------------------------------------------- active rooms timeline
ar = [(t, s) for t, s, _ in series["input_text.active_rooms"]]
ar_t = [t for t, _ in ar]
def active_rooms_at(t):
    i = bisect.bisect_right(ar_t, t) - 1
    if i < 0: return None
    s = ar[i][1]
    return [x for x in s.split(",") if x] if s else []
hs = [(t, s) for t, s, _ in series["input_text.heat_state"]]
hs_t = [t for t, _ in hs]
def state_at(t):
    i = bisect.bisect_right(hs_t, t) - 1
    return hs[i][1] if i >= 0 else None

print(f"\n== compressor cycles per pump run (threshold {THR:.0f} W) ==")
tot_starts = 0; tot_hours = 0
by_nrooms = collections.defaultdict(lambda: [0, 0.0])   # n_rooms -> [starts, hours]
by_state = collections.defaultdict(lambda: [0, 0.0])
for a, b in runs:
    starts, ons, offs_ = cycles_for(a, b)
    h = (b - a).total_seconds()/3600
    tot_starts += len(starts); tot_hours += h
    print(f"  {a:%m-%d %H:%M} {h:5.1f}h  starts={len(starts):3d} ({len(starts)/h if h else 0:4.1f}/h)  "
          f"on-dur med={statistics.median(ons) if ons else 0:5.1f}min  off-dur med={statistics.median(offs_) if offs_ else 0:5.1f}min  "
          f"short(<6min)={sum(1 for x in ons if x < 6)}")
    # attribute each start to the number of active rooms / state at that time
    for st in starts:
        n = active_rooms_at(st); n = len(n) if n is not None else -1
        by_nrooms[n][0] += 1
        by_state[state_at(st)][0] += 1
# hours per n_rooms / state while relay on (sample on a 1-min grid)
t = START
while t < END:
    if in_run(t):
        n = active_rooms_at(t); n = len(n) if n is not None else -1
        by_nrooms[n][1] += 1/60
        by_state[state_at(t)][1] += 1/60
    t += dt.timedelta(minutes=1)
print(f"  TOTAL relay-on {tot_hours:.0f} h, compressor starts {tot_starts} -> {tot_starts/tot_hours:.2f}/h")
print("  by number of active rooms: n_rooms -> starts/h (hours)")
for n in sorted(by_nrooms):
    s, h = by_nrooms[n]
    print(f"    {n:2d}: {s/h if h else 0:4.1f}/h  ({h:5.1f} h, {s} starts)")
print("  by FSM state:")
for k in sorted(by_state, key=str):
    s, h = by_state[k]
    print(f"    {str(k):11s}: {s/h if h else 0:4.1f}/h  ({h:5.1f} h)")

# ---------------------------------------------------------------- per-day energy
print("\n== per day: relay-on hours, kWh (trapezoid on power), compressor starts, mean Tout, mean avg room temp ==")
days = collections.defaultdict(lambda: {"kwh": 0.0, "on_h": 0.0, "starts": 0, "tout": [], "avg": []})
for i in range(len(pw)-1):
    (t0, p0), (t1, p1) = pw[i], pw[i+1]
    d = (t1 - t0).total_seconds()
    if d > 3600: continue
    days[t0.date()]["kwh"] += (p0 + p1)/2 * d/3600/1000
for a, b in runs:
    t = a
    while t < b:
        nxt = min(b, (t + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
        days[t.date()]["on_h"] += (nxt - t).total_seconds()/3600
        t = nxt
    for st in cycles_for(a, b)[0]:
        days[st.date()]["starts"] += 1
for t, v in out:
    days[t.date()]["tout"].append(v)
for t, s, _ in series["sensor.ho_avg_room_temp"]:
    if fl(s) is not None: days[t.date()]["avg"].append(fl(s))
for d in sorted(days):
    v = days[d]
    if d < START.date(): continue
    print(f"  {d}  on={v['on_h']:4.1f}h  {v['kwh']:5.1f} kWh  starts={v['starts']:3d}  "
          f"Tout={statistics.mean(v['tout']) if v['tout'] else float('nan'):5.1f}  "
          f"Troom_avg={statistics.mean(v['avg']) if v['avg'] else float('nan'):5.2f}")

# ---------------------------------------------------------------- FSM
print("\n== FSM: time share and transitions ==")
share = collections.Counter(); trans = collections.Counter()
for i in range(len(hs)):
    t0, s0 = hs[i]
    t1 = hs[i+1][0] if i+1 < len(hs) else END
    share[s0] += (t1 - t0).total_seconds()/3600
    if i+1 < len(hs): trans[(s0, hs[i+1][1])] += 1
for k, v in share.most_common(): print(f"  {k:12s} {v:6.1f} h")
for k, v in trans.most_common(): print(f"  {k[0]:>12s} -> {k[1]:<12s} {v}")

# ---------------------------------------------------------------- rooms: sessions and overshoot
print("\n== per-room heating sessions (input_boolean.heating_*) and overshoot ==")
sp = {}
for r in ROOMS:
    v = series.get(f"input_number.user_sp_{r}", [])
    sp[r] = [(t, fl(s)) for t, s, _ in v]
def sp_at(r, t):
    arr = sp[r]
    i = bisect.bisect_right([x for x, _ in arr], t) - 1
    return arr[i][1] if i >= 0 else None
ct = {}
for r in ROOMS:
    ct[r] = [(t, fl(row["current_temperature"])) for t, s, row in series.get(f"climate.{r}", [])
             if fl(row["current_temperature"]) is not None]
def temp_at(r, t):
    arr = ct[r]
    i = bisect.bisect_right([x for x, _ in arr], t) - 1
    return arr[i][1] if i >= 0 else None
def temp_max_after(r, t, hours):
    return max([v for x, v in ct[r] if t <= x <= t + dt.timedelta(hours=hours)] + [temp_at(r, t) or -99])

for r in ROOMS:
    hb = [(t, s) for t, s, _ in series.get(f"input_boolean.heating_{r}", [])]
    sess = []; on_t = None
    for t, s in hb:
        if s == "on" and on_t is None: on_t = t
        elif s == "off" and on_t is not None: sess.append((on_t, t)); on_t = None
    if not sess:
        print(f"  {r:16s} no sessions"); continue
    durs = [(b-a).total_seconds()/60 for a, b in sess]
    over = []; over3 = []; lag = []
    for a, b in sess:
        s_ = sp_at(r, b); te = temp_at(r, b)
        if s_ is None or te is None: continue
        over.append(te - s_)
        over3.append(temp_max_after(r, b, 3) - s_)
        # lag: first temperature increase after enable
        t0 = temp_at(r, a)
        later = [(x, v) for x, v in ct[r] if x > a and v > (t0 if t0 is not None else -99)]
        if later and later[0][0] < b + dt.timedelta(hours=1): lag.append((later[0][0]-a).total_seconds()/60)
    print(f"  {r:16s} sessions={len(sess):3d} total={sum(durs)/60:5.1f}h  dur med={statistics.median(durs):5.0f}min max={max(durs):4.0f}  "
          f"T-sp at stop med={statistics.median(over) if over else float('nan'):+.2f}  "
          f"max(T)-sp within 3h med={statistics.median(over3) if over3 else float('nan'):+.2f}  "
          f"lag to first rise med={statistics.median(lag) if lag else float('nan'):4.0f}min (n={len(lag)})")

# how often did a session end by cap (duration >= cap) vs satisfied
print("\n== session end reasons (approx): duration >= 120 min (old cap) ==")
for r in ROOMS:
    hb = [(t, s) for t, s, _ in series.get(f"input_boolean.heating_{r}", [])]
    sess = []; on_t = None
    for t, s in hb:
        if s == "on" and on_t is None: on_t = t
        elif s == "off" and on_t is not None: sess.append((on_t, t)); on_t = None
    if not sess: continue
    capped = sum(1 for a, b in sess if (b-a).total_seconds()/60 >= 119)
    short = sum(1 for a, b in sess if (b-a).total_seconds()/60 <= 5)
    print(f"  {r:16s} {capped}/{len(sess)} sessions >=119min, {short} sessions <=5min")

# ---------------------------------------------------------------- power while DHW_QUOTA / all valves closed
print("\n== power stats by state (median W, share of samples >600W) ==")
bys = collections.defaultdict(list)
for t, p in pw:
    if in_run(t): bys[state_at(t)].append(p)
for k, v in bys.items():
    print(f"  {str(k):11s} n={len(v):6d} med={statistics.median(v):6.0f}W  >600W: {sum(1 for x in v if x > 600)/len(v):.0%}")
