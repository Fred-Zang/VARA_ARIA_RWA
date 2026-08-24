# -*- coding: utf-8 -*-
"""Orchestration principale de la démonstration publique du pipeline RWA."""

# =====================================================================
# Pipeline public de démonstration — architecture issue de la version de production
# =====================================================================

import logging
import os
from datetime import datetime
import sys
import math
import json
import argparse
import atexit
from pathlib import Path


import pipeline_steps as pipe_steps
import rwa_methods as rwa
import config as conf


# =====================================================================
# LOGGING PRINCIPAL
# =====================================================================

log_dir = conf.log_folder
log_dir.mkdir(exist_ok=True)

date_utc = datetime.utcnow().strftime("%Y_%m_%d")
log_filename = log_dir / f"logs_process_{date_utc}.log"

log_file = open(log_filename, "a", encoding="utf-8", buffering=1)
log_file_handler = logging.StreamHandler(log_file)
handlers = [log_file_handler]

# reset des anciens handlers
for h in logging.root.handlers[:]:
    logging.root.removeHandler(h)

handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=handlers
)

# Réduire bruit HTTP / Gemini / OpenAI
for logger_name in ("openai", "httpx", "httpcore", "urllib3", "google.genai"):
    logging.getLogger(logger_name).setLevel(logging.WARNING)

# =====================================================================
# JOURNAL DES SUSPICIONS OCR
# =====================================================================
suspect_log_path = log_dir / "ocr_suspects.log"
suspect_log = open(suspect_log_path, "a", encoding="utf-8", buffering=1)

@atexit.register
def _close_logs():
    try:
        logging.shutdown()
    finally:
        try:
            log_file.close()
        except OSError:
            pass
        try:
            suspect_log.close()
        except OSError:
            pass


# =====================================================================
# TRACKING DU PIPELINE
# =====================================================================

def _tracking_file() -> Path:
    return conf.log_track_dict / "track_pipeline.json"


def _read_tracking() -> dict:
    fp = _tracking_file()
    if not fp.exists():
        return {}
    try:
        with fp.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Tracking local illisible, redémarrage avec un état vide : %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _save_tracking(data: dict) -> None:
    """Écrit le tracking local de façon atomique pour limiter les fichiers partiels."""
    target = _tracking_file()
    tmp = target.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, target)


# =====================================================================
# PIPELINE SERVEUR
# =====================================================================

def run_pipeline(file_processed: str | None = None):
    """
    Pipeline complet de démonstration, dérivé de la logique serveur :

    - NE CHANGE JAMAIS PENDING → PROCESSING (laisse le cron gérer)
    - Ne traite QUE les report_id déjà en PROCESSING
    - Etapes :
        OCR → Framework → Stage 1 failover → Stage 2 → POST DB
    """
    conf.validate_runtime_configuration()
    logging.info("")
    logging.info("----- 🚀 DÉMARRAGE PIPELINE RWA -----")

    if file_processed is not None:
        file_processed = rwa.validate_file_processed(file_processed)

    tracking = _read_tracking()
    start_time = datetime.utcnow().isoformat()
    had_report_error = False

    # Initialisation défensive :
    # Ces variables ne sont définies que si le Stage 1 a réellement été exécuté.
    # Si un report échoue avant (OCR/framework/etc.), on veut éviter un UnboundLocalError en fin de fonction.
    df_report = None
    entries = None
    monitoring_stage1 = None

    # =============================================================
    # 1️⃣ Charger uniquement les report_id déjà en PROCESSING pour lancer le pipeline complet
    #  car publier 1 WP sur le serveur => WP passe en PENDING puis le CRON passe le WP en PROCESSING avant traitement par ce script pipeline_main
    # =============================================================
    dict_processing = pipe_steps.step_recover_WPs_processing()
    if not dict_processing:
        logging.info("Aucun WP en PROCESSING → Fin du pipeline.")
        return None, None, None

    if file_processed:
        original_count = len(dict_processing)
        dict_processing = {
            rid: meta for rid, meta in dict_processing.items()
            if meta["file_processed"] == file_processed
        }
        logging.info(f"🎯 Filtrage --file_processed : {original_count} → {len(dict_processing)}")
        if not dict_processing:
            logging.info("Aucun report_id PROCESSING ne correspond au filtre.")
            return None, None, None

    # =============================================================
    # 2️⃣ Boucle principale par report_id PROCESSING
    # =============================================================
    for report_id, meta in dict_processing.items():

        fp = rwa.validate_file_processed(meta["file_processed"])
        tracking_entry = tracking.get(report_id, {})
        tracking_entry["start_time"] = start_time
        tracking_entry["file_processed"] = fp

        logging.info(f"📄 Traitement du WP : {fp} (report_id={report_id})")

        # ---------------------------------------------------------
        # Étape 1 : OCR / Extraction texte
        # ---------------------------------------------------------
        txt_path = pipe_steps.step_run_3_pipe_ocr(fp)

        if not txt_path or not txt_path.exists():
            had_report_error = True
            logging.error("❌ OCR impossible → status ERROR")
            rwa.update_report_status(conf.RWA_API_KEY, report_id, "ERROR")
            continue

        tracking_entry["ocr_txt_path"] = str(txt_path)
        tracking[report_id] = tracking_entry
        _save_tracking(tracking)

        # ---------------------------------------------------------
        # Étape 1bis : Upload du PDF paginé (référence) vers l’API applicative
        # ---------------------------------------------------------
        # Objectif :
        # - envoyer le WP d’origine après pagination (PDF _paginated)
        # - l’associer au report_id côté API comme fichier "reference"
        # - ne PAS bloquer tout le pipeline si cet upload échoue (best effort)
        pdf_paginated_path = conf.paginated_folder / f"{fp}_paginated.pdf"
        if pdf_paginated_path.exists():
            try:
                resp_post = rwa.post_wp_paginated(
                    api_key=conf.RWA_API_KEY,
                    report_id=report_id,
                    pdf_paginated_path=pdf_paginated_path
                )
                # Le tracking conserve le statut utile sans archiver la réponse API brute.
                tracking_entry["wp_paginated_uploaded"] = (
                    isinstance(resp_post, dict) and resp_post.get("message") == "success"
                )
                tracking_entry["wp_paginated_response_message"] = (
                    resp_post.get("message") if isinstance(resp_post, dict) else None
                )
                tracking[report_id] = tracking_entry
                _save_tracking(tracking)

                if tracking_entry["wp_paginated_uploaded"]:
                    logging.info(f"✅ WP paginé uploadé : {pdf_paginated_path.name} (report_id={report_id})")
                else:
                    logging.warning("⚠️ Upload du WP paginé non confirmé par le backend.")

            except Exception as e:
                # Best effort : on loggue, on marque dans le tracking, puis on continue.
                tracking_entry["wp_paginated_uploaded"] = False
                tracking_entry["wp_paginated_error"] = str(e)
                tracking[report_id] = tracking_entry
                _save_tracking(tracking)
                logging.warning(f"⚠️ Upload WP paginé échoué (pipeline continue) : {e}", exc_info=True)
        else:
            # Le PDF paginé n’existe pas → on ne poste pas.
            tracking_entry["wp_paginated_uploaded"] = False
            tracking_entry["wp_paginated_missing_path"] = str(pdf_paginated_path)
            tracking[report_id] = tracking_entry
            _save_tracking(tracking)
            logging.warning(f"⚠️ PDF paginé introuvable → pas de POST : {pdf_paginated_path}")


        # ---------------------------------------------------------
        # Étape 2 : Framework
        # ---------------------------------------------------------
        fw_json, df_fw = pipe_steps.step_recover_framework(fp)

        if df_fw is None or df_fw.empty:
            had_report_error = True
            logging.error("❌ Framework introuvable → ERROR")
            rwa.update_report_status(conf.RWA_API_KEY, report_id, "ERROR")
            continue

        tracking_entry["framework_loaded"] = True
        tracking[report_id] = tracking_entry
        _save_tracking(tracking)

        # ---------------------------------------------------------
        # Étape 3 : Calcul des chunks
        # ---------------------------------------------------------
        total_pages = rwa.count_total_pages_in_txt(txt_path)
        num_chunks = math.ceil(total_pages / 2)
        logging.info(f"📚 {fp} contient {total_pages} pages → {num_chunks} chunks")

        # ---------------------------------------------------------
        # Étape 4 : Stage 1 (retry + failover gérés au niveau chunk)
        # ---------------------------------------------------------
        try:
            df_report, entries, monitoring_stage1 = pipe_steps.step_run_llm_analysis_with_failover(
                file_processed=fp,
                num_wp_blocks=num_chunks
            )
        except Exception as e:
            # Échec fatal du Stage 1 :
            # - retry + failover chunk épuisés
            # - on marque le report en ERROR
            had_report_error = True
            logging.error("❌ Stage 1 exception → ERROR : %s", e, exc_info=True)
            rwa.update_report_status(conf.RWA_API_KEY, report_id, "ERROR")
            continue

        # Sécurité supplémentaire : Stage 1 ne doit jamais renvoyer une liste vide
        if not entries:
            had_report_error = True
            logging.error("❌ Stage 1 vide → ERROR")
            rwa.update_report_status(conf.RWA_API_KEY, report_id, "ERROR")
            continue

        # ✅ Stage 1 terminé avec succès → tracking OK
        tracking_entry["stage1_done"] = True
        tracking[report_id] = tracking_entry
        _save_tracking(tracking)

        # ---------------------------------------------------------
        # Monitoring : ajout des IDs API (best effort)
        # ---------------------------------------------------------
        try:
            rwa.update_latest_monitoring_file(
                file_processed=fp,
                updates={
                    "report_id": report_id,
                    "project_id": meta.get("project_id"),
                }
            )
        except Exception as e:
            logging.warning(f"⚠️ Monitoring update (report_id/project_id) ignoré : {e}")


        # ---------------------------------------------------------
        # Étape 5 : Stage 2
        # ---------------------------------------------------------
        if getattr(conf, "ENABLE_STAGE2", True):
            try:
                pipe_steps.step_run_stage2_delete_redundancy(fp)
                tracking_entry["stage2_done"] = True
            except Exception as e:
                had_report_error = True
                logging.error("❌ Stage 2 exception → ERROR : %s", e, exc_info=True)
                rwa.update_report_status(conf.RWA_API_KEY, report_id, "ERROR")
                tracking[report_id] = tracking_entry
                _save_tracking(tracking)
                continue


        # ---------------------------------------------------------
        # Étape 6 : publication du rapport principal
        # Cette étape correspond au flux métier client :
        # - JSON final Stage 2 enrichi
        # - POST détaillé des framework_id en base
        # - si succès, le report peut être marqué COMPLETED
        # ---------------------------------------------------------
        try:
            # prépare le JSON final pour POST DB :
            # - convert_flags_in_json() pour produire le JSON _flaginternals_simplified
            # - post_entries_from_llm() pour l'appel API
            post_entries_result = pipe_steps.step_post_entries(fp, report_id)
            logging.info(f"📤 POST DB réussi pour {report_id}")
        except Exception as e:
            had_report_error = True
            logging.error("❌ POST DB échec : %s", e)
            rwa.update_report_status(conf.RWA_API_KEY, report_id, "ERROR")
            continue


        tracking_entry["post_db"] = True
        tracking[report_id] = tracking_entry
        _save_tracking(tracking)

        # ---------------------------------------------------------
        # Étape 7 : statut métier principal du report
        # IMPORTANT :
        # COMPLETED signifie ici que le pipeline client principal
        # (Agents 1 + 2 + POST DB principal) est terminé avec succès.
        # L'Agent 3 est un post-traitement interne non bloquant.
        # ---------------------------------------------------------
        if not rwa.update_report_status(conf.RWA_API_KEY, report_id, "COMPLETED"):
            had_report_error = True
            logging.error("❌ Le backend n’a pas confirmé le statut COMPLETED.")
            continue
        logging.info("🎉 Rapport principal → COMPLETED")

        # ---------------------------------------------------------
        # Étape 8 : Agent 3 (post-traitement interne)
        # Cette étape produit une synthèse exécutive complémentaire :
        # - génération des sections critical_weakness / strategic_recommendations
        # - archivage des artefacts Agent 3
        # - POST API dédié de la synthèse
        # En cas d'échec, le statut principal du report n'est pas modifié.
        # ---------------------------------------------------------
        try:
            agent3_result = pipe_steps.step_run_agent3_summary(
                file_processed=fp,
                report_id=report_id,
                enriched_path=post_entries_result["enriched_path"],
            )
            logging.info(f"🧠 Agent 3 réussi pour {report_id}")
        except Exception as e:
            logging.error(f"❌ Agent 3 échec non bloquant : {e}", exc_info=True)
            tracking_entry["agent3_done"] = False
            tracking_entry["agent3_error"] = type(e).__name__
            tracking[report_id] = tracking_entry
            _save_tracking(tracking)
            # L’échec de cette synthèse complémentaire ne modifie pas le statut principal.
            continue

        tracking_entry["agent3_done"] = True
        tracking_entry["agent3_output_json_path"] = agent3_result["final_output_path"]
        tracking[report_id] = tracking_entry
        _save_tracking(tracking)

    # FIN pipeline
    logging.info("🧠 FIN DU PIPELINE RWA")
    if had_report_error:
        raise RuntimeError("Au moins un rapport a échoué pendant le pipeline principal.")
    return df_report, entries, monitoring_stage1


# =====================================================================
# CLI
# =====================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file_processed", type=str, default=None)
    args = parser.parse_args()

    try:
        run_pipeline(args.file_processed)
    except Exception as e:
        # Une erreur non interceptée doit être visible par l'ordonnanceur via un code non nul.
        logging.critical(
            f"💥 Erreur fatale non interceptée dans pipeline.py : {e}",
            exc_info=True,
        )
        raise SystemExit(1)


