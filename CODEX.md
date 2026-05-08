# Guide Codex

Fichier d'entrée Codex pour le bot Polymarket. Voir `CLAUDE.md` pour la version Claude Code (contenu équivalent).

## Sécurité

- Ne jamais révéler les valeurs de `.env`, clés privées, secrets API ou passphrases.
- Ne pas contourner `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- Ne pas implémenter de trades live aléatoires ou non filtrés. Le chemin `noise_fallback` est la seule voie de trade forcé et est plafonné à $5 par trade et 2 trades par tick.
- Préserver `data/paper_state.json`, `data/trade_journal.jsonl` et `data/strategy_overrides.json` sauf si l'utilisateur demande un reset explicite.
- Aucun appel LLM (Codex, Claude, autre) dans le chemin de scan ou de sélection de trade.
- Le bot n'a pas la capacité d'écrire ni de pusher du code source de lui-même.

## Carte du projet

- `polymarket_bot/main.py` : commandes CLI et boucles. Orchestration du tick, sizing, journal de trades, commandes `journal-stats` et `tune-strategy`.
- `polymarket_bot/smart_money.py` : leaderboards, fetch parallèle des trades, regroupement par token, scoring, reverse-lookup.
- `polymarket_bot/auto_tuner.py` : overrides bornés depuis le trade journal (défensif).
- `polymarket_bot/bitcoin.py` : modèle BTC threshold edge (Black-Scholes via volatilité).
- `polymarket_bot/trading.py` : ordres BUY/SELL live et calcul du stake.
- `polymarket_bot/dashboard.py` : dashboard local sur `http://127.0.0.1:8765`.
- `polymarket_bot/portfolio.py` : ledger local, positions, ordres pending, exits.
- `polymarket_bot/gamma.py` : client Gamma + reverse-lookup par clob_token_ids.
- `polymarket_bot/strategy.py` : ranking des candidats.
- `scripts/run_live_70.sh` : runner live canonique.
- `tests/test_strategy.py` : 49 tests.

## Commandes

Tests :

```bash
python3 -B -m unittest discover -s tests
```

Dashboard :

```bash
python3 -B -m polymarket_bot.main dashboard
```

Stats du journal :

```bash
python3 -B -m polymarket_bot.main journal-stats
```

Auto-tuner manuel :

```bash
python3 -B -m polymarket_bot.main tune-strategy
```

Boucle smart-money autonome :

```bash
POLYMARKET_ENABLE_LIVE_TRADING=1 python3 -B -m polymarket_bot.main auto-loop
```

## Commande live recommandée

```bash
bash scripts/run_live_70.sh
```

Voir `CLAUDE.md` pour la liste complète des paramètres et la séquence d'un tick.

## Stratégie pour gagner de l'argent

Copy-trading smart-money. Le bot attend que des wallets profitables (top leaderboard mensuel, PnL ≥$1k, volume ≥$2k, ROI ≥3%) achètent le même token dans une fenêtre courte (30 min), puis miroite ce flow.

### L'edge

Les wallets en haut des leaderboards mensuels avec PnL et volume significatifs ont en moyenne un edge informationnel sur les marchés qu'ils tradent. Quand plusieurs achètent le même token simultanément, le signal collectif est plus fort qu'un wallet isolé. Le bot copie.

### Conditions d'entrée

- Trades BUY récents de wallets qualifiés (PnL/volume/ROI/recency).
- Consensus multi-wallets sur le même token.
- Assez d'USDC copié.
- Marché tradable : spread absolu et relatif serrés, ask dans la bande de prix, pas trop proche de l'expiration.
- Pas de duplicate market ni event-level (sports).
- Sizing pondéré par conviction (0.55x à 2.5x la base).

### Sorties

- Take-profit ladder +100%/+200%/+300% partiel.
- Trailing stop armé à +25%, giveback 50%.
- Peak-protect armé à +100%, sortie sous +40%.
- Stop-loss -40% après 15 min en position.
- Cohort-sell exit (active SELL detection 120 min lookback) ou cohort-silent (pas de fresh BUY).
- Exit positif près de l'expiration.

### Auto-tuner

Lit le trade journal à chaque tick. Pause sous 30 trades clôturés. Resserre les filtres et le sizing après les patterns perdants. Défensif uniquement.

### Pas un profit garanti

L'edge vient de copier le flow public fort en évitant les mauvais fills. No-signal / no-trade est une position valide.
