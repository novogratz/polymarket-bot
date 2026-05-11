# Stratégies d'entrée et de sortie

Document maître expliquant **toutes les lanes** d'achat et toutes les conditions de vente du bot. Aucune ligne de code ici, juste la mécanique métier. Pour les paramètres, voir `docs/PROFILES.md`.

## Vue d'ensemble d'un tick

Chaque tick exécute, dans cet ordre :

1. **Auto-tune** — relit le journal des trades et durcit les seuils si récents trades en perte (jamais l'inverse).
2. **Scan Gamma** — récupère ~640 marchés actifs candidats.
3. **Sync positions live** + refresh USDC (CLOB).
4. **Exits cohorte** — détecte les SELL ou le silence de la cohorte d'entrée.
5. **Exits techniques** — TP ladder, trailing, peak protect, stop-loss, near-expiry, max-hold, resolved market.
6. **Smart-money — strict** (1ʳᵉ passe).
7. **Smart-money — relaxed** (2ᵉ passe, si strict=0 et positions < min_open).
8. **Smart-money — deep fallback** (3ᵉ passe, si relaxed=0 et toujours < min_open).
9. **Reverse-lookup** — découvre des tokens à fort flux smart-money non couverts par le scan Gamma.
10. **Placement** des ordres avec sizing conviction-weighted.
11. **Noise fallback** — si toutes les passes smart-money = 0 et conditions remplies.
12. **BTC edge** — modèle Black-Scholes sur les BTC thresholds.
13. **Persist** ledger + journal + tick history.
14. **Sleep** `auto_interval_seconds`.

L'ordre n'est pas accidentel : on vide d'abord (exits), on remplit ensuite (entries), on rattrape en dernier (noise).

---

## Lanes d'achat

Le bot a **6 lanes** d'achat distinctes. Les 4 premières partagent le même fetch leaderboard+trades (un seul appel API par tick).

### Lane 1 — Smart-money strict

C'est le mode normal. Pour entrer :

1. Le wallet doit être dans la **cohorte qualifiée** :
   - Présent dans le leaderboard Polymarket sur les fenêtres configurées (WEEK / MONTH / ALL).
   - PnL net ≥ `min_trader_pnl`, volume ≥ `min_trader_volume`, ROI ≥ `min_trader_roi`.
   - Et si la persistance est active : intersecté dans ≥ N listes simultanément OU présent dans ≥X% du cache historique.

2. Plusieurs wallets de la cohorte ont **acheté le même token** récemment :
   - Nb distinct ≥ `filters.min_consensus` (ou `crypto.min_consensus` sur crypto, ou `crypto.micro_min_consensus` sur crypto-micros).
   - Somme $ copiée ≥ `filters.min_copied_usdc` (ou `crypto.min_copied_usdc`).
   - Le BUY le plus récent date de moins de `signal_staleness_seconds` (réellement minutes — clé legacy).

3. Le **marché passe les filtres d'exécution** :
   - Prix `best_ask` dans `[price_min, price_max]` (ou `crypto.min_buy_price` sur crypto).
   - Spread absolu ≤ `max_absolute_spread`, spread relatif ≤ `max_relative_spread`.
   - Chase premium ≤ `max_chase_premium` (on ne court pas après une hausse déjà payée).
   - `hours_to_close ∈ [min_hours_to_close, max_hours_to_close]`.
   - `accepts_orders = true`, `tick_size` connu.

4. Le **portefeuille a de la place** :
   - Pas d'autre position ouverte sur ce market_id ni ce token_id.
   - Pas de saturation sportive (`max_sports_positions`).
   - Cash disponible ≥ stake calculé.

Tag dans le journal : `smart_money`.

### Lane 2 — Smart-money relaxed

Active **uniquement si la strict pass = 0 signaux** ET `positions_ouvertes < min_open_positions`.

Différences vs strict :
- `min_consensus` remplacé par `fallback_consensus` (typiquement plus bas, parfois 1).

Tout le reste reste identique. Tag : `smart_money_relaxed`.

### Lane 3 — Smart-money deep fallback

Active **uniquement si relaxed = 0** ET toujours `positions_ouvertes < min_open_positions` ET `deep_fallback.enabled = true`.

Différences :
- `min_consensus = 1` (single-wallet autorisé).
- `min_copied_usdc` remplacé par `deep_fallback.min_copied_usdc` (typiquement plus élevé, ex $250).
- Reste des filtres techniques inchangés.

L'idée : accepter un seul gros wallet qui a copié ≥ $250 plutôt que de ne rien faire. Tag : `smart_money_deep`.

### Lane 4 — Reverse-lookup

Tourne en parallèle des passes 1-3. Pour chaque token où le flux smart-money cumulé dépasse `reverse_lookup.min_copied_usdc`, si ce token **n'est pas dans le scan Gamma standard**, on requête Gamma directement par `clob_token_ids`. Si le marché récupéré passe `reverse_lookup.min_liquidity_usd` et `reverse_lookup.min_volume_usd`, il est ajouté au pool des candidats, puis re-soumis aux filtres standards des passes 1-3.

Permet de découvrir des marchés long-tail que le scan Gamma (basé sur `soon_hours`) rate. Tag : `smart_money_reverse_lookup`.

### Lane 5 — BTC edge (Black-Scholes)

Tourne en fin de tick si `btc_edge.enabled = true`. Indépendante de la cohorte smart-money.

Pour chaque marché BTC threshold (« Le BTC atteindra-t-il $X avant date Y ? ») :

1. Calculer la **probabilité modélisée** via Black-Scholes (volatilité historique sur `volatility_days`, taux sans risque ignoré).
2. Comparer à la **probabilité de marché** (prix `best_ask`).
3. Si la différence (edge modélisé vs marché) ≥ `min_edge_over_market` ET les autres filtres passent (`min_buy_price`, `max_buy_price`, `max_spread`, `min_hours_to_close`, `min_model_probability`), placer un BUY.
4. Stake plafonné à `per_trade_cap_usd` (typiquement $5 hard).

Tag : `btc_edge`.

### Lane 6 — Noise fallback

Tourne **uniquement** si toutes les lanes smart-money (passes 1-4) ont produit 0 signaux ET `noise_fallback.enabled = true` ET :
- `positions_ouvertes < min_open_positions` OU
- `cash_pct > cash_pressure_threshold`.

Sélectionne `max_trades_per_tick` marchés depuis les candidats Gamma triés par score, filtrés par `min_buy_price`, `max_buy_price`, `max_spread`. Stake hard cap = `stake_usd` ($10 par défaut).

**Important** : c'est un backstop, pas un mode de trading. L'espérance de gain est ≈ 0 (long-tail Polymarket). Sa fonction est d'éviter que le bot reste 100% en cash sur un démarrage tranquille, pas de générer du PnL. Voir la note "Risques du noise" plus bas.

Tag : `noise_fallback`.

---

## Sizing par conviction

Le stake de base = `cash × position_pct`. Il est ensuite multiplié par un **facteur de conviction** :

| Profil du signal | Facteur |
|---|---:|
| Crypto micro | 0.55× |
| Weak (consensus < 2 ou copied < $250) | 0.7× |
| 2-wallet $250+ | 0.9× |
| 2-wallet $1k+ | 1.1× |
| 3-wallet $250+ | 1.1× |
| 3-wallet $500+ | 1.3× |
| 4-wallet $1k+ | 1.6× |
| 4-wallet $2k+ | 2.0× |
| 5+ wallets $5k+ | 2.5× |

Le résultat est ensuite **borné** par :

1. `min(stake, max(max_position_ceiling_usd, equity × max_position_ceiling_pct))` — ceiling dynamique.
2. `min(stake, max_trade_usd)` si `max_trade_usd > 0` — hard cap absolu.
3. `min(stake, equity × high_conviction_balance_fraction)` — fraction max du cash sur un seul ticker.
4. `min(stake, crypto.micro_max_trade_usd)` si crypto-micro.
5. `max(stake, starter_trade_usd)` si position ouvre depuis 0.

Le cash restant après le trade doit rester ≥ `cash × cash_floor_pct`.

---

## Lanes de vente (exits)

Toutes les exits tournent **avant** les entries. Une position peut sortir par plusieurs raisons concurrentes — la première qui matche déclenche le SELL.

### Exit 1 — Take-profit ladder (partielle)

Configuré via `take_profit_ladder` au format `"seuil:fraction,..."`. Exemple `0.25:0.15,0.5:0.25,1.0:0.50,2.0:0.25,3.0:0.15` :

- À +25% de PnL, vendre 15% des shares.
- À +50%, vendre 25% de plus.
- À +100%, vendre 50%.
- À +200%, vendre 25%.
- À +300%, vendre 15% (solde).

Permet de matérialiser du profit sans tout liquider. Tag : `take_profit_T<n>` (T0, T1, ...).

### Exit 2 — Trailing stop (totale)

S'arme dès que le PnL peak atteint `trailing_stop_arm_pct` (typiquement +25%). Ensuite, exit total si PnL retombe à `(1 - trailing_stop_giveback) × peak` (typiquement giveback=0.50 → exit si on revient à +12.5% après avoir touché +25%).

Sécurise les gains sans bloquer la course haussière. Tag : `trailing_stop`.

### Exit 3 — Peak protect (totale)

Variante "haute conviction" du trailing. S'arme à `peak_protect_arm_pct` (typiquement +100%). Exit si PnL retombe à `peak_protect_exit_pct` (typiquement +40%). Donne plus d'espace que le trailing classique.

Quand peak protect est armé, le **stop loss est désactivé** (la position a déjà prouvé qu'elle peut grimper). Tag : `peak_protect`.

### Exit 4 — Stop loss (totale)

Si PnL < `-stop_loss_pct` (typiquement -40%) ET la position a plus de `stop_loss_min_age_minutes` (typiquement 15 min), exit. Le min-age évite la sortie sur bruit initial du carnet.

Désactivé si peak protect armé. Tag : `stop_loss`.

### Exit 5 — Cohort sell

Si `cohort_exit.enabled = true` ET la position a plus de `cohort_exit.min_age_minutes` :
- Récupère les trades récents (`cohort_exit.lookback_minutes`) des wallets d'entrée.
- Si ≥ `cohort_exit.min_wallets` ont **vendu** ce token dans la fenêtre → exit.

Permet de réagir au flip de la cohorte. Le sizing fetch est parallèle pour ne pas pénaliser le tick. Tag : `cohort_sell`.

### Exit 6 — Near-expiry positive

Si le marché ferme dans moins de `near_expiry_minutes_to_close` (typiquement 20 min) ET PnL ≥ `near_expiry_min_profit` (+5%), exit.

Évite de rester engagé jusqu'à l'expiration où la liquidité s'évapore. Tag : `near_expiry`.

### Exit 7 — Max-hold-time

Si la position a plus de `max_hold_hours` (défaut 24h), force-close. Filet de sécurité pour les positions "oubliées" qui restent à plat. Tag : `max_hold_time`.

### Exit 8 — Resolved market

Si `bid ≥ resolved_market_threshold` (typiquement 0.97), le marché est de facto résolu en notre faveur. Exit pour matérialiser sans attendre le settlement. Tag : `resolved_market`.

### Exit 9 — Cohort silent (variante de cohort exit)

Si aucun wallet de la cohorte n'a re-acheté le token dans la fenêtre `cohort_exit.lookback_minutes` et la position est suffisamment ancienne, considérée comme abandonnée par les wallets, exit. Cas dégénéré du cohort sell. Tag : `cohort_silent`.

---

## Auto-tuner défensif

Tourne au début de chaque tick. Lit le journal des trades clos. Quand ≥ `auto_tune.min_closed_trades` (défaut 30), applique des durcissements **temporaires** (override TTL ~24h) sur les paramètres du profil :

| Condition observée | Ajustement |
|---|---|
| > 40% trades sortis en stop loss | `max_chase_premium ×= 0.80`, `max_relative_spread ×= 0.85` |
| consensus=2 trades avg PnL < -$0.30 (n ≥ 20) | `min_consensus = 3` |
| sports avg PnL < -$0.30 (n ≥ 15) | `sports_score_penalty ×= 1.5` |
| Win rate < 30% | `min_copied_usdc ×= 1.5` |
| Avg PnL < -$0.20 (toutes catégories) | `position_pct ×= 0.75` |

**Asymétrique** : ça durcit après pertes, ça ne relâche jamais après gains. Loosening sur un sample biaisé = amplifier le bruit.

Les overrides sont écrits dans `data/strategy_overrides.json` (ou `data/dry_runs/<run>/overrides.json`) et appliqués au-dessus des valeurs du profil.

---

## Risques évités, lane par lane

| Risque | Filtre qui le neutralise |
|---|---|
| **Fake edge** (1 wallet lucky-strike) | `min_trader_volume`, `min_trader_roi`, `min_consensus`, persistance multi-période |
| **Bad execution** (paye le spread) | `max_absolute_spread`, `max_relative_spread`, `max_chase_premium` |
| **Concentration** (6 paris sur 1 event) | dédup par market_id/token_id/event_slug, `max_sports_positions` |
| **Round-trip to flat** (winner qui rend tout) | TP ladder, trailing stop, peak protect |
| **Drawdown lent** (loser qui bleed) | stop loss après min-age, max-hold-time |
| **Cohort flip** (entrée vend) | cohort sell, cohort silent |
| **Marché illiquide** | `min_liquidity_usd`, `min_volume_usd`, `accepts_orders` |
| **Near-expiry illiquidity** | near-expiry positive exit, `min_hours_to_close` |

## Risques du noise fallback

Le `noise_fallback` n'a **aucun edge informationnel**. Il choisit les marchés au score Gamma, qui privilégie la liquidité et la fraîcheur — pas la qualité du pari. À long terme :

- Espérance ≈ 0 sur la longue traîne Polymarket (sports random, mèmes...).
- Frais d'exécution + spreads consommés à chaque trade.
- Sur 30 noise trades à $10, statistiquement on perd $5-20 nets.

**À éviter pour la prod live**. Utile uniquement pour :
- Tester que la stack place et clôt des ordres réellement (smoke test).
- Forcer le bot à ne pas rester totalement immobile pendant un test (vraiment niche).

Pour un dry-run sérieux ou la prod : `[noise_fallback] enabled = false`. Le bot reste immobile quand il n'y a pas de signal — c'est le comportement attendu.

---

## Décisions de design clés

1. **Pas d'opinion sur les marchés**. Le bot ne modélise pas les outcomes (sauf BTC edge). Il mirror le flux des wallets compétents observés.

2. **Pas de LLM dans la decision path**. Tout est Python déterministe. Reproductible, debuggable, économique. Le LLM peut analyser les résultats (auto-tuner, journal-stats), pas guider les ordres.

3. **Asymétrie défense/offense**. L'auto-tuner durcit mais ne relâche jamais. Les filtres rejettent largement et acceptent prudemment.

4. **Réutilisation des fetches**. Un seul appel leaderboard+trades partagé entre les 3 passes smart-money + le reverse-lookup + l'exit cohorte. Les appels Polymarket sont chers (rate-limited).

5. **Sizing dynamique, pas fixe**. Le stake évolue avec le cash, l'equity, le nb de positions ouvertes, la conviction. Pas de "Kelly criterion" pur, mais une approximation bornée.

6. **Exits avant entries**. Toujours. Une position toxique qui ne sort pas bloque la place d'un meilleur trade.

7. **Le journal est la source de vérité**. Win rate, P&L par bucket, distribution des reasons : tout est dans `data/trade_journal.jsonl` (ou `data/dry_runs/<run>/journal.jsonl`). L'auto-tuner lit ça, pas le ledger. Commande : `pmbot journal-stats`.

---

## Ce que le bot ne fait PAS

- Pas d'arbitrage cross-market.
- Pas de market-making (poser des ordres bid + ask).
- Pas de short-selling explicite (mais shorter un YES = acheter le NO).
- Pas de leverage emprunté.
- Pas de scalping high-frequency (intervalle minimum 10s/tick).
- Pas de réaction sur news (pas de feed externe).
- Pas d'opinion sur l'outcome (sauf BTC).
- Pas de signal d'entrée généré "from scratch" — tout vient soit de la cohorte, soit du noise.
