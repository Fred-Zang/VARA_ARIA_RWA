# Dossiers de données runtime

Ce répertoire reproduit l’organisation principale utilisée par le pipeline documentaire et ses artefacts LLM.

Tous les contenus runtime ou clients sont exclus de Git. Les fichiers `.gitkeep` servent uniquement à rendre l’architecture visible dans le dépôt public.

## Chemins actifs de la V1 publique

- `origin_WP/` : White Papers sources téléchargés.
- `paginated_WP/` : documents préparés avec repères de pages pour la traçabilité des citations.
- `reduced_weight_pdf_WP/` : PDF optimisés lorsqu’une réduction de taille est nécessaire.
- `ocr_WP/` : sorties documentaires normalisées par OCR.
- `ocr_problems/` : artefacts de diagnostic OCR.
- `llm_generated_reports/` : chunks et artefacts LLM intermédiaires.
- `llm_generated_reports_stage_1/` : résultat agrégé de l’Agent 1.
- `llm_generated_reports_stage_2/` : résultat dédupliqué de l’Agent 2.
- `llm_generated_reports_post_db/` : payload simplifié préparé pour publication.
- `llm_postprocessing/agent3_outputs/` : synthèse Agent 3 et artefacts d’audit d’exécution.
- `llm_prompt_folder/` : placeholders publics et mini-framework synthétique.
- `generated_excels/` : exports de rapport optionnels.

## Placeholders historiques / production

- `archives/`
- `llm_reports_downloaded/`

Ils sont conservés uniquement pour documenter l’organisation plus large de l’espace de travail de production et ne sont pas nécessaires à tous les chemins d’exécution de cette V1 nettoyée.

**Aucun document client, résultat LLM, rapport ou framework de production ne doit être commité dans ce dossier.**
