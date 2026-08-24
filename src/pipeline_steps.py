# -*- coding: utf-8 -*-
# pipeline_steps.py
from datetime import datetime
import glob
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Set, Tuple

import rwa_methods as rwa
import document_processing as pipe_ocr
import stage2_agent as stage2
import config as conf




# ─────────────────────────────────────────────────────────────
# Étape 1a – requete DB status PENDING => x report_id + x url => new WP à traiter en local
# ─────────────────────────────────────────────────────────────
@rwa.log_execution_time
def step_recover_WPs_pending() -> Dict[str, Dict[str, str]]:
    """
    step_recover_WPs_pending : récupère tous les report_id en statut PENDING depuis l’API RWA, télécharge les fichiers
    et les stocke localement dans input_whitepaper_folder.

    fonctions appelées : rwa.get_status_projects(), rwa.get_file_from_report_id(), logging.info(), logging.error(), logging.warning()

    :return: Dict[str, Dict[str, str]], mapping report_id → {'project_id': str, 'file_processed': str}

    :raises ValueError: si project_id ou report_id est manquant
    """
    try:
        logging.info("Début de la récupération des WPs en status PENDING")
        projects, _ = rwa.get_status_projects(conf.RWA_API_KEY)

        if not projects:
            logging.info("Aucun projet PENDING trouvé.")
            return {}


        work_dict: Dict[str, Dict[str, str]] = {}
        seen_reports: Set[str] = set()

        # Parcourt chaque projet retourné par l’API
        for proj in projects:
            project_id = proj.get("project_id")
            if not project_id:
                logging.error("project_id manquant dans le projet %s", proj)
                raise ValueError(f"project_id manquant pour l'entrée: {proj}")

            reports = proj.get("reports")
            if not isinstance(reports, list):
                logging.warning("Pas de rapports ou format inattendu pour project_id=%s", project_id)
                continue

            # Parcourt chaque report dans le projet
            for report in reports:
                report_id = report.get("report_id")
                if not report_id:
                    logging.error("report_id manquant pour project_id=%s, report=%s", project_id, report)
                    raise ValueError(f"report_id manquant pour project_id={project_id}")

                # 🛑 Ignorer les report_id qui ne sont pas en PENDING
                status = report.get("status", "").upper()
                if status != "PENDING":
                    logging.info(f"⏩ Ignoré report_id={report_id} avec status={status}")
                    continue
                # Évite les doublons si même report_id présent plusieurs fois
                if report_id in seen_reports:
                    logging.info("report_id %s déjà traité, on saute.", report_id)
                    continue

                seen_reports.add(report_id)

                # 📥 Téléchargement du fichier source du whitepaper
                logging.info("- Récupération Infos new WP pour project_id=%s, report_id=%s", project_id, report_id)
                try:
                    downloaded_path = rwa.get_file_from_report_id(
                        conf.RWA_API_KEY,
                        project_id=project_id,
                        report_id=report_id,
                        download_folder=str(conf.input_whitepaper_folder)
                    )
                except Exception as e:
                    logging.error("Erreur lors du téléchargement du report_id=%s", report_id, exc_info=True)
                    # en cas d’échec du subprocess, on remonte le statut en ERROR
                    rwa.update_report_status(api_key=conf.RWA_API_KEY, report_id=report_id, status="ERROR")

                    continue

                if not downloaded_path:
                    logging.warning("get_file_from_report_id a renvoyé un chemin vide pour report_id=%s", report_id)
                    # en cas d’échec du subprocess, on remonte le statut en ERROR
                    rwa.update_report_status(api_key=conf.RWA_API_KEY, report_id=report_id, status="ERROR")

                    continue

                file_processed = Path(downloaded_path).stem
                logging.info("* Téléchargement réussi: %s ", downloaded_path)

                # Ajoute ce report_id au dictionnaire de travail
                work_dict[report_id] = {
                    "project_id": project_id,
                    "file_processed": file_processed
                }

        logging.info("Récupération terminée, total WPs traités: %d", len(work_dict))
        return work_dict

    except Exception:
        logging.exception("Échec de la récupération des WPs PENDING.")
        raise


@rwa.log_execution_time
def step_recover_WPs_processing() -> Dict[str, Dict[str, str]]:
    """
    step_recover_WPs_processing : récupère tous les report_id en statut PROCESSING depuis l’API RWA,
    télécharge les fichiers (ou utilise celui déjà présent) et les stocke localement dans input_whitepaper_folder.

    fonctions appelées : rwa.get_status_projects(), rwa.get_file_from_report_id(), logging.info(), logging.error(), logging.warning()

    :return: Dict[str, Dict[str, str]], mapping report_id → {'project_id': str, 'file_processed': str}
    :raises RuntimeError: si l’appel à get_status_projects échoue ou retourne un format inattendu
    :raises ValueError: si project_id ou report_id est manquant
    :raises IOError: si le téléchargement échoue ou le chemin est invalide
    """
    try:
        logging.info("Début de la récupération des WPs en status PROCESSING")
        projects, _ = rwa.get_status_projects(
            api_key=conf.RWA_API_KEY,
            status="PROCESSING",
        )

        if not projects:
            logging.info("Aucun projet PROCESSING trouvé.")
            return {}

        work_dict: Dict[str, Dict[str, str]] = {}
        seen_reports: Set[str] = set()

        # Parcourt chaque projet retourné par l’API
        for proj in projects:
            project_id = proj.get("project_id")
            if not project_id:
                logging.error("project_id manquant dans le projet %s", proj)
                raise ValueError(f"project_id manquant pour l'entrée: {proj}")

            reports = proj.get("reports")
            if not isinstance(reports, list):
                logging.warning("Pas de rapports ou format inattendu pour project_id=%s", project_id)
                continue

            for report in reports:
                report_id = report.get("report_id")
                if not report_id:
                    logging.error("report_id manquant pour project_id=%s, report=%s", project_id, report)
                    raise ValueError(f"report_id manquant pour project_id={project_id}")

                status = report.get("status", "").upper()
                if status != "PROCESSING":
                    logging.info(f"⏩ Ignoré report_id={report_id} avec status={status}")
                    continue
                if report_id in seen_reports:
                    logging.info("report_id %s déjà traité, on saute.", report_id)
                    continue

                seen_reports.add(report_id)

                logging.info("- Récupération Infos WP PROCESSING pour project_id=%s, report_id=%s",
                             project_id, report_id)
                try:
                    downloaded_path = rwa.get_file_from_report_id(
                        conf.RWA_API_KEY,
                        project_id=project_id,
                        report_id=report_id,
                        download_folder=str(conf.input_whitepaper_folder)
                    )
                except Exception as e:
                    logging.error("Erreur lors du téléchargement du report_id=%s", report_id, exc_info=True)
                    # informer l’API du statut ERROR puis passer au report suivant
                    rwa.update_report_status(api_key=conf.RWA_API_KEY, report_id=report_id, status="ERROR")
                    continue


                if not downloaded_path:
                    logging.warning("get_file_from_report_id a renvoyé un chemin vide pour report_id=%s", report_id)
                    # informer l’API du statut ERROR puis passer au report suivant
                    rwa.update_report_status(api_key=conf.RWA_API_KEY, report_id=report_id, status="ERROR")
                    continue

                file_processed = Path(downloaded_path).stem
                logging.info("* Téléchargement réussi: %s", downloaded_path)

                work_dict[report_id] = {
                    "project_id": project_id,
                    "file_processed": file_processed
                }

        logging.info("Récupération PROCESSING terminée, total WPs traités: %d", len(work_dict))
        return work_dict

    except Exception:
        logging.exception("Échec de la récupération des WPs PROCESSING.")
        raise


# ─────────────────────────────────────────────────────────────
# Étape 1b – Récupération et sauvegarde du framework sur la db et sauvegarde locale
# ─────────────────────────────────────────────────────────────
@rwa.log_execution_time
def step_recover_framework(file_processed: str = None):
    """
    step_recover_framework : récupère le framework d’évaluation RWA via l’API, et sauvegarde le JSON original localement
    sous le nom {file_processed}_framework_original.json dans report_folder.

    fonctions appelées : rwa.get_whitepaper_framework(), json.dump(), logging.info()

    :param file_processed: str, nom de base du fichier traité (sans extension) pour nommer le fichier de framework sauvegardé
    :return: tuple (framework_json, framework_df)
        - framework_json: list[dict], contenu brut du framework depuis l’API
        - framework_df: pd.DataFrame, version tabulaire du framework pour traitement LLM

    """
    if file_processed is not None:
        file_processed = rwa.validate_file_processed(file_processed)

    # Appel à l’API pour obtenir le framework du whitepaper
    json_data, df = rwa.get_whitepaper_framework(conf.RWA_API_KEY)
    if json_data is None or df is None:
        logging.error("Impossible de récupérer le framework pour %s", file_processed)
        return None, None

    # ✅ Sauvegarde du framework original pour référence future
    if file_processed:
        # Construction du chemin pour sauvegarder le framework JSON localement
        save_path = conf.report_folder / f"{file_processed}_framework_original.json"
        # L'écriture atomique évite de conserver un framework tronqué après interruption.
        rwa.save_json_file(save_path, json_data)
        logging.info(f"📎 Framework original archivé : {save_path.name}")

    return json_data, df



# ─────────────────────────────────────────────────────────────
# Étape 2 – produire fichier TXT (normal ou OCR) / extraction et déplacement du fichier
# ─────────────────────────────────────────────────────────────
@rwa.log_execution_time
def step_run_3_pipe_ocr(file_processed: str) -> Path:
    """
    step_run_3_pipe_ocr : extraction de texte ou OCR sur un fichier DOCX, DOC ou PDF.
    Sélectionne et appelle le pipeline adapté selon l’extension :
      • .docx / .doc → pipeline_docx_to_txt
      • .pdf (vectoriel) → pipeline_pdf_vectoriel_to_txt
      • .pdf (raster) → pipeline_pdf_raster_to_txt

    :param file_processed: identifiant du fichier sans extension (ex: "rapport_XYZ")
    :return: Path vers le fichier .txt généré dans ocr_folder, ou None si aucun fichier n’a été trouvé ou en cas d’erreur d’OCR
    :raises ValueError: si l’extension n’est pas supportée
    """
    file_processed = rwa.validate_file_processed(file_processed)
    logging.info("step_run_3_pipe_ocr: début du traitement documentaire")

    # Construction des chemins possibles dans le dossier d’entrée, en ignorant la casse :
    # Sur Linux, "fichier.PDF" != "fichier.pdf". On normalise donc la comparaison.
    src_folder = conf.input_whitepaper_folder

    def _find_source_by_ext_case_insensitive(ext: str) -> Path | None:
        """
        Retourne le premier fichier du dossier src_folder dont :
        - le stem correspond à file_processed (comparaison insensible à la casse)
        - le suffix correspond à ext (comparaison insensible à la casse)
        """
        target_stem = file_processed.lower()
        target_ext = ext.lower()
        candidates: list[Path] = []

        for p in src_folder.iterdir():
            if not p.is_file():
                continue
            if p.stem.lower() != target_stem:
                continue
            if p.suffix.lower() != target_ext:
                continue
            candidates.append(p)

        # S'il y a plusieurs candidats (rare mais possible), on choisit le plus récent.
        if candidates:
            candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            return candidates[0]
        return None

    # On cherche les fichiers en respectant la priorité actuelle : DOCX → DOC → PDF
    path_docx = _find_source_by_ext_case_insensitive(".docx")
    path_doc = _find_source_by_ext_case_insensitive(".doc")
    path_pdf = _find_source_by_ext_case_insensitive(".pdf")

    # 2) Cas DOCX
    if path_docx is not None:
        logging.info(f"Fichier DOCX détecté : {path_docx.name}")
        try:
            pipe_ocr.pipeline_docx_to_txt(path_docx, conf.paginated_folder, conf.ocr_folder)
            out_txt = conf.ocr_folder / f"{path_docx.stem}.txt"
            logging.info(f"TXT généré pour DOCX → {out_txt}")
            return out_txt
        except Exception:
            logging.exception(f"Erreur pipeline DOCX sur {path_docx.name}")
            return None

    # 3) Cas DOC
    if path_doc is not None:
        logging.info(f"Fichier DOC détecté : {path_doc.name}")
        try:
            pipe_ocr.pipeline_docx_to_txt(path_doc, conf.paginated_folder, conf.ocr_folder)
            out_txt = conf.ocr_folder / f"{path_doc.stem}.txt"
            logging.info(f"TXT généré pour DOC → {out_txt}")
            return out_txt
        except Exception:
            logging.exception("Erreur pipeline DOC sur %s", path_doc.name)
            return None

    # 4) Cas PDF
    if path_pdf is not None:
        logging.info(f"Fichier PDF détecté : {path_pdf.name}")
        try:
            # Détection raster vs vectoriel
            is_rast, page_count = pipe_ocr.is_raster_pdf(path_pdf)
            logging.info(f"is_raster_pdf → is_raster={is_rast}, pages={page_count}")

            if is_rast:
                logging.info("PDF raster → lancement pipeline OCR")
                out_txt = pipe_ocr.pipeline_pdf_raster_to_txt(path_pdf, conf.paginated_folder, conf.ocr_folder)
                logging.info(f"TXT généré par pipeline raster → {out_txt}")
                return out_txt

            logging.info("PDF vectoriel natif → extraction sans OCR")
            pipe_ocr.pipeline_pdf_vectoriel_to_txt(path_pdf, conf.paginated_folder, conf.ocr_folder)
            out_txt = conf.ocr_folder / f"{path_pdf.stem}.txt"
            logging.info(f"TXT généré par pipeline vecteur → {out_txt}")
            return out_txt
        except Exception:
            logging.exception(f"Erreur pipeline PDF sur {path_pdf.name}")
            return None

    # 5) Aucun fichier trouvé
    msg = (f"Aucun fichier trouvé pour '{file_processed}' "
           f"dans {src_folder} avec extension .docx, .doc ou .pdf (insensible à la casse)")
    logging.error(msg)
    return None



@rwa.log_execution_time
def step_run_llm_analysis_with_failover(
    file_processed: str,
    num_wp_blocks: int = 5,
) -> Tuple[Any, list[dict], dict]:
    """
    Orchestration du Stage 1 (analyse LLM par chunk).

    Cette étape :
    - prépare et déclenche l’analyse Stage 1 sur un report donné,
    - délègue le retry (max 5) et le failover multi-provider au niveau des chunks
      via build_llm_analysis_by_framework_blocks_stage1(),
    - ne gère pas directement la logique de bascule entre providers.

    En cas d’échec fatal du Stage 1, une exception est levée afin de marquer
    le report en ERROR et de permettre au pipeline de poursuivre avec le report suivant.
    """
    file_processed = rwa.validate_file_processed(file_processed)

    # On utilise uniquement le premier provider à titre informatif.
    # Le retry et le failover sont désormais gérés AU NIVEAU CHUNK
    # dans build_llm_analysis_by_framework_blocks_stage1().
    provider, model = conf.STAGE1_PROVIDER_ORDER[0]

    logging.info(
        f"🚀 [Stage 1] Démarrage analyse LLM "
        f"(failover géré par chunk) | provider initial={provider.upper()} | model={model}"
    )

    # Lancement du Stage 1 complet.
    # Toute exception levée ici signifie :
    # - échec après retry + failover chunk
    # - le pipeline appelant doit passer le report_id en ERROR
    df_fw, entries, monitoring_data = rwa.build_llm_analysis_by_framework_blocks_stage1(
        file_processed=file_processed,
        num_wp_blocks=num_wp_blocks,
        model=model,
        provider=provider,
    )

    return df_fw, entries, monitoring_data




@rwa.log_execution_time
def step_run_stage2_delete_redundancy(file_processed: str):
    """
    Étape post-Stage1 : exécute l’Agent 2 (delete redundancy).

    Le choix du provider et du modèle pour le Stage 2 est géré
    en interne dans stage2_agent via conf.STAGE2_PROVIDER_ORDER.
    Cette fonction se contente de :
      - logguer le provider/modèle choisis pour la lisibilité
      - déléguer l'orchestration à stage2.run_stage2_delete_redundancy().
    """
    file_processed = rwa.validate_file_processed(file_processed)
    try:
        # récupération lisible du provider/modèle choisis pour Stage 2
        first_provider, first_model = stage2.get_stage2_provider_and_model()
        logging.info(
            f"🔁 Post-traitement : Stage 2 — Delete Redundancy "
            f"(start {first_provider.upper()} | modèle {first_model})"
        )
    except Exception as e:
        logging.warning(
            f"⚠️ Impossible de récupérer provider/modèle Stage 2 via get_stage2_provider_and_model() : {e}"
        )
        # on loggue tout de même une info minimale
        first_provider, first_model = "unknown", "unknown"
        logging.info("🔁 Post-traitement : Stage 2 — Delete Redundancy (start, provider inconnu)")

    # appel direct : la fonction Stage 2 ne prend que file_processed
    return stage2.run_stage2_delete_redundancy(file_processed)


# ---------------------------------------------------------
# Step finale du flux principal :
# - sélection du dernier JSON Stage 2
# - ajout de flag_internals + flag unique par metric_id
# - contrôle pré-POST
# - POST détaillé des résultats principaux vers l'API
# ---------------------------------------------------------
@rwa.log_execution_time
def step_post_entries(file_processed: str, report_id: str):
    """
    Étape finale : enrichit le JSON Stage 2, ajoute flag_internals,
    crée le payload, et poste chaque framework_id vers la API RWA.
    """

    file_processed = rwa.validate_file_processed(file_processed)

    # Recherche du dernier JSON Stage 2 horodaté.
    stage2_files = sorted(
        glob.glob(str(conf.report_stage_2_folder / f"{file_processed}_*_stage2_deduplicated_with_justifications.json")),
        key=os.path.getmtime,
        reverse=True
    )
    if not stage2_files:
        raise FileNotFoundError(f"Aucun JSON Stage2 trouvé pour {file_processed}")
    json_stage2 = Path(stage2_files[0])

    logging.info(f"🚀 Lancement du POST des entrées vers la API RWA pour {file_processed}")

    # Dans le json final : ajoute un champ 'flag_internals' et calcul le flag unique par metric_ID
    enriched_path = rwa.convert_flags_in_json(json_stage2)

    # Lecture du JSON final pré-POST (celui qui sera réellement envoyé à la DB)
    with open(enriched_path, "r", encoding="utf-8") as f:
        entries = json.load(f)

    # -------------------------------------------------------------------
    # Pré-contrôle (avant POST) : vérifie la présence de tous les sous-points
    # '(1)', '(2)', ... attendus d'après llm_prompt dans la liste output.
    #
    # On enregistre un résultat compact :
    #   - output_with_missing_subpoints = [None] si tout est OK
    #   - sinon = [{"framework_id": ..., "missing_subpoints": ["(2)", "(4)"]}, ...]
    #
    # Important : le monitoring ne doit jamais casser le pipeline.
    # -------------------------------------------------------------------
    pre_post_subpoints_checked_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    output_with_missing_subpoints_error = None  # si reste None => le controle s'est bien passé

    # -------------------------------------------------------------------
    # Stats flags calculées sur le JSON final pré-POST (entries en mémoire)
    # -------------------------------------------------------------------
    try:
        flag_stats = rwa.compute_prepost_flag_stats(entries)
    except Exception as e:
        # Best effort : ne jamais bloquer le POST pour du monitoring
        flag_stats = {"framework_stats": None, "flag_internals_stats": None}
        logging.warning(f"⚠️ Impossible de calculer les stats de flags pré-POST : {e}")


    try:
        output_with_missing_subpoints = rwa.compute_output_with_missing_subpoints(entries)
    except Exception as e:
        # On conserve une trace minimale d'erreur sans bloquer le POST
        output_with_missing_subpoints = [{"framework_id": None, "missing_subpoints": ["(compute_failed)"]}]
        output_with_missing_subpoints_error = str(e)[:200]
        logging.warning(f"⚠️ Pré-contrôle sous-points impossible : {e}")

    # -------------------------------------------------------------------
    # Écriture UNIQUE du monitoring : chemins + contrôle sous-points
    # (report_id / project_id sont déjà ajoutés ailleurs dans pipeline.py)
    # -------------------------------------------------------------------
    rwa.update_latest_monitoring_file(
        file_processed=file_processed,
        updates={
            "stage2_final_json_path": str(json_stage2.resolve()),
            "pre_post_json_path": str(Path(enriched_path).resolve()),
            "framework_stats": flag_stats.get("framework_stats"),
            "flag_internals_stats": flag_stats.get("flag_internals_stats"),
            "output_with_missing_subpoints": output_with_missing_subpoints,
            "pre_post_subpoints_checked_at": pre_post_subpoints_checked_at,
            "output_with_missing_subpoints_error": output_with_missing_subpoints_error,
        }
    )

    # NOTE : ai_model correspond ici au modèle par défaut configuré.
    # Le Stage 1 / Stage 2 pouvant utiliser plusieurs providers/modèles via failover,
    # la traçabilité fine est assurée séparément dans les fichiers de monitoring LLM.
    result = rwa.post_entries_from_llm(
        api_key=conf.RWA_API_KEY,
        report_id=report_id,
        entries=entries,
        ai_model=conf.DEFAULT_LLM_MODEL,
    )

    total = int(result.get("total", 0))
    success_count = int(result.get("success", 0))
    error_count = int(result.get("error", 0))

    # Le POST détaillé fait partie du chemin métier principal : un batch vide ou
    # partiellement publié doit remonter une exception afin que pipeline.py soit
    # l'unique composant à décider du statut ERROR / COMPLETED du rapport.
    if total <= 0:
        raise RuntimeError("POST DB refusé : aucune entrée finale à publier.")
    if error_count != 0 or success_count != total:
        raise RuntimeError(
            f"POST DB incomplet : {success_count}/{total} entrées publiées, "
            f"{error_count} erreur(s)."
        )

    return {
        "post_result": result,
        "enriched_path": str(Path(enriched_path).resolve()),
        "stage2_json_path": str(json_stage2.resolve()),
    }


# ---------------------------------------------------------
# Step Agent 3 :
# - consomme le JSON enrichi pré-POST du flux principal
# - retire les métriques et sous-points green
# - génère la synthèse exécutive en JSON strict
# - poste automatiquement les 2 sections si génération valide
# ---------------------------------------------------------
@rwa.log_execution_time
def step_run_agent3_summary(file_processed: str, report_id: str, enriched_path: str):
    """
    Étape Agent 3 :
    - consomme le JSON enrichi pré-POST produit après Stage 2,
    - nettoie les points green,
    - génère les 2 sections de synthèse,
    - archive les artefacts,
    - met à jour le monitoring.
    """
    file_processed = rwa.validate_file_processed(file_processed)
    logging.info("🧠 Lancement Agent 3")

    result = rwa.run_agent3_from_enriched_json(
        enriched_path=Path(enriched_path),
        report_id=report_id,
        file_processed=file_processed,
        output_root_dir=conf.agent3_outputs_folder,
        post_to_api=True,
    )

    rwa.update_latest_monitoring_file(
        file_processed=file_processed,
        updates={
            "agent3_source_json_path": str(Path(enriched_path).resolve()),
            "agent3_output_json_path": result["final_output_path"],
            "agent3_audit_json_path": result["audit_output_path"],
            "agent3_provider": result["provider_used"],
            "agent3_model": result["model_used"],
            "agent3_was_repaired": result["was_repaired"],
            "agent3_repairs_applied": result["repairs_applied"],
            "agent3_post_results_count": len(result["post_results"]),
        }
    )

    logging.info("✅ Agent 3 terminé")
    return result
