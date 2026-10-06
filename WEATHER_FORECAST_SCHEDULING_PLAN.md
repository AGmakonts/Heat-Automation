# Weather-forecast-aware heating schedule — validation plan

Status: **idea, not scheduled for implementation**. This document records what
the idea is, what has to be true for it to pay off, what data we need to
decide, and how it would be built if the data says yes.

## TL;DR

- The physics is right: an air-source heat pump's COP at W35 is roughly 3.0 at
  -7 °C outdoor and 4.0 at +7 °C (manufacturer datasheets), i.e. about 2 % per
  °C. The off window (01:00–06:00) currently makes the pump restart at ~06:00,
  which in winter is usually the coldest hour of the day. Moving part of that
  work into the warmer evening is the concrete win. DHW quota runs are fully
  movable and need no thermal model at all.
- The gain is bounded by the diurnal swing (typically 3–6 °C in a Polish
  winter) and by how much energy the slab can store without overshooting the
  comfort band. Expect **single-digit percent** of pump electricity, not more.
  Whether that is worth the complexity is exactly what the data has to show.
- We cannot measure COP (no heat meter, no flow/return temperature). We can
  measure heating rate, cooling rate, pump input power, outdoor temperature
  and forecast error. That is enough to size the opportunity with the
  datasheet COP curve.
- **Data capture must be fixed before the cold season**: HA's recorder keeps
  raw states for 10 days by default, and the `sensor.ho_*` template sensors
  have no `state_class`, so nothing of the room temperatures survives a purge.
  See Phase 0.
- Decision gate: build it only if the replayed simulation on fitted
  per-room parameters shows ≥ 5 % lower COP-weighted pump energy at equal or
  better comfort-deficit minutes, and the Met.no 6–12 h forecast error is
  clearly below the diurnal swing.

## 1. The idea as testable claims

| # | Claim | How we test it |
|---|-------|----------------|
| C1 | Pump efficiency rises with outdoor temperature enough to matter | Datasheet COP(T_out) at W35 + measured distribution of T_out while the compressor runs. No local COP measurement possible; see §2. |
| C2 | Room cooling rate is predictable from (T_room − T_out) and history | Fit a per-room 1R1C model on no-heating intervals; check R² and day-to-day stability of the constant. |
| C3 | Room heating rate is predictable from history | Fit rise rate per room vs T_out and vs number of concurrently open rooms; check the slab lag. |
| C4 | Forecasts are good enough to plan 6–18 h ahead | Log Met.no hourly forecast snapshots and compare against the Komfovent sensor at t+6 h and t+12 h. |
| C5 | There is enough wiggle room to move load without comfort loss | Replay recorded outdoor days through the existing `ThermalSim` with fitted constants, old vs new scheduler; compare COP-weighted pump minutes and deficit minutes. |

If C4 fails (forecast MAE ≳ diurnal swing) or C5 shows < 5 % gain, stop here.

## 2. What the current data can and cannot answer

Entities we already record (dashboard "All Data" tab, `tools/analyze_history.py`):

- 8 × `climate.*` `current_temperature` and setpoint, mirrored into `sensor.ho_temp_*`, `sensor.ho_target_*`, `sensor.ho_delta_*`
- `input_boolean.heating_*` (valve commanded open), `input_text.heat_state`, `input_text.active_floor`
- `switch.zasilanie_pompy_sonoff_10017fadeb_1` (pump commanded) and `sensor.zasilanie_pompy_sonoff_10017fadeb_power` (W; ~100 W circulation, > 1000 W compressor)
- `sensor.komfovent_outdoor_temperature` (measured at the house) and `sensor.ho_outdoor_temp`
- AppDaemon log: `[DECISION]` lines with `Tout=`, `[PUMP]`, `[ROOM]` events

What is missing and what it blocks:

| Missing | Consequence |
|---------|-------------|
| Flow / return temperature, heat output | No measured COP. Use datasheet COP curve as the weighting function; treat "°C·room gained per kWh vs T_out" only as a sanity check, because weather compensation lowers flow temperature on warm days and confounds it. |
| Pump energy (kWh) | The Sonoff may expose an energy sensor; if so add it to the dashboard. Otherwise integrate power in analysis (1-min samples are fine). |
| Forecast history | Met.no only exposes the current forecast. Without snapshots we cannot evaluate C4. Must be logged (Phase 0). |
| Solar gain, wind, window opening, occupancy | Unexplained residual in the cooling/heating fits. Acceptable for a go/no-go; a night-time (01:00–06:00) fit removes sun and most occupancy. |
| DHW tank temperature | DHW runs can be moved in time but we cannot see the cost (tank cooling). Treat the quota as today: time-based. |
| TRV temperature resolution | LocalTuya TRVs may report 0.5 °C steps. At ~1 °C/h rise that is a coarse signal; fits must use long intervals (≥ 2 h). Check from the CSV before trusting slopes. |

Retention problem: the recorder purges raw states after `purge_keep_days`
(default 10). Hourly long-term statistics are kept forever, but only for
sensors with `state_class: measurement`; climate attributes are never
aggregated and `input_text`/`input_boolean` never get statistics. Today the
`sensor.ho_*` temperature sensors have no `state_class`, so after a purge the
room history is gone. Hourly statistics are also too coarse for rate fits
(the 5-minute short-term statistics are purged together with states).

## 3. Phase 0 — capture (do before the first cold week)

Cheap, no behaviour change:

1. `recorder: purge_keep_days: 150` (or an InfluxDB/VictoriaMetrics sink).
   Raw states for the whole season are the only thing that supports minute
   resolution fits. Disk: 8 TRVs + a handful of sensors at 1-min changes is
   small.
2. Add `state_class: measurement` to `sensor.ho_temp_*`, `sensor.ho_target_*`,
   `sensor.ho_outdoor_temp` so hourly statistics exist as a fallback.
3. Forecast snapshots: a trigger-based template sensor (hourly
   `time_pattern`) calling `weather.get_forecasts` with `type: hourly` on
   `weather.forecast_home`, exposing numeric sensors `ho_fc_t_plus_6h`,
   `ho_fc_t_plus_12h`, `ho_fc_t_min_next_24h`, `ho_fc_t_max_next_24h`, each
   with `state_class: measurement`. Alternatively log the first 24 hourly
   temperatures from AppDaemon once per hour; AppDaemon ≥ 4.5 supports
   `call_service(..., return_response=True)`.
4. Add the Sonoff energy sensor to the "All Data" tab if it exists.
5. Keep exporting the CSV + AppDaemon log as done today; the analysis
   tooling below consumes the same inputs.

## 4. Phase 1 — offline analysis (after 3–4 weeks with T_out < 5 °C)

A new `tools/thermal_fit.py` (or a section in `analyze_history.py`) that
reuses `Series`, `intervals_on` and `room_snapshot`:

Cooling fit (per room), model `dT/dt = −k·(T_room − T_out) + c`:

- Intervals where the room flag is off for ≥ 2 h, skipping the first 90 min
  after the flag went off (slab discharge). The off window gives a clean
  5 h sample every night with no sun.
- Output: `k` [1/h], `c` [°C/h], R², number of intervals, spread of `k`
  across days. `1/k` is the room time constant. Rooms with R² < 0.6 or `k`
  varying > 50 % day to day are not predictable enough and fall back to the
  current reactive rule.

Heating fit (per room):

- Intervals with flag on, pump switch on and power > 1000 W.
- Lag: minutes from valve open to first sustained rise (expect 30–60 min
  for a slab).
- Rise rate after the lag vs T_out and vs number of open rooms on the
  floor. The second dependency is what the LERP room count already assumes;
  this is the first time it gets measured.

Pump operating point:

- Histogram of T_out during compressor-on minutes, today. Multiply by the
  datasheet COP(T_out) at the pump's flow temperature to get the baseline
  "COP-weighted minutes". This number is the denominator for every saving
  claim.
- Fraction of compressor minutes in the 06:00–09:00 block vs 18:00–01:00
  block, with the mean T_out of each. That is the headline opportunity: if
  the evening block is not warmer by ≥ 2 °C on most days, the idea is dead
  regardless of the model quality.

Forecast error (C4): MAE and bias of `ho_fc_t_plus_6h` / `_12h` vs
`sensor.komfovent_outdoor_temperature` at the target hour. Also check
whether the Komfovent intake reads systematically different from Met.no
(sheltered location, building heat); the planner must use the sensor's
frame of reference.

Replay (C5): parameterise `ThermalSim` in
`tests/test_heat_orchestrator_sim.py` with the fitted per-room `k`, lag and
rise rate instead of the current generic constants, feed it recorded
minute-level T_out for 10–20 real days, and run old vs candidate scheduler.
Metrics already exist in the sim: `pump_minutes`, `pump_starts`,
`deficit_minutes`, `peak`, `valve_cycles`. Add `cop_weighted_minutes`.

Go / no-go table:

| Metric | Go if |
|--------|-------|
| Evening vs 06:00 T_out difference (median over cold days) | ≥ 2 °C |
| Forecast MAE at t+12 h | ≤ 1.5 °C |
| Cooling fit R² (rooms that matter: salon, sypialnia, bathrooms) | ≥ 0.6 |
| Replayed COP-weighted pump minutes | ≥ 5 % lower |
| Replayed deficit minutes and peak overshoot | not worse |
| Extra pump starts per day | ≤ +1 |

## 5. Phase 2 — shadow mode

Before any behaviour change: implement the predictor inside the app, compute
the candidate decision every tick and only log it
(`[PREDICT] room=salon T_pred(06:00)=20.9 need=True would=precharge`), for
several weeks. `analyze_history.py` then scores the predictions against what
actually happened (prediction error at horizon, how often the shadow plan
would have started earlier / deferred). This is the same pattern as the
existing log-vs-CSV correlation and costs nothing in comfort.

## 6. Phase 3 — implementation options, if the data says go

Ordered by value per line of code.

### Option B first: move DHW quota runs to the warm hours

No thermal model, no comfort constraint beyond what the quota already is.
Today an exclusive DHW run starts "as soon as the pause has elapsed". Change
the start rule to prefer the forecast-warmest 3–4 h of the day
(`ho_fc_t_max_next_24h` hour ± 2 h), with a deadline fallback so the quota
is still met before the off window. Risk to check first: when hot water is
actually used (morning showers vs evening), because a tank reheated at
14:00 is colder at 07:00 than one reheated at 23:00. Needs one new helper
(`dhw_prefer_warm_hours`, boolean) and a small change in the
`remaining_quota > 0 and cooldown_ok and dhw_pause_ok` branch.

### Option A: predictive demand for room heating

Replace the reactive onset rule with a predictive one, keeping everything
else (floor exclusivity, LERP, hysteresis release, all pump timers):

```
need_heat_now(r)        = T_cur < T_user − h_on                     # today
T_pred(r, H)            = T_out_fc + (T_cur − T_out_fc)·exp(−k_r·H) + c_r·H
need_heat_predictive(r) = T_pred(r, H) < T_user − h_on
```

- `H` is the horizon to the next moment heating becomes impossible or
  expensive: time until `off_window_start` (so the room is pre-charged in
  the evening and survives the night), or a configurable `defer_max_h`.
- Pre-charge: evening demand appears earlier than today, bounded by
  `T_user + h_off` on the release side, so the slab is charged while T_out
  is still high. Comfort improves (no 06:00 dip) and the morning run is
  shorter at the coldest hour.
- Deferral: a room that needs heat now, with forecast T_out rising ≥ X °C
  within `defer_max_h`, waits as long as `T_pred` stays above
  `T_user − h_on − comfort_margin`. `comfort_margin` is the "wiggle room"
  and is the only new comfort parameter. Deferral is riskier than
  pre-charging; ship pre-charge first.
- Fallback: forecast unavailable, `k_r` not fitted, or `_outdoor_temp_source`
  not the primary sensor → plain `need_heat_now`. The predictive rule can
  only change *when* demand appears; it never bypasses `min_pump_on`,
  `min_pump_off`, `min_state_duration` or the rotation caps.
- Code touch points: `_need_heat` / `_need_heat_floor` (new predictive
  variant), a `_get_forecast()` sibling of `_get_outdoor_temp()` cached per
  hour, per-room `k_r` / `c_r` either as `input_number` helpers written by
  the analysis tool or as constants in `ROOMS`, new helpers
  `predictive_heating` (boolean), `comfort_margin`, `defer_max_h`.
- Test: `ThermalSim` already closes the loop; add a varying `t_out` series
  and the fitted constants, and assert the evening pre-charge happens and
  the morning deficit shrinks.

### Option C: receding-horizon planner (MPC-lite)

Every hour, over the next 24 hourly forecast slots, choose per slot which
floor (or DHW, or nothing) runs, minimising `Σ minutes / COP(T_out_slot)`
subject to `T_pred(r) ≥ T_user − h_on − comfort_margin` for all rooms, the
off window, and floor exclusivity; execute only the current slot through the
existing FSM. With linear 1R1C rooms and one binary choice per slot this is
a small dynamic programme, not a solver dependency. EMHASS does this
generically (linear thermal model, forecasted outdoor temperature, cost
objective) but models one deferrable load per thermostat and knows nothing
about GF-xor-FF or TRV setpoint manipulation; it is a reference, not a
drop-in. Only worth it if Option A's measured gain is clearly limited by its
single-horizon rule.

Recommendation: B, then A (pre-charge only), then evaluate; C stays a
possibility.

## 7. Expected magnitude and risks

- COP slope ≈ 0.07 per °C around 0 °C at W35 (3.0 at −7, 4.0 at +7). A 4 °C
  shift on the moved share of energy ≈ 10 % on that share. If one third of
  daily compressor minutes can move, the total is ≈ 3 %. Lowering the flow
  temperature (heating curve on the pump itself) is a larger lever and
  independent of this project.
- Pre-charging raises the evening room temperature, so overnight losses
  grow with the extra ΔT; the fitted `k_r` quantifies this and the replay
  must count it. Slab storage is lossy.
- Forecast error is asymmetric in cost: an over-optimistic deferral
  produces a cold room, an over-pessimistic pre-charge only costs a bit of
  overshoot. Hence pre-charge first, deferral last.
- If the household is on a time-of-use tariff (G12/G12w style), the price
  signal dominates COP and the objective in C changes; the data capture is
  the same.

## 8. Open questions (answers change the plan)

1. Pump make/model and its COP table at W30–W35, and whether it runs its own
   weather-compensated heating curve (the flow temperature then depends on
   T_out, which affects both heating rate and COP).
2. Electricity tariff: flat or time-of-use?
3. Does the Sonoff expose an energy (kWh) sensor?
4. TRV temperature resolution (0.1 °C or 0.5 °C)?
5. When is hot water used? Determines whether Option B is safe.

## Sources

- HA recorder defaults and statistics retention:
  https://home-assistant.io/components/recorder/ ,
  https://smarthomescene.com/blog/understanding-home-assistants-database-and-statistics-model/
- `weather.get_forecasts`, hourly forecast fields:
  https://home-assistant.io/integrations/weather ,
  https://community.home-assistant.io/t/weather-get-forecasts/737531
- AppDaemon service responses (`return_response`, v4.5.0):
  https://appdaemon.readthedocs.io/en/latest/HASS_API_REFERENCE.html
- ASHP COP at A7/A2/A−7 W35 (manufacturer data):
  https://www.idm-energie.at/en/heat-pump-efficiency/outdoor-temperature/ ,
  https://www.stiebel-eltron.com/en/home/products-solutions/renewables/heat_pump/air_water_heat_pumps/wpl-13-23-e/wpl-13-e/technical-data.product.pdf
- Grey-box RC identification from thermostat data:
  https://www.sciencedirect.com/science/article/abs/pii/S0378778822007423 ,
  https://www.sciencedirect.com/science/article/abs/pii/S0378778821001201
- EMHASS thermal deferrable load model:
  https://emhass.readthedocs.io/en/latest/thermal_model.html
