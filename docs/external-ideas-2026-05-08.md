# Idées externes — recherche écosystème OSS Polymarket — 2026-05-08

Synthèse d'une exploration de l'écosystème open-source des bots Polymarket et prediction-market
copy-trading. Complément à `docs/audit-2026-05-08.md` (qui ne couvre que l'audit interne du code).

**Méthodologie** : `gh search repos polymarket` + filtrage des spams SEO (~80% des résultats),
lecture des READMEs substantiels, focus sur projets > 5⭐ ou commits récents (< 6 mois).

## Verdict de positionnement

**Le bot local est plausiblement #1 ou #2** dans le segment « copy-trading Polymarket OSS qu'on
lancerait avec du vrai argent ». Sur ~200+ « polymarket trading bot » sur GitHub, ~80% sont des
spams SEO. Sur les ~20 sérieux :

**En avance** :

- **Déterminisme** — aucun concurrent ne garde le LLM hors du scan path. Tous les autres
  (`whale-watcher`, `polymarket-pipeline`, `polyclaw`, `Polymarket/agents`) couplent la sélection
  de markets au jugement LLM → lent, coûteux, biais caché.
- **Test coverage** — 52 tests est rare. La majorité des concurrents en a zéro.
- **Exit machinery** — la combinaison TP ladder + peak-protect + trailing + cohort-sell + max-hold
  est plus sophistiquée que tout ce qui est visible chez la concurrence.
- **Auto-tuner défensif borné** — l'asymétrie « tighten after losses, never loosen after wins » est
  correcte et absente des concurrents self-learning (ex : `aulekator`).
- **Conviction-weighted sizing tiers** — explicite, audité, borné. La plupart des copy bots font du
  flat sizing ou du Kelly naïf.

**À parité** : exécution live (py-clob-client + signatures), dashboard, BTC threshold, paper-mode,
journal/stats — tout standard.

**En retard** :

- Pas de backtester (`evan-kolberg/prediction-market-backtesting` est largement devant).
- Pas d'ingestion de données historiques (ne consomme aucun des datasets parquet OSS dispos).
- Pas de lead/follow analysis sur la cohort.
- Pas de burst suppression sur les DCA same-wallet.
- Polling, pas WebSocket — cohort-sell en retard de plusieurs minutes vs concurrents WS.
- Pas de tiered scan par bucket de volume.
- Pas de hedge discovery (`polyclaw` a un système contrapositive unique).
- Pas de Brier calibration sur les tiers de conviction.

## Concurrents directs

| Repo | Stars | Lang | Stratégie | Différenciation |
|---|---|---|---|---|
| [chaoleiyv/polymarket-whale-watcher](https://github.com/chaoleiyv/polymarket-whale-watcher) | 205 | Py | Tiered monitoring 700+ markets, détection trades > $10k, agent LLM 14 outils → "Information Asymmetry Score" | **Concurrent #1** — closest thing to "smart-money detection done right". Plus de research, mais LLM dans le scan = lent + coûteux. |
| [warproxxx/poly-maker](https://github.com/warproxxx/poly-maker) | 1.1k | Py | Market-making keeper (WS order book, config Google Sheets) | Production-grade par un trader PM réel. Inclut `poly_merger` (NO+YES → cash). |
| [askwhyharsh/lazytrader](https://github.com/askwhyharsh/lazytrader) | 3 | Go | Pooled-capital copy-trading service basé leaderboard | Modèle fund-pool (productisé). |
| [freq-trades/polymarket-trading-bot-v2](https://github.com/freq-trades/polymarket-trading-bot-v2) | 176 | JS | 5-min BTC arb + module copy-trading sous `src/copy/` | Un des rares bots JS légitimes. |
| [al1enjesus/polymarket-whales](https://github.com/al1enjesus/polymarket-whales) | 45 | Py | Polls CLOB, alerte terminal/Telegram trades > seuil | Lightweight, no API key. Public Telegram channel. |

## Outils complémentaires

À brancher sur le bot ou nourrir l'analyse.

| Repo | Stars | Usage |
|---|---|---|
| [evan-kolberg/prediction-market-backtesting](https://github.com/evan-kolberg/prediction-market-backtesting) | 809 | **Backtester NautilusTrader** avec L2 book replay, charts equity/DD/Sharpe/Brier, optimizer Optuna TPE. À utiliser pour valider la stratégie ex-ante. |
| [warproxxx/poly_data](https://github.com/warproxxx/poly_data) | 1.8k | **Dataset 1.1B trades** Polymarket (Goldsky subgraph + markets). Snapshot téléchargeable. Carburant pour le backtester. |
| [Jon-Becker/prediction-market-analysis](https://github.com/Jon-Becker/prediction-market-analysis) | 3.3k | 36 GiB Polymarket+Kalshi parquet curé, indexers, scripts d'analyse. |
| [SII-WANGZJ/Polymarket_data](https://github.com/SII-WANGZJ/Polymarket_data) | 555 | 1.1B records Polymarket, formats analysis-ready. |
| [pmxt-dev/pmxt](https://github.com/pmxt-dev/pmxt) | 1.7k | "CCXT for prediction markets" — abstraction unifiée Polymarket/Kalshi/etc. À intégrer si on veut ajouter Kalshi. |
| [ferumlabs/polymarket-leaderboard-scraper](https://github.com/ferumlabs/polymarket-leaderboard-scraper) | 0 | Scrape exhaustif leaderboard + résolution handle X/Twitter. Utile pour qualif manuelle de cohort. |
| [darrnhard/polymarket-smart-money](https://github.com/darrnhard/polymarket-smart-money) | 1 | **Notebook recherche** — analyse comportementale de 3 top wallets sur 6.8M events on-chain. Source des findings lead/follow et burst pattern. |
| [aarora4/Awesome-Prediction-Market-Tools](https://github.com/aarora4/Awesome-Prediction-Market-Tools) | 370 | Directory curé. |
| [Polymarket/agents](https://github.com/Polymarket/agents) | 3.4k | Framework LLM-RAG officiel. Référence si on veut un module recherche LLM séparé du scan. |
| [Polymarket/poly-market-maker](https://github.com/Polymarket/poly-market-maker) | 294 | Keeper market-making officiel (Bands/AMM). |

## Top 3 idées à piquer (ROI estimé élevé)

### EXT1 — Burst suppression

**Source** : [darrnhard/polymarket-smart-money](https://github.com/darrnhard/polymarket-smart-money)

**Finding** : un wallet smart-money fait souvent **5-7 trades sur le même token en 60-90s** (DCA).

**Problème dans le bot local** : chaque fill est traité comme un signal frais → **inflation
artificielle du consensus** (`MIN_CONSENSUS=2` peut être atteint par 1 seul wallet qui fait 2 fills
en 30s).

**Fix** : dans `smart_money.py`, collapse les trades `same-wallet + same-token` dans une fenêtre de
90s en un seul événement de signal (somme des sizes, prix VWAP).

**Effort** : ~2-3h. **Impact** : élimine probablement ~10-20% des entries actuelles qui sont en
fait du faux consensus.

### EXT2 — Lead/follow detection

**Source** : [darrnhard/polymarket-smart-money](https://github.com/darrnhard/polymarket-smart-money)

**Finding** : analyse pairwise sur 6.8M events montre que `sovereign` lead `rn1`/`swisstony` de
**160-400 min sur 68-82% des markets partagés**.

**Problème dans le bot local** : tous les wallets de la cohort sont pondérés également (par leur
PnL passé). Le bot **sous-pondère la source d'alpha et sur-pondère les copies de copies**.

**Fix** : ajouter une métrique `mean_lead_time_minutes` par wallet (calculée hors-ligne sur
historique des trades). Pondérer le sizing du signal par cette métrique. Idéalement : ne mirror
que les leads, ignorer les followers.

**Effort** : ~1 jour (calcul hors-ligne + intégration scoring). **Impact** : pourrait être le plus
gros levier PnL identifié à date — on copierait moins de signal mais avec plus d'edge.

### EXT3 — Backtester sur `poly_data`

**Source** : [evan-kolberg/prediction-market-backtesting](https://github.com/evan-kolberg/prediction-market-backtesting)
+ [warproxxx/poly_data](https://github.com/warproxxx/poly_data)

**Problème dans le bot local** : sans backtester, l'auto-tuner défensif tourne aveugle pendant les
30+ premiers trades live. Tout changement de stratégie est testé en live, donc en risque réel.

**Fix** : brancher le scanner+stratégie locale sur le harness NautilusTrader d'evan-kolberg, alimenté
par le dataset 1.1B trades de warproxxx. Sortir Sharpe / max DD / Brier ex-ante avant tout
déploiement.

**Effort** : ~2-3 jours (intégration NautilusTrader, mapping data schema). **Impact** : transforme
chaque modification de stratégie d'un pari à un test mesurable. Convergence avec MC5 dans audit
interne.

## Idées moyennes

| ID | Idée | Source | Détail |
|---|---|---|---|
| EXT4 | **Tiered market scan** | [chaoleiyv whale-watcher](https://github.com/chaoleiyv/polymarket-whale-watcher) | Bucket markets en `>500k / >10k / >1k` volume, appliquer floors `MIN_COPIED_USDC` différenciés. Aujourd'hui `$75` uniforme = trop strict sur deep, trop lâche sur thin. |
| EXT5 | **WebSocket order book** | [warproxxx/poly-maker](https://github.com/warproxxx/poly-maker), [freq-trades v2](https://github.com/freq-trades/polymarket-trading-bot-v2) | Souscription WS sur les markets de la cohort active → cohort-sell sub-seconde vs lookback 120 min actuel. |
| EXT6 | **Brier score par tier de conviction** | [evan-kolberg](https://github.com/evan-kolberg/prediction-market-backtesting), [suislanchez weather](https://github.com/suislanchez/polymarket-kalshi-weather-bot) | Vérifier empiriquement si le tier `5-wallet $5k+ → 2.5×` est réellement 2.5× meilleur ou juste plus gros. Aujourd'hui : non vérifié. |
| EXT7 | **Position merge** | [poly-maker poly_merger](https://github.com/warproxxx/poly-maker) | Quand on détient YES + NO sur même condition, merger en USDC immédiatement plutôt que d'attendre résolution. Libère capital. |
| EXT8 | **Hedge discovery** | [polyclaw](https://github.com/chainstacklabs/polyclaw) | Détection « covering portfolio » Tier 1 (≥95%). Si long YES sur "Trump primaire NH" et "Trump nomination GOP" drop sous cover-price → hedge auto. |
| EXT9 | **Fractional Kelly explicite** | [suislanchez weather](https://github.com/suislanchez/polymarket-kalshi-weather-bot), [brodyautomates](https://github.com/brodyautomates/polymarket-pipeline) | Standard industrie en prediction markets : Kelly fractionnaire 15-25%. Les conviction-tier multipliers actuels sont une approximation implicite ; rendre explicite donne un cap principled. |
| EXT10 | **Information Asymmetry Score (proxy non-LLM)** | [chaoleiyv whale-watcher](https://github.com/chaoleiyv/polymarket-whale-watcher) | Inspiré du LLM-based, mais déterministe : `(depth × news_recency × wallet_hit_rate_on_tag) → 0-1` gate sur sizing. |
| EXT11 | **Telegram alerts** | [al1enjesus/polymarket-whales](https://github.com/al1enjesus/polymarket-whales) | Push sur cohort-sell exits / stop-loss. Cosmétique mais utile pour monitoring sans dashboard ouvert. |
| EXT12 | **Twitter handle enrichment** | [ferumlabs scraper](https://github.com/ferumlabs/polymarket-leaderboard-scraper) | Tag wallets avec handle social pour revue manuelle de cohort. |

## Convergences avec l'audit interne

Idées identifiées indépendamment dans `audit-2026-05-08.md` (interne) ET dans cette analyse externe :

| Externe | Interne | Convergence |
|---|---|---|
| EXT3 (Backtester) | MC5 (Walk-forward replay) | Les deux soulignent l'absence comme blocker majeur |
| EXT2 (Lead/follow) | MC1 (Forward-validation wallets) | Approches différentes du même problème : valider les wallets ex-ante |
| EXT4 (Tiered scan) | — | Nouveau via externe |
| EXT6 (Brier par tier) | — | Nouveau via externe |
| EXT5 (WebSocket) | — | Nouveau via externe |

## Findings nouveaux (n'étaient pas dans l'audit interne)

- **EXT1 (burst suppression)** — révélation comportementale via [darrnhard](https://github.com/darrnhard/polymarket-smart-money), n'aurait pas été trouvée par lecture de code seul.
- **EXT2 (lead/follow)** — même source, finding empirique sur historique on-chain.
- **EXT7 (position merge)** — feature du SDK Polymarket peu connue, vue dans [poly-maker](https://github.com/warproxxx/poly-maker).
- **EXT8 (hedge discovery)** — pattern unique de [polyclaw](https://github.com/chainstacklabs/polyclaw).

## Ce que ça veut dire pour la roadmap

Les **3 actions à plus haut levier**, en combinant les deux audits :

1. **Backtester** (EXT3 / MC5) — débloque toute mesure ex-ante. Pré-requis pour tout le reste.
2. **Lead/follow + burst suppression** (EXT2 + EXT1) — refonte du scoring de cohort, probablement
   le plus gros gain PnL si on arrive à pondérer correctement.
3. **Quick wins de l'audit interne** (QW1-QW10) — déjà documentés dans `audit-2026-05-08.md`,
   indépendants du backtester, exécutables tout de suite.
