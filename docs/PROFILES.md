# Référence des profils TOML

Un profil est un fichier TOML dans `configs/profiles/<nom>.toml` qui contient **toute la stratégie** (sizing, filtres, exits, fallbacks, persistance). Le bot le charge avec :

```bash
uv run pmbot auto-loop --dry-run --profile <nom> --run <run-id>
uv run pmbot auto-loop --live  --profile <nom>
```

## Profils livrés

| Profil | Esprit | Sizing | Filtres | Fallbacks |
|---|---|---|---|---|
| `baseline` | référence dormante | 10% | standards | noise OFF, BTC OFF, persist OFF |
| `aggressive` | levier élevé | 25% | standards | noise ON, BTC ON, persist ON |
| `aggressive-live` | live adapté dry-run | 18% | live | noise ON, BTC ON, persist ON |
| `live-90` | reproduction prod live | 18% | live | noise ON, BTC ON, persist ON |
| `tight-filters` | sélectif premium | 10% | très serrés | noise OFF, deep OFF |

Tu peux créer le tien en copiant `baseline.toml` puis en ajustant.

## Hiérarchie env vs profil

```
.env  >  --no-persistence flag  >  profil TOML  >  défauts code
```

Une variable définie dans `.env` **écrase** la valeur du profil (cf `apply_profile_to_env(override=False)` dans `profiles.py`). C'est voulu pour permettre les overrides ponctuels en CLI, mais ça veut dire que **si une clé stratégie est dans ton `.env`, le profil ne peut pas la changer**.

Cible : `.env` ne contient que secrets (`PRIVATE_KEY`, `API_KEY`...), endpoints (`CLOB_URL`, `GAMMA_URL`...) et machine state (`ASSUME_LIVE_BALANCE_USD`, `SYNC_LIVE_POSITIONS`). Tout le reste dans le profil.

---

## Section `[run]`

| Clé | Type | Rôle |
|---|---|---|
| `starting_cash` | float | Cash de départ pour un dry-run (`data/dry_runs/<run>/state.json`). Ignoré en live (cash synchronisé depuis Polymarket). |

---

## Section `[sizing]` — taille des positions

| Clé | Type | Défaut | Rôle |
|---|---|---|---|
| `position_pct` | float | 0.18 | Fraction du cash utilisée pour calculer le stake de base (`cash × position_pct`). |
| `max_position_ceiling_usd` | float | 150 | Plafond absolu par trade en $. Le sizing dynamique ne dépasse jamais ça. |
| `max_position_ceiling_pct` | float | 0.30 | Plafond en % de l'equity (`equity × pct`). Le ceiling effectif est `max(ceiling_usd, ceiling_pct × equity)`. |
| `max_trade_usd` | float | 0 | Hard cap absolu par trade, **par-dessus** le ceiling dynamique. `0` = désactivé. |
| `high_conviction_balance_fraction` | float | 0.30 | Fraction max du cash autorisée pour une seule position high-conviction (5+ wallets, $5k+ copié). |
| `cash_floor_pct` | float | 0.05 | Réserve de cash : le bot ne déploie jamais ces derniers X%. |
| `min_open_positions` | int | 7 | Cible de positions ouvertes. Sizing dynamique pousse vers cet objectif. |
| `starter_trade_usd` | float | 15 | Stake plancher pour ouvrir une position à 0 (sinon arrondi à 0). |
| `assumed_live_balance_usd` | float | 0 | Bankroll présumée en live au démarrage avant la 1ʳᵉ synchro CLOB. `0` = ne pas pré-supposer. |

## Section `[trader_cohort]` — sélection des wallets smart-money

| Clé | Type | Rôle |
|---|---|---|
| `leaderboard_window` | str | Fenêtre primaire si `leaderboard_windows` vide. Valeurs : `WEEK`, `MONTH`, `ALL`. |
| `leaderboard_windows` | str | CSV de fenêtres à interroger (`"WEEK,MONTH,ALL"`). Utilisé par le filtre persistance. |
| `top_n` | int | Nb de wallets par catégorie (8 catégories) à récupérer. |
| `min_trader_pnl` | float | PnL net minimum du wallet sur la fenêtre ($). |
| `min_trader_volume` | float | Volume minimum tradé ($). Filtre les lucky-strikes. |
| `min_trader_roi` | float | ROI minimum (PnL/volume). 0.02 = 2%. |
| `trade_fetch_concurrency` | int | Nb de workers parallèles pour fetcher les trades de la cohorte. |
| `trade_lookback_minutes` | int | Fenêtre des BUYs récents à récupérer par wallet. |

## Section `[discovery]` — découverte des marchés Gamma

| Clé | Type | Rôle |
|---|---|---|
| `scan_limit` | int | Nb max de marchés ramenés par batch Gamma (3 batches par tick : récent, volume, keywords). |
| `soon_hours` | int | Horizon temporel du scan : marchés qui ferment dans les X heures. |
| `fresh_signal_bonus` | float | Bonus de score appliqué quand le BUY le plus récent est < 5 min. |
| `priority_category_bonus` | float | Bonus de score pour les catégories priorisées (politique, économie). |

## Section `[race]` — clés v4+ (weather-only 2026-07-06, full-deploy 2026-07-09)

| Clé | Type | Rôle |
|---|---|---|
| `weather_only` | bool | **Lane MÉTÉO UNIQUEMENT (ACTIVE, 2026-07-06).** `true` → la sélection ne garde QUE les marchés météo/température (`is_weather_market` : temperature, °C/°F, weather, rainfall, snowfall, high/low temp) et bypasse le ban météo normal. Tout le reste est écarté. Env var : `POLYMARKET_RACE_WEATHER_ONLY`. |
| `full_deploy` | bool | **Sizing FULL-DEPLOY (ACTIF, 2026-07-09 — « 100% of the account is always invested »).** `true` → chaque tick répartit TOUT le cash dispo sur les picks (cash/N), borné par le cap de diversification ci-dessous ; le cash résiduel repart via la top-up lane. OVERRIDE `fixed_stake_usd`. Env var : `POLYMARKET_RACE_FULL_DEPLOY`. |
| `full_deploy_max_position_pct` | float | **Cap de diversification (2026-07-10, « positions at $90 when bankroll total is $200 is not acceptable »).** Aucune position ne peut dépasser cette fraction de l'équité (défaut profils **0.05** = 5%, plancher $5 pour le minimum Polymarket). Le bankroll se répartit sur ≥20 marchés météo distincts quand ils existent ; le cash que le cap ne peut pas placer attend de nouveaux marchés au lieu de s'empiler. 0 = sans cap (comportement 2026-07-09). Env var : `POLYMARKET_RACE_FULL_DEPLOY_MAX_POSITION_PCT`. |
| `topup_dry_ticks` | int | **Patience de redistribution (2026-07-11).** Nombre de ticks consécutifs SANS nouveau marché éligible avant que le cash restant soit redistribué ÉQUITABLEMENT (cash/N) sur toutes les positions existantes dont le marché repasse encore les filtres d'entrée. Cette redistribution égale est EXEMPTE du cap 5% (« it doesnt need to be 5%... as long as its equally distributed »). Un nouveau marché remet le compteur à zéro. Défaut profils **3** ; 0 = jamais. Env var : `POLYMARKET_RACE_TOPUP_DRY_TICKS`. |
| `fixed_stake_usd` | float | **Sizing dollar fixe (RETIRÉ 2026-07-09 — c'est le rollback du full-deploy).** > 0 → chaque trade mise EXACTEMENT ce montant ($5), plafonné seulement par le cash dispo. Désactive Kelly / %-equity / martingale / averaging / double-down / scaling. Ignoré si `full_deploy = true`. 0 = off. |
| `max_price_hard_cap` | float | Plafond ABSOLU du prix d'entrée (ask). L'entrée est clampée à ce prix quel que soit `max_price`, donc 0.97/0.98/0.99 jamais tradables. 0 = désactivé. |
| `unban_all_markets` | bool | `true` → `is_excluded_market` est bypassé à la sélection : toutes les catégories autorisées, gouvernées par l'auto-disable data-driven (`categories.py`). Sans effet pratique sous `weather_only`. Env var : `POLYMARKET_UNBAN_ALL_MARKETS`. |
| `category_min_samples` | int | Taille d'échantillon avant qu'une catégorie puisse être auto-désactivée (défaut 100). 0 = auto-disable off. |
| `category_disable_roi` | float | Une catégorie avec ≥ `category_min_samples` trades réalisés ET un ROI < ce seuil (défaut −0.05) est retirée de la sélection. `other` jamais désactivée ; `weather` jamais désactivée tant que `weather_only` est ON (garde anti-famine, 2026-07-10). |
| `min_edge` | float | **Gate EV opt-in (`forecast.py`).** > 0 → ne trade que si `predicted_probability − ask ≥ min_edge`. 0 = off. Cible recommandée après données : 0.03. |
| `min_quality_score` | float | **Gate qualité opt-in.** > 0 → ne trade que si `quality_score` (0–100 ; edge / volume / clarté résolution / ROI catégorie & bucket) ≥ seuil. 0 = off. Cible : 70. |
| `min_resolution_clarity` | float | **Filtre résolution-safety ALWAYS-ON.** > 0 → skip les marchés au settlement subjectif/ambigu (`resolution_clarity` < seuil ; defaut profils 60). Sans historique requis — reste actif sous `unban_all_markets`. 0 = off. |
| `forecast_prior` | float | Prior du modèle (taux de victoire global) utilisé sans historique (défaut 0.95). |
| `forecast_pseudo_count` | float | Pseudo-compte de shrinkage vers le prior (défaut 20). |
| `preferred_volume_usd` | float | Volume 24h « préféré » pour le sous-score liquidité du quality_score (défaut 5000). |
| `promotion_min_trades` / `promotion_min_roi` | int / float | Gate de promotion (reporting) : scaler seulement après ≥ N trades ET ROI ≥ seuil (défaut 500 / 0.05). |

## Section `[market_filters]` — exigences minimales sur le marché

| Clé | Type | Rôle |
|---|---|---|
| `min_liquidity_usd` | float | Liquidité du carnet d'ordres minimum. |
| `min_volume_usd` | float | Volume historique total minimum. |

## Section `[filters]` — éligibilité d'un signal smart-money

| Clé | Type | Rôle |
|---|---|---|
| `min_consensus` | int | Nb min de wallets distincts ayant acheté le token (passe **strict**). |
| `fallback_consensus` | int | Idem pour la passe **relaxed** (2ᵉ passe). Souvent plus bas que `min_consensus`. |
| `min_copied_usdc` | float | Somme $ copiée minimum sur le token par la cohorte. |
| `min_trade_usd` | float | Un BUY individuel < ce seuil est ignoré (poussière). |
| `max_chase_premium` | float | `(prix_actuel / prix_payé_cohorte - 1)` max. Évite de courir après une hausse déjà faite. |
| `price_min` / `price_max` | float | Prix d'achat (ask) acceptable. Hors band → reject. |
| `max_absolute_spread` | float | Spread bid/ask en cents. |
| `max_relative_spread` | float | Spread / prix. Combinaison utile sur les marchés à 5-10c. |
| `signal_staleness_seconds` | int | ⚠️ clé legacy : nom en seconds mais env var en **minutes** (`SMART_MAX_SIGNAL_AGE_MINUTES`). Le BUY le plus récent doit dater de moins de X minutes. |
| `min_hours_to_close` | float | Le marché doit fermer dans plus de X heures (pas trop tard). |
| `max_hours_to_close` | float | Et pas plus de X heures (pas trop tôt). |
| `max_orders_per_tick` | int | Cap dur sur le nb d'ordres placés par tick. `0` = illimité. |
| `max_sports_positions` | int | Plafond de positions sports ouvertes simultanément (anti-concentration). |
| `sports_score_penalty` | float | Pénalité de score appliquée aux candidats sports (au-delà du plafond, c'est un downgrade). |

## Section `[crypto]` — règles spécifiques aux marchés crypto

| Clé | Type | Rôle |
|---|---|---|
| `min_buy_price` | float | Prix minimum sur un marché crypto (souvent plus haut que `filters.price_min` : 0.70). |
| `min_hours_to_close` / `max_hours_to_close` | float | Horizon spécifique crypto (souvent plus court). |
| `min_copied_usdc` | float | Seuil $ copié crypto (souvent plus haut : 1000). |
| `min_consensus` | int | Consensus minimum crypto (souvent plus strict : 3). |
| `micro_min_consensus` | int | Idem pour les crypto-micros (sub-$1 quotes). |
| `micro_max_entry_slippage` | float | Slippage max accepté sur les crypto-micros. |
| `micro_max_trade_usd` | float | Plafond dur sur le stake crypto-micro (capé à $5 par défaut). |

## Section `[execution]` — placement d'ordre

| Clé | Type | Rôle |
|---|---|---|
| `max_entry_slippage` | float | Slippage max accepté à l'exécution (fill_price vs expected_price). |
| `pending_order_ttl_seconds` | int | Si un ordre reste pending au-delà de X sec, on le cancel et on retente. |
| `min_sell_usd` | float | SELL d'un montant inférieur ignoré (poussière). |

## Section `[exits]` — sorties de position

| Clé | Type | Rôle |
|---|---|---|
| `take_profit_ladder` | str | Échelle de TP partiels : `"0.25:0.15,0.5:0.25,1.0:0.50,..."` = à +25% vendre 15%, à +50% vendre 25%, etc. |
| `trailing_stop_arm_pct` | float | Arme le trailing stop quand le peak atteint +X%. |
| `trailing_stop_giveback` | float | Sortie si retour à `(1 - giveback) × peak` (typiquement giveback=0.50). |
| `peak_protect_arm_pct` | float | Arme le peak protect quand peak atteint +X% (typiquement +100%). |
| `peak_protect_exit_pct` | float | Sortie si retour à +X% après peak protect armé. |
| `stop_loss_pct` | float | Legacy / compatibility field. The current grinder/live stack does not use stop-loss exits. |
| `stop_loss_min_age_minutes` | int | Legacy / compatibility field kept for older profiles. No-op in the current grinder/live stack. |
| `max_hold_hours` | float | Force-close à X heures de durée (par défaut 24h). |
| `near_expiry_min_profit` | float | Exit profitable juste avant clôture si PnL ≥ +X%. |
| `near_expiry_minutes_to_close` | int | Fenêtre du near-expiry exit (X minutes avant clôture). |
| `resolved_market_threshold` | float | Le marché est considéré résolu si le bid atteint X (grinder : 0.99 depuis le 2026-06-10, repli 0.98). |

## Section `[cohort_exit]` — sortie sur signal cohorte

| Clé | Type | Rôle |
|---|---|---|
| `enabled` | bool | Active la détection. |
| `lookback_minutes` | int | Fenêtre d'observation des SELLs cohorte. |
| `min_age_minutes` | int | Âge minimum de la position avant éligibilité. |
| `min_wallets` | int | Nb min de wallets d'entrée ayant vendu pour déclencher. |

## Section `[deep_fallback]` — 3ᵉ passe smart-money

| Clé | Type | Rôle |
|---|---|---|
| `enabled` | bool | Active la 3ᵉ passe (single-wallet, filtres relâchés). |
| `min_copied_usdc` | float | Seuil $ copié pour cette passe (typiquement plus élevé que strict, ex $250). |

## Section `[reverse_lookup]` — découverte par flux smart-money

| Clé | Type | Rôle |
|---|---|---|
| `enabled` | bool | Active la recherche inverse. |
| `max_tokens` | int | Top N tokens à flux smart-money à requêter Gamma. |
| `min_copied_usdc` | float | Un token est candidat si flux ≥ X $ sur la cohorte. |
| `min_liquidity_usd` / `min_volume_usd` | float | Filtres marché appliqués aux marchés trouvés. |

## Section `[btc_edge]` — modèle Black-Scholes BTC thresholds

| Clé | Type | Rôle |
|---|---|---|
| `enabled` | bool | Active la lane BTC edge. |
| `per_trade_cap_usd` | float | Stake max par trade BTC. |
| `min_edge_over_market` | float | Edge modélisé minimum vs prix marché (0.08 = 8%). |
| `min_buy_price` / `max_buy_price` | float | Band de prix accepté sur un marché BTC. |
| `max_spread` | float | Spread max accepté. |
| `min_trade_usd` | float | Stake plancher. |
| `min_model_probability` | float | Proba modélisée min (filtre les loteries même avec edge). |
| `min_hours_to_close` | float | Le marché doit fermer dans plus de X heures (Black-Scholes a besoin d'un horizon). |
| `volatility_days` | int | Fenêtre de calcul de la vol historique BTC. |

## Section `[noise_fallback]` — paris aléatoires si plus rien

| Clé | Type | Rôle |
|---|---|---|
| `enabled` | bool | ⚠️ Si OFF, le bot reste inactif quand aucun signal smart-money. Recommandé OFF pour la plupart des cas. |
| `max_trades_per_tick` | int | Nb max de trades noise par tick. |
| `stake_usd` | float | Stake hard cap par noise trade ($10 par défaut). |
| `cash_pressure_threshold` | float | Le noise s'arme si `cash_pct > threshold` (ou `positions < min_open_positions`). |
| `min_buy_price` / `max_buy_price` | float | Band de prix sur les candidats noise (typiquement 0.20-0.80, évite les extrêmes). |
| `max_spread` | float | Spread max sur un candidat noise. |

## Section `[auto_tune]` — tightening défensif automatique

| Clé | Type | Rôle |
|---|---|---|
| `enabled` | bool | Active l'auto-tuner. |
| `min_closed_trades` | int | Seuil de trades clos avant que l'auto-tuner ne propose des ajustements (défaut 30). |

## Section `[persistence]` — filtre wallets persistants

| Clé | Type | Rôle |
|---|---|---|
| `enabled` | bool | Active le filtre. Quand OFF, tous les wallets de la cohorte qualifiée passent. |
| `window_days` | int | Profondeur du cache historique. |
| `cache_threshold` | float | Fraction min du cache où le wallet doit apparaître pour passer (0.70 = 70%). |
| `intersect_periods` | str | CSV des leaderboards comparés (`"WEEK,MONTH,ALL"`). |
| `intersect_min` | int | Nb min de listes simultanées où le wallet doit apparaître. Avec `2`, il faut être dans ≥2 des 3 listes. |

Le filtre passe si **(intersect ≥ intersect_min) OU (cache ≥ cache_threshold)**. Tant que le cache n'a pas atteint `window_days / 2` snapshots, il est ignoré (warmup).

## Section `[mirror]` — mode copy-trade d'un ou plusieurs wallets

Activée uniquement quand `[run].mode = "mirror"`. Le bot ne fait alors aucun scan smart-money — il poll les trades des wallets cibles et les reproduit avec un sizing fixe.

| Clé | Type | Rôle |
|---|---|---|
| `target` | str | Wallet unique à copier (legacy, mutuellement exclusif avec `targets`). |
| `targets` | list[str] | Liste de wallets (multi-target). |
| `size_usd` | float | Taille fixe par BUY mirroré (USD). |
| `mirror_sells` | bool | Copier aussi les SELL du wallet cible. Indispensable pour les wallets short-hold. |
| `min_target_stake_usd` | float | Filtre micro-trades côté wallet cible. |
| `max_trade_age_seconds` | int | **Défaut : 60s.** Trades plus vieux que ce seuil sont ignorés. Mettre `0` pour désactiver le filtre (rejouera jusqu'aux 100 dernières trades par wallet — utile pour un bootstrap, dangereux en redémarrage long). |

Le défaut `60s` s'applique à **tous les profils mirror** qui ne déclarent pas explicitement la clé. Un profil hérité (`copy-wallet`, `copy-0x4924`) qui pollait toutes les trades fraîches obtient désormais ce comportement. Pour rejouer un historique au redémarrage, mettre `max_trade_age_seconds = 0` dans le profil.

## Section `[telemetry]` — affichage

| Clé | Type | Rôle |
|---|---|---|
| `quiet` | bool | Mode silencieux : footer 1-ligne par tick + actions exécutées seulement. |
| `auto_interval_seconds` | int | Période entre deux ticks (sleep). |

---

## Cheatsheet : créer un nouveau profil

1. Copier `baseline.toml` en `<nom>.toml` dans `configs/profiles/`.
2. Ajuster les sections pertinentes (n'enlever aucune clé, ça resterait `0` côté env).
3. Lancer : `uv run pmbot auto-loop --dry-run --profile <nom> --run <run-id>`.
4. Vérifier le snapshot : `cat data/dry_runs/<run-id>/config_snapshot.toml` — doit refléter ce que tu attendais.
5. Si des valeurs ne correspondent pas, regarde si `.env` les shadow.

## Cheatsheet : override ponctuel pour un test rapide

```bash
POLYMARKET_SMART_MIN_CONSENSUS=1 \
POLYMARKET_SMART_NOISE_FALLBACK_ENABLED=0 \
uv run pmbot auto-loop --dry-run --profile baseline --run my-test
```

L'env var écrase la valeur du profil pour ce run, sans modifier le TOML.
