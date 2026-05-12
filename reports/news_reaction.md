# Latence Polymarket news-ancree -- 2026-05-11 23:44 UTC

## Methodologie

- T0 = timestamp UTC publie d'une news verifiee (BLS, FOMC, Truth Social, etc.).
- Serie de prix CLOB sur [T0 - 30 min, T0 + 6.0 h], resolution effective ~1 point/minute.
- Premier move = premier instant t >= T0 ou |price(t) - price(T0)| >= 2c.
- Move final = price(T0 + post_window) - price(T0). pct_at_X = move_at_X / final.

- Events ingerez : **5** -- events exploitables (>=5 points) : **5**.

- Limite : l'endpoint CLOB est echantillonne ~1/min, donc tout move < 60s est invisible par construction. La latence renvoyee est une **borne superieure** arrondie a la minute superieure.

## Events retenus

| event | T0 (UTC) | source | direction | t0_price | final | latency 1er move |
|:------|:--------|:------|:----------|---------:|------:|----------------:|
| cpi-march-release-2026-04-10 | 2026-04-10T12:30:00Z | [BLS](https://www.bls.gov/news.release/archives/cpi_04102026.htm) | UP | 0.980 | +1.95c | n/a |
| nfp-march-release-2026-04-03 | 2026-04-03T12:30:00Z | [BLS](https://www.bls.gov/news.release/archives/empsit_04032026.htm) | UP | 0.280 | +71.95c | 2.0min |
| fomc-april-2026-04-29 | 2026-04-29T18:00:00Z | [Federal Reserve](https://www.federalreserve.gov/newsevents/pressreleases/monetary20260429a.htm) | DOWN | 0.007 | -0.65c | 6.0min |
| nfp-april-release-2026-05-08 | 2026-05-08T12:30:00Z | [BLS](https://www.bls.gov/news.release/archives/empsit_05082026.htm) | UP | 0.150 | +84.95c | 2.0min |
| uk-labour-final-results-2026-05-09 | 2026-05-09T15:40:00Z | [Croydon Council declaration / wire](https://www.npr.org/2026/05/10/nx-s1-5817491/uk-elections-keir-starmer-resign-reform-green) | UP | 0.195 | +3.00c | 2.4h |

### Detail par event

**cpi-march-release-2026-04-10** -- CPI March 2026 released — annual +3.3%, monthly +0.9% (well above 2.8% threshold)
- Marche : `Will annual inflation increase by >=2.8% in March?` (token `108968538130342611...`)
- T0 price = 0.980, final move = +1.95c (direction observee : UP)
- Latence 1er move >= 2c : **n/a**
- Move a T+60s : +0.00c (+0.0% du final)
- Move a T+5min : +1.70c (+87.2%)
- Move a T+15min : +1.95c (+100.0%)
- Move a T+1h : +1.95c (+100.0%)
- Note : Timestamp exact: BLS embargo lifted at 8:30 a.m. ET (12:30 UTC). YES doit passer de ~0.50 a ~0.999.

**nfp-march-release-2026-04-03** -- March 2026 jobs report — unemployment rate 4.3%, NFP +178k
- Marche : `Will the March 2026 unemployment rate be 4.3%?` (token `997893073478611245...`)
- T0 price = 0.280, final move = +71.95c (direction observee : UP)
- Latence 1er move >= 2c : **2.0min**
- Move a T+60s : +0.00c (+0.0% du final)
- Move a T+5min : +71.50c (+99.4%)
- Move a T+15min : +71.95c (+100.0%)
- Move a T+1h : +71.95c (+100.0%)
- Note : Timestamp exact: BLS 8:30 ET. Sortie 4.3% donc YES de ce bucket doit monter.

**fomc-april-2026-04-29** -- FOMC holds rates steady at 3.50-3.75% (8-4 vote)
- Marche : `Fed rate cut by April 2026 meeting?` (token `103665283657652818...`)
- T0 price = 0.007, final move = -0.65c (direction observee : DOWN)
- Latence 1er move >= 2c : **6.0min**
- Move a T+60s : +0.00c (-0.0% du final)
- Move a T+5min : +0.10c (-15.4%)
- Move a T+15min : -0.55c (+84.6%)
- Move a T+1h : -0.65c (+100.0%)
- Note : Timestamp exact: FOMC statement release 2:00 PM ET. Pas de cut donc YES doit s'effondrer vers 0.

**nfp-april-release-2026-05-08** -- April 2026 jobs report — NFP +115k, unemployment 4.3%
- Marche : `Will the US add between 100k and 150k jobs in April?` (token `654225952724215421...`)
- T0 price = 0.150, final move = +84.95c (direction observee : UP)
- Latence 1er move >= 2c : **2.0min**
- Move a T+60s : +0.00c (+0.0% du final)
- Move a T+5min : +84.50c (+99.5%)
- Move a T+15min : +84.95c (+100.0%)
- Move a T+1h : +84.95c (+100.0%)
- Note : Timestamp exact: BLS 8:30 ET. Sortie 115k -> bucket 100-150k resolved YES.

**uk-labour-final-results-2026-05-09** -- Final UK local council results declared 16:40 BST (15:40 UTC) — Labour loses 1,000+ seats
- Marche : `Starmer out by June 30, 2026?` (token `345545558274385511...`)
- T0 price = 0.195, final move = +3.00c (direction observee : UP)
- Latence 1er move >= 2c : **2.4h**
- Move a T+60s : +0.00c (+0.0% du final)
- Move a T+5min : +0.00c (+0.0%)
- Move a T+15min : +0.00c (+0.0%)
- Move a T+1h : +0.00c (+0.0%)
- Note : Timestamp moyen: 16:40 BST a Croydon, derniere declaration officielle. Le marche bougeait deja depuis le 7-8 mai (jour J), le 'news anchor' a 16:40 le 9 mai capture la confirmation finale.

## Filtrage agregat

Pour eviter de moyenner sur des marches a final_abs_move < 3c (signal noye dans le bruit ~1c de la grille minute), on agrege uniquement sur **3 events** (rejet de 2 a faible move).

## Courbe de slippage news-ancree (agregee)

Fraction du move final deja accomplie a T0+X. 0% = rien n'a bouge ; 100% = tout le move est deja la ; >100% = overshoot puis retour ; <0% = mouvement initial en sens contraire.

| Delai | n | p25 | mediane | p75 |
|------:|--:|----:|--------:|----:|
| T+60s | 3 | +0.0% | +0.0% | +0.0% |
| T+300s | 3 | +49.7% | +99.4% | +99.4% |
| T+900s | 3 | +50.0% | +100.0% | +100.0% |
| T+3600s | 3 | +50.0% | +100.0% | +100.0% |

## Latence news -> premier move (>= 2c)

- events avec premier move detectable : **4 / 5**
- p25 : 2.0min
- **mediane : 4.0min**
- p75 : 40.0min

## Comparaison endogene vs news-ancree

Le script `market_reaction_time.py` ancre T0 sur le **debut endogene** du saut (premier tick ou l'amplitude depasse le seuil sur 5 min) ; il observe donc, par construction, ~0% deja accompli a T+60s.

Le script news-ancree ancre T0 sur le **timestamp publie** ; la fraction deja accomplie a T+60s reflete la latence humaine + propagation order-flow.

| Delai | endogene (rapport precedent) | news-ancree (ici) |
|------:|-----------------------------:|------------------:|
| T+60s  | +0.0% | +0.0% |
| T+5min | +115.4% | +99.4% |
| T+15min | +100.0% | +100.0% |

Si la valeur news-ancree a T+60s est > 0%, cela signifie qu'**une partie du move s'est faite avant que T0 ne soit declenche** -- typique d'une fuite ou d'une diffusion par etapes (cas elections : la news se diffuse par paliers).

## Verdict -- fenetre operationnelle du bot

- A T+300s la mediane du move accompli atteint ~50%. **Le bot doit reagir avant cette borne** pour capter la moitie restante du move.

- Latence humaine mediane observee (news -> premier tick >= 2c sur Polymarket) : **~4.0min**. C'est le delai minimal entre la publication et l'apparition d'un signal exploitable sur la chain. Tout pipeline bot-side qui depasse cette enveloppe (fetch leaderboard + scan + reverse-lookup + placement d'ordre) capte deja un marche en mouvement.

**Attention echantillon** : 5 events exploitables (apres rejet des marches sans serie disponible ou crees apres la news). Echantillon trop petit pour des conclusions statistiquement robustes -- chiffres indicatifs.
