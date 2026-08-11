# Décisions, limites assumées, et la suite

Le rendu en un document : chaque choix d'architecture avec son argument, ce qui
a été sciemment laissé de côté et ce qui forcerait à le faire, et l'ordre dans
lequel la suite serait construite. Chaque choix pointe vers le document qui
l'argumente en détail.

## 0. Les chiffres face auxquels tout est défendu

De l'énoncé : **aujourd'hui** ~1 000 documents/jour et ~50 utilisateurs
concurrents ; **cible 12 mois** ~100 000 documents/jour, ~5 000 utilisateurs
concurrents, p95 du pipeline sous 2 minutes.

Dérivé de `scripts/simulate_pipeline.py` (200 000 pipelines simulés, modélisant
exactement les mocks fournisseurs) :

| | aujourd'hui | cible, journée 8h | cible, 8h + burst ×3 |
|---|---|---|---|
| documents/seconde | 0,012 | 3,47 | 10,42 |
| exécutions de step/s (retries inclus) | 0,07 | 20,7 | 62,0 |
| slots de step concurrents | 0,4 | 130 | 390 |
| p95 pipeline | — | 56,9s pour un budget de 120s | |

Côté upload, même histoire : 100 000/jour dont 80 % dans une fenêtre de 8 heures
donnent **2,8 uploads/s**, soit à 2 Mo de PDF moyen 5,6 Mo/s ≈ 45 Mbit/s en
pointe, avec environ 9 uploads en vol à tout instant.

Tout ce qui suit est défendu face à ces nombres, et non seulement mon intuition.

## 1. Postgres suffit largement

- **La charge d'écriture est de quelques centaines de lignes par seconde en
  pointe.** Un document coûte 1 ligne `documents` + 4 lignes `document_steps` à
  la création, puis un début et une fin par exécution de step (5,9 en comptant
  les retries), plus les checkpoints de DBOS. Disons ~30 petites écritures de
  ligne par document → **~310 écritures/s au burst ×3**. Un nœud Postgres modeste
  encaisse des milliers de petites transactions d'écriture par seconde. Il n'y a
  pas de problème d'écriture à résoudre.
- **La charge de file est de 62 claims/seconde.** `SELECT … FOR UPDATE SKIP
  LOCKED` en soutient des milliers sur un nœud. C'est le chiffre pour lequel on
  croit avoir besoin de Kafka ; il a besoin d'un index.
- **Les lectures sont des parcours d'index, mesurés.** La liste est une page
  keyset : 0,33–0,49 ms sur 2 000 031 documents dont 500 000 dans l'organisation
  appelante, et le coût ne croît pas avec la profondeur de scroll
  ([liste-documents.md §2](liste-documents.md)).
- **Le volume reste petit parce que le gros payload n'y est délibérément pas.**
  ~1,5 Ko de métadonnées par document → ~150 Mo/jour → ~55 Go/an. Inliner le
  texte OCR au lieu d'une projection `{"chars": n, "preview": …}` donnerait ~10
  Go/jour d'amplification d'écriture — d'où le preview en jsonb, le texte
  complet restant dans le checkpoint (F6 le déplace vers l'object store).
- **5 000 utilisateurs concurrents, ce n'est pas 5 000 connexions base.** C'est
  un tier d'API poolé devant un pool. Le seul endroit où ce nombre aurait mordu,
  c'est la progression : 5 000 watchers en polling 1 Hz font 5 000 lectures/s.
  En push, les lectures suivent les *événements* (134/s au pic cible) et non
  watchers × secondes — 37 à 112× moins de travail — pour une connexion `LISTEN`
  par processus, pas par watcher.
- **Donc la base n'est pas la contrainte. Les threads le sont** (L9), et
  l'intervalle de polling de la file aussi (L10). Les deux sont nommés, et aucun
  ne se règle en ajoutant de l'infrastructure.
- **Un seul service stateful, c'est une sauvegarde, un failover, une procédure de
  restauration, une chose à superviser, une chose qui réveille quelqu'un la
  nuit.** À 0,07 job/seconde aujourd'hui, tout service stateful supplémentaire
  coûte plus en exploitation qu'il ne rapporte en capacité.

## 2. L'exécution durable *au-dessus de Postgres* est la bonne forme

- **L'état du workflow et l'état du document doivent être d'accord, et ici ils
  partagent le même domaine transactionnel.** L'orchestrateur checkpointe dans le
  schéma `dbos` de la base où vit la projection. Rien à réconcilier à travers une
  frontière réseau à l'endroit où se décide la correction.
- **La reprise reprend, elle ne recommence pas.** Chaque sortie de step est
  persistée à mesure, donc un worker tué repart au premier step incomplet. Avec
  1,6 % de documents abandonnés à la cible — ~1 600/jour — pouvoir rejouer *à
  partir* d'un historique par step est une exigence d'exploitation, pas un
  confort.
- **Le moteur est une bibliothèque, pas un cluster.** Aucun control plane à
  faire tourner, mettre à jour, sécuriser ou payer. `compose.yaml` se résume à
  `db → migrate → seed → api` ; DBOS migre ses propres tables au lancement.
- **Le DAG est une constante, donc le bénéfice du replay est inutilisé.** Quatre
  steps, forme fixe, pas de boucle, pas de branchement dynamique, pas d'état
  accumulé en mémoire, et des sorties qui sont une chaîne, un dict, une liste de
  chaînes et un id opaque. Choisir le replay déterministe (Temporal) reviendrait
  à accepter les contraintes de déterminisme et le versioning de workflow en
  échange d'un contrôle de flux durable arbitraire que ce pipeline n'a pas.
  C'est ça l'argument de fond, pas « Temporal est lourd »
  ([orchestration.md §2](orchestration.md)).
- **Le point de suspension est réel, et c'est là que les files de tâches
  s'arrêtent.** Le workflow se termine à `awaiting_partner` ; un partenaire qui
  répond en heures se modélise par une colonne de statut et une clé de
  corrélation unique — moins cher, et testable indépendamment.
- **Le débit n'a pas tranché, et ne pouvait pas.** Tous les candidats passent les
  62 exécutions de step/s avec deux ordres de grandeur de marge
  ([orchestration.md §1](orchestration.md)).

### Pourquoi Celery serait un mauvais choix ici

Ce n'est pas un jugement de qualité — Celery est mature, largement exploité,
facile à recruter — et ce n'est pas un jugement qui vaut partout : pour une
équipe qui l'exploite déjà en production, la plupart des objections ci-dessous
portent sur *l'ajout* d'un broker plutôt que sur la vie avec, et la familiarité
vaut de l'argent. Sur un service greenfield, c'est la mauvaise *forme* : une
file de tâches, là où on a un workflow avec état et un point de suspension
externe.

- **Il ajoute un second domaine de durabilité pour 0,07 job/seconde.** Le broker
  porte l'état de la file, Postgres l'état du document, et rien ne les garde
  cohérents : un worker qui acquitte au broker puis meurt avant de commiter a
  menti. Corriger proprement veut dire enqueue-après-commit ou outbox
  transactionnel — plus de machinerie que la file qu'on voulait s'épargner.
- **Le broker est un service stateful que vous exploitez désormais.** Redis
  n'est pas durable par défaut : rendez-le durable et vous opérez un second
  service stateful, laissez-le tel quel et vous perdez des documents au
  redémarrage. RabbitMQ est durable, et c'est un troisième conteneur.
- **Le fan-out réclame un `chord`**, qui réclame un result backend, dont le
  compteur de complétion vit dans le broker. L'interaction chord × retries est
  un piège connu : une tâche rejouée dans le groupe peut laisser le chord
  suspendu. Or `metadata` et `chunking` échouent une fois sur trois — ce n'est
  pas le cas rare ici, c'est le cas courant.
- **Celery rejoue une tâche, il ne reprend pas un workflow.** Si `external_call`
  échoue après le succès de `metadata` et `chunking`, il n'y a aucun checkpoint
  d'où repartir : ce qui est fini vit dans un compteur de broker. On finit par
  écrire `document_steps` comme véritable machine à états — exactement ce pour
  quoi on voulait un framework.
- **La sémantique de crash impose un choix sans bon côté.** Par défaut
  (`acks_late=False`) le message est acquitté à la livraison : un worker tué perd
  le step silencieusement. Avec `acks_late=True` le message est re-livré, ce qui
  exige des steps rejouables — et les fonctions fournisseurs ne sont
  explicitement pas idempotentes. Bien le faire suppose un bail avec expiration
  et une reprise sur état : reconstruire le checkpointing, à la main, mal.
- **`awaiting_partner` n'a aucune place dedans.** Parker pendant des heures n'est
  pas le métier d'une file de tâches ; la machine à états et le sweep de timeout
  sont entièrement à votre charge.
- **L'observabilité est en forme de tâche, pas de document.** Flower montre des
  événements de tâche. Répondre à « où en est le document X, et pourquoi »
  demande de corréler des logs.

### Simplicité, robustesse, et migration path

- **Peu de moving parts :** une base, une image applicative, zéro broker,
  zéro cache, zéro service d'orchestration. Tout mode de panne est un mode de
  panne Postgres, celui que toute l'équipe sait déjà diagnostiquer.
- **La robustesse vient de la base, pas de la discipline applicative.**
  L'isolation tenant est une politique RLS, donc une requête qui perd son `WHERE`
  ne renvoie rien. L'atomicité d'un upload est un `rename(2)`. La cohérence de la
  projection est une transaction. Rien de tout cela ne demande au relecteur de
  faire confiance au code.
- **Monter en charge ne demande pas de ré-architecturer d'abord.** Plus de
  réplicas d'API, plus de concurrence worker, puis une base plus grosse, puis des
  réplicas de lecture pour la liste. La file est déjà partitionnée par `org_id`,
  donc l'import de 10 000 documents d'un tenant ne peut pas affamer l'upload
  unique d'un autre.
- **Opérer en managé sans changer une ligne.** DBOS Cloud, ou un Conductor
  self-hosted, apporte l'UI de workflows, la rétention et l'observabilité par
  dessus la même bibliothèque et les mêmes tables. C'est une décision de
  déploiement, prise plus tard, à code constant.
- **Changer carrément de moteur est borné, par construction.** Le modèle de
  lecture exposé au tenant (`documents`, `document_steps`) est **le nôtre**,
  écrit par les wrappers de step, jamais relu depuis le schéma du moteur. Le
  contrat d'API, la projection, la politique de retry et les tests sont
  indépendants du moteur ; les steps sont des fonctions async ordinaires à
  frontières explicites. Porter vers Temporal ou Restate, c'est redécorer ces
  frontières et recâbler un adaptateur `PipelineRunner` — pas réécrire le
  pipeline. Les quatre déclencheurs qui le justifieraient sont dans
  [orchestration.md §4](orchestration.md), et aucun n'est un chiffre de volume.

## 3. Le reste de l'architecture, en renvois

Chaque domaine est défendu, avec ses mesures, dans le document qui lui est
consacré. Ce tableau est là pour y aller directement.

| Sujet | Où c'est argumenté |
|---|---|
| Tenant issu du token, les quatre niveaux, `app_rw` sans `BYPASSRLS`, preuves RLS par SQL non filtré | [architecture-upload.md §5](architecture-upload.md) |
| Access JWT contre refresh opaque, rotation et révocation de famille, oracle d'énumération fermé, les deux rôles Postgres | [authentification.md](authentification.md) |
| Octets avant la ligne, `storage_key` serveur, contrat `ObjectStore`, double plafond de taille, PDF décidé sur les octets | [architecture-upload.md §1–§4, §6](architecture-upload.md) |
| Page comme position, curseur `(created_at, id)` opaque et non signé, index, jointure uploader | [liste-documents.md §2–§4](liste-documents.md) |
| Projection à sens unique, quatre lignes de step dès la création, politique de retry mesurée, trigger `NOTIFY` et SSE | [pipeline.md](pipeline.md) |
| HMAC sur les octets bruts vérifié avant parsing, fenêtre de fraîcheur, idempotence du sink | [webhook-entrant.md](webhook-entrant.md) |
| Réponse du partenaire gardée et non jetée, `409` tant que ce n'est pas `ready`, ce qui n'est pas rendu | [donnees-extraites.md](donnees-extraites.md) |
| Clés de corrélation, niveaux de log, ce qui n'est jamais loggé | [observabilite.md](observabilite.md) |

Deux choix n'ont pas de document dédié et tiennent en deux points :

- **Les en-têtes de sécurité viennent d'une bibliothèque**, pas de chaînes
  écrites à la main : `SecurityHeadersMiddleware` applique le preset `STRICT` de
  [`secure`](https://github.com/TypeError/secure) à toutes les réponses. Seule la
  CSP est choisie localement, **indexée sur le `Content-Type` de la réponse et
  non sur une liste de chemins** — le JSON reçoit `default-src 'none'` puisqu'un
  corps JSON n'affiche rien, le HTML de `/docs` reçoit exactement les hôtes que
  chargent les pages FastAPI. `tests/api/test_security_headers.py` parse le vrai
  HTML des docs et asserte que chaque asset référencé est autorisé par la CSP
  servie, sinon une CSP stricte blanchit Swagger tout en renvoyant `200`.
- **Les ports sont des `Protocol`, pas des ABC**, et `app/domain` n'importe que
  la bibliothèque standard. Les adaptateurs les satisfont structurellement :
  l'infrastructure n'importe jamais le domaine pour en hériter, les tests
  écrivent leurs fakes sans framework de mock, et « changer d'object store » ou
  « changer d'orchestrateur » reste la modification d'un fichier.

## 4. Limites assumées

Chacune nomme la condition qui forcerait à la corriger : aucune n'est un
« un jour ».

**L1 — Les octets transitent par l'API au lieu d'aller directement au
stockage.** 2,8 uploads/s ≈ 45 Mbit/s en pointe n'est pas une charge
significative. *Déclencheur :* la taille moyenne des objets, pas le nombre de
documents — à 100 Mo de moyenne, le même débit fait 2,2 Gbit/s et on ajoute des
nœuds d'API pour pelleter des octets. `size_bytes` est enregistré à chaque
upload : le déclencheur est mesurable dès aujourd'hui
([architecture-upload.md §3](architecture-upload.md)).

**L2 — L'object store est un répertoire POSIX sur un seul nœud.** Un volume
compose ne survit pas à un second réplica d'API : deux nœuds, deux ensembles de
fichiers disjoints. *Déclencheur :* tout déploiement à plus d'un nœud d'API,
c'est-à-dire tout déploiement réel. D'où F1 en tête de liste.

**L3 — Chaque upload est écrit deux fois sur le disque local.** Starlette spoule
le corps multipart dans un fichier temporaire avant le handler, puis le store
l'écrit de nouveau. Y remédier suppose de parser le multipart incrémentalement
sur `request.stream()`, pour un gain aujourd'hui non mesurable.

**L4 — Un upload rejoué crée un second document.** Pas d'`Idempotency-Key`. Non
construit parce qu'aucun client ne rejoue automatiquement aujourd'hui.

**L5 — Un crash entre le commit du stockage et l'`INSERT` laisse un blob
orphelin.** Invisible pour tous les lecteurs, récupérable en diffant les clés
contre la table. Aucun sweeper n'existe encore.

**L6 — Un partenaire fantôme attend indéfiniment.** Si le webhook n'arrive
jamais, le document reste en `awaiting_partner` sans rien pour l'en sortir. La
requête de détection est `document_steps.ended_at` du step `external_call`,
au-delà d'un seuil, document toujours en `awaiting_partner`.

**L7 — Un `external_call` réexécuté produit un job en double.** Le partenaire
émet un second `job_id`, et le premier — jamais enregistré — répondra `404` pour
toujours quand il rappellera. Inhérent à tout appel sortant non idempotent ; la
correction est un job de réconciliation plus une clé d'idempotence sur la
requête partenaire, pas un autre orchestrateur.

Le crash entre l'acceptation par le partenaire et le commit du checkpoint est le
déclencheur évident, mais pas le plus fréquent : une `ConnectionError` après que
le partenaire a accepté est indiscernable d'une requête jamais arrivée, et le
step repart alors sur sa politique de retry ordinaire — sans crash. C'est le
chemin nominal, pas le cas dégradé.

**L8 — 1,6 % des documents sont abandonnés.** ~1 600/jour à la cible, sans deadletter table/queue ni endpoint de replay. Le checkpointing fait qu'un rejeu
*reprendrait* au lieu de recommencer ; rien ne l'expose encore.

**L9 — Chaque step en vol tient un thread OS**, parce que les mocks fournisseurs
appellent un `time.sleep()` bloquant. ~0,4 step concurrent aujourd'hui, ~390 à la
cible. Le sleep tient lieu d'E/S réseau : la correction est d'`await` de vrais
appels HTTP quand les fournisseurs seront réels, pas d'ajouter des threads.

**L10 — Les workers partagent le processus de l'API.** DBOS est lancé par un
middleware dans `create_app` : les threads du pipeline et le traitement des
requêtes se disputent un seul processus et ne se dimensionnent pas séparément.
`PIPELINE_QUEUE_POLLING_INTERVAL_SECONDS` (défaut 1,0s) coûte par ailleurs deux
fois par document, soit ~2s des ~63s de marge.

**L11 — Le flux SSE couvre l'état, pas les transitions manquées pendant une
coupure.** Chaque connexion s'ouvre sur un snapshot de l'état courant — c'est ce
qui rend la reconnexion sans rejeu et le plafond de 5 minutes par connexion sûr
plutôt que lossy — mais un client absent pendant un `running → succeeded →
running` ne voit que le point d'arrivée. Sans conséquence pour une UI de
progression qui affiche l'état courant ; faux pour tout ce qui a besoin du
journal des transitions. *Correction si nécessaire :* Redis Streams ou un curseur
`Last-Event-ID` sur une table d'événements retenus — deux stockages que ce design
n'a délibérément pas.

**L12 — L'`EventSource` du navigateur ne sait pas s'authentifier.** Il ne peut
pas envoyer d'en-tête `Authorization`, donc une vraie UI demande un ticket de
flux à courte durée de vie plutôt qu'un token en query string. Non construit :
il n'y a pas d'UI, et inventer l'endpoint maintenant serait spéculatif.

**L13 — L'auth est faite maison, et ce n'est pas une cible de production.**
Émission et validation de JWT, rotation des refresh et révocation de famille sont
implémentées ici pour que l'exercice soit exerçable de bout en bout. En
production, j'aurai délégué à un fournisseur d'identité managé, avec OIDC. Ce qui
manque nommément — révocation d'un access token avant son `exp`, limitation de
débit sur le login, purge des refresh, inscription et reset — est listé en fin de
[authentification.md](authentification.md).

**L14 — La liste n'a ni filtre, ni tri, ni pagination arrière, ni total.**
Chaque filtre interagit avec la clé de tri, donc avec le curseur, et un prédicat
sélectif comme `status` voudrait son propre index. C'est la pression qui plaide
pour F2 ([liste-documents.md §7](liste-documents.md)).

**L15 — Les documents uploadés ne sont pas téléchargeables.** `ObjectStore.get`
existe et est testé ; aucune route ne l'expose.

**L16 — Les tests d'intégration sont ignorés sans `TEST_POSTGRES_DSN`.** Un
`pytest` par défaut est vert alors que les preuves RLS — les tests qui comptent
le plus — n'ont jamais tourné.

**L17 — L'observabilité s'arrête aux logs.** Le JSON structuré avec corrélation
requête / tenant / document est en place ([observabilite.md](observabilite.md))
et répond à « qu'est-il arrivé à cet upload ». Ce qui manque est tout
l'agrégat : pas d'endpoint de métriques, pas de traces, pas de dashboards, pas
d'alerting. Chaque déclencheur de ce document — la bande passante d'upload de L1,
le p95 du pipeline contre 120s, le taux d'abandon de 1,6 %, la profondeur réelle
de pagination — est énoncé comme un seuil mesurable, et aucun n'est mesurable
aujourd'hui dans un système en fonctionnement. D'où F3.

**L18 — `ocr` et `chunks` sont des projections, pas des payloads.**
`GET /documents/{id}/data` rend `{"chars": n, "preview": …}` et `{"count": n}` ;
le texte OCR complet et la liste des chunks restent dans le checkpoint DBOS et ne
sont pas récupérables. Inliner le texte coûterait ~10 Go/jour d'amplification
d'écriture à la cible, pour une donnée qu'aucun écran n'affiche entièrement.
*Déclencheur :* le premier consommateur qui a besoin du texte lui-même — c'est
F6, qui en fait une clé d'object store plutôt qu'une colonne
([donnees-extraites.md](donnees-extraites.md)).

## 5. La suite, dans l'ordre

**F1 — Passer l'object store sur S3 (ou tout vrai stockage objet).** Le prochain
mouvement, et le moins cher. Le stockage objet est la réponse production pour des
fichiers dans un système distribué : il est fait pour exactement ce profil
d'opérations — beaucoup d'écrivains indépendants, durabilité et réplication comme
propriétés du service, règles de cycle de vie et de rétention, chiffrement côté
serveur, policy par préfixe — dont rien n'est fourni par un répertoire sur le
disque d'un nœud (L2).

Et c'est peu cher parce que rien ne bouge hors de l'adaptateur. `ObjectStore` est
déjà taillé sur ce que S3 garantit réellement : `put` possède
write-commit-abort, ce qui correspond à `UploadPart` /
`CompleteMultipartUpload` / `AbortMultipartUpload`, et « complet ou absent » est
exactement ce que cette API fournit. `storage_key` vaut déjà
`{org_id}/{document_id}`, donc le préfixe tenant dont ont besoin les règles de
cycle de vie et une clé par tenant est déjà là. Un fichier de plus dans
`app/infrastructure/storage/`, une ligne dans la racine de composition. Pas de
changement de modèle de données, pas de changement d'appelant, pas de changement
d'API.

**F2 — GraphQL pour la liste de documents.** C'est là que REST commence à coûter
plus qu'il ne rapporte, et la raison est la sélection de champs. Le découpage en
deux formes sur une même table — `DocumentSummary` ici, le détail par step
derrière `GET /documents/{id}` — a été subi, pas choisi : l'alternative était
quatre lignes de step par document listé, à chaque affichage de liste. Le seul
levier de REST quand les jeux de champs divergent, c'est un endpoint de plus,
chacun avec sa requête, son schéma et ses tests écrits à la main. La jointure
uploader montre la même couture un cran plus bas : elle est inconditionnelle,
donc un client qui ne veut que des noms de fichiers la paie quand même.

En GraphQL, le client nomme les champs dont il a besoin : la réponse s'étend ou
se réduit par consommateur, et la jointure ne tourne tout simplement pas pour
l'appelant qui n'a pas demandé `uploadedBy { fullName }`. La pagination arrive
avec une réponse spécifiée (la Relay connection spec) au lieu d'un codec de
curseur qu'on réécrirait à l'identique pour chaque future liste, et les filtres
typés (L14) deviennent un champ à ajouter plutôt qu'une branche dans un
constructeur de requêtes qui grossit.

La migration est peu chère, et c'est ça l'argument : `DocumentSummary` et
`DocumentPage` sont des types domaine sans framework dedans, et
`list_page(limit, after)` est déjà une requête de connexion à la Relay — `after`
*est* le `after` de Relay. Ce qui serait jeté, c'est le routeur et ses schémas de
réponse, une soixantaine de lignes. Ce qu'il faut construire à côté est ce que
REST donne gratuitement : autorisation par champ, limites de profondeur et de
coût de requête, et une stratégie de persisted queries — l'autorisation par champ
ratée dans un système multi-tenant est une fuite de données, pas une page lente.
*Déclencheur :* le deuxième consommateur avec un jeu de champs différent, ou la
première combinaison de filtres qui fait pousser un constructeur de requêtes
conditionnel ([liste-documents.md §6](liste-documents.md)).

**F3 — Métriques et traces : OpenTelemetry pour l'instrumentation, un APM
derrière** (L17). Les logs sont faits ; c'est la moitié manquante, et c'est
l'élément qui rend le reste de cette liste décidable, puisque chaque point y est
écrit comme un seuil et qu'aucun ne peut se déclencher aujourd'hui.

- **SDK OpenTelemetry, neutre vis-à-vis du fournisseur par construction.**
  Instrumenter une fois avec l'API OTel et exporter via le collector ; Prometheus,
  Datadog, Grafana Cloud ou un backend OTLP natif devient alors une configuration
  de collector plutôt qu'un changement de code. Engager *l'application* dans un
  SDK propriétaire est l'erreur à éviter — le même raisonnement qui met `argon2`
  et `PyJWT` derrière des ports ici.
- **Traces (Jaeger, ou tout backend OTLP).** Le span qui compte n'est pas la
  requête HTTP, c'est le document. Une trace enracinée sur l'upload, avec un span
  fils par step portant numéro de tentative et issue, et le webhook partenaire
  raccroché par `job_id`, répond à « où est le document X et pourquoi est-il
  lent » d'une seule vue. Aujourd'hui cette question demande de corréler des logs
  à la main — et c'est exactement la question qu'on pose à un pipeline en forme de
  document. Propager `traceparent` sur l'appel sortant l'étend au partemiddleware dans `create_app` : les threads du pipeline et le traitement des
requêtes se disputent un seul processus et ne se dimensionnent pas séparément.
`PIPELINE_QUEUE_POLLING_INTERVAL_SECONDS` (défaut 1,0s) coûte par ailleurs deux
fois par document, soit ~2s des ~63s de marge.naire.
- **Métriques, prêtes pour Prometheus** (`/metrics`, ou push OTLP) : p95 et p99
  du pipeline contre le budget de 120s ; durée, nombre de tentatives et taux
  d'échec par step ; taux d'abandon comme compteur de premier ordre (L8) ;
  profondeur de file et temps d'attente avant claim, là où passent réellement les
  ~63s de marge ; débit d'upload et distribution de `size_bytes`, soit exactement
  le déclencheur de L1/F4 ; latence et taux d'erreur par route ; saturation du
  pool de connexions.
- **Le travail sur les logs a déjà posé la couture.** La propagation de
  `request_id`, l'assainissement du `X-Request-Id` entrant et les clés
  `document_id` / `workflow_id` qui survivent au passage vers les workers
  détachés sont exactement ce qu'un contexte de trace remplace et prolonge :
  `trace_id` rejoint les champs existants plutôt qu'il ne les remplace.
- **Les points d'instrumentation sont déjà isolés**, et c'est ce qui rend
  l'ajout peu cher : FastAPI et SQLAlchemy ont de l'auto-instrumentation OTel, et
  les wrappers de step dans `app/pipeline/` sont le point de passage unique de
  toute transition — le même endroit où la projection est écrite et où les
  événements de log sont émis.
- **La cardinalité est la seule chose à ne pas rater.** `document_id` va sur un
  attribut de span, jamais sur un label de métrique. Les labels restent bornés :
  nom de step, issue, et — seulement si le nombre de tenants reste de l'ordre de
  la centaine — `org_id`.

**F4 — Uploads presigned et chunked** (L1, L3). Les octets vont client →
stockage et l'API ne fait plus que signer. Conditionné à F1, puisque présigner un
répertoire POSIX n'a pas de sens. Deux conséquences à nommer plutôt qu'à
découvrir : ça supprime la propriété « refuser avant d'écrire quoi que ce soit »,
puisque le contrôle des octets de tête PDF tourne aujourd'hui sur les 4 premiers
Ko *dans l'API* — ce contrôle se déplace vers une validation post-complétion ou
dans l'OCR, et la limite de taille devient une condition `content-length-range`
de policy. Et ça promeut les uploads abandonnés au rang de problème de premier
ordre — une URL émise jamais utilisée, un multipart démarré jamais terminé — ce
qui rend F5 obligatoire plutôt qu'optionnel.

**F5 — Jobs de réconciliation.** Un job, trois requêtes, toutes détectant des
états incapables de progresser seuls : un document `awaiting_partner` dont le
step `external_call` s'est terminé il y a plus de N minutes (L6, L7) ; des blobs
sans ligne (L5) ; et, une fois le presigned en place, les multipart incomplets —
que S3 expire d'ailleurs seul avec une règle de cycle de vie
`AbortIncompleteMultipartUpload`.

**F6 — Pousser le texte OCR dans l'object store** (L18). Le step doit renvoyer
une clé de stockage, pas des mégaoctets de texte. Ça retire de la projection *et*
du checkpoint le seul payload qui croît avec la taille du document, et c'est du
câblage plutôt que de l'infrastructure nouvelle : `GET /documents/{id}/data` rend
déjà une clé `ocr`, qui porterait alors une référence de stockage plutôt qu'un
aperçu.

**F7 — Table de dead-letter et endpoint de rejeu** (L8).

**F8 — `Idempotency-Key` à l'upload** (L4) : un en-tête et une contrainte
d'unicité, pas une déduplication côté client.

**F9 — Un endpoint de téléchargement** (L15). `ObjectStore.get` est déjà testé :
il reste une route et une réponse streamée.

**F10 — Déléguer l'authentification à un fournisseur d'identité managé** (L13),
et un ticket de flux à courte durée de vie pour que l'`EventSource` du navigateur
s'authentifie sans token en query string (L12). C'est la même décision — arrêter
de fabriquer des credentials à la main — et les deux attendent un vrai client.

**F11 — Séparer les workers du processus d'API** (L10), pour que les deux se
dimensionnent indépendamment et qu'une rafale de threads pipeline ne dégrade pas
la latence des requêtes.

**F12 — Un hook pre-commit lançant `ruff`, `mypy` et les tests**, et une
exécution d'intégration qui échoue au lieu d'être ignorée quand la base est
absente (L16). La CI elle-même est hors périmètre de l'exercice.

**F13 — Un test de charge qui produit le p95 au lieu de le simuler.**
