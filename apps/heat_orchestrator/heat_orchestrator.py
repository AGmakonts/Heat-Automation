"""
Heat Orchestrator – AppDaemon App for Home Assistant
=====================================================
Controls underfloor heating with a heat pump, managing floor selection (GF/FF),
room-level valve control via thermostats, pump on/off logic, DHW quota,
and nightly off-window enforcement.

Spec version: 2026-02-09
"""

from __future__ import annotations

import hassapi as hass
import datetime

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GF_ROOMS = ["gabinet_ani", "lazienka_parter", "salon_2"]
FF_ROOMS = ["sypialnia", "lazienka_pietro", "pokoj_z_oknem_naroznym", "pokoj_z_tarasem"]
ALL_ROOMS = GF_ROOMS + FF_ROOMS

CLIMATE_PREFIX = "climate."
USER_SP_PREFIX = "input_number.user_sp_"
PRIORITY_PREFIX = "input_number.priority_"
HEATING_PREFIX = "input_boolean.heating_"

PUMP_SWITCH = "switch.sonoff_10017fadeb"
PUMP_OFF_BUTTON = "input_button.wylacznik_pompy"
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

        # Track last decision tick log to avoid spam
        self._last_logged_state: str | None = None
        self._log_every_n_ticks: int = 5
        self._tick_counter: int = 0

        # --- Bootstrap user setpoints if empty ---
        self._bootstrap_user_setpoints()

        # --- Listeners: thermostat setpoint changes (user tracking) ---
        for room in ALL_ROOMS:
            entity = f"{CLIMATE_PREFIX}{room}"
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
            sp_entity = f"{USER_SP_PREFIX}{room}"
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
        entity = f"{CLIMATE_PREFIX}{room}"
        val = self.get_state(entity, attribute="temperature")
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _get_climate_current_temp(self, room: str) -> float | None:
        entity = f"{CLIMATE_PREFIX}{room}"
        val = self.get_state(entity, attribute="current_temperature")
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _pump_is_on(self) -> bool:
        return self.get_state(PUMP_SWITCH) == "on"

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
        t_user = self._get_number(f"{USER_SP_PREFIX}{room}")
        if t_cur is None or t_user is None:
            return False
        return t_cur < (t_user - self.hyst_on)

    def _satisfied(self, room: str) -> bool:
        """Room is satisfied: Tcur >= Tuser - hyst_off."""
        t_cur = self._get_climate_current_temp(room)
        t_user = self._get_number(f"{USER_SP_PREFIX}{room}")
        if t_cur is None or t_user is None:
            return True
        return t_cur >= (t_user - self.hyst_off)

    def _need_heat_floor(self, floor: str) -> bool:
        rooms = GF_ROOMS if floor == "GF" else FF_ROOMS
        return any(self._need_heat(r) for r in rooms)

    # -----------------------------------------------------------------------
    # Scoring
    # -----------------------------------------------------------------------
    def _room_score(self, room: str) -> float:
        t_cur = self._get_climate_current_temp(room)
        t_user = self._get_number(f"{USER_SP_PREFIX}{room}")
        priority = self._param(f"{PRIORITY_PREFIX}{room}", 50.0)
        if t_cur is None or t_user is None:
            return 0.0
        deficit = max(0.0, t_user - t_cur)
        return deficit * priority

    def _floor_score(self, floor: str) -> float:
        rooms = GF_ROOMS if floor == "GF" else FF_ROOMS
        scores = [self._room_score(r) for r in rooms if self._need_heat(r)]
        return max(scores) if scores else 0.0

    # -----------------------------------------------------------------------
    # Room enable / disable
    # -----------------------------------------------------------------------
    def _enable_room(self, room: str):
        t_user = self._get_number(f"{USER_SP_PREFIX}{room}")
        if t_user is None or t_user < 5.0 or t_user > 30.0:
            climate_sp = self._get_climate_setpoint(room)
            if climate_sp is not None and 15.0 <= climate_sp <= 30.0:
                t_user = climate_sp
            else:
                t_user = 21.0

        entity = f"{CLIMATE_PREFIX}{room}"
        current_sp = self._get_climate_setpoint(room)
        if current_sp is not None and abs(current_sp - t_user) < 0.05:
            self._set_heating_sensor(room, True)
            return  # already correct

        self.automation_guard[room] = True
        try:
            self.call_service(
                "climate/set_temperature", entity_id=entity, temperature=t_user
            )
            self.log(f"[ROOM] enable {room} → {t_user}°C")
            self._set_heating_sensor(room, True)
        except Exception as e:
            self.log(f"[ERROR] enable_room {room}: {e}", level="ERROR")
            # Retry once
            try:
                self.call_service(
                    "climate/set_temperature", entity_id=entity, temperature=t_user
                )
                self._set_heating_sensor(room, True)
            except Exception as e2:
                self.log(f"[ERROR] enable_room {room} retry failed: {e2}", level="ERROR")
                self.unmanaged_rooms[room] = self.datetime()

        self.run_in(self._release_guard, GUARD_RELEASE_DELAY, room=room)

    def _disable_room(self, room: str):
        entity = f"{CLIMATE_PREFIX}{room}"
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

    # Mapping for rooms whose input_boolean entity ID differs from room_id
    _HEATING_ENTITY_OVERRIDES: dict[str, str] = {
        "salon": "input_boolean.heating_salon_2",
    }

    def _set_heating_sensor(self, room: str, heating: bool):
        """Update the per-room heating status input_boolean."""
        entity = self._HEATING_ENTITY_OVERRIDES.get(room, f"{HEATING_PREFIX}{room}")
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

        try:
            new_val = float(new)
        except (ValueError, TypeError):
            return

        if not (5.0 <= new_val <= 30.0):
            return

        sp_entity = f"{USER_SP_PREFIX}{room}"
        current_user_sp = self._get_number(sp_entity)

        if current_user_sp is not None and abs(current_user_sp - new_val) < 0.05:
            return  # No change

        self._set_number(sp_entity, new_val)
        self.log(f"[USER] {room} setpoint changed to {new_val}°C")

    def _on_weather_change(self, entity, attribute, old, new, **kwargs):
        pass  # Tick handles weather; this is placeholder for potential future use

    # -----------------------------------------------------------------------
    # Pump control
    # -----------------------------------------------------------------------
    def _pump_on(self):
        if self._pump_is_on():
            return
        self.call_service("switch/turn_on", entity_id=PUMP_SWITCH)
        now_str = self.datetime().strftime("%Y-%m-%d %H:%M:%S")
        self.call_service(
            "input_datetime/set_datetime",
            entity_id="input_datetime.last_pump_on",
            datetime=now_str,
        )
        # Increment starts
        starts = self._get_number("input_number.pump_starts_today") or 0
        self._set_number("input_number.pump_starts_today", starts + 1)
        self.log("[PUMP] ON")

    def _pump_off(self):
        if not self._pump_is_on():
            return
        self.call_service("input_button/press", entity_id=PUMP_OFF_BUTTON)
        now_str = self.datetime().strftime("%Y-%m-%d %H:%M:%S")
        self.call_service(
            "input_datetime/set_datetime",
            entity_id="input_datetime.last_pump_off",
            datetime=now_str,
        )
        self.log("[PUMP] OFF (graceful)")

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
    # Room selection per mode (bulk / sequential / limited)
    # -----------------------------------------------------------------------
    def _select_rooms(self, floor: str) -> list[str]:
        rooms = GF_ROOMS if floor == "GF" else FF_ROOMS
        candidates = [r for r in rooms if self._need_heat(r)]

        if not candidates:
            return []

        # Sort by priority desc, then deficit desc
        def sort_key(r):
            prio = self._param(f"{PRIORITY_PREFIX}{r}", 50.0)
            t_cur = self._get_climate_current_temp(r) or 0.0
            t_user = self._get_number(f"{USER_SP_PREFIX}{r}") or 21.0
            deficit = max(0.0, t_user - t_cur)
            return (-prio, -deficit)

        candidates.sort(key=sort_key)

        t_out = self._get_outdoor_temp()

        if t_out >= self.bulk_mode_temp:
            # Bulk mode: all rooms with demand
            return candidates
        elif t_out <= self.sequential_mode_temp:
            # Sequential: only 1 room
            return candidates[:1]
        else:
            # Limited mode
            return candidates[: self.max_rooms_limited]

    # -----------------------------------------------------------------------
    # Daily reset
    # -----------------------------------------------------------------------
    def _daily_reset(self, **kwargs):
        self._set_number("input_number.pump_on_minutes_today", 0)
        self._set_number("input_number.pump_starts_today", 0)
        self.log("[RESET] Daily counters zeroed")

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

        # --- 3. If pump is OFF ---
        if not self._pump_is_on():
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
                # Reconsider floor
                if active_floor == "GF" and score_ff > score_gf and demand_ff:
                    active_floor = "FF"
                    self.log(f"[DECISION] switching floor GF→FF (FF_score={score_ff:.1f} > GF_score={score_gf:.1f})")
                elif active_floor == "FF" and score_gf > score_ff and demand_gf:
                    active_floor = "GF"
                    self.log(f"[DECISION] switching floor FF→GF (GF_score={score_gf:.1f} > FF_score={score_ff:.1f})")

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

    def _disable_all_rooms(self):
        for room in ALL_ROOMS:
            self._disable_room(room)

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
