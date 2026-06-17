# Codebase Audit — Heat Orchestrator

Date: 2026-06-17
Scope: full repository (AppDaemon app, HA helpers package, spec, docs)

## Overview

A single-file AppDaemon app (`apps/heat_orchestrator/heat_orchestrator.py`,
~977 lines) controlling heat-pump underfloor heating for Home Assistant, plus a
helpers package (`packages/heat_orchestrator_helpers.yaml`), a detailed
implementation spec, and setup/update guides. The Python compiles cleanly
(`py_compile` OK). There are no automated tests and no CI.

---

## 🔴 Critical — the `salon` / `salon_2` identity split — RESOLVED

> **Resolution (2026-06-17):** The HA entities were intentionally renamed to
> `salon_2` after the initial implementation, and HA entities (including
> helpers) must not be renamed. The `user_sp_salon` helper is the sole entity
> that kept the old name. Fixed in code by routing the `salon_2` room's user
> setpoint through an explicit override (`_USER_SP_OVERRIDES` /
> `_user_sp_entity()`) to `input_number.user_sp_salon`, and by removing the
> stale (never-matching) `_HEATING_ENTITY_OVERRIDES` entry. No HA entities were
> renamed. Original finding retained below for context.

The room is registered in code as `salon_2` (`heat_orchestrator.py:19`), but all
entity names are derived from the `room_id`. The supporting entities are split
across two spellings:

| Derived entity (room_id = `salon_2`)      | Defined in helpers?                       | Docs say                |
|-------------------------------------------|-------------------------------------------|-------------------------|
| `climate.salon_2`                         | n/a (external)                            | `climate.salon`         |
| `input_number.user_sp_salon_2`            | ❌ **missing** (`user_sp_salon` exists)    | `user_sp_salon`         |
| `input_number.priority_salon_2`           | ✅ exists                                  | `priority_salon`        |
| `input_boolean.heating_salon_2`           | ✅ exists                                  | `heating_salon`         |
| `input_number.heating_minutes_salon_2`    | ✅ exists                                  | `heating_minutes_salon` |

### Consequences

- **`user_sp_salon_2` does not exist.** `_get_number()` returns `None`, so
  `_need_heat("salon_2")` and `_room_score()` always treat the salon as having
  no demand — the salon never heats, and the GF floor permanently loses a room.
  Bootstrap also writes to a non-existent helper.
- **`climate.salon_2`** is almost certainly wrong (README, spec and SETUP_GUIDE
  all say `climate.salon`). If so, all reads return `None` and every
  `set_temperature` targets a missing entity — the salon is entirely unmanaged.
- The override map `_HEATING_ENTITY_OVERRIDES = {"salon": "input_boolean.heating_salon_2"}`
  (`heat_orchestrator.py:499`) is **dead code**: it is keyed `"salon"`, but no
  room is named `salon` (the room is `salon_2`), so it never matches. `salon_2`
  falls through to the default, which coincidentally resolves to the correct
  boolean — masking the inconsistency.

### Recommendation (applied)

Because the live HA entities are canonically `salon_2` and must not be renamed,
the deviation is handled in code: `salon_2` maps to the existing
`input_number.user_sp_salon` via `_USER_SP_OVERRIDES`. The `climate.salon_2`,
`priority_salon_2`, `heating_salon_2` and `heating_minutes_salon_2` entities all
match the `salon_2` room_id and need no override.

---

## 🟠 Medium

1. **Setpoint can be overwritten by 7 °C on a latency edge.** `_disable_room`
   sets the thermostat to 7 °C with `automation_guard=True`, released after a
   fixed 2 s (`GUARD_RELEASE_DELAY`). If HA delivers the state-change callback
   after 2 s, `_on_thermostat_change` sees the guard down and the room not
   heating, and stores `user_sp = 7.0` — exactly the data loss the guard is
   meant to prevent (spec §4.2). Rare under normal latency, but a real
   timing-dependent corruption path. Consider an "expected setpoint" check in
   addition to the time-based guard.

2. **Docs vs. helpers disagree on salon entity names.** `SETUP_GUIDE.md` and
   `UPDATE_GUIDE.md` instruct users to create `input_boolean.heating_salon`,
   `input_number.user_sp_salon`, `priority_salon`, while the shipped helpers
   package defines the `_2` variants for priority/heating/heating_minutes. A
   user following the guide creates entities the code will not find. (Same root
   cause as the critical item.)

---

## 🟡 Low / cleanup

3. **`fromisoformat` with a space separator needs Python 3.11+.**
   `_get_state_since` / `_minutes_since` parse `"%Y-%m-%d %H:%M:%S"` via
   `datetime.fromisoformat`. On <3.11 this raises (caught → returns `None`),
   silently weakening pump-cooldown and min-state-duration checks. Pin/note the
   Python version or use `strptime`.

4. **Dead/legacy config — RESOLVED (code).** The unused `bulk_mode_temp`,
   `sequential_mode_temp` and `max_rooms_limited` properties (superseded by
   `_lerp_max_rooms`) were removed, along with the never-read `_last_logged_state`
   instance variable and the no-op `_on_weather_change` listener/handler. The
   corresponding HA helpers are retained for backward compatibility (per the
   "don't rename HA entities" constraint). Docs (README parameter table, spec
   §13.5) still describe the old bulk/limited/sequential mode and remain to be
   updated.

5. **`_need_heat_floor` uses `_has_demand`, not `_need_heat`.** A deliberate,
   reasonable hysteresis improvement that deviates from spec §5 without the spec
   being updated. Doc drift.

6. **Redundant `_get_outdoor_temp()` calls per tick.** Called in `_tick`, again
   inside `_select_rooms` (invoked from `_apply_floor`, `_update_diagnostics`
   and logging). Each can trigger a `weather.get_forecasts` service call on the
   fallback path. Cache once per tick.

7. **`day_reset_time` read only at `initialize()`** (`heat_orchestrator.py:90`).
   Changing it later in HA won't reschedule the daily reset until AppDaemon
   restarts.

8. **`_set_number` rounds to 1 decimal** but setpoint helpers use `step: 0.5`.
   Values like `21.3` may be stored that don't align to the step.

9. **No tests and no CI.** Control logic with this many edge cases (FSM
   transitions, hysteresis, quota, cooldowns) would benefit from a small pytest
   suite with a mocked `hass` object — it would have caught the salon split.

---

## ✅ Strengths

- Clean structure, good docstrings, consistent logging conventions.
- The unavailable→available TRV recovery edge (`heat_orchestrator.py:541`) is
  correctly handled.
- Hysteresis band, LERP room selection, cooldown / max-continuous-heating, and
  DHW-quota logic match the spec well.
- Error handling on `set_temperature` (retry + `unmanaged_rooms` timeout)
  follows spec §11.6.
- Floor-exclusivity invariant is upheld throughout.
