# Wazzzxpense 💸

Application web **mobile-first** (installable sur le téléphone comme une app) pour piloter le budget du foyer :
dépenses, revenus, crédits, assurances, paiements en plusieurs fois, provisions, notes de frais Jems et factures.

Déploiement prévu : **https://wazzzxpense.walautao.fr** sur un VPS IONOS.

## Le principe en 1 minute

1. **Au moment d'un achat** → bouton **＋** : montant, pour qui, catégorie, photo du ticket. L'achat part en
   « attente ».
2. **Chaque mois** → *Plus › Importer un relevé* : déposez l'export **CSV ou OFX** de votre banque (ou le **PDF** du
   relevé, lu par l'IA). L'app :
   - écarte les doublons (réimporter une période qui chevauche est sans risque) ;
   - **rapproche les achats en attente** (même montant, dates proches) : ils récupèrent catégorie, personne, tags et
     facture ;
   - reconnaît crédits, assurances, échéances en plusieurs fois et récurrences ;
   - applique vos **règles** (« libellé contient AXA → Assurances, tag axa ») puis **apprend** de vos choix passés ;
   - **contrôle le solde** calculé par rapport à celui de la banque.
3. **À trier** : ce qui reste est présenté une carte par marchand ; un choix range aussi les opérations similaires
   et crée la règle pour les fois suivantes.
4. **Bilan du mois** : entrées/sorties, budgets, répartition par personne et périmètre, et une **inspection
   automatique** (dépassements, hausses inhabituelles, frais bancaires, doublons, abonnements non suivis, notes de
   frais à faire, achats jamais débités). En option, une **analyse IA** propose un verdict et 3 actions concrètes.

## Fonctionnalités

| Besoin | Où |
|---|---|
| Catégories, **pour qui** (moi, ma femme, chaque enfant, famille), **périmètre** Perso / Famille / **Jems** | sur chaque opération / achat |
| **Tags** libres et recherche (« toutes les dépenses AXA », « assurance-mini ») | *Opérations* (recherche), *Plus › Tags* |
| Achats en attente de passage en banque, rapprochement automatique | bouton ＋, *À trier › Achats en attente* |
| **Disponible réel** = solde banque − achats pas encore débités ; estimation de fin de mois | Accueil |
| **Crédits** : capital restant dû, échéances payées/restantes, coût des intérêts, date de fin | *Plus › Crédits* |
| **Assurances** : contrats, assureur, fréquence, échéance annuelle, coût annuel, documents | *Plus › Assurances* |
| **Paiements en plusieurs fois** (Alma, Oney, Klarna, 3x/4x CB…) : échéancier pointé automatiquement | *Plus › Paiements en plusieurs fois* |
| **Récurrences** (salaire, loyer, abonnements) et **provisions** (taxe foncière…) : combien mettre de côté par mois | *Plus › Récurrences*, *Budgets* |
| **Échéancier** 30 → 180 jours avec solde projeté et point bas | *Plus › Échéancier* |
| **Budgets** mensuels par catégorie avec jauges | *Plus › Budgets* |
| **Notes de frais Jems** : à déclarer → déclarée → remboursée, export ZIP (CSV + justificatifs renommés) | *Plus › Notes de frais Jems* |
| **Factures / justificatifs** (photo ou PDF) sur opérations, achats, crédits, assurances | partout, *Plus › Factures* |
| **Recherche** par mot (sans accents), montant (`42,50`, `>100`, `50-100`), date (`12/03`, `03/2026`), période, tri | *Opérations* |
| Comptes joints / perso / carte, « pour qui » et périmètre par défaut par compte, alerte découvert + coût des agios | *Paramètres*, Accueil, Bilan |
| **Profil du foyer** (JSON) : personnes, comptes, catégories, règles, crédits, assurances, récurrences en un import | *Paramètres › Profil* |
| Export CSV, **sauvegarde ZIP** complète | *Opérations*, *Paramètres* |

### Formats de relevés

- **PDF Crédit Agricole** : lus **directement sur le serveur, sans IA** (rien ne sort de votre VPS). Le compte est
  reconnu par son numéro (créé automatiquement au 1er import, solde de départ repris du relevé), la carte à **débit
  différé** est importée achat par achat sur un compte « carte » (le prélèvement mensuel « Dépenses Carte » devient
  un virement interne : rien n'est compté deux fois), et les totaux sont comparés à ceux de la banque.
  Plusieurs relevés peuvent être déposés d'un coup.

- **CSV** : détection automatique des colonnes (Date / Libellé / Montant, ou Débit + Crédit), séparateurs `;` `,`
  tabulation, encodages UTF-8 et Windows. Testé sur les formats type Crédit Agricole, Boursorama et génériques.
- **OFX / QFX** : le plus fiable (identifiant unique par opération + solde du relevé pour le contrôle).
- **PDF** : nécessite `ANTHROPIC_API_KEY`. L'IA extrait les opérations ; **vérifiez toujours l'aperçu** avant de
  valider (le contrôle de solde vous signale un écart).

👉 Privilégiez l'export **OFX** (ou CSV) depuis l'espace client de votre banque : c'est gratuit, exact et instantané.

## Installation sur le VPS IONOS

Prérequis : Docker sur le VPS, et un enregistrement DNS **A** `wazzzxpense.walautao.fr` → IP du VPS (espace
IONOS : *Domaines & SSL › walautao.fr › DNS*).

```bash
sudo mkdir -p /opt/wazzzxpense && sudo chown $USER /opt/wazzzxpense
git clone -b claude/determined-ptolemy-gsbolc https://github.com/wdemirdjian-14/Jemsly.git /opt/wazzzxpense
cd /opt/wazzzxpense
cp .env.example .env && nano .env   # APP_USERNAME + APP_PASSWORD (long !), ANTHROPIC_API_KEY (facultatif)
./scripts/deploy.sh                 # l'app écoute sur 127.0.0.1:8765
```

### Avec le nginx déjà installé sur le VPS (cas par défaut)

```bash
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/wazzzxpense
sudo ln -s /etc/nginx/sites-available/wazzzxpense /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d wazzzxpense.walautao.fr
```

### Sans serveur web sur le VPS

Ajoutez `USE_CADDY=1` dans `.env` puis relancez `./scripts/deploy.sh` : Caddy prend les ports 80/443 et gère
le certificat HTTPS tout seul.

Ouvrez https://wazzzxpense.walautao.fr, connectez-vous, puis sur le téléphone : *Partager › Sur l'écran d'accueil*
(iPhone) ou *⋮ › Installer l'application* (Android).

**Mises à jour** : `./scripts/deploy.sh` (git pull + rebuild).

**Sauvegardes** : en plus du bouton *Paramètres › Sauvegarde*, planifiez la sauvegarde quotidienne :

```bash
crontab -e
15 3 * * * /opt/wazzzxpense/scripts/backup.sh >> $HOME/wzx-backup.log 2>&1
```

Les archives vont dans `backups/` (30 jours). Copiez-les régulièrement hors du VPS.

## Premiers réglages conseillés

1. *Paramètres › Personnes* : renommez « Ma femme », remplacez « Enfants » par une ligne par enfant.
2. *Paramètres › Comptes* : un compte par compte bancaire (courant, joint…).
3. Importez **3 mois** de relevés : l'inspection compare toujours au mois courant aux 3 précédents.
4. Déclarez crédits, assurances et récurrences en indiquant le **mot du libellé bancaire** (ex. `CETELEM`, `AXA`) :
   les opérations passées sont rattachées immédiatement.
5. Après le 1er import OFX/PDF, cliquez sur **« Recaler le solde »** pour que le solde affiché soit celui de la banque.

## Sécurité & données

- Accès réservé : **identifiant + mot de passe** (haché PBKDF2), aucune page visible sans connexion, sessions
  signées en HTTPS, limitation des tentatives. Comptes supplémentaires et changement de mot de passe dans
  *Paramètres › Utilisateurs* ; mot de passe oublié : changer `APP_PASSWORD` dans `.env` et redéployer.
- Toutes les données restent sur votre VPS (`./data` : base SQLite + factures). Rien n'est envoyé ailleurs, sauf
  si la clé Anthropic est configurée : les relevés PDF et le résumé du mois (libellés et montants) sont alors
  transmis à l'API Claude au moment où vous le demandez.
- Les montants sont stockés en centimes (pas d'erreurs d'arrondi).

## Développement

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
APP_USERNAME=dev APP_PASSWORD=dev uvicorn app.main:app --reload     # http://127.0.0.1:8000
pytest
```

Stack : Python 3.13, FastAPI, SQLAlchemy + SQLite (WAL), templates Jinja rendus côté serveur, CSS maison, PWA,
Claude API (optionnel). Pas de build front, pas de base externe : simple à maintenir et à sauvegarder.

```
app/
  main.py          appli, connexion, middleware
  models.py        modèle de données
  services.py      rapprochement automatique, soldes, échéancier, crédits, inspection du mois
  importers.py     lecture CSV / OFX
  ai.py            lecture PDF + analyse mensuelle (Claude)
  routers/         écrans (core, imports, finance, reports, settings)
  templates/       pages HTML
  static/          CSS, JS, icônes, service worker
deploy/            Caddyfile, exemple nginx
scripts/           deploy.sh, backup.sh
tests/
```
