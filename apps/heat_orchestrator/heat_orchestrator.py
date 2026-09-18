"""
Heat Orchestrator – AppDaemon App for Home Assistant
=====================================================
Controls underfloor heating with a heat pump, managing floor selection (GF/FF),
room-level valve control via thermostats, pump on/off logic, DHW quota,
and nightly off-window enforcement.

Spec version: 2026-02-10
"""

from __future__ import annotations

import hassapi as hass
import datetime

# ---------------------------------------------------------------------------
# Room configuration (single source of truth)
# ---------------------------------------------------------------------------
class Room:
    """Static config for a managed room.

    `climate` is the HA climate entity (LocalTuya, rename-prone -> explicit).
    Helper entity IDs are owned by this app and derived from `key`, so they
    can never drift out of sync with the climate entity.
    """

    __slots__ = ("key", "floor", "climate")

    def __init__(self, key: str, floor: str, climate: str):
        self.key = key
        self.floor = floor
        self.climate = climate

    @property
    def user_sp(self) -> str:
        return f"input_number.user_sp_{self.key}"

    @property
    def priority(self) -> str:
        return f"input_number.priority_{self.key}"

    @property
    def heating(self) -> str:
        return f"input_boolean.heating_{self.key}"

    @property
    def heating_minutes(self) -> str:
        return f"input_number.heating_minutes_{self.key}"


ROOMS: dict[str, Room] = {
    r.key: r
    for r in [
        Room("gabinet_ani", "GF", "climate.gabinet_ani"),
        Room("lazienka_parter", "GF", "climate.lazienka_parter"),
        Room("salon", "GF", "climate.salon"),
        Room("garaz", "GF", "climate.garaz"),
        Room("sypialnia", "FF", "climate.sypialnia"),
        Room("lazienka_pietro", "FF", "climate.lazienka_pietro"),
        Room("pokoj_narozny", "FF", "climate.pokoj_narozny"),
        Room("pokoj_z_garazem", "FF", "climate.pokoj_z_garazem"),
    ]
}

GF_ROOMS = [k for k, r in ROOMS.items() if r.floor == "GF"]
FF_ROOMS = [k for k, r in ROOMS.items() if r.floor == "FF"]
ALL_ROOMS = list(ROOMS)

# ---------------------------------------------------------------------------
# Pump control
# ---------------------------------------------------------------------------
# On/off are HA scripts (uruchom/wylacz) which toggle the Sonoff relay. The
# relay switch follows the command within a tick or so (production logs show
# it can still read "on" one tick after wylacz — see PUMP_STOP_SETTLE_MIN),
# so it is the authoritative "is the pump on" signal (PUMP_SWITCH). The Sonoff
# also meters power, used
# ONLY as a health cross-check: if the pump is commanded ON (switch on) but
# draws almost nothing for a while, it likely is not actually running.
# (Idle/circulation ~100 W; compressor running >1000 W; truly off/dead ~0 W.)
PUMP_START_SCRIPT = "script.uruchom_pompe"
PUMP_STOP_SCRIPT = "script.wylacz_pompe"
PUMP_SWITCH = "switch.zasilanie_pompy_sonoff_10017fadeb_1"
PUMP_POWER_SENSOR = "sensor.zasilanie_pompy_sonoff_10017fadeb_power"
PUMP_HEALTH_MIN_WATTS = 50.0   # below this while commanded ON = suspicious
PUMP_HEALTH_GRACE_MIN = 5.0    # ignore the first minutes after a start (spin-up)
PUMP_STOP_SETTLE_MIN = 3.0     # after wylacz the relay may still read "on" for a tick or two

WEATHER_ENTITY = "weather.forecast_home"

# FSM States
STATE_OFF_LOCKOUT = "OFF_LOCKOUT"
STATE_OFF = "OFF"
STATE_HEAT_GF = "HEAT_GF"
STATE_HEAT_FF = "HEAT_FF"
STATE_DHW_QUOTA = "DHW_QUOTA"

GUARD_RELEASE_DELAY = 2  # seconds


class HeatOrchestrator(hass.Hass):
    """Main heat orchestrator AppDaemon application."""

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------
    def initialize(self):
        self.log("=== HeatOrchestrator initializing ===")

        # Automation guard – prevents recording automation-driven setpoint
        # changes as user changes.
        self.automation_guard: dict[str, bool] = {r: False for r in ALL_ROOMS}

        # Set of rooms temporarily marked as "unmanaged" after errors
        self.unmanaged_rooms: dict[str, datetime.datetime] = {}

        # Last known outdoor temperature (fallback) and the tick it was read on
        self._last_outdoor_temp: float | None = None
        self._outdoor_temp_minute: datetime.datetime | None = None
        self._outdoor_temp_cached: float = 0.0

        # Pump health (power-meter cross-check) — diagnostic only, never used to
        # decide whether the pump is on. "OK" | "NO_FLOW" | "OFF" | "UNKNOWN".
        self._pump_health: str | None = None

        # Track last decision tick log to avoid spam
        self._last_logged_state: str | None = None
        self._log_every_n_ticks: int = 5
        self._tick_counter: int = 0

        # Track per-room cooldown expiry time
        self.room_cooldown_until: dict[str, datetime.datetime | None] = {r: None for r in ALL_ROOMS}

        # Rooms switched off by the orchestrator while they still wanted heat
        # (rotation cap, forced cooldown, room limit, floor switch). They keep
        # the "keep heating until satisfied" threshold when they come back, so
        # an interruption cannot strand them inside the hysteresis band.
        self.room_resume_pending: dict[str, bool] = {r: False for r in ALL_ROOMS}

        # --- Bootstrap user setpoints if empty ---
        self._bootstrap_user_setpoints()

        # --- Listeners: thermostat setpoint changes (user tracking) ---
        for room in ALL_ROOMS:
            entity = ROOMS[room].climate
            self.listen_state(
                self._on_thermostat_change,
                entity,
                attribute="temperature",
                room=room,
            )

        # --- Listener: weather changes ---
        self.listen_state(self._on_weather_change, WEATHER_ENTITY)

        # --- Main tick every 60 seconds ---
        self.run_every(self._tick, "now", 60)

        # --- Daily reset ---
        reset_time = self.get_state("input_datetime.day_reset_time")
        if reset_time:
            self.run_daily(self._daily_reset, reset_time)
        else:
            self.run_daily(self._daily_reset, "00:00:00")

        self.log("=== HeatOrchestrator ready ===")

    # -----------------------------------------------------------------------
    # Bootstrap
    # -----------------------------------------------------------------------
    def _bootstrap_user_setpoints(self):
        """On first run, seed user_sp helpers from current thermostat setpoints."""
        for room in ALL_ROOMS:
            sp_entity = ROOMS[room].user_sp
            current_val = self._get_number(sp_entity)
            if current_val is None or current_val < 5.0:
                climate_sp = self._get_climate_setpoint(room)
                if climate_sp is not None and 5.0 <= climate_sp <= 30.0:
                    self._set_number(sp_entity, climate_sp)
                    self.log(f"[BOOTSTRAP] {sp_entity} seeded with {climate_sp}")
                else:
                    self._set_number(sp_entity, 21.0)
                    self.log(f"[BOOTSTRAP] {sp_entity} fallback to 21.0")

    # -----------------------------------------------------------------------
    # Helpers – state reading
    # -----------------------------------------------------------------------
    def _get_number(self, entity: str) -> float | None:
        """Read an input_number or sensor as float, return None on failure."""
        val = self.get_state(entity)
        if val in (None, "unknown", "unavailable", ""):
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _set_number(self, entity: str, value: float):
        self.call_service(
            "input_number/set_value", entity_id=entity, value=round(value, 1)
        )

    def _get_climate_setpoint(self, room: str) -> float | None:
        entity = ROOMS[room].climate
        val = self.get_state(entity, attribute="temperature")
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _get_climate_current_temp(self, room: str) -> float | None:
        entity = ROOMS[room].climate
        val = self.get_state(entity, attribute="current_temperature")
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _pump_is_on(self) -> bool:
        """Authoritative pump state = the Sonoff relay (mains) switch.

        The uruchom/wylacz scripts toggle this switch, so it reflects the
        commanded state immediately. Power draw is deliberately NOT used here:
        the heat pump's compressor cycles on its own thermostat, so power dips
        to idle while the pump is still on. Power is only a health cross-check
        (see _check_pump_health).
        """
        return self.get_state(PUMP_SWITCH) == "on"

    def _pump_stopping(self) -> bool:
        """True while a stop command is settling: wylacz_pompe was called less
        than PUMP_STOP_SETTLE_MIN ago, no start was issued since, but the relay
        still reads "on". Without this the tick after a stop sees "pump on"
        and re-enters DHW_QUOTA/HEAT_* (resetting state_since) or re-fires the
        stop script and re-stamps last_pump_off.
        """
        if not self._pump_is_on():
            return False
        mins_off = self._minutes_since("input_datetime.last_pump_off")
        if mins_off is None or mins_off >= PUMP_STOP_SETTLE_MIN:
            return False
        mins_on = self._minutes_since("input_datetime.last_pump_on")
        return mins_on is None or mins_on > mins_off

    def _get_fsm_state(self) -> str:
        val = self.get_state("input_text.heat_state")
        if val in (STATE_OFF_LOCKOUT, STATE_OFF, STATE_HEAT_GF, STATE_HEAT_FF, STATE_DHW_QUOTA):
            return val
        return STATE_OFF

    def _set_fsm_state(self, state: str):
        self.call_service(
            "input_text/set_value", entity_id="input_text.heat_state", value=state
        )
        now_str = self.datetime().strftime("%Y-%m-%d %H:%M:%S")
        self.call_service(
            "input_datetime/set_datetime",
            entity_id="input_datetime.state_since",
            datetime=now_str,
        )

    def _get_state_since(self) -> datetime.datetime | None:
        val = self.get_state("input_datetime.state_since")
        if val in (None, "unknown", "unavailable", ""):
            return None
        try:
            return datetime.datetime.fromisoformat(val)
        except Exception:
            return None

    # -----------------------------------------------------------------------
    # Helpers – parameters
    # -----------------------------------------------------------------------
    def _param(self, entity: str, default: float) -> float:
        val = self._get_number(entity)
        return val if val is not None else default

    @property
    def room_off_setpoint(self) -> float:
        return self._param("input_number.room_off_setpoint", 7.0)

    @property
    def hyst_on(self) -> float:
        return self._param("input_number.heating_hyst_on", 0.3)

    # Offset added to user setpoint when commanding the thermostat.
    # Thermostat valves close when current temp is within ~0.5°C of their
    # target. To prevent valves closing prematurely (while the orchestrator
    # still considers the room as heating), we command the thermostat to
    # user_sp + THERMOSTAT_OVERSHOOT. The orchestrator alone decides when
    # the room is satisfied (based on the unmodified user_sp).
    THERMOSTAT_OVERSHOOT: float = 2.0

    @property
    def hyst_off(self) -> float:
        return self._param("input_number.heating_hyst_off", 0.2)

    @property
    def min_state_duration(self) -> float:
        return self._param("input_number.min_state_duration_min", 25.0)

    @property
    def min_pump_on(self) -> float:
        return self._param("input_number.min_pump_on_min", 40.0)

    @property
    def min_pump_off(self) -> float:
        return self._param("input_number.min_pump_off_min", 25.0)

    @property
    def dhw_min_run_hours(self) -> float:
        return self._param("input_number.dhw_min_run_hours", 3.5)

    @property
    def dhw_exclusive_max_run(self) -> float:
        """Max continuous DHW-only run [min]; 0 disables duty-cycling."""
        return self._param("input_number.dhw_exclusive_max_run_min", 45.0)

    @property
    def dhw_exclusive_pause(self) -> float:
        """Pause between DHW-only runs [min]; 0 disables duty-cycling."""
        return self._param("input_number.dhw_exclusive_pause_min", 90.0)

    def _dhw_duty_cycle_enabled(self) -> bool:
        """Duty-cycling of DHW-only runs is active only when both the max run
        and the pause are configured to non-zero values."""
        return self.dhw_exclusive_max_run > 0 and self.dhw_exclusive_pause > 0

    @property
    def bulk_mode_temp(self) -> float:
        return self._param("input_number.bulk_mode_temp", 5.0)

    @property
    def sequential_mode_temp(self) -> float:
        return self._param("input_number.sequential_mode_temp", -5.0)

    @property
    def max_rooms_limited(self) -> int:
        val = self._param("input_number.max_rooms_limited", 2.0)
        return max(1, int(val))

    @property
    def max_continuous_heating_min(self) -> float:
        """Max continuous heating per room [min] while the floor is contended."""
        return self._param("input_number.max_continuous_heating_min", 120.0)

    @property
    def max_continuous_heating_solo_min(self) -> float:
        """Max continuous heating per room [min] when nothing is waiting for the
        slot (no contention). Guards against slab overcharge only; 0 disables
        the limit entirely. See `_floor_is_contended`."""
        return self._param("input_number.max_continuous_heating_solo_min", 240.0)

    # -----------------------------------------------------------------------
    # OFF window
    # -----------------------------------------------------------------------
    def _in_off_window(self, now: datetime.datetime | None = None) -> bool:
        if now is None:
            now = self.datetime()

        start_str = self.get_state("input_datetime.off_window_start")
        end_str = self.get_state("input_datetime.off_window_end")

        try:
            start = datetime.datetime.strptime(start_str, "%H:%M:%S").time()
        except Exception:
            start = datetime.time(1, 0)
        try:
            end = datetime.datetime.strptime(end_str, "%H:%M:%S").time()
        except Exception:
            end = datetime.time(6, 0)

        current_time = now.time()

        if start <= end:
            return start <= current_time < end
        else:  # spans midnight
            return current_time >= start or current_time < end

    # -----------------------------------------------------------------------
    # Outdoor temperature
    # -----------------------------------------------------------------------
    def _get_outdoor_temp(self) -> float:
        """Outdoor temperature, memoised for the duration of one tick.

        Room selection asks for it several times per tick (room limit, both
        floors' contention check). The attribute read is cheap, but the
        fallback path calls weather.get_forecasts, which is not.

        Keyed on the wall-clock minute rather than the tick counter, so a
        caller outside `_tick` (a state listener, a scheduled callback) gets a
        value with a bounded age instead of whatever the last tick happened to
        read. The tick period is 60 s, so inside a tick the two are equivalent.
        """
        minute = self.datetime().replace(second=0, microsecond=0)
        if self._outdoor_temp_minute == minute:
            return self._outdoor_temp_cached
        value = self._read_outdoor_temp()
        self._outdoor_temp_minute = minute
        self._outdoor_temp_cached = value
        return value

    def _read_outdoor_temp(self) -> float:
        # Try attribute first
        temp = self.get_state(WEATHER_ENTITY, attribute="temperature")
        if temp is not None:
            try:
                t = float(temp)
                self._last_outdoor_temp = t
                return t
            except (ValueError, TypeError):
                pass

        # Fallback: call weather.get_forecasts
        try:
            resp = self.call_service(
                "weather/get_forecasts",
                entity_id=WEATHER_ENTITY,
                type="hourly",
                return_result=True,
            )
            if resp and WEATHER_ENTITY in resp:
                forecasts = resp[WEATHER_ENTITY].get("forecast", [])
                if forecasts:
                    t = float(forecasts[0]["temperature"])
                    self._last_outdoor_temp = t
                    return t
        except Exception as e:
            self.log(f"[WARN] weather.get_forecasts failed: {e}", level="WARNING")

        # Last known or neutral
        if self._last_outdoor_temp is not None:
            self.log("[WARN] Using last known outdoor temp", level="WARNING")
            return self._last_outdoor_temp

        self.log("[WARN] No outdoor temp available, using 0°C", level="WARNING")
        return 0.0

    # -----------------------------------------------------------------------
    # LERP-based room count calculation
    # -----------------------------------------------------------------------
    def _lerp_max_rooms(self, t_out: float) -> int:
        """Calculate max rooms to heat based on outdoor temperature using LERP."""
        t_min = self._param("input_number.lerp_temp_min", -10.0)
        t_max = self._param("input_number.lerp_temp_max", 10.0)
        r_min = max(1, int(self._param("input_number.lerp_rooms_min", 1.0)))
        r_max = max(1, int(self._param("input_number.lerp_rooms_max", 5.0)))

        # Ensure r_max >= r_min to avoid counterintuitive behavior
        if r_max < r_min:
            r_min, r_max = r_max, r_min

        if t_min >= t_max:
            return r_min  # safety: degenerate config

        if t_out <= t_min:
            return r_min
        if t_out >= t_max:
            return r_max

        # Linear interpolation
        frac = (t_out - t_min) / (t_max - t_min)
        result = r_min + frac * (r_max - r_min)
        return max(r_min, int(result))  # floor, not round — conservative

    # -----------------------------------------------------------------------
    # Demand model
    # -----------------------------------------------------------------------
    def _is_unmanaged(self, room: str) -> bool:
        """Room was marked unmanaged after failed commands; the mark expires
        after 15 minutes (and is cleared here when it does)."""
        marked = self.unmanaged_rooms.get(room)
        if marked is None:
            return False
        if self.datetime() - marked < datetime.timedelta(minutes=15):
            return True
        del self.unmanaged_rooms[room]
        return False

    def _need_heat(self, room: str) -> bool:
        """Room needs heating: Tcur < Tuser - hyst_on."""
        if self._is_unmanaged(room):
            return False

        t_cur = self._get_climate_current_temp(room)
        t_user = self._get_number(ROOMS[room].user_sp)
        if t_cur is None or t_user is None:
            return False
        return t_cur < (t_user - self.hyst_on)

    def _satisfied(self, room: str) -> bool:
        """Room is satisfied: Tcur >= Tuser + hyst_off."""
        t_cur = self._get_climate_current_temp(room)
        t_user = self._get_number(ROOMS[room].user_sp)
        if t_cur is None or t_user is None:
            return True
        return t_cur >= (t_user + self.hyst_off)

    def _need_heat_floor(self, floor: str) -> bool:
        rooms = GF_ROOMS if floor == "GF" else FF_ROOMS
        return any(self._has_demand(r) for r in rooms)

    def _has_demand(self, room: str) -> bool:
        """Hysteresis-aware demand check.

        For rooms already heating: keep heating until satisfied (offset threshold).
        For rooms not heating: only start if need_heat (onset threshold).
        This creates the proper two-threshold hysteresis band.

        A room that the orchestrator stopped *involuntarily* (rotation cap,
        forced cooldown, LERP room limit, floor switch) keeps the upper
        threshold as well — see `room_resume_pending`. Without that it would
        have to fall back below the onset threshold before it could finish the
        job it was interrupted in, which strands it inside the hysteresis band
        (a 0.5 °C dead zone at the defaults) for as long as the slab takes to
        drift back down.
        """
        # Respect unmanaged room timeout
        if self._is_unmanaged(room):
            return False

        if self._is_room_heating(room) or self.room_resume_pending.get(room):
            # Heating, or interrupted mid-job → keep going until satisfied
            if self._satisfied(room):
                # Latch releases on satisfaction, never on the onset threshold.
                # This is the one mutation in an otherwise read-only predicate;
                # it is idempotent, so every caller can afford it.
                self.room_resume_pending[room] = False
                return False
            return True
        else:
            # Not heating → only start at onset threshold
            return self._need_heat(room)

    def _latch_if_interrupted(self, room: str):
        """Latch a room that is being switched off mid-job.

        Both conditions are load-bearing and evaluated *before* the room is
        switched off, while its heating flag still reads "on":

        * `_is_room_heating` — the room was actually being heated. A room that
          merely crossed the onset threshold and never got a slot was not
          interrupted, and latching it would hold it in demand at the upper
          threshold from its first dip onwards. That would keep the other
          floor's `_need_heat_floor` true and so keep this floor permanently
          "contended", re-imposing the rotation cap this app is trying to lift.
        * `_has_demand` — it stopped short. A room that reached its setpoint
          was satisfied, not interrupted.
        """
        if self._is_room_heating(room) and self._has_demand(room):
            self.room_resume_pending[room] = True

    def _clear_resume_pending(self, room: str | None = None):
        if room is None:
            for r in ALL_ROOMS:
                self.room_resume_pending[r] = False
        else:
            self.room_resume_pending[room] = False

    # -----------------------------------------------------------------------
    # Scoring
    # -----------------------------------------------------------------------
    def _room_score(self, room: str) -> float:
        t_cur = self._get_climate_current_temp(room)
        t_user = self._get_number(ROOMS[room].user_sp)
        priority = self._param(ROOMS[room].priority, 50.0)
        if t_cur is None or t_user is None:
            return 0.0
        deficit = max(0.0, t_user - t_cur)
        return deficit * priority

    def _floor_score(self, floor: str) -> float:
        rooms = GF_ROOMS if floor == "GF" else FF_ROOMS
        scores = [self._room_score(r) for r in rooms if self._has_demand(r)]
        return max(scores) if scores else 0.0

    # -----------------------------------------------------------------------
    # Room enable / disable
    # -----------------------------------------------------------------------
    def _is_room_heating(self, room: str) -> bool:
        """Check if a room is currently being heated (heating sensor is on)."""
        entity = ROOMS[room].heating
        try:
            state = self.get_state(entity)
            return state == "on"
        except Exception:
            return False

    def _get_heating_minutes(self, room: str) -> float:
        """Read accumulated heating minutes for a room from its HA helper."""
        val = self._get_number(ROOMS[room].heating_minutes)
        if val is None:
            self.log(f"[WARN] heating_minutes helper missing for {room}", level="WARNING")
            return 0.0
        return val

    def _set_heating_minutes(self, room: str, value: float):
        """Set accumulated heating minutes for a room in its HA helper."""
        self._set_number(ROOMS[room].heating_minutes, max(0.0, value))

    def _reset_heating_minutes(self, room: str):
        """Reset accumulated heating minutes for a room to zero."""
        current = self._get_number(ROOMS[room].heating_minutes)
        if current is not None and current == 0:
            return  # already zero — skip the service call (called every tick)
        self._set_heating_minutes(room, 0)

    def _enable_room(self, room: str):
        t_user = self._get_number(ROOMS[room].user_sp)
        if t_user is None or t_user < 5.0 or t_user > 30.0:
            climate_sp = self._get_climate_setpoint(room)
            if climate_sp is not None and 15.0 <= climate_sp <= 30.0:
                t_user = climate_sp
            else:
                t_user = 21.0

        # Command thermostat to user_sp + overshoot so the valve stays open
        # past the thermostat's internal ~0.5°C deadband. The orchestrator
        # disables the room when current temp reaches the real user_sp.
        target_sp = min(30.0, t_user + self.THERMOSTAT_OVERSHOOT)

        entity = ROOMS[room].climate
        current_sp = self._get_climate_setpoint(room)
        if current_sp is not None and abs(current_sp - target_sp) < 0.05:
            self._set_heating_sensor(room, True)
            return  # already correct

        self.automation_guard[room] = True
        try:
            self.call_service(
                "climate/set_temperature", entity_id=entity, temperature=target_sp
            )
            self.log(f"[ROOM] enable {room} → {target_sp}°C (user_sp={t_user}°C)")
            self._set_heating_sensor(room, True)
        except Exception as e:
            self.log(f"[ERROR] enable_room {room}: {e}", level="ERROR")
            # Retry once
            try:
                self.call_service(
                    "climate/set_temperature", entity_id=entity, temperature=target_sp
                )
                self._set_heating_sensor(room, True)
            except Exception as e2:
                self.log(f"[ERROR] enable_room {room} retry failed: {e2}", level="ERROR")
                self.unmanaged_rooms[room] = self.datetime()

        self.run_in(self._release_guard, GUARD_RELEASE_DELAY, room=room)

    def _disable_room(self, room: str):
        entity = ROOMS[room].climate
        off_sp = self.room_off_setpoint
        current_sp = self._get_climate_setpoint(room)
        if current_sp is not None and abs(current_sp - off_sp) < 0.05:
            self._set_heating_sensor(room, False)
            return  # already at off setpoint
        if current_sp is None:
            # TRV unavailable (no setpoint attribute). This runs every tick in
            # steady OFF, so don't hammer an offline device; it is re-parked
            # on the first tick after it comes back.
            self._set_heating_sensor(room, False)
            return
        if self._is_unmanaged(room):
            return  # recent command failures; retried after the 15 min timeout

        self.automation_guard[room] = True
        try:
            self.call_service(
                "climate/set_temperature", entity_id=entity, temperature=off_sp
            )
            self.log(f"[ROOM] disable {room} → {off_sp}°C")
            self._set_heating_sensor(room, False)
        except Exception as e:
            self.log(f"[ERROR] disable_room {room}: {e}", level="ERROR")
            try:
                self.call_service(
                    "climate/set_temperature", entity_id=entity, temperature=off_sp
                )
                self._set_heating_sensor(room, False)
            except Exception as e2:
                self.log(f"[ERROR] disable_room {room} retry failed: {e2}", level="ERROR")
                self.unmanaged_rooms[room] = self.datetime()

        self.run_in(self._release_guard, GUARD_RELEASE_DELAY, room=room)

    def _set_heating_sensor(self, room: str, heating: bool):
        """Update the per-room heating status input_boolean."""
        entity = ROOMS[room].heating
        try:
            current = self.get_state(entity)
            target = "on" if heating else "off"
            if current == target:
                return  # already in correct state
            service = "input_boolean/turn_on" if heating else "input_boolean/turn_off"
            self.call_service(service, entity_id=entity)
        except Exception as e:
            self.log(f"[WARN] heating sensor {entity}: {e}", level="WARNING")

    def _release_guard(self, **kwargs):
        room = kwargs.get("room")
        if room:
            self.automation_guard[room] = False

    # -----------------------------------------------------------------------
    # User setpoint listener
    # -----------------------------------------------------------------------
    def _on_thermostat_change(self, entity, attribute, old, new, **kwargs):
        room = kwargs.get("room")
        if room is None:
            return

        if self.automation_guard.get(room, False):
            return  # Automation-driven change, ignore

        if new is None:
            return

        # Ignore the unavailable→available recovery edge. When a TRV drops
        # offline its `temperature` attribute disappears (callback fires with
        # new=None, handled above). When it reconnects, AppDaemon delivers
        # old=None with the cached/parked setpoint as `new`. That is not a
        # user action and must not be recorded as one — otherwise a room
        # parked at room_off_setpoint (7.0°C) silently overwrites user_sp.
        if old in (None, "unknown", "unavailable", ""):
            return

        try:
            new_val = float(new)
        except (ValueError, TypeError):
            return

        if not (5.0 <= new_val <= 30.0):
            return

        # If the room is currently being heated by the orchestrator, the
        # thermostat is being held at user_sp + overshoot. A manual change
        # from the user expresses their desired *room target*, so we recover
        # the underlying user_sp by subtracting the overshoot. When the room
        # is not being heated (thermostat parked at room_off_setpoint), the
        # user's value is taken as-is.
        overshoot = self.THERMOSTAT_OVERSHOOT
        if self._is_room_heating(room) and new_val >= (self.room_off_setpoint + overshoot):
            stored_val = max(5.0, new_val - overshoot)
        else:
            stored_val = new_val

        sp_entity = ROOMS[room].user_sp
        current_user_sp = self._get_number(sp_entity)

        if current_user_sp is not None and abs(current_user_sp - stored_val) < 0.05:
            return  # No change

        self._set_number(sp_entity, stored_val)
        self.log(
            f"[USER] {room} setpoint changed → user_sp={stored_val}°C "
            f"(thermostat shown: {new_val}°C)"
        )

        # Log the resulting demand evaluation right away, so a "nothing
        # happened" outcome is explainable from the log alone (hysteresis
        # vs missing temperature reading).
        t_cur = self._get_climate_current_temp(room)
        if t_cur is None:
            self.log(
                f"[USER] {room} current_temperature unavailable — "
                f"demand cannot be evaluated",
                level="WARNING",
            )
        else:
            threshold = stored_val - self.hyst_on
            verdict = "demand" if t_cur < threshold else "no demand (hysteresis)"
            self.log(
                f"[USER] {room} demand check: t_cur={t_cur}°C "
                f"threshold={threshold:.1f}°C → {verdict}"
            )

    def _on_weather_change(self, entity, attribute, old, new, **kwargs):
        pass  # Tick handles weather; this is placeholder for potential future use

    # -----------------------------------------------------------------------
    # Pump control
    # -----------------------------------------------------------------------
    def _pump_on(self):
        if self._pump_is_on():
            return  # switch already on
        self.call_service("script/turn_on", entity_id=PUMP_START_SCRIPT)
        now_str = self.datetime().strftime("%Y-%m-%d %H:%M:%S")
        self.call_service(
            "input_datetime/set_datetime",
            entity_id="input_datetime.last_pump_on",
            datetime=now_str,
        )
        # Increment starts
        starts = self._get_number("input_number.pump_starts_today") or 0
        self._set_number("input_number.pump_starts_today", starts + 1)
        self.log("[PUMP] ON (script.uruchom_pompe)")

    def _pump_off(self):
        if not self._pump_is_on():
            return  # switch already off
        if self._pump_stopping():
            return  # stop already issued; relay is still settling
        self.call_service("script/turn_on", entity_id=PUMP_STOP_SCRIPT)
        now_str = self.datetime().strftime("%Y-%m-%d %H:%M:%S")
        self.call_service(
            "input_datetime/set_datetime",
            entity_id="input_datetime.last_pump_off",
            datetime=now_str,
        )
        self.log("[PUMP] OFF (graceful, script.wylacz_pompe)")

    def _check_pump_health(self):
        """Cross-check commanded pump state against measured power draw.

        Diagnostic only — does NOT influence control. Flags the case where the
        orchestrator commanded the pump ON (switch on) but it draws almost
        nothing (so it likely is not actually running). The first few minutes
        after a start are ignored to allow relay + pump spin-up.
        """
        if not self._pump_is_on():
            self._set_pump_health("OFF")
            return
        mins_on = self._minutes_since("input_datetime.last_pump_on")
        if mins_on is not None and mins_on < PUMP_HEALTH_GRACE_MIN:
            return  # within spin-up grace; don't judge yet
        power = self._get_number(PUMP_POWER_SENSOR)
        if power is None:
            self._set_pump_health("UNKNOWN")
        elif power < PUMP_HEALTH_MIN_WATTS:
            self._set_pump_health("NO_FLOW", power=power)
        else:
            self._set_pump_health("OK")

    def _set_pump_health(self, status: str, power: float | None = None):
        if status != self._pump_health:
            if status == "NO_FLOW":
                self.log(
                    f"[HEALTH] pump commanded ON but power={power:.0f}W "
                    f"(<{PUMP_HEALTH_MIN_WATTS:.0f}W) — pump may not be running",
                    level="WARNING",
                )
            self._pump_health = status
        entity = "input_text.pump_health"
        if self.entity_exists(entity) and self.get_state(entity) != status:
            self.call_service("input_text/set_value", entity_id=entity, value=status)

    def _minutes_since(self, dt_entity: str) -> float | None:
        val = self.get_state(dt_entity)
        if val in (None, "unknown", "unavailable", ""):
            return None
        try:
            dt = datetime.datetime.fromisoformat(val)
            delta = self.datetime() - dt
            return delta.total_seconds() / 60.0
        except Exception:
            return None

    # -----------------------------------------------------------------------
    # Quota
    # -----------------------------------------------------------------------
    def _remaining_quota(self) -> float:
        quota_min = self.dhw_min_run_hours * 60.0
        on_today = self._get_number("input_number.pump_on_minutes_today") or 0.0
        return max(0.0, quota_min - on_today)

    # -----------------------------------------------------------------------
    # Room selection per mode (LERP-based)
    # -----------------------------------------------------------------------
    def _is_room_in_cooldown(self, room: str, now: datetime.datetime) -> bool:
        """Check if a room is currently in cooldown."""
        cooldown_until = self.room_cooldown_until.get(room)
        if cooldown_until and now < cooldown_until:
            return True
        elif cooldown_until:
            # Cooldown expired, clear it
            self.room_cooldown_until[room] = None
        return False

    def _max_rooms_for_floor(self, floor: str) -> int:
        """LERP room limit, clamped to the number of rooms on the floor."""
        rooms = GF_ROOMS if floor == "GF" else FF_ROOMS
        return min(self._lerp_max_rooms(self._get_outdoor_temp()), len(rooms))

    def _floor_is_contended(self, floor: str, demand_count: int) -> bool:
        """Is anything actually waiting for a heating slot?

        The continuous-heating cap exists to rotate the slot between rooms
        that cannot all be served at once. That is the case when either

        * more rooms on this floor want heat than the LERP limit allows, or
        * the other floor wants heat — the cap is what eventually empties this
          floor's candidate list and lets the floor switch happen, since the
          score comparison alone never flips while this floor's deficit leads.

        With neither true, kicking a room out of its slot hands that slot to
        nobody: the room just cools back down and gets re-enabled later.
        """
        other = "FF" if floor == "GF" else "GF"
        if self._need_heat_floor(other):
            return True
        return demand_count > self._max_rooms_for_floor(floor)

    def _effective_max_continuous(
        self, floor: str, demand_count: int, contended: bool | None = None
    ) -> float:
        """Continuous-heating cap that applies right now. 0 = no limit.

        The two helpers have overlapping ranges, so an uncontended floor is
        never held to a tighter cap than a contended one would be: that would
        invert the premise of the whole rule. 0 stays special-cased, since it
        means "no limit", not "zero minutes".
        """
        if contended is None:
            contended = self._floor_is_contended(floor, demand_count)
        if contended:
            return self.max_continuous_heating_min
        solo = self.max_continuous_heating_solo_min
        return solo if solo == 0 else max(solo, self.max_continuous_heating_min)

    def _build_candidates(self, floor: str, apply_side_effects: bool = True) -> list[str]:
        """Build list of rooms with demand that are eligible (not in cooldown).

        Args:
            floor: "GF" or "FF"
            apply_side_effects: If True, applies cooldown when rooms exceed max time.
                               If False, skips that. Note this does not make the
                               call pure: evaluating demand still releases the
                               resume latch of any satisfied room.

        Returns:
            List of eligible rooms (not sorted, not LERP-limited).
        """
        rooms = GF_ROOMS if floor == "GF" else FF_ROOMS
        now = self.datetime()

        demand_rooms = [r for r in rooms if self._has_demand(r)]
        contended = self._floor_is_contended(floor, len(demand_rooms))
        max_continuous = self._effective_max_continuous(floor, len(demand_rooms), contended)

        candidates = []
        for room in demand_rooms:
            # Check if room has exceeded the continuous heating time that
            # applies under the current contention (0 = uncapped).
            heating_min = self._get_heating_minutes(room)
            if max_continuous > 0 and heating_min >= max_continuous:
                # Apply cooldown/reset/logging only when side effects are enabled
                if apply_side_effects and not self._is_room_in_cooldown(room, now):
                    cooldown_duration_min = self.min_state_duration
                    self.room_cooldown_until[room] = now + datetime.timedelta(minutes=cooldown_duration_min)
                    self._reset_heating_minutes(room)
                    self.log(
                        f"[ROOM] {room} forced cooldown after {heating_min:.0f}min continuous heating "
                        f"(cap={max_continuous:.0f}min, contended={contended})"
                    )
                # Always exclude rooms that exceeded max heating time
                continue

            # Check if room is in cooldown
            if self._is_room_in_cooldown(room, now):
                continue

            # Room is eligible
            candidates.append(room)

        return candidates

    def _has_selectable_rooms(self, floor: str) -> bool:
        """Check if a floor has any rooms with demand that are NOT in cooldown.

        Does not trigger cooldown enforcement or logging; used for
        floor-switching decisions. Not entirely side-effect free: evaluating
        demand releases the resume latch of any room that has since reached its
        setpoint (`_has_demand`). That release is idempotent and carries no
        service call, so it is safe on this path — but `apply_side_effects`
        governs only the cooldown branch, not every mutation below it.
        """
        return bool(self._build_candidates(floor, apply_side_effects=False))

    def _select_rooms(self, floor: str) -> list[str]:
        """Select rooms to heat on the given floor.

        Returns sorted list of rooms, limited by LERP-based outdoor temperature calculation.
        """
        candidates = self._build_candidates(floor)

        if not candidates:
            return []

        # Sort by priority desc, then deficit desc
        def sort_key(r):
            prio = self._param(ROOMS[r].priority, 50.0)
            t_cur = self._get_climate_current_temp(r) or 0.0
            t_user = self._get_number(ROOMS[r].user_sp) or 21.0
            deficit = max(0.0, t_user - t_cur)
            return (-prio, -deficit)

        candidates.sort(key=sort_key)

        # LERP room limit, clamped to the floor size and the candidate count
        max_rooms = min(self._max_rooms_for_floor(floor), len(candidates))

        return candidates[:max_rooms]

    # -----------------------------------------------------------------------
    # Daily reset
    # -----------------------------------------------------------------------
    def _daily_reset(self, **kwargs):
        self._set_number("input_number.pump_on_minutes_today", 0)
        self._set_number("input_number.pump_starts_today", 0)

        # Clear all cooldown states, resume latches and heating minute counters
        for room in ALL_ROOMS:
            self.room_cooldown_until[room] = None
            self._reset_heating_minutes(room)
        self._clear_resume_pending()

        self.log("[RESET] Daily counters zeroed, cooldown states and heating minutes cleared")

    # -----------------------------------------------------------------------
    # Main tick
    # -----------------------------------------------------------------------
    def _tick(self, **kwargs):
        now = self.datetime()
        self._tick_counter += 1
        current_state = self._get_fsm_state()

        # --- Pump run-time accounting ---
        if self._pump_is_on():
            on_min = self._get_number("input_number.pump_on_minutes_today") or 0.0
            self._set_number("input_number.pump_on_minutes_today", on_min + 1)

        # --- Pump health cross-check (power vs commanded state) ---
        self._check_pump_health()

        # --- Per-room heating minutes accounting ---
        for room in ALL_ROOMS:
            if self._is_room_heating(room):
                mins = self._get_heating_minutes(room)
                self._set_heating_minutes(room, mins + 1)

        # --- 1. OFF window check ---
        if self._in_off_window(now):
            if self._pump_is_on():
                mins_on = self._minutes_since("input_datetime.last_pump_on")
                if mins_on is not None and mins_on >= self.min_pump_on:
                    self._pump_off()
                    self._disable_all_rooms()
                    if current_state != STATE_OFF_LOCKOUT:
                        self._set_fsm_state(STATE_OFF_LOCKOUT)
                        self.log(f"[DECISION] state=OFF_LOCKOUT reason=off_window pump_off")
                else:
                    # Wait for min_pump_on to elapse
                    mins_on_str = f"{mins_on:.0f}" if mins_on is not None else "unknown"
                    self.log(
                        f"[DECISION] state={current_state} OFF_WINDOW but min_pump_on not met "
                        f"({mins_on_str}/{self.min_pump_on:.0f} min)"
                    )
            else:
                if current_state != STATE_OFF_LOCKOUT:
                    self._set_fsm_state(STATE_OFF_LOCKOUT)
                    self.log(f"[DECISION] state=OFF_LOCKOUT reason=off_window")
                # Idempotent re-park: a manual setpoint change while parked is
                # recorded in user_sp by the listener, but must not linger on
                # the TRV (open valve, misleading display) until the next
                # state transition.
                self._disable_all_rooms()
            # The off window is hours long. A resume latch set just before it
            # would be stale by morning and would restart the pump for a room
            # sitting inside the hysteresis band, so the night clears it and
            # rooms re-qualify on the normal onset threshold.
            self._clear_resume_pending()
            return

        # --- 2. Compute demand and quota ---
        demand_gf = self._need_heat_floor("GF")
        demand_ff = self._need_heat_floor("FF")
        remaining_quota = self._remaining_quota()
        has_demand = demand_gf or demand_ff

        score_gf = self._floor_score("GF") if demand_gf else 0.0
        score_ff = self._floor_score("FF") if demand_ff else 0.0

        t_out = self._get_outdoor_temp()

        # --- 3. If pump is OFF ---
        if not self._pump_is_on():
            # Check min_pump_off cooldown
            mins_off = self._minutes_since("input_datetime.last_pump_off")
            cooldown_ok = mins_off is None or mins_off >= self.min_pump_off

            # DHW-only starts additionally honor the exclusive pause, so the
            # daily quota gets spread across the day instead of running as one
            # continuous block. Room-demand starts are never delayed by this.
            dhw_pause_ok = True
            if self._dhw_duty_cycle_enabled():
                required_pause = max(self.min_pump_off, self.dhw_exclusive_pause)
                dhw_pause_ok = mins_off is None or mins_off >= required_pause

            if has_demand and cooldown_ok:
                # Pick floor
                floor = "GF" if score_gf >= score_ff else "FF"
                new_state = STATE_HEAT_GF if floor == "GF" else STATE_HEAT_FF
                self._apply_floor(floor)
                self._pump_on()
                self._set_fsm_state(new_state)
                self.log(
                    f"[DECISION] state={new_state} reason=demand "
                    f"floor={floor} GF_score={score_gf:.1f} FF_score={score_ff:.1f} "
                    f"Tout={t_out:.1f} quota_remaining={remaining_quota:.0f}"
                )
            elif remaining_quota > 0 and cooldown_ok and dhw_pause_ok:
                # DHW quota mode
                self._disable_all_rooms()
                self._pump_on()
                self._set_fsm_state(STATE_DHW_QUOTA)
                self.log(
                    f"[DECISION] state=DHW_QUOTA reason=quota "
                    f"quota_remaining={remaining_quota:.0f}"
                )
            else:
                if current_state != STATE_OFF:
                    self._set_fsm_state(STATE_OFF)
                # Idempotent re-park (see OFF_LOCKOUT): keeps TRVs at the off
                # setpoint while the state is steady OFF, so a manual setpoint
                # change that produced no demand does not stay on the TRV.
                self._disable_all_rooms()
                if self._tick_counter % self._log_every_n_ticks == 0:
                    reason = "no_demand_no_quota"
                    if not cooldown_ok:
                        reason = f"pump_cooldown ({mins_off:.0f}/{self.min_pump_off:.0f})"
                    elif remaining_quota > 0 and not dhw_pause_ok:
                        required_pause = max(self.min_pump_off, self.dhw_exclusive_pause)
                        reason = f"dhw_exclusive_pause ({mins_off:.0f}/{required_pause:.0f})"
                    self.log(
                        f"[DECISION] state=OFF reason={reason} "
                        f"Tout={t_out:.1f} quota_remaining={remaining_quota:.0f}"
                    )
            return

        # --- 4. Pump is ON ---
        if self._pump_stopping():
            # wylacz_pompe was just called and the relay has not opened yet.
            # Re-entering DHW_QUOTA/HEAT_* here would reset state_since and
            # flap the FSM for one tick; just wait for the switch to read off.
            self.log(f"[PUMP] stop pending (relay still on) state={current_state}")
            self._update_diagnostics()
            return

        if has_demand:
            # Determine active floor from current state
            if current_state == STATE_HEAT_GF:
                active_floor = "GF"
            elif current_state == STATE_HEAT_FF:
                active_floor = "FF"
            else:
                # Was in DHW_QUOTA or other state, pick best floor
                active_floor = "GF" if score_gf >= score_ff else "FF"

            # Check if we should switch floors
            state_since = self._get_state_since()
            min_dur_ok = True
            if state_since is not None:
                elapsed = (now - state_since).total_seconds() / 60.0
                min_dur_ok = elapsed >= self.min_state_duration

            if min_dur_ok:
                # Reconsider floor based on scores
                if active_floor == "GF" and score_ff > score_gf and demand_ff:
                    active_floor = "FF"
                    self.log(f"[DECISION] switching floor GF→FF (FF_score={score_ff:.1f} > GF_score={score_gf:.1f})")
                elif active_floor == "FF" and score_gf > score_ff and demand_gf:
                    active_floor = "GF"
                    self.log(f"[DECISION] switching floor FF→GF (GF_score={score_gf:.1f} > FF_score={score_ff:.1f})")

                # Switch floor if there are no selectable rooms on the active floor
                # but the other floor has selectable rooms and demand
                if not self._has_selectable_rooms(active_floor):
                    other_floor = "FF" if active_floor == "GF" else "GF"
                    active_demand = demand_gf if active_floor == "GF" else demand_ff
                    other_demand = demand_ff if other_floor == "FF" else demand_gf
                    if other_demand and self._has_selectable_rooms(other_floor):
                        self.log(
                            f"[DECISION] switching floor {active_floor}→{other_floor} "
                            f"reason=no_selectable_rooms on {active_floor} (active_demand={active_demand})"
                        )
                        active_floor = other_floor

            new_state = STATE_HEAT_GF if active_floor == "GF" else STATE_HEAT_FF
            selected = self._apply_floor(active_floor)

            if not selected:
                # Demand exists but every candidate is in cooldown, on both
                # floors: the pump would otherwise keep running against closed
                # valves (all TRVs parked at room_off_setpoint) for the whole
                # cooldown, producing nothing. Put the run to work on DHW if
                # there is quota left, otherwise stop it.
                #
                # This is a second entry into DHW_QUOTA, and unlike the one
                # below it runs with has_demand True, so dhw_exclusive_max_run
                # (which lives in the `elif remaining_quota > 0` branch) does
                # not bound it. It does not need to: the cooldown that emptied
                # both floors is min_state_duration long, so the run returns to
                # HEAT_* as soon as the rooms come back.
                other_floor = "FF" if active_floor == "GF" else "GF"
                if not self._has_selectable_rooms(other_floor):
                    if remaining_quota > 0:
                        if current_state != STATE_DHW_QUOTA:
                            self._set_fsm_state(STATE_DHW_QUOTA)
                            self.log(
                                f"[DECISION] state=DHW_QUOTA reason=no_selectable_rooms "
                                f"floor={active_floor} quota_remaining={remaining_quota:.0f}"
                            )
                        self._update_diagnostics()
                        return
                    mins_on = self._minutes_since("input_datetime.last_pump_on")
                    if mins_on is not None and mins_on >= self.min_pump_on:
                        self._pump_off()
                        self._set_fsm_state(STATE_OFF)
                        self.log(
                            f"[DECISION] state=OFF reason=no_selectable_rooms "
                            f"floor={active_floor} quota_remaining={remaining_quota:.0f} pump_off"
                        )
                        self._update_diagnostics()
                        return
                    if self._tick_counter % self._log_every_n_ticks == 0:
                        mins_on_str = f"{mins_on:.0f}" if mins_on is not None else "unknown"
                        self.log(
                            f"[DECISION] state={current_state} no_selectable_rooms "
                            f"but waiting for min_pump_on "
                            f"({mins_on_str}/{self.min_pump_on:.0f} min)"
                        )

            if current_state != new_state:
                self._set_fsm_state(new_state)

            if self._tick_counter % self._log_every_n_ticks == 0:
                self.log(
                    f"[DECISION] state={new_state} floor={active_floor} "
                    f"rooms={selected} Tout={t_out:.1f} "
                    f"quota_remaining={remaining_quota:.0f}"
                )

        elif remaining_quota > 0:
            # No demand but quota remaining
            self._disable_all_rooms()
            if current_state != STATE_DHW_QUOTA:
                self._set_fsm_state(STATE_DHW_QUOTA)
                self.log(
                    f"[DECISION] state=DHW_QUOTA reason=quota "
                    f"quota_remaining={remaining_quota:.0f}"
                )
            elif self._dhw_duty_cycle_enabled():
                # DHW quota is the only reason the pump is running. Limit the
                # continuous run so the quota gets spread across the day;
                # the pause before the next run is enforced at pump start.
                # state_since is set on every entry into DHW_QUOTA, so it
                # measures exclusive-run time only.
                state_since = self._get_state_since()
                if state_since is not None:
                    elapsed = (now - state_since).total_seconds() / 60.0
                    if elapsed >= self.dhw_exclusive_max_run:
                        mins_on = self._minutes_since("input_datetime.last_pump_on")
                        if mins_on is not None and mins_on >= self.min_pump_on:
                            self._pump_off()
                            self._set_fsm_state(STATE_OFF)
                            self.log(
                                f"[DECISION] state=OFF reason=dhw_exclusive_max_run "
                                f"({elapsed:.0f}/{self.dhw_exclusive_max_run:.0f} min) "
                                f"pause={max(self.min_pump_off, self.dhw_exclusive_pause):.0f} min "
                                f"quota_remaining={remaining_quota:.0f}"
                            )
                        else:
                            # Compressor protection wins: effective max run is
                            # max(dhw_exclusive_max_run, min_pump_on).
                            if self._tick_counter % self._log_every_n_ticks == 0:
                                mins_on_str = f"{mins_on:.0f}" if mins_on is not None else "unknown"
                                self.log(
                                    f"[DECISION] state=DHW_QUOTA exclusive max run reached "
                                    f"but waiting for min_pump_on "
                                    f"({mins_on_str}/{self.min_pump_on:.0f} min)"
                                )

        else:
            # No demand, no quota → pump off
            mins_on = self._minutes_since("input_datetime.last_pump_on")
            if mins_on is not None and mins_on >= self.min_pump_on:
                self._pump_off()
                self._disable_all_rooms()
                self._set_fsm_state(STATE_OFF)
                self.log("[DECISION] state=OFF reason=no_demand_no_quota pump_off")
            else:
                if self._tick_counter % self._log_every_n_ticks == 0:
                    mins_on_str = f"{mins_on:.0f}" if mins_on is not None else "unknown"
                    self.log(
                        f"[DECISION] waiting for min_pump_on "
                        f"({mins_on_str}/{self.min_pump_on:.0f} min) before OFF"
                    )

        # --- Update diagnostic helpers ---
        self._update_diagnostics()

    # -----------------------------------------------------------------------
    # Apply floor selection (enable selected rooms, disable rest)
    # -----------------------------------------------------------------------
    def _apply_floor(self, floor: str) -> list[str]:
        """Drive the TRVs for the given floor. Returns the selected rooms."""
        active_rooms = GF_ROOMS if floor == "GF" else FF_ROOMS
        inactive_rooms = FF_ROOMS if floor == "GF" else GF_ROOMS

        selected = self._select_rooms(floor)

        for room in active_rooms:
            if room in selected:
                self._enable_room(room)
                self._clear_resume_pending(room)
            else:
                self._latch_if_interrupted(room)
                self._disable_room(room)

        for room in inactive_rooms:
            self._latch_if_interrupted(room)
            self._disable_room(room)
            self._reset_heating_minutes(room)

        return selected

    def _disable_all_rooms(self):
        for room in ALL_ROOMS:
            self._latch_if_interrupted(room)
            self._disable_room(room)
            self._reset_heating_minutes(room)

    # -----------------------------------------------------------------------
    # Diagnostics
    # -----------------------------------------------------------------------
    def _update_diagnostics(self):
        """Update optional diagnostic entities."""
        try:
            state = self._get_fsm_state()
            active_floor = "none"
            if state == STATE_HEAT_GF:
                active_floor = "GF"
            elif state == STATE_HEAT_FF:
                active_floor = "FF"

            # Active floor
            floor_entity = "input_text.active_floor"
            if self.entity_exists(floor_entity):
                self.call_service(
                    "input_text/set_value",
                    entity_id=floor_entity,
                    value=active_floor,
                )

            # Active rooms
            rooms_entity = "input_text.active_rooms"
            if self.entity_exists(rooms_entity):
                if active_floor in ("GF", "FF"):
                    selected = self._select_rooms(active_floor)
                    rooms_str = ",".join(selected)
                else:
                    rooms_str = ""
                self.call_service(
                    "input_text/set_value",
                    entity_id=rooms_entity,
                    value=rooms_str,
                )
        except Exception:
            pass  # Diagnostics are optional
