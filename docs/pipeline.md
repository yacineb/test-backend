# Le pipeline de traitement des documents

Comment le pipeline est construit et pourquoi. Le choix de l'orchestrateur est
argumenté dans [orchestration.md](orchestration.md) ; ici, il s'agit du code.

```
upload commité ──► ocr ──► metadata ──┐
                      └──► chunking ──┴──► external_call ──► awaiting_partner
                                                                    │
                                            POST /webhooks/partner ─┘──► ready
```

## Ce qui le déclenche

Le pipeline démarre quand la transaction portant la ligne document et ses quatre
lignes de step **commite** — et seulement alors le job est enfilé. Enfiler dans
la transaction est une course que le worker gagne généralement, puisqu'il
n'attend pas un client HTTP : il prendrait un job et ne trouverait rien à mettre
à jour.

C'est `upload_document` qui en est responsable. Il possédait déjà « ce qui rend
un upload acceptable » ; il possède désormais aussi « l'upload est terminé », qui
est le même fait. Il commite, démarre le pipeline, enregistre l'id de workflow,
et commite à nouveau. Cela demande une session que le handler commite lui-même,
donc l'endpoint d'upload tourne sur `Database.tenant_session_manual` plutôt que
sur `tenant_session`, qui ne commite qu'après la réponse. Commiter efface
`app.current_org_id` — `set_config` demande une valeur locale à la transaction
pour qu'elle ne fuie pas vers le prochain emprunt d'une connexion poolée — et
`TenantUnitOfWork` la ré-épingle après chaque commit, ce qui préserve la
propriété.

`tests/unit/test_upload_document.py` asserte qu'un commit avait bien eu lieu au
moment de l'appel au runner, et qu'un upload échoué ne démarre jamais de
pipeline.

## À quoi sert `external_call`

Le partenaire est un **service d'archivage réglementé** : il indexe les chunks
dans un coffre de conformité, valide les métadonnées extraites contre des règles
de rétention, et publie le document dans le système d'enregistrement du client.
Ce travail prend des minutes à des heures de leur côté, donc l'appel ne renvoie
qu'un `job_id` opaque et le résultat arrive plus tard par webhook.

## La jonction du webhook

Le workflow **se termine** à `awaiting_partner` plutôt que de parker sur
`DBOS.recv()`. Parker fonctionne, mais garder un état de workflow ouvert pendant
des heures face à un tiers n'apporte rien qu'une colonne de statut et une clé de
corrélation n'apportent, et couper à cet endroit garde le webhook entrant
testable indépendamment.

Le contrat sur lequel s'appuie `DbPartnerJobSink` :

- Un document n'est **jamais** en `awaiting_partner` sans un `partner_job_id`
  visible. Les deux sont écrits dans la transaction qui marque `external_call`
  réussi, parce que le partenaire peut rappeler à l'instant même où il rend la
  main.
- `partner_job_id` est unique, donc une notification ne peut jamais résoudre vers
  deux documents.
- La résolution passe par la **session système** (`BYPASSRLS`). Le partenaire ne
  nomme aucun tenant — il n'a que le `job_id` — donc il n'y a rien pour scoper la
  recherche tant que la ligne n'est pas trouvée, et l'organisation en est ensuite
  *dérivée*. Même raisonnement que « trouver un utilisateur par email » au login.
- La livraison est idempotente. Les partenaires rejouent, et un document sorti de
  `awaiting_partner` est décidé ; ré-appliquer pourrait faire basculer un
  document `ready` en `failed` sur une reprise obsolète.

`ready` n'est atteignable que par ce chemin. Signature, fraîcheur et codes de
statut sont dans [webhook-entrant.md](webhook-entrant.md).

## Suivre la progression

`GET /documents/{id}/events` streame du `text/event-stream`. Mesuré contre la
stack en fonctionnement, un changement de statut atteint un client connecté en
**~80 ms** ; la cible était « de l'ordre de la seconde ».

Le mécanisme tient en un saut, parce que la source d'événements existe déjà :
chaque changement de statut est une écriture dans `document_steps`, et la
migration `0005` transforme chacune en `NOTIFY`. Postgres est déjà le broker,
donc rien n'a été ajouté à `compose.yaml`.

```
worker / webhook ──commit──► trigger ──pg_notify('document_progress', <doc_id>)
                                                    │
                                 une connexion LISTEN par processus d'API
                                                    │
                                          fan-out en mémoire
                                                    │
                            la tâche SSE relit la projection → yield
```

**Pourquoi un trigger plutôt que du code applicatif.** `NOTIFY` est
transactionnel : il est délivré au commit et jamais pour une écriture annulée,
donc un listener n'est jamais informé d'une ligne qu'il ne pourrait pas lire. Et
il couvre tous les chemins d'écriture par construction — y compris le webhook
partenaire, qui pose `ready` via un autre repository sur la session *système*, et
qui est exactement le genre de chemin qu'une notification applicative oublie.
`tests/integration/test_progress_notify.py` épingle les deux.

**Pourquoi le payload n'est qu'un id.** Les abonnés relisent la projection, donc
une notification dupliquée, coalescée ou désordonnée est sans conséquence, il n'y
a pas de plafond de 8000 octets à contourner, et aucune donnée tenant ne traverse
un canal que tous les processus écoutent. Le corps de l'événement est le même
`DocumentDetailResponse` que renvoie l'endpoint de polling : les deux ne peuvent
pas diverger.

**Pourquoi ça coûte si peu.** Une connexion `LISTEN` *par processus*, pas par
abonné : 5 000 watchers coûtent 5 000 sockets et une connexion base. Les lectures
suivent les événements, pas watchers × secondes — 37 à 112× moins de travail
qu'un polling à 1s à la cible 12 mois :

| | watchers | push | polling @1s |
|---|---|---|---|
| aujourd'hui | 50 | 0,4 évén./s | 50 req/s |
| pic à 12 mois | 5 000 | 134 évén./s | 5 000 req/s |

Trois détails qui tiennent l'ensemble :

- **La coalescence est gratuite.** La queue de chaque abonné est en `maxsize=1`
  et jette quand elle est pleine — un réveil déjà en attente signifie « relis »,
  un second dirait exactement la même chose. Le debouncer est la queue, sans
  timer.
- **Chaque connexion s'ouvre sur un snapshot**, ce qui rend la reconnexion sans
  rejeu et rend le plafond de 5 minutes par connexion sûr plutôt que lossy. Un
  document parké en `awaiting_partner` toute la nuit ne tient pas une socket
  toute la nuit.
- **Une reconnexion du listener réveille tous les abonnés**, parce que les
  notifications émises pendant la coupure sont perdues.

**Swagger ne sait pas afficher un flux** — il semblera figé. Utilisez `curl -N`,
ou faites du polling sur `GET /documents/{id}`, qui renvoie le corps identique.

```bash
curl -N -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/documents/$DOC/events
```

L'`EventSource` du navigateur ne peut pas envoyer d'en-tête `Authorization` : une
vraie UI demanderait un ticket de flux à courte durée de vie. Non construit — il
n'y a pas d'UI, et inventer cet endpoint maintenant serait spéculatif.

## Politique de retry : mesurée, pas choisie

Les mocks fournisseurs dorment *avant* le tirage d'échec, donc une tentative
ratée coûte le même temps qu'une réussie : réessayer coûte cher ici, d'où une
politique mesurée. `scripts/simulate_pipeline.py`, 200 000 pipelines simulés :

| politique | p50 | p95 | p99 | documents abandonnés |
|---|---|---|---|---|
| 1 tentative (sans retry) | 18,7s | 26,6s | 28,7s | **80,2 %** |
| 3 tentatives, sans backoff | 25,3s | 42,6s | 51,1s | 14,1 % |
| 5 tentatives, sans backoff | 26,6s | 48,5s | 60,4s | 1,6 % |
| **5 tentatives, expo 1/2/4/8s** | **28,5s** | **56,9s** | **73,7s** | **1,6 %** |
| 5 tentatives, expo 5/10/20/40s | 35,3s | 95,1s | 137,3s | 1,7 % |

- **Les retries sont structurants.** Sans eux, 80 % des documents n'atteignent
  jamais le partenaire. DBOS livre `retries_allowed=False, max_attempts=3` ; ce
  défaut donnerait 14 % d'abandon.
- **Le backoff doit rester petit.** Une base de 5s pousse à elle seule le p99
  au-delà des 120s. Ces échecs sont des tirages à pile ou face simulés, pas un
  aval en souffrance : la patience n'achète rien.
- **La marge est là pour la mise en file.** Un p95 de 56,9s contre 120s laisse
  ~63s, qui sont le budget d'attente d'un worker — la vraie contrainte de scale.
- **Le fan-out gagne ~9s au p95** (56,9s en parallèle contre 65,5s en série).

`tests/unit/test_retry_policy.py` épingle ces nombres : les baisser fait échouer
le build au lieu de tripler silencieusement le taux d'abandon.

## Modèle de données : deux propriétaires, un sens

DBOS checkpointe chaque step dans ses propres tables du schéma `dbos`.
`documents` et `document_steps` sont un **modèle de lecture exposé au tenant** —
écrit par les wrappers de step, lu par l'API, jamais orchestré dessus. Nous ne
lisons pas le schéma de DBOS : c'est son contrat interne, pas le nôtre. La
duplication est délibérée et à sens unique, ce qui la distingue du problème des
deux sources de vérité que crée un broker.

- **`org_id` est en tête d'index**, donc la liste d'un tenant est un parcours
  d'intervalle d'index plutôt qu'un scan-puis-filtre.
- **Les quatre lignes de step existent dès la création**, écrites dans la même
  transaction que le document. « Pas démarré » est une ligne à `status=pending`,
  jamais une ligne absente : rien en aval n'a de cas particulier pour la
  progression partielle, et tout écrivain ultérieur peut supposer que sa ligne
  existe.
- **`output` est toujours du `jsonb`.** Un vrai texte OCR pèse des mégaoctets ;
  seul un aperçu est projeté (`{"chars": n, "preview": "..."}`) et le texte
  complet reste dans le checkpoint. L'inliner coûterait ~10 Go/jour
  d'amplification d'écriture à la cible.
- **`document_steps` porte un `org_id` dénormalisé.** Toute autre table tenant
  fonde sa politique RLS sur une colonne qu'elle possède, et une politique qui
  devrait rejoindre `documents` serait à la fois plus lente et un cas particulier
  dans la migration.

La migration `0003` ajoute `workflow_id`, `partner_job_id` et `failed_step` à
`documents`, plus `document_steps` et sa politique. `DocumentStatus` est passé
d'une à cinq valeurs ; c'était déjà une colonne texte plutôt qu'un enum natif,
précisément pour qu'ajouter des valeurs ne demande pas de verrou.

## Tenancy

Les workers du pipeline écrivent **sans utilisateur présent**, et c'est le cas
intéressant. Leur organisation vient de l'argument du workflow lui-même, qui est
aussi ce qui épingle `app.current_org_id` sur leur session : les écritures worker
sont couvertes par RLS exactement comme les écritures de requête, et un worker ne
peut pas écrire hors du tenant pour lequel il a été démarré.

La file de travail est **partitionnée par `org_id`**, donc une organisation
uploadant 10 000 documents ne peut pas affamer l'upload unique d'une autre. Sans
ça, l'équité entre tenants est un accident d'ordonnancement.

`tests/integration/test_document_tenancy.py` asserte l'isolation deux fois : via
le repository, qui filtre explicitement, et via du SQL volontairement non filtré,
que seule la base peut arrêter.

## Le problème du sleep bloquant

Les mocks fournisseurs appellent un `time.sleep()` bloquant, donc chaque step en
vol tient un thread. Les steps sont `async` et poussent les mocks dans
`asyncio.to_thread`, ce qui garde la boucle d'événements libre mais ne supprime
pas le thread. À la charge d'aujourd'hui c'est ~0,4 step concurrent ; à la cible
12 mois c'est ~390, ce qui fait trop de threads OS pour être confortable. Le
sleep du mock tient lieu d'E/S réseau, donc la correction est d'`await` de vrais
appels HTTP quand les fournisseurs seront réels — et alors 390 coroutines ne sont
rien. Signalé plutôt que résolu : le construire maintenant, ce serait construire
pour une charge qui n'existe pas.

`PIPELINE_QUEUE_POLLING_INTERVAL_SECONDS` (défaut 1,0s) est de la latence pure,
et tombe deux fois par document : ~2s des ~63s de marge.

## Tests

Le code fournisseur n'est jamais modifié, y compris par les tests. `steps.py`
résout `random` depuis ses propres globales de module, donc un test peut
remplacer **ce seul nom** ; le vrai module `random`, et tout ce qui l'utilise
ailleurs, reste intact.

- `tests/unit/test_steps_contract.py` diffe le module livré contre le bloc de
  code de `README.md` — l'énoncé lui-même. Tous les nombres de latence ci-dessus
  dérivent de ces mocks, donc un nettoyage bien intentionné échoue bruyamment au
  lieu de les invalider silencieusement. `ruff format` comme son tri d'imports
  casseraient la correspondance, d'où l'exclusion du fichier dans
  `pyproject.toml`.
- `tests/unit/` couvre les cas d'usage et la projection contre des fakes : pas de
  base, pas d'orchestrateur.
- `tests/integration/` couvre RLS sur `documents` et `document_steps`, le trigger
  `NOTIFY` et le vrai sink partenaire, contre un Postgres exécutant les vraies
  migrations.
- Le pipeline de bout en bout est exercé via `docker compose`.

Les limites connues et la suite sont rassemblées dans
[decisions-et-limites.md](decisions-et-limites.md).
