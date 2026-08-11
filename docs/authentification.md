# Authentification

Comment une paire email/mot de passe devient un `AuthContext`, et comment une
session survit — ou meurt — dans le temps. Ce document s'arrête à la frontière de
confiance : ce que le tenant fait *une fois résolu* — les quatre niveaux
d'isolation, `app.current_org_id`, les preuves RLS — est argumenté dans
[architecture-upload.md §5](architecture-upload.md).

Dit d'emblée, parce que ça cadre tout le reste : **cette auth est faite maison et
n'est pas une cible de production** (L13). Elle existe pour que l'exercice soit
exerçable de bout en bout. En production, je délègue à un fournisseur d'identité
managé en OIDC — c'est F10. Ce qui suit défend les choix *sachant cela* : chacun
est fait pour être jeté d'un bloc, pas pour être étendu.

## La donnée d'abord

```python
@dataclass(frozen=True, slots=True)
class AuthContext:
    user_id: UUID
    org_id: UUID          # porté par toute requête authentifiée

@dataclass(frozen=True, slots=True)
class RefreshToken:
    id: UUID
    user_id: UUID
    org_id: UUID
    family_id: UUID       # chaîne toutes les rotations d'un même login
    token_hash: str       # SHA-256 ; le secret ne touche jamais la base
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    revoked_at: datetime | None = None
```

**`family_id` est tout le design.** Sans lui, « détecter un vol » devient un
parcours de graphe parent → enfant, ou un tas de conditionnels. Avec lui, la
révocation est un `UPDATE` sur une colonne indexée, et « ce token a déjà servi »
est un test de champ — `consumed_at IS NOT NULL` — pas un algorithme.

Deux timestamps nullables plutôt qu'un enum `status` : `consumed_at` et
`revoked_at` ne sont pas exclusifs (un token consommé se fait révoquer avec sa
famille), et ils portent le *quand*, qu'un enum perdrait.

## Deux crédentiels, deux natures

|  | Access | Refresh |
|---|---|---|
| Forme | JWT signé HS256 | 48 octets aléatoires urlsafe |
| Vérification | signature locale, zéro I/O | lookup base par hash |
| Durée | 6 h | 30 j |
| En base | rien | SHA-256 seulement |
| Révocable | non, jusqu'à `exp` | oui, par famille |

Le refresh n'est **délibérément pas** un JWT (`jwt.py:15-20`). Le seul avantage
d'un JWT est la vérification sans état ; or un refresh doit être révocable, ce
qui impose un aller-retour base de toute façon. L'avantage est nul et le coût
réel : un JWT porte des claims lisibles, une valeur opaque ne fuite rien si elle
atterrit dans un log ou un historique shell.

**SHA-256 pour le refresh, argon2id pour le mot de passe.** Ce n'est pas une
incohérence, c'est la différence entre les deux entrées : un refresh est 384 bits
d'aléa — il n'y a rien à brute-forcer, et le hachage tourne à chaque rotation ;
un mot de passe est court, choisi par un humain, et c'est exactement le cas
qu'argon2id est fait pour rendre coûteux (`jwt.py:79`, `hashing.py:8`).

## Le login ne dit pas si l'email existe

Un email inconnu appelle quand même `verify_dummy` (`login.py:17`), qui vérifie
le mot de passe présenté contre un vrai hash argon2 d'une valeur que personne ne
connaît, construit une fois au démarrage (`hashing.py:14`).

Sans ça, le chemin « email inconnu » revient en microsecondes quand un mauvais
mot de passe coûte le facteur de travail complet d'argon2 : **la latence de
réponse devient un oracle d'énumération d'utilisateurs**.

L'ordre compte aussi. `is_active` est testé *après* le mot de passe
(`login.py:23`) : l'inverse répondrait « compte désactivé » à qui ne connaît pas
le mot de passe, et confirmerait l'existence du compte.
→ `test_unknown_email_and_wrong_password_are_indistinguishable`,
`test_disabled_account_is_rejected_after_password_check`.

## Rotation : la machine à états

Chaque `POST /auth/refresh` consomme le token présenté et en rend un nouveau de
la même famille.

| Token présenté | Réponse | Effet de bord |
|---|---|---|
| inconnu, ou déjà révoqué | `401` | — |
| **déjà consommé** | `401` | **toute la famille révoquée** |
| expiré | `401` | — |
| valide, utilisateur désactivé | `403` | — |
| valide | `200` + nouvelle paire | le présenté passe `consumed_at` |

Le cas qui compte est le deuxième. Un token consommé qui revient signifie que
deux porteurs détiennent la même valeur : le client légitime et quelqu'un
d'autre. On ne sait pas lequel des deux parle — donc on coupe les deux. C'est le
seul signal de vol qu'un serveur puisse observer sans rien demander au client.

### La subtilité qui vaut son test

```python
await deps.refresh_tokens.revoke_family(stored.family_id, now)
await deps.uow.commit()          # avant le raise, pas après
raise RefreshTokenReused(...)
```

L'exception déroule la session. Si la révocation restait non commitée, l'API
répondrait `401` **et laisserait la famille volée utilisable** : la détection
sans l'effet. C'est la raison pour laquelle `UnitOfWork.commit` est explicite
plutôt qu'implicite au teardown de requête — c'est le seul endroit du code où le
commit doit précéder l'exception.

Prouvé deux fois, parce qu'un fake pourrait mentir : en unitaire
(`test_replay_commits_the_revocation_before_raising`) et en intégration contre un
vrai Postgres (`test_replay_revocation_survives_the_exception`).

## Le token porte le tenant

Le JWT d'accès porte `sub`, `org`, `typ`, `iss`, `iat`, `exp`, `jti`.
`get_auth_context` (`deps.py:82`) est **le seul endroit** qui transforme un
crédentiel en `AuthContext` — donc le seul endroit qui connaît et croit le
tenant, et donc le seul qui lie `org_id`/`user_id` aux contextvars de log
([observabilite.md](observabilite.md)).

`typ` est revérifié au décodage (`jwt.py:63`) : **un refresh ne doit jamais
fonctionner comme bearer**. Rien aujourd'hui n'émet `typ != access` ; le contrôle
fait une ligne et l'échec qu'il empêche est total.
→ `test_me_rejects_a_refresh_token_used_as_a_bearer`.

Ce que le token ne porte pas : rôles, permissions, email. Il n'y a pas de modèle
d'autorisation au-delà du tenant, et ajouter des claims que personne ne lit
serait spéculatif.

`get_auth_context` est `async` **délibérément**, alors que rien n'y est attendu
(`deps.py:88`) : FastAPI exécute une dépendance *sync* dans un thread de worker,
qui reçoit une copie du contexte — les contextvars liées y seraient jetées au
retour, et chaque ligne de log perdrait son tenant.

## L'auth est une dépendance, pas un middleware

Deux conséquences concrètes, et c'est tout l'argument :

- `/health` reste hors auth **sans liste d'exclusion de chemins** — la forme qui
  se désynchronise dès qu'une route bouge.
- Les routes protégées apparaissent dans le schéma OpenAPI, où un relecteur voit
  lesquelles le sont. → `test_openapi_advertises_the_bearer_scheme`.

## Les deux rôles Postgres

Avant le login, il n'y a pas d'organisation connue : « trouver l'utilisateur par
email » et « trouver le refresh par hash » sont précisément les requêtes qu'on ne
*peut pas* scoper par tenant. D'où un second rôle, et une frontière nette autour
de son usage.

| Rôle | RLS | Sert à |
|---|---|---|
| `app_rw` | soumis (`NOBYPASSRLS`) | tout ce qui suit l'authentification |
| `app_auth` | `BYPASSRLS` | les deux lookups pré-auth, et le seeding |

Deux pools distincts (`db/session.py`), pas un rôle unique avec un `SET`
conditionnel : le privilège est une propriété de la connexion, et le mélanger
ferait de chaque checkout un cas particulier.

`refresh_tokens` porte quand même `org_id` et sa politique `org_isolation`
(migration `0001`) : écrit une fois à l'émission, et vrai pour toute lecture
faite ensuite sur `app_rw`. Les politiques sont en `FORCE ROW LEVEL SECURITY`,
donc le propriétaire de la table y est soumis aussi.

## Surface

| Route | Auth | Notes |
|---|---|---|
| `POST /auth/login` | — | `401` indistinct · `403` si désactivé · `422` email malformé |
| `POST /auth/refresh` | — | rotation ; voir la machine à états |
| `POST /auth/logout` | — | `204` toujours |
| `GET /me` | Bearer | `401` si le sujet ne résout plus dans ce tenant |

`logout` est `204` même pour un token inconnu, et silencieux (`logout.py:7`) : se
déconnecter ne doit pas être un oracle pour tester des valeurs de token. Il
révoque la **famille**, pas le token — sinon la déconnexion laisserait vivantes
les rotations précédentes.

## Configuration

| Variable | Défaut | Note |
|---|---|---|
| `JWT_SECRET` | dev-only | ≥ 32 octets, sinon PyJWT prévient : une clé HS256 plus courte que le digest affaiblit le MAC (RFC 7518 §3.2) |
| `JWT_ALGORITHM` | `HS256` | |
| `JWT_ISSUER` | `test-backend` | vérifié au décodage, pas seulement émis |
| `JWT_ACCESS_TTL_SECONDS` | `21600` (6 h) | |
| `JWT_REFRESH_TTL_SECONDS` | `2592000` (30 j) | |
| `APP_RW_PASSWORD` / `APP_AUTH_PASSWORD` | = nom du rôle | interpolés dans un `DO $$` — d'où le charset restreint `[A-Za-z0-9_.-]` |

Des secondes plutôt que des `timedelta` : pydantic ne coerce que l'ISO-8601
depuis l'environnement, et `JWT_ACCESS_TTL=PT6H` est un pire bouton que
`JWT_ACCESS_TTL_SECONDS=21600`. L'unité est dans le nom.

## Tests

50 tests couvrent cette surface.

| Fichier | Ce qui est prouvé | |
|---|---|---|
| `unit/test_jwt.py` | round-trip, expiration, mauvaise signature, **token non signé (`alg: none`)**, mauvais `typ`, le secret n'est jamais stocké | 7 |
| `unit/test_login.py` | normalisation email, indistinguabilité, ordre du test `is_active`, nouvelle famille par login | 8 |
| `unit/test_refresh.py` | rotation, continuité de famille, rejeu → révocation, commit avant raise | 8 |
| `unit/test_logout.py` | révocation de famille, silence sur les inconnus | 4 |
| `api/test_auth_routes.py` | codes de statut, refresh rejeté comme bearer, OpenAPI | 14 |
| `integration/test_refresh_persistence.py` | rotation à travers des sessions réelles, révocation qui survit à l'exception, échec de login n'écrit rien | 3 |
| `integration/test_tenant_isolation.py` | les preuves RLS, en SQL non filtré | 6 |

Les deux derniers fichiers demandent `TEST_POSTGRES_DSN` (L16) : ce sont ceux qui
comptent le plus, et un `pytest` nu les saute.

## Ce qui n'est pas là

- **Un access token n'est pas révocable avant son `exp`.** `logout` tue la
  famille de refresh, mais un JWT déjà émis reste valide jusqu'à 6 h. `jti` est
  émis et rien ne le consomme : il n'y a pas de denylist. C'est le prix assumé de
  la vérification sans état — le seul levier est le TTL, et le vrai correctif est
  F10, pas une table de révocation maison.
- **Aucune limitation de débit sur `/auth/login`.** Un brute-force n'est borné
  que par le coût d'argon2. À la maille d'un reverse-proxy en production, ou de
  l'IdP après F10.
- **Les refresh expirés et révoqués ne sont jamais purgés.** La table croît avec
  les logins. Un `DELETE` daté sur `expires_at`, indexé, le jour où ça pèse.
- **Ni inscription, ni reset de mot de passe, ni MFA, ni vérification d'email.**
  Les comptes viennent de `app/seed.py`.

Chacun de ces points disparaît avec **F10** au lieu d'être construit ici. C'est
ce qui rend L13 tenable : ce module est petit, isolé derrière `TokenService` et
`PasswordHasher`, et remplaçable d'un bloc.
