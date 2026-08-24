# Logs runtime & traçabilité

Ce répertoire documente l’organisation opérationnelle de la traçabilité du pipeline.

Les contenus runtime sont exclus de Git ; les `.gitkeep` conservent uniquement l’arborescence.

- `monitoring_process/` : monitoring par exécution et métadonnées provider/modèle.
- `monitoring_process/llm_call_metrics/` : métriques unitaires des appels LLM.
- `llm_raw_outputs/` : sorties LLM brutes optionnelles, désactivées par défaut.
- `track_dict_new_WPs_processed/` : suivi d’état utilisé par l’orchestration.
- `special_problem_to_solve/` : artefacts de diagnostic pour les cas anormaux.
- `track_folder/` : placeholder historique / production.

**Ne jamais commiter de logs de production, identifiants, prompts, extraits documentaires, credentials ou payloads de monitoring.**
