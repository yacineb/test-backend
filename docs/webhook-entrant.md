# Webhook partenaire entrant

`external_call` renvoie un `job_id` opaque ; le vrai résultat arrive plus tard en
`POST /webhooks/partner`. Tant que cette notification n'est pas reçue *et
vérifiée*, le document n'est pas `ready`. Sa place dans le pipeline est décrite
dans [pipeline.md](pipeline.md).

## La donnée d'abord

Une seule valeur traverse la frontière ; tout le reste est de la machinerie
autour :

```python
@dataclass(frozen=True, slots=True)
class PartnerNotification:
    job_id: str                  # opaque, émis par le partenaire
    status: PartnerJobStatus     # completed | failed
    result: dict[str, Any] | None
    occurred_at: datetime        # heure côté partenaire, dans la signature
```

`job_id` est la seule clé de jointure. Le partenaire ne sait rien de nos
organisations, de nos utilisateurs ni de nos ids de document, et c'est le point :
le webhook ne porte aucune identité de tenant, donc la tenancy est résolue *par
nous*, en cherchant quel document attend ce `job_id`. On ne peut pas faire
toucher à un webhook une organisation vers laquelle il n'était pas déjà dirigé,
puisque l'appelant n'en nomme aucune.

## Authenticité : HMAC sur les octets bruts

`X-Partner-Signature` vaut `HMAC-SHA256(raw_body, PARTNER_HMAC_SECRET)`, en hexa.
Deux propriétés dont dépend l'implémentation :

- **Ce sont les octets bruts qui sont signés, pas le modèle parsé.** Un
  `json.dumps` d'un corps parsé diffère de ce que le partenaire a envoyé par les
  espaces et l'ordre des clés : re-sérialiser avant de vérifier échouerait sur des
  requêtes valides et, pire, inviterait quelqu'un à « corriger » en relâchant le
  contrôle. `HmacSha256Signer` prend des `bytes` et rien d'autre.
- **La vérification a lieu avant le parsing.** C'est une dépendance FastAPI, pas
  les quatre premières lignes du handler : FastAPI résout les dépendances avant de
  valider le corps, donc une requête non signée est un `401` et le parser ne voit
  jamais le payload. Épinglé par `test_unsigned_and_malformed_is_401_not_422`.

La comparaison est un `hmac.compare_digest` : une mauvaise signature coûte le
même temps qu'une bonne.

## Fraîcheur : le HMAC n'empêche pas le rejeu

Une requête valide capturée reste valide pour toujours — la signature dit
*authentique*, pas *récent*. `occurred_at` est dans le payload signé, donc
infalsifiable, et les notifications hors de
`PARTNER_WEBHOOK_TOLERANCE_SECONDS` (défaut 300) sont rejetées en `400`. La
fenêtre est contrôlée dans les deux sens : une horloge partenaire en avance est
aussi suspecte qu'un rejeu. Mettre la tolérance à `0` désactive le contrôle.

C'est une fenêtre, pas de l'idempotence. À l'intérieur, une reprise reste un
doublon — et c'est l'affaire du sink.

## Codes de statut

| Situation | Code | Pourquoi |
|---|---|---|
| Vérifié, accepté | `202` | Le résultat peut encore être appliqué de façon asynchrone |
| Signature absente ou fausse | `401` | Le corps n'est jamais parsé |
| Signature valide, corps malformé | `422` | Émetteur authentique, payload cassé |
| `occurred_at` hors fenêtre | `400` | Rejouer les mêmes octets n'y changera rien |
| `job_id` inconnu | `404` | Nous ne l'avons jamais émis ; ne pas réessayer |

Le `401` ne porte délibérément pas de `WWW-Authenticate: Bearer` : la route ne
fait pas partie de la surface JWT et le partenaire n'a aucun bearer à offrir.

## Appliquer le résultat

`DbPartnerJobSink` cherche le document par `partner_job_id` sur la session
système, applique le résultat, et dérive l'organisation de la ligne trouvée. Deux
obligations qu'il porte :

- **Lever `UnknownPartnerJob`** quand rien n'attend ce `job_id`, ce que le
  gestionnaire d'erreurs traduit en `404`.
- **Être idempotent.** Les partenaires rejouent, donc le même `job_id` arrivera
  deux fois. L'implémentation est une mise à jour conditionnelle sur l'état
  courant du document, pas une table de déduplication à part : un document sorti
  de `awaiting_partner` est décidé, et une reprise obsolète ne doit pas faire
  basculer un document `ready` en `failed`.

## Le tester depuis Swagger

La signature doit couvrir les octets exacts qui passent sur le fil, ce qui rend
l'endpoint intestable à la main sans aide. `POST /webhooks/partner/sign` signe
exactement les octets qu'on lui poste :

1. Saisir la notification dans `/webhooks/partner/sign` et exécuter.
2. Copier la `signature` renvoyée.
3. Envoyer **le même texte** à `/webhooks/partner` avec cette signature dans
   l'en-tête `X-Partner-Signature`.

Le helper prend un corps brut plutôt qu'un modèle parsé, exprès : un modèle parsé
devrait être re-sérialisé avant signature, et les seuls octets qui vérifieraient
alors seraient ceux à l'intérieur du champ `body` échappé en JSON de la réponse —
qu'un humain devrait déséchapper à la main. Modifier le texte entre les deux
appels, ne serait-ce que d'un espace, invalide la signature : c'est le contrôle
qui fonctionne, pas un bug.

Le handler déclare à la fois un `PartnerWebhookRequest` parsé et la `Request`
brute. Le modèle parsé n'est jamais lu ; il est là pour que FastAPI valide le
payload et affiche une zone de texte éditable dans `/docs` comme pour toute autre
route, pendant que la signature couvre bien `request.body()`.

Ce helper est un oracle de forgerie : quiconque peut l'appeler peut tout signer.
Il est actif par défaut pour que `docker compose up` donne un `/docs`
fonctionnel, et se désactive avec `PARTNER_WEBHOOK_SIGNING_HELPER=false` partout
où le secret est réel.

## Configuration

| Variable | Défaut | Signification |
|---|---|---|
| `PARTNER_HMAC_SECRET` | placeholder de dev | Secret partagé, hors bande. **À surcharger en production.** |
| `PARTNER_WEBHOOK_TOLERANCE_SECONDS` | `300` | Fenêtre anti-rejeu ; `0` désactive |
| `PARTNER_WEBHOOK_SIGNING_HELPER` | `true` | Expose `/webhooks/partner/sign` |
