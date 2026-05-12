# Temps de réaction Polymarket — 2026-05-11 23:29 UTC

## Paramètres

- Marchés scannés : **50**
- Lookback : **72.0 h**
- Seuil jump : **5.0 ¢** en ≤ **5 min**
- Fenêtre post-jump : **60 min**
- Jumps détectés : **59**

## Courbe de slippage

Part du mouvement final déjà accomplie à T0+X (1.0 = move complet, négatif = retour en arrière, >1.0 = overshoot).

| Délai | médiane | p25 | p75 | n |
|------:|--------:|----:|----:|--:|
| T+30s | +0.0% | +0.0% | -0.0% | 59 |
| T+60s | +0.0% | +0.0% | +4.4% | 59 |
| T+300s | +115.4% | +62.8% | +150.0% | 59 |
| T+900s | +100.0% | +45.6% | +111.1% | 59 |
| T+3600s | +100.0% | +100.0% | +100.0% | 59 |

## Temps de convergence

- médiane : **540s**
- p25 : 510s
- p75 : 690s
- p90 : 780s

## Breakdown par catégorie

| Cat | n | p50 @60s | p50 @300s | p50 @900s |
|:----|--:|---------:|----------:|----------:|
| SPORTS | 28 | +0.0% | +118.2% | +100.0% |
| OTHER | 19 | +1.3% | +84.6% | +75.5% |
| ECONOMICS | 12 | +0.0% | +127.7% | +113.2% |

## Breakdown par liquidité

| Bande | n | p50 @60s | p50 @300s | p50 @900s |
|:------|--:|---------:|----------:|----------:|
| low (5k-50k) | 15 | +0.0% | +118.2% | +100.0% |
| mid (50k-500k) | 35 | +0.0% | +86.7% | +100.0% |
| high (>500k) | 9 | +0.8% | +200.0% | +80.0% |

## Recommandation latence

⚠️ Limite méthodologique : l'endpoint `prices-history` a une résolution effective ≈ 1 point / minute. Les deltas T+30s sont donc dominés par 0% (même minute que T0) ; T+60s est le premier point réellement informatif.

À T+60s la médiane du move déjà accompli est +0.0%, il reste donc **+100.0%** du gain à capturer. C'est la borne au-delà de laquelle copier devient peu rentable.
