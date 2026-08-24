# Sécurité & confidentialité

Ce dépôt est une démonstration publique, réduite et anonymisée, dérivée d’un pipeline de conformité RWA déployé en production. Il ne constitue ni un déploiement production « clé en main », ni une référence réglementaire.

## Contrôles inclus dans cette V1 publique

- Les secrets, le domaine backend réel et les routes API de production ne sont pas publiés. La configuration se fait par variables d’environnement et le backend par défaut utilise `example.invalid`.
- La configuration runtime est validée avant tout traitement réel ; HTTPS est imposé par défaut.
- Les nouveaux artefacts runtime sont créés avec un `umask` restrictif (`0077`) et les dossiers sensibles utilisent des permissions propriétaire uniquement lorsque le système de fichiers le permet.
- Les téléchargements de White Papers utilisent une allow-list obligatoire par défaut, revalident chaque redirection, refusent les IP non publiques, limitent le nombre de redirections et la taille téléchargée, restreignent les extensions et contrôlent une signature minimale du format.
- Les archives DOCX sont contrôlées avant LibreOffice : archive ZIP valide, pas de chiffrement, pas de traversée de chemin, nombre d’éléments et taille décompressée limités.
- Les PDF/OCR disposent de limites configurables sur le nombre de pages, le nombre de pixels par page et les durées de rendu/OCR/sous-processus.
- LibreOffice et Ghostscript reçoivent un environnement construit sur une allow-list minimale (`PATH`, locale/zone horaire si présentes, puis paramètres strictement nécessaires), sans secrets applicatifs ni variables de proxy. Ghostscript est lancé en mode `-dSAFER`.
- Tous les appels HTTP, clients LLM et sous-processus explicitement lancés par le code utilisent des timeouts. Les retries SDK sont désactivés lorsque le pipeline possède déjà sa propre logique de retry/failover.
- Les sorties LLM brutes sont désactivées par défaut et stockées, lorsqu’elles sont explicitement activées, dans un dossier runtime exclu de Git.
- Les principaux artefacts JSON et fichiers de tracking sont écrits de manière atomique afin de limiter le risque de fichiers tronqués après interruption.
- Le lanceur CRON utilise `flock` : le verrou est détenu par le noyau pendant toute la durée du processus et libéré automatiquement à sa disparition.
- Le statut final `COMPLETED` appartient uniquement au pipeline principal. Une publication API vide ou partielle remonte une erreur et ne peut pas être masquée par une mise à jour de statut effectuée plus bas dans la pile.
- L’image Docker s’exécute avec un utilisateur non privilégié ; Docker Compose supprime les capacités Linux, active `no-new-privileges`, limite le nombre de processus, n’expose aucun port hôte et ne monte que les répertoires runtime nécessaires.
- `.gitignore` et `.dockerignore` excluent secrets, documents clients, résultats générés, logs et artefacts runtime du contrôle de version et du contexte de build.
- `.github/dependabot.yml` programme une surveillance hebdomadaire des dépendances Python et de l’image Docker.

## Limites et responsabilités d’un déploiement réel

Le traitement de documents non fiables fait intervenir des parseurs natifs (LibreOffice, Poppler, Tesseract, Ghostscript, PyMuPDF). Les garde-fous ci-dessus réduisent le risque mais **ne remplacent pas un sandbox dédié**. Un déploiement à haut niveau d’assurance devrait isoler la préparation documentaire dans un service/conteneur distinct avec egress réseau strict ou nul, sans secrets LLM/API, et appliquer en complément un secret manager, chiffrement des données et sauvegardes, politiques de rétention/suppression, journalisation centralisée avec contrôle d’accès, scans réguliers des dépendances/images et les contrôles de sécurité du backend, de la base et des interfaces Web environnantes.

Le framework synthétique inclus sert uniquement à documenter la structure attendue. Le framework de production, les prompts de production, les White Papers clients et les résultats d’audit réels sont volontairement absents.
