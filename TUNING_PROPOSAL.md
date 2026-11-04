# Pakiet ustawień orchestratora: minimalizacja zużycia energii

Stan na 2026-11-04. Dotyczy instalacji:

- źródło: przerobiony klimatyzator → wymiennik płytowy → woda 50–60°C
- bojler CWU działa też jako bufor, zasilany równolegle z mieszaczem
- mieszacz ręczny (bez termostatu), nastaw ~33°C na rozdzielacz; zasilanie dryfuje
  z temperaturą powrotu (dołek przy starcie, pełznięcie w górę przy długim biegu)
- podłoga: sklejka brzozowa + dąb na wylewce (limit powierzchni 27°C, duża bezwładność)
- dwa rozdzielacze (GF/FF) z wyłącznością pięter

Założenia niezweryfikowane: taryfa jednostrefowa, pompa obiegowa podłogi na tym samym
przekaźniku co klimatyzator, rozdzielczość `current_temperature` z termostatów ≤ 0,1°C.

## 1. Helpery do ustawienia (Developer Tools → States / dashboard)

| Helper | Obecnie | Ustaw | Uzasadnienie |
|---|---|---|---|
| `input_number.heating_hyst_on` | 0.3 | **0.3** | Bez zmian. Szerszy próg wydłuża odzysk przy ograniczonej mocy z podłogi. |
| `input_number.heating_hyst_off` | 0.2 | **0.05** | Stop jak najwcześniej; wylewka pod drewnem dogrzeje 0,3–0,8°C po zamknięciu, a zasilanie po długim biegu pełznie do ~38°C. |
| `input_number.min_state_duration_min` | 25 | **60** | Przełączenie piętra = dołek na mieszaczu (zimny powrót) + 1–2 h zanim pokój zareaguje. 25 min to poniżej stałej czasowej. Ten helper to też długość cooldownu po capie. |
| `input_number.min_pump_on_min` | 40 | **60** | Każdy start = kilkanaście minut słabego zasilania z mieszacza. Mniej, dłuższych biegów. Zob. wyciek w sekcji 3. |
| `input_number.min_pump_off_min` | 25 | **45** | Wycina sporadyczne krótkie restarty; przy drewnie ponowny demand i tak przychodzi po godzinach. |
| `input_number.max_continuous_heating_min` | 120 | **240** | 120 min przy 33°C przesuwa pokój o ułamek stopnia; cap poniżej tego to churn (cap → cooldown → latch → powrót). |
| `input_number.max_continuous_heating_solo_min` | 240 | **360** | Bezpiecznik na pełznięcie zasilania przy długim biegu jednej pętli (powierzchnia dębu ~27–28°C) i na zepsuty czujnik. Nie ustawiać 0. |
| `input_number.lerp_rooms_min` | 1 | **3** | Gdy bojler jest pełny, jedyny odbiór wymiennika to podłoga; 1 pętla to kilkaset watów i cyklowanie inwertera. 3 to kompromis z triage w głębokim mrozie. Jeśli klimatyzator wyraźnie nie nadąża poniżej -10°C i chcesz grzać priorytetowe pokoje kosztem reszty: 2. |
| `input_number.lerp_rooms_max` | 5 | **4** | Piętra mają po 4 pokoje. |
| `input_number.lerp_temp_min` | -10 | **-10** | Limit 3 pokoi tylko przy realnym niedoborze mocy. |
| `input_number.lerp_temp_max` | 10 | **0** | Powyżej 0°C bez limitu (4). Funkcja robi `int()`, więc przy 3→4 to jeden próg przy `lerp_temp_max`. |
| `input_number.dhw_min_run_hours` | 2.5 | **2.5** | Bez zmian do czasu czujnika w bojlerze (sekcja 3). |
| `input_number.dhw_exclusive_max_run_min` | 45 | **50** | 3 bloki zamiast 5 → 2 starty mniej dziennie poza sezonem. |
| `input_number.dhw_exclusive_pause_min` | 90 | **150** | Bloki ok. 06:00, 09:20, 12:40; kwota zamknięta przed wieczorem. |
| `input_datetime.off_window_start/end` | 01:00–06:00 | **01:00–06:00** | Bez zmian przy G11. Przy G12/G12w: usuń okno nocne, rozważ okno w szczycie dziennym. Jeśli poranny odzysk jest za wolny: 02:00–05:00. |
| `input_number.room_off_setpoint` | 7 | 7 | bez zmian |
| `bulk_mode_temp`, `sequential_mode_temp`, `max_rooms_limited` | — | — | nieużywane przez kod |

Setpointy i priorytety pokoi: bez zmian wg komfortu. Każde 0,5°C niżej w średniej temperaturze
domu to więcej niż wszystkie timery razem.

## 2. Zmiany sprzętowe (poza orchestratorem), wg zysku

1. **Nastawa gorącej strony (wymiennik/bojler)**: 60°C → 50°C. Każdy stopień to ~2–3% prądu
   sprężarki, 10°C to rząd 20%. 50°C w bojlerze jest akceptowalne dla CWU; ewentualnie dobój do 60°C
   raz w tygodniu w stałym oknie. 60°C to granica skraplania dla R32/R410A.
2. **Termostatyczny zawór mieszający** (typ ESBE VTA, kilkaset zł). Stałe 33°C niezależnie od
   powrotu: usuwa pełznięcie przy długich biegach (ochrona dębu), łagodzi dołek startowy i pozwala
   obniżyć gorącą stronę bez ręcznego przestawiania mieszacza. Warunek sensownego punktu 1.
3. **Czujnik temperatury w bojlerze w HA** (DS18B20 lub odczyt z przeróbki). Umożliwia punkt 3 w
   sekcji 3.

## 3. Zmiany w kodzie do rozważenia (nie wykonane)

1. `heating_hyst_off`: dopuścić 0 lub wartość ujemną (stop przed setpointem, np. `sp - 0.2`).
   Przy tej bezwładności optymalny stop jest poniżej setpointu. Obowiązkowe, jeśli termostaty
   raportują `current_temperature` z krokiem 0,5°C (wtedy 0.05 vs 0.2 nic nie zmienia, a każdy
   pokój przestrzela min. 0,5°C).
2. `_tick`, gałąź "brak demand, brak quota" (ok. l. 1291): podczas czekania na `min_pump_on`
   pokoje zostają włączone na `sp + 2`. Dodać `_disable_all_rooms()` od razu; pompa dobija timer
   przy zamkniętych zaworach jak w `DHW_QUOTA`. Zysk zależy od tego, czy pompa obiegowa podłogi
   jest na tym samym przekaźniku (pytanie otwarte).
3. Kwota CWU po temperaturze zamiast po czasie: `DHW_QUOTA` wchodzi gdy bojler < X°C, kończy
   gdy ≥ Y°C. Usuwa biegi jednostki bez odbioru przy pełnym bojlerze i niedogrzanie przy pustym.
   Wymaga czujnika z sekcji 2.
4. `lerp_rooms_min`: podnieść `max` helpera z 3 do 4, żeby dało się wyłączyć limit.

## 4. Jak zweryfikować po zmianie

- `input_number.pump_starts_today`: cel kilka startów dziennie w sezonie, 3 poza sezonem.
- Historia `sensor.zasilanie_pompy_sonoff_10017fadeb_power` w mroźny dzień przy pełnym bojlerze:
  pobór ma być stabilny (inwerter moduluje), nie skakać między ~100 W a kilkuset W co kilka minut.
  Próg "sprężarka pracuje" dla przerobionego klimatyzatora to raczej 300–400 W, nie 1000 W
  z komentarza w kodzie. Jeśli cykluje przy 3–4 otwartych pętlach: za duża jednostka na ten
  odbiór, pomoże tylko większy bufor po stronie podłogi albo niższa gorąca strona.
- Historia `climate.*` `current_temperature`: przestrzelenie ponad `user_sp` po stopie. Jeśli
  regularnie > 0,5°C, wdrożyć punkt 1 z sekcji 3.
- Powierzchnia dębu po 4–6 h ciągłego biegu jednej pętli (pirometr): jeśli > 27°C, obniżyć
  `max_continuous_heating_solo_min` lub przestawić mieszacz.

## 5. Pytania otwarte

- Taryfa (G11 / G12 / G12w)? Decyduje o oknie nocnym.
- Pompa obiegowa podłogi na tym samym przekaźniku co klimatyzator?
- Rozdzielczość `current_temperature` termostatów (0,1 czy 0,5°C)?
- Pojemność bojlera i histereza termostatu wymiennika (częstość cykli sprężarki).
