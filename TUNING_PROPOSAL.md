# Pakiet ustawień orchestratora: minimalizacja zużycia energii

Stan na 2026-11-04. Dotyczy instalacji:

- źródło: przerobiony klimatyzator → wymiennik płytowy → woda 50–60°C
- bojler CWU działa też jako bufor, zasilany równolegle z mieszaczem
- mieszacz ręczny (bez termostatu), nastaw ~33°C na rozdzielacz; zasilanie dryfuje
  z temperaturą powrotu (dołek przy starcie, pełznięcie w górę przy długim biegu)
- podłoga: sklejka brzozowa + dąb na wylewce (limit powierzchni 27°C, duża bezwładność)
- dwa rozdzielacze (GF/FF) z wyłącznością pięter

Zweryfikowane (dump z 2026-11-04 21:31): taryfa **płaska** (jedna cena całą dobę), dwie pompy obiegowe (po jednej na piętro) na tym samym
przekaźniku co klimatyzator, załączane gdy ≥1 zawór piętra otwarty; bojler 200 l; termostaty
raportują `current_temperature` z krokiem **0,5°C**; w sezonie pompa chodzi ~16 h/dobę (961 min
do 21:31 przy +2°C, 2 starty).

## 1. Helpery do ustawienia (Developer Tools → States / dashboard)

| Helper | Obecnie | Ustaw | Uzasadnienie |
|---|---|---|---|
| `input_number.heating_hyst_on` | 0.3 | **0.3** | Bez zmian. Szerszy próg wydłuża odzysk przy ograniczonej mocy z podłogi. |
| `input_number.heating_hyst_off` | 0.05 | **0.05** (docelowo **0** po zmianie kodu) | Przy kroku 0,5°C odczytu `sp + 0.05` zatrzymuje dopiero na odczycie `sp + 0,5`: każdy pokój jest systematycznie 0,5°C za ciepły (dump: gabinet 22,5/22, łazienka 24,0/23,5). Wymaga sekcji 3 pkt 1. |
| `input_number.min_state_duration_min` | 60 | **60** | Przełączenie piętra = dołek na mieszaczu (zimny powrót) + 1–2 h zanim pokój zareaguje. 25 min to poniżej stałej czasowej. Ten helper to też długość cooldownu po capie. |
| `input_number.min_pump_on_min` | 60 | **60** | Każdy start = kilkanaście minut słabego zasilania z mieszacza. Mniej, dłuższych biegów. Zob. wyciek w sekcji 3. |
| `input_number.min_pump_off_min` | 45 | **45** | Wycina sporadyczne krótkie restarty; przy drewnie ponowny demand i tak przychodzi po godzinach. |
| `input_number.max_continuous_heating_min` | 240 | **240** | 120 min przy 33°C przesuwa pokój o ułamek stopnia; cap poniżej tego to churn (cap → cooldown → latch → powrót). |
| `input_number.max_continuous_heating_solo_min` | 360 | **360** | Bezpiecznik na pełznięcie zasilania przy długim biegu jednej pętli (powierzchnia dębu ~27–28°C) i na zepsuty czujnik. Nie ustawiać 0. |
| `input_number.lerp_rooms_min` | 2 | **2** | Ustawione przez Ciebie, zostaje. W praktyce ogranicza demand, nie LERP (dump: limit 3, demand 1). |
| `input_number.lerp_rooms_max` | 4 | **4** | ok |
| `input_number.lerp_temp_min` | -16 | **-16** | ok |
| `input_number.lerp_temp_max` | 10 | **10** | ok |
| `input_number.dhw_min_run_hours` | 2.5 | **2.5** | Bez zmian do czasu czujnika w bojlerze (sekcja 3). |
| `input_number.dhw_exclusive_max_run_min` | 45 | **60** | Poza sezonem liczą się tylko starty: 3 bloki (04:00, ~07:30, ~11:00) zamiast 4–5. Alternatywa 0/0: jeden blok, 1 start. |
| `input_number.dhw_exclusive_pause_min` | 40 | **150** | 40 < `min_pump_off` 45, więc i tak działało 45. |
| `input_datetime.off_window_start/end` | 00:00–04:00 | **00:00–04:00** | Przy taryfie płaskiej energetycznie ~neutralne: 4 h przerwy obniża średnią temperaturę domu o ~0,5°C przez 1/6 doby (ułamek procenta), a odzysk o 04:00 idzie przy prawie tej samej temperaturze zewnętrznej. Zostaw ze względu na komfort snu albo skasuj, bez straty. |
| `input_number.room_off_setpoint` | 7 | 7 | bez zmian |
| `bulk_mode_temp`, `sequential_mode_temp`, `max_rooms_limited` | — | — | nieużywane przez kod |

Setpointy i priorytety pokoi: bez zmian wg komfortu. Odnotowane raz: salon, łazienka parter
i sypialnia na 23,5°C to największa dźwignia kWh w instalacji (ok. 6% na stopień).

## 2. Zmiany sprzętowe (poza orchestratorem), wg zysku

1. **Nastawa gorącej strony (wymiennik/bojler)**: 60°C → 50°C. Każdy stopień to ~2–3% prądu
   sprężarki, 10°C to rząd 20%. 50°C w bojlerze jest akceptowalne dla CWU; ewentualnie dobój do 60°C
   raz w tygodniu w stałym oknie. 60°C to granica skraplania dla R32/R410A.
2. **Termostatyczny zawór mieszający** (typ ESBE VTA, kilkaset zł). Stałe 33°C niezależnie od
   powrotu: usuwa pełznięcie przy długich biegach (ochrona dębu), łagodzi dołek startowy i pozwala
   obniżyć gorącą stronę bez ręcznego przestawiania mieszacza. Warunek sensownego punktu 1.
3. **Czujnik temperatury w bojlerze w HA** (DS18B20 lub odczyt z przeróbki). Umożliwia punkt 3 w
   sekcji 3.

## 3. Zmiany w kodzie, wg priorytetu (nie wykonane)

1. **`heating_hyst_off` ≤ 0** (helper `min: -0.5`). Potwierdzony krok odczytu 0,5°C: dziś stop
   następuje na odczycie `sp + 0,5`. Z `hyst_off = 0` stop na odczycie `= sp`, pasmo
   `sp-0,5 … sp` zamiast `sp-0,5 … sp+0,5`. Średnia temperatura wszystkich pokoi niżej o 0,5°C
   bez zmiany nastaw. Obejście bez kodu: `user_sp` o 0,5 niżej (nastawy przestają znaczyć to,
   co pokazują termostaty).
2. Kwota CWU po temperaturze zamiast po czasie (`DHW_QUOTA` gdy bojler < X°C, koniec ≥ Y°C).
   Wymaga czujnika z sekcji 2. Poza sezonem usuwa biegi bez odbioru przy pełnym bojlerze.
3. `_tick`, gałąź "brak demand, brak quota" (ok. l. 1291): podczas czekania na `min_pump_on`
   zawory zostają otwarte na `sp + 2`, pompy obiegowe tłoczą 33°C do dogrzanych pokoi. Dodać
   `_disable_all_rooms()`. W sezonie (pompa ~16 h/dobę) praktycznie nie występuje.
4. Eksperymentalnie: boost `+0,5°C` do `user_sp` w najcieplejszych godzinach doby
   (ok. 10:00–16:00, COP klimatyzatora o 15–20% lepszy niż nocą, bez odszraniania) plus dłuższe
   okno OFF nocą (00:00–06:00). Zysk rzędu kilku procent, niepewny, bo cieplejszy dom w południe
   to większe straty. Dopiero po punkcie 1 i po pomiarze.

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

- Histereza termostatu wymiennika / bojlera (częstość cykli sprężarki przy jednej pętli).
- Historia mocy Sonoffa w mroźny dzień przy pełnym bojlerze (czy cykluje przy 1–2 pętlach).
