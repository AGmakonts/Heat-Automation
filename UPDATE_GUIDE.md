# Heat Orchestrator – Update Guide

This document describes changes introduced after the initial release and how to apply them to a running installation.

---

## Update 2026-02-10: Per-Room Heating Status Sensors

### What changed

**New feature:** Each room now has an `input_boolean.heating_<room_id>` entity that tracks whether the room is currently being heated (on) or idle (off). These entities are recorded in Home Assistant history, enabling you to plot heating periods per room on dashboard charts.

**New entities (7):**

| Entity ID | Description |
|-----------|-------------|
| `input_boolean.heating_gabinet_ani` | Heating status – Gabinet Ani |
| `input_boolean.heating_lazienka_parter` | Heating status – Łazienka Parter |
| `input_boolean.heating_salon` | Heating status – Salon |
| `input_boolean.heating_sypialnia` | Heating status – Sypialnia |
| `input_boolean.heating_lazienka_pietro` | Heating status – Łazienka Piętro |
| `input_boolean.heating_pokoj_z_oknem_naroznym` | Heating status – Pokój z oknem narożnym |
| `input_boolean.heating_pokoj_z_tarasem` | Heating status – Pokój z tarasem |

**Files modified:**

| File | Change |
|------|--------|
| `packages/heat_orchestrator_helpers.yaml` | Added `input_boolean` section with 7 heating status entities |
| `apps/heat_orchestrator/heat_orchestrator.py` | Added `HEATING_PREFIX` constant, `_set_heating_sensor()` method, calls in `_enable_room()` and `_disable_room()` |
| `SETUP_GUIDE.md` | Added heating sensors to the dashboard card YAML |

### How to apply

#### Step 1: Update the helpers package

Copy the updated `packages/heat_orchestrator_helpers.yaml` to your Home Assistant config:

```
/config/packages/heat_orchestrator_helpers.yaml
```

If you created helpers manually (Option B in the Setup Guide), create 7 new **Toggle** helpers via the UI instead:

1. Go to **Settings → Devices & services → Helpers**
2. Click **+ Create Helper → Toggle**
3. For each room, create a toggle named `heating_<room_id>` (e.g., `heating_salon`)
4. The entity ID will be `input_boolean.heating_<room_id>`

#### Step 2: Restart Home Assistant

Go to **Settings → System → Restart** so the new `input_boolean` entities are created.

After restart, verify they exist:
- Go to **Developer Tools → States**
- Search for `input_boolean.heating_` — you should see all 7 entities

#### Step 3: Update the AppDaemon app

Copy the updated `heat_orchestrator.py` to your AppDaemon apps directory:

```
<appdaemon_config>/apps/heat_orchestrator.py
```

> **Reminder:** The file goes directly in the `apps/` folder, **not** inside an `apps/heat_orchestrator/` subdirectory.

AppDaemon will auto-detect the file change and reload the app. Check the logs for:

```
INFO heat_orchestrator: === HeatOrchestrator initializing ===
INFO heat_orchestrator: === HeatOrchestrator ready ===
```

#### Step 4: Update your dashboard (optional)

Add the heating sensors to your Lovelace dashboard. You can use an **Entities card**:

```yaml
type: entities
title: Room Heating Status
entities:
  - entity: input_boolean.heating_gabinet_ani
    name: Gabinet Ani
  - entity: input_boolean.heating_lazienka_parter
    name: Łazienka Parter
  - entity: input_boolean.heating_salon
    name: Salon
  - entity: input_boolean.heating_sypialnia
    name: Sypialnia
  - entity: input_boolean.heating_lazienka_pietro
    name: Łazienka Piętro
  - entity: input_boolean.heating_pokoj_z_oknem_naroznym
    name: Pokój z oknem narożnym
  - entity: input_boolean.heating_pokoj_z_tarasem
    name: Pokój z tarasem
```

Or use a **History Graph card** to visualize heating periods over time:

```yaml
type: history-graph
title: Heating History
hours_to_show: 24
entities:
  - entity: input_boolean.heating_gabinet_ani
    name: Gabinet Ani
  - entity: input_boolean.heating_lazienka_parter
    name: Łazienka Parter
  - entity: input_boolean.heating_salon
    name: Salon
  - entity: input_boolean.heating_sypialnia
    name: Sypialnia
  - entity: input_boolean.heating_lazienka_pietro
    name: Łazienka Piętro
  - entity: input_boolean.heating_pokoj_z_oknem_naroznym
    name: Pokój z oknem narożnym
  - entity: input_boolean.heating_pokoj_z_tarasem
    name: Pokój z tarasem
```

---

## Update 2026-02-10: AppDaemon Import Fix

### What changed

The Python import was changed from `from appdaemon.plugins.hass import Hass` to `import hassapi as hass` for compatibility with AppDaemon 4.x. The `from __future__ import annotations` import was also added for Python 3.9 compatibility with modern type hints (`float | None`, `dict[str, bool]`).

### How to apply

This fix is included in the updated `heat_orchestrator.py`. If you already applied the file from the step above, no additional action is needed.

---

## Update 2026-02-10: Directory Structure Fix

### What changed

The Setup Guide previously instructed placing files in `apps/heat_orchestrator/heat_orchestrator.py`. This caused AppDaemon to treat the `heat_orchestrator/` directory as a Python package, preventing it from finding the `HeatOrchestrator` class.

**Correct structure:**
```
<appdaemon_config>/
├── appdaemon.yaml
├── secrets.yaml
└── apps/
    ├── heat_orchestrator.py   ← directly in apps/
    └── apps.yaml              ← directly in apps/
```

### How to apply

If you previously placed the files inside `apps/heat_orchestrator/`:

1. Move both files up one level into `apps/`
2. Delete the empty `heat_orchestrator/` subdirectory
3. AppDaemon will auto-reload

---

## General Update Procedure

For any future updates to this project:

1. **Check this file** for new update entries
2. **Update helpers YAML** → Restart Home Assistant
3. **Update Python app** → AppDaemon auto-reloads (check logs)
4. **Update dashboard** → Edit Lovelace manually if needed
