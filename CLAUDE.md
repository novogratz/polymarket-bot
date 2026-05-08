# Guide Claude Code

Fichier d'entrée Claude Code pour le bot Polymarket.

## Sécurité

- Ne jamais révéler les valeurs de `.env`, clés privées, secrets API ou passphrases.
- Ne pas contourner `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- Ne pas implémenter de trades live aléatoires ou non filtrés. Le chemin `noise_fallback` est la seule voie de trade forcé et est plafonné à $5 par trade et 2 trades par tick.
- Préserver le ledger local `data/paper_state.json` sauf si l'utilisateur demande un reset explicite.
- Préserver `data/trade_journal.jsonl` et `data/strategy_overrides.json` sauf si l'utilisateur demande un reset explicite.
- Aucun appel LLM (Claude, Codex, autre) dans le chemin de scan ou de sélection de trade. Le scanner reste du Python déterministe sur les APIs Polymarket.
- Le bot n'a pas la capacité d'écrire ni de pusher du code source de lui-même.

## Carte du projet

- `polymarket_bot/main.py` : commandes CLI et boucles de stratégie. Orchestration du tick, helpers de sizing, écriture du trade journal, commandes `journal-stats` et `tune-strategy`.
- `polymarket_bot/smart_money.py` : récupération des leaderboards, fetch parallèle des trades, regroupement de signaux, scoring, helper de reverse-lookup.
- `polymarket_bot/auto_tuner.py` : lit le trade journal à chaque tick et calcule des overrides de stratégie bornés (défensif uniquement — resserre après pertes).
- `polymarket_bot/bitcoin.py` : modèle BTC threshold edge (Black-Scholes via volatilité).
- `polymarket_bot/trading.py` : placement d'ordres BUY/SELL live authentifiés et calcul final du stake.
- `polymarket_bot/dashboard.py` : dashboard HTML temps réel local sur `http://127.0.0.1:8765`.
- `polymarket_bot/portfolio.py` : ledger local avec cash, positions ouvertes, ordres pending et historique de sorties.
- `polymarket_bot/gamma.py` : client Gamma (scan markets + reverse-lookup par clob_token_ids).
- `polymarket_bot/strategy.py` : ranking des candidats depuis les payloads Gamma.
- `polymarket_bot/models.py` : dataclasses partagées et helpers de parsing.
- `scripts/run_live_70.sh` : runner live canonique pour bankroll ~$90.
- `tests/test_strategy.py` : 49 tests sur scoring, sizing, plans de sortie, règles auto-tuner.

## Workflow de développement

Tests :

```bash
python3 -B -m unittest discover -s tests
```

Dashboard :

```bash
python3 -B -m polymarket_bot.main dashboard
```

Stats du trade journal (P&L par bucket, win rate, suggestions de tightening) :

```bash
python3 -B -m polymarket_bot.main journal-stats
```

Lancer l'auto-tuner manuellement (écrit `data/strategy_overrides.json`) :

```bash
python3 -B -m polymarket_bot.main tune-strategy
```

Boucle smart-money autonome :

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 python3 -B -m polymarket_bot.main auto-loop
```

## Commande live recommandée

Utiliser le script canonique :

```bash
bash scripts/run_live_70.sh
```

Le script est la source de vérité pour la config live. Réglages actuels :

- `POLYMARKET_ASSUME_LIVE_BALANCE_USD=90`, `POLYMARKET_SYNC_LIVE_POSITIONS=1`.
- Sizing : `POSITION_PCT=0.18`, `MAX_POSITION_CEILING_USD=150`, `CASH_FLOOR_PCT=0.05` (~95% de déploiement), `MIN_OPEN_POSITIONS=7`.
- Cohort traders : leaderboard `MONTH`, top 100, `MIN_TRADER_PNL=$1k`, `MIN_TRADER_VOLUME=$2k`, `MIN_TRADER_ROI=3%`. Fetch parallèle `TRADE_FETCH_CONCURRENCY=24`.
- Discovery : scan Gamma standard + scan keyword + reverse-lookup des 100 tokens à plus de $50 de flow smart-money pas dans le scan initial.
- Filtres entrée : `MIN_CONSENSUS=2`, `MIN_COPIED_USDC=$75`, `MAX_CHASE_PREMIUM=0.13`, bande de prix 0.03–0.96, spread absolu ≤8c, spread relatif ≤45%, fraîcheur signal ≤10 min.
- 3 passes de scan par tick : strict → relaxed → deep fallback.
- Sorties : take-profit ladder `1.0:0.50,2.0:0.25,3.0:0.15`, peak-protect armé à +100% sortie sous +40%, trailing stop armé à +25% sortie sur 50% de giveback, stop-loss à -40% (après 15 min en position), cohort-sell exit (active SELL detection sur 120 min), exit positif près de l'expiration.
- BTC edge intégré : à la fin de chaque tick smart-money, `btc_edge_once` tourne avec cap $5 et 8% d'edge minimum sur le prix de marché.
- Noise fallback : quand les 3 passes smart-money retournent 0 et que les positions ouvertes sont sous `MIN_OPEN_POSITIONS`, jusqu'à 2 trades $5 sur les meilleurs candidats. Tagués `noise_fallback` dans le journal.
- Auto-tune : `SMART_AUTO_TUNE_ENABLED=1` (en pause sous 30 trades clôturés ; défensif uniquement).

Dashboard sur `http://127.0.0.1:8765` par défaut.

## Séquence d'un tick

Chaque tick imprime la progression sur stdout puis un résumé JSON. Ordre :

1. Auto-tune : lit le journal, calcule des overrides si ≥30 trades clôturés, applique sur les settings env.
2. Charge les marchés Gamma (scan + scan keyword).
3. Sync les positions live Polymarket dans le ledger.
4. Refresh le cash USDC live depuis le CLOB.
5. Détection cohort-exit (SELL actif des wallets d'entrée, ou pas de fresh BUY).
6. Stratégie de sortie : take-profit ladder, trailing stop, peak-protect, stop-loss, cohort exits, near-expiry.
7. Scan smart-money : strict → relaxed → deep fallback. Une seule fetch leaderboard+trades partagée.
8. Reverse-lookup des tokens à fort flow pas dans les candidats actuels ; merge dans le pool éligible.
9. Place les trades depuis la liste d'opportunités avec sizing dynamique par slot vers le cash floor.
10. Noise fallback (si activé et sous `MIN_OPEN_POSITIONS`).
11. Tick BTC edge (si activé).
12. Persiste le portfolio + écrit les entrées du journal pour les positions clôturées.
13. Imprime le JSON, dort `AUTO_INTERVAL_SECONDS`.

## Stratégie pour gagner de l'argent

La stratégie par défaut est du copy-trading smart-money. Le bot n'invente pas d'opinion sur chaque marché — il attend des preuves d'order-flow public que des wallets profitables achètent le même token, puis miroite ce flow avec un sizing borné.

### L'edge

L'hypothèse : les wallets qui apparaissent en haut des leaderboards mensuels Polymarket avec un PnL positif et un volume significatif ont en moyenne un edge informationnel ou analytique sur les marchés qu'ils tradent. Quand plusieurs de ces wallets achètent le même token dans une fenêtre courte (30 min), le signal collectif est plus fort qu'un wallet isolé. Le bot copie ce flow.

Risques que la stratégie évite :
- Edge factice : un wallet chanceux sur un trade isolé. Filtré par les seuils ROI / volume / consensus multi-wallets.
- Mauvaise exécution : payer le spread et perdre l'edge entier. Filtré par spread absolu, spread relatif, chase premium maximum.
- Concentration : 6 paris sur le même évènement. Filtré par dédup par market + dédup par event-slug pour les sports.
- Round-trip à zéro : un winner qui retourne plat. Filtré par take-profit ladder + trailing stop + peak-protect.
- Drawdown sans sortie : un loser qui sombre lentement. Filtré par stop-loss après âge minimum.
- Cohort qui tourne : les wallets d'entrée vendent. Filtré par cohort-sell detection active.

### Conditions d'entrée

Une entrée live exige :
- Trades BUY récents de wallets leaderboard qui passent les planchers PnL, volume, ROI.
- Consensus multi-wallets sur le même token (relâché en passes de fallback quand sous le target de positions ouvertes).
- Assez d'USDC copié pour matter, échelonné par tier de conviction.
- Marché tradable : spreads serrés (absolu et relatif), ask dans la bande de prix, pas trop proche de l'expiration.
- Pas de position ouverte existante sur le même marché ni sur le même token. Les sports respectent un cap de concentration par évènement.
- `POLYMARKET_ENABLE_LIVE_TRADING=1` explicite.
- Sizing pondéré par conviction : signaux faibles près du floor, signaux très haute conviction (5+ wallets, $5k+ copié) jusqu'à 2.5× la base, plafonnés par le ceiling per-position.

### Sorties (avant chaque nouvelle entrée)

- Take-profit ladder par défaut +100% / +200% / +300%, ventes partielles.
- Trailing stop armé à +25% de pic, sortie sur 50% de giveback tant que P&L positif.
- Peak-protect armé à +100% de pic, sortie sur retour à +40%.
- Stop-loss à -40% après 15 min en position (ne tire pas si peak-protect déjà armé).
- Cohort-sell exit si un wallet d'entrée a VENDU le token dans la fenêtre lookback ; cohort-silent exit si aucun wallet du cohort n'a réacheté.
- Exit positif près de l'expiration : 20 min avant clôture si P&L ≥+5%.

### Sizing par conviction

```
crypto micro                     -> 0.55x
weak (sous 2-wallet $250)        -> 0.7x
2-wallet $250+                   -> 0.9x
2-wallet $1k+                    -> 1.1x
3-wallet $250+                   -> 1.1x
3-wallet $500+                   -> 1.3x
4-wallet $1k+                    -> 1.6x
4-wallet $2k+                    -> 2.0x
5-wallet $5k+                    -> 2.5x
```

Le multiplicateur est appliqué sur `cash * SMART_POSITION_PCT`, plafonné par `SMART_MAX_POSITION_CEILING_USD` et `SMART_CRYPTO_MICRO_MAX_TRADE_USD`.

### Auto-tuner défensif

L'auto-tuner lit le trade journal à chaque tick. À partir de 30 trades clôturés :
- Stop-loss > 40% des trades : resserre `MAX_CHASE_PREMIUM` ×0.80 et `MAX_RELATIVE_SPREAD` ×0.85.
- Trades consensus=2 PnL moyen < -$0.30 (≥20 sample) : monte `MIN_CONSENSUS` à 3.
- Sports PnL moyen < -$0.30 (≥15 sample) : bump `SPORTS_SCORE_PENALTY` ×1.5.
- Win rate < 30% : monte `MIN_COPIED_USDC` ×1.5.
- PnL moyen < -$0.20 : réduit `POSITION_PCT` ×0.75.

Défensif uniquement : resserre après pertes, ne relâche pas après gains. Sortir un sample biaisé par variance pour assouplir = amplifier le bruit.

### Pas un profit garanti

L'edge attendu vient de copier le flow public fort en évitant les mauvais fills. Ce n'est pas du profit garanti. **No-signal / no-trade** est une position valide. Le bot n'a pas vocation à trader 24/7 ; les heures creuses restent creuses.
