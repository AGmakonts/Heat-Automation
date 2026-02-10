# Heat Orchestrator – Step-by-Step Setup Guide

## Prerequisites

- Home Assistant instance running (HAOS, Docker, or Core)
- AppDaemon 4.x add-on / installation
- The following integrations already configured:
  - Climate entities for all thermostats (`climate.gabinet_ani`, `climate.lazienka_parter`, `climate.salon`, `climate.sypialnia`, `climate.lazienka_pietro`, `climate.pokoj_z_oknem_naroznym`, `climate.pokoj_z_tarasem`)
  - Pump switch: `switch.sonoff_10017fadeb`
  - Pump OFF button: `input_button.wylacznik_pompy` (must exist before starting)
  - Weather: `weather.forecast_home` (Met.no integration)

---

## Step 1: Create the Input Button (if it doesn't exist)

If `input_button.wylacznik_pompy` doesn't already exist, create it:

1. Go to **Settings → Devices & services → Helpers**
2. Click **+ Create Helper**
3. Choose **Button**
4. Name: `Wyłącznik Pompy`
5. Entity ID will be `input_button.wylacznik_pompy`
6. Click **Create**

> **Note:** This button should trigger an automation or script that performs the pump's graceful shutdown sequence.

---

## Step 2: Install the Helpers (Package)

### Option A: Using Packages (recommended)

1. Open your Home Assistant config directory (e.g., `/config/` or via File Editor / VS Code add-on)

2. Create a `packages` folder if it doesn't exist:
   ```
   /config/packages/
   ```

3. Copy the file `packages/heat_orchestrator_helpers.yaml` from this repository into:
   ```
   /config/packages/heat_orchestrator_helpers.yaml
   ```

4. Edit `/config/configuration.yaml` and add the packages directive (if not already present):
   ```yaml
   homeassistant:
     packages:
       heat_orchestrator: !include packages/heat_orchestrator_helpers.yaml
   ```

5. **Restart Home Assistant** (Settings → System → Restart)

6. After restart, verify the helpers exist:
   - Go to **Settings → Devices & services → Helpers**
   - You should see all `user_sp_*`, `priority_*`, and global parameter helpers

### Option B: Create Helpers Manually via UI

If you prefer not to use packages, create each helper manually through the UI:

**Settings → Devices & services → Helpers → + Create Helper**

For each room (`gabinet_ani`, `lazienka_parter`, `salon`, `sypialnia`, `lazienka_pietro`, `pokoj_z_oknem_naroznym`, `pokoj_z_tarasem`):

1. **Number** – `user_sp_<room_id>` (range 5–30, step 0.5, unit °C)
2. **Number** – `priority_<room_id>` (range 1–100, step 1)

Then create global helpers:

| Type | Entity ID | Range | Step | Initial | Unit |
|------|-----------|-------|------|---------|------|
| Number | `room_off_setpoint` | 5–15 | 0.5 | 7 | °C |
| Number | `heating_hyst_on` | 0.1–2.0 | 0.1 | 0.3 | °C |
| Number | `heating_hyst_off` | 0.05–1.0 | 0.05 | 0.2 | °C |
| Number | `min_state_duration_min` | 5–120 | 5 | 25 | min |
| Number | `min_pump_on_min` | 5–120 | 5 | 40 | min |
| Number | `min_pump_off_min` | 5–120 | 5 | 25 | min |
| Number | `dhw_min_run_hours` | 0–12 | 0.5 | 3.5 | h |
| Number | `pump_on_minutes_today` | 0–1440 | 1 | 0 | min |
| Number | `pump_starts_today` | 0–100 | 1 | 0 | – |
| Number | `bulk_mode_temp` | -20–20 | 1 | 5 | °C |
| Number | `sequential_mode_temp` | -30–10 | 1 | -5 | °C |
| Number | `max_rooms_limited` | 1–7 | 1 | 2 | – |
| Number | `max_continuous_heating_min` | 30–480 | 15 | 120 | min |
| Number | `lerp_temp_min` | -30–10 | 1 | -10 | °C |
| Number | `lerp_temp_max` | -10–30 | 1 | 10 | °C |
| Number | `lerp_rooms_min` | 1–3 | 1 | 1 | – |
| Number | `lerp_rooms_max` | 1–7 | 1 | 5 | – |
| DateTime (time only) | `off_window_start` | – | – | 01:00 | – |
| DateTime (time only) | `off_window_end` | – | – | 06:00 | – |
| DateTime (time only) | `day_reset_time` | – | – | 00:00 | – |
| DateTime (date+time) | `state_since` | – | – | – | – |
| DateTime (date+time) | `last_pump_on` | – | – | – | – |
| DateTime (date+time) | `last_pump_off` | – | – | – | – |
| Text | `heat_state` | – | – | OFF | – |
| Text | `active_floor` | – | – | none | – |
| Text | `active_rooms` | – | – | (empty) | – |

---

## Step 3: Set Initial Values for Helpers

After the helpers are created:

1. Go to **Developer Tools → States** (or the Helpers page)
2. Set the OFF window times:
   - `input_datetime.off_window_start` → `01:00:00`
   - `input_datetime.off_window_end` → `06:00:00`
   - `input_datetime.day_reset_time` → `00:00:00`
3. Set priorities for each room (default 50, higher = more important):
   - `input_number.priority_salon` → e.g., `80`
   - `input_number.priority_sypialnia` → e.g., `90`
   - etc.
4. Verify `input_number.room_off_setpoint` = `7.0`

---

## Step 4: Install AppDaemon

### For HAOS (Home Assistant OS):

1. Go to **Settings → Add-ons → Add-on Store**
2. Search for **AppDaemon**
3. Click **Install**
4. After installation, go to the **Configuration** tab
5. Ensure Python packages are empty (no extra packages needed)
6. Click **Start**
7. Enable **Start on boot** and **Watchdog**

### For Docker / Core:

Follow the [official AppDaemon installation docs](https://appdaemon.readthedocs.io/en/latest/INSTALL.html).

---

## Step 5: Configure AppDaemon

1. Navigate to the AppDaemon config directory:
   - HAOS: `/addon_configs/a0d7b954_appdaemon/`  
     (accessible via File Editor, VS Code add-on, or Samba)
   - Docker: your mapped `conf` directory

2. Verify `appdaemon.yaml` has the HASS plugin configured:
   ```yaml
   appdaemon:
     latitude: !secret latitude
     longitude: !secret longitude
     elevation: !secret elevation
     time_zone: Europe/Warsaw
     plugins:
       HASS:
         type: hass
         ha_url: "http://homeassistant.local:8123"
         token: !secret ha_token
   ```

   > **Getting a token:** Go to your HA profile (click your name bottom-left) → **Long-Lived Access Tokens** → Create Token. Copy it to `secrets.yaml`:
   > ```yaml
   > ha_token: "eyJ0eXAi..."
   > ```

3. Make sure the `apps` directory exists:
   ```
   appdaemon_config/
   ├── appdaemon.yaml
   ├── secrets.yaml
   └── apps/
       ├── heat_orchestrator.py
       └── apps.yaml
   ```

   > **Important:** Place both files directly in the `apps/` folder — do **not** put them inside a `heat_orchestrator/` subdirectory. AppDaemon would treat the directory name as a Python package and fail to find the class.

---

## Step 6: Deploy the App

1. Copy the two files from this repository into your AppDaemon apps directory:

   ```
   apps/heat_orchestrator/heat_orchestrator.py  →  <appdaemon_config>/apps/heat_orchestrator.py
   apps/heat_orchestrator/apps.yaml             →  <appdaemon_config>/apps/apps.yaml
   ```

   > **Note:** The files in this repo are under `apps/heat_orchestrator/` for organisation, but on AppDaemon they must sit directly in `apps/`.

2. AppDaemon will automatically detect the new files and load the app.

---

## Step 7: Verify It Works

### Check AppDaemon logs:

- **HAOS:** Go to the AppDaemon add-on → **Log** tab  
- **Docker:** `docker logs appdaemon` or check `appdaemon.log`

You should see:
```
INFO heat_orchestrator: === HeatOrchestrator initializing ===
INFO heat_orchestrator: [BOOTSTRAP] input_number.user_sp_salon seeded with 22.0
INFO heat_orchestrator: === HeatOrchestrator ready ===
INFO heat_orchestrator: [DECISION] state=OFF reason=no_demand_no_quota Tout=3.5 quota_remaining=210
```

### Check Home Assistant:

1. Go to **Developer Tools → States**
2. Search for `input_text.heat_state` – it should show the FSM state
3. Search for `input_text.active_floor` – shows which floor is active
4. Search for `input_number.pump_on_minutes_today` – pump runtime counter

---

## Step 8: Fine-tune Parameters

All parameters are adjustable live via the UI without restarting anything:

| Parameter | What it does | Where to change |
|-----------|-------------|-----------------|
| `input_number.heating_hyst_on` | Temperature drop below setpoint to trigger heating | Helpers page |
| `input_number.heating_hyst_off` | How close to setpoint counts as "satisfied" | Helpers page |
| `input_number.min_state_duration_min` | Min minutes before switching floors | Helpers page |
| `input_number.min_pump_on_min` | Min pump run before allowing shutdown | Helpers page |
| `input_number.min_pump_off_min` | Cooldown period after pump stops | Helpers page |
| `input_number.dhw_min_run_hours` | Daily pump quota for hot water | Helpers page |
| `input_number.bulk_mode_temp` | Above this outdoor temp → heat all demanding rooms | Helpers page |
| `input_number.sequential_mode_temp` | Below this outdoor temp → heat only 1 room | Helpers page |
| `input_number.max_rooms_limited` | Number of rooms in "limited" mode | Helpers page |
| `input_number.priority_<room>` | Room priority (higher = heated first) | Helpers page |

---

## Step 9: Create a Dashboard Card (Optional)

Add an **Entities card** to your Lovelace dashboard for monitoring:

```yaml
type: entities
title: Heat Orchestrator
entities:
  - entity: input_text.heat_state
    name: State
  - entity: input_text.active_floor
    name: Active Floor
  - entity: input_text.active_rooms
    name: Active Rooms
  - entity: input_number.pump_on_minutes_today
    name: Pump Minutes Today
  - entity: input_number.pump_starts_today
    name: Pump Starts Today
  - entity: input_datetime.last_pump_on
    name: Last Pump ON
  - entity: input_datetime.last_pump_off
    name: Last Pump OFF
  - type: divider
  - entity: input_boolean.heating_gabinet_ani
    name: Heating Gabinet Ani
  - entity: input_boolean.heating_lazienka_parter
    name: Heating Łazienka Parter
  - entity: input_boolean.heating_salon
    name: Heating Salon
  - entity: input_boolean.heating_sypialnia
    name: Heating Sypialnia
  - entity: input_boolean.heating_lazienka_pietro
    name: Heating Łazienka Piętro
  - entity: input_boolean.heating_pokoj_z_oknem_naroznym
    name: Heating Pokój z oknem narożnym
  - entity: input_boolean.heating_pokoj_z_tarasem
    name: Heating Pokój z tarasem
  - type: divider
  - entity: input_number.user_sp_salon
  - entity: input_number.user_sp_sypialnia
  - entity: input_number.user_sp_gabinet_ani
  - entity: input_number.user_sp_lazienka_parter
  - entity: input_number.user_sp_lazienka_pietro
  - entity: input_number.user_sp_pokoj_z_oknem_naroznym
  - entity: input_number.user_sp_pokoj_z_tarasem
  - type: divider
  - entity: input_number.heating_hyst_on
  - entity: input_number.heating_hyst_off
  - entity: input_number.min_state_duration_min
  - entity: input_number.min_pump_on_min
  - entity: input_number.min_pump_off_min
  - entity: input_number.dhw_min_run_hours
  - entity: input_number.bulk_mode_temp
  - entity: input_number.sequential_mode_temp
  - entity: input_number.max_rooms_limited
```

---

## Troubleshooting

### App doesn't start
- Check the AppDaemon **error log** (separate from main log)
- Ensure the HASS plugin token is valid
- Verify all helper entities exist (the app handles missing entities gracefully but logs warnings)

### Pump doesn't turn on
- Verify `switch.sonoff_10017fadeb` is available and controllable
- Check if you're inside the OFF window (01:00–06:00)
- Check `input_number.min_pump_off_min` cooldown hasn't elapsed yet

### Pump doesn't turn off
- The pump OFF uses `input_button.wylacznik_pompy` – make sure it triggers your graceful shutdown automation
- Check `input_number.min_pump_on_min` – the pump won't stop until this minimum is met

### User setpoints are lost
- Check the AppDaemon log for `[USER]` entries to verify manual changes are detected
- Make sure the thermostat change happens *after* the automation guard is released (2s delay)

### Both floors heating simultaneously
- This should never happen. If it does, check the log for errors. The app enforces GF XOR FF at every tick.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                   AppDaemon                          │
│  ┌─────────────────────────────────────────────────┐ │
│  │           HeatOrchestrator (FSM)                │ │
│  │                                                 │ │
│  │  States: OFF_LOCKOUT │ OFF │ HEAT_GF │          │ │
│  │          HEAT_FF │ DHW_QUOTA                    │ │
│  │                                                 │ │
│  │  Every 60s tick:                                │ │
│  │   1. Check OFF window                           │ │
│  │   2. Calculate demand per room / floor          │ │
│  │   3. Select active floor (highest score)        │ │
│  │   4. Select rooms (bulk/limited/sequential)     │ │
│  │   5. Enable/disable rooms via thermostats       │ │
│  │   6. Control pump ON/OFF                        │ │
│  │   7. Handle DHW quota                           │ │
│  └─────────────────────────────────────────────────┘ │
└──────────────┬──────────────────────┬────────────────┘
               │                      │
    ┌──────────▼──────────┐  ┌───────▼────────────┐
    │   Home Assistant    │  │   climate.*         │
    │   Helpers           │  │   (thermostats)     │
    │   (input_number,    │  │                     │
    │    input_datetime,  │  │   switch.sonoff_*   │
    │    input_text)      │  │   (pump ON)         │
    │                     │  │                     │
    │                     │  │   input_button.*    │
    │                     │  │   (pump OFF)        │
    └─────────────────────┘  └────────────────────┘
```
