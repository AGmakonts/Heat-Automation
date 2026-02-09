# Heat Orchestrator for Home Assistant

An AppDaemon-based heating controller for underfloor heating systems with a heat pump. Manages floor selection, room-level valve control via thermostats, pump safety logic, DHW quota, and nightly off-window enforcement.

## Overview

This system controls a heat pump powering underfloor heating across two manifolds (ground floor and first floor). Because the heat pump has no direct communication with room thermostats or valves, the orchestrator manages heating by manipulating thermostat setpoints — lowering them to 7°C to close valves, and restoring user-set temperatures to open them.

### Key Features

- **Floor exclusivity** — only one floor (GF or FF) heats at a time to respect hydraulic constraints
- **Smart room selection** — prioritizes rooms by temperature deficit × user-defined priority
- **Outdoor temperature modes** — adapts the number of simultaneously heated rooms based on outside temperature (bulk / limited / sequential)
- **User setpoint memory** — remembers manual thermostat adjustments even when rooms are temporarily disabled
- **DHW quota** — ensures the pump runs a configurable minimum daily hours for hot water
- **Nightly off-window** — enforces a pump-off period (default 01:00–06:00)
- **Anti-oscillation** — minimum timers on pump cycles and floor switches
- **Fully configurable** — all parameters adjustable live via Home Assistant helpers

### FSM States

| State | Description |
|-------|-------------|
| `OFF_LOCKOUT` | Forced OFF during nightly window |
| `OFF` | Pump off, outside lockout window |
| `HEAT_GF` | Pump on, ground floor active |
| `HEAT_FF` | Pump on, first floor active |
| `DHW_QUOTA` | Pump on, all rooms off, filling daily quota |

## Project Structure

```
├── apps/
│   └── heat_orchestrator/
│       ├── heat_orchestrator.py   # AppDaemon app (FSM + control logic)
│       └── apps.yaml              # AppDaemon app registration
├── packages/
│   └── heat_orchestrator_helpers.yaml  # HA helpers (42 entities)
├── home-assistant-heat-orchestrator-spec.md  # Full specification
├── SETUP_GUIDE.md                 # Detailed step-by-step setup
└── README.md
```

## Controlled Entities

**Ground Floor (GF)**
- `climate.gabinet_ani`
- `climate.lazienka_parter`
- `climate.salon`

**First Floor (FF)**
- `climate.sypialnia`
- `climate.lazienka_pietro`
- `climate.pokoj_z_oknem_naroznym`
- `climate.pokoj_z_tarasem`

**Pump**
- ON: `switch.sonoff_10017fadeb`
- OFF: `input_button.wylacznik_pompy` (graceful shutdown)

**Weather**
- `weather.forecast_home` (Met.no)

## Quick Start

### 1. Install Helpers

Copy `packages/heat_orchestrator_helpers.yaml` to `/config/packages/` and add to `configuration.yaml`:

```yaml
homeassistant:
  packages:
    heat_orchestrator: !include packages/heat_orchestrator_helpers.yaml
```

Restart Home Assistant.

### 2. Set Initial Values

After restart, go to **Developer Tools → States** and configure:

- `input_datetime.off_window_start` → `01:00:00`
- `input_datetime.off_window_end` → `06:00:00`
- `input_datetime.day_reset_time` → `00:00:00`
- `input_number.priority_*` → set per-room priorities (1–100, higher = heated first)

### 3. Install AppDaemon

Install the AppDaemon add-on from the HA Add-on Store (or via Docker). Configure `appdaemon.yaml` with a long-lived access token:

```yaml
appdaemon:
  time_zone: Europe/Warsaw
  plugins:
    HASS:
      type: hass
      ha_url: "http://homeassistant.local:8123"
      token: !secret ha_token
```

### 4. Deploy the App

Copy `apps/heat_orchestrator/` into your AppDaemon apps directory:

```
<appdaemon_config>/apps/heat_orchestrator/
├── heat_orchestrator.py
└── apps.yaml
```

AppDaemon will auto-detect and load the app.

### 5. Verify

Check the AppDaemon log for:
```
INFO heat_orchestrator: === HeatOrchestrator ready ===
```

## Default Parameters

| Parameter | Default | Helper Entity |
|-----------|---------|---------------|
| Room OFF setpoint | 7.0°C | `input_number.room_off_setpoint` |
| Heating hysteresis ON | 0.3°C | `input_number.heating_hyst_on` |
| Heating hysteresis OFF | 0.2°C | `input_number.heating_hyst_off` |
| Min floor state duration | 25 min | `input_number.min_state_duration_min` |
| Min pump ON time | 40 min | `input_number.min_pump_on_min` |
| Min pump OFF time | 25 min | `input_number.min_pump_off_min` |
| DHW min daily hours | 3.5 h | `input_number.dhw_min_run_hours` |
| Bulk mode threshold | +5°C | `input_number.bulk_mode_temp` |
| Sequential mode threshold | -5°C | `input_number.sequential_mode_temp` |
| Max rooms (limited mode) | 2 | `input_number.max_rooms_limited` |
| OFF window | 01:00–06:00 | `input_datetime.off_window_start/end` |

## How It Works

Every 60 seconds the orchestrator runs a tick cycle:

1. **OFF window check** — if inside the nightly window, shut down the pump (respecting minimum on-time) and enter `OFF_LOCKOUT`
2. **Compute demand** — for each room, check if current temperature is below user setpoint minus hysteresis
3. **Score floors** — `floor_score = max(deficit × priority)` across all rooms with demand
4. **Select floor** — pick the highest-scoring floor (won't switch before `min_state_duration` elapses)
5. **Select rooms** — based on outdoor temperature: all demanding rooms (bulk), top N (limited), or top 1 (sequential)
6. **Control thermostats** — enable selected rooms (restore user setpoint), disable others (set to 7°C)
7. **Control pump** — turn on/off respecting min on/off timers and cooldown periods
8. **DHW quota** — if no heating demand but daily quota unmet, keep pump running with all rooms disabled

## Documentation

- [SETUP_GUIDE.md](SETUP_GUIDE.md) — detailed step-by-step installation and configuration
- [home-assistant-heat-orchestrator-spec.md](home-assistant-heat-orchestrator-spec.md) — full technical specification

## License

Private project.
