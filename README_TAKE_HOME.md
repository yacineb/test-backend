# Guide de lecture du rendu

L'énoncé est dans [README.md](README.md). Ce document est le point d'entrée pour
relire ce qui a été construit : ce que ça fait, comment le lancer, où chaque
décision est argumentée, et ce qui n'a volontairement pas été fait.

**Commencez ici, puis lisez [docs/decisions-et-limites.md](docs/decisions-et-limites.md).**
Ce document défend chaque choix d'architecture face aux cibles de scale de
l'énoncé, liste les limites assumées, et ordonne la suite. Tout le reste en est
le détail.

## Temps passé

4h c'est assez serré. l'arbitrage a été : **construire le chemin critique complet et documenter le reste avec le déclencheur qui le rendrait nécessaire**, plutôt que d'empiler des fonctionnalités à moitié faites.


## Choix techniques

| Couche | Choix | En une ligne |
|---|---|---|
| API | **FastAPI** + Pydantic v2 | imposé, et le schéma OpenAPI sert de contrat exerçable depuis `/docs` |
| Base | **PostgreSQL 17**, SQLAlchemy 2 async, asyncpg, Alembic | la seule dépendance stateful du système |
| Isolation tenant | **RLS Postgres** + rôle applicatif sans `BYPASSRLS` | l'isolation tient même si le code applicatif a un bug |
| Orchestration | **DBOS Transact** (exécution durable, checkpointing) | une bibliothèque au-dessus de Postgres, pas un cluster de plus — le comparatif avec Celery, Temporal et Restate est en [orchestration.md](docs/orchestration.md) |
| Temps réel | **SSE** sur `LISTEN/NOTIFY` Postgres | ~80 ms bout en bout, zéro service ajouté |
| Stockage fichiers | port `ObjectStore`, adaptateur POSIX | contrat « complet ou absent », prêt pour S3 (F1) |
| Auth | JWT d'accès + refresh opaque rotatif | volontairement jetable : à déléguer à un IdP managé (F10) — le détail est en [authentification.md](docs/authentification.md) |
| Détection de type | **puremagic** sur les octets de tête | jamais le `Content-Type` annoncé par le client |
| Sécurité HTTP | **secure** (preset `STRICT`) + CSP maison | les en-têtes suivent la bibliothèque, la CSP suit le `Content-Type` |
| Logs | **structlog** en rendu, stdlib aux points d'appel | aucune couche métier ne dépend de structlog |
| Build | **uv**, image docker multi-stage, runtime **distroless** | pas de shell, pas de gestionnaire de paquets, uid 65532 |

Les décisions structurantes, en une phrase chacune :

1. **Une seule dépendance stateful.** Postgres porte les données applicatives,
   les checkpoints de l'orchestrateur, la file de travail et le bus de
   progression. Pas de broker, pas de cache.
2. **L'exécution durable plutôt qu'une file de tâches.** Le pipeline est un
   workflow avec un point de suspension externe (`awaiting_partner`), pas une
   suite de tâches indépendantes. Le comparatif avec Celery, en détail :
   [orchestration.md §3](docs/orchestration.md) et
   [decisions-et-limites.md §2](docs/decisions-et-limites.md).
3. **Le tenant vient du token, et rien d'autre.** `org_id` n'est un paramètre
   d'aucune API : uploader chez un autre tenant n'est pas une requête qu'on
   refuse, c'est une requête qu'on ne peut pas exprimer.
4. **Les octets avant la ligne.** L'objet est durable avant l'`INSERT`, donc
   toute ligne de `documents` référence un objet complet — un seul invariant, pas
   deux faits à réconcilier.
5. **La projection est à nous.** `documents` / `document_steps` sont un modèle de
   lecture écrit par nos wrappers, jamais relu depuis le schéma de DBOS : c'est
   ce qui borne le coût d'un changement de moteur.
6. **Les chiffres sont mesurés, pas estimés.** Politique de retry, pagination,
   limite de taille, latence SSE : chaque nombre cité vient d'une mesure
   reproductible.

## Ce que c'est, en un schéma

```
POST /documents ──► sniff PDF ──► stream vers ObjectStore ──► INSERT ligne ──┐
   (multipart)    (octets de tête)   (atomique, complet-ou-absent)           │
                                                                          commit
                                                                             │
   ocr ──► metadata ──┐                                                      ▼
     └──► chunking ───┴──► external_call ──► awaiting_partner        workflow DBOS
                                                    │                (checkpointé)
                          POST /webhooks/partner ───┘──► ready

  progression : écriture document_steps ──► trigger ──► NOTIFY ──► SSE, ~80 ms
```

`compose.yaml` se résume à `db → migrate → seed → api`.

## Le lancer

```bash
docker compose up --build          # Swagger sur http://localhost:8000/docs
```

Migrations et seed tournent au démarrage : deux organisations existent
immédiatement.

| Organisation | Email | Mot de passe |
|---|---|---|
| Acme Corp | `alice@acme.example.com` | `password123` |
| Globex | `bob@globex.example.com` | `password123` |

Un passage bout en bout en cinq minutes :

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"alice@acme.example.com","password":"password123"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

# 1. Upload. Rien ici ne nomme d'organisation : elle vient du token.
DOC=$(curl -s -X POST http://localhost:8000/documents \
  -H "Authorization: Bearer $TOKEN" -F "file=@some.pdf;type=application/pdf" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

# 2. Suivre le pipeline en direct (Swagger ne sait pas afficher un flux, curl -N si).
curl -N -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/documents/$DOC/events

# 3. Lister. Reconnectez-vous en bob@globex.example.com et relistez : aucun des
#    deux ne voit les documents de l'autre, et seul le token a changé.
curl -s "http://localhost:8000/documents?limit=2" -H "Authorization: Bearer $TOKEN"
```

Puis terminez le document depuis Swagger : `POST /webhooks/partner/sign` signe un
body, et `POST /webhooks/partner` avec cette signature dans `X-Partner-Signature`
fait passer le document à `ready`. Envoyez **les mêmes octets** aux deux : la
signature les couvre exactement.

Une fois `ready`, `GET /documents/$DOC/data` rend les données extraites — une
clé par step, plus le `result` que le partenaire a envoyé. Avant, c'est un `409`
qui nomme l'état courant plutôt qu'un corps à moitié rempli
([donnees-extraites.md](docs/donnees-extraites.md)).

Installation, exécution hors Docker, variables d'environnement et commandes de
test sont dans [CONTRIBUTING.md](CONTRIBUTING.md).

## L'API

| Méthode | Chemin | Notes |
|---|---|---|
| `POST` | `/auth/login` · `/auth/refresh` · `/auth/logout` | JWT d'accès, refresh opaque rotatif |
| `GET` | `/me` | l'utilisateur authentifié et son organisation |
| `POST` | `/documents` | upload multipart streamé ; PDF uniquement, décidé sur les octets |
| `GET` | `/documents` | l'organisation appelante, plus récents d'abord, paginé par curseur |
| `GET` | `/documents/{id}` | état de traitement, step par step |
| `GET` | `/documents/{id}/events` | le même corps, poussé en SSE |
| `GET` | `/documents/{id}/data` | les données extraites, une fois le document `ready` |
| `POST` | `/webhooks/partner` | callback partenaire signé en HMAC |
| `POST` | `/webhooks/partner/sign` | helper de signature, pour `/docs`, dev uniquement |
| `GET` | `/health` | liveness |

## Parcours de lecture

**Lisez la première ligne.** C'est l'argumentaire complet du rendu, et il pointe
vers le reste là où vous voudrez creuser une affirmation précise. Les huit autres
sont de la profondeur à la demande — mesures, plans de requête, contrats — pas
une file d'attente à écouler. Les neuf ensemble : environ 99 minutes en lecture
attentive, moitié moins en survol.

| Document | La question à laquelle il répond | ~min |
|---|---|---|
| **[decisions-et-limites.md](docs/decisions-et-limites.md)** | **Chaque choix défendu face aux cibles de scale ; ce qui n'est volontairement pas construit et ce qui forcerait à le faire ; la suite, dans l'ordre** | **24** |
| [orchestration.md](docs/orchestration.md) | Pourquoi DBOS plutôt que Temporal, Restate, Celery ou une file maison — et pourquoi le débit ne pouvait pas trancher | 7 |
| [architecture-upload.md](docs/architecture-upload.md) | La table `documents`, le contrat de stockage, pourquoi la limite de taille est appliquée deux fois, d'où vient le tenant | 15 |
| [authentification.md](docs/authentification.md) | Pourquoi le refresh n'est pas un JWT, comment un rejeu tue la session, pourquoi il y a deux rôles Postgres | 11 |
| [liste-documents.md](docs/liste-documents.md) | Pagination keyset mesurée sur 2M lignes, ce que contient le curseur, pourquoi cet endpoint devrait finir en GraphQL | 11 |
| [pipeline.md](docs/pipeline.md) | Le DAG, la politique de retry mesurée, la projection, et comment la progression arrive au client en ~80 ms | 12 |
| [webhook-entrant.md](docs/webhook-entrant.md) | Signature sur les octets bruts, fenêtre anti-rejeu, codes de statut, idempotence | 6 |
| [observabilite.md](docs/observabilite.md) | Niveaux de log, clés de corrélation, ce qui n'est jamais loggé | 6 |
| [donnees-extraites.md](docs/donnees-extraites.md) | Où vit la réponse du partenaire et pourquoi pas ailleurs, pourquoi `409` avant `ready`, ce qui n'est pas rendu | 6 |

## Ce qui est vérifié

```bash
uv run pytest        # 233 tests, aucun service requis
TEST_POSTGRES_DSN=… uv run pytest   # 270, suite d'intégration comprise
```

Les tests unitaires et d'API tournent sur des fakes en mémoire. La suite
d'intégration — isolation RLS assertée via du SQL volontairement *non filtré*, le
trigger `NOTIFY`, le vrai sink partenaire — exécute les migrations Alembic
réelles contre un vrai Postgres, et est ignorée tant que `TEST_POSTGRES_DSN` ne
pointe pas sur une base jetable (voir [CONTRIBUTING.md](CONTRIBUTING.md)). Ce
skip silencieux est une faiblesse connue, listée en L16.

Les chiffres cités dans les docs sont mesurés, pas estimés : les latences du
pipeline viennent de `scripts/simulate_pipeline.py` sur 200 000 exécutions
simulées et sont épinglées par `tests/unit/test_retry_policy.py` ; les temps de
pagination et les plans de requête viennent d'une table de 2 000 031 lignes ; les
mesures de limite d'upload et de latence SSE viennent de la stack compose en
fonctionnement.

## Où regarder dans le code

| | |
|---|---|
| `app/domain/ports.py` | toutes les frontières du système, dans un seul fichier |
| `app/application/upload_document.py` | le cas d'usage d'upload, commit-puis-enqueue inclus |
| `app/infrastructure/storage/posix.py` | le contrat de stockage complet-ou-absent |
| `app/infrastructure/db/session.py` | sessions RLS et les deux rôles de base |
| `app/pipeline/workflow.py` | le DAG et les wrappers de projection |
| `app/pipeline/steps.py` | les mocks fournisseurs, à l'octet près depuis l'énoncé |
| `app/infrastructure/progress.py` | connexion `LISTEN`, fan-out en mémoire |
| `app/api/cursors.py` | le codec du curseur keyset |
| `migrations/versions/` | schéma, rôles, politiques RLS, trigger `NOTIFY` |
