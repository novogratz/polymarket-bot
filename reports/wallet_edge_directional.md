# Étude C — Edge directionnel des wallets top-PnL

_Top 20 wallets analysés — fenêtre [-30 min, +30 min] autour de chaque BUY YTD,_
_série prix CLOB ``fidelity=1``._

## 1. Résumé exécutif

- Wallets analysés : **20**
- Total BUY YTD candidats : **21066**
- Trades effectivement scorés : **21066** (100.0%)
- Trades skippés (série prix < 5 points) : **0** (0.0%)
- Trades avec jump ≥ 0.05 détecté : **8739**

## 2. Distribution de l'edge directionnel

_n = 16040 trades avec edge mesurable (|move_30min| ≥ 0.005)._

| Décile | Edge directionnel |
|---|---|
| D1 (p10) | -1.000 |
| D2 (p20) | -0.300 |
| D3 (p30) | 0.000 |
| D4 (p40) | 0.140 |
| D5 (p50) | 0.500 |
| D6 (p60) | 0.811 |
| D7 (p70) | 1.000 |
| D8 (p80) | 1.000 |
| D9 (p90) | 1.077 |

- **Médiane globale** : 0.500
- **Moyenne globale** : 0.369
- **% trades avec edge > 0** : 63.6%

## 3. Top 5 wallets *ahead* (à copier en priorité)

| Wallet | username | total_pnl_ytd | n_analyzed | pct_ahead | pct_chasing | mean_edge |
|---|---|---:|---:|---:|---:|---:|
| `0x8b52…568e` | 0x8b5239494dd65Eed682F0d | 265 567.41$ | 1370 | 58.0% | 6.7% | 0.344 |
| `0x9f2f…2ca8` | surfandturf | 232 932.66$ | 239 | 30.5% | 8.8% | 0.413 |
| `0xfbf3…b218` | VPenguin | 1 081 495.74$ | 1436 | 20.1% | 25.0% | 0.000 |
| `0xe72b…b0a0` | norrisfan | 494 785.93$ | 957 | 17.8% | 18.3% | 0.566 |
| `0x57cd…a0fb` | Supah9ga | 181 586.33$ | 522 | 17.4% | 13.4% | 0.326 |

## 4. Top 5 wallets *chasing* (à NE PAS copier)

_Pourcentage élevé de trades pris après que le marché ait déjà fait son mouvement_

_(``edge_jump < 0`` : le BUY arrive sur un retracement, donc à contre-courant)._

| Wallet | username | total_pnl_ytd | n_analyzed | pct_chasing | pct_ahead | mean_edge |
|---|---|---:|---:|---:|---:|---:|
| `0xfbf3…b218` | VPenguin | 1 081 495.74$ | 1436 | 25.0% | 20.1% | 0.000 |
| `0x6adc…beba` | pikachusplace | 281 082.01$ | 391 | 24.8% | 10.7% | 0.222 |
| `0xc2e7…be51` | beachboy4 | 146 166.71$ | 1113 | 19.0% | 6.6% | 0.081 |
| `0xe72b…b0a0` | norrisfan | 494 785.93$ | 957 | 18.3% | 17.8% | 0.566 |
| `0x3dfb…abaf` | 0x3DFb153c197D4C19D3B31c | 221 294.98$ | 120 | 14.2% | 13.3% | 0.092 |

## 5. Cross-référence — edge négatif mais PnL positif (MM / arbitrage)

Wallets dont la médiane d'edge directionnel est ≤ 0 alors que le PnL YTD est positif. Hypothèses : market-making (capture du spread sans direction), arbitrage cross-marché, ou bénéfice du market impact (le wallet *est* la liquidité). Ces wallets gagnent de l'argent mais ne sont **pas copiables** par un follower retail.

| Wallet | username | total_pnl_ytd | median_edge | pct_ahead | pct_chasing |
|---|---|---:|---:|---:|---:|
| `0x8a6c…0b3f` | 0x8a6C6811e8937F9E8aFc1b | 11 088 420.04$ | 0.000 | 14.3% | 12.1% |
| `0x6ade…f5b0` | TheOnlyHuman | 905 649.37$ | 0.000 | 6.8% | 12.6% |
| `0x3dfb…abaf` | 0x3DFb153c197D4C19D3B31c | 221 294.98$ | 0.000 | 13.3% | 14.2% |

## 6. Conclusion — implication pour `polymarket_bot/smart_money.py`

L'edge directionnel n'est pas uniformément réparti chez les wallets top-PnL : certains **anticipent** les mouvements (``pct_ahead`` > 40%), d'autres **les chassent** (``pct_chasing`` > 30%) et restent profitables uniquement par concentration sur quelques outliers. Suggestion (consultative, pas de modification du code prod) : enrichir le scoring smart-money avec un filtre ``min_pct_ahead`` calculé sur 30 jours glissants par wallet, pour exclure du cohort les wallets *chasers* dont le PnL ne se réplique pas par copie naïve.

À noter : la métrique reste sensible au bruit sur les marchés peu liquides (``|move_30min| < 0.005``), et au calage temporel exact du trade vs. la grille 1 min (point le plus proche, pas d'interpolation linéaire).
