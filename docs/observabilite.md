# Logs et corrélation

JSON structuré sur stdout, une ligne par événement significatif, avec assez de
corrélation pour suivre un upload depuis la requête HTTP jusqu'au webhook
partenaire qui le termine.

## Stdlib aux points d'appel, structlog en rendu

```python
logger = logging.getLogger(__name__)
logger.info("upload.stored", extra={"document_id": ..., "size_bytes": ...})
```

Aucune couche n'importe structlog : il est configuré une fois dans
`app/observability.py` comme *renderer*, et les records stdlib le traversent.
`domain` et `application` continuent donc de n'importer que la bibliothèque
standard, et changer de renderer ne touche aucun point d'appel. Les noms
d'événements sont des identifiants pointés stables plutôt que des phrases, pour
être grepés, comptés et alertés sans parser de la prose ; tout ce qui varie va
dans `extra`.

## Corrélation

Une seule clé ne suffit plus dès que le travail survit à la requête :

| clé | couvre | posée par |
|---|---|---|
| `request_id` | une requête HTTP | `RequestContextMiddleware`, renvoyée en `X-Request-Id` |
| `org_id`, `user_id` | tout ce qui suit l'authentification | la dépendance d'auth |
| `document_id` | un document, workers détachés compris | les points d'appel qui l'ont |
| `workflow_id` | une exécution de pipeline | `pipeline.enqueued` le relie à la requête |

Un `X-Request-Id` entrant est respecté, pour qu'une passerelle corrèle entre
services, mais assaini d'abord : il est contrôlé par l'appelant et atterrit dans
chaque ligne de log de la requête — le chemin classique d'injection de logs. Les
valeurs hors de `[A-Za-z0-9._-]{1,64}` sont remplacées.

**`request_id` n'atteint pas toutes les lignes du pipeline, et c'est attendu** :
DBOS exécute les branches parallèles sur leurs propres tâches, sans propager les
contextvars de la requête. D'où `document_id` sur chaque ligne de pipeline — la
clé qui survit au passage de relais — et `pipeline.enqueued`, qui enregistre
`request_id` et `workflow_id` ensemble pour joindre les deux moitiés.

## Niveaux

Le rôle d'un niveau est de répondre à « est-ce que quelqu'un doit regarder ? ».

| niveau | signifie | exemples |
|---|---|---|
| `ERROR` | on a échoué sur ce qu'on avait promis | `upload.row_failed`, `pipeline.failed`, tout 5xx |
| `WARNING` | une requête refusée, à surveiller en agrégat | `auth.token.rejected`, `pipeline.step.attempt_failed` |
| `INFO` | un état durable a changé | `upload.stored`, `pipeline.step.succeeded` |
| `DEBUG` | mécanique, coupé en production | `storage.put.aborted` |

Deux choix assumés : **les erreurs client sont en WARNING**, parce qu'un
utilisateur qui uploade un PNG, c'est l'API qui fonctionne, et alerter là-dessus
est la façon dont on apprend à ignorer les alertes (un *pic* de 415 est une autre
histoire, et c'est le rôle de l'agrégation) ; et **une tentative de step échouée
est en WARNING**, puisque les mocks échouent une fois sur trois par construction —
seul l'épuisement des retries est un vrai échec. Les sondes de santé loggent en
DEBUG : en INFO elles seraient la majorité du volume sans rien dire.

## Jamais loggé

Mots de passe, tokens, signatures HMAC, contenus de fichiers.
`tests/api/test_logging.py` l'asserte sur la sortie **rendue**, pas seulement sur
les records : un token qui n'apparaîtrait qu'après formatage serait quand même un
token dans un fichier de log. `upload.stored` enregistre un `sha256_prefix` de 12
caractères plutôt que le digest complet.

## Deux pièges désamorcés

- **Le niveau applicatif ne pilote pas l'écho SQL.** `sqlalchemy.engine` émet
  chaque requête *et ses paramètres liés* dès INFO — ce qui, en héritant d'un root
  en INFO, donnait des milliers de lignes contenant noms de fichiers, digests et
  l'email cherché au login. Les loggers tiers sont épinglés à WARNING ; l'écho SQL
  est la décision de `DB_ECHO`.
- **Alembic ne désactive pas les loggers de l'application.** Le template standard
  appelle `fileConfig(...)` avec `disable_existing_loggers=True`, ce qui désactive
  tout `app.*` : une migration en interne et plus rien ne logge, silencieusement.
  `migrations/env.py` passe `disable_existing_loggers=False`.

Les deux sont couverts par `tests/unit/test_observability.py`.

`LOG_LEVEL` (défaut `INFO`) et `LOG_FORMAT` (`json`, ou `console` en local)
pilotent le reste. uvicorn tourne avec `--no-access-log` :
`RequestContextMiddleware` émet lui-même la ligne d'accès, avec l'id de requête,
le tenant et la durée — laisser celle d'uvicorn loggerait tout deux fois, sans les
champs de corrélation.

## Ce qui n'est pas là

Ni métriques ni traces : rien n'expose `/metrics`, rien n'émet de spans, et
aucune question agrégée — p95 contre le budget de 120s, taux d'abandon, bande
passante d'upload — ne trouve de réponse dans un système en fonctionnement. Les
clés ci-dessus répondent à « qu'est-il arrivé à *cet* upload » depuis
`docker compose logs`, et c'est là que passe la ligne actuelle.

C'est un ordre de priorité assumé : la corrélation devait exister d'abord, et un
exporteur pointé vers rien est de la configuration sans contrepartie. Le
mouvement suivant est OpenTelemetry (spans + métriques, APM ou Prometheus
derrière), argumenté en F3 dans
[decisions-et-limites.md](decisions-et-limites.md). La couture est déjà posée :
`request_id` devient un contexte de trace.
