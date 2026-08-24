# -*- coding: utf-8 -*-
"""
Stage 2 Agent — Delete Redundancy
Intégré au pipeline : utilise le même logging, la même clé, et le même wrapper LLM que le Stage 1.
"""
import json
import logging
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

import config as conf
import prompts as prompt_rwa
import rwa_methods as rwa


def _load_stage1_json_latest(file_processed: str) -> list[dict]:
    """
    Charge le JSON final du Stage 1 pour un 'file_processed' donné.
    On prend le fichier le plus récent correspondant au motif *_llm_aggregated_with_anomalies_stage1.json.
    """
    file_processed = rwa.validate_file_processed(file_processed)
    candidates = sorted(conf.report_stage_1_folder.glob(
        f"{file_processed}_*_llm_aggregated_with_anomalies_stage1.json"
    ))
    if not candidates:
        raise FileNotFoundError(
            f"Aucun JSON Stage 1 trouvé pour '{file_processed}' dans {conf.report_stage_1_folder}"
        )
    latest = candidates[-1]
    logging.info(f"[Stage2] JSON Stage 1 chargé : {latest.name}")
    with open(latest, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Le JSON Stage 1 doit être une liste d’objets.")
    return data



# Récupère le provider et le modèle prioritaires (premier élément de STAGE2_PROVIDER_ORDER)
def get_stage2_provider_and_model() -> tuple[str, str]:
    """
    Retourne le provider et le modèle prioritaires configurés pour le Stage 2.

    Cette fonction lit explicitement la configuration STAGE2_PROVIDER_ORDER
    et retourne le premier couple (provider, model) défini.

    Rôle :
    - Centraliser l'accès au provider/modèle prioritaires du Stage 2.
    - Améliorer la lisibilité du code appelant.
    - Éviter la duplication de l'accès direct à STAGE2_PROVIDER_ORDER[0].

    Important :
    - Cette fonction ne gère ni le retry, ni le failover.
    - Elle n'influence pas le provider réellement utilisé lors des appels LLM.
    - Le retry et le failover sont entièrement pilotés par :
        * call_llm_with_retry_stage2()
        * call_llm_with_failover_stage2_entry()
    """
    return conf.STAGE2_PROVIDER_ORDER[0]


def _call_agent_2_on_one_entry(entry: dict, file_processed: str) -> dict:
    """
    Stage 2 — Agent de suppression des redondances pour un framework_id.

    Cette fonction :
    - construit un prompt de déduplication à partir d'une entrée Stage 1,
    - appelle le LLM avec retry (max 5) et failover multi-provider,
    - interprète les indices retournés (kept_indices / deleted_indices),
    - filtre les listes associées (flag, page_number, excerpt, output),
    - enrichit l'entrée avec la réponse Stage 2 et les métadonnées LLM.

    En cas de réponse vide ou invalide, l'entrée est retournée inchangée.
    Toute exception levée est considérée comme fatale pour le Stage 2
    et provoque l'arrêt du traitement du report.
    """
    framework_id = entry.get("framework_id")
    flags = entry.get("flag", [])
    excerpts = entry.get("whitepaper_excerpt", [])
    outputs = entry.get("output", [])
    page_numbers = entry.get("page_number", [])
    llm_prompt = entry.get("llm_prompt", "")
    category = entry.get("category", "Unspecified category")

    if not excerpts or not outputs:
        logging.warning(f"[Stage2] framework_id={framework_id} → aucune donnée à traiter (listes vides).")
        return entry



    # 🔹 Construction du prompt enrichi avec le contexte de la catégorie
    entry_data = {
        "framework_id": framework_id,
        "category": category,
        "llm_prompt": llm_prompt,
        "flag": flags,
        "page_number": page_numbers,
        "whitepaper_excerpt": excerpts,
        "output": outputs
    }


    prompt_stage2 = prompt_rwa.prompt_template_stage2_delete_redundancy.substitute(
        entry_json=json.dumps(entry_data, ensure_ascii=False, indent=2)
    )

    messages = [
        SystemMessage(content="You are an RWA framework expert."),
        HumanMessage(content=prompt_stage2)
    ]

    # Le provider réellement utilisé est déterminé par le mécanisme de failover.
    # Appel LLM Stage 2 avec retry (max 5) + failover multi-provider (selon STAGE2_PROVIDER_ORDER)
    data, raw_text, provider_used, model_used = rwa.call_llm_with_failover_stage2_entry(
        messages=messages,
        file_processed=file_processed,
        framework_id=framework_id
    )


    if not data:
        logging.warning(f"[Stage2] framework_id={framework_id} → réponse LLM vide ou inexploitable (après retry+failover).")
        return entry

    # 🔍 Normalisation : certains providers renvoient une liste avec un seul objet
    response = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else data
    if not isinstance(response, dict):
        logging.warning(f"[Stage2] framework_id={framework_id} → format inattendu de la réponse (type={type(response)}).")
        return entry

    sequence_keys = ("flag", "page_number", "whitepaper_excerpt", "output")
    lengths = {len(entry.get(key, [])) for key in sequence_keys}
    if len(lengths) != 1:
        raise ValueError(
            f"Stage 2 : listes désalignées avant déduplication pour framework_id={framework_id}."
        )
    item_count = lengths.pop()

    def normalize_indices(value, field_name: str) -> list[int]:
        if not isinstance(value, list):
            raise ValueError(f"Stage 2 : {field_name} doit être une liste.")
        normalized: list[int] = []
        for index in value:
            if isinstance(index, bool) or not isinstance(index, int):
                raise ValueError(f"Stage 2 : {field_name} doit contenir uniquement des entiers.")
            if index < 0 or index >= item_count:
                raise ValueError(f"Stage 2 : indice hors limites dans {field_name}: {index}.")
            if index in normalized:
                raise ValueError(f"Stage 2 : indice dupliqué dans {field_name}: {index}.")
            normalized.append(index)
        return normalized

    kept = normalize_indices(response.get("kept_indices", []), "kept_indices")
    deleted = normalize_indices(response.get("deleted_indices", []), "deleted_indices")
    if not kept:
        raise ValueError("Stage 2 : kept_indices ne peut pas être vide pour une entrée non vide.")
    if set(kept) & set(deleted):
        raise ValueError("Stage 2 : kept_indices et deleted_indices se chevauchent.")
    if set(kept) | set(deleted) != set(range(item_count)):
        raise ValueError("Stage 2 : kept_indices et deleted_indices doivent couvrir tous les éléments.")

    justifications = response.get("justifications", {})
    if not isinstance(justifications, dict):
        raise ValueError("Stage 2 : justifications doit être un objet JSON.")

    def safe_filter(values: list, indices: list[int]) -> list:
        return [values[index] for index in indices]

    filtered = {
        **entry,
        "flag": safe_filter(entry.get("flag", []), kept),
        "page_number": safe_filter(entry.get("page_number", []), kept),
        "whitepaper_excerpt": safe_filter(entry.get("whitepaper_excerpt", []), kept),
        "output": safe_filter(entry.get("output", []), kept),
        "response_stage_2_delete_redundancy": {
            "provider_used": provider_used,
            "model_used": model_used,
            "kept_indices": kept,
            "deleted_indices": deleted,
            "justifications": justifications
        }

    }

    logging.info(f"[Stage2] framework_id={framework_id} → {len(kept)} conservé(s), {len(deleted)} supprimé(s).")
    return filtered



def run_stage2_delete_redundancy(file_processed: str):
    """
    Orchestration Stage 2 :
    - charge le JSON du Stage 1
    - boucle sur les frameworks
    - sauvegarde le JSON Stage 2
    """
    file_processed = rwa.validate_file_processed(file_processed)

    # Vérification dynamique du provider prioritaire.
    first_provider, first_model = conf.STAGE2_PROVIDER_ORDER[0]

    if first_provider.lower() == "openai" and not conf.OPENAI_API_KEY:
        raise RuntimeError("Clé OpenAI manquante – impossible d'exécuter le Stage 2 avec OpenAI.")
    elif first_provider.lower() == "gemini" and not conf.GEMINI_API_KEY:
        raise RuntimeError("Clé Gemini manquante – impossible d'exécuter le Stage 2 avec Google Generative AI.")


    entries = _load_stage1_json_latest(file_processed)
    logging.info(f"[Stage2] Orchestration prête → provider prioritaire = {first_provider.upper()} | modèle = {first_model}")

    processed = []
    for e in entries:
        fid = e.get("framework_id")
        logging.info(f"[Stage2] Traitement framework_id={fid}")
        try:
            processed.append(_call_agent_2_on_one_entry(e, file_processed))
        except Exception as err:
            logging.error(f"[Stage2] Erreur framework_id={fid} : {err}", exc_info=True)

            # On attache l'erreur sur l'entrée pour inspection éventuelle,
            # mais on ne continue pas le Stage 2 (philosophie : échec fatal du report)
            e["response_stage_2_delete_redundancy"] = {"error": str(err)}
            processed.append(e)

            raise RuntimeError(
                f"Stage 2 fatal: échec framework_id={fid} après retries+failover"
            ) from err


    timestamp = datetime.utcnow().strftime("%y_%m_%d-%H-%M-%S")
    out_path = conf.report_stage_2_folder / f"{file_processed}_{timestamp}_stage2_deduplicated_with_justifications.json"
    rwa.save_json_file(out_path, processed)
    logging.info(f"[Stage2] 💾 JSON archivé (horodaté) : {out_path.name}")

    # On complète le monitoring global (créé en fin de Stage 1) avec le chemin Stage 2
    # afin de centraliser la traçabilité dans un seul fichier par exécution.
    rwa.update_latest_monitoring_file(
        file_processed=file_processed,
        updates={"stage2_final_json_path": str(out_path.resolve())}
    )


    return processed

