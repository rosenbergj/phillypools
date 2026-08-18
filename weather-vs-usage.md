# Can the site tell you what the weather was?

A game, played August 18, 2026: predict Philadelphia's best and worst days
from phillypools traffic alone, *then* look up what the weather actually did.

## Rules

Twenty-five days of prod usage, July 24 through August 17. The counting
signal is **JS-confirmed visitors** — bot traffic runs ~150/day and barely
moves, so it tells you nothing. July 24 is thrown out as a partial day
(recording started mid-day), leaving 8 weekend days and 16 clean weekdays.

Raw counts won't do, because two things drive them that aren't weather:

- **Season trend** — traffic grew about 2.5%/day across the window.
- **Day of week** — against a Monday baseline: Sat +42%, Fri +34%, Sun +25%,
  Thu +3%, Wed −20%, Tue −38%.

Strip both out and what's left is the **residual**: how far a day beat or
missed what its calendar slot alone predicted. That's the guess.

Weather came afterward from the Open-Meteo reanalysis archive, Center City.

## The picks

| | prediction | why |
|---|---|---|
| Best weekend | **Sun Aug 9** | 176 confirmed, +65% — the biggest day of the season, nothing close |
| Worst weekend | **Sun Aug 16** | 137 → 57 overnight from Saturday, −55% |
| Best weekday | **Thu Aug 6** | +40%, and high on raw count too |
| Worst weekday | **Tue Jul 28** | 22 confirmed, −48%, the floor of the whole dataset |

Plus a supporting call: Jul 30, Aug 3 and Aug 5 were also bad weather.

## The answers

| date | dow | high °F | feels °F | rain in | rain hrs | sun hrs | conf | resid |
|---|---|---|---|---|---|---|---|---|
| Jul 25 | Sat | 81.6 | 84.3 | 0.00 | 0 | 14.0 | 100 | +15% |
| Jul 26 | Sun | 82.6 | 87.1 | 0.00 | 0 | 14.0 | 102 | +35% |
| Jul 27 | Mon | 85.3 | 93.0 | 0.03 | 5 | 14.0 | 67 | +11% |
| **Jul 28** | Tue | **78.9** | 84.2 | **1.34** | 14 | 7.6 | 22 | **−48%** |
| Jul 29 | Wed | 82.0 | 86.0 | 0.04 | 5 | 14.0 | 52 | 0% |
| Jul 30 | Thu | 81.1 | 85.0 | 0.15 | 15 | 13.9 | 44 | −34% |
| Jul 31 | Fri | 86.4 | 92.1 | 0.02 | 4 | 13.9 | 99 | +5% |
| Aug 1 | Sat | 87.3 | 91.9 | 0.01 | 2 | 12.5 | 106 | +2% |
| Aug 2 | Sun | 88.6 | 91.9 | 0.47 | 9 | 7.6 | 89 | −1% |
| Aug 3 | Mon | 85.5 | 93.9 | 1.19 | 18 | 8.7 | 49 | −32% |
| Aug 4 | Tue | 85.0 | 90.0 | 0.00 | 0 | 13.8 | 81 | +62% |
| Aug 5 | Wed | 82.0 | 91.9 | 0.53 | 13 | 6.2 | 46 | −25% |
| **Aug 6** | Thu | 88.6 | **99.7** | 0.11 | 8 | 13.6 | 111 | **+40%** |
| Aug 7 | Fri | 89.1 | 99.9 | 0.14 | 7 | 12.2 | 104 | −7% |
| Aug 8 | Sat | 86.2 | 95.9 | 0.18 | 6 | 13.1 | 113 | −8% |
| **Aug 9** | Sun | **90.4** | 99.3 | 0.00 | 1 | 13.8 | 176 | **+65%** |
| Aug 10 | Mon | 90.0 | 96.7 | 0.11 | 5 | 10.7 | 109 | +28% |
| Aug 11 | Tue | 85.5 | 93.3 | 0.27 | 9 | 11.9 | 70 | +18% |
| Aug 12 | Wed | 87.6 | 94.5 | 0.17 | 6 | 11.6 | 98 | +34% |
| Aug 13 | Thu | 88.3 | 93.6 | 0.14 | 4 | 12.6 | 103 | +9% |
| Aug 14 | Fri | 88.8 | 94.6 | 0.01 | 2 | 12.3 | 135 | +2% |
| Aug 15 | Sat | 87.9 | 92.4 | 0.00 | 0 | 13.6 | 137 | −7% |
| **Aug 16** | Sun | **80.8** | 82.4 | 0.12 | 5 | **2.4** | 57 | **−55%** |
| Aug 17 | Mon | 85.7 | 96.3 | 0.06 | 8 | 9.5 | 104 | +3% |

All four headline picks landed.

- **Aug 9** was the hottest day in the window, full stop — 90.4°F, feels-like
  99, no rain, near-full sun.
- **Aug 16** was the coolest weekend day *and* got 2.4 hours of sun against a
  13–14 hour ceiling, by a mile the gloomiest day of the period. Called it a
  washout, which was too strong: only 0.12in fell. It wasn't rain, it was ten
  missing degrees under a lid of cloud.
- **Jul 28** took both bottom rankings at once: coolest day of the window
  (78.9°F) and wettest (1.34in over 14 hours).
- **Aug 6** hit feels-like 99.7, second to Aug 7's 99.9 by a rounding error,
  with more sun.

And the four weak weekdays flagged — Jul 28, Jul 30, Aug 3, Aug 5 — turn out
to be *precisely* the four rainiest weekdays by hours of rain (14, 15, 18, 13;
next-wettest weekday is 9).

## The two things this taught us

**It's a rain gauge, not a thermometer.** Correlations against the residual:

| variable | pearson | spearman |
|---|---|---|
| sunshine hours | +0.62 | +0.48 |
| rain hours | −0.60 | −0.51 |
| rain inches | −0.54 | −0.54 |
| max temp | +0.54 | +0.41 |
| feels-like max | +0.47 | +0.37 |

Heat is the *weaker* signal, which inverts the premise of the game. Note also
that rain **duration** beats rain **volume**: Jul 30 lost a third of its
traffic on 0.15 inches, because it drizzled for fifteen hours.

**Relief, not heat.** Once a day is dry and sunny, extra degrees buy very
little. Aug 4 was the single biggest weekday anomaly at +62% on an unremarkable
85°F — but it was the first dry, cloudless day after four consecutive days with
rain in them. Meanwhile Aug 7, the hottest feels-like day of the entire period,
came in *below* its slot at −7%, and Aug 8 right behind it at −8%.

Which points at what the residuals really encode: not "was today nice" but
"was today the **first** nice day." Aug 7 was day two of a heat wave; the
lookups had already happened on day one. People check where the pool is when
the weather turns. After that they just go.

## Reproducing it

Usage numbers come from `UsageDaily` on prod, read-only:

```bash
railway ssh --service web -- python manage.py shell -c "
from pools.models import UsageDaily
rows = UsageDaily.objects.filter(metric='visitors', key__in=['','js_confirmed','bot']) \
    .values_list('day','key','visitors','events').order_by('day')
for r in rows: print('|'.join(str(x) for x in r))
"
```

Weather is one unauthenticated call:

```bash
curl -s "https://archive-api.open-meteo.com/v1/archive?latitude=39.9526&longitude=-75.1652\
&start_date=2026-07-24&end_date=2026-08-18\
&daily=temperature_2m_max,temperature_2m_min,apparent_temperature_max,\
precipitation_sum,precipitation_hours,sunshine_duration\
&temperature_unit=fahrenheit&precipitation_unit=inch&timezone=America%2FNew_York"
```

Worth replaying next season with a full June–August window, when there'll be
enough days to fit the weather terms directly instead of eyeballing residuals.
