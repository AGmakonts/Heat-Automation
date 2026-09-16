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
