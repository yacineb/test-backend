# Lister les documents

Le côté lecture de [architecture-upload.md](architecture-upload.md) : ce qu'est
une ligne de liste, pourquoi la pagination est par curseur, et ce que ça coûte.
Tous les nombres ont été mesurés contre Postgres 17 avec 2 000 031 documents,
dont 500 000 dans l'organisation appelante.

## 1. Ce qu'est une ligne de liste

```python
@dataclass(frozen=True, slots=True)
class DocumentSummary:
    id: UUID
    filename: str
    status: DocumentStatus
    created_at: datetime
    uploader_id: UUID
    uploader_name: str
    uploader_email: str
```

Délibérément **pas** un `Document` : `sha256`, `storage_key`, `content_type` et
`size_bytes` sont l'enregistrement d'upload, et rien de tout ça n'est sur cet
écran. Deux formes, c'est sept colonnes lues au lieu de onze, et un champ ajouté
à l'upload qui n'élargit pas silencieusement chaque réponse de liste.
`uploader_name` et `uploader_email` viennent d'une jointure vers `users` (§4) :
renvoyer un UUID brut pousserait chaque client vers un N+1 pour afficher un nom.

**`status` est la colonne du pipeline** — `uploaded`, `processing`,
`awaiting_partner`, `ready`, `failed`. La liste lit cette colonne et rien d'autre
du pipeline, donc **cet endpoint n'a pas changé quand le pipeline est arrivé et
ne changera pas quand un état sera ajouté**. Il est sérialisé en chaîne plutôt
qu'en enum OpenAPI, pour qu'un nouvel état ne transforme pas les anciens clients
en erreurs de validation.

Les quatre lignes de step, avec tentatives et erreurs, ne sont volontairement pas
là : c'est l'affaire de `GET /documents/{id}`, et les charger par ligne listée est
exactement le N+1 que cette requête existe pour éviter.

## 2. Une page est une position, pas un compte

`OFFSET n` répond à « saute n lignes » en *produisant* n lignes puis en les
jetant : le coût est linéaire en profondeur, payé à chaque requête. Le seek
demande les lignes *après une position*, et la position est la clé de tri.

```sql
SELECT … FROM documents d JOIN users u ON u.id = d.uploaded_by
WHERE  d.org_id = :org AND u.org_id = :org
  AND  (d.created_at, d.id) < (:cursor_created_at, :cursor_id)
ORDER BY d.created_at DESC, d.id DESC
LIMIT :limit + 1;
```

Mesuré, `LIMIT 50`, 500 000 documents dans l'organisation :

| profondeur | `OFFSET` | keyset |
|---|---|---|
| page 1 | 4,0 ms | 0,49 ms |
| 1 000 | 0,65 ms | 1,3 ms |
| 10 000 | 129 ms | 0,74 ms |
| 100 000 | 168 ms | 0,33 ms |
| 400 000 | 197 ms | 0,36 ms |

Deux choses à dire franchement plutôt qu'à glisser sous le tapis.

**À la page 20, l'offset est plus rapide** — 0,65 ms contre 1,3 ms. Quiconque
prétend que le keyset est universellement plus rapide vend quelque chose : à
faible profondeur les deux plans sont un parcours d'index, et celui de l'offset a
un prédicat plus simple.

**La dégradation est une falaise, pas une pente.** Entre 1 000 et 10 000 lignes,
le planner abandonne l'index et bascule sur un scan séquentiel parallèle avec tri
sur disque — 19 Mo de fichiers temporaires écrits, par requête, pour renvoyer
cinquante lignes (272 ms sur le plan complet). Le plan keyset à la même position
est un `Index Scan Backward` qui lit 51 lignes en 0,137 ms — et ce 51 est le même
page 1 ou page 8 000. C'est la propriété réellement achetée : **le coût d'une page
ne dépend pas du nombre de pages qui la précèdent.**

**La raison de correction compte plus que la vitesse.** Les pages en offset sont
définies contre une liste qui bouge : insérez un document en tête pendant qu'un
client est page 1 et chaque `OFFSET` suivant décale d'un — la dernière ligne de
la page 1 réapparaît en tête de page 2, et une ligne est purement sautée. Sur une
liste triée du plus récent au plus ancien, dans un système dont la raison d'être
est d'ingérer des documents, l'insertion en tête est le régime permanent.
`test_paging_is_stable_when_a_document_is_added_mid_scroll` est ce scénario
contre un vrai Postgres.

`offset` a été **remplacé, pas complété** : il n'est plus accepté. L'endpoint
n'avait aucun consommateur externe, donc rien à protéger ; accepter les deux
voudrait dire deux ordonnancements à garder cohérents et une invitation
permanente à utiliser celui qui tombe de la falaise page 200.

## 3. Le curseur

- **`(created_at, id)`, pas `created_at`.** Un timestamp n'est pas unique —
  trivialement dans un import en masse. Un curseur incapable de départager une
  égalité soit répète des lignes (`<=`), soit en saute (`<`), silencieusement dans
  les deux cas. La clé primaire rend la position totale, et
  `test_paging_over_a_shared_timestamp_loses_nothing` force une frontière de page
  à l'intérieur d'une égalité de neuf documents.
- **Comparaison de row-value, pas de disjonction.** `(a, b) < (x, y)` devient une
  condition d'index unique ; `created_at < :ts OR (created_at = :ts AND id < :id)`
  fait deux branches, se trompe facilement d'un `<=`, et ne se résout pas en un
  seul parcours.
- **Opaque sur le fil** — base64url de `{created_at ISO 8601}|{uuid}`. Un curseur
  lisible devient une API publique : les clients en fabriqueraient, et la clé de
  tri ne pourrait plus changer. Or elle changera (trier par statut ou par
  uploader), et l'opacité rend ce changement additif.
- **Non signé, parce qu'il ne porte aucune autorité.** L'organisation vient du
  token et est réappliquée par RLS : un curseur forgé permet au mieux de démarrer
  sa *propre* liste où l'on veut. Vérifié — Bob rejouant le curseur d'Alice
  obtient une page vide.
- **La précision est structurante.** `created_at` est un `timestamptz` et
  l'aller-retour est exact à la microseconde. Tronquer à la seconde n'échouerait
  pas bruyamment : ça sauterait ou répéterait des lignes à chaque frontière de
  page. Un timestamp naïf est rejeté en `400` au décodage plutôt que de refaire
  surface en erreur de driver.

L'index suit : la migration `0004` remplace `(org_id, created_at)` par
`(org_id, created_at, id)`. Sans `id`, la comparaison de row-value retombe en
filtre sur le tas et parcourt toute l'égalité. Mesuré avec un curseur à 25 000
lignes dans un bloc de 50 000 partageant un timestamp :

| index | lignes parcourues | buffers | temps |
|---|---|---|---|
| `(org_id, created_at, id)` | 51 | 55 | **0,177 ms** |
| `(org_id, created_at)` | 24 999 | 2 024 | **18,9 ms** |

107× sur le temps pour une colonne de plus sur un index qui existait déjà. La
migration crée le nouvel index avant de supprimer l'ancien.

## 4. La jointure uploader

Une jointure **interne**, et c'est une décision. Elle est sûre parce que
`uploaded_by` est `NOT NULL` avec clé étrangère vers `users`, écrit uniquement
depuis le token qui fournit aussi `org_id`, et que cette clé étrangère n'a
délibérément aucune action `ON DELETE`. Une jointure externe rendrait les deux
champs uploader nullables à travers toutes les couches pour modéliser un état qui
ne peut pas se produire.

Le prédicat `u.org_id = :org` est redondant — RLS couvre `users` et l'invariant
ci-dessus tient — mais il est là pour la même raison que les repositories filtrent
sur `org_id` : une base mal configurée ne doit pas devenir une lecture
inter-tenant silencieuse. Il ne coûte rien, `users` étant petite et matérialisée
une fois (`Buffers: shared hit=1`).

## 5. À terme, ça devrait être du GraphQL

C'est ici que REST commence à coûter plus qu'il ne rapporte, et la raison est la
sélection de champs. **Le découpage en deux formes a été subi, pas choisi** :
l'alternative était quatre lignes de step par document listé. Le seul levier de
REST quand les jeux de champs divergent est un endpoint de plus, chacun avec sa
requête, son schéma et ses tests. La jointure uploader montre la même couture un
cran plus bas : inconditionnelle, donc payée même par un client qui ne veut que
des noms de fichiers.

La pression augmente — tags et résultats d'extraction sont les prochains champs
évidents, et le filtrage (« encore en traitement », « importés par Alice ») ferait
pousser un constructeur de requêtes conditionnel, chaque filtre interagissant avec
la clé de tri donc avec le curseur.

**Pourquoi c'est quand même du REST aujourd'hui :** un consommateur, une liste, un
tri. GraphQL maintenant, ce serait un schéma, une couche d'exécution, de
l'autorisation par resolver, des limites de profondeur et de coût, et des
persisted queries — avant qu'un second consommateur ne le justifie. Et
l'autorisation par champ ratée en multi-tenant est une fuite de données, pas une
page lente.

La migration restera peu chère parce que l'essentiel est déjà indépendant du
transport : `DocumentSummary` et `DocumentPage` sont des types domaine sans
framework, `list_page(limit, after)` est déjà une requête de connexion à la Relay
où `after` *est* le `after` de Relay, et la clé keyset comme la sonde `limit + 1`
vivent dans le repository. Ce qui serait jeté, c'est le routeur et ses schémas,
une soixantaine de lignes. **L'endpoint est jetable, le modèle de données ne
l'est pas.** Le dossier complet est en F2 dans
[decisions-et-limites.md](decisions-et-limites.md).

## 6. Volontairement non construit

- **Filtres** (statut, uploader, plage de dates) : chaque filtre change aussi
  l'index qui sert la requête — un prédicat `status` sur
  `(org_id, created_at, id)` est un filtre, pas un seek — donc les ajouter sans
  mesurer serait deviner.
- **Autres tris** : une autre clé de curseur et un autre index. L'opacité du
  curseur rend l'ajout additif.
- **Le total** : `count(*)` sur l'organisation est exactement le scan que ce
  design évite. La réponse serait `pg_class.reltuples` ou un compteur maintenu.
- **Pagination arrière** : le prédicat miroir plus un `prev_cursor`. Rien ne le
  demande.

## 7. Ce qui est vérifié

- Aller-retour du curseur exact à la microseconde ; une microseconde d'écart
  donne un curseur différent.
- Curseurs invalides en `400`, jamais `500` — non-base64, timestamp illisible, id
  non-UUID, séparateur manquant, payload non-UTF-8, timestamp naïf.
- Curseur opaque : ni l'id ni l'année n'apparaissent, et aucun encodage d'URL
  n'est nécessaire.
- La pagination visite chaque document exactement une fois, contre le fake et
  contre un vrai Postgres ; les égalités ne perdent rien ; une insertion en cours
  de scroll ne décale pas la page.
- Une dernière page pleine ne renvoie pas de curseur suivant, et un curseur
  au-delà de la fin donne une page vide plutôt qu'une erreur.
- L'uploader vient de la jointure, et la liste est scopée par RLS à
  l'organisation du token.
