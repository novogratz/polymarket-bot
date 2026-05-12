# Analyse YTD — 658 wallets Polymarket

## Vue globale

- Échantillon         : **658 wallets** (filtre n_trades ≥ 5 appliqué amont)
- Total PnL net YTD   : $  -286,768,461
- Médiane PnL net     : $       -725.52
- Moyenne PnL net     : $   -435,818.33
- Winners (PnL > 0)   : 294 (44.7%)
- Losers  (PnL < 0)   : 364 (55.3%)

## Distribution PnL net

**Déciles** (PnL net YTD trié desc)

| Décile | Borne sup ($) | Médiane ($) | Borne inf ($) |
|---:|---:|---:|---:|
| D1 |   11,088,420 |       80,248 |       36,954 |
| D2 |       36,428 |       16,945 |       10,359 |
| D3 |       10,116 |        6,100 |        3,617 |
| D4 |        3,479 |        1,947 |          710 |
| D5 |          646 |            0 |         -723 |
| D6 |         -728 |       -1,524 |       -3,560 |
| D7 |       -3,645 |       -8,693 |      -17,034 |
| D8 |      -17,226 |      -32,990 |      -61,994 |
| D9 |      -63,324 |     -113,881 |     -309,706 |
| D10 |     -310,815 |   -1,198,103 |  -41,564,708 |

**Concentration des gains** (somme PnL positif uniquement)

- Somme totale des gains : $22,620,804
- Top    1% (  6 wallets) capture **64.6%** des gains ($14,609,651)
- Top    5% ( 32 wallets) capture **83.6%** des gains ($18,918,158)
- Top   10% ( 65 wallets) capture **92.0%** des gains ($20,816,328)
- Top   25% (164 wallets) capture **98.7%** des gains ($22,333,228)

## Par top_category (wallet's dominant category)

| Catégorie | N | % | PnL méd | PnL moy | Winners % | WinRate méd | Hold méd (min) | n_trades méd |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| OTHER | 375 | 57% | $-564 | $-157,054 | 46% | 52% | 2,486 | 431 |
| SPORTS | 117 | 18% | $-27,619 | $-1,761,706 | 34% | 25% | 43 | 737 |
| WEATHER | 84 | 13% | $-736 | $-4,405 | 42% | 51% | 66 | 2,013 |
| FINANCE | 33 | 5% | $1,487 | $7,907 | 64% | 58% | 151 | 378 |
| POLITICS | 27 | 4% | $-5,881 | $-775,289 | 37% | 50% | 2,076 | 129 |
| ECONOMICS | 16 | 2% | $6,461 | $-44,133 | 69% | 74% | 8,787 | 92 |
| CULTURE | 6 | 1% | $1,784 | $-914 | 67% | 72% | 1,787 | 270 |

## Cohortes comportementales

Heuristique :
- **sports_hf**       : SPORTS + n_trades ≥ 1000 + hold méd < 60 min (arbitrage live)
- **sports_swing**    : SPORTS + le reste
- **politics_long**   : POLITICS + hold méd ≥ 1000 min (~16h+)
- **politics_quick**  : POLITICS + hold méd < 1000 min
- **other**           : autres catégories

| Cohorte | N | PnL méd | PnL moy | Winners % | WinRate méd | Vol méd | n_trades méd | Hold méd |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| sports_hf | 29 | $-239,040 | $-3,427,152 | 31% | 0% | $1,650,258 | 3,408 | 0m |
| sports_swing | 88 | $-20,338 | $-1,212,866 | 35% | 40% | $1,340,464 | 378 | 104m |
| politics_long | 14 | $216 | $-45,239 | 57% | 70% | $56,868 | 174 | 5,621m |
| politics_quick | 13 | $-13,211 | $-1,561,498 | 15% | 0% | $10,221 | 103 | 0m |
| other | 514 | $-213 | $-116,179 | 47% | 53% | $159,453 | 470 | 1,485m |

## Profil winner vs loser (≥ 5 SELLs matchés)

| Métrique | Winners (n) | Losers (n) | Spread |
|---|---:|---:|---:|
| Échantillon | 240 | 240 | — |
| Win rate médian | 60.4% | 50.0% | +10.4pp |
| Hold méd (min) | 1,725 | 1,147 | +578 |
| Volume BUY méd | 234,775 | 226,760 | +8,016 |
| n_trades méd | 604 | 712 | -108 |
| n_matched_sells méd | 87 | 55 | +32 |

## Suggestions de filtres (basé sur le top 50)

Plages observées chez les meilleurs wallets — à considérer comme **plancher** pour qualifier
un wallet en cohorte smart-money (sélection plus stricte que le simple top leaderboard) :

- **Win rate** (≥ 5 SELLs) : p25 = 48%, médiane = 65%, p75 = 75%
  → suggéré : `MIN_TRADER_WIN_RATE` ≈ **48%** (p25 du top)
- **Volume BUY YTD** : p25 = $439,950, médiane = $1,733,073
  → suggéré : `MIN_TRADER_VOLUME_YTD` ≈ **$439,950**
- **n_trades YTD** : p25 = 203, médiane = 564
- **Hold time médian** : p25 = 99 min, médiane = 976 min

**Distribution catégorie dans le top 50 :**
  - OTHER      :  26 ( 52%)
  - SPORTS     :  19 ( 38%)
  - WEATHER    :   3 (  6%)
  - FINANCE    :   1 (  2%)
  - POLITICS   :   1 (  2%)

## Top 20 (par PnL net YTD)

| # | User | PnL net | Real | Unreal | Vol BUY | n | Win% | Hold méd | Cat |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 0x8a6C6811e8937F9E8aFc… | $11,088,420 | $12,908,263 | $-1,819,843 | $29,770,953 | 2678 | 81% | 179m | SPORTS |
| 2 | VPenguin | $1,081,496 | $1,298,699 | $-217,204 | $4,969,164 | 2261 | 71% | 64m | SPORTS |
| 3 | TheOnlyHuman | $905,649 | $704,223 | $201,426 | $10,516,332 | 2211 | 86% | 196m | SPORTS |
| 4 | denizz | $604,654 | $467,199 | $137,455 | $5,263,477 | 3299 | 56% | 5,399m | OTHER |
| 5 | norrisfan | $494,786 | $375,083 | $119,703 | $2,576,443 | 1215 | 75% | 154m | SPORTS |
| 6 | 4gibg4i3o | $434,646 | $-38,245 | $472,892 | $8,697,347 | 2890 | 32% | 64,760m | OTHER |
| 7 | MrSparklySimpsons | $310,279 | $480,885 | $-170,606 | $17,656,646 | 1310 | 75% | 228m | SPORTS |
| 8 | pikachusplace | $281,082 | $264,937 | $16,145 | $1,528,706 | 554 | 72% | 356m | SPORTS |
| 9 | 0x8b5239494dd65Eed682F… | $265,567 | $294,093 | $-28,526 | $2,495,819 | 1844 | 76% | 36m | FINANCE |
| 10 | nojnn | $249,131 | $268,668 | $-19,537 | $2,713,325 | 2096 | 72% | 10,596m | OTHER |
| 11 | surfandturf | $232,933 | $232,183 | $750 | $2,483,470 | 266 | 80% | 149m | SPORTS |
| 12 | debased | $225,660 | $115,440 | $110,220 | $6,408,144 | 3104 | 52% | 6,275m | OTHER |
| 13 | matanovik | $222,805 | $112,008 | $110,797 | $5,113,775 | 2946 | 55% | 282m | SPORTS |
| 14 | 0x3DFb153c197D4C19D3B3… | $221,295 | $277,550 | $-56,255 | $493,496 | 143 | 100% | 218m | SPORTS |
| 15 | Supah9ga | $181,586 | $167,986 | $13,600 | $2,140,673 | 575 | 88% | 180m | SPORTS |
| 16 | yuahldj | $181,298 | $0 | $181,298 | $24,758 | 5 | 0% | 0m | POLITICS |
| 17 | Ferwhere | $158,487 | $166,015 | $-7,527 | $86,179 | 152 | 28% | 1,677m | OTHER |
| 18 | VladimirPooper | $156,963 | $156,963 | $0 | $422,102 | 201 | 47% | 15,940m | WEATHER |
| 19 | bin8888 | $148,707 | $142,632 | $6,075 | $805,803 | 267 | 97% | 21,639m | OTHER |
| 20 | beachboy4 | $146,167 | $-26,968 | $173,135 | $100,234,763 | 1119 | 17% | 8m | SPORTS |

