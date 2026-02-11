# Home Assistant – Heat Orchestrator (AppDaemon, Python) – Specyfikacja implementacyjna

Data: 2026-02-10  
Cel: dokument jest „kontraktem” dla agentów AI implementujących sterownik ogrzewania w Pythonie (AppDaemon) dla Home Assistant.

---

## 1. Kontekst i ograniczenia

### 1.1 System
- Źródło ciepła: pompa ciepła (brak komunikacji z termostatami i zaworami).
- Ogrzewanie podłogowe: zawory sterowane przez termostaty pokojowe (`climate.*`).
- Dwa rozdzielacze:
  - Parter (GF)
  - Piętro (FF)

### 1.2 Zachowanie termostatów / zaworów
- Zawór w pokoju otwiera się tylko wtedy, gdy **aktualna temperatura** jest **niższa** od **zadanej** na termostacie.
- „Wyłączenie obwodu pokoju” realizujemy przez obniżenie zadanej temperatury poniżej aktualnej:
  - **OFF setpoint = 7°C** (ustalone jako bezpieczne).
- Użytkownik może ręcznie ustawiać temperatury na termostatach – automatyka **musi pamiętać te nastawy osobno** (bo przy OFF setpoincie zostaną utracone).

### 1.3 Ograniczenia pompy i hydrauliki
- **Nie wolno** jednocześnie grzać parteru i piętra (GF XOR FF).
- Pompa:
  - ON: `switch.sonoff_10017fadeb` (**tylko włączanie**).
  - OFF: `input_button.wylacznik_pompy` (graceful shutdown, wymagane do wyłączania).
- CWU/bojler:
  - Brak czujników.
  - Bojler traktowany jako bufor; osiąga ok. 55°C.
  - Wymóg: pompa ma pracować **min. 3–4h dziennie** (quota czasu pracy).

### 1.4 Okno wymuszonego OFF
- OFF window: **01:00–06:00** (lokalny czas HA).
- W tym oknie system **nie może** uruchamiać pompy.

### 1.5 Temperatura zewnętrzna
- Źródło: `weather.forecast_home` (Met.no).
- Preferowane: `state_attr(weather.forecast_home, "temperature")`.
- Fallback: `weather.get_forecasts` (hourly) i `forecast[0].temperature`.

Źródła:  
- Integracja weather i serwis `weather.get_forecasts`: https://www.home-assistant.io/integrations/weather/  
- Zmiany forecast (usunięcie atrybutu `forecast`): https://www.home-assistant.io/blog/2023/09/06/release-20239/

---

## 2. Encje sterowane i monitorowane

### 2.1 Termostaty pokojowe (`climate.*`)

**Parter (GF)**
- `climate.gabinet_ani`
- `climate.lazienka_parter`
- `climate.salon`

**Piętro (FF)**
- `climate.sypialnia`
- `climate.lazienka_pietro`
- `climate.pokoj_z_oknem_naroznym`
- `climate.pokoj_z_tarasem`

Wykorzystywane atrybuty:
- `current_temperature`
- `temperature` (setpoint)

### 2.2 Pompa (zasilanie układu)
- Włączanie (tylko ON): `switch.sonoff_10017fadeb`
- Wyłączanie (tylko OFF): `input_button.wylacznik_pompy`

### 2.3 Pogoda (Met.no)
- `weather.forecast_home`

---

## 3. Konfiguracja i stan – wymagane Helpers w Home Assistant

### 3.1 Per pokój (7x)
Dla każdego pokoju `room_id` (np. `salon`, `sypialnia`):
- `input_number.user_sp_<room_id>` – zapamiętany setpoint użytkownika (°C)
  - zakres: 5..30, krok: 0.5
- `input_number.priority_<room_id>` – priorytet (1..100)
- `input_number.heating_minutes_<room_id>` – skumulowany czas grzania pokoju (min)
  - zakres: 0..1440, krok: 1
  - inkrementowany co tick (+1 min) gdy pokój jest aktywnie grzany
  - resetowany: przy cooldown, przełączeniu piętra, wyłączeniu wszystkich pokoi, daily reset

**Lista `room_id`**
- `gabinet_ani`
- `lazienka_parter`
- `salon`
- `sypialnia`
- `lazienka_pietro`
- `pokoj_z_oknem_naroznym`
- `pokoj_z_tarasem`

### 3.2 Globalne parametry sterowania
- `input_number.room_off_setpoint` (°C) = 7.0
- `input_number.heating_hyst_on` (°C) – próg włączenia demand (np. 0.3)
- `input_number.heating_hyst_off` (°C) – próg uznania za „dogrzane” (np. 0.1–0.2)
- `input_number.min_state_duration_min` (min) – minimalny czas trzymania wybranego piętra/pokojów (np. 20–30)
- `input_number.min_pump_on_min` (min) – minimalny czas pracy pompy przed wyłączeniem (np. 30–45)
- `input_number.min_pump_off_min` (min) – minimalny czas postoju przed ponownym startem (np. 20–30)

### 3.3 Okno OFF (noc)
- `input_datetime.off_window_start` = 01:00
- `input_datetime.off_window_end` = 06:00

### 3.4 CWU quota (brak czujników)
- `input_number.dhw_min_run_hours` (h) – minimalna praca pompy na dobę (domyślnie 3.5)
- `input_number.pump_on_minutes_today` (min) – licznik czasu pracy pompy w dobie (sterowany przez AppDaemon)
- `input_datetime.day_reset_time` – reset liczników (domyślnie 00:00)

### 3.5 Diagnostyka / runtime (wymagane)
- `input_text.heat_state` – stan automatu:
  - `OFF_LOCKOUT`, `OFF`, `HEAT_GF`, `HEAT_FF`, `DHW_QUOTA`
- `input_datetime.state_since` – kiedy stan został ustawiony
- `input_number.pump_starts_today`
- `input_datetime.last_pump_on`
- `input_datetime.last_pump_off`
- (opcjonalnie) `input_text.active_floor`, `input_text.active_rooms` (CSV / JSON string)

---

## 4. Zasady nadrzędne sterowania

### 4.1 Pamiętanie setpointów użytkownika (krytyczne)
Automatyka:
- **zapisuje** ręczne nastawy użytkownika do `input_number.user_sp_<room_id>`
- **nigdy** nie nadpisuje intencji użytkownika (poza technicznym OFF=7°C)
- po ponownym „włączeniu pokoju” przywraca `user_sp`

### 4.2 Guard przeciw pętli zdarzeń (krytyczne)
W AppDaemon musi istnieć mechanizm `automation_guard[room]`, aby:
- zmiana setpointu wykonana przez automatykę nie została uznana za ręczną i nie zapisała 7°C jako `user_sp`.

Wymaganie:
- podczas `climate.set_temperature` ustaw `automation_guard=True` dla pokoju, po zakończeniu (z opóźnieniem 1–3s) przywróć `False`.

### 4.3 Zakaz jednoczesnego grzania pięter
Sterownik nigdy nie pozostawia aktywnych pokoi na GF i FF jednocześnie.
- Zawsze jest wybrane jedno piętro aktywne (GF lub FF) albo tryb DHW_QUOTA (wszystkie pokoje OFF).

### 4.4 Okno OFF 01:00–06:00
- Pompa nie może pracować w tym oknie.
- Jeśli pompa jest ON o 01:00, sterownik inicjuje OFF przez `input_button.wylacznik_pompy` (z poszanowaniem `min_pump_on_min`, patrz 9.2).

---

## 5. Model demand (zapotrzebowania na grzanie)

Dla pokoju `r`:
- `Tcur = state_attr(climate.r, "current_temperature")`
- `Tuser = state(input_number.user_sp_r)`
- `hon = input_number.heating_hyst_on`
- `hoff = input_number.heating_hyst_off`

Definicje:
- `need_heat(r) = (Tcur < Tuser - hon)`
- `satisfied(r) = (Tcur >= Tuser - hoff)`

Dla piętra:
- `need_heat_floor(F) = any(need_heat(r) for r in rooms(F))`

---

## 6. Temperatura zewnętrzna i tryb „bulk vs sequential”

### 6.1 Pobranie temperatury zewnętrznej
Funkcja `get_outdoor_temp()`:
1) `temp = state_attr("weather.forecast_home","temperature")` jeśli istnieje i jest liczbą.
2) W przeciwnym razie:
   - call service `weather.get_forecasts` (type=`hourly`, target=`weather.forecast_home`)
   - `temp = response["weather.forecast_home"]["forecast"][0]["temperature"]`

### 6.2 Progi trybu (LERP-based)
Wymagane helpers (LERP):
- `input_number.lerp_temp_min` (°C) – np. -10 (temperatura dla minimum pokoi)
- `input_number.lerp_temp_max` (°C) – np. +10 (temperatura dla maksimum pokoi)
- `input_number.lerp_rooms_min` – np. 1 (minimalna liczba pokoi do grzania)
- `input_number.lerp_rooms_max` – np. 5 (maksymalna liczba pokoi do grzania)

Helpers zachowane dla kompatybilności (ale nie używane w głównej logice):
- `input_number.bulk_mode_temp` (°C) – np. +5
- `input_number.sequential_mode_temp` (°C) – np. -5
- `input_number.max_rooms_limited` – np. 2

Funkcja LERP:
```python
def _lerp_max_rooms(t_out):
    t_min = lerp_temp_min  # domyślnie -10
    t_max = lerp_temp_max  # domyślnie +10
    r_min = lerp_rooms_min  # domyślnie 1
    r_max = lerp_rooms_max  # domyślnie 5
    
    if t_min >= t_max:
        return r_min  # degenerate config
    
    if t_out <= t_min:
        return r_min
    if t_out >= t_max:
        return r_max
    
    # Linear interpolation
    frac = (t_out - t_min) / (t_max - t_min)
    result = r_min + frac * (r_max - r_min)
    return max(r_min, int(result))  # floor, conservative
```

Tryb:
- Liczba pokoi do grzania = `min(lerp_max_rooms(T_out), liczba pokoi na piętrze, liczba kandydatów z demand)`
- Wybór pokoi następuje po sortowaniu według priority desc, deficit desc
- System nigdy nie przekroczy liczby pokoi dostępnych na danym piętrze (max 3 dla GF, max 4 dla FF)

### 6.3 Maksymalny czas ciągłego grzania pokoju
Wymagane helpery:
- `input_number.max_continuous_heating_min` (min) – np. 120 (maksymalny czas ciągłego grzania jednego pokoju)
- `input_number.heating_minutes_<room_id>` (min) – skumulowany czas grzania per pokój (przechowywany w HA, przetrwa restart AppDaemon)

Logika:
- Dla każdego pokoju śledzone są:
  - `input_number.heating_minutes_<room_id>` – skumulowany czas grzania (min), inkrementowany co tick (+1) gdy pokój jest aktywnie grzany (`_is_room_heating()`)
  - `room_cooldown_until[room]` – timestamp końca okresu cooldown (in-memory)
- Gdy `heating_minutes >= max_continuous_heating_min`:
  - Pokój jest **wyłączany z listy kandydatów** w `_select_rooms()`
  - Pokój wchodzi w **cooldown** na czas = `min_state_duration_min`
  - Licznik `heating_minutes` jest **resetowany do 0**
  - Log: `[ROOM] {room} forced cooldown after {minutes}min continuous heating`
  - **Ważne:** Pokój nadal zwraca `need_heat()=True` (aby poprawnie obliczać demand na piętrze i nie tracić demand floor)
- Po zakończeniu cooldown pokój znów staje się dostępny do wyboru
- **Ważne:** `_disable_room()` **NIE** resetuje licznika `heating_minutes` – dzięki temu pokój tymczasowo wyłączony (np. po osiągnięciu temperatury docelowej) zachowuje swój skumulowany czas grzania
- Licznik `heating_minutes` jest resetowany do 0 w następujących sytuacjach:
  - Przy wejściu w **cooldown** (timer restartuje po zakończeniu cooldown)
  - Przy **przełączeniu piętra** (pokoje na nieaktywnym piętrze)
  - Przy **wyłączeniu wszystkich pokoi** (DHW_QUOTA, OFF, OFF_LOCKOUT)
  - Przy **daily reset**
- Licznik przetrwa restart AppDaemon (przechowywany jako HA helper)

Scenariusz:
- Jeśli wszystkie pokoje na aktywnym piętrze są w cooldown, ale drugie piętro ma demand → system może przełączyć piętro (jeśli `min_state_duration` pozwala)

---

## 7. CWU quota (brak czujników)

### 7.1 Definicja quota
- `quota_min = dhw_min_run_hours * 60`
- `remaining = max(0, quota_min - pump_on_minutes_today)`

### 7.2 Zachowanie w trybie DHW_QUOTA
Jeśli:
- brak demand na GF i FF (żaden pokój nie potrzebuje grzania)
- i `remaining > 0`
- i jesteśmy poza oknem OFF
to:
- stan = `DHW_QUOTA`
- **wszystkie** pokoje ustaw na OFF setpoint (7°C)
- pompa może być ON do czasu `remaining == 0` (o ile nie wejdziemy w okno OFF)

---

## 8. Wybór piętra i pokoi

### 8.1 Skoring pokoi
- `deficit(r) = max(0, Tuser - Tcur)`
- `priority(r) = input_number.priority_r`
- `room_score(r) = deficit(r) * priority(r)`

### 8.2 Skoring pięter
- `floor_score(F) = max(room_score(r) for r in rooms(F) if need_heat(r))`
- jeśli brak pokoi z `need_heat` → `floor_score(F)=0`

### 8.3 Zasady przełączania piętra
- Jeśli aktualny stan to `HEAT_GF` lub `HEAT_FF`:
  - nie przełączaj piętra, jeśli `now - state_since < min_state_duration_min`.
  - gdy wolno przełączyć i drugie piętro ma wyższy score → przełącz piętro.

### 8.4 Wybór pokoi na aktywnym piętrze
- `candidates = [r for r in rooms(active_floor) if need_heat(r)]`
- sortuj `candidates` po:
  1) `priority desc`
  2) `deficit desc`
- wybierz liczbę pokoi wg trybu z sekcji 6.2
- pokoje wybrane: `enable_room(r)` (przywróć user_sp)
- pozostałe na aktywnym piętrze: `disable_room(r)` (7°C)
- wszystkie pokoje na nieaktywnym piętrze: `disable_room(r)` (7°C)

---

## 9. Sterowanie pompą – zasady ON/OFF

### 9.1 Definicje pomocnicze
- `in_off_window(now)` – czy czas w [off_window_start, off_window_end)
- `pump_is_on` – stan `switch.sonoff_10017fadeb` (lub inna encja statusu, jeśli switch jest tylko „enable”)

### 9.2 OFF window (noc)
Jeśli `in_off_window(now)`:
- Jeśli pompa ON:
  - Jeżeli `now - last_pump_on >= min_pump_on_min` → wyłącz przez `input_button.wylacznik_pompy`.
  - Jeżeli min_pump_on_min nie minął → **pozostaw ON** do spełnienia minimum, ale:
    - wymuś wyłączenie natychmiast po osiągnięciu `min_pump_on_min` (najbliższy tick).
- Stan: `OFF_LOCKOUT` (nie uruchamiaj w tym oknie).

### 9.3 Warunki uruchomienia pompy (OFF → ON)
Poza off window:
- `now - last_pump_off >= min_pump_off_min`
- oraz przynajmniej jeden warunek:
  - `need_heat_floor(GF)` lub `need_heat_floor(FF)`
  - `remaining_quota > 0`

Akcja ON:
- `switch.turn_on("switch.sonoff_10017fadeb")`
- `pump_starts_today += 1`
- `last_pump_on = now`

### 9.4 Warunki wyłączenia pompy (ON → OFF)
Poza off window:
- `now - last_pump_on >= min_pump_on_min`
- oraz jednocześnie:
  - brak demand na GF i FF
  - `remaining_quota == 0`

Akcja OFF:
- `input_button.press("input_button.wylacznik_pompy")`
- `last_pump_off = now`
- stan = `OFF`

---

## 10. Adapter termostatów – operacje wykonawcze

### 10.1 `enable_room(room)`
- Odczytaj `Tuser = input_number.user_sp_room`.
- Jeśli `Tuser` nieustawione / `None`:
  - fallback: użyj bieżącego `state_attr(climate.room,"temperature")` jeśli sensowne (np. 15..30),
  - inaczej fallback stały: 21.0.
- Ustaw `automation_guard[room]=True`.
- `climate.set_temperature(entity_id=climate.room, temperature=Tuser)`
- Po 1–3s: `automation_guard[room]=False`.

### 10.2 `disable_room(room)`
- Ustaw `automation_guard[room]=True`.
- `climate.set_temperature(entity_id=climate.room, temperature=room_off_setpoint)` (7°C)
- Po 1–3s: `automation_guard[room]=False`.

### 10.3 Listener ręcznych zmian użytkownika
Warunek zapisu:
- event: zmiana `state_attr(climate.room,"temperature")`
- jeśli `automation_guard[room] == False`:
  - zapisz do `input_number.user_sp_room` nową wartość (o ile jest w rozsądnym zakresie, np. 5..30).

---

## 11. AppDaemon – wymagania implementacyjne

### 11.1 Technologia
- AppDaemon app (Python 3).
- Jeden app: `heat_orchestrator.py` + config w `apps.yaml`.
- Pliki muszą być gotowe do wdrożenia w standardowej instalacji AppDaemon.

### 11.2 Wyzwalacze i harmonogram
- Tick sterownika: co 60 sekund (`run_every`).
- Listenery:
  - `listen_state` na każdy `climate.*` (zmiana setpointu i/lub current_temperature).
  - `listen_state` na `weather.forecast_home` (opcjonalnie, ale wskazane).
  - `listen_state` na helpery (priorytety, histerezy, progi, min czasy, okno OFF).

### 11.3 Reset dobowy
- Codziennie o `day_reset_time`:
  - `pump_on_minutes_today = 0`
  - `pump_starts_today = 0`

### 11.4 Zliczanie czasu pracy pompy
- Jeśli pompa ON: w każdym ticku dodaj +1 min do `pump_on_minutes_today`.

### 11.5 Logowanie (wymagane)
Każdy tick, jeśli decyzja się zmienia lub co X minut:
- `[DECISION] state=... reason=... floor=... rooms=[...] Tout=... quota_remaining=...`
- `[PUMP] ON/OFF ...`
- `[ROOM] enable/disable ...`

### 11.6 Odporność na błędy
- Jeśli `current_temperature` w pokoju jest `None` → pokój ignorowany w demand (log warn).
- Jeśli `climate.set_temperature` rzuci wyjątek / fail:
  - retry 1 raz,
  - potem oznacz pokój jako „unmanaged” (wewnętrzny set) na 15 min i nie używaj go do decyzji przełączeń w tym czasie.

---

## 12. Automat stanów (FSM) – definicja formalna

### 12.1 Stany
- `OFF_LOCKOUT` – wymuszony OFF (noc)
- `OFF` – pompa wyłączona, poza oknem lockout
- `HEAT_GF` – pompa ON, aktywny parter
- `HEAT_FF` – pompa ON, aktywne piętro
- `DHW_QUOTA` – pompa ON, wszystkie pokoje OFF (7°C), dobijamy quota

### 12.2 Priorytety przejść (kolejność w ticku)
1. OFF window check → wymuś `OFF_LOCKOUT` i wyłączenie (zgodnie z 9.2)
2. Oblicz `remaining_quota`
3. Oblicz demand GF/FF
4. Jeśli pompa OFF:
   - jeśli demand (GF/FF) → włącz i wybierz piętro
   - else jeśli remaining_quota>0 → włącz i `DHW_QUOTA`
   - else pozostań OFF
5. Jeśli pompa ON:
   - jeśli demand istnieje → `HEAT_GF` lub `HEAT_FF` wg skoringu i min_state_duration
   - else jeśli remaining_quota>0 → `DHW_QUOTA`
   - else (brak demand i quota==0) → OFF (zgodnie z 9.4)

---

## 13. Kryteria akceptacji (testy funkcjonalne)

1. **Pamięć setpointu użytkownika**
   - Ustaw ręcznie 22°C w `climate.salon` → `input_number.user_sp_salon=22`.
   - Automat wyłączy salon (7°C) → `user_sp_salon` nie zmienia się.
   - Automat ponownie włączy salon → setpoint wraca na 22°C.

2. **Zakaz grzania dwóch pięter naraz**
   - Przy demand na obu piętrach aktywne jest tylko jedno piętro; drugie ma wszystkie pokoje na 7°C.

3. **OFF window**
   - O 01:00 pompa przechodzi w OFF najpóźniej po spełnieniu `min_pump_on_min`.
   - W przedziale 01:00–06:00 pompa nie startuje.

4. **Quota CWU**
   - Gdy pokoje dogrzane, a `pump_on_minutes_today < dhw_min_run_hours*60` → pompa przechodzi w `DHW_QUOTA` i dobija quota (poza off window).

5. **Tryb zależny od temperatury zewnętrznej**
   - Przy `T_out <= sequential_mode_temp` aktywne jest maks. 1 pomieszczenie na piętrze.
   - Przy `T_out >= bulk_mode_temp` aktywne są wszystkie pomieszczenia z demand na piętrze.

6. **Anty-oscylacje**
   - Piętro nie przełącza się częściej niż `min_state_duration_min`.

---

## 14. Zakres prac dla agentów AI (deliverables)

Agent ma dostarczyć:
1. Kod AppDaemon:
   - `apps/heat_orchestrator.py`
   - konfigurację `apps.yaml` z mapowaniem encji (rooms + helpers).
2. Minimalny README:
   - jak skonfigurować helpery (lista, nazwy)
   - jak uruchomić i debugować
3. (Opcjonalnie) sekcja YAML do stworzenia helperów (jeśli wykonalne) lub instrukcja manualna krok po kroku.

---

## 15. Parametry domyślne (można zmienić helperami)
- `room_off_setpoint = 7.0°C`
- `heating_hyst_on = 0.3°C`
- `heating_hyst_off = 0.2°C`
- `min_state_duration_min = 25`
- `min_pump_on_min = 40`
- `min_pump_off_min = 25`
- `dhw_min_run_hours = 3.5`
- `bulk_mode_temp = +5°C`
- `sequential_mode_temp = -5°C`
- `max_rooms_limited = 2`
- `off_window = 01:00–06:00`

---

## 16. Uwagi implementacyjne (ważne)
- Zawsze wyłączaj pompę przez `input_button.wylacznik_pompy` (nigdy przez switch OFF).
- Switch `switch.sonoff_10017fadeb` używaj tylko do ON.
- Upewnij się, że logika `weather.get_forecasts` jest odporna na brak danych (wtedy użyj ostatniej znanej temperatury lub wartości neutralnej 0°C i zaloguj ostrzeżenie).
- Przy pierwszym uruchomieniu, jeśli `user_sp_*` jest puste, wypełnij je z aktualnych setpointów termostatów (o ile w zakresie 5..30).
