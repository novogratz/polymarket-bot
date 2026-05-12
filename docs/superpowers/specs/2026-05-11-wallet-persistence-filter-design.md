# Filtre de persistance d'edge des wallets smart-money

**Date :** 2026-05-11
**Statut :** design approuvé, en attente de plan d'implémentation
**Branche :** `worktree-wallet-persistence-filter`

## Problème

La cohorte smart-money actuelle est construite via un snapshot unique du leaderboard Polymarket sur la période `MONTH` (top 100), pré-filtré par PnL/Volume/ROI minima. Cette approche ne distingue pas les **traders à edge persistant** des **wallets temporairement chanceux**.

Plusieurs sources convergentes (laikalabs.ai, PolyTrack, polycopytrade.space, Medium @0xmega) confirment qu'une part significative des « top traders mensuels » sont du bruit statistique — variance forte sur 30 jours, edge non persistant. Le cas Théo (French Whale Trump 2024) est l'exception médiatisée, pas la norme. Aucune étude académique formelle ne mesure cette persistance sur Polymarket.

Conséquence sur le bot : nous copions des wallets qui n'ont pas démontré de skill stable, augmentant le PnL espéré attendu négatif sur la lane smart-money.

## Objectif

Ajouter un **filtre de persistance** branché entre le pré-filtre existant (PnL/Vol/ROI) et le fetch des trades. Un wallet n'est conservé dans la cohorte que s'il satisfait **au moins l'un** des deux critères suivants :

1. **Intersection multi-période** : présent dans ≥ 2 des 3 leaderboards Polymarket (WEEK, MONTH, ALL_TIME) au tick courant.
2. **Persistance par cache** : présent dans ≥ 70 % des 30 derniers snapshots quotidiens stockés localement.

Le critère 2 demande un warmup (≥ 1 snapshot quotidien dans le cache). Pendant le warmup, seul le critère 1 s'applique.

## Non-objectifs

- Pas de remplacement des seuils PnL/Vol/ROI existants : le nouveau filtre s'ajoute en série, après eux.
- Pas de modification du scoring de signal ou du sizing : le filtre est binaire (pass/fail) au niveau de la cohorte, et propage un score informatif au journal (sans effet sur la décision).
- Pas de support multi-période sur le cache (uniquement MONTH stocké) — extension future possible.
- Pas de commande `wallet-stats` (skipped lors du brainstorming, ajout possible plus tard si besoin de debug).

## Architecture

```
TICK START (smart_money.fetch_smart_money_data)

  1. Pull leaderboard sur 3 périodes (WEEK, MONTH, ALL)
     → 3 listes de SmartTrader avec dédup wallet

  2. Pré-filtre PnL/Volume/ROI (existant, inchangé)
     → traders_qualified sur base MONTH

  3. [NEW] WalletPersistenceFilter
     ├─ store.record_snapshot(today, wallets_month)  (idempotent par date)
     ├─ pour chaque trader qualifié :
     │   ├─ intersect_count = nombre de listes (W,M,A) contenant le wallet (0..3)
     │   ├─ intersect_score = intersect_count / 3  (float, informatif)
     │   ├─ si snapshot_count >= window_days / 2 :  # cache utilisable
     │   │     cache_score = presence_days / window_days
     │   │   sinon (warmup) :
     │   │     cache_score = 0.0
     │   ├─ qualified = (intersect_count >= intersect_min) OR (cache_score >= cache_threshold)
     │   └─ persistence_score = max(intersect_score, cache_score)
     └─ retourne (cohort filtrée, signals: dict[wallet→PersistenceSignal])

  4. fetch trades pour cohort filtrée (existant)
  5. analyze_smart_money_with_data (existant)
  6. choose / place trade (existant)
     → persistence_score écrit dans trade_journal au BUY et préservé au close
```

Le filtre est **un module additionnel** branché entre étapes 2 et 4 — le code existant en aval ne voit qu'une cohorte plus petite.

## Composants

### `polymarket_bot/wallet_persistence.py` (nouveau)

```python
@dataclass(frozen=True)
class PersistenceSignal:
    wallet: str
    intersect_score: float    # 0.0, 0.33, 0.67, 1.0
    cache_score: float        # 0.0–1.0
    persistence_score: float  # max(intersect, cache)
    qualified: bool

class WalletHistoryStore:
    """Persistance JSON append-only, idempotent par date.

    - record_snapshot(date, wallets): no-op si date déjà présente
    - presence_count(wallet, window_days): nombre de jours dans la fenêtre
    - snapshot_count(): nombre total de snapshots stockés
    - prune au-delà de 2× window_days pour borner la taille
    """
    def __init__(self, path: Path, window_days: int = 30): ...
    def record_snapshot(self, date: date, wallets: list[str]) -> bool: ...
    def presence_count(self, wallet: str, window_days: int) -> int: ...
    def snapshot_count(self) -> int: ...

def compute_persistence(
    wallet: str,
    *,
    in_week: bool, in_month: bool, in_all: bool,
    cache_presence_days: int,
    window_days: int,
    cache_threshold: float = 0.70,
    intersect_threshold: int = 2,
) -> PersistenceSignal: ...

def filter_cohort_by_persistence(
    qualified_traders: list[SmartTrader],
    *,
    leaderboards: dict[str, set[str]],
    store: WalletHistoryStore,
    settings: Settings,
) -> tuple[list[SmartTrader], dict[str, PersistenceSignal]]: ...
```

Pure function `compute_persistence` (zéro I/O) + I/O isolé dans `WalletHistoryStore`. Testabilité maximale.

### Modifications dans `smart_money.py`

- `_top_traders()` retourne un `dict[period, list[SmartTrader]]` (mapping par période). La déduplication précédente est déplacée en aval pour préserver l'info de période.
- `fetch_smart_money_data()` :
  - applique le pré-filtre PnL/Vol/ROI comme avant
  - si `settings.persistence_enabled` : appelle `filter_cohort_by_persistence` après le pré-filtre, avant le fetch des trades
  - peuple `SmartMoneyData.persistence_signals: dict[str, PersistenceSignal]`
- Bypass si `settings.persistence_enabled == False` : aucun changement de comportement.

### Modifications dans `main.py`

- Au moment du BUY, récupérer le `persistence_score` max parmi les wallets entry du signal et l'écrire dans l'entrée du `trade_journal` comme champ `persistence_score`.
- À la clôture (close), préserver le champ (déjà stocké à l'entrée, just propagé).
- Affichage tick output (mode non-quiet) : ligne du type
  ```
     cohort: 100 → 80 (PnL/Vol/ROI) → 45 (persistence: 30 cache, 28 intersect, 13 both)
  ```

### CLI

- `auto-loop --no-persistence` : flag qui force `persistence_enabled=False` pour le run courant (pour A/B testing).

### Config (`config.py`)

Nouveaux champs `Settings`, tous lus depuis env vars :

| Champ | Env var | Default |
|---|---|---|
| `persistence_enabled` | `POLYMARKET_PERSISTENCE_ENABLED` | `True` |
| `persistence_cache_path` | `POLYMARKET_PERSISTENCE_CACHE_PATH` | `data/wallet_history.json` |
| `persistence_window_days` | `POLYMARKET_PERSISTENCE_WINDOW_DAYS` | `30` |
| `persistence_cache_threshold` | `POLYMARKET_PERSISTENCE_CACHE_THRESHOLD` | `0.70` |
| `persistence_intersect_periods` | `POLYMARKET_PERSISTENCE_INTERSECT_PERIODS` | `"WEEK,MONTH,ALL"` |
| `persistence_intersect_min` | `POLYMARKET_PERSISTENCE_INTERSECT_MIN` | `2` |

Note : `cache_path` n'est pas tunable par profil TOML (env-var seulement), pour garder le schéma TOML focalisé sur les tunables stratégiques.

### Profils TOML

Ajout au `_SCHEMA` de `profiles.py` d'une section `persistence` :

```python
"persistence": {
    "enabled": ("POLYMARKET_PERSISTENCE_ENABLED", "bool"),
    "window_days": ("POLYMARKET_PERSISTENCE_WINDOW_DAYS", "int"),
    "cache_threshold": ("POLYMARKET_PERSISTENCE_CACHE_THRESHOLD", "float"),
    "intersect_periods": ("POLYMARKET_PERSISTENCE_INTERSECT_PERIODS", "str"),
    "intersect_min": ("POLYMARKET_PERSISTENCE_INTERSECT_MIN", "int"),
},
```

Application aux profils existants :

| Profil | `enabled` | `intersect_min` | Justification |
|---|---|---|---|
| `baseline.toml` | `false` | — | Référence non-filtrée pour A/B test |
| `aggressive.toml` | `true` | `2` | Filtre actif, params recommandés |
| `aggressive-live.toml` | `true` | `2` | Cohérent avec live |
| `live-90.toml` | `true` | `2` | Cohérent avec live |
| `tight-filters.toml` | `true` | `3` | 3/3 strict, cohérent avec l'esprit du profil |

## Format de stockage

Fichier `data/wallet_history.json` :

```json
{
  "version": 1,
  "snapshots": [
    {"date": "2026-05-11", "wallets": ["0xabc...", "0xdef..."]},
    {"date": "2026-05-12", "wallets": ["0xabc...", "0x123..."]}
  ]
}
```

**Choix :**
- Format liste plate par date (simple à diff/inspecter, vs mapping wallet→dates).
- **Append-only** : ancienne donnée jamais réécrite, purge automatique au-delà de 60 jours (2× window_days).
- **Présence seule** (pas de PnL/Vol/ROI stocké) : économie ~95 % de taille.
- Écriture atomique : `tempfile.NamedTemporaryFile` dans le même dossier puis `os.replace`.
- Fichier préservé par défaut (comme `trade_journal.jsonl`) — jamais reset sauf demande explicite.

**Concurrence dry-run / live :** chaque mode a son propre fichier (`data/wallet_history.json` vs `data/dry_run_wallet_history.json`), cohérent avec la séparation existante des ledgers.

## Observabilité

Trois mécanismes de mesure d'impact :

1. **Journal des trades** : champ `persistence_score` dans chaque entrée du `trade_journal.jsonl`, propagé du BUY au close. Permet d'analyser a posteriori la corrélation persistance ↔ PnL.

2. **Tick output (non-quiet)** : ligne `cohort: A → B → C` qui montre l'effet immédiat du filtre. Affiche aussi la répartition cache/intersect/both pour calibration.

3. **Flag `--no-persistence`** : permet de lancer en parallèle deux runs nommés (`smoke` avec filtre, `smoke-nopers` sans) pour comparaison directe via `pmbot dry-run compare`. Mécanisme rigoureux de validation.

Pas de commande `pmbot wallet-stats` à cette étape (debug ponctuel reportable).

## Tests

Nouveau fichier `tests/test_wallet_persistence.py` :

### Unitaires — `compute_persistence`
- intersect 3/3 → qualified, intersect_score = 1.0
- intersect 2/3 → qualified, intersect_score ≈ 0.67
- intersect 1/3 + cache 0% → not qualified
- intersect 0/3 + cache 80% → qualified via cache path
- bordure : cache_score == 0.70 → qualified
- bordure : cache_score == 0.69 → not qualified
- bordure : intersect_min=3 et intersect_count=2 → not qualified
- warmup actif (snapshot_count < window_days/2) → cache_score forcé à 0, qualification uniquement par intersection
- `persistence_score == max(intersect_score, cache_score)`

### Unitaires — `WalletHistoryStore`
- `record_snapshot` idempotent : 2 appels même date → 1 entrée
- `presence_count` correct sur fenêtre glissante (5 wallets sur 10 jours, fenêtre 30 jours)
- purge automatique des snapshots au-delà de 2 × window_days
- atomicité write : crash mid-write ne corrompt pas le fichier (mock `os.replace`)
- format JSON v1 stable + tolérance d'une version inconnue (warning, pas crash)

### Intégration — `filter_cohort_by_persistence`
- pipeline complet : 100 traders → 80 (pré-filtre) → X (persistance) avec X cohérent
- bypass `persistence_enabled=False` → cohorte inchangée, signals vide
- store vide (warmup) → tombe sur intersection seule, cache_score=0 partout

### Journal
- trade BUY contient `persistence_score`
- trade SELL propage le `persistence_score` du BUY parent

Cible : ≥ 20 tests sur les nouveaux composants. Tous les 170 tests existants restent verts.

## Migration et compatibilité

- Pas de breaking change. Le filtre est activé par défaut (`POLYMARKET_PERSISTENCE_ENABLED=True`) mais en l'absence du fichier `data/wallet_history.json`, le cache_score sera 0 partout — le filtre tombe naturellement sur le critère 1 (intersection).
- Le premier `record_snapshot` se déclenche au premier tick et crée le fichier automatiquement.
- Pas de migration de données : le cache se construit organiquement au fil des ticks.
- Trade journal : les anciennes entrées sans `persistence_score` restent lisibles (champ optionnel à la lecture).

## Risques et mitigations

| Risque | Mitigation |
|---|---|
| Filtre trop strict → cohorte vide → 0 trades | Critère OR (cache OU intersection) au lieu de AND ; bypass via `--no-persistence` pour rollback rapide |
| Cache corrompu | Écriture atomique + parse défensif (warning, pas crash) ; rebuild progressif au fil des snapshots |
| API leaderboard down sur une période | Le code existant log déjà ces erreurs ; si toutes les périodes échouent, fallback comportement actuel (filtre désactivé pour ce tick) |
| Sur-fitting sur 30 jours de bruit | Validation rigoureuse via A/B test obligatoire avant déploiement live |
| Quota d'appels API Polymarket | +2 leaderboard calls/tick (WEEK + ALL en plus de MONTH). Coût négligeable, déjà supporté par l'archi multi-period existante |

## Validation

Avant déploiement live, **2 semaines de comparaison dry-run obligatoire** :

```bash
# Reset
uv run pmbot dry-run rm smoke --yes
uv run pmbot dry-run rm smoke-nopers --yes

# Run baseline (persistence ON via profile)
uv run pmbot auto-loop --dry-run --run smoke --profile aggressive

# Run contrôle (persistence OFF via flag)
uv run pmbot auto-loop --dry-run --run smoke-nopers --no-persistence --profile aggressive

# Comparaison après J+14
uv run pmbot dry-run compare smoke smoke-nopers
```

Critère go/no-go : différence de PnL net sur 14 jours ≥ +3 % en faveur de la variante avec filtre. Sinon, garder le filtre désactivé par défaut et revoir les paramètres.
