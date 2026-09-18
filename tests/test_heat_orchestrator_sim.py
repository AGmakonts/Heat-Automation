"""
Minute-by-minute simulation of HeatOrchestrator against a stubbed hassapi.

Runs the real `_tick()` logic with an in-memory Home Assistant state table
and a configurable relay lag (the Sonoff relay does not flip until N ticks
after the start/stop script is called).  No AppDaemon or HA required.

Run:  python -m pytest tests
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import types
from pathlib import Path

import pytest

APP_PATH = Path(__file__).resolve().parents[1] / "apps" / "heat_orchestrator" / "heat_orchestrator.py"


# ---------------------------------------------------------------------------
# hassapi stub
# ---------------------------------------------------------------------------
class FakeHass:
    """Minimal stand-in for appdaemon.plugins.hass.hassapi.Hass."""

    def __init__(self):
        self.states: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.logs: list[tuple[str, str]] = []
        self._run_in: list[tuple] = []
        self.now = dt.datetime(2026, 6, 1, 6, 0, 0)
        # relay behaviour
        self.relay_lag_ticks = 0
        self._pending_relay: tuple[str, int] | None = None
        self.tick_index = 0

    # --- state table -------------------------------------------------------
    def set(self, entity: str, state, **attrs):
        e = self.states.setdefault(entity, {"state": None, "attributes": {}})
        e["state"] = None if state is None else str(state)
        e["attributes"].update(attrs)

    def get_state(self, entity, attribute=None, **kwargs):
        e = self.states.get(entity)
        if e is None:
            return None
        if attribute is not None:
            return e["attributes"].get(attribute)
        return e["state"]

    def entity_exists(self, entity):
        return entity in self.states

    # --- time / scheduling ---------------------------------------------------
    def datetime(self, *a, **k):
        return self.now

    def run_in(self, cb, delay, **kwargs):
        self._run_in.append((cb, kwargs))

    def flush_run_in(self):
        pending, self._run_in = self._run_in, []
        for cb, kwargs in pending:
            cb(**kwargs)

    def run_every(self, *a, **k):
        pass

    def run_daily(self, *a, **k):
        pass

    def listen_state(self, *a, **k):
        pass

    def log(self, msg, level="INFO", **kwargs):
        self.logs.append((level, msg))

    # --- services -----------------------------------------------------------
    def call_service(self, service, **kwargs):
        self.calls.append((service, dict(kwargs)))
        entity = kwargs.get("entity_id")
        if service == "input_number/set_value":
            self.set(entity, kwargs["value"])
        elif service == "input_text/set_value":
            self.set(entity, kwargs["value"])
        elif service == "input_datetime/set_datetime":
            self.set(entity, kwargs["datetime"])
        elif service == "input_boolean/turn_on":
            self.set(entity, "on")
        elif service == "input_boolean/turn_off":
            self.set(entity, "off")
        elif service == "climate/set_temperature":
            self.set(entity, self.get_state(entity), temperature=kwargs["temperature"])
        elif service == "script/turn_on":
            target = "on" if entity.endswith("uruchom_pompe") else "off"
            if self._pending_relay and self._pending_relay[0] == target:
                pass  # already commanded; a real relay flips on the first call
            else:
                self._pending_relay = (target, self.tick_index + 1 + self.relay_lag_ticks)
        elif service == "weather/get_forecasts":
            return {}
        return None

    def apply_pending_relay(self):
        if self._pending_relay and self.tick_index >= self._pending_relay[1]:
            target, _ = self._pending_relay
            self._pending_relay = None
            self.set(PUMP_SWITCH, target)
            self.set(PUMP_POWER, 1200 if target == "on" else 0)


PUMP_SWITCH = "switch.zasilanie_pompy_sonoff_10017fadeb_1"
PUMP_POWER = "sensor.zasilanie_pompy_sonoff_10017fadeb_power"


def load_app_module():
    stub = types.ModuleType("hassapi")
    stub.Hass = FakeHass
    sys.modules["hassapi"] = stub
    spec = importlib.util.spec_from_file_location("heat_orchestrator_under_test", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Simulation fixture
# ---------------------------------------------------------------------------
class Sim:
    def __init__(self, relay_lag_ticks: int = 0, start: dt.datetime | None = None):
        self.mod = load_app_module()
        self.app = self.mod.HeatOrchestrator()
        h = self.app
        h.relay_lag_ticks = relay_lag_ticks
        if start:
            h.now = start
        self._seed_defaults()
        h.initialize()
        # Ignore anything issued during initialize()
        h.calls.clear()
        h.logs.clear()

    def _seed_defaults(self):
        h = self.app
        m = self.mod
        num = {
            "room_off_setpoint": 7, "heating_hyst_on": 0.3, "heating_hyst_off": 0.2,
            "min_state_duration_min": 25, "min_pump_on_min": 40, "min_pump_off_min": 25,
            "dhw_min_run_hours": 3.5, "pump_on_minutes_today": 0, "pump_starts_today": 0,
            "dhw_exclusive_max_run_min": 45, "dhw_exclusive_pause_min": 90,
            "bulk_mode_temp": 5, "sequential_mode_temp": -5, "max_rooms_limited": 2,
            "max_continuous_heating_min": 120,
            "max_continuous_heating_solo_min": 240,
            "lerp_temp_min": -10, "lerp_temp_max": 10, "lerp_rooms_min": 1, "lerp_rooms_max": 5,
        }
        for k, v in num.items():
            h.set(f"input_number.{k}", v)
        for k in ("last_pump_on", "last_pump_off", "state_since"):
            h.set(f"input_datetime.{k}", "unknown")
        h.set("input_datetime.off_window_start", "22:00:00")
        h.set("input_datetime.off_window_end", "06:00:00")
        h.set("input_datetime.day_reset_time", "00:00:00")
        h.set("input_text.heat_state", "OFF")
        h.set("input_text.active_floor", "none")
        h.set("input_text.active_rooms", "")
        h.set("input_text.pump_health", "UNKNOWN")
        h.set(PUMP_SWITCH, "off")
        h.set(PUMP_POWER, 0)
        h.set(m.WEATHER_ENTITY, "sunny", temperature=20.0)
        for key, room in m.ROOMS.items():
            # Summer: every room warm, parked at the off setpoint, no demand.
            h.set(room.climate, "heat", temperature=7.0, current_temperature=25.0)
            h.set(room.user_sp, 21.0)
            h.set(room.priority, 50)
            h.set(room.heating, "off")
            h.set(room.heating_minutes, 0)

    # --- driving -------------------------------------------------------------
    def step(self, minutes: int = 1):
        h = self.app
        for _ in range(minutes):
            h.now += dt.timedelta(seconds=60)
            h.tick_index += 1
            h.apply_pending_relay()
            h._tick()
            h.flush_run_in()

    def run_until(self, predicate, max_minutes: int):
        for _ in range(max_minutes):
            self.step()
            if predicate():
                return True
        return False

    # --- observation -----------------------------------------------------------
    def pump_on(self) -> bool:
        return self.app.get_state(PUMP_SWITCH) == "on"

    def state(self) -> str:
        return self.app.get_state("input_text.heat_state")

    def calls(self, service: str, entity: str | None = None):
        return [
            c for c in self.app.calls
            if c[0] == service and (entity is None or c[1].get("entity_id") == entity)
        ]

    def log_lines(self, needle: str):
        return [m for _, m in self.app.logs if needle in m]


def pump_blocks(sim: Sim, minutes: int) -> list[tuple[dt.datetime, dt.datetime]]:
    """Run `minutes` ticks and return contiguous pump-on intervals."""
    blocks, start = [], None
    for _ in range(minutes):
        sim.step()
        on = sim.pump_on()
        if on and start is None:
            start = sim.app.now
        elif not on and start is not None:
            blocks.append((start, sim.app.now))
            start = None
    if start is not None:
        blocks.append((start, sim.app.now))
    return blocks


def mins(a: dt.datetime, b: dt.datetime) -> float:
    return (b - a).total_seconds() / 60


# ---------------------------------------------------------------------------
# DHW duty-cycle (PR #10 feature)
# ---------------------------------------------------------------------------
def test_dhw_quota_is_duty_cycled_across_the_day():
    sim = Sim()
    blocks = pump_blocks(sim, 12 * 60)

    assert len(blocks) >= 4, blocks
    # every block but the last is capped at the exclusive max run (±1 tick)
    for a, b in blocks[:-1]:
        assert 44 <= mins(a, b) <= 47, (a, b)
    # pauses honour dhw_exclusive_pause (90 min), never shorter
    for (_, end), (start, _) in zip(blocks, blocks[1:]):
        assert mins(end, start) >= 89, (end, start)
    # total on-time ≈ quota (3.5 h) — the last block may be padded to min_pump_on
    total = sum(mins(a, b) for a, b in blocks)
    assert 210 <= total <= 230, total


def test_duty_cycle_disabled_when_max_run_is_zero_gives_single_block():
    sim = Sim()
    sim.app.set("input_number.dhw_exclusive_max_run_min", 0)
    blocks = pump_blocks(sim, 12 * 60)

    assert len(blocks) == 1, blocks
    assert 209 <= mins(*blocks[0]) <= 212


def test_room_demand_start_is_not_delayed_by_dhw_pause():
    sim = Sim()
    # First DHW block runs and stops; we are now inside the 90 min pause.
    assert sim.run_until(lambda: sim.log_lines("reason=dhw_exclusive_max_run"), 120)
    sim.step(30)  # past min_pump_off (25) but well inside the pause
    assert not sim.pump_on()

    room = sim.mod.ROOMS["salon"]
    sim.app.set(room.climate, "heat", current_temperature=18.0)  # demand
    sim.step(1)
    assert sim.pump_on() or sim.calls("script/turn_on", "script.uruchom_pompe")
    assert sim.state() == "HEAT_GF"


# ---------------------------------------------------------------------------
# Steady-OFF re-park (PR #10) and its fix for unavailable / unmanaged TRVs
# ---------------------------------------------------------------------------
def _quota_done(sim: Sim):
    sim.app.set("input_number.pump_on_minutes_today", 210)


def test_manual_setpoint_in_steady_off_is_reparked_within_one_tick():
    sim = Sim()
    _quota_done(sim)
    sim.step(2)
    assert sim.state() == "OFF"

    room = sim.mod.ROOMS["sypialnia"]
    sim.app.set(room.climate, "heat", temperature=22.0)  # user turned the TRV
    sim.step(1)
    parks = sim.calls("climate/set_temperature", room.climate)
    assert parks and parks[-1][1]["temperature"] == 7.0


def test_steady_off_repark_does_not_command_unavailable_trv_every_tick():
    sim = Sim()
    _quota_done(sim)
    room = sim.mod.ROOMS["garaz"]
    # TRV offline: its temperature attribute is gone
    sim.app.set(room.climate, "unavailable", temperature=None)

    sim.step(10)

    assert sim.calls("climate/set_temperature", room.climate) == []
    # and the other rooms, already parked, were not commanded either
    others = [c for c in sim.calls("climate/set_temperature") if c[1]["entity_id"] != room.climate]
    assert others == []


def test_steady_off_repark_skips_unmanaged_room_until_timeout():
    sim = Sim()
    _quota_done(sim)
    room = sim.mod.ROOMS["lazienka_pietro"]
    sim.app.set(room.climate, "heat", temperature=22.0)
    sim.app.unmanaged_rooms["lazienka_pietro"] = sim.app.now  # two failed commands earlier

    sim.step(5)
    assert sim.calls("climate/set_temperature", room.climate) == []

    sim.step(12)  # > 15 min since it was marked unmanaged
    assert sim.calls("climate/set_temperature", room.climate), "should resume once the timeout expires"


# ---------------------------------------------------------------------------
# Relay lag after a stop command (production log shows 1 tick)
# ---------------------------------------------------------------------------
def test_relay_lag_after_duty_stop_does_not_reenter_dhw_quota():
    sim = Sim(relay_lag_ticks=1)
    assert sim.run_until(lambda: sim.log_lines("reason=dhw_exclusive_max_run"), 120)
    assert sim.pump_on(), "relay must still be on for one tick after the stop"
    since_after_stop = sim.app.get_state("input_datetime.state_since")
    quota_entries_before = len(sim.log_lines("state=DHW_QUOTA reason=quota"))

    sim.step(1)  # the lag tick: relay still 'on', FSM says OFF

    assert sim.state() == "OFF"
    assert sim.app.get_state("input_datetime.state_since") == since_after_stop
    assert len(sim.log_lines("state=DHW_QUOTA reason=quota")) == quota_entries_before

    sim.step(1)
    assert not sim.pump_on()
    assert sim.state() == "OFF"


def test_relay_lag_does_not_refire_stop_script_or_restamp_last_pump_off():
    sim = Sim(relay_lag_ticks=1)
    assert sim.run_until(lambda: sim.log_lines("reason=dhw_exclusive_max_run"), 120)
    stops_before = len(sim.calls("script/turn_on", "script.wylacz_pompe"))
    off_stamp = sim.app.get_state("input_datetime.last_pump_off")

    sim.step(2)

    assert len(sim.calls("script/turn_on", "script.wylacz_pompe")) == stops_before
    assert sim.app.get_state("input_datetime.last_pump_off") == off_stamp


def test_off_window_stop_with_relay_lag_fires_stop_script_once():
    # Start at 21:00 with quota remaining so the pump runs into the off window.
    sim = Sim(relay_lag_ticks=1, start=dt.datetime(2026, 6, 1, 21, 0, 0))
    sim.app.set("input_number.dhw_exclusive_max_run_min", 0)  # continuous run
    sim.step(50)
    assert sim.pump_on()
    sim.step(20)  # crosses 22:00 -> off window; min_pump_on (40) is satisfied

    stops = sim.calls("script/turn_on", "script.wylacz_pompe")
    assert len(stops) == 1, stops
    assert not sim.pump_on()
    assert sim.state() == "OFF_LOCKOUT"


# ---------------------------------------------------------------------------
# Contention-aware continuous-heating cap, resume latch, empty-selection
# fallback  (2026-09-18 morning log)
# ---------------------------------------------------------------------------
def _demand(sim: Sim, room_key: str, cur: float, user_sp: float = 23.5):
    """Give a room a fixed current temperature and user setpoint."""
    room = sim.mod.ROOMS[room_key]
    sim.app.set(room.climate, "heat", current_temperature=cur)
    sim.app.set(room.user_sp, user_sp)


def _no_demand(sim: Sim, room_key: str):
    _demand(sim, room_key, 25.0, 21.0)


def _warm_morning(relay_lag_ticks: int = 0, t_out: float = 12.2) -> Sim:
    """06:00, two GF rooms below setpoint, no FF demand — the logged case."""
    sim = Sim(relay_lag_ticks=relay_lag_ticks, start=dt.datetime(2026, 9, 18, 6, 0, 0))
    sim.app.set(sim.mod.WEATHER_ENTITY, "cloudy", temperature=t_out)
    _demand(sim, "salon", 22.0)
    _demand(sim, "lazienka_parter", 22.2)
    return sim


def test_uncontended_floor_is_not_forced_into_cooldown_at_the_rotation_cap():
    """Two rooms, five slots, no FF demand: rotation would hand the slot to
    nobody, so the 120 min cap must not fire (the 2026-09-18 07:00 case)."""
    sim = _warm_morning()
    sim.step(130)

    assert sim.log_lines("forced cooldown") == []
    assert sim.state() == "HEAT_GF"
    for key in ("salon", "lazienka_parter"):
        assert sim.app.get_state(sim.mod.ROOMS[key].heating) == "on"


def test_uncontended_floor_still_stops_at_the_solo_safety_cap():
    sim = _warm_morning()
    sim.app.set("input_number.max_continuous_heating_solo_min", 150)
    sim.step(155)

    assert sim.log_lines("forced cooldown"), "solo cap must still bound the run"
    assert "cap=150min" in sim.log_lines("forced cooldown")[0]
    assert "contended=False" in sim.log_lines("forced cooldown")[0]


def test_solo_cap_zero_means_no_limit_when_uncontended():
    sim = _warm_morning()
    sim.app.set("input_number.max_continuous_heating_solo_min", 0)
    sim.step(8 * 60)

    assert sim.log_lines("forced cooldown") == []


def test_cap_still_rotates_when_more_rooms_want_heat_than_slots():
    """Cold day: LERP allows one room, two want heat → contention → rotation."""
    sim = Sim(start=dt.datetime(2026, 9, 18, 6, 0, 0))
    sim.app.set(sim.mod.WEATHER_ENTITY, "cloudy", temperature=-10.0)  # LERP → 1 room
    _demand(sim, "salon", 22.0)
    _demand(sim, "gabinet_ani", 22.0)
    sim.step(130)

    cooldowns = sim.log_lines("forced cooldown")
    assert cooldowns, "contended floor must still rotate"
    assert "contended=True" in cooldowns[0]
    assert "cap=120min" in cooldowns[0]


def test_cap_still_rotates_when_the_other_floor_is_waiting():
    """Five slots and only two GF rooms with demand, but FF wants heat with a
    much lower score: the score comparison alone never flips, so the cap is the
    only thing that hands FF the pump. Skipping it on room count alone (GF is
    not full) would starve FF for the rest of the day."""
    sim = _warm_morning()
    _demand(sim, "sypialnia", 20.6, user_sp=21.0)  # FF demand, small deficit

    assert sim.run_until(lambda: sim.state() == "HEAT_FF", 130), "FF never got the pump"
    assert sim.app.get_state(sim.mod.ROOMS["sypialnia"].heating) == "on"
    # the GF rooms were interrupted mid-job, not satisfied
    assert sim.app.room_resume_pending["salon"]
    assert sim.app.room_resume_pending["lazienka_parter"]


def test_room_interrupted_by_rotation_resumes_inside_the_hysteresis_band():
    """A room rotated out at sp-0.05 must finish the job when it gets the slot
    back, not wait for the onset threshold (sp-0.3) that a slab can take hours
    to drift down to."""
    sim = _warm_morning()
    _no_demand(sim, "lazienka_parter")
    _demand(sim, "sypialnia", 20.6, user_sp=21.0)
    assert sim.run_until(lambda: sim.state() == "HEAT_FF", 130)

    # salon is now inside the band: below setpoint, above the onset threshold
    _demand(sim, "salon", 23.45)          # sp 23.5, onset 23.2, satisfied 23.7
    _demand(sim, "sypialnia", 21.3, user_sp=21.0)  # FF satisfied → GF's turn
    assert sim.app.room_resume_pending["salon"]

    assert sim.run_until(
        lambda: sim.app.get_state(sim.mod.ROOMS["salon"].heating) == "on", 40
    ), "interrupted room must resume without re-crossing the onset threshold"
    assert sim.state() == "HEAT_GF"


def test_resume_latch_releases_when_the_room_is_satisfied():
    sim = _warm_morning()
    _no_demand(sim, "lazienka_parter")
    _demand(sim, "sypialnia", 20.6, user_sp=21.0)
    assert sim.run_until(lambda: sim.state() == "HEAT_FF", 130)
    assert sim.app.room_resume_pending["salon"]

    _demand(sim, "salon", 23.8)  # >= sp + hyst_off
    sim.step(2)

    assert not sim.app.room_resume_pending["salon"]
    assert sim.app.get_state(sim.mod.ROOMS["salon"].heating) == "off"


def test_resume_latch_does_not_survive_the_night_off_window():
    sim = Sim(start=dt.datetime(2026, 9, 17, 21, 20, 0))
    sim.app.set(sim.mod.WEATHER_ENTITY, "cloudy", temperature=12.0)
    _demand(sim, "salon", 22.0)
    sim.step(35)  # 21:55 — heating, mid-job
    assert sim.app.get_state(sim.mod.ROOMS["salon"].heating) == "on"
    sim.step(20)  # 22:15 — off window parked it and dropped the latch
    assert not sim.app.room_resume_pending["salon"]

    _demand(sim, "salon", 23.45)  # inside the band by morning
    sim.step(9 * 60)              # through the night, past 06:00

    assert not sim.app.room_resume_pending["salon"]
    assert sim.app.get_state(sim.mod.ROOMS["salon"].heating) == "off", \
        "a stale latch must not reopen a room sitting 0.05°C below setpoint"
    assert sim.state() != "HEAT_GF"


def _freeze_all_rooms(sim: Sim, minutes: int = 30):
    """Put every room in cooldown, as a simultaneous rotation would."""
    for key in sim.mod.ALL_ROOMS:
        sim.app.room_cooldown_until[key] = sim.app.now + dt.timedelta(minutes=minutes)


def test_no_selectable_rooms_puts_the_run_on_dhw_instead_of_closed_valves():
    """The 07:01 log line: state=HEAT_GF rooms=[] with the pump running."""
    sim = _warm_morning()
    sim.step(30)
    assert sim.state() == "HEAT_GF" and sim.pump_on()
    _freeze_all_rooms(sim)
    sim.step(2)

    assert sim.state() == "DHW_QUOTA", sim.state()
    assert sim.pump_on(), "quota left → keep the compressor working on DHW"
    for key in sim.mod.ALL_ROOMS:
        room = sim.mod.ROOMS[key]
        assert sim.app.get_state(room.climate, attribute="temperature") == 7.0
    assert sim.log_lines("reason=no_selectable_rooms")


def test_no_selectable_rooms_and_no_quota_stops_the_pump():
    sim = _warm_morning()
    sim.app.set("input_number.dhw_min_run_hours", 0)  # no quota to fall back on
    sim.step(45)  # past min_pump_on
    assert sim.pump_on()
    _freeze_all_rooms(sim)

    assert sim.run_until(lambda: not sim.pump_on(), 5), "pump must not idle on closed valves"
    assert sim.log_lines("reason=no_selectable_rooms")
    assert sim.state() == "OFF"


def test_no_selectable_rooms_waits_for_min_pump_on_before_stopping():
    sim = _warm_morning()
    sim.app.set("input_number.dhw_min_run_hours", 0)
    sim.step(10)
    _freeze_all_rooms(sim)
    sim.step(5)

    assert sim.pump_on(), "compressor protection wins over the idle stop"
    assert sim.log_lines("no_selectable_rooms but waiting for min_pump_on")


def test_rooms_come_back_after_cooldown_without_a_pump_restart_penalty():
    sim = _warm_morning()
    sim.step(30)
    _freeze_all_rooms(sim, minutes=20)
    starts_before = sim.app.get_state("input_number.pump_starts_today")

    sim.step(30)

    assert sim.pump_on()
    assert sim.state() == "HEAT_GF"
    assert sim.app.get_state("input_number.pump_starts_today") == starts_before, \
        "a rotation gap must not cost a compressor start"


# ---------------------------------------------------------------------------
# Thermal scenarios
#
# The tests above drive fixed room temperatures, which isolates a decision but
# cannot show what the decision costs over a morning. ThermalSim closes the
# loop with a two-state slab/room model: the slab charges towards the flow
# temperature while the room's valve is open and the pump runs, discharges into
# the room afterwards (that residual is what makes a long uninterrupted run
# overshoot), and the room leaks to outdoor in proportion to ΔT.
#
# The constants are deliberately mild-house/UFH shaped, not a model of this
# specific building: ~1 °C/h rise on an open valve, ~0.4 °C/h drift at ΔT=30,
# slab time constant ~50 min. Absolute minutes from these runs mean little;
# the old-vs-new comparison on the same model is the point.
# ---------------------------------------------------------------------------
SLAB_CHARGE = 0.02      # slab → flow temp, per minute (τ ≈ 50 min)
SLAB_TO_ROOM = 0.0015   # slab → room coupling, per minute
ROOM_LOSS = 0.00022     # room → outdoor, per minute (≈0.4 °C/h at ΔT=30)


class ThermalSim(Sim):
    def __init__(self, t_out: float, **kw):
        super().__init__(**kw)
        self.t_out = t_out
        self.app.set(self.mod.WEATHER_ENTITY, "cloudy", temperature=t_out)
        self.slab = {k: 20.0 for k in self.mod.ALL_ROOMS}
        self.history: list[dict] = []

    @property
    def flow_temp(self) -> float:
        """Weather-compensated flow temperature: 40 °C at -15, 28 °C at +15."""
        return max(28.0, min(40.0, 34.0 - 0.4 * self.t_out))

    def setup_room(self, key: str, cur: float, user_sp: float, priority: int = 50):
        room = self.mod.ROOMS[key]
        self.app.set(room.climate, "heat", current_temperature=cur)
        self.app.set(room.user_sp, user_sp)
        self.app.set(room.priority, priority)
        self.slab[key] = cur

    def _valve_open(self, key: str) -> bool:
        room = self.mod.ROOMS[key]
        sp = self.app.get_state(room.climate, attribute="temperature")
        cur = self.app.get_state(room.climate, attribute="current_temperature")
        return sp is not None and cur is not None and float(sp) > float(cur) + 0.3

    def advance_physics(self):
        pump = self.pump_on()
        for key in self.mod.ALL_ROOMS:
            room = self.mod.ROOMS[key]
            cur = float(self.app.get_state(room.climate, attribute="current_temperature"))
            target = self.flow_temp if (pump and self._valve_open(key)) else cur
            self.slab[key] += (target - self.slab[key]) * SLAB_CHARGE
            cur += (self.slab[key] - cur) * SLAB_TO_ROOM
            cur += (self.t_out - cur) * ROOM_LOSS
            self.app.set(room.climate, "heat", current_temperature=round(cur, 3))

    def run(self, minutes: int):
        for _ in range(minutes):
            self.step(1)
            self.advance_physics()
            self.history.append(self.snapshot())
        return self

    def snapshot(self) -> dict:
        open_rooms = [k for k in self.mod.ALL_ROOMS
                      if self.app.get_state(self.mod.ROOMS[k].heating) == "on"]
        return {
            "t": self.app.now,
            "state": self.state(),
            "pump": self.pump_on(),
            "open": open_rooms,
            "temps": {k: float(self.app.get_state(self.mod.ROOMS[k].climate,
                                                  attribute="current_temperature"))
                      for k in self.mod.ALL_ROOMS},
        }

    # --- metrics ------------------------------------------------------------
    def idle_pump_minutes(self) -> int:
        """Pump running in a HEAT state with every valve parked: pure loss.

        DHW_QUOTA and the tick in which the stop script fires also show closed
        valves, but there the closed valves are the point.
        """
        return sum(1 for h in self.history
                   if h["pump"] and not h["open"] and h["state"].startswith("HEAT_"))

    def pump_minutes(self) -> int:
        return sum(1 for h in self.history if h["pump"])

    def pump_starts(self) -> int:
        return sum(1 for a, b in zip(self.history, self.history[1:])
                   if not a["pump"] and b["pump"])

    def valve_cycles(self, key: str) -> int:
        return sum(1 for a, b in zip(self.history, self.history[1:])
                   if key not in a["open"] and key in b["open"])

    def deficit_minutes(self, key: str, target: float) -> int:
        return sum(1 for h in self.history if h["temps"][key] < target)

    def peak(self, key: str) -> float:
        return max(h["temps"][key] for h in self.history)


def test_thermal_uncontended_morning_reaches_setpoint_without_idling_the_pump():
    """The logged scenario, with the rooms allowed to actually warm up."""
    sim = ThermalSim(t_out=12.2, start=dt.datetime(2026, 9, 18, 5, 0, 0))
    sim.setup_room("salon", 21.8, 23.5)
    sim.setup_room("lazienka_parter", 22.0, 23.5)
    for key in sim.mod.ALL_ROOMS:
        if key not in ("salon", "lazienka_parter"):
            sim.setup_room(key, 24.0, 21.0)
    sim.run(8 * 60)

    assert sim.idle_pump_minutes() == 0, "pump must never run on closed valves"
    # Both rooms finish the job instead of being cut at 120 min and left short.
    assert sim.history[-1]["temps"]["salon"] >= 23.5
    assert sim.history[-1]["temps"]["lazienka_parter"] >= 23.5
    # The 240 min solo cap still fires once on a job this long. With the DHW
    # quota already spent by then there is nothing else for the run to do, so
    # it costs one extra compressor start; the resume latch brings both rooms
    # straight back after the cooldown.
    assert sim.pump_starts() <= 2
    assert sim.valve_cycles("salon") <= 2
    assert sim.peak("salon") <= 24.0, sim.peak("salon")


def test_thermal_cold_day_contention_still_shares_the_pump_between_floors():
    sim = ThermalSim(t_out=-10.0, start=dt.datetime(2026, 9, 18, 6, 0, 0))
    for key in sim.mod.ALL_ROOMS:
        sim.setup_room(key, 24.0, 21.0)
    sim.setup_room("salon", 20.0, 21.5)          # GF, biggest deficit
    sim.setup_room("sypialnia", 20.4, 21.0)      # FF, smaller deficit
    sim.run(8 * 60)

    floors = {h["state"] for h in sim.history}
    assert "HEAT_GF" in floors and "HEAT_FF" in floors, "FF must not starve"
    assert sim.idle_pump_minutes() == 0
    assert sim.valve_cycles("salon") <= 6, "rotation must not thrash the TRV"


def test_outdoor_temp_is_read_once_per_tick_when_the_weather_entity_is_degraded():
    """Room selection asks for the outdoor temperature several times per tick;
    the fallback path calls a service, so it must be memoised."""
    sim = Sim(start=dt.datetime(2026, 9, 18, 6, 0, 0))
    sim.app.set(sim.mod.WEATHER_ENTITY, "unavailable", temperature=None)
    _demand(sim, "salon", 22.0)

    sim.step(10)

    assert len(sim.calls("weather/get_forecasts")) == 10
