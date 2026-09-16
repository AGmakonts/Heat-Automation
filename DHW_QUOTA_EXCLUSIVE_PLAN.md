# Plan implementacji: DHW Quota Exclusive — duty-cycle grzania wody

## 1. Problem

W okresie letnim pokoje nie zgłaszają zapotrzebowania na ciepło (`has_demand == False`),
więc jedynym powodem pracy pompy jest kwota DHW (`dhw_min_run_hours`, domyślnie 3,5 h).

Obecna logika w `_tick()` (`apps/heat_orchestrator/heat_orchestrator.py`):

- zaraz po zakończeniu off window (np. 6:00) pompa startuje w stanie `DHW_QUOTA`
  (`remaining_quota > 0`, `cooldown_ok`),
- pracuje **nieprzerwanie**, aż `pump_on_minutes_today` wyczerpie całą kwotę
  (np. 6:00–9:30),
- przez resztę dnia pompa stoi, a woda po wielu cyklach cyrkulacji stygnie —
  grzanie wody odbywa się efektywnie tylko rano.

## 2. Cel

Gdy grzanie wody (DHW quota) jest **jedynym** powodem pracy pompy, rozłożyć
wykorzystanie kwoty równomiernie w ciągu dnia przez pracę cykliczną:
maksymalnie `max duration` ciągłej pracy, potem przerwa `pause duration`,
i tak aż do wyczerpania kwoty dziennej.

Definicja „jedynego powodu": stan `DHW_QUOTA` jest z konstrukcji ekskluzywny —
FSM wchodzi w niego wyłącznie gdy `has_demand == False`. Gdy pokoje są grzane,
kwota i tak nalicza się przez `pump_on_minutes_today`, więc nowa logika nie
dotyka w ogóle stanów `HEAT_GF`/`HEAT_FF`.

## 3. Nowe helpery (packages/heat_orchestrator_helpers.yaml)

Dodać w sekcji „CWU / DHW quota":

```yaml
  dhw_exclusive_max_run_min:
    name: "DHW Quota Exclusive Max Duration"
    min: 0
    max: 240
    step: 5
    initial: 45
    unit_of_measurement: "min"
    icon: mdi:timer-sand

  dhw_exclusive_pause_min:
    name: "DHW Quota Exclusive Pause Duration"
    min: 0
    max: 360
    step: 5
    initial: 90
    unit_of_measurement: "min"
    icon: mdi:timer-pause
```

Semantyka wartości `0` (dla któregokolwiek z helperów): funkcja wyłączona —
zachowanie dotychczasowe (jeden ciągły blok DHW). Zapewnia to pełną
kompatybilność wsteczną, także gdy helpery nie istnieją jeszcze w HA
(domyślne wartości w kodzie przez `_param`).

## 4. Zmiany w heat_orchestrator.py

### 4.1. Nowe parametry (sekcja „Helpers – parameters")

```python
@property
def dhw_exclusive_max_run(self) -> float:
    """Max continuous DHW-only run [min]; 0 disables duty-cycling."""
    return self._param("input_number.dhw_exclusive_max_run_min", 45.0)

@property
def dhw_exclusive_pause(self) -> float:
    """Pause between DHW-only runs [min]; 0 disables duty-cycling."""
    return self._param("input_number.dhw_exclusive_pause_min", 90.0)

def _dhw_duty_cycle_enabled(self) -> bool:
    return self.dhw_exclusive_max_run > 0 and self.dhw_exclusive_pause > 0
```

### 4.2. Bramka pauzy przy starcie DHW (gałąź „pompa OFF", ok. linii 810)

Obecny warunek startu DHW: `remaining_quota > 0 and cooldown_ok`.

Nowy warunek — start DHW-only dodatkowo wymaga, by od ostatniego wyłączenia
pompy minęło co najmniej `max(min_pump_off, dhw_exclusive_pause)`:

```python
elif remaining_quota > 0 and cooldown_ok and dhw_pause_ok:
```

gdzie przed gałęzią:

```python
dhw_pause_ok = True
if self._dhw_duty_cycle_enabled():
    required_pause = max(self.min_pump_off, self.dhw_exclusive_pause)
    dhw_pause_ok = mins_off is None or mins_off >= required_pause
```

Uwagi:

- `mins_off is None` (brak `last_pump_off`, np. pierwszy start) → start dozwolony,
  spójnie z istniejącym `cooldown_ok`.
- Pauza bramkuje **tylko** start DHW-only. Start z powodu zapotrzebowania pokoi
  (`has_demand`) pozostaje bez zmian — sprawdzany jest wcześniej i nadal
  podlega wyłącznie `min_pump_off`.
- Po nocnym off window `last_pump_off` pochodzi z początku okna (np. 1:00),
  więc pierwszy poranny cykl DHW startuje bez dodatkowego opóźnienia.
- W gałęzi `else` (stan OFF) rozszerzyć log o powód
  `dhw_exclusive_pause (elapsed/required)`, gdy to pauza blokuje start.

### 4.3. Limit czasu ciągłej pracy DHW (gałąź „pompa ON, brak demand, quota > 0", ok. linii 887)

Czas w stanie mierzymy z istniejącego `input_datetime.state_since` — jest
ustawiany przy każdym wejściu w `DHW_QUOTA` (zarówno ze startu pompy, jak i z
przejścia `HEAT_* → DHW_QUOTA`), więc liczy wyłącznie czas ekskluzywnej pracy
DHW. Przetrwa też restart HA/AppDaemon (helper trwały).

```python
elif remaining_quota > 0:
    # No demand but quota remaining
    self._disable_all_rooms()
    if current_state != STATE_DHW_QUOTA:
        self._set_fsm_state(STATE_DHW_QUOTA)
        self.log(...)  # jak dotychczas
    elif self._dhw_duty_cycle_enabled():
        state_since = self._get_state_since()
        mins_on = self._minutes_since("input_datetime.last_pump_on")
        if state_since is not None:
            elapsed = (now - state_since).total_seconds() / 60.0
            if elapsed >= self.dhw_exclusive_max_run:
                if mins_on is not None and mins_on >= self.min_pump_on:
                    self._pump_off()
                    self._set_fsm_state(STATE_OFF)
                    self.log(
                        f"[DECISION] state=OFF reason=dhw_exclusive_max_run "
                        f"({elapsed:.0f}/{self.dhw_exclusive_max_run:.0f} min) "
                        f"pause={self.dhw_exclusive_pause:.0f} min "
                        f"quota_remaining={remaining_quota:.0f}"
                    )
                else:
                    # ochrona sprężarki ma priorytet: efektywny max run
                    # to max(dhw_exclusive_max_run, min_pump_on)
                    self.log(... waiting for min_pump_on ...)
```

Uwagi:

- `min_pump_on` ma priorytet nad `dhw_exclusive_max_run` (ochrona pompy
  ciepła przed krótkimi cyklami) — efektywny czas bloku to
  `max(dhw_exclusive_max_run, min_pump_on)`. Udokumentować przy helperze.
- Nie wprowadzamy nowego stanu FSM (np. `DHW_PAUSE`) — przerwa to zwykły
  `OFF` z logowanym powodem. FSM, diagnostyka i dashboardy pozostają
  bez zmian; o wznowieniu decyduje bramka pauzy z pkt 4.2.
- Wyjście z `DHW_QUOTA` przez `_pump_off()` ustawia `last_pump_off`, od
  którego liczona jest pauza — cykl domyka się bez dodatkowych helperów.

### 4.4. Interakcje i przypadki brzegowe

| Scenariusz | Zachowanie |
|---|---|
| Demand pojawia się w trakcie biegu DHW | bez zmian — gałąź `has_demand` przejmuje sterowanie (przejście do `HEAT_*`), licznik ekskluzywny przestaje mieć znaczenie |
| Demand pojawia się w trakcie pauzy | start grzania pokoi natychmiast po `min_pump_off` — pauza DHW nie blokuje |
| Przejście `HEAT_* → DHW_QUOTA` (pompa już chodzi) | `state_since` resetuje się przy zmianie stanu → limit liczy tylko czas ekskluzywny |
| `remaining_quota < max_run` | blok kończy się wyczerpaniem kwoty (istniejąca gałąź `no_demand_no_quota`) |
| Off window w trakcie bloku DHW | bez zmian — krok 1 ticka ma bezwzględny priorytet |
| `max_run = 0` lub `pause = 0` | zachowanie dotychczasowe (ciągły blok) |
| Restart HA/AppDaemon w trakcie bloku/pauzy | `state_since`, `last_pump_off`, `pump_on_minutes_today` są trwałymi helperami → logika wznawia się poprawnie |
| Daily reset | bez zmian — kwota zeruje się, cykl zaczyna się od nowa |

### 4.5. Przykładowy przebieg dnia (lato)

Kwota 3,5 h (210 min), `max_run = 45`, `pause = 90`, off window 1:00–6:00:

```
06:00–06:45  DHW blok 1 (45 min)
08:15–09:00  DHW blok 2 (90 min)
10:30–11:15  DHW blok 3 (135 min)
12:45–13:30  DHW blok 4 (180 min)
15:00–15:30  DHW blok 5 (210 min → kwota wyczerpana)
```

Grzanie wody rozłożone od rana do popołudnia zamiast jednego bloku 6:00–9:30.
Liczbę i rozstaw bloków użytkownik stroi dwoma helperami.

## 5. Dokumentacja

- **README.md** — dopisać oba helpery do tabeli parametrów, akapit o duty-cycle DHW.
- **FLOWCHARTS.md** — zaktualizować: (a) tick flow: `QUOTA_OFF` dostaje warunek
  pauzy, `DHW_CONTINUE` dostaje sprawdzenie max duration; (b) diagram FSM:
  krawędź `DHW_QUOTA → OFF` z powodem „exclusive max run reached",
  `OFF → DHW_QUOTA` opisana warunkiem „quota remaining ∧ pause elapsed".
- **home-assistant-heat-orchestrator-spec.md** — nowa podsekcja w opisie DHW
  quota (semantyka, wartość 0, priorytet `min_pump_on`/`min_pump_off`).
- **SETUP_GUIDE.md** — instrukcja utworzenia dwóch nowych helperów.
- **release_notes/v1.2.0.md** — nota wydania.

## 6. Plan testów (manualny — repo nie ma testów automatycznych)

1. **Lato / duty-cycle**: obniżyć `user_sp_*` tak, by nie było demand; ustawić
   `max_run=5`, `pause=10`, `min_pump_on=5`, `min_pump_off=5`,
   `dhw_min_run_hours=0.5`; obserwować w logach cykl
   `DHW_QUOTA → OFF(reason=dhw_exclusive_max_run) → DHW_QUOTA` aż do
   `quota_remaining=0`.
2. **Preempcja przez demand**: w trakcie bloku DHW podnieść `user_sp` jednego
   pokoju → oczekiwane przejście do `HEAT_*` bez wyłączania pompy.
3. **Demand w pauzie**: w trakcie pauzy podnieść `user_sp` → start grzania po
   `min_pump_off`, bez czekania na pauzę DHW.
4. **Kompatybilność**: ustawić `max_run=0` → jeden ciągły blok jak dotychczas.
5. **`min_pump_on > max_run`**: ustawić `min_pump_on=15`, `max_run=5` →
   blok trwa 15 min (log „waiting for min_pump_on").
6. **Restart**: zrestartować AppDaemon w środku bloku i w środku pauzy →
   cykl kontynuowany zgodnie z trwałymi helperami.

## 7. Kolejność implementacji

1. Helpery YAML (pkt 3).
2. Parametry i `_dhw_duty_cycle_enabled()` (pkt 4.1).
3. Bramka pauzy w gałęzi „pompa OFF" (pkt 4.2) + log powodu blokady.
4. Limit max run w gałęzi „pompa ON / brak demand" (pkt 4.3).
5. Aktualizacja dokumentacji (pkt 5).
6. Testy manualne wg pkt 6.
