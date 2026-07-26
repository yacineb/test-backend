# Choix de l'orchestrateur

**Retenu : DBOS Transact.** La capacité ne discrimine pas les candidats, le
modèle de reprise si, et cette charge relève du côté checkpointing.

## 1. Le débit ne peut pas trancher

Chiffres de `scripts/simulate_pipeline.py`, qui modélise exactement les mocks
fournisseurs (`time.sleep()` s'exécute *avant* le tirage d'échec, donc une
tentative ratée coûte le même temps qu'une réussie). Politique retenue, 5
tentatives, 1/2/4/8s :

| par document | valeur |
|---|---|
| temps total p50 / p95 | 28,5s / 56,9s |
| step-secondes consommées | 37,5s |
| exécutions de step (retries inclus) | 5,9 |
| taux de succès | 98,4 % |

À la cible 12 mois de 100 000 documents/jour :

| régime | débit | exéc. step/s | slots concurrents |
|---|---|---|---|
| uniforme sur 24h | 1,16 doc/s | 6,9 | 43 |
| journée de 8h | 3,47 doc/s | 20,7 | 130 |
| journée de 8h, burst ×3 | 10,42 doc/s | 62,0 | 390 |

Postgres soutient des milliers de `SELECT … FOR UPDATE SKIP LOCKED` par seconde
sur un nœud. Le pire cas en demande **62**. Les 1 000 documents/jour d'aujourd'hui
valent un centième de la première ligne. Tous les candidats passent la barre avec
deux ordres de grandeur de marge : choisir sur le débit, c'est choisir sur un
chiffre qui ne discrimine pas.

**Ce qui contraint réellement, ce sont les threads, pas le débit** — voir
[pipeline.md](pipeline.md).

## 2. Replay contre checkpointing — l'axe qui tranche

| | reprise | apporte | coûte |
|---|---|---|---|
| **Replay déterministe** (Temporal, Cadence) | ré-exécute le workflow sur un historique d'événements persisté | contrôle de flux arbitraire durable gratuitement ; boucles, état en mémoire, timers illimités, historique de qualité audit | contraintes de déterminisme (pas d'horloge, pas d'aléa, pas d'E/S directe) et versioning des workflows dont des instances sont en vol |
| **Checkpointing** (DBOS, Restate, Sayiir) | persiste la sortie de chaque step, saute ceux qui sont finis | du code ordinaire, aucune sémantique de replay, versioning simple | toute sortie de step doit être sérialisable et rester petite ; l'état en mémoire entre steps est perdu |

Ce pipeline fait quatre steps, de forme fixe, sans boucle, sans branchement
dynamique, sans état accumulé en mémoire. Les sorties sont une chaîne, un dict,
une liste de chaînes et un id opaque — toutes JSON-natives et petites dès que le
texte OCR passe derrière une clé de stockage.

**Le bénéfice du replay est entièrement inutilisé ici, et son coût est payé
intégralement.** C'est ça l'argument, pas « Temporal est lourd ».

## 3. Le paysage

| | modèle | infrastructure | fan-out/join | attente externe | gouvernance |
|---|---|---|---|---|---|
| **DBOS Transact** | checkpoint | bibliothèque + Postgres | queues + handles | `send`/`recv`, timeouts durables | DBOS Inc., MIT, 2.x |
| **Temporal** | replay | cluster multi-services | natif | signaux natifs | échelle CNCF |
| **Restate** | checkpoint | serveur mono-binaire | natif | awakeables | Restate Inc. |
| **Sayiir** | checkpoint | bibliothèque + Postgres | `.fork()/.branch()/.join()` | `wait_for_signal(timeout=)` | mainteneur unique, MIT |
| **Procrastinate / PgQueuer** | file de tâches | bibliothèque + Postgres | à construire soi-même | à construire soi-même | petites équipes |
| **Celery** | file de tâches | broker + workers | `chord` (fragile) | non supporté | très mature |

**Pourquoi pas Celery**, l'option vers laquelle on tend spontanément : c'est une
file de tâches, alors qu'on a un workflow avec état et un point de suspension
externe. Deux domaines de durabilité (broker + Postgres) pour 0,07 job/s sans
rien pour les garder cohérents ; un `chord` pour le fan-out dont le compteur de
complétion vit dans le broker et interagit mal avec les retries par tâche ; aucun
checkpoint d'où repartir, donc `document_steps` devient la vraie machine à états
qu'on attendait du framework ; et `awaiting_partner` n'a aucune place dans une
file de tâches. Pas un jugement de qualité, un jugement de forme — et il ne
s'applique pas à une équipe qui exploite déjà Celery, où le coût marginal est bien
plus faible. Le détail est en
[decisions-et-limites.md §2](decisions-et-limites.md).

**Sur Sayiir** *(divulgation : je suis le maintainer de ce repo)* : répond au besoin mais encore largement utilisé, solo maintainer pour l'instant. Donc alternative écartée.

## 4. Quand revoir la décision

Pas sur un chiffre de volume. À revoir quand :

1. **Les types de documents se multiplient et leurs DAG divergent** — le DAG
   cesse d'être une constante et devient une donnée → Temporal ou Restate.
2. **L'état intermédiaire dépasse ce qu'il est raisonnable de checkpointer**, ou
   cesse d'être sérialisable → Temporal.
3. **L'historique d'événements devient une exigence de conformité** plutôt qu'un
   confort de debug → Temporal. Plausible pour un produit d'archivage réglementé.
4. **Une validation humaine entre dans le pipeline**, ajoutant des points de
   suspension en jours → les deux moteurs à checkpointing gèrent très bien.

Mouvements moins chers, disponibles avant de changer de moteur : faire renvoyer à
`ocr` une clé de stockage plutôt que le texte ; rendre les steps réellement
`async` quand les fournisseurs seront de vrais appels HTTP ; dimensionner
délibérément la concurrence de file par tenant.
