# polymarket-bot

Bot Polymarket smart-money copy-trading avec dashboard local, ledger persistant, journal de trades, auto-tuner, et chemin BTC edge optionnel.

La strategie par defaut surveille les achats recents des wallets profitables des leaderboards, exige du consensus sur le meme token, applique des filtres execution serres (spread absolu et relatif, prix, fraicheur, chase premium), puis dimensionne chaque trade en pourcentage du bankroll avec multiplicateur de conviction. Les sorties sont multi-niveaux: take-profit ladder, trailing stop, peak-protect, stop-loss, cohort-sell.

## Installer

```bash
python3 -m pip install -r requirements.txt
```

## Lancer le bot live

Tout est dans le script:

```bash
bash scripts/run_live_70.sh
```

Configure pour un bankroll d'environ $90. Tourne en foreground, `Ctrl+C` pour arreter.

## Commandes CLI

```bash
python3 -B -m polymarket_bot.main auto-loop           # boucle live (ce que lance le script)
python3 -B -m polymarket_bot.main dashboard           # dashboard local http://127.0.0.1:8765
python3 -B -m polymarket_bot.main journal-stats       # stats agregees du journal de trades
python3 -B -m polymarket_bot.main tune-strategy       # lance l auto-tuner manuellement
python3 -B -m polymarket_bot.main bootstrap-creds     # derive les credentials CLOB depuis le wallet
python3 -B -m polymarket_bot.main reset-ledger        # remet le ledger local depuis le live
```

## Stratégie pour gagner de l'argent

### L'edge

L'hypothèse : les wallets en haut des leaderboards mensuels Polymarket avec un PnL positif et un volume significatif ont en moyenne un edge informationnel ou analytique sur les marchés qu'ils tradent. Quand plusieurs de ces wallets achètent le même token dans une fenêtre courte (30 min), le signal collectif est plus fort qu'un wallet isolé. Le bot copie ce flow avec un sizing borné.

Risques que la stratégie évite explicitement :

- **Edge factice** : un wallet chanceux sur un trade isolé. Filtré par seuils ROI / volume / consensus multi-wallets.
- **Mauvaise exécution** : payer le spread efface l'edge entier. Filtré par spread absolu, spread relatif, chase premium maximum.
- **Concentration** : 6 paris sur le même évènement. Filtré par dédup par market et par event-slug pour les sports.
- **Round-trip à zéro** : un winner qui retourne plat. Filtré par take-profit ladder + trailing stop + peak-protect.
- **Drawdown sans sortie** : un loser qui sombre. Filtré par stop-loss après âge minimum.
- **Cohort qui tourne** : les wallets d'entrée vendent. Filtré par cohort-sell detection active (lit les SELL trades du cohort dans la fenêtre de lookback).

### Conditions d'entrée

- Trades BUY récents de wallets leaderboard qui passent les planchers PnL ($1k+), volume ($2k+), ROI (3%+).
- Consensus multi-wallets sur le même token (relâché en passes de fallback quand sous le target d'ouvertures).
- Assez d'USDC copié pour matter, échelonné par tier de conviction.
- Marché tradable : spreads serrés (absolu ≤8c, relatif ≤45%), ask dans 0.03–0.96, pas trop proche de l'expiration.
- Pas de position ouverte existante sur le même marché ni sur le même token. Sports respectent un cap par évènement.
- `POLYMARKET_ENABLE_LIVE_TRADING=1` explicite.

### Sizing par conviction

Chaque trade = `cash * SMART_POSITION_PCT (0.18) * conviction_multiplier`, plafonné par `SMART_MAX_POSITION_CEILING_USD ($150)`.

```
crypto micro                     -> 0.55x
weak (<2-wallet $250)            -> 0.7x
2-wallet $250+                   -> 0.9x
2-wallet $1k+                    -> 1.1x
3-wallet $250+                   -> 1.1x
3-wallet $500+                   -> 1.3x
4-wallet $1k+                    -> 1.6x
4-wallet $2k+                    -> 2.0x
5-wallet $5k+                    -> 2.5x
```

À $90 cash : signal weak ≈ $11, signal mid 4-wallet $1k ≈ $26, very-high 5-wallet $5k ≈ $40.

Le `SMART_CASH_FLOOR_PCT=0.05` redistribue dynamiquement le budget de déploiement entre les opportunités restantes du tick pour viser ~95% du bankroll déployé.

### Sorties (avant chaque nouvelle entrée)

- **Take-profit ladder** par défaut +100% / +200% / +300%, ventes partielles.
- **Trailing stop** armé à +25% de pic, sortie sur 50% de giveback tant que P&L positif.
- **Peak-protect** armé à +100% de pic, sortie sur retour à +40%.
- **Stop-loss** à -40% après 15 min en position (ne tire pas si peak-protect déjà armé).
- **Cohort-sell exit** si un wallet d'entrée a VENDU le token dans la fenêtre lookback.
- **Cohort-silent exit** si aucun wallet du cohort n'a réacheté.
- **Exit positif près de l'expiration** : 20 min avant clôture si P&L ≥+5%.

### Auto-tuner défensif

Le bot adapte ses paramètres à partir des outcomes réels du journal :

| Si | Action |
|---|---|
| Stop-loss > 40% des trades | Resserre `MAX_CHASE_PREMIUM` ×0.80 et `MAX_RELATIVE_SPREAD` ×0.85 |
| Trades consensus=2 PnL moyen < -$0.30 (≥20 sample) | Monte `MIN_CONSENSUS` à 3 |
| Sports PnL moyen < -$0.30 (≥15 sample) | Bump `SPORTS_SCORE_PENALTY` ×1.5 |
| Win rate < 30% | Monte `MIN_COPIED_USDC` ×1.5 |
| PnL moyen < -$0.20 | Réduit `POSITION_PCT` ×0.75 |

Pause sous 30 trades clôturés pour éviter l'overfit. **Défensif uniquement** : resserre après pertes, ne relâche pas après gains. Désactiver : `POLYMARKET_SMART_AUTO_TUNE_ENABLED=0`.

### Ce que la stratégie ne fait pas

- Ne pas inventer une opinion sur un marché sans signal. **No-signal / no-trade** est une position valide.
- Ne pas trader 24/7. Les heures creuses restent creuses.
- Ne pas modifier son code source ni pusher sur git.
- Ne pas appeler de LLM dans la boucle de trading.
- Ne pas garantir de profit. C'est un système qui cherche un edge.

## Capacites du bot

- **Smart-money copy-trading:** scan multi-categories des leaderboards Polymarket, fetch parallele des trades recents, regroupement par token, exigence de consensus multi-wallets.
- **Reverse-lookup:** pour les tokens ou les wallets profitables ont achete mais qui ne sont pas dans le scan Gamma initial, le bot fetche les marches manquants par batches de 20 token-ids et les merge dans le pool eligible.
- **3 passes de scan par tick:** strict, relaxed (consensus floor relache), deep fallback (consensus=1 + filtres assouplis). Une seule fetch leaderboard+trades pour les trois passes.
- **Sizing en pourcentage:** chaque trade = `cash * SMART_POSITION_PCT * conviction_multiplier`, avec ceiling absolu, plancher en cash, et redistribution dynamique du budget restant entre les opportunites restantes pour maintenir un cash floor cible.
- **Multiplicateurs de conviction:** weak 0.7x, mid 0.9x, strong-3-wallet 1.1-1.3x, high-4-wallet 1.6-2.0x, very-high-5-wallet+ 2.5x, crypto-micro 0.55x.
- **Sortie multi-niveaux:** take-profit ladder partiel, trailing stop (arme a +25%, sortie sur 50% giveback), peak-protect (+100% arme, sortie sous +40%), stop-loss -40% (apres 15 min), cohort-sell (active SELL detection 120 min lookback) et cohort-silent (pas de fresh BUY), exit positif pres de l expiration.
- **Trade journal:** chaque position cloturee ecrit une ligne JSON dans `data/trade_journal.jsonl` avec toutes les metadonnees du signal d entree, raison de sortie, P&L realise.
- **Auto-tuner defensif:** chaque tick, lit le journal et applique des overrides bornes a `data/strategy_overrides.json` quand les filtres sont trop laches. Pause sous 30 trades cloture pour eviter l overfit. Defensif seulement: serre apres pertes, ne relache pas apres gains.
- **BTC edge integre:** apres chaque tick smart-money, le modele Black-Scholes-from-volatility de `bitcoin.py` est interroge. Si l edge depasse `BTC_MIN_EDGE` (defaut 8%), un petit trade $5 est place. Discipline, pas de "achete a 0.95 c est facile".
- **Noise fallback:** quand les 3 passes smart-money retournent 0 ET que le nombre de positions ouvertes est sous `MIN_OPEN_POSITIONS`, jusqu a 2 trades $5 sont places sur les meilleurs candidats Gamma (mid-priced, tight spread). Trades tagges `noise_fallback` dans le journal pour mesurer le cout.
- **Sync live:** chaque tick synchronise les positions live Polymarket dans le ledger et rafraichit le cash USDC live.
- **Dashboard:** http://127.0.0.1:8765 actualise toutes les 5 secondes avec equity, positions ouvertes, trades recents, candidats, raisons de rejet.

## Auto-tuner

Le bot adapte ses parametres a partir des outcomes reels du journal:

| Si | Action |
|---|---|
| Stop-loss > 40% des trades | Serre `MAX_CHASE_PREMIUM` x0.80 et `MAX_RELATIVE_SPREAD` x0.85 |
| Trades consensus=2 PnL moyen < -$0.30 (>=20 sample) | Monte `MIN_CONSENSUS` a 3 |
| Sports PnL moyen < -$0.30 (>=15 sample) | Bump `SPORTS_SCORE_PENALTY` x1.5 |
| Win rate < 30% | Monte `MIN_COPIED_USDC` x1.5 |
| PnL moyen < -$0.20 | Reduit `POSITION_PCT` x0.75 |

Les overrides sont ecrits dans `data/strategy_overrides.json` et appliques en plus des env vars chaque tick. Pour desactiver: `POLYMARKET_SMART_AUTO_TUNE_ENABLED=0`.

Le bot ne pousse pas de code git automatiquement et ne se modifie pas a l execution. Les ajustements sont des donnees, pas du code.

## Variables d environnement principales

```bash
# Authentification (obligatoire pour live)
POLYMARKET_ENABLE_LIVE_TRADING=1
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_FUNDER_ADDRESS=0x...
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...

# Sizing
POLYMARKET_SMART_POSITION_PCT=0.18
POLYMARKET_SMART_MAX_POSITION_CEILING_USD=150
POLYMARKET_SMART_CASH_FLOOR_PCT=0.05
POLYMARKET_SMART_HIGH_CONVICTION_BALANCE_FRACTION=0.15
POLYMARKET_MAX_POSITION_USD=7
POLYMARKET_SMART_MAX_TRADE_USD=7

# Cohort de wallets
POLYMARKET_SMART_TIME_PERIOD=MONTH
POLYMARKET_SMART_LEADERBOARD_LIMIT=100
POLYMARKET_SMART_MIN_TRADER_PNL=1000
POLYMARKET_SMART_MIN_TRADER_VOLUME=2000
POLYMARKET_SMART_MIN_TRADER_ROI=0.03
POLYMARKET_SMART_TRADE_FETCH_CONCURRENCY=24

# Filtres entree
POLYMARKET_SMART_MIN_CONSENSUS=2
POLYMARKET_SMART_FALLBACK_CONSENSUS=2
POLYMARKET_SMART_MIN_COPIED_USDC=75
POLYMARKET_SMART_MAX_CHASE_PREMIUM=0.13
POLYMARKET_SMART_MAX_ENTRY_SLIPPAGE=0.12
POLYMARKET_SMART_MIN_BUY_PRICE=0.03
POLYMARKET_SMART_MAX_BUY_PRICE=0.96
POLYMARKET_SMART_MAX_SPREAD=0.08
POLYMARKET_SMART_MAX_RELATIVE_SPREAD=0.45
POLYMARKET_SMART_MAX_SIGNAL_AGE_MINUTES=10
POLYMARKET_SMART_TRADE_LOOKBACK_MINUTES=30

# Discovery
POLYMARKET_SMART_REVERSE_LOOKUP_ENABLED=1
POLYMARKET_SMART_REVERSE_LOOKUP_MAX_TOKENS=100
POLYMARKET_SMART_REVERSE_LOOKUP_MIN_COPIED_USDC=50

# Activite minimum
POLYMARKET_MIN_OPEN_POSITIONS=7
POLYMARKET_SMART_DEEP_FALLBACK_ENABLED=1
POLYMARKET_SMART_DEEP_FALLBACK_MIN_COPIED_USDC=50
POLYMARKET_SMART_NOISE_FALLBACK_ENABLED=1
POLYMARKET_SMART_NOISE_FALLBACK_MAX_TRADES_PER_TICK=2
POLYMARKET_SMART_NOISE_FALLBACK_MAX_TRADE_USD=5

# Sorties
POLYMARKET_SMART_TAKE_PROFIT_TIERS=1.0:0.50,2.0:0.25,3.0:0.15
POLYMARKET_SMART_PEAK_PROTECT_TRIGGER=1.0
POLYMARKET_SMART_PEAK_PROTECT_FLOOR=0.40
POLYMARKET_SMART_TRAILING_STOP_ARM_PCT=0.25
POLYMARKET_SMART_TRAILING_STOP_GIVEBACK_PCT=0.50
POLYMARKET_SMART_STOP_LOSS_PCT=0.40
POLYMARKET_SMART_STOP_LOSS_MIN_AGE_MINUTES=15
POLYMARKET_SMART_COHORT_EXIT_ENABLED=1
POLYMARKET_SMART_COHORT_EXIT_LOOKBACK_MINUTES=120
POLYMARKET_SMART_COHORT_EXIT_MIN_AGE_MINUTES=20
POLYMARKET_SMART_EXIT_MINUTES_TO_CLOSE=20
POLYMARKET_SMART_EXIT_MIN_PROFIT=0.05

# BTC edge integre
POLYMARKET_BTC_EDGE_INTEGRATED=1
POLYMARKET_BTC_MAX_TRADE_USD=5
POLYMARKET_BTC_MIN_EDGE=0.08
POLYMARKET_BTC_MAX_SPREAD=0.04

# Auto-tuner
POLYMARKET_SMART_AUTO_TUNE_ENABLED=1
POLYMARKET_SMART_AUTO_TUNE_MIN_TRADES=30

# Chemins
POLYMARKET_STATE_PATH=data/paper_state.json
POLYMARKET_TRADE_JOURNAL_PATH=data/trade_journal.jsonl
POLYMARKET_STRATEGY_OVERRIDES_PATH=data/strategy_overrides.json

# Loop
POLYMARKET_AUTO_INTERVAL_SECONDS=20
POLYMARKET_SYNC_LIVE_POSITIONS=1
```

La liste complete des variables est dans `polymarket_bot/config.py`. Le `scripts/run_live_70.sh` est la source de verite pour la config live actuelle.

## Identifiants API

Le placement d ordres live CLOB exige le set CLOB en trois parties:

```bash
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
```

Les credentials relayer (`RELAYER_API_KEY`, `RELAYER_API_KEY_ADDRESS`) sont differents et ne suffisent pas pour le chemin actuel d ordres CLOB. Si seuls les credentials relayer sont configures, le bot scanne mais refuse les ordres avec une erreur claire.

## Trade journal et tuning

Apres quelques heures de trading:

```bash
python3 -B -m polymarket_bot.main journal-stats
```

Affiche win rate global, P&L total, breakdown par categorie / consensus / strategie / raison de sortie / bucket de prix d entree, et suggestions de tightening si le sample depasse 30 trades.

```bash
python3 -B -m polymarket_bot.main tune-strategy
```

Lance le tuner manuellement et ecrit `data/strategy_overrides.json`. Les overrides sont aussi calcules automatiquement chaque tick si `POLYMARKET_SMART_AUTO_TUNE_ENABLED=1`.

## Dashboard

```bash
python3 -B -m polymarket_bot.main dashboard
```

Ouvre `http://127.0.0.1:8765`. Refresh toutes les 5 secondes: mode du bot, equity, positions ouvertes, trades recents, order IDs, candidats du scanner, raisons de rejet du dernier tick.

## Tests

```bash
python3 -B -m unittest discover -s tests
```

49 tests couvrent ranking, sizing, plans de sortie, regles auto-tuner, et formats d ordres.

## Notes

- Ce n est pas un profit garanti. C est un systeme qui cherche un edge en copiant le flow public, en filtrant l execution, et en serrant les sorties. Le no-signal / no-trade est la position par defaut.
- Le scanner et la selection de trade sont du code Python deterministe sur les APIs Polymarket. Aucun appel LLM dans la boucle de trading.
- Le score du scanner est base sur urgence, liquidite, volume, et tradabilite. Ce n est pas un modele d expected value.
- Le bot n a pas la capacite d ecrire ou de pusher du code source de lui-meme. Les ajustements de strategie sont des fichiers de donnees auditables, pas des modifications de code.

## Docs Agent

- `CLAUDE.md` pour Claude Code (entree principale).
- `AGENTS.md` pour les agents type Codex.
- `docs/AUTONOMOUS_STRATEGY.md` pour les regles et le comportement du dashboard.
