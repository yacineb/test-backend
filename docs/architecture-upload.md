# Upload : architecture et justification

Comment un fichier passe du client au stockage durable avec une ligne en base, et
pourquoi chaque décision a été prise. L'exécution du pipeline est dans
[pipeline.md](pipeline.md), la lecture dans
[liste-documents.md](liste-documents.md), et les limites que ce design assume
sont rassemblées dans [decisions-et-limites.md](decisions-et-limites.md).

## 1. Le modèle de données

Le design, c'est la table. Tout le reste en découle.

```sql
CREATE TABLE documents (
    id            uuid          PRIMARY KEY,
    org_id        uuid          NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    uploaded_by   uuid          NOT NULL REFERENCES users(id),
    filename      varchar(255)  NOT NULL,
    content_type  varchar(255)  NOT NULL,
    size_bytes    bigint        NOT NULL,
    sha256        varchar(64)   NOT NULL,
    storage_key   varchar(512)  NOT NULL UNIQUE,
    status        varchar(32)   NOT NULL,
    created_at    timestamptz   NOT NULL DEFAULT now()
);

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE  ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON documents
    FOR ALL
    USING      (org_id = (NULLIF(current_setting('app.current_org_id', true), ''))::uuid)
    WITH CHECK (org_id = (NULLIF(current_setting('app.current_org_id', true), ''))::uuid);
```

`uploaded_by` n'a délibérément aucune action `ON DELETE`, contrairement à
`org_id`. Supprimer une organisation doit emporter ses documents ; supprimer un
*utilisateur* ne doit pas détruire des enregistrements que son organisation
possède encore. Il n'existe pas encore de chemin de suppression d'utilisateur, et
l'action manquante force à répondre explicitement à cette question le jour où il
apparaîtra, plutôt qu'à la voir tranchée silencieusement par une cascade.

Trois propriétés portent le reste.

**`storage_key` est générée côté serveur : `{org_id}/{document_id}`.** Aucune
partie ne vient du client. `filename` est stocké comme *donnée* — renvoyé dans
les réponses, affiché aux utilisateurs, jamais utilisé pour construire un chemin.
Ce n'est pas de la défense en profondeur posée sur de la validation : ça la
remplace. Aucun `../` à rejeter, aucun octet nul à retirer, aucune attaque de
normalisation Unicode, aucune collision de système de fichiers insensible à la
casse, parce qu'aucune chaîne contrôlée par l'utilisateur n'atteint un chemin. Un
assainisseur peut être oublié sur un nouvel appelant ; un format de clé sans
endroit où mettre l'entrée d'un attaquant ne peut pas l'être. Le préfixe `org_id`
est aussi la frontière naturelle pour les règles de cycle de vie S3, les policies
de bucket, les métriques par tenant et une clé KMS par tenant — se tromper plus
tard est une migration de données, viser juste aujourd'hui ne coûte rien.

**Les octets sont commités avant l'insertion de la ligne.** Générer un UUID,
streamer les octets vers le stockage, et seulement une fois l'objet durable,
`INSERT`. L'alternative courante — insérer `status='uploading'` puis basculer —
fait de « une ligne existe » et « l'objet existe » deux faits distincts, donc
chaque lecteur du système, pour toujours, doit savoir quelles lignes sont
réelles. Avec les octets d'abord, il y a un invariant total : **toute ligne de
`documents` référence un objet complet et durable.** Le coût est qu'un crash
entre le commit du stockage et l'`INSERT` laisse un objet orphelin sans ligne.
Les orphelins sont une facture de stockage, ils sont invisibles pour tous les
lecteurs, et un sweeper les récupère ; le mode de panne de l'alternative est un
chemin de lecture corrompu.

**`status` est une colonne, pas une décision de schéma repoussée.** Elle ne
contenait que `uploaded` à sa création, mais les états du pipeline atterrissent
dans cette colonne juste après, et l'ajouter plus tard aurait signifié une
migration sur une table déjà pleine de lignes.

## 2. L'abstraction de stockage

```python
class ObjectStore(Protocol):
    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> int: ...
    def get(self, key: str) -> AsyncIterator[bytes]: ...
    async def delete(self, key: str) -> None: ...
```

Trois méthodes, aucun bouton de configuration, aucune politique. C'est petit
parce que l'intersection utile entre « un répertoire POSIX » et « un object store
distribué » est petite, et prétendre le contraire est la façon dont les
abstractions deviennent des mensonges. Elle porte exactement une garantie, que
les deux backends implémentent nativement au lieu de l'émuler :

> **Une clé existe complète, ou n'existe pas. Il n'y a jamais d'objet partiel,
> sous aucune panne.**

| | POSIX | S3 / stockage objet |
|---|---|---|
| écriture | `write()` vers `{key}.tmp` | `UploadPart` par chunk |
| commit | `fsync` puis `rename` atomique | `CompleteMultipartUpload` |
| abandon | `unlink` du fichier temporaire | `AbortMultipartUpload` |

C'est pourquoi `put` possède tout le cycle write-commit-abort au lieu d'exposer un
handle de fichier. Une interface à handle (`open() -> writer`) reporterait
l'ordre du commit sur chaque appelant, et n'a aucune implémentation S3 correcte,
puisque S3 n'a pas de handle à donner. Conséquence : un upload crashé est
*invisible* plutôt que simplement bien rangé, et rien en aval n'a besoin de
distinguer un objet complet d'un objet tronqué.

Le backend POSIX n'est pas un jouet — même contrat, `write` → `fsync` → `rename`,
avec les E/S bloquantes déportées via `anyio.to_thread`. Ce dernier point compte
plus qu'il n'y paraît : appeler `open()`/`write()` directement dans un handler
async bloque toute la boucle d'événements du worker pendant l'écriture disque, ce
qui sérialise toutes les autres requêtes de ce worker, health checks compris. Ça
paraîtrait parfait en développement et s'écroulerait sous charge.

**Ce que l'interface n'a délibérément pas :** pas d'`exists()`, pas de `list()`,
pas de `copy()`, pas de dictionnaire de métadonnées, pas de `presigned_url()` —
aucun n'est nécessaire à un appelant actuel. Le dernier est le tentant, puisque
le §3 dit que les uploads presigned sont la direction long terme, mais une
méthode avec une implémentation, aucun appelant et aucun test n'est pas de la
préparation : c'est une hypothèse non vérifiée qui sera fausse sur un détail que
seul le second backend révélera.

## 3. Streamer à travers l'API, et quand arrêter

Les octets vont aujourd'hui client → API → stockage. L'alternative, ce sont les
URL présignées : l'API rend une URL signée, le client `PUT` directement vers S3,
et l'API ne touche jamais le payload. C'est le design qui passe à l'échelle, et
c'est la direction. La seule question est de savoir s'il se justifie maintenant.

**L'arithmétique.** À la cible 12 mois, la pointe est à **2,8 uploads/s** ; à
2 Mo de PDF moyen, cela fait **5,6 Mo/s ≈ 45 Mbit/s**, avec environ **9 uploads
en vol** à tout instant, chacun coûtant une tâche async et un fichier temporaire.
Ce n'est pas une charge significative pour un seul processus d'API, encore moins
pour un tier scalé horizontalement. Le design presigned supprimerait une charge
qui n'existe pas.

**Le déclencheur qui inverse la décision** n'est pas le nombre de documents mais
la *taille moyenne des objets*, et la relation est linéaire. Avec le plafond de
100 Mo comme moyenne — un corpus de PDF scannés, riches en images — les mêmes 2,8
uploads/s deviennent **280 Mo/s ≈ 2,2 Gbit/s soutenus**, et le tier d'API est
alors dimensionné uniquement pour pelleter des octets :

> **Passer aux uploads presigned quand la bande passante soutenue à travers le
> tier d'API approche du point où l'on ajoute des nœuds pour la bande passante
> plutôt que pour la concurrence de requêtes.** `size_bytes` est enregistré à
> chaque upload depuis le premier jour : la donnée pour trancher existe déjà.

**La migration sera peu chère le moment venu**, parce que le flux presigned change
où voyagent les octets, pas le modèle de données : `storage_key` est déjà générée
côté serveur, donc c'est déjà ce qui serait signé ; « octets avant la ligne »
correspond déjà au cycle de vie presigned ; le contrat `ObjectStore` est
exactement ce que fournit `CompleteMultipartUpload` ; et le plafond de 100 Mo
devient une condition `content-length-range` de policy S3, soit un point
d'application plus strict, pas plus faible. Ce qui s'ajoute réellement, c'est le
problème des uploads abandonnés — et c'est une raison de ne pas le prendre avant
que la charge ne le justifie.

**Le coût connu de la solution actuelle :** avec `UploadFile`, Starlette spoule le
corps de la requête dans un fichier temporaire local avant l'exécution du
handler, donc sur le backend POSIX chaque upload est écrit deux fois sur le
disque local. C'est une vraie inefficacité, ce n'est pas le goulot aux tailles
ci-dessus, et l'alternative — parser le multipart incrémentalement sur
`request.stream()` — c'est du multipart écrit à la main pour un gain non
mesurable.

## 4. La limite de 100 Mo est appliquée deux fois

Ce n'est pas de la redondance. Les deux contrôles gardent des choses différentes
et aucun n'absorbe l'autre.

**Niveau 1 — middleware ASGI, ~101 Mo, corps entier.** Refuse sur
`Content-Length` avant de lire un octet ; quand cet en-tête est absent ou ment —
le transfer-encoding chunked le rend trivialement falsifiable — il enveloppe
`receive` et interrompt dès que le compteur dépasse la limite. Ce niveau existe
parce que **`UploadFile` spoule avant l'exécution du handler** : une limite
implémentée seulement dans le code du handler n'est pas une limite, puisqu'un
client streamant 10 Go les aurait tous écrits dans le répertoire temporaire du
serveur avant la première ligne de code applicatif. Le ~1 Mo de marge couvre les
frontières multipart et les en-têtes de parties.

**Niveau 2 — une passe sur les chunks, exactement 100 Mo, octets du fichier
seulement.** La limite produit documentée, avec un `413` propre et actionnable
par le client. La même passe unique compte les octets et calcule le SHA-256 :
l'intégrité ne coûte aucun parcours supplémentaire.

Mesuré contre `docker compose up`, à la limite de production :

| requête | résultat | temps |
|---|---|---|
| exactement 104 857 600 octets | `201` | 0,25 s |
| 104 857 601 octets (limite + 1) | `413`, niveau cas d'usage | 0,13 s |
| 110 000 000 octets | `413`, niveau middleware | **0,0015 s** |

La dernière ligne est tout l'intérêt du découpage : le middleware répond en moins
de deux millisecondes parce qu'il refuse sur `Content-Length` sans jamais lire le
corps, et la ligne du milieu prend ~85× plus longtemps précisément parce que le
corps *a été* bufferisé avant que le contrôle exact par fichier ne puisse
tourner. Un design à un seul niveau obtient l'un des deux comportements, jamais
les deux. Après les trois requêtes, le volume de stockage ne contenait que
l'upload réussi : aucun fichier partiel, aucun résidu `.tmp`.

Le niveau 1 est écrit à la main, et c'est une réponse assumée à « utilise une
bibliothèque » : il n'y en a pas. Aucun paquet PyPI maintenu n'implémente un
plafond de corps ASGI, uvicorn 0.51 n'expose aucune limite de taille de corps, et
Starlette ne fournit que la constante de statut `413`. En production, cette
responsabilité appartient au reverse proxy — `client_max_body_size` de nginx — et
le middleware est la garantie qui tient quand l'application tourne sans. La
limite est une configuration (`MAX_UPLOAD_BYTES`), pas une constante, et c'est ce
qui rend la frontière réellement testable : les tests la fixent petite et
assertent que la limite *exacte* passe et que limite + 1 échoue.

## 5. D'où vient le tenant

**Ni `org_id` ni `uploaded_by` n'est un paramètre de l'API d'upload.** Aucun
champ, en-tête ou query string ne peut influencer l'un ou l'autre. Les deux sont
lus dans le token d'accès vérifié :

```python
async def upload(ctx: CurrentUser, deps: UploadDepsDep, file: UploadFile) -> ...:
```

Une requête sans token n'atteint jamais le handler ; une requête avec token
l'atteint déjà scopée. « Uploader chez un autre tenant » n'est pas une requête
qu'on peut *refuser* — c'est une requête qu'on ne peut pas *exprimer*.

Cette source unique se propage ensuite à quatre niveaux indépendants, chacun
capable à lui seul de bloquer une écriture inter-tenant :

1. **Le cas d'usage** dérive `storage_key = {org_id}/{document_id}` de `ctx`.
2. **Le repository** est construit scopé et lève sur un `Document` dont l'`org_id`
   diffère — cet écart est un bug de câblage, pas une erreur client.
3. **La session** est scopée RLS : `app.current_org_id` est posé par transaction
   et l'application se connecte en `app_rw`, qui n'a *pas* `BYPASSRLS`.
4. **Postgres** applique `org_isolation` en `USING` et `WITH CHECK`, donc une
   requête qui perd son `WHERE` ne renvoie rien et un insert visant ailleurs est
   refusé par la base.

Les niveaux 1–2 sont du code applicatif et un bug peut les mettre en défaut. Les
niveaux 3–4 sont la base et tiennent même si le code a tort — d'où
`tests/integration/test_document_isolation.py` qui asserte via du SQL
volontairement *non filtré*. Si ces tests passent, c'est que Postgres fait un
travail indépendant, et non que le `WHERE` fait tout le travail.

## 6. Uniquement des PDF, décidé sur les octets

Le corpus est en PDF, donc `application/pdf` est le seul type accepté, et le
contrôle porte sur les octets de tête du fichier plutôt que sur le
`Content-Type` envoyé par le client. Cette distinction est le fond du sujet : un
en-tête de requête est une affirmation de la partie qu'on cherche à valider, donc
le traiter comme une preuve rend le contrôle contournable en éditant une chaîne.
Renommer `payload.png` en `report.pdf` et déclarer `application/pdf` donne un
`415`, parce que les huit premiers octets disent toujours PNG.

Le cas d'usage ne prend donc **aucun argument `content_type`**, et le routeur ne
transmet pas `file.content_type` : il n'existe aucun chemin par lequel
l'affirmation du client atteindrait la décision ou la ligne stockée. Un vrai PDF
uploadé en `application/octet-stream` est accepté et stocké en `application/pdf`.

Les 4 premiers Ko sont tirés du flux, contrôlés, puis rejoués devant le reste,
donc la décision tombe avant l'appel à `ObjectStore.put` : un upload refusé
n'ouvre jamais de fichier temporaire. Ce coup d'œil absorbe aussi le cas du
fichier vide — un flux sans tête est vide, et on le sait avant toute écriture,
raison pour laquelle le contrôle `size == 0` post-écriture a disparu au lieu
d'être simplement inutile.

`puremagic`, pas `python-magic` : le second se lie au `libmagic` du système, et
l'image de runtime est distroless — pas de shell, pas de gestionnaire de paquets
— donc cette bibliothèque devrait être embarquée à la main et maintenue en phase
avec l'image de base. Le détecteur est derrière un port `ContentTypeDetector`,
comme toute autre dépendance tierce ici (`argon2` derrière `PasswordHasher`,
`PyJWT` derrière `TokenService`).

Ce que ce contrôle n'est *pas* : un parseur. Il vérifie que le fichier *commence*
comme un PDF, et un en-tête PDF collé devant des octets arbitraires passe. La
vraie validation appartient à l'étape OCR, qui doit de toute façon parser le
fichier ; celle-ci existe pour rejeter les incohérences évidentes à peu de frais,
à la frontière.

## 7. Ce qui est vérifié

Les affirmations ci-dessus qui sont testées plutôt qu'assénées :

- **Atomicité du stockage** — une exception en cours de flux ne laisse ni la clé
  ni un fichier `.tmp`.
- **Plafond, niveau 1** — un `Content-Length` surdimensionné est rejeté sans lire
  le corps ; un corps chunked qui sous-déclare sa taille est quand même
  interrompu.
- **Plafond, niveau 2** — exactement la limite passe, limite + 1 échoue.
- **Intégrité** — les octets relus depuis le stockage ont bien le SHA-256
  enregistré.
- **Hygiène de clé** — un nom de fichier hostile (`../../etc/passwd`) apparaît
  dans la colonne `filename` et nulle part dans `storage_key`.
- **Atomicité en échec** — quand l'insert échoue, l'objet qu'il aurait référencé
  est supprimé.
- **Le type vient des octets** — un PNG déclaré PDF donne `415` ; un PDF déclaré
  `octet-stream` est stocké en `application/pdf`.
- **L'auth est obligatoire** — upload et liste répondent `401` sans token, et les
  deux portent `security` dans le schéma OpenAPI.
- **Le tenant ne vient que du token** — glisser un `org_id` en champ de
  formulaire supplémentaire ne change rien.
- **RLS couvre `documents`** (vrai Postgres) — un `SELECT` non filtré sur une
  session scopée à une organisation ne voit pas les lignes d'une autre, un
  `INSERT` visant ailleurs est refusé par `WITH CHECK`, et une connexion qui n'a
  jamais posé `app.current_org_id` voit zéro ligne plutôt que toutes.

Vérifié de bout en bout contre la stack en fonctionnement : le token d'Alice
porte `org: 1ae48790-…`, et l'objet écrit pour son upload a atterri sur
`1ae48790-…/9a5a4311-…` — le préfixe de stockage est la revendication `org` du
token, pas quelque chose fourni par la requête.
