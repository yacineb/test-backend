# Récupérer les données extraites

L'énoncé demande quatre choses ; celle-ci est la dernière : « récupérer les
données extraites une fois le traitement terminé ». `GET /documents/{id}/data`
la sert, et n'est disponible qu'une fois le document `ready` — c'est-à-dire une
fois le pipeline passé **et** le webhook signé du partenaire reçu.

## Les données existaient déjà, sauf une

Chaque step écrit son résultat dans `document_steps.output` au moment où il
réussit ([pipeline.md](pipeline.md#modèle-de-données--deux-propriétaires-un-sens)).
L'endpoint est donc un remodelage de lignes déjà présentes : pas de nouvelle
requête, pas de nouveau chemin de lecture, pas de second modèle à garder
cohérent.

Une seule donnée manquait, et elle manquait pour une mauvaise raison : la
réponse du partenaire. Le webhook validait `result` sous la signature, puis le
jetait. Un champ qu'on authentifie et qu'on laisse tomber est le pire des deux
mondes — on paie la vérification et on ne garde pas le contenu.

```json
GET /documents/{id}/data
{
  "document_id": "…",
  "status": "ready",
  "ocr":      {"chars": 14, "preview": "lorem ipsum..."},
  "metadata": {"doc_type": "fake_type"},
  "chunks":   {"count": 3},
  "partner":  {"job_id": "j_9e91ccab60194616",
               "result": {"vault_ref": "RA-2026-00417", "indexed_at": "…"},
               "occurred_at": "…"}
}
```

Une clé par step, portant la sortie de ce step. Le client n'a pas à savoir
lesquels tournent en parallèle ni dans quel ordre : la forme de la réponse est
celle du DAG, pas celle de son exécution.

## Où vit la réponse du partenaire, et pourquoi pas ailleurs

`partner_result` (jsonb) et `partner_occurred_at` sont des colonnes de
`documents`, à côté du `partner_job_id` auquel elles répondent — migration
`0006`.

- **Pas dans l'`output` du step `external_call`.** Ce step s'est terminé quand
  le partenaire a accusé réception ; sa sortie est le `job_id`. Le résultat
  arrive des minutes ou des heures plus tard, par un autre chemin d'écriture,
  sur la session système. L'écrire dans une ligne de step déjà close mélangerait
  deux événements dans la même case.
- **Parce que c'est le résultat qui décide du `status`.** `ready` n'est
  atteignable que par cette notification, donc le statut et le payload sont la
  même information. Une seule `UPDATE` les écrit tous les trois : il n'existe
  aucune fenêtre où un document est `ready` avec sa réponse manquante, et
  l'idempotence du sink protège les trois colonnes d'un coup.
- **`jsonb` plutôt que des colonnes typées.** La forme appartient au
  partenaire. Le jour où il ajoute un champ n'est pas un jour où ce service doit
  migrer son schéma — c'est le même raisonnement que « les clés inconnues du
  corps sont ignorées » dans [webhook-entrant.md](webhook-entrant.md).
- **Gardé aussi pour une issue `failed`.** Le `result` est alors le seul récit
  de pourquoi le partenaire a refusé le document. Le jeter, c'est perdre
  l'explication au moment précis où on en a besoin.

`complete()` et `fail()` ont fusionné en un `record_outcome()` : elles ne
différaient plus que par le statut écrit, et la branche du sink est partie avec
elles.

## `409` avant que ce soit prêt

Un document en cours de traitement existe, l'appelant a le droit de le voir, et
ses données arrivent. Les trois réponses possibles n'ont pas la même valeur :

| Réponse | Pourquoi pas |
|---|---|
| `404` | Mentirait sur l'existence, et se confondrait avec le `404` qui protège la frontière tenant |
| `200` avec des `null` | Rend « pas encore » indistinguable de « ce step n'a rien produit » ; chaque client réinvente alors le test |
| `409` avec le statut | L'appelant apprend **ce qu'il attend** |

Le corps nomme l'état courant — `no extracted data yet: the document is
processing` — donc un client qui se trompe d'instant sait s'il doit repasser ou
renoncer. Pour savoir quand revenir, il y a déjà `GET /documents/{id}` et le
flux SSE ; cet endpoint ne duplique pas le suivi de progression.

Un document `failed` est également en `409` : terminal, mais il n'y a rien à
rendre, et c'est l'endpoint de statut qui porte `failed_step` et `last_error`.

## Ce qui n'est pas rendu, et pourquoi

`ocr` et `chunks` sont des **projections, pas des payloads** : `{"chars": n,
"preview": …}` et `{"count": n}`. Le texte OCR complet et la liste des chunks
restent dans le checkpoint DBOS, que nous ne lisons jamais — c'est le contrat
interne du moteur, pas le nôtre.

Ce n'est pas un oubli : inliner le texte coûterait ~10 Go/jour d'amplification
d'écriture à la cible 12 mois, pour une donnée qu'aucun écran n'affiche
entièrement. Sa place est l'object store, le step renvoyant une clé plutôt que
des mégaoctets — c'est **F6**, et la limite est **L18** dans
[decisions-et-limites.md](decisions-et-limites.md#4-limites-assumées).
L'endpoint est déjà la bonne forme pour ça : le jour où F6 est fait, `ocr` gagne
une clé ou une URL signée, et rien d'autre ne bouge.

## Compatibilité

Les deux colonnes sont nullables et personne ne les remplit rétroactivement. Un
document devenu `ready` avant la migration `0006` répond donc `200` avec
`"partner": null` plutôt qu'une erreur : la réponse du partenaire est absente,
ce qui est exactement le cas — elle n'a jamais été gardée. Vérifié sur une base
migrée en place, pas seulement sur une base neuve.

## Tests

- `tests/unit/test_extracted_data.py` — la projection : une clé par step, le
  bloc partenaire, et un partenaire qui n'a envoyé aucun `result`.
- `tests/api/test_document_routes.py` — les codes : `200` une fois `ready`,
  `409` pour chaque état antérieur et pour `failed`, `404` inconnu, `401` sans
  token.
- `tests/integration/test_partner_sink.py` — la persistance contre une vraie
  base : le payload est gardé pour une réussite comme pour un échec, et une
  reprise du partenaire n'écrase pas ce qui a été enregistré.
