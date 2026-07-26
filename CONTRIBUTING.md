# Contribuer

Comment lancer, tester et naviguer dans le projet. *Pourquoi* il est construit
ainsi est dans [README_TAKE_HOME.md](README_TAKE_HOME.md) et les documents qu'il
indexe.

## Prérequis

- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Docker avec Compose v2, pour le parcours conteneurisé

Vous n'avez **pas** besoin d'un Python système. Le projet vise **Python 3.14 au
minimum** (`requires-python = ">=3.14"`) et l'épingle dans `.python-version` ; uv
télécharge cet interpréteur au premier usage.

## Le lancer

### Avec Docker (au plus près de la production)

```bash
docker compose up --build
```

- API : http://localhost:8000 · Swagger : http://localhost:8000/docs · Santé :
  http://localhost:8000/health

L'ordre de démarrage est `db` → `migrate` → `seed` → `api`, chacun conditionné à
la fin du précédent, donc l'API ne démarre jamais contre une base non migrée.
Compose déclare un healthcheck : `docker compose ps` affiche `healthy` dès que
l'application répond. Arrêt avec `docker compose down` (ajouter `-v` pour
supprimer aussi le volume).

### Se connecter

Le seed crée deux organisations avec un utilisateur chacune :

| Organisation | Email | Mot de passe |
|---|---|---|
| Acme Corp | `alice@acme.example.com` | `password123` |
| Globex | `bob@globex.example.com` | `password123` |

`POST /auth/login` renvoie un token d'accès ; collez-le dans le bouton
**Authorize** de Swagger pour exercer les endpoints authentifiés.

### En local, avec rechargement à chaud

```bash
uv sync                                          # crée .venv depuis uv.lock
docker compose up -d db                          # Postgres seul
uv run alembic upgrade head                      # schéma, rôles, politiques RLS
uv run python -m app.seed                        # deux orgs, deux utilisateurs
uv run uvicorn app.main:app --reload             # http://127.0.0.1:8000
```

Le service `db` de compose ne publie pas de port : pour une exécution côté hôte,
soit vous en ajoutez un, soit vous pointez `DATABASE_URL` / `AUTH_DATABASE_URL` /
`MIGRATION_DATABASE_URL` vers votre propre Postgres, et `STORAGE_ROOT` vers un
répertoire accessible en écriture (`STORAGE_ROOT=./var/uploads`). `uv sync`
installe aussi le groupe dev, et `uv run <cmd>` s'exécute dans l'environnement du
projet — il n'y a aucun `activate` à retenir.

## Tests et lint

```bash
uv run pytest                # tests unitaires + API, aucune base requise
uv run ruff check .          # lint
uv run ruff format .         # formatage
```

Les tests unitaires et d'API utilisent des fakes en mémoire : l'exécution par
défaut ne demande aucun service. Les tests d'intégration exercent la row-level
security et le trigger `NOTIFY` contre un vrai Postgres et sont **ignorés** tant
qu'on ne les pointe pas vers un :

```bash
docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=postgres \
    -e POSTGRES_DB=appdb --name pg-test postgres:17-alpine
TEST_POSTGRES_DSN='postgres:postgres@localhost:5433/appdb' uv run pytest
```

La fixture supprime et recrée le schéma `public`, puis exécute la vraie migration
Alembic — ce sont donc les politiques et les rôles qui sont testés, pas une
réimplémentation. À ne pointer que vers une base jetable.

**`app/pipeline/steps.py` est reproduit à l'octet près depuis `README.md` et ne
doit pas être modifié** — pas même reformaté. `tests/unit/test_steps_contract.py`
le diffe contre l'énoncé, et le fichier est exclu de `ruff format` comme de son
tri d'imports dans `pyproject.toml`.

## Organisation

```
app/domain/          entités, ports (Protocols), erreurs — ni I/O, ni framework
app/application/     cas d'usage : login, refresh, logout, upload_document
app/infrastructure/  SQLAlchemy, argon2, PyJWT, object store POSIX, hub LISTEN
app/api/             routeurs FastAPI, dépendances, mapping d'erreurs, middlewares
app/pipeline/        steps et workflow DBOS ; steps.py est l'énoncé verbatim
app/config.py        configuration, entièrement pilotée par l'environnement
app/observability.py configuration des logs et middleware de contexte de requête
app/seed.py          seed de développement idempotent
app/main.py          fabrique d'application et endpoint /health
scripts/             simulate_pipeline.py — d'où viennent les chiffres de latence
migrations/          Alembic ; 0001 auth/RLS · 0002 documents · 0003 pipeline ·
                     0004 index de liste · 0005 NOTIFY de progression
tests/unit/          cas d'usage contre des fakes en mémoire
tests/api/           routes avec les adaptateurs surchargés
tests/integration/   vrai Postgres ; prouve RLS. Ignorés sans TEST_POSTGRES_DSN
Dockerfile           build multi-stage, runtime distroless
compose.yaml         db, migrate, seed, api
```

Les dépendances ne pointent que vers l'intérieur : `domain` n'importe que la
bibliothèque standard, `application` importe `domain`, `infrastructure`
implémente les ports du domaine, et `api` câble le tout.
`[tool.uv] package = false` — c'est une application, pas une bibliothèque ;
`pytest` trouve `app/` via `pythonpath = ["."]`.

## Configuration

Tout passe par l'environnement, via `app/config.py`. Les variables les plus
susceptibles d'être touchées :

| variable | défaut | signification |
|---|---|---|
| `STORAGE_ROOT` | `/data/uploads` | où écrit l'object store POSIX |
| `MAX_UPLOAD_BYTES` | `104857600` | limite par fichier, 100 Mio |
| `MAX_BODY_OVERHEAD_BYTES` | `1048576` | marge au-dessus, pour le cadre multipart |
| `UPLOAD_CHUNK_BYTES` | `1048576` | taille de chunk en lecture/écriture |
| `PARTNER_HMAC_SECRET` | placeholder de dev | secret partagé du webhook |
| `PARTNER_WEBHOOK_SIGNING_HELPER` | `true` | expose l'oracle de signature de `/docs` |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | `console` pour une exécution locale lisible |
| `DB_ECHO` | `false` | écho SQL, indépendant de `LOG_LEVEL` |

## Dépendances

`uv.lock` est commité et fait foi. Ne jamais l'éditer à la main.

```bash
uv add <package>             # dépendance runtime
uv add --dev <package>       # dépendance de développement
uv lock --upgrade            # rafraîchir le lock
```

Le build Docker exécute `uv sync --locked`, qui **échoue** si `uv.lock` est
désynchronisé de `pyproject.toml`. Commitez les deux ensemble.

## À propos de l'image Docker

1. **builder** (`ghcr.io/astral-sh/uv`) — installe un CPython 3.14 relocalisable
   dans `/opt/python` et les dépendances verrouillées dans `/app/.venv`. Les
   dépendances sont installées avant la copie des sources, donc éditer du code
   n'invalide pas la couche de dépendances.
2. **runtime** (`gcr.io/distroless/cc-debian12:nonroot`) — copie l'interpréteur,
   le venv et `app/`. Pas de shell, pas de gestionnaire de paquets, pas de
   `pip`/`uv` ; tourne en uid 65532.

