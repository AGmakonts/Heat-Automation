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
# On/off are HA scripts. Script entities have no persistent pump run-state, so
# actual run-state is read from the power meter: the pump draws >200 W when
# running and far less when idle, so PUMP_RUNNING_WATTS (150) sits safely
# between the two as the discrimination threshold (tunable). The mains switch
# is read-only safety context.
PUMP_START_SCRIPT = "script.uruchom_pompe"
PUMP_STOP_SCRIPT = "script.wylacz_pompe"
PUMP_POWER_SENSOR = "sensor.zasilanie_pompy_sonoff_10017fadeb_power"
PUMP_MAINS_SWITCH = "switch.zasilanie_pompy_sonoff_10017fadeb_1"
PUMP_RUNNING_WATTS = 150.0
PUMP_SPINUP_GRACE_SEC = 120  # power lags the start/stop command; debounce window

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

        # Last known outdoor temperature (fallback)
        self._last_outdoor_temp: float | None = None

        # Pump command intent (debounces the power sensor start/stop lag).
        # Rehydrate from the persisted last_pump_on/off timestamps so a restart
        # mid-ramp doesn't re-fire the start script or double-count a start.
        self._pump_intent: str | None = None
        self._pump_intent_ts: datetime.datetime | None = None
        _on_min = self._minutes_since("input_datetime.last_pump_on")
        _off_min = self._minutes_since("input_datetime.last_pump_off")
        if (
            _on_min is not None
            and (_off_min is None or _on_min <= _off_min)
            and _on_min * 60.0 < PUMP_SPINUP_GRACE_SEC
        ):
            self._pump_intent = "on"
            self._pump_intent_ts = self.datetime() - datetime.timedelta(minutes=_on_min)
        elif _off_min is not None and _off_min * 60.0 < PUMP_SPINUP_GRACE_SEC:
            self._pump_intent = "off"
            self._pump_intent_ts = self.datetime() - datetime.timedelta(minutes=_off_min)

        # Track last decision tick log to avoid spam
        self._last_logged_state: str | None = None
        self._log_every_n_ticks: int = 5
        self._tick_counter: int = 0

        # Track per-room cooldown expiry time
        self.room_cooldown_until: dict[str, datetime.datetime | None] = {r: None for r in ALL_ROOMS}

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
        """Pump is running when its power draw exceeds the running threshold."""
        power = self._get_number(PUMP_POWER_SENSOR)
        return power is not None and power > PUMP_RUNNING_WATTS

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
        return self._param("input_number.max_continuous_heating_min", 120.0)

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
    def _need_heat(self, room: str) -> bool:
        """Room needs heating: Tcur < Tuser - hyst_on."""
        if room in self.unmanaged_rooms:
            if self.datetime() - self.unmanaged_rooms[room] < datetime.timedelta(minutes=15):
                return False
            else:
                del self.unmanaged_rooms[room]

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
        """
        # Respect unmanaged room timeout
        if room in self.unmanaged_rooms:
            if self.datetime() - self.unmanaged_rooms[room] < datetime.timedelta(minutes=15):
                return False
            else:
                del self.unmanaged_rooms[room]

        if self._is_room_heating(room):
            # Currently heating → keep going until satisfied (offset threshold)
            return not self._satisfied(room)
        else:
            # Not heating → only start at onset threshold
            return self._need_heat(room)

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

    def _on_weather_change(self, entity, attribute, old, new, **kwargs):
        pass  # Tick handles weather; this is placeholder for potential future use

    # -----------------------------------------------------------------------
    # Pump control
    # -----------------------------------------------------------------------
    def _pump_on(self):
        now = self.datetime()
        if self._pump_is_on():
            return
        # Power lags the start command; suppress duplicate starts during ramp-up.
        if (
            self._pump_intent == "on"
            and self._pump_intent_ts is not None
            and (now - self._pump_intent_ts).total_seconds() < PUMP_SPINUP_GRACE_SEC
        ):
            return
        if self.get_state(PUMP_MAINS_SWITCH) == "off":
            self.log(
                f"[PUMP] mains switch {PUMP_MAINS_SWITCH} is OFF; start may not take effect",
                level="WARNING",
            )
        self.call_service("script/turn_on", entity_id=PUMP_START_SCRIPT)
        self._pump_intent = "on"
        self._pump_intent_ts = now
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
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
        now = self.datetime()
        # Power decays after the stop command; suppress duplicate stops.
        if (
            self._pump_intent == "off"
            and self._pump_intent_ts is not None
            and (now - self._pump_intent_ts).total_seconds() < PUMP_SPINUP_GRACE_SEC
        ):
            return
        if not self._pump_is_on():
            return
        self.call_service("script/turn_on", entity_id=PUMP_STOP_SCRIPT)
        self._pump_intent = "off"
        self._pump_intent_ts = now
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        self.call_service(
            "input_datetime/set_datetime",
            entity_id="input_datetime.last_pump_off",
            datetime=now_str,
        )
        self.log("[PUMP] OFF (graceful, script.wylacz_pompe)")

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

    def _build_candidates(self, floor: str, apply_side_effects: bool = True) -> list[str]:
        """Build list of rooms with demand that are eligible (not in cooldown).
        
        Args:
            floor: "GF" or "FF"
            apply_side_effects: If True, applies cooldown when rooms exceed max time.
                               If False, only checks eligibility without side effects.
        
        Returns:
            List of eligible rooms (not sorted, not LERP-limited).
        """
        rooms = GF_ROOMS if floor == "GF" else FF_ROOMS
        now = self.datetime()
        
        candidates = []
        for room in rooms:
            # Check if room needs heat
            if not self._has_demand(room):
                continue
            
            # Check if room has exceeded max continuous heating time
            heating_min = self._get_heating_minutes(room)
            if heating_min >= self.max_continuous_heating_min:
                # Apply cooldown/reset/logging only when side effects are enabled
                if apply_side_effects and not self._is_room_in_cooldown(room, now):
                    cooldown_duration_min = self.min_state_duration
                    self.room_cooldown_until[room] = now + datetime.timedelta(minutes=cooldown_duration_min)
                    self._reset_heating_minutes(room)
                    self.log(f"[ROOM] {room} forced cooldown after {heating_min:.0f}min continuous heating")
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
        
        This is a pure predicate check without side effects - does not trigger
        cooldown enforcement or logging. Used for floor-switching decisions.
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

        # Use LERP to determine max rooms
        t_out = self._get_outdoor_temp()
        max_rooms_lerp = self._lerp_max_rooms(t_out)
        
        # Clamp to floor room count
        rooms = GF_ROOMS if floor == "GF" else FF_ROOMS
        max_rooms_for_floor = len(rooms)
        max_rooms = min(max_rooms_lerp, max_rooms_for_floor)
        
        # Clamp to candidate count
        max_rooms = min(max_rooms, len(candidates))
        
        return candidates[:max_rooms]

    # -----------------------------------------------------------------------
    # Daily reset
    # -----------------------------------------------------------------------
    def _daily_reset(self, **kwargs):
        self._set_number("input_number.pump_on_minutes_today", 0)
        self._set_number("input_number.pump_starts_today", 0)
        
        # Clear all cooldown states and heating minute counters
        for room in ALL_ROOMS:
            self.room_cooldown_until[room] = None
            self._reset_heating_minutes(room)
        
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
                    self._disable_all_rooms()
                    self.log(f"[DECISION] state=OFF_LOCKOUT reason=off_window")
            return

        # --- 2. Compute demand and quota ---
        demand_gf = self._need_heat_floor("GF")
        demand_ff = self._need_heat_floor("FF")
        remaining_quota = self._remaining_quota()
        has_demand = demand_gf or demand_ff

        score_gf = self._floor_score("GF") if demand_gf else 0.0
        score_ff = self._floor_score("FF") if demand_ff else 0.0

        t_out = self._get_outdoor_temp()

        # A graceful stop was just commanded but the power sensor lags (power
        # decays for up to PUMP_SPINUP_GRACE_SEC after the stop script runs).
        # Treat that window as OFF for control so we don't re-open valves on a
        # pump that is shutting down and then flap OFF→HEAT→OFF. This restores
        # the old switch-based instant-off behaviour and lets the normal
        # min_pump_off cooldown govern the next start.
        pump_stopping = (
            self._pump_intent == "off"
            and self._pump_intent_ts is not None
            and (now - self._pump_intent_ts).total_seconds() < PUMP_SPINUP_GRACE_SEC
        )

        # --- 3. If pump is OFF ---
        if not self._pump_is_on() or pump_stopping:
            # Check min_pump_off cooldown
            mins_off = self._minutes_since("input_datetime.last_pump_off")
            cooldown_ok = mins_off is None or mins_off >= self.min_pump_off

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
            elif remaining_quota > 0 and cooldown_ok:
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
                    self._disable_all_rooms()
                if self._tick_counter % self._log_every_n_ticks == 0:
                    reason = "no_demand_no_quota"
                    if not cooldown_ok:
                        reason = f"pump_cooldown ({mins_off:.0f}/{self.min_pump_off:.0f})"
                    self.log(
                        f"[DECISION] state=OFF reason={reason} "
                        f"Tout={t_out:.1f} quota_remaining={remaining_quota:.0f}"
                    )
            return

        # --- 4. Pump is ON ---
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
            self._apply_floor(active_floor)

            if current_state != new_state:
                self._set_fsm_state(new_state)

            if self._tick_counter % self._log_every_n_ticks == 0:
                selected = self._select_rooms(active_floor)
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
    def _apply_floor(self, floor: str):
        active_rooms = GF_ROOMS if floor == "GF" else FF_ROOMS
        inactive_rooms = FF_ROOMS if floor == "GF" else GF_ROOMS

        selected = self._select_rooms(floor)

        for room in active_rooms:
            if room in selected:
                self._enable_room(room)
            else:
                self._disable_room(room)

        for room in inactive_rooms:
            self._disable_room(room)
            self._reset_heating_minutes(room)

    def _disable_all_rooms(self):
        for room in ALL_ROOMS:
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
