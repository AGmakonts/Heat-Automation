# Test Suite Design & CI Plan — Heat Orchestrator

Role: QA Architect
Date: 2026-06-17
Status: Proposed (no test code written yet)

---

## 1. Objectives & philosophy

This codebase is developed exclusively by AI coding agents and controls real
hardware (a heat pump, valves, hot water). Tests must therefore serve three
purposes, in priority order:

1. **Executable specification.** Encode every *intended behaviour* from
   `home-assistant-heat-orchestrator-spec.md` so the suite is the contract an
   agent must satisfy. A green suite = the spec holds.
2. **Regression guardrail for agents.** CI must block any change that violates a
   behaviour or a safety invariant, so an agent cannot "fix" one thing and
   silently break another (e.g. the recent `salon_2` / `user_sp_salon` split).
3. **Living documentation.** Test names and structure map 1:1 to spec sections
   so a human reviewer can read the suite as prose.

### Design stance: behaviour-first, not unit-first

We test **observable behaviour through the real control logic**, not private
methods in isolation. The orchestrator's behaviour *is* its side effects on
Home Assistant entities, so we run the actual `HeatOrchestrator` against a
simulated HA and assert on outcomes. Private-method unit tests are reserved only
for pure, branch-heavy functions (LERP, off-window math) where direct testing is
cheaper and clearer. This avoids tests that merely mirror the implementation —
the failure mode that makes AI-generated test suites worthless.

---

## 2. Testing strategy — the layers (a "trophy", not a pyramid)

Weighted toward the middle (behaviour) rather than the bottom (units):

| Layer | Weight | What it covers | Why |
|---|---|---|---|
| **Contract / wiring** | small but critical | Every entity id the code reads/writes exists in the helpers package; pump is never turned off via `switch.turn_off`; service payloads are well-formed | Catches the entire *class* of bug we just fixed, statically and cheaply |
| **Behaviour / acceptance** | **largest** | Each spec rule and FSM transition driven through the real orchestrator against the HA simulator | The core executable spec |
| **Scenario / integration** | large | Multi-tick simulations over virtual time (full heat cycle, off-window entry/exit, quota fill, floor switch with anti-oscillation, forced cooldown) | Catches time-dependent and state-accumulation bugs |
| **Invariant / property** | medium | Safety properties that must hold in *all* states (floor exclusivity, off-window lockout, user_sp never silently → 7°C, LERP monotonicity) | Generative coverage of states humans won't enumerate |
| **Focused unit** | small | Pure functions: `_lerp_max_rooms`, `_in_off_window`, scoring, `_user_sp_entity`, datetime round-trip | Cheap, exhaustive branch coverage where it pays |

---

## 3. Test architecture & harness

### 3.1 The core enabler — a Home Assistant simulator

`HeatOrchestrator(hass.Hass)` binds to its base class at import time, so we
inject a **fake `hassapi` module** into `sys.modules` *before* importing the app
(done in `conftest.py`). The fake's `Hass` class is our simulator, so the real
orchestrator transparently runs against it.

The simulator (`tests/harness/hass_sim.py`) provides the full AppDaemon surface
the app uses:

- **State store** — `dict[entity_id -> {state, attributes}]`. `get_state(entity,
  attribute=...)` reads it.
- **Service router** — `call_service(domain/service, **kwargs)` mutates the
  store so reads reflect writes (true behaviour, not call-spying):
  - `input_number/set_value`, `input_text/set_value`,
    `input_datetime/set_datetime`
  - `input_boolean/turn_on|turn_off`, `switch/turn_on`, `input_button/press`
  - `climate/set_temperature` (writes the `temperature` attribute)
  - `weather/get_forecasts` (returns a configurable forecast structure)
  - Every call is also appended to a `service_log` for assertions and for the
    "forbidden call" invariant.
- **Virtual clock** — `datetime()` returns a test-controlled `now`;
  `clock.advance(minutes=n)` moves time and (optionally) fires due `run_every` /
  `run_daily` / `run_in` callbacks.
- **Scheduler stubs** — `run_every`, `run_daily`, `run_in` register callbacks;
  tests either advance the clock to fire them or invoke `_tick()` directly.
  `run_in` is what makes the automation-guard release testable.
- **Listener registry** — `listen_state` records handlers keyed by entity +
  attribute; `sim.set_state(entity, ...)` triggers the matching callback with
  `old`/`new`, so we can simulate manual thermostat changes and TRV
  offline/online edges.
- **Log capture** — `log(msg, level=...)` stored for `[DECISION]` / `[ROOM]` /
  `[PUMP]` assertions.
- `entity_exists()` backed by the store.

This is a deliberate, single, well-understood test double. We do **not** mock
`get_state`/`call_service` per-test — that would couple tests to the
implementation.

### 3.2 Fixtures & builders (`tests/harness/`)

- `world` fixture: fresh simulator + virtual clock.
- `helpers_from_yaml`: parse `packages/heat_orchestrator_helpers.yaml` and seed
  every helper at its `initial` (or a sensible default). **Single source of
  truth** — keeps fixtures honest and feeds the contract test.
- `room` builder: set `climate.<room>` current_temperature/temperature, priority,
  user_sp, heating boolean in one call.
- `orchestrator` fixture: instantiate `HeatOrchestrator`, run `initialize()`,
  return it wired to `world`.
- A tiny **Given/When/Then DSL** (`given_room_cold(...)`, `when_tick()`,
  `then_pump_is_on()`, `then_room_enabled(...)`) so acceptance tests read like the
  spec. (Option: `pytest-bdd` with Gherkin `.feature` files for the §13
  acceptance criteria if we want human-readable feature files; recommended only
  for the top-level acceptance set to avoid harness sprawl.)

---

## 4. Proposed layout

```
tests/
  harness/
    __init__.py
    hass_sim.py          # the HA simulator (fake hassapi.Hass)
    clock.py             # virtual clock + scheduler
    builders.py          # room/state/forecast builders
    dsl.py               # given/when/then helpers
  conftest.py            # inject fake hassapi; expose fixtures
  contract/
    test_entity_wiring.py
    test_pump_safety_calls.py
  unit/
    test_lerp.py
    test_off_window.py
    test_scoring.py
    test_user_sp_override.py
    test_datetime_roundtrip.py
  behaviour/
    test_setpoint_memory.py        # spec §4.1, §10
    test_automation_guard.py       # spec §4.2
    test_floor_exclusivity.py      # spec §4.3
    test_off_window.py             # spec §4.4, §9.2
    test_demand_model.py           # spec §5
    test_lerp_room_count.py        # spec §6.2
    test_max_continuous_heating.py # spec §6.3
    test_dhw_quota.py              # spec §7
    test_floor_room_selection.py   # spec §8
    test_pump_control.py           # spec §9.3, §9.4
    test_thermostat_adapter.py     # spec §10
    test_resilience.py             # spec §11.6
    test_fsm_transitions.py        # spec §12
    test_bootstrap_and_reset.py    # init seeding, §11.3 daily reset
    test_diagnostics.py            # active_floor / active_rooms
    test_weather_fallback.py       # §6.1 fallback chain
  scenario/
    test_full_heating_cycle.py
    test_night_lockout_cycle.py
    test_quota_then_off.py
    test_floor_handover.py
  invariant/
    test_safety_invariants.py      # hypothesis-driven
  features/                        # optional pytest-bdd
    acceptance.feature
```

---

## 5. Behaviour catalogue (mapped to spec)

Each row becomes one or more tests; markers `@pytest.mark.spec("4.1")` give
traceability and let CI emit a coverage-vs-spec matrix.

| Spec | Behaviour | Representative assertions |
|---|---|---|
| §4.1 / §10.1-10.2 | **User setpoint memory** | Manual 22°C → `user_sp=22`; disable parks `climate` at 7°C but `user_sp` unchanged; re-enable commands `22 + overshoot` (capped 30) |
| §4.2 | **Automation guard** | Setpoint change during `automation_guard=True` is *not* written back to `user_sp` |
| §4.2 (bug) | **Guard release latency edge** | After `_disable_room`, a delayed callback must **not** overwrite `user_sp` with 7°C — *expected to fail today; pins the open 🟠 finding* |
| §4.3 | **Floor exclusivity** | After any tick, never are both a GF room and an FF room enabled |
| §4.4 / §9.2 | **Off-window** | At 01:00 pump turns off only after `min_pump_on`; no start in 01:00–06:00; state `OFF_LOCKOUT` |
| §5 | **Demand model** | `need_heat` at `Tcur < Tuser-hyst_on`; `satisfied` at `Tcur ≥ Tuser+hyst_off`; heating room keeps demand until satisfied (hysteresis band, `_has_demand`) |
| §6.1 | **Weather fallback** | attribute → `get_forecasts` → last-known → `0.0` + WARN log, each branch |
| §6.2 | **LERP room count** | `t≤min→r_min`, `t≥max→r_max`, midpoint floors down, `t_min≥t_max` degenerate→`r_min`, `r_max<r_min` swap, clamp to floor size & candidate count |
| §6.3 | **Max continuous heating** | At `heating_minutes ≥ max`: room excluded, enters cooldown = `min_state_duration`, counter reset, WARN log; counter survives a simulated AppDaemon restart (persisted helper); reset on cooldown/floor-switch/disable-all/daily-reset but **not** on plain `_disable_room` |
| §7 | **DHW quota** | No demand + `remaining>0` + outside off-window → `DHW_QUOTA`, all rooms 7°C, pump on; `+1/min` accounting; quota→0 then OFF after `min_pump_on` |
| §8 | **Floor & room selection** | Floor picked by max `deficit×priority`; rooms sorted priority desc then deficit desc; switch blocked before `min_state_duration`; switch when higher score; switch when no selectable rooms on active floor but other floor has them |
| §9.3/§9.4 | **Pump on/off** | ON needs `min_pump_off` elapsed + (demand or quota); OFF needs `min_pump_on` elapsed + no demand + quota 0; ON via `switch.turn_on`, OFF via `input_button.press`, `pump_starts_today++`, timestamps set |
| §10.3 | **Manual-change listener** | Heating room: stored = `new − overshoot`; non-heating: stored as-is; out-of-range ignored; `old in (None/unknown/unavailable)` recovery edge ignored |
| §11.6 | **Resilience** | `current_temperature=None` → room excluded from demand (+WARN); `set_temperature` raises → retried once → on second failure room flagged `unmanaged` for 15 min |
| §11.3 | **Daily reset** | Zeroes `pump_on_minutes_today`/`pump_starts_today`, clears cooldowns & heating minutes |
| §12.2 | **FSM transition priority** | Ordering OFF-window → quota → demand honoured; correct state labels persisted with `state_since` |
| bootstrap | **First-run seeding** | Empty `user_sp` seeded from climate setpoint if 5–30, else 21.0 |
| diag | **Diagnostics** | `active_floor`/`active_rooms` updated when present; absence tolerated (no crash) |

---

## 6. Safety invariants (property-based, `hypothesis`)

Generate randomized entity states / configs / clock times; after a tick, assert:

1. **Floor exclusivity** — enabled GF rooms and enabled FF rooms are never both non-empty.
2. **Off-window lockout** — inside the window, pump is never *started*.
3. **Setpoint integrity** — `user_sp` is never written to `room_off_setpoint` as a result of automation-driven thermostat moves.
4. **Pump-off channel** — `switch.turn_off` is never called for the pump (off only via `input_button.press`).
5. **LERP monotonicity & bounds** — non-decreasing in `t_out`, always in `[r_min, r_max]` and `≤ floor room count`.
6. **No unmanaged crash** — any combination of `unknown`/`unavailable`/`None` states completes a tick without raising.

---

## 7. Contract / wiring tests (highest ROI, lowest cost)

- **Entity wiring** — derive every `input_*` entity id the code can touch
  (`ALL_ROOMS × {user_sp(+override), priority, heating, heating_minutes}` + all
  static globals + diagnostics), then assert each is declared in
  `heat_orchestrator_helpers.yaml`. *This test fails on the exact `salon_2` /
  `user_sp_salon` mismatch we hit* — making that whole bug class impossible to
  reintroduce. External domains (`climate.`, `switch.`, `weather.`,
  `input_button.`) are checked against a documented allowlist, not the YAML.
- **Pump-safety calls** — scan/behaviourally confirm the pump is only ever
  enabled via `switch.sonoff_10017fadeb` and disabled via
  `input_button.wylacznik_pompy`.
- **YAML sanity** — helpers file parses; setpoint helpers have `min/max/step`
  consistent with the code's 5–30 clamping.

---

## 8. Tooling & dependencies (`requirements-dev.txt`)

- `pytest`, `pytest-cov` — runner + coverage
- `hypothesis` — property/invariant tests
- `pyyaml` — parse the helpers package for fixtures + contract tests
- `freezegun` *optional* — only if the virtual clock proves insufficient (prefer our own clock to keep control explicit)
- `ruff` — lint + format (fast, single tool)
- `mypy` — type-check (code already uses annotations + `from __future__ import annotations`)
- `pytest-bdd` *optional* — Gherkin for the §13 acceptance feature file
- `pre-commit` *optional* — run ruff/mypy locally before commit

No production deps change; `hassapi` is only ever the test fake, never installed.

---

## 9. CI design (GitHub Actions)

`.github/workflows/tests.yml`:

```yaml
name: tests
on: [push, pull_request]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps: [checkout, setup-python@3.12, pip install ruff mypy,
            ruff check ., ruff format --check ., mypy apps/]
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python: ["3.10", "3.11", "3.12", "3.13"]
    steps:
      - checkout
      - setup-python {{ matrix.python }}
      - pip install -r requirements-dev.txt
      - pytest --cov=apps/heat_orchestrator --cov-report=xml
               --cov-fail-under=85 -q
      - upload coverage artifact
```

Design notes:
- **Python matrix includes 3.10 deliberately.** `datetime.fromisoformat` only
  accepts the space-separated format the code writes from 3.11+. The
  `test_datetime_roundtrip` unit test will fail on 3.10 — surfacing the latent
  bug in `_get_state_since` / `_minutes_since`. CI thus *documents* the minimum
  supported runtime and forces a conscious decision (pin runtime or fix the
  parser).
- **Gates:** lint + types must pass; coverage gate `--cov-fail-under=85` as a
  backstop (behaviour matrix is the real target, see §10). Branch protection
  requires the `test` and `lint` checks before merge — the agent guardrail.
- **PR annotations:** publish the spec-coverage matrix (which §IDs are tested)
  as a job summary so reviewers see traceability at a glance.

`.github/workflows/` is also a good home for a scheduled (cron) run to catch
environment drift.

### Web-session enablement

Add a **SessionStart hook** (per the `session-start-hook` skill) so Claude Code
web sessions auto-install `requirements-dev.txt` and can run `pytest`/`ruff`
during development without manual setup.

---

## 10. Coverage & traceability strategy

- **Behaviour matrix is the primary metric**, not line %. Every spec section in
  §5 maps to ≥1 `@pytest.mark.spec("X")` test; a small CI script asserts no spec
  ID is missing and prints the matrix.
- **Line coverage gate (85%)** is only a backstop against dead paths.
- **Known-bug tests** (guard latency, datetime/3.10) are tagged
  `@pytest.mark.xfail(strict=True, reason="open finding #N")` until fixed, then
  flip to passing — turning the audit findings into TDD targets with a visible
  countdown.

---

## 11. Phased rollout

1. **M1 — Harness + contract (foundation).** Simulator, clock, fixtures,
   conftest injection, entity-wiring + pump-safety contract tests, CI skeleton
   (lint + test on 3.12). Immediate value: locks the wiring.
2. **M2 — Core behaviour.** Demand model, pump control, floor exclusivity,
   off-window, thermostat adapter, setpoint memory, LERP. Add full Python matrix.
3. **M3 — Stateful behaviour & scenarios.** Quota, max-continuous-heating
   cooldown, floor switching/anti-oscillation, daily reset, multi-tick scenario
   tests, virtual-clock scheduler firing.
4. **M4 — Invariants & resilience.** Hypothesis safety properties, fault
   injection, weather fallback, diagnostics, recovery edges.
5. **M5 — Hardening.** Coverage gate to 85%, spec-matrix CI check, optional
   pytest-bdd acceptance feature, SessionStart hook, pre-commit, flip xfail bugs.

---

## 12. Risks & mitigations

- **Simulator fidelity drift** — the fake could diverge from real HA semantics.
  *Mitigation:* keep the simulator minimal and model only documented HA service
  contracts; seed helpers from the real YAML; review the simulator as production
  code.
- **Over-specified tests mirroring implementation** — would re-break on every
  refactor and give false confidence. *Mitigation:* assert on observable
  outcomes (entity state, FSM, logs), forbid asserting private call sequences
  except in the pump-safety contract test.
- **Flaky time** — *Mitigation:* fully virtual clock; never real `sleep`/wall
  clock.
- **AI agents writing tests that assert current (buggy) behaviour** —
  *Mitigation:* tests are derived from the **spec**, not the code; the spec is
  the oracle, and contract/invariant tests are written independently of
  implementation details.

---

## 13. Open findings this suite will pin (TDD targets)

- 🟠 Automation-guard release is time-based (2 s); a delayed callback can write
  `user_sp = 7°C`. → `behaviour/test_automation_guard.py::test_delayed_callback_does_not_corrupt_user_sp` (xfail until fixed).
- 🟡 `datetime.fromisoformat` space-separator needs Python ≥3.11. →
  `unit/test_datetime_roundtrip.py` + 3.10 CI matrix entry.
- 🟡 Redundant `_get_outdoor_temp()` calls per tick. → assert `weather/get_forecasts`
  is invoked at most once per tick (efficiency regression guard).
- 🟡 `day_reset_time` only read at `initialize()`. → documented; scenario test
  asserts current behaviour and flags the limitation.
