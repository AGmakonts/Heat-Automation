# Heat Orchestrator – Decision Flowcharts

## 1. Per-Tick Decision Flow

This flowchart describes the logic executed every 60 seconds by the `_tick()` method.

```mermaid
flowchart TD
    START([Tick every 60s]) --> PUMP_ACCOUNTING{Pump ON?}

    PUMP_ACCOUNTING -- Yes --> ADD_MINUTE[pump_on_minutes_today += 1]
    PUMP_ACCOUNTING -- No --> OFF_WIN_CHECK
    ADD_MINUTE --> OFF_WIN_CHECK

    %% ── Step 1: OFF window ──
    OFF_WIN_CHECK{In OFF window?\n01:00 – 06:00}
    OFF_WIN_CHECK -- Yes --> PUMP_ON_IN_WIN{Pump ON?}
    OFF_WIN_CHECK -- No --> COMPUTE_DEMAND

    PUMP_ON_IN_WIN -- Yes --> MIN_ON_MET{min_pump_on\nelapsed?}
    PUMP_ON_IN_WIN -- No --> SET_LOCKOUT[Set state = OFF_LOCKOUT\nDisable all rooms]

    MIN_ON_MET -- Yes --> PUMP_OFF_WIN[Pump OFF\nDisable all rooms\nSet state = OFF_LOCKOUT]
    MIN_ON_MET -- No --> WAIT_MIN_ON[Keep pump ON\nWait for min_pump_on]

    SET_LOCKOUT --> TICK_END([End tick])
    PUMP_OFF_WIN --> TICK_END
    WAIT_MIN_ON --> TICK_END

    %% ── Step 2: Compute demand & quota ──
    COMPUTE_DEMAND[Compute demand GF / FF\nCompute remaining quota\nCompute floor scores\nGet outdoor temperature]

    COMPUTE_DEMAND --> PUMP_STATE{Pump ON?}

    %% ── Step 3: Pump is OFF ──
    PUMP_STATE -- "No (Pump OFF)" --> COOLDOWN_OK{min_pump_off\ncooldown elapsed?}

    COOLDOWN_OK -- No --> STAY_OFF[Set state = OFF]
    COOLDOWN_OK -- Yes --> HAS_DEMAND_OFF{Any floor\nhas demand?}

    HAS_DEMAND_OFF -- Yes --> PICK_FLOOR_OFF[Pick floor with\nhighest score\nGF vs FF]
    HAS_DEMAND_OFF -- No --> QUOTA_OFF{Remaining\nquota > 0?}

    QUOTA_OFF -- Yes --> DHW_START[Disable all rooms\nPump ON\nSet state = DHW_QUOTA]
    QUOTA_OFF -- No --> STAY_OFF

    PICK_FLOOR_OFF --> APPLY_FLOOR_OFF[Apply floor selection\nPump ON\nSet state = HEAT_GF / HEAT_FF]

    STAY_OFF --> TICK_END
    DHW_START --> TICK_END
    APPLY_FLOOR_OFF --> DIAG

    %% ── Step 4: Pump is ON ──
    PUMP_STATE -- "Yes (Pump ON)" --> HAS_DEMAND_ON{Any floor\nhas demand?}

    HAS_DEMAND_ON -- Yes --> DET_ACTIVE[Determine active floor\nfrom current FSM state]
    HAS_DEMAND_ON -- No --> QUOTA_ON{Remaining\nquota > 0?}

    QUOTA_ON -- Yes --> DHW_CONTINUE[Disable all rooms\nSet state = DHW_QUOTA]
    QUOTA_ON -- No --> MIN_ON_OFF{min_pump_on\nelapsed?}

    MIN_ON_OFF -- Yes --> PUMP_OFF_DEMAND[Pump OFF\nDisable all rooms\nSet state = OFF]
    MIN_ON_OFF -- No --> WAIT_PUMP[Wait for min_pump_on\nbefore turning OFF]

    DET_ACTIVE --> MIN_DUR{min_state_duration\nelapsed?}

    MIN_DUR -- Yes --> RECONSIDER{Other floor\nhas higher score?}
    MIN_DUR -- No --> KEEP_FLOOR[Keep current floor]

    RECONSIDER -- Yes --> SWITCH_FLOOR[Switch active floor]
    RECONSIDER -- No --> CHECK_SELECTABLE{Active floor has\nselectable rooms?}

    CHECK_SELECTABLE -- Yes --> KEEP_FLOOR
    CHECK_SELECTABLE -- No --> OTHER_AVAIL{Other floor has\ndemand & selectable\nrooms?}

    OTHER_AVAIL -- Yes --> SWITCH_FLOOR
    OTHER_AVAIL -- No --> KEEP_FLOOR

    SWITCH_FLOOR --> APPLY_FLOOR_ON
    KEEP_FLOOR --> APPLY_FLOOR_ON[Apply floor selection:\nselect rooms, enable/disable]

    APPLY_FLOOR_ON --> DIAG
    DHW_CONTINUE --> DIAG
    PUMP_OFF_DEMAND --> TICK_END
    WAIT_PUMP --> DIAG

    DIAG[Update diagnostics] --> TICK_END
```

## 2. Overall Process Overview

This flowchart shows the high-level architecture: system components, FSM states, and how they relate.

```mermaid
flowchart TD
    subgraph Triggers
        TIMER([60s Timer Tick])
        USER([User changes\nthermostat setpoint])
        DAILY([Daily reset\nat 00:00])
    end

    subgraph FSM["Finite State Machine"]
        OFF_LOCKOUT[OFF_LOCKOUT\nForced OFF during\nnightly window]
        OFF[OFF\nPump off\noutside lockout]
        HEAT_GF[HEAT_GF\nPump ON\nGround floor active]
        HEAT_FF[HEAT_FF\nPump ON\nFirst floor active]
        DHW_QUOTA[DHW_QUOTA\nPump ON, all rooms OFF\nFilling daily quota]
    end

    subgraph Decision["Decision Logic"]
        OFF_WIN{OFF Window\n01:00 – 06:00?}
        DEMAND{Heating demand\non any floor?}
        QUOTA{DHW quota\nremaining?}
        FLOOR_SCORE{Floor scoring\ndeficit × priority}
        ROOM_SELECT{Room selection\nLERP + priority\n+ cooldown}
    end

    subgraph Inputs["Inputs"]
        TEMPS[Room temperatures\n7 climate entities]
        WEATHER[Outdoor temperature\nweather.forecast_home]
        SETPOINTS[User setpoints\nstored in helpers]
        PRIORITIES[Room priorities\n1 – 100]
        PARAMS[Parameters\nhysteresis, timers,\nLERP config]
    end

    subgraph Actuators["Actuators"]
        PUMP_ON_ACT[Pump ON\nswitch.sonoff]
        PUMP_OFF_ACT[Pump OFF\ninput_button]
        THERMO[Thermostats\nset_temperature]
    end

    %% ── Trigger connections ──
    TIMER --> OFF_WIN
    USER --> SETPOINTS
    DAILY --> RESET([Reset counters\n& cooldowns])

    %% ── Decision flow ──
    OFF_WIN -- Yes --> OFF_LOCKOUT
    OFF_WIN -- No --> DEMAND

    DEMAND -- Yes --> FLOOR_SCORE
    DEMAND -- No --> QUOTA

    QUOTA -- "Yes" --> DHW_QUOTA
    QUOTA -- "No" --> OFF

    FLOOR_SCORE --> ROOM_SELECT
    ROOM_SELECT --> HEAT_GF
    ROOM_SELECT --> HEAT_FF

    %% ── Input connections ──
    TEMPS --> DEMAND
    WEATHER --> ROOM_SELECT
    SETPOINTS --> DEMAND
    PRIORITIES --> FLOOR_SCORE
    PARAMS --> ROOM_SELECT

    %% ── FSM transitions ──
    OFF_LOCKOUT -. "exit OFF window" .-> OFF
    OFF -. "demand detected" .-> HEAT_GF
    OFF -. "demand detected" .-> HEAT_FF
    OFF -. "quota remaining" .-> DHW_QUOTA
    HEAT_GF -. "FF scores higher\n& min_state elapsed" .-> HEAT_FF
    HEAT_FF -. "GF scores higher\n& min_state elapsed" .-> HEAT_GF
    HEAT_GF -. "no demand,\nquota remaining" .-> DHW_QUOTA
    HEAT_FF -. "no demand,\nquota remaining" .-> DHW_QUOTA
    HEAT_GF -. "no demand,\nno quota" .-> OFF
    HEAT_FF -. "no demand,\nno quota" .-> OFF
    DHW_QUOTA -. "demand detected" .-> HEAT_GF
    DHW_QUOTA -. "demand detected" .-> HEAT_FF
    DHW_QUOTA -. "quota filled" .-> OFF

    %% ── Actuator connections ──
    HEAT_GF --> PUMP_ON_ACT
    HEAT_FF --> PUMP_ON_ACT
    DHW_QUOTA --> PUMP_ON_ACT
    OFF --> PUMP_OFF_ACT
    OFF_LOCKOUT --> PUMP_OFF_ACT
    HEAT_GF --> THERMO
    HEAT_FF --> THERMO

    %% ── Guard system ──
    subgraph Guard["Automation Guard"]
        GUARD_SET[Set guard = True\nbefore automation\nsetpoint change]
        GUARD_REL[Release guard\nafter 2s delay]
    end

    THERMO --> GUARD_SET
    GUARD_SET --> GUARD_REL

    subgraph RoomSelection["Room Selection Detail"]
        direction TB
        RS1[Get rooms with demand\non active floor]
        RS2[Exclude rooms\nin cooldown]
        RS3[Sort by priority desc\nthen deficit desc]
        RS4[LERP: outdoor temp\n→ max room count]
        RS5[Select top N rooms]
        RS6[Enable selected rooms\nrestore user setpoint]
        RS7[Disable other rooms\nset to 7°C]
        RS1 --> RS2 --> RS3 --> RS4 --> RS5 --> RS6 --> RS7
    end

    ROOM_SELECT -.-> RoomSelection
```

## Legend

| Symbol | Meaning |
|--------|---------|
| Solid arrows (`-->`) | Direct action / data flow |
| Dashed arrows (`-.->`) | State transitions |
| Diamond `{}` | Decision point |
| Rounded rectangle `([])` | Start / end / event |
| Rectangle `[]` | Action / process |

## Key Concepts

- **Tick**: The main control loop runs every 60 seconds
- **OFF Window**: Nightly forced-off period (default 01:00–06:00) when the pump must not run
- **Demand**: A room needs heating when its current temperature falls below the user setpoint minus hysteresis
- **Floor Exclusivity**: Only one floor (GF or FF) can heat at a time — never both
- **LERP**: The number of rooms heated simultaneously scales linearly with outdoor temperature
- **DHW Quota**: When no rooms need heating, the pump may still run to meet a daily minimum run-time for domestic hot water
- **Cooldown**: Rooms that have been heating continuously for too long are forced into a cooldown period
- **Automation Guard**: Prevents automation-driven setpoint changes from being recorded as user changes
