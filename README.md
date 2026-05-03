# polymarket-bot

Scanner de marches Polymarket avec dashboard local, portefeuille papier et chemin de trading live authentifie.

La strategie autonome par defaut est du copy trading smart-money : le bot surveille les achats recents de wallets profitables dans les leaderboards, exige un consensus entre plusieurs wallets, filtre les spreads trop larges et les prix incoherents, puis evite les positions deja ouvertes. La strategie BTC edge reste disponible comme strategie optionnelle separee.

## Strategie Pour Gagner De L'argent

Le bot essaie de gagner de l'argent en suivant le flux informe au lieu de deviner les resultats. L'hypothese est que le meilleur signal public n'est pas un titre de marche isole, mais des achats repetes de wallets qui ont recemment bien performe en PnL.

La strategie autonome fonctionne comme suit :

1. Scanner les marches Polymarket actifs, liquides, tradables et qui ferment assez vite pour garder le capital en mouvement.
2. Recuperer les wallets des leaderboards par categorie et garder seulement les traders avec un PnL configure non negatif.
3. Lire leurs achats recents via la Data API publique de Polymarket.
4. Chercher un consensus : au moins `POLYMARKET_SMART_MIN_CONSENSUS` wallets profitables differents doivent avoir achete le meme token recemment.
5. Entrer seulement si le marche a un carnet executable, un spread assez serre, une taille de trades copies suffisante, et un ask dans la bande de prix configuree.
6. Ignorer le trade si le ledger local a deja ce marche/outcome ouvert.
7. Dimensionner depuis le solde USDC live avec `POLYMARKET_TRADE_FRACTION=1.0` par defaut, plafonne par `POLYMARKET_SMART_MAX_TRADE_USD=5` et `POLYMARKET_MAX_POSITION_USD=5`.
8. Synchroniser les positions live Polymarket dans le ledger local pour eviter les vieux etats locaux.
9. Avant chaque nouvelle entree, appliquer une strategie de sortie sur les positions live ouvertes pour prendre du profit et proteger les gros gains.

Ce n'est pas un profit garanti. C'est un systeme qui cherche un edge : copier un flux public fort, eviter les mauvais fills, garder les tailles sous controle, et refuser les trades quand le signal est faible.

## Lancer

```bash
python3 -m pip install -r requirements.txt
python3 -m polymarket_bot.main scan
python3 -m polymarket_bot.main paper-tick
python3 -m polymarket_bot.main bootstrap-creds
python3 -m polymarket_bot.main reset-ledger
python3 -m polymarket_bot.main trade-once
python3 -m polymarket_bot.main smart-money-once
python3 -m polymarket_bot.main smart-money-loop
python3 -m polymarket_bot.main auto-loop
python3 -m polymarket_bot.main btc-edge-once
python3 -m polymarket_bot.main btc-edge-loop
python3 -m polymarket_bot.main dashboard
```

URL du dashboard :

```text
http://127.0.0.1:8765
```

## Configuration

Cree un fichier local `.env` a la racine du projet avec ton wallet et tes parametres de trading.

Variables d'environnement :

```bash
POLYMARKET_SCAN_LIMIT=200
POLYMARKET_SOON_HOURS=72
POLYMARKET_PAPER_BALANCE_USD=20
POLYMARKET_MAX_POSITION_USD=5
POLYMARKET_TRADE_FRACTION=1.0
POLYMARKET_BTC_MIN_MODEL_PROBABILITY=0.90
POLYMARKET_BTC_MIN_BUY_PRICE=0.70
POLYMARKET_BTC_MAX_BUY_PRICE=0.82
POLYMARKET_BTC_MIN_EDGE=0.08
POLYMARKET_BTC_MAX_SPREAD=0.03
POLYMARKET_BTC_MIN_TRADE_USD=1
POLYMARKET_BTC_MAX_TRADE_USD=25
POLYMARKET_BTC_VOLATILITY_DAYS=7
POLYMARKET_AUTO_INTERVAL_SECONDS=10
POLYMARKET_AUTO_MAX_TICKS=0
POLYMARKET_DATA_API_URL=https://data-api.polymarket.com
POLYMARKET_SMART_CATEGORIES=OVERALL,FINANCE,ECONOMICS,TECH,POLITICS,SPORTS,CULTURE,WEATHER
POLYMARKET_SMART_DISCOVERY_KEYWORDS=election,trump,senate,congress,fed,inflation,cpi,unemployment,gdp,weather,rain,snow,hurricane,temperature,box office,movie,earnings,stock,nasdaq
POLYMARKET_SMART_TIME_PERIOD=WEEK
POLYMARKET_SMART_LEADERBOARD_LIMIT=25
POLYMARKET_SMART_SCAN_LIMIT=1000
POLYMARKET_SMART_SOON_HOURS=72
POLYMARKET_SMART_TRADE_LOOKBACK_MINUTES=240
POLYMARKET_SMART_MAX_SIGNAL_AGE_MINUTES=0
POLYMARKET_SMART_FRESH_SIGNAL_BONUS=8
POLYMARKET_SMART_MIN_CONSENSUS=2
POLYMARKET_SMART_FALLBACK_CONSENSUS=2
POLYMARKET_MIN_OPEN_POSITIONS=3
POLYMARKET_STARTER_TRADE_USD=25
POLYMARKET_MIN_ORDER_SHARES=5
POLYMARKET_SMART_MIN_TRADER_PNL=0
POLYMARKET_SMART_MIN_TRADE_USD=1
POLYMARKET_SMART_MIN_COPIED_USDC=50
POLYMARKET_SMART_MIN_BUY_PRICE=0.02
POLYMARKET_SMART_MAX_BUY_PRICE=0.98
POLYMARKET_SMART_MAX_SPREAD=0.10
POLYMARKET_SMART_MIN_HOURS_TO_CLOSE=0.25
POLYMARKET_SMART_MAX_HOURS_TO_CLOSE=72
POLYMARKET_SMART_MAX_CHASE_PREMIUM=0.10
POLYMARKET_SMART_PRIORITY_CATEGORY_BONUS=6
POLYMARKET_SMART_SPORTS_SCORE_PENALTY=8
POLYMARKET_SMART_MAX_SPORTS_POSITIONS=3
POLYMARKET_SMART_MAX_ENTRY_SLIPPAGE=0.10
POLYMARKET_SMART_CRYPTO_MICRO_MIN_CONSENSUS=3
POLYMARKET_SMART_CRYPTO_MICRO_MAX_ENTRY_SLIPPAGE=0.05
POLYMARKET_SMART_CRYPTO_MICRO_MAX_TRADE_USD=5
POLYMARKET_SMART_ALLOW_CRYPTO=0
POLYMARKET_SMART_CRYPTO_MIN_HOURS_TO_CLOSE=6
POLYMARKET_SMART_CRYPTO_MAX_HOURS_TO_CLOSE=48
POLYMARKET_SMART_CRYPTO_MIN_COPIED_USDC=1000
POLYMARKET_SMART_CRYPTO_MIN_CONSENSUS=3
POLYMARKET_SMART_CRYPTO_MIN_BUY_PRICE=0.70
POLYMARKET_SMART_MAX_TRADE_USD=5
POLYMARKET_SMART_HIGH_CONVICTION_BALANCE_FRACTION=0
POLYMARKET_SMART_MAX_ORDERS_PER_TICK=0
POLYMARKET_SMART_TAKE_PROFIT_TIERS=1.0:0.50,2.0:0.25,3.0:0.15
POLYMARKET_SMART_PEAK_PROTECT_TRIGGER=1.0
POLYMARKET_SMART_PEAK_PROTECT_FLOOR=0.40
POLYMARKET_SMART_MIN_SELL_USD=1
POLYMARKET_SMART_EXIT_MINUTES_TO_CLOSE=20
POLYMARKET_SMART_EXIT_MIN_PROFIT=0.05
POLYMARKET_SMART_PENDING_ORDER_TTL_SECONDS=45
POLYMARKET_SYNC_LIVE_POSITIONS=1
POLYMARKET_LIVE_POSITION_MIN_VALUE_USD=1
POLYMARKET_MIN_LIQUIDITY_USD=500
POLYMARKET_MIN_VOLUME_USD=1000
POLYMARKET_DASHBOARD_PORT=8765
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_FUNDER_ADDRESS=0x...
POLYMARKET_SIGNATURE_TYPE=1
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
POLYMARKET_ENABLE_LIVE_TRADING=1
```

`paper-tick` ouvre une position simulee sur le meilleur marche proche, plafonnee par `POLYMARKET_MAX_POSITION_USD`, puis marque les positions simulees au marche.

`bootstrap-creds` derive ou charge les identifiants API Polymarket depuis la cle du wallet.

`reset-ledger` efface les positions locales du dashboard et remet le cash local depuis le solde CLOB live quand les identifiants sont disponibles. Utilise-le apres des trades manuels qui rendent le ledger du dashboard stale. Cette commande ne cancel pas et ne vend pas les positions sur Polymarket.

`trade-once` place un ordre live marketable limit sur le meilleur marche eligible. Il refuse de tourner si `POLYMARKET_ENABLE_LIVE_TRADING=1` n'est pas defini.

`smart-money-once` place un trade live seulement si des wallets profitables ont recemment achete le meme token avec assez de consensus, de taille et de qualite de carnet.

`smart-money-loop` lance la strategie smart-money toutes les `POLYMARKET_AUTO_INTERVAL_SECONDS` secondes. `auto-loop` est un alias pour ce mode autonome par defaut.

Pour scanner plus vite, surcharge l'intervalle au lancement :

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 POLYMARKET_AUTO_INTERVAL_SECONDS=30 python3 -B -m polymarket_bot.main auto-loop
```

Si le CLOB retourne a tort `Live Balance: 0.0` alors que le compte Polymarket est finance, tu peux forcer une balance de sizing explicite. Les ordres restent gates par `POLYMARKET_ENABLE_LIVE_TRADING=1`, les checks smart-money, et le cap par trade :

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 POLYMARKET_ASSUME_LIVE_BALANCE_USD=35 POLYMARKET_MAX_POSITION_USD=5 POLYMARKET_AUTO_INTERVAL_SECONDS=30 python3 -B -m polymarket_bot.main auto-loop
```

Chaque tick smart-money imprime un `scan_report` avec les meilleures opportunites considerees, le signal selectionne s'il existe, les compteurs traders/trades, et les raisons de rejet quand rien ne qualifie. Le scanner n'utilise pas Codex, Claude ou un LLM.

L'univers smart-money utilise `POLYMARKET_SMART_SOON_HOURS=72` par defaut, avec un cap d'entree `POLYMARKET_SMART_MAX_HOURS_TO_CLOSE=72`. Pour forcer des trades plus courts, garder le scan large si besoin mais mettre par exemple `POLYMARKET_SMART_MAX_HOURS_TO_CLOSE=24`. Les entrees autonomes exigent un consensus smart-money (`POLYMARKET_SMART_MIN_CONSENSUS=2` par defaut) sur des BUY recents de wallets profitables. S'il n'y a pas ce consensus, le bot skip au lieu de forcer un trade. Le bot refuse aussi les marches trop proches de l'expiration (`POLYMARKET_SMART_MIN_HOURS_TO_CLOSE=0.25`), les marches trop lointains pour le cap configure, et applique des regles plus strictes aux micro-marches crypto up/down.

Chaque opportunite inclut `selection_reason` et `selection_metrics`, qui expliquent pourquoi le bot l'a choisie: consensus wallets, taille copiee, PnL total des wallets, prix moyen copie, ask actuel, spread, temps avant cloture, score, et checks passes. Le bot score les marches courts plus haut quand la qualite smart-money est comparable. Le bot essaie toutes les opportunites qualifiees d'un tick, avec `POLYMARKET_SMART_MAX_TRADE_USD=5` par defaut, jusqu'a manquer de fonds, atteindre `POLYMARKET_SMART_MAX_ORDERS_PER_TICK` si configure, ou epuiser les signaux. Si les caps env sont plus hauts, le sizing augmente seulement avec la qualite du signal: consensus 2 reste petit, consensus 3+ peut grossir, consensus 4+ avec gros flow peut atteindre le cap. Les BUY live utilisent des market orders FOK: si le carnet ne peut pas remplir immediatement dans le prix garde, le signal est skip et le bot passe a l'opportunite suivante.

Avant de chercher de nouvelles entrees, chaque tick synchronise les positions live via la Data API (`POLYMARKET_SYNC_LIVE_POSITIONS=1`) puis applique une strategie de sortie sur les positions live ouvertes. Par defaut `POLYMARKET_SMART_TAKE_PROFIT_TIERS=1.0:0.50,2.0:0.25,3.0:0.15`: a +100% le bot vend 50% des shares, a +200% il vend 25% des shares initiales, a +300% il vend 15%, puis garde le reste. Si une position a deja atteint +100% (`POLYMARKET_SMART_PEAK_PROTECT_TRIGGER=1.0`) et retombe sous +40% (`POLYMARKET_SMART_PEAK_PROTECT_FLOOR=0.40`), le bot essaie de vendre le solde restant. Si une position profitable approche de l'expiration (`POLYMARKET_SMART_EXIT_MINUTES_TO_CLOSE=20`, `POLYMARKET_SMART_EXIT_MIN_PROFIT=0.05`), le bot essaie aussi de sortir. Les ventes utilisent le bid executable et sont journalisees dans la position (`exits`, `realized_pnl`, shares restantes).

`btc-edge-once` trade seulement les marches BTC above/below parsables quand le modele Coinbase spot/volatilite trouve assez d'edge. Il ignore les marches generiques.

`btc-edge-loop` lance la strategie BTC edge toutes les `POLYMARKET_AUTO_INTERVAL_SECONDS` secondes. Mets `POLYMARKET_AUTO_MAX_TICKS=0` pour une boucle illimitee.

## Identifiants API

Le placement d'ordres live CLOB a besoin du set CLOB en trois parties :

```bash
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
```

Une cle relayer est differente :

```bash
RELAYER_API_KEY=...
RELAYER_API_KEY_ADDRESS=0x...
```

Les identifiants relayer seuls ne suffisent pas pour le chemin actuel de placement d'ordres CLOB. Si seulement des identifiants relayer sont configures, `auto-loop` scannera les marches mais refusera de placer un ordre live avec une erreur locale claire, au lieu de reessayer le bootstrap `/auth/api-key` bloque par Cloudflare.

## Dashboard

Demarre le dashboard temps reel :

```bash
python3 -B -m polymarket_bot.main dashboard
```

Ouvre `http://127.0.0.1:8765`. Il se rafraichit toutes les 5 secondes et montre le mode du bot, l'equity, les positions ouvertes, les trades recents du bot, les order IDs si disponibles, et les candidats du scanner.

## Docs Agent

Ce repo contient des fichiers markdown compatibles avec les agents :

- `AGENTS.md` pour les agents type Codex.
- `CLAUDE.md` pour Claude Code.
- `docs/AUTONOMOUS_STRATEGY.md` pour les regles de trading et le comportement du dashboard.

## Notes

Le score du scanner est base sur l'urgence, la liquidite, le volume et la tradabilite. Ce n'est pas un modele d'expected value.
Le bot utilise le flow d'authentification wallet documente par Polymarket. Un login Safari seul ne suffit pas pour trader.
