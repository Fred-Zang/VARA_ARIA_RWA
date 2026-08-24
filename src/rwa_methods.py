# -*- coding: utf-8 -*-
"""Fonctions métier du pipeline RWA public.

Ce module regroupe l'accès API, l'orchestration LLM, les validations et la
traçabilité conservés dans la démonstration publique. Les prompts et données
métier de production ne sont pas inclus dans ce dépôt.
"""

import hashlib
import ipaddress
import json
import logging
import math
import os
import random
import re
import socket
import sys
import tempfile
import time
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from difflib import SequenceMatcher
from functools import wraps
from glob import glob
from itertools import groupby
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from google import genai
from google.genai import types as genai_types
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import RateLimitError as OpenAIRateLimitError

import config as conf
import prompts as prompt_rwa


def _gemini_generate_json(model: str, prompt_text: str, max_output_tokens: int):
    """Appelle Gemini via le SDK Google GenAI courant avec un timeout explicite."""
    if not conf.GEMINI_API_KEY:
        raise RuntimeError("Clé Gemini manquante.")

    # Le SDK Google GenAI exprime HttpOptions.timeout en millisecondes.
    http_options = genai_types.HttpOptions(
        timeout=conf.LLM_REQUEST_TIMEOUT_SECONDS * 1000
    )
    generation_config = genai_types.GenerateContentConfig(
        temperature=0,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json",
    )
    with genai.Client(api_key=conf.GEMINI_API_KEY, http_options=http_options) as client:
        return client.models.generate_content(
            model=model,
            contents=prompt_text,
            config=generation_config,
        )


def _messages_to_prompt_text(messages: List[Any]) -> str:
    """Concatène les contenus des messages sans dépendre d'un provider LLM."""
    return "\n".join(
        message.content
        if hasattr(message, "content")
        else str(message.get("content", ""))
        for message in messages
    )


def _gemini_usage_tokens(response: Any) -> Tuple[int, int, int]:
    """Extrait les compteurs du SDK Google GenAI sans journaliser le contenu."""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return 0, 0, 0

    if isinstance(usage, dict):
        prompt = int(usage.get("prompt_token_count", 0) or 0)
        completion = int(usage.get("candidates_token_count", 0) or 0)
        total = int(usage.get("total_token_count", prompt + completion) or 0)
    else:
        prompt = int(getattr(usage, "prompt_token_count", 0) or 0)
        completion = int(getattr(usage, "candidates_token_count", 0) or 0)
        total = int(getattr(usage, "total_token_count", prompt + completion) or 0)
    return prompt, completion, total


def _gemini_finish_reason(response: Any) -> str:
    """Retourne le motif de fin du premier candidat Gemini sous forme textuelle."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return "UNKNOWN"
    value = getattr(candidates[0], "finish_reason", None)
    if value is None:
        return "UNKNOWN"
    name = getattr(value, "name", None)
    return str(name or value)


def validate_file_processed(value: str) -> str:
    """Valide l'identifiant local dérivé d'un nom de document téléchargé."""
    if not isinstance(value, str) or not value:
        raise ValueError("file_processed doit être une chaîne non vide.")
    if len(value) > 160 or not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError("file_processed contient des caractères ou une longueur non autorisés.")
    return value


# ──────────────────────────────────────────────────────────────────────────────
# Stage 0 : Préparations générales & utilitaires & préparation des données
# ──────────────────────────────────────────────────────────────────────────────

# @rwa.log_execution_time juste avant la focntion à timer

def log_execution_time(func):
    """
    log_execution_time : décorateur qui mesure et affiche dans les logs la durée d'exécution d'une fonction.
    fonctions appelées : time.time(), logging.info()

    :param func: function, fonction Python à envelopper pour mesurer le temps d'exécution
    :return: function, fonction enveloppée avec mesure de temps et logs d'exécution
    """


    @wraps(func)
    def wrapper(*args, **kwargs):
        """
        wrapper : fonction interne générée par le décorateur log_execution_time,
        utilisée pour envelopper dynamiquement une fonction cible afin de mesurer et logger son temps d'exécution.
        fonctions appelées : time.time(), logging.info(), func(*args, **kwargs)

        :param args: tuple, arguments positionnels passés à la fonction décorée
        :param kwargs: dict, arguments nommés passés à la fonction décorée
        :return: résultat de l’exécution de la fonction décorée
        """

        # Teste si l'encodage console accepte les emojis
        try:
            encoding_ok = sys.stdout.encoding.lower().startswith("utf")
        except Exception:
            encoding_ok = False

        emoji_start = "⏱ " if encoding_ok else "[START] "
        emoji_end = "✅ " if encoding_ok else "[END] "

        logging.info("")
        logging.info(f"{emoji_start}Début exécution : {func.__name__}")
        start_time = time.time()
        result = func(*args, **kwargs)
        duration = round(time.time() - start_time, 2)
        logging.info(f"{emoji_end}Fin de {func.__name__} en {duration} sec.")
        logging.info("")
        return result
    return wrapper






# Fonction utilitaire héritée, toujours utilisée dans le pipeline public
def get_whitepaper_framework(api_key: str, base_url: str = conf.API_BASE_URL) -> tuple[Optional[list[dict]], Optional[pd.DataFrame]]:
    """
    get_whitepaper_framework : récupère le framework d’évaluation RWA via l’API applicative, et le retourne au format JSON et DataFrame.
    fonctions appelées : requests.get(), pd.DataFrame()

    :param api_key: str, clé API d’authentification
    :param base_url: str, URL de base de l’API (par défaut : <RWA_API_BASE_URL>)
    :return: tuple (framework_json, framework_df)
        - framework_json: Optional[list[dict]], version JSON du framework (utile pour les prompts LLM)
        - framework_df: Optional[pd.DataFrame], version tabulaire pour traitement Python
    """

    url = f"{base_url}{conf.API_FRAMEWORK_PATH}"
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": api_key
    }

    try:
        response = requests.get(url, headers=headers, timeout=conf.API_REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict) and "data" in data:
            json_data = data["data"]
            df = pd.DataFrame(json_data)
            return json_data, df
        else:
            logging.warning("⚠️ Structure inattendue reçue depuis le backend.")
            return None, None

    except Exception as e:
        logging.error(f"❌ Error fetching whitepaper-framework: {e}")
        logging.warning("⚠️ Requête framework échouée ; les en-têtes d’authentification ne sont pas journalisés.")
        return None, None
# -------------------------------------------------------------------------------------
# 2) récupérerer GET la DB d'1 seul project et 1 seul report
# -------------------------------------------------------------------------------------
def get_report_from_project(api_key: str,
                             project_id: str,
                             report_id: str,
                             base_url: str = conf.API_BASE_URL) -> Optional[Dict]:
    """
    get_report_from_project : récupère le rapport spécifié par report_id pour le projet donné.
    fonctions appelées : requests.get()

    :param api_key: str, clé API d’authentification
    :param project_id: str, identifiant du projet
    :param report_id: str, identifiant du rapport
    :param base_url: str, URL de base (défaut : <RWA_API_BASE_URL>)
    :return: Optional[Dict], JSON complet du rapport ou None si non trouvé
    """

    url = f"{base_url}{conf.API_PROJECTS_PATH}"
    headers = {"X-Api-Key": api_key}
    params = {"project_id": project_id}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=conf.API_REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict) and "data" in data:
            for project in data["data"]:
                if project.get("project_id") == project_id:
                    for report in project.get("reports", []):
                        if report.get("report_id") == report_id:
                            return report
            logging.warning(f"⚠️ Rapport {report_id} non trouvé pour le projet {project_id}")
        else:
            logging.warning("⚠️ Structure inattendue reçue depuis le backend.")
    except Exception as e:
        logging.error(f"❌ Erreur récupération rapport {report_id} du projet {project_id}: {e}")
        logging.warning("⚠️ Échec de récupération du rapport ; les en-têtes d’authentification ne sont pas journalisés.")
    return None
def _validate_download_url(url: str) -> None:
    """Valide une URL distante avant chaque requête de téléchargement.

    Les redirections sont contrôlées séparément afin que chaque destination soit
    validée. Les noms d'hôte sont résolus et toute adresse non publique est
    refusée pour limiter les risques SSRF.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Le téléchargement du whitepaper exige une URL HTTPS valide.")
    if parsed.username or parsed.password:
        raise ValueError("Les identifiants intégrés dans une URL de téléchargement sont refusés.")

    host = parsed.hostname.lower().rstrip(".")

    if conf.REQUIRE_DOWNLOAD_ALLOWLIST and not conf.ALLOWED_DOWNLOAD_HOSTS:
        raise RuntimeError(
            "RWA_REQUIRE_DOWNLOAD_ALLOWLIST est activé mais RWA_ALLOWED_DOWNLOAD_HOSTS est vide."
        )

    if conf.ALLOWED_DOWNLOAD_HOSTS and not any(
        host == allowed or host.endswith(f".{allowed}")
        for allowed in conf.ALLOWED_DOWNLOAD_HOSTS
    ):
        raise ValueError("Le domaine de téléchargement n’est pas présent dans l’allow-list configurée.")

    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(
                host,
                parsed.port or 443,
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise ValueError("Le domaine de téléchargement ne peut pas être résolu.") from exc

    if not addresses:
        raise ValueError("Le domaine de téléchargement ne résout vers aucune adresse IP.")

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError("Adresse IP de téléchargement invalide.") from exc
        if not ip.is_global:
            raise ValueError("Une adresse IP non publique a été refusée pour le téléchargement.")


def _request_download_without_unchecked_redirects(url: str) -> requests.Response:
    """Suit un nombre limité de redirections en revalidant chaque destination."""
    current_url = url
    redirect_codes = {301, 302, 303, 307, 308}

    for redirect_count in range(conf.MAX_DOWNLOAD_REDIRECTS + 1):
        _validate_download_url(current_url)
        response = requests.get(
            current_url,
            stream=True,
            allow_redirects=False,
            timeout=conf.DOWNLOAD_REQUEST_TIMEOUT,
        )

        if response.status_code not in redirect_codes:
            response.raise_for_status()
            return response

        location = response.headers.get("Location")
        response.close()
        if not location:
            raise ValueError("Redirection de téléchargement sans en-tête Location.")
        if redirect_count >= conf.MAX_DOWNLOAD_REDIRECTS:
            raise ValueError("Nombre maximal de redirections de téléchargement dépassé.")
        current_url = urljoin(current_url, location)

    raise RuntimeError("Boucle de redirection inattendue.")


def _validate_downloaded_file_signature(path: Path, suffix: str) -> None:
    """Vérifie une signature minimale cohérente avec l'extension annoncée."""
    with path.open("rb") as stream:
        header = stream.read(8)

    expected = {
        ".pdf": lambda data: data.startswith(b"%PDF-"),
        ".docx": lambda data: data.startswith(b"PK\x03\x04"),
        ".doc": lambda data: data.startswith(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"),
    }
    checker = expected.get(suffix.lower())
    if checker is None or not checker(header):
        raise ValueError("La signature du fichier téléchargé ne correspond pas à son extension.")


def get_file_from_report_id(
    api_key: str,
    project_id: str,
    report_id: str,
    download_folder: str,
    base_url: str = conf.API_BASE_URL,
) -> Optional[str]:
    """Télécharge de manière contrôlée le document associé à un rapport."""

    def _sanitize_filename(original_name: str, fallback_stem: str) -> str:
        candidate = Path(original_name)
        suffix = candidate.suffix.lower()
        if suffix not in conf.ALLOWED_WHITEPAPER_EXTENSIONS:
            raise ValueError(f"Extension de whitepaper non autorisée : {suffix or '<aucune>'}")

        safe_stem = re.sub(r"[^A-Za-z0-9]+", "_", candidate.stem)
        safe_stem = re.sub(r"_+", "_", safe_stem).strip("_")
        if not safe_stem:
            safe_stem = re.sub(r"[^A-Za-z0-9]+", "_", fallback_stem)
            safe_stem = re.sub(r"_+", "_", safe_stem).strip("_") or "document"

        # Évite les noms pathologiquement longs tout en laissant la place à l'extension.
        return f"{safe_stem[:120]}{suffix}"

    report = get_report_from_project(api_key, project_id, report_id, base_url)
    if not report:
        logging.warning("Impossible de récupérer les métadonnées du rapport à télécharger.")
        return None

    file_info = report.get("file", {})
    url = file_info.get("url")
    file_name = file_info.get("file_name")
    if not isinstance(url, str) or not isinstance(file_name, str) or not url or not file_name:
        logging.warning("Informations de téléchargement manquantes dans le rapport.")
        return None

    try:
        safe_file_name = _sanitize_filename(file_name, fallback_stem=report_id)
        target_dir = Path(download_folder).resolve()
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        full_path = target_dir / safe_file_name

        response = _request_download_without_unchecked_redirects(url)
        temp_path: Optional[Path] = None
        try:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    advertised_size = int(content_length)
                except ValueError as exc:
                    raise ValueError("Content-Length invalide dans la réponse de téléchargement.") from exc
                if advertised_size < 0 or advertised_size > conf.MAX_WHITEPAPER_BYTES:
                    raise ValueError("Le whitepaper dépasse la taille maximale autorisée.")

            downloaded_bytes = 0
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target_dir,
                prefix=".download-",
                suffix=".part",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    downloaded_bytes += len(chunk)
                    if downloaded_bytes > conf.MAX_WHITEPAPER_BYTES:
                        raise ValueError("Le whitepaper dépasse la taille maximale autorisée.")
                    temp_file.write(chunk)
                temp_file.flush()
                os.fsync(temp_file.fileno())

            _validate_downloaded_file_signature(temp_path, Path(safe_file_name).suffix)
            os.replace(temp_path, full_path)
            temp_path = None
            logging.info("Document téléchargé et validé (%d octets).", downloaded_bytes)
            return str(full_path)
        finally:
            response.close()
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
        logging.error("Échec sécurisé du téléchargement : %s", exc)
        return None


def get_status_projects(
    api_key: str,
    status: str = "PENDING",
    base_url: str = conf.API_BASE_URL,
    uid: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Récupère les projets d'un statut donné et leurs identifiants de rapports.

    Un HTTP 400 est interprété comme « aucune donnée » conformément au contrat
    historique de l'API. Les autres erreurs réseau/HTTP sont propagées afin que
    l'ordonnanceur ne les confonde pas avec une file de travail vide.
    """
    url = f"{base_url}{conf.API_PROJECTS_PATH}"
    headers = {"X-Api-Key": api_key}
    params: Dict[str, str] = {"status": status}
    if uid:
        params["uid"] = uid
    if project_id:
        params["project_id"] = project_id

    response = requests.get(url, headers=headers, params=params, timeout=conf.API_REQUEST_TIMEOUT)
    if response.status_code == 400:
        logging.info("Aucun projet au statut %s.", status)
        return [], []
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("Le backend a renvoyé une réponse JSON invalide pour la liste des projets.") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Structure inattendue reçue lors de la récupération des projets.")

    projects: List[Dict[str, Any]] = payload["data"]
    report_ids = [
        report_id
        for project in projects
        for report in project.get("reports", [])
        if isinstance(report, dict)
        for report_id in [report.get("report_id")]
        if isinstance(report_id, str) and report_id
    ]
    return projects, report_ids


def update_report_status(api_key: str,
                         report_id: str,
                         status: str,
                         base_url: str = conf.API_BASE_URL
                         ) -> bool:
    """
    Envoie à l’API la mise à jour du statut d’un rapport RWA.

    :param api_key:   clé API
    :param report_id: identifiant du rapport
    :param status:    nouveau statut (ex: "COMPLETED")
    :param base_url:  URL de base de l’API
    :return:          True si succès, False sinon
    """
    url = f"{base_url}{conf.API_REPORT_UPDATE_PATH}"
    headers = {"X-Api-Key": api_key}
    body = {
        "report_id": report_id,
        "status":    status
    }

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=conf.API_REQUEST_TIMEOUT)
        # Vérifier et logger le code avant d'interpréter le JSON
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            logging.error(f"❌ HTTP {resp.status_code} lors de la mise à jour du statut.")
            return False

        # Lire le JSON (ou renvoyer le texte brut si invalid JSON)
        try:
            data = resp.json()
        except ValueError:
            logging.info(f"Réponse non-JSON reçue lors de la mise à jour du statut du rapport {report_id}.")
            return False

        if data.get("error") is None and data.get("message") == "success":
            logging.info(f"✅ Statut du rapport {report_id} mis à jour en '{status}'.")
            return True
        else:
            logging.error(f"⚠️ Le backend n’a pas confirmé la mise à jour du statut pour {report_id}.")
    except Exception as e:
        logging.error(f"❌ Erreur lors de la maj du statut pour {report_id} : {e}")

    return False



# ---------------------------------------------------------------------------------------------------
# ✅ Compte les pages dans le .txt OCR avec pagination "--- PAGE N ---"
# ---------------------------------------------------------------------------------------------------
def count_total_pages_in_txt(txt_path: Path) -> int:
    """Compte et valide les marqueurs de pagination d'un texte préparé.

    Le pipeline ne doit pas inventer un nombre de pages si le fichier est absent,
    illisible ou mal paginé : le calcul des chunks dépend directement de cette
    valeur. Une anomalie de pagination est donc considérée comme bloquante.
    """
    txt_path = Path(txt_path)
    if not txt_path.is_file():
        raise FileNotFoundError(f"Fichier texte paginé introuvable : {txt_path}")

    text = txt_path.read_text(encoding="utf-8")
    page_numbers = [int(value) for value in re.findall(r"--- PAGE (\d+) ---", text)]

    if not page_numbers:
        raise ValueError(
            f"Aucun marqueur de pagination '--- PAGE N ---' détecté dans {txt_path.name}."
        )

    expected = list(range(1, len(page_numbers) + 1))
    if page_numbers != expected:
        raise ValueError(
            "Pagination incohérente : les marqueurs doivent être uniques, "
            "continus et commencer à 1."
        )

    return len(page_numbers)


def post_wp_paginated(api_key: str, report_id: str, pdf_paginated_path: Path) -> dict:
    """
    post_wp_paginated : poste le whitepaper paginé au backend applicatif en tant que fichier de référence.

    :param api_key: str, clé d’API à utiliser
    :param report_id: str, ID du rapport RWA
    :param pdf_paginated_path: Path, chemin vers le fichier PDF paginé à uploader
    :return: dict, réponse JSON ou erreur
    """
    url = f"{conf.API_BASE_URL}{conf.API_REFERENCE_UPLOAD_PATH}"
    headers = {"X-Api-Key": api_key}
    payload = {"report_id": report_id}

    try:
        with open(pdf_paginated_path, 'rb') as pdf_file:
            files = {
                'file': (
                    pdf_paginated_path.name,
                    pdf_file,
                    'application/pdf'
                )
            }
            response = requests.post(
                url,
                headers=headers,
                data=payload,
                files=files,
                timeout=conf.UPLOAD_REQUEST_TIMEOUT,
            )
        try:
            data = response.json()
        except ValueError:
            data = {"raw_text": response.text}

        if response.status_code >= 400:
            logging.error(f"❌ Erreur HTTP {response.status_code} lors de l’upload du fichier paginé.")
        else:
            logging.info(f"✅ WP paginé posté avec succès : {pdf_paginated_path.name}")

        return data

    except Exception as e:
        logging.exception(f"❌ Exception lors du POST fichier paginé : {e}")
        return {"error": str(e)}


# ----------------------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Étape 1c - Remplissage par LLM
# -----------------------------------------------------------------------------



def split_framework_into_blocks(framework: list[dict]) -> dict[str, list[dict]]:
    """
    split_framework_into_blocks : découpe la liste de métriques du framework en sous-listes par catégorie.
    fonctions appelées : dict.setdefault(), str.split()

    :param framework: list[dict], liste des entrées contenant au minimum la clé 'metric_name'
    :return: dict[str, list[dict]], blocs de métriques par catégorie (lettres avant le point)
    """
    blocks: dict[str, list[dict]] = {}
    for entry in framework:
        cat = entry.get("category", "")
        if not cat:
            continue
        blocks.setdefault(cat, []).append(entry)
    return blocks





def split_whitepaper_into_chunks(text: str, num_blocks: int) -> list[str]:
    """
    segmente le texte intégral d’un whitepaper en un nombre donné de blocs, en préservant l’intégralité de chaque page entre marqueurs.
    fonctions appelées : re.split(), math.ceil(), len()

    :param text: str, contenu complet du whitepaper avec lignes '--- PAGE X ---'
    :param num_blocks: int, nombre de blocs souhaité
    :return: list[str], liste des blocs de texte concaténé par pages
    """
    # Sépare par pages
    # On inclut le marqueur '--- PAGE' dans chaque segment pour repérer la page
    pages = re.split(r'(?=--- PAGE \d+ ---)', text)
    # Filtre les éventuels segments vides
    pages = [p.strip() for p in pages if p.strip()]

    total_pages = len(pages)
    if total_pages == 0 or num_blocks <= 0:
        return []

    # Taille de chunk en nombre de pages
    chunk_size = math.ceil(total_pages / num_blocks)

    chunks = []
    for i in range(0, total_pages, chunk_size):
        block_pages = pages[i : i + chunk_size]
        chunks.append("\n\n".join(block_pages))
    return chunks




def archive_and_verify_wp_chunks(text: str,
                                 num_wp_blocks: int,
                                 file_processed: str,
                                 report_folder: str) -> list[set[int]]:
    """
    archive_and_verify_wp_chunks : découpe, archive et vérifie les segments du whitepaper,
    garantissant l’absence de chevauchements ou de pages manquantes.
    fonctions appelées : re.split(), Path.write_text(), re.search(), logging.error(), ValueError

    :param text: str, texte intégral du whitepaper avec marqueurs de page
    :param num_wp_blocks: int, nombre de segments à créer
    :param file_processed: str, identifiant du fichier analysé
    :param report_folder: str, dossier de destination pour les archives
    :return: list[set[int]], liste des ensembles de numéros de pages pour chaque chunk
    """
    file_processed = validate_file_processed(file_processed)

    # 1) Split into pages
    pages = re.split(r'(?=--- PAGE \d+ ---)', text)
    pages = [p.strip() for p in pages if p.strip()]

    # Extract page numbers
    page_numbers = [int(re.search(r'--- PAGE (\d+) ---', p).group(1)) for p in pages]

    # 🧭 NOTE IMPORTANTE :
    # Le paramètre num_wp_blocks (fourni depuis le pipeline) détermine le découpage réel :
    #   - Si num_wp_blocks < total_pages → plusieurs pages regroupées par chunk (ex: 5 blocs de 6 pages)
    #   - Si num_wp_blocks == total_pages → 1 page = 1 chunk (mode précision maximale)



    total_pages = len(pages)
    chunk_size = -(-total_pages // num_wp_blocks)  # ceil division (ceil = plafond)

    # Determine archive folder (must exist already)
    chunks_folder = Path(report_folder) / "wp_chunks"

    archived_page_sets = []

    # 2) Archive each chunk
    for idx in range(0, total_pages, chunk_size):
        block_pages = pages[idx: idx + chunk_size]
        chunk_file = chunks_folder / f"{file_processed}_chunk{idx//chunk_size + 1}.txt"
        chunk_file.write_text("\n\n".join(block_pages), encoding="utf-8")

        # Collect page numbers
        pages_in_chunk = [int(re.search(r'--- PAGE (\d+) ---', p).group(1)) for p in block_pages]
        archived_page_sets.append(set(pages_in_chunk))

    # 3) Verify no overlaps or missing pages
    all_pages = set()
    for i, page_set in enumerate(archived_page_sets, start=1):
        overlap = all_pages.intersection(page_set)
        if overlap:
            raise ValueError(f"Overlap in chunk {i}: pages {sorted(overlap)} twice!")
        all_pages.update(page_set)

    missing = set(page_numbers) - all_pages
    if missing:
        raise ValueError(f"Missing pages: {sorted(missing)}")

    logging.info(f"Archived {len(archived_page_sets)} chunks successfully.")
    return archived_page_sets



# ──────────────────────────────────────────────────────────────────────────────
# Stage 0 : avant tout appel à stage 1 ou stege 2
#    - On stocke séparément l’état pour stage1 et stage2.
#    - "armed_at" : timestamp d’armement
#    - "trigger_after_sec" : délai aléatoire (60..120s)
#    - "provider_to_fail" : provider ciblé (ex: "openai")
#    - "active" : True => simulation activée pour ce stage
#    - "triggered" : True => panne déjà déclenchée (on loggue une seule fois)
# ──────────────────────────────────────────────────────────────────────────────
def arm_simulated_provider_outage(
    stage: str,
    provider_to_fail: str,
    enable: bool = True,
    min_delay_sec: int = 60,
    max_delay_sec: int = 120
) -> None:
    """
    Active ou désactive une simulation de panne pour un provider et un stage donnés.

    Rôle :
    - Forcer l’indisponibilité simulée d’un provider LLM afin de tester
      les mécanismes de retry et de failover.
    - Permettre des scénarios de test reproductibles sans dépendre
      d’incidents réels.

    Comportement :
    - Marque un provider comme indisponible pour un stage donné.
    - Cette information est ensuite consultée avant chaque appel LLM.
    - N’a aucun effet si la simulation est désactivée.

    Remarques :
    - Cette fonction est destinée aux tests et validations.
    - Elle ne doit pas être activée en environnement de production
      sans intention explicite.
    """
    stage = (stage or "").lower().strip()
    provider_to_fail = (provider_to_fail or "").lower().strip()

    if stage not in conf._SIMULATED_PROVIDER_OUTAGE:
        logging.warning(f"[SIMULATION] Stage inconnu '{stage}' → simulation ignorée.")
        return

    if not enable:
        conf._SIMULATED_PROVIDER_OUTAGE[stage] = {
            "active": False,
            "armed_at": None,
            "trigger_after_sec": None,
            "provider_to_fail": None,
            "triggered": False
        }
        logging.info(f"[SIMULATION] Désactivée pour {stage}.")
        return

    # Sécurise les bornes
    min_delay_sec = int(min_delay_sec)
    max_delay_sec = int(max_delay_sec)
    if max_delay_sec < min_delay_sec:
        max_delay_sec = min_delay_sec

    trigger_after = random.randint(min_delay_sec, max_delay_sec)
    conf._SIMULATED_PROVIDER_OUTAGE[stage] = {
        "active": True,
        "armed_at": time.time(),
        "trigger_after_sec": trigger_after,
        "provider_to_fail": provider_to_fail,
        "triggered": False
    }

    logging.warning(
        f"[SIMULATION] Panne provider armée pour {stage} : "
        f"provider='{provider_to_fail.upper()}', déclenchement dans ~{trigger_after}s "
        f"(fenêtre [{min_delay_sec},{max_delay_sec}]s)."
    )


def is_provider_available_for_stage(stage: str, provider: str) -> bool:
    """
    Indique si un provider LLM est disponible pour un stage donné.

    Rôle :
    - Centraliser la décision d’appel ou non d’un provider.
    - Intégrer les règles de simulation de panne dans le flux normal
      du retry + failover.

    Comportement :
    - Retourne False si une simulation de panne est active pour ce provider
      et ce stage.
    - Retourne True dans tous les autres cas.

    Remarques :
    - Cette fonction est utilisée par les mécanismes de failover
      pour ignorer proprement un provider indisponible.
    - Elle ne déclenche aucun effet de bord.
    """
    stage = (stage or "").lower().strip()
    provider = (provider or "").lower().strip()

    state = conf._SIMULATED_PROVIDER_OUTAGE.get(stage)
    if not state or not state.get("active"):
        return True

    # Si on n’a pas correctement armé, on laisse passer
    armed_at = state.get("armed_at")
    trigger_after = state.get("trigger_after_sec")
    provider_to_fail = (state.get("provider_to_fail") or "").lower().strip()
    if not armed_at or trigger_after is None or not provider_to_fail:
        return True

    # Déclenchement réel de la panne après le délai aléatoire
    elapsed = time.time() - float(armed_at)
    if elapsed >= float(trigger_after) and provider == provider_to_fail:
        if not state.get("triggered"):
            state["triggered"] = True
            logging.error(
                f"[SIMULATION] PANNE SIMULÉE ACTIVE ({stage}) : "
                f"provider='{provider.upper()}' indisponible (déclenchée après {int(elapsed)}s)."
            )
        return False

    return True

# ──────────────────────────────────────────────────────────────────────────────
# Stage 1 : Appels LLM agent 1 remplissage du framework
# ──────────────────────────────────────────────────────────────────────────────
def filter_metrics_for_llm(metrics: list[dict]) -> list[dict]:
    """
    stage 1 : prépare le bloc `metrics_block` à envoyer au LLM.
    Elle ne conserve que les champs essentiels mentionnés dans le prompt général,
    pour éviter tout biais dû à des informations descriptives non prévues.

    Conserve uniquement :
      - "id"
      - "category"
      - "llm_prompt"

    :param metrics: list[dict], liste brute issue du framework complet.
    :return: list[dict], liste nettoyée contenant uniquement les 3 champs utiles.
    """
    filtered = []
    for entry in metrics:
        filtered.append({
            "id": entry.get("id"),   # 'id' en inpunt de 'metrics_block' du prompt 1 et non 'framework_id'
            "category": entry.get("category"),
            "llm_prompt": entry.get("llm_prompt", "")
        })
    return filtered


def call_llm_with_failover_stage1_chunk(
    chunk,
    metric_id,
    file_processed: str,
    framework_label: Optional[str] = None
):
    """
    Stage 1 — Appel LLM avec failover entre providers (par chunk).

    Fonctionnement :
    - Pour un chunk donné (messages), la fonction parcourt les providers définis
      dans STAGE1_PROVIDER_ORDER.
    - Pour chaque provider, elle appelle call_llm_with_retry_stage1(), qui gère
      jusqu'à N tentatives (retry) sur le même input.
    - Si un provider échoue après épuisement de ses retry, le traitement bascule
      automatiquement vers le provider suivant.
    - Si tous les providers échouent, une exception est levée afin de signaler
      l'échec du Stage 1 pour ce report.

    Remarques importantes :
    - Le failover est piloté exclusivement par STAGE1_PROVIDER_ORDER.
    - Aucune logique spécifique dépendante du provider (GPT ou Gemini) n'est
      implémentée ici.
    - La décision de succès ou d'échec repose uniquement sur le parsing JSON final
      effectué dans call_llm_with_retry_stage1().
    """

    # Sert uniquement à tracer la dernière erreur rencontrée
    last_error = None

    # On itère explicitement sur l’ordre de bascule défini dans la configuration
    for provider, model in conf.STAGE1_PROVIDER_ORDER:
        if not is_provider_available_for_stage("stage1", provider):
            logging.warning(
                f"[Stage1][ChunkFailover] ⛔ Provider={provider.upper()} SKIP "
                f"(panne simulée active) | metric_id={metric_id}"
            )
            continue
        logging.info(
            f"[Stage1][ChunkFailover] ▶ Tentative provider={provider.upper()} | model={model} | metric_id={metric_id}"
        )

        try:
            # Appel LLM avec retry interne (max_retries=5)
            parsed, raw_text, retries_exhausted = call_llm_with_retry_stage1(
                messages=chunk,
                file_processed=file_processed,
                metric_id=metric_id,
                provider=provider,
                model=model,
                max_retries=5
            )

            # Si le LLM a répondu correctement (au moins une entrée parsée)
            if parsed:
                logging.info(
                    f"[Stage1][ChunkFailover] ✅ Succès provider={provider.upper()} | metric_id={metric_id}"
                )
                return parsed, raw_text


            # Cas rare : le LLM a répondu mais sans contenu exploitable
            # On considère cela comme un échec du provider
            logging.warning(
                f"[Stage1][ChunkFailover] ⚠ Provider={provider.upper()} épuisé "
                f"(réponses vides après retry) → bascule provider suivant | metric_id={metric_id}"
            )

        except Exception as e:
            # Toute exception est considérée comme un échec du provider
            last_error = e
            logging.warning(
                f"[Stage1][ChunkFailover] ❌ Provider={provider.upper()} échec complet "
                f"({type(e).__name__}: {e}) → bascule provider suivant | metric_id={metric_id}"
            )
            continue

    # Si on arrive ici, aucun provider n’a réussi après ses retries
    msg = (
        f"[Stage1][ChunkFailover] 🛑 Tous les providers ont échoué "
        f"pour metric_id={metric_id} (file={file_processed}). "
        f"Dernière erreur: {type(last_error).__name__ if last_error else 'N/A'}: {last_error}"
    )
    logging.error(msg)

    # On stoppe volontairement le Stage 1
    raise RuntimeError(msg)



def call_llm_with_retry_stage1(
    messages: List[Dict[str, Any]],
    file_processed: str,
    metric_id: Optional[int] = None,
    provider: str = "openai",
    model: str = conf.DEFAULT_LLM_MODEL,
    max_retries: int = 5,
    backoff_seconds: float = 15.0,
) -> Tuple[List[Dict[str, Any]], str, bool]:
    """Appelle un provider Stage 1 avec retry local et parsing JSON strict."""
    validate_file_processed(file_processed)
    last_text_out = ""

    for attempt in range(1, max_retries + 1):
        finish_reason = "N/A"
        try:
            if provider.lower() == "gemini":
                response = _gemini_generate_json(
                    model=model,
                    prompt_text=_messages_to_prompt_text(messages),
                    max_output_tokens=16384,
                )
                text_out = (getattr(response, "text", "") or "").strip()
                finish_reason = _gemini_finish_reason(response)
                prompt_tokens, completion_tokens, total_tokens = _gemini_usage_tokens(response)
            elif provider.lower() == "openai":
                if not conf.OPENAI_API_KEY:
                    raise RuntimeError("Clé OpenAI manquante.")
                llm = ChatOpenAI(
                    model_name=model,
                    temperature=0,
                    max_tokens=16384,
                    openai_api_key=conf.OPENAI_API_KEY,
                    timeout=conf.LLM_REQUEST_TIMEOUT_SECONDS,
                    max_retries=0,
                )
                result = llm.generate([messages])
                text_out = result.generations[0][0].text.strip()
                token_usage = (result.llm_output or {}).get("token_usage", {}) or {}
                prompt_tokens = int(token_usage.get("prompt_tokens", 0) or 0)
                completion_tokens = int(token_usage.get("completion_tokens", 0) or 0)
                total_tokens = prompt_tokens + completion_tokens
            else:
                raise ValueError(f"Provider LLM non supporté : {provider}")

            if not text_out:
                raise ValueError("Stage 1 : réponse LLM vide.")

            last_text_out = text_out
            log_raw_llm_output(file_processed, provider, "stage1", text_out)
            logging.info(
                "[Stage1][%s][Attempt %d/%d] Réponse reçue (%d caractères, tokens=%d/%d/%d, finish=%s)",
                provider.upper(),
                attempt,
                max_retries,
                len(text_out),
                prompt_tokens,
                completion_tokens,
                total_tokens,
                finish_reason,
            )

            clean_text = _clean_json_output(text_out, expect_array=True)
            if clean_text.count("[") >= 1 and clean_text.count("]") >= 1:
                clean_text = clean_text[: clean_text.rfind("]") + 1]

            data = json.loads(clean_text)
            if not isinstance(data, list):
                raise ValueError("Stage 1 : sortie JSON invalide (liste attendue).")
            if not data:
                raise ValueError("Stage 1 : sortie JSON vide.")

            validate_entry_structure(data, context=f"LLM-output/{file_processed}")
            return data, last_text_out, False

        except OpenAIRateLimitError as exc:
            retry_after = getattr(exc, "retry_after", None)
            if not retry_after and getattr(exc, "response", None) is not None:
                retry_after = exc.response.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else backoff_seconds * attempt
            logging.warning(
                "[%s %d/%d] Rate limit ; nouvelle tentative dans %.1fs.",
                provider.upper(),
                attempt,
                max_retries,
                wait,
            )
        except Exception as exc:
            wait = backoff_seconds * attempt
            logging.warning(
                "[Stage1][%s %d/%d] Échec %s : %s",
                provider.upper(),
                attempt,
                max_retries,
                type(exc).__name__,
                exc,
            )
            if any(token in str(exc).lower() for token in ("429", "quota", "rate limit")):
                wait = max(wait, 20.0 * attempt)

        if attempt < max_retries:
            time.sleep(wait)

    logging.error("Provider %s épuisé après %d tentatives Stage 1.", provider.upper(), max_retries)
    return [], last_text_out, True


def fix_red_flag_excerpt_error(entry: dict, context: str = "", placeholder: str = "—") -> int:
    """
    Corrige un cas fréquent de désalignement Stage 1 :
    - Le LLM fournit une liste `flag` (et souvent `output` / `page_number`) complète,
      mais omet un ou plusieurs items dans `whitepaper_excerpt`, en particulier quand `flag == "red"`.
    - La troncature "à min(len)" dans validate_entry_structure() supprime alors des items utiles
      (souvent non-red), ce qu'on veut éviter.

    Stratégie (volontairement conservative et "structure-first") :
    1) Garantir que `whitepaper_excerpt` est une liste.
    2) Normaliser la longueur de `whitepaper_excerpt` sur la longueur de `flag` :
       - Si un index i existe dans `flag` mais pas dans `whitepaper_excerpt` :
           * si flag[i] == "red"  -> on insère le placeholder "—"
           * sinon               -> on insère une chaîne vide "" (on signale l'absence plus tard,
                                    mais on évite de perdre l'item par troncature)
    3) Pour chaque index i où flag[i] == "red" :
       - Forcer `whitepaper_excerpt[i]` à placeholder si vide ou différent de celui-ci.

    Important :
    - Cette fonction ne modifie PAS `flag`, `output`, `page_number`.
    - Elle ne corrige pas les incohérences autres que `whitepaper_excerpt`.
    - Elle renvoie le nombre total de corrections appliquées (padding + remplacements).

    Args:
        entry (dict): entrée JSON LLM (1 framework_id) avec clés attendues.
        context (str): texte de contexte pour logs (ex: "LLM-output/xxx").
        placeholder (str): valeur placeholder imposée pour les flags "red" (par défaut "—").

    Returns:
        int: nombre de corrections appliquées sur `whitepaper_excerpt`.
    """
    fid = entry.get("framework_id", None)

    flags = entry.get("flag", [])
    excerpts = entry.get("whitepaper_excerpt", [])

    # Sécurise les types : si on ne peut pas travailler proprement, on sort sans casser.
    if not isinstance(flags, list):
        return 0

    # Si whitepaper_excerpt est absent ou pas une liste, on le reconstruit en liste vide.
    if not isinstance(excerpts, list):
        excerpts = []

    corrections = 0
    target_len = len(flags)

    # 1) Aligne la longueur des excerpts sur celle des flags pour éviter toute troncature destructrice.
    #    On remplit index par index pour rester strictement positionnel.
    if len(excerpts) < target_len:
        for i in range(len(excerpts), target_len):
            if flags[i] == "red":
                excerpts.append(placeholder)
            else:
                excerpts.append("")
            corrections += 1

    # Si excerpts est plus long que flags, on tronque uniquement excerpts (sans toucher aux autres listes).
    # Cela évite un désalignement artificiel causé par un surplus d'excerpts.
    if len(excerpts) > target_len:
        excerpts = excerpts[:target_len]
        corrections += 1

    # 2) Force placeholder pour les flags red (même si excerpt présent mais incorrect).
    for i, fl in enumerate(flags):
        if fl != "red":
            continue

        current = excerpts[i] if i < len(excerpts) else ""
        current_str = str(current).strip()

        # Si vide, "-" ou autre valeur ≠ placeholder => on force placeholder.
        if current_str != placeholder:
            excerpts[i] = placeholder
            corrections += 1

    if corrections:
        logging.info(
            f"[{context}] framework_id={fid} → redressement excerpts sur flags 'red' "
            f"(placeholder='{placeholder}') : {corrections} correction(s)."
        )

    entry["whitepaper_excerpt"] = excerpts
    return corrections


def validate_entry_structure(entries: list[dict], context: str = "") -> list[dict]:
    """
    validate_entry_structure :
    ──────────────────────────
    Vérifie et stabilise la structure JSON de chaque entrée issue du LLM.
    Elle détecte les incohérences formelles, les types inattendus, et
    les désalignements de longueur entre les listes, afin de garantir
    la compatibilité du pipeline avant agrégation (Stage 1).

    ⚙️  Fonctionnement général
    --------------------------
    Pour chaque entrée LLM, la fonction :
      1. Vérifie la présence des clés requises :
         ["framework_id", "flag", "page_number", "whitepaper_excerpt", "output"].
      2. Vérifie le type de chaque clé (list attendu sauf pour framework_id).
      3. Vérifie les longueurs de ces listes :
         - Si elles diffèrent (ex: [2,2,1,2]),
           → ajoute l’anomalie "list length mismatch"
           → tronque toutes les listes à min(len)
             (préserve l’intégrité structurelle, quitte à perdre un item malformé).
      4. Vérifie les listes vides.
      5. Signale toute clé inattendue ou un framework_id manquant.

    📦  Effet attendu
    -----------------
    - L’entrée est toujours renvoyée sous forme d’objet JSON cohérent,
      sans provoquer d’erreur de parsing dans les étapes suivantes.
    - Les anomalies détectées sont listées dans `entry["anomaly_detected"]`
      pour suivi ultérieur par les étapes Stage 1+ / Stage 2.

    ⚠️  Sur les désalignements et le troncage
    ----------------------------------------
    En cas de longueurs désalignées (ex: [3,3,2,3]) :
      1) La fonction tente d'abord un redressement ciblé de `whitepaper_excerpt`
         basé sur `flag` (cas fréquent : oubli du placeholder "—" sur des flags "red").
         - Forçage du placeholder "—" pour les items `flag == "red"`.
         - Complétion de `whitepaper_excerpt` pour préserver l'alignement et éviter
           de perdre des items utiles (notamment non-red) par troncature.
      2) Si l'incohérence persiste après ce redressement, alors (seulement alors)
         toutes les listes sont tronquées à `min(len)` pour garantir la cohérence structurelle
         et éviter un mélange entre sous-points.

    Exemple concret :
        flag = ["green", "green"]
        page_number = [8, 9]
        whitepaper_excerpt = ["A"]
        output = ["(1): textA", "(2): textB"]

      ➜ longueurs [2,2,1,2]
      ➜ tronquées à [1,1,1,1]
      ➜ résultat stable et cohérent :
        {
          "flag": ["green"],
          "page_number": [8],
          "whitepaper_excerpt": ["A"],
          "output": ["(1): textA"],
          "anomaly_detected": [["list length mismatch"]]
        }

    🧩  Anomalies détectées :
      - "missing key: <key>"          → clé absente du JSON
      - "invalid type: <key>"         → type non conforme (ex: str au lieu de list)
      - "list length mismatch"        → désalignement entre listes (corrigé par troncage)
      - "empty list"                  → clé présente mais vide
      - "unknown key"                 → champ non attendu
      - "missing framework_id"        → identifiant absent

    🔁  Postulat de conception :
    ----------------------------
    - Ne supprime jamais d’entrée complète (aucun rejet).
    - Ne tente aucune correction sémantique.
    - Garantit uniquement la cohérence **structurelle** pour les étapes suivantes.

    Paramètres
    ----------
    entries : list[dict]
        Liste des objets JSON générés par le LLM.
    context : str
        Contexte d’appel (utile pour logs, ex: “LLM-output/chunk3”).

    Retour
    -------
    list[dict]
        La même liste, stabilisée et enrichie avec les anomalies détectées.
    """
    # clé autorisée = champs autorisé (car l'agent 1 LLM pourrait inventer un champs non demandé dans le json produit)
    REQUIRED_KEYS = ["framework_id", "flag", "page_number", "whitepaper_excerpt", "output"]
    VALID_TYPES = {
        "framework_id": (int,),
        "flag": (list,),
        "page_number": (list,),
        "whitepaper_excerpt": (list,),
        "output": (list,)
    }

    for i, entry in enumerate(entries):
        fid = entry.get("framework_id", None)
        anomalies = entry.get("anomaly_detected", [])

        # Sécurisation du champ anomaly_detected
        if not isinstance(anomalies, list):
            anomalies = []

        # 1️⃣ Vérifie la présence et le type de toutes les clés
        for key in REQUIRED_KEYS:
            if key not in entry:
                anomalies.append([f"missing key: {key}"])
                logging.warning(f"[{context}] framework_id={fid} → clé manquante '{key}'")
            else:
                if not isinstance(entry[key], VALID_TYPES[key]):
                    anomalies.append([f"invalid type: {key}"])
                    logging.warning(f"[{context}] framework_id={fid} → type invalide pour '{key}' ({type(entry[key])})")

        # 2️⃣ Vérifie la cohérence des longueurs
        try:
            ln = [len(entry.get(k, [])) for k in ["flag", "page_number", "whitepaper_excerpt", "output"]]
            if len(set(ln)) > 1:
                anomalies.append(["list length mismatch"])

                # Ajoute au log la provenance complète (si context contient "LLM-output")
                chunk_hint = ""
                if "LLM-output" in context:
                    # extrait le nom du fichier traité depuis le context
                    parts = context.split("/")
                    file_hint = parts[-1] if len(parts) > 1 else ""
                    # recherche d’un chunk correspondant dans le dossier parsed
                    possible_files = list(Path(conf.parsed_dir).glob(f"{file_hint}*_parsed.json"))
                    if possible_files:
                        latest = max(possible_files, key=os.path.getmtime)
                        chunk_hint = f" (dernier chunk : {latest.name})"

                logging.warning(
                    f"[{context}] framework_id={fid} → longueurs incohérentes détectées : {ln} "
                    f"(incohérence transitoire — redressée ultérieurement au niveau chunk)"
                    f"{chunk_hint}"
                )

                # 2.a) Tentative de redressement ciblé AVANT troncature :
                #      cas fréquent où whitepaper_excerpt est plus court car le LLM oublie le placeholder
                #      sur des flags "red". On complète/normalise whitepaper_excerpt sans perdre les autres items.
                fixed = fix_red_flag_excerpt_error(entry, context=context, placeholder="—")

                # 2.b) Recalcule les longueurs après redressement
                ln_after = [len(entry.get(k, [])) for k in ["flag", "page_number", "whitepaper_excerpt", "output"]]
                if len(set(ln_after)) == 1:
                    logging.info(
                        f"[{context}] framework_id={fid} → incohérence de longueurs résolue "
                        f"par redressement excerpts (fixed={fixed}) : {ln} → {ln_after}"
                    )
                else:
                    # 2.c) Toujours incohérent => troncature de sécurité à min(len) (comportement existant)
                    logging.warning(
                        f"[{context}] framework_id={fid} → incohérence persistante après redressement "
                        f"excerpts (fixed={fixed}) : {ln_after} → TRONCATURE à min(len)."
                    )
                    min_len = min(ln_after)
                    for key in ["flag", "page_number", "whitepaper_excerpt", "output"]:
                        entry[key] = entry.get(key, [])[:min_len]


        except Exception as e:
            anomalies.append([f"length check failed: {e}"])
            logging.error(f"[{context}] framework_id={fid} → erreur de vérification des longueurs : {e}")

        # 3️⃣ Vérifie si une des listes est totalement vide
        for k in ["flag", "page_number", "whitepaper_excerpt", "output"]:
            if isinstance(entry.get(k), list) and len(entry[k]) == 0:
                anomalies.append(["empty list"])
                logging.warning(f"[{context}] framework_id={fid} → liste vide pour '{k}'")

        # 4️⃣ Signale les clés inattendues (champs auter que flag, output etc. demandé à l'agent 1 LLM en réponse)
        # comme par exmple le LLM garde le champ "metric_id" qu'il n'est pas sensé donner
        for k in entry.keys():
            if k not in REQUIRED_KEYS and k != "anomaly_detected":
                anomalies.append([f"unknown key: {k}"])

        # 5️⃣ Vérifie la présence du framework_id
        if fid is None:
            anomalies.append(["missing framework_id"])

        entry["anomaly_detected"] = anomalies

    return entries






def parse_llm_prompt_subpoints(llm_prompt: str) -> List[Tuple[str, str]]:
    """
    Découpe un llm_prompt numéroté en sous-points structurés.

    Exemples de formats pris en charge:
      "The following 3 information: (1) A ... (2) B ... (3) C ..."
      "A clear description of both (1) X and (2) Y."

    Retour:
      Liste ordonnée de tuples: [("(1)", "A ..."), ("(2)", "B ..."), ...]
      Si aucune numérotation explicite n'est trouvée, retourne [("(1)", llm_prompt)].

    Notes:
      - On capture la position de chaque "(n)" puis on découpe le texte entre positions.
      - Le texte de chaque sous-point est nettoyé (strip) sans toucher à la ponctuation.
    """
    pattern = re.compile(r"\((\d+)\)")
    matches = list(pattern.finditer(llm_prompt))
    if not matches:
        # Aucun sous-point explicite → fallback: 1 sous-point unique
        return [("(1)", llm_prompt.strip())]

    subpoints: List[Tuple[str, str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(llm_prompt)
        idx = f"({m.group(1)})"
        text = llm_prompt[start:end]  # inclut "(n)" + contenu qui suit
        # Enlève l'indice "(n)" au début pour ne garder que le contenu du point
        text = text[text.find(")") + 1 :].strip() if ")" in text else text.strip()
        subpoints.append((idx, text))
    return subpoints





def _extract_point_index_from_text(text: str) -> str | None:
    """
    Essaie d'extraire un index de sous-point "(n)" depuis une chaîne.
    Retourne la forme canonique "(n)" si trouvée, sinon None.
    """
    m = re.search(r"\((\d{1,2})\)", text or "")
    return f"({m.group(1)})" if m else None



# sélection par sous-point au meilleur flag
def aggregate_stage1_entries_by_flag(entries_by_framework: dict[int, list[dict]]) -> list[dict]:
    """
    Agrège les sorties Stage 1 multi-chunks par framework_id selon la priorité des flags.

    Objectif :
    - Consolider toutes les réponses LLM issues des différents chunks du whitepaper
      pour un même framework_id.
    - Appliquer la hiérarchie de priorité des flags RWA :
        green > amber > red.

    Logique appliquée :
    - Tous les sous-points réellement détectés sont conservés.
    - Les flags "red" sont conservés même si l’extrait est un placeholder ("—").
    - Aucun sous-point factice n’est créé.
    - Si un sous-point attendu est totalement absent, une anomalie
      ["llm_output_empty"] est ajoutée.
    - Les sous-points sont triés par index (1), (2), (3), etc.
    - Chaque framework_id produit exactement une entrée agrégée finale.

    Paramètre :
    - entries_by_framework : dictionnaire {framework_id: [entries_chunk1, entries_chunk2, ...]}

    Valeur de retour :
    - Liste d’entrées agrégées prêtes pour les contrôles de cohérence et le Stage 2.
    """

    aggregated_entries = []

    # Parcourt tous les frameworks détectés
    for fid, entries_list in entries_by_framework.items():
        ordered_pages = []
        ordered_excerpts = []
        ordered_outputs = []
        ordered_flags = []
        anomalies = []

        try:
            # Récupération du llm_prompt du 1er chunk (tous identiques pour un même framework_id)
            llm_prompt = entries_list[0].get("llm_prompt", "") if entries_list else ""
            subpoints = parse_llm_prompt_subpoints(llm_prompt)
            total_subpoints = len(subpoints) if subpoints else 0

            # Agrégation multi-chunk : collecte de toutes les citations
            all_citations = []
            for e in entries_list:
                flags = e.get("flag", [])
                pages = e.get("page_number", [])
                excerpts = e.get("whitepaper_excerpt", [])
                outputs = e.get("output", [])
                for p, ex, out, fl in zip(pages, excerpts, outputs, flags):
                    idx = _extract_point_index_from_text(out) or _extract_point_index_from_text(ex)
                    all_citations.append((idx, p, ex, out, fl))

            # Organisation par sous-point (1), (2), ...
            by_point = defaultdict(list)
            for idx, p, ex, out, fl in all_citations:
                if idx:
                    by_point[idx].append((p, ex, out, fl))

            # Tri des sous-points dans l'ordre croissant (1)...(N)
            all_point_indices = sorted(by_point.keys())

            # Fusion / sélection par sous-point
            for idx in all_point_indices:
                items = by_point[idx]
                if not items:
                    continue

                # Tri selon priorité de flag (green > amber > red)
                items.sort(key=lambda x: conf.FLAG_PRIORITY.get(x[3], 3))
                best_flag = items[0][3]
                selected_items = [it for it in items if it[3] == best_flag]

                # Cas multiple green → conserver tous
                if best_flag == "green":
                    chosen = selected_items
                # Cas amber → tous amber
                elif best_flag == "amber":
                    chosen = selected_items
                # Cas red → un seul red
                else:
                    chosen = [selected_items[0]]

                for p, ex, out, fl in chosen:
                    ordered_pages.append(p)
                    ordered_excerpts.append(ex)
                    ordered_outputs.append(out)
                    ordered_flags.append(fl)
                    anomalies.append([])  # pas d'anomalie locale ici

            # Vérification des sous-points attendus
            # total_subpoints = nombre total de sous-points attendus pour ce framework_id
            # (extrait directement du llm_prompt via parse_llm_prompt_subpoints()).
            # all_point_indices = liste des indices (1), (2), etc., effectivement trouvés dans tous les chunks combinés.
            # Si len(all_point_indices) < total_subpoints
            # → cela signifie que le LLM n’a pas répondu à tous les sous-points.
            if total_subpoints > 0 and len(all_point_indices) < total_subpoints:
                missing = total_subpoints - len(all_point_indices)
                for _ in range(missing):
                    anomalies.append(["llm_output_empty"])
                logging.warning(f"[aggregate_stage1] framework_id={fid} → {missing} sous-points manquants")

            # warning si framework sans AUCUNE citation retenue
            if not ordered_outputs:
                logging.warning(
                    f"[aggregate_stage1] framework_id={fid} → aucune citation retenue après agrégation (tous sous-points absents ou red)."
                )


            # Création de l’entrée agrégée finale
            aggregated_entry = {
                "framework_id": fid,
                "flag": ordered_flags,
                "page_number": ordered_pages,
                "whitepaper_excerpt": ordered_excerpts,
                "output": ordered_outputs,
                "anomaly_detected": anomalies,
            }

            aggregated_entries.append(aggregated_entry)

        except Exception as e:
            logging.error(f"[aggregate_stage1] Erreur sur framework_id={fid} : {e}", exc_info=True)

    logging.info(f"[aggregate_stage1] ✅ Agrégation terminée : {len(aggregated_entries)} frameworks consolidés.")
    return aggregated_entries



def validate_stage1_page_consistency(entries: list[dict], whitepaper_text: str, file_base_name: str, log_folder: str | Path) -> list[dict]:
    """
    Valide la cohérence des sorties Stage 1 agrégées par page.

    Fonctionnement :
    - Analyse les entrées Stage 1 produites pour une même page.
    - Vérifie la cohérence des longueurs et des structures attendues.
    - Détecte les anomalies éventuelles sans interrompre le pipeline.

    Valeur de retour :
    - Retourne une liste d'anomalies par index, permettant un diagnostic fin
      sans bloquer le traitement global.

    Remarques :
    - Cette fonction est volontairement non bloquante : elle vise à signaler
      des incohérences potentielles tout en laissant le pipeline se poursuivre.
    - Aucune logique LLM ou retry/failover n'est implémentée ici.
    """

    # ───────────────────────────────────────────────
    # 🔧 Détection de la page réelle via “--- PAGE X ---”
    # ───────────────────────────────────────────────
    def infer_page(excerpt: str, wp_text: str) -> int | None:
        """
        Essaie d’inférer (déduire) la page où se trouve un extrait.
        Cherche d’abord les séparateurs “--- PAGE X ---” puis compare les zones de texte.
        Retourne le numéro de page trouvé ou None.
        """
        if not excerpt or len(excerpt.strip()) < 15:
            return None

        # Découpe du texte complet par pages
        pages = wp_text.split("--- PAGE ")
        if len(pages) == 1:
            # Aucun séparateur détecté → on tente une correspondance globale
            match = SequenceMatcher(None, excerpt[:150], wp_text).find_longest_match(0, len(excerpt), 0, len(wp_text))
            return 1 if match.size > 0 else None

        # Comparaison page par page
        best_match = 0
        best_score = 0
        for i, page_content in enumerate(pages[1:], start=1):
            score = SequenceMatcher(None, excerpt[:200], page_content).ratio()
            if score > best_score:
                best_score = score
                best_match = i

        if best_score > 0.35:  # seuil empirique
            return best_match
        return None

    # ───────────────────────────────────────────────
    # 📘 Initialisation des logs et structure de sortie
    # ───────────────────────────────────────────────
    log_path = Path(log_folder) / f"control_citations_page_fix_{file_base_name}.json"
    log_data = []

    for entry in entries:
        flags = entry.get("flag", [])
        pages = entry.get("page_number", [])
        excerpts = entry.get("whitepaper_excerpt", [])
        outputs = entry.get("output", [])

        # Longueur maximale observée pour alignement
        max_len = max(len(flags), len(pages), len(excerpts), len(outputs)) if any([flags, pages, excerpts, outputs]) else 0
        anomalies = [[] for _ in range(max_len)]

        # Alignement doux (pas de suppression)
        if len(flags) != max_len or len(pages) != max_len or len(excerpts) != max_len or len(outputs) != max_len:
            anomalies[0].append("list length normalized")
        # Aucune apparition de None dans page_number même temporairement.
        flags = (flags + [None] * (max_len - len(flags)))[:max_len]
        pages = (pages + [0] * (max_len - len(pages)))[:max_len]  # 0 au lieu de None
        excerpts = (excerpts + [""] * (max_len - len(excerpts)))[:max_len]
        outputs = (outputs + [""] * (max_len - len(outputs)))[:max_len]

        # ───────────────────────────────────────────────
        # 🔍 Boucle de contrôle item par item
        # ───────────────────────────────────────────────
        # taille des listes en référence
        len_flag = len(entry["flag"])

        for i in range(max_len):
            flag = flags[i]
            excerpt = excerpts[i]
            page = pages[i]
            output = outputs[i]

            # 1️⃣ Excerpt manquant (sauf pour les flag red)
            if (not excerpt or excerpt.strip() in ["—", "-", ""]) and flag != "red":
                anomalies[i].append("excerpt not found")

            # 2️⃣ Page absente ou inférée (sauf pour les flag red)
            if page is None and flag != "red":
                inferred = infer_page(excerpt, whitepaper_text)
                if inferred is not None:
                    anomalies[i].append("page inferred")
                    pages[i] = inferred
                else:
                    anomalies[i].append("page missing")

            # 3️⃣ Vérification de cohérence flag/output/excerpt
            # ------------------------------------------------
            # ✅ Alignement avec le prompt finalisé :
            #   - flag "red" → doit avoir :
            #       output commençant par "(n): No explicit or implicit evidence found regarding"
            #       page_number = [0]
            #       whitepaper_excerpt = ["—"]
            #   - flag "green"/"amber" → ne doivent PAS contenir :
            #       • "no evidence found"
            #       • placeholder "—" ou "-"
            #   - Toutes les listes flag/page_number/whitepaper_excerpt/output doivent avoir la même longueur
            #
            #   Types d’anomalies détectées :
            #     • invalid red output → mauvaise structure de output pour flag red
            #     • flag/output contradiction → incohérence entre flag et contenu
            #     • placeholder present → présence d’un "—" ou "-" invalide
            #     • list length mismatch → longueurs des listes incohérentes
            #

            # Vérification de cohérence de taille des listes

            for key in ["page_number", "whitepaper_excerpt", "output"]:
                if len(entry[key]) != len_flag:
                    anomalies.append(["list length mismatch"])
                    break


            output = str(entry["output"][i]).strip() if i < len(entry["output"]) else ""
            excerpt = str(entry["whitepaper_excerpt"][i]).strip() if i < len(entry["whitepaper_excerpt"]) else ""
            page = entry["page_number"][i] if i < len(entry["page_number"]) else None


            # 🔴 Cas 1 : flag = "red"
            if flag == "red":
                # Le pattern correct : "(n): No explicit or implicit evidence found regarding ..."
                if not re.match(
                    r"^\(\d+\):\s*No explicit or implicit evidence found regarding",
                    output,
                    re.IGNORECASE
                ):
                    anomalies[i].append("invalid red output")

                # Vérifie que page_number est bien 0
                if page != 0:
                    anomalies[i].append("invalid red page_number")

                # Vérifie que whitepaper_excerpt est bien "—"
                if excerpt not in ["—", "-"]:
                    anomalies[i].append("invalid red excerpt")

            # 🟢 Cas 2 : flag = "green" ou "amber"
            elif flag in ("green", "amber"):
                # Incohérence : une phrase négative ou placeholder interdit
                if re.search(r"no explicit|no implicit|no evidence", output, re.IGNORECASE):
                    anomalies[i].append("flag/output contradiction")

                # Placeholder interdit dans output ou excerpt
                if output in ["—", "-"] or excerpt in ["—", "-"]:
                    anomalies[i].append("placeholder present")

                # Vérifie que page_number n’est pas 0 ni None
                if page in (None, 0):
                    anomalies[i].append("invalid non-red page_number")

            # 4️⃣ Vérification du type de page
            if not isinstance(page, int):
                anomalies[i].append("invalid page type")


        # Enregistrement dans l’entrée enrichie
        entry["flag"] = flags
        entry["page_number"] = pages
        entry["whitepaper_excerpt"] = excerpts
        entry["output"] = outputs
        entry["anomaly_detected"] = anomalies

        # Ajout au log pour monitoring
        log_data.append({
            "framework_id": entry.get("framework_id"),
            "metric_id": entry.get("metric_id"),
            "anomalies_count": sum(1 for a in anomalies if a),
            "types": sorted(set(a for sub in anomalies for a in sub))
        })

        logging.info(f"[validate_stage1_page_consistency] {sum(len(a) for a in entry['anomaly_detected'])} anomalies détectées pour framework_id={entry.get('framework_id')}")


    # ───────────────────────────────────────────────
    # 💾 Sauvegarde du log de contrôle
    # ───────────────────────────────────────────────
    try:
        save_json_file(log_path, log_data)
        logging.info(f"[validate_stage1_page_consistency] Log sauvegardé : {log_path.name}")
    except Exception as e:
        logging.error(f"[validate_stage1_page_consistency] Erreur log : {e}")


    return entries



def merge_stage1_framework_traceability(
    df_framework: pd.DataFrame,
    entries: list[dict],
    agg_path: Path | None  # ← agg_path devient optionnel
) -> list[dict]:
    """
    Fusionne les résultats Stage 1 agrégés avec le framework de référence.

    Objectifs :
    - Enrichir chaque entrée Stage 1 avec les métadonnées du framework :
      metric_id, category, title, llm_prompt.
    - Garantir la traçabilité complète entre sorties LLM et framework source.
    - Produire un JSON enrichi prêt pour le Stage 2 ou l’archivage final.

    Clé de jointure :
    - entries.framework_id ↔ df_framework.id

    Paramètres :
    - df_framework : DataFrame du framework de référence.
    - entries : liste des entrées Stage 1 agrégées.
    - agg_path : chemin de sauvegarde optionnel (None → aucune écriture).

    Valeur de retour :
    - Liste de dictionnaires représentant les entrées enrichies.

    Remarques :
    - La fusion est non destructive : les entrées sont conservées même si
      certaines métadonnées framework sont manquantes.
    - Les NaN sont convertis en None pour produire un JSON propre.
    """
    if not entries or df_framework.empty:
        logging.warning("⚠️ Aucun résultat à fusionner pour la traçabilité (entries vides ou framework vide).")
        if agg_path:
            agg_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            logging.info("ℹ️ Aucun fichier de traçabilité à écrire (agg_path=None)")
        return entries


    # --- DataFrames d'entrée
    df_entries = pd.DataFrame(entries)

    # Sécurise la présence de la clé de jointure côté entries
    if "framework_id" not in df_entries.columns:
        logging.error("❌ `framework_id` absent de entries : impossible de fusionner.")
        agg_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        return entries

    # Sous-ensemble framework (renommage id -> framework_id pour la jointure)
    fw_keep = ["id", "metric_id", "category", "title", "llm_prompt"]
    missing_fw_cols = [c for c in fw_keep if c not in df_framework.columns]
    if missing_fw_cols:
        logging.warning(f"⚠️ Colonnes manquantes côté framework : {missing_fw_cols}")

    df_fw_sub = (
        df_framework[[c for c in fw_keep if c in df_framework.columns]]
        .rename(columns={"id": "framework_id"})
    )

    # --- Diagnostics d’alignement d’IDs (sur framework_id des 2 côtés)
    llm_ids = set(pd.Series(df_entries["framework_id"]).dropna().unique().tolist())
    fw_ids = set(pd.Series(df_fw_sub["framework_id"]).dropna().unique().tolist())
    matching = llm_ids & fw_ids
    missing_fw = llm_ids - fw_ids   # présents côté LLM mais absents du framework
    missing_llm = fw_ids - llm_ids  # présents côté framework mais absents du LLM

    logging.info("────────── Comparaison framework ↔ entries LLM ──────────")
    logging.info(f"Total métriques framework : {len(fw_ids)}")
    logging.info(f"Total entries LLM         : {len(llm_ids)}")
    logging.info(f"IDs communs               : {len(matching)}")
    if missing_fw:
        logging.warning(f"⚠️ IDs présents dans entries mais absents du framework : {sorted(list(missing_fw))[:10]}")
    if missing_llm:
        logging.warning(f"⚠️ IDs présents dans framework mais absents du LLM : {sorted(list(missing_llm))[:10]}")
    logging.info("────────────────────────────────────────────────────────")

    # --- Fusion principale (left pour conserver toutes les entries)
    df_merged = df_entries.merge(df_fw_sub, on="framework_id", how="left")

    # --- Colonnes finales (ne garder que ce qui existe pour éviter KeyError)
    final_cols = [
        "framework_id",
        "flag",
        "page_number",
        "whitepaper_excerpt",
        "output",
        "metric_id",
        "title",
        "category",
        "llm_prompt",
        "anomaly_detected",
    ]
    existing_final = [c for c in final_cols if c in df_merged.columns]
    df_merged = df_merged[existing_final]

    # --- Convertit NaN → None pour un JSON propre (null)
    df_merged = df_merged.where(pd.notna(df_merged), None)

    # --- Sauvegarde JSON enrichi (optionnelle si agg_path est fourni)
    try:
        payload = json.dumps(
            df_merged.to_dict(orient="records"),
            ensure_ascii=False,
            indent=2
        )
        if agg_path:
            agg_path.write_text(payload, encoding="utf-8")
            logging.info(f"💾 Fusion enrichie sauvegardée : {agg_path.name}")
        else:
            # Aucun fichier intermédiaire à écrire (comportement voulu)
            logging.info("ℹ️ Fusion enrichie non sauvegardée (agg_path=None)")
    except Exception as e:
        logging.error(f"❌ Erreur lors de la sauvegarde du merge enrichi : {e}")


    logging.info(f"✅ Fusion framework ↔ entries terminée ({len(df_merged)} lignes).")
    return df_merged.to_dict(orient="records")


# --- 1. Nouvelle signature avec nombre de chunks WP ---
@log_execution_time
def build_llm_analysis_by_framework_blocks_stage1(
    file_processed: str,
    num_wp_blocks: int,
    model: str,
    provider: str
) -> Tuple[pd.DataFrame, list[dict], dict]:
    """
    Construit l’analyse complète du whitepaper par blocs et par chunks,
    en déléguant tous les appels LLM à `call_llm_with_retry_stage1`.

    🔹 Rôle :
      - Charger le framework associé au whitepaper.
      - Segmenter le texte OCR en `num_wp_blocks`.
      - Préparer les prompts LLM pour chaque métrique.
      - Déléguer les appels IA à `call_llm_with_retry_stage1`, indépendamment du provider.
      - Agréger les résultats multi-chunks en un seul JSON normalisé.

    Args:
        file_processed (str): Nom du fichier texte du whitepaper.
        num_wp_blocks (int): Nombre de blocs textuels à analyser.
        model (str): Nom du modèle à utiliser (défini dans `config.py`).
        provider (str): Provider ("openai" ou "gemini") selon la configuration active.

    Returns:
        tuple:
            df_fw (pd.DataFrame): DataFrame enrichi du framework.
            entries_aggregated (list[dict]): Liste complète des résultats agrégés.
            monitoring_data (dict): Données d’usage et de coût des appels LLM.

    Notes:
        - Cette fonction ne dépend plus d’aucune instance `ChatOpenAI`.
        - Tous les appels IA passent exclusivement par `call_llm_with_retry_stage1()`.
        - Les providers et modèles actifs sont déterminés automatiquement.
    """

    file_processed = validate_file_processed(file_processed)

    # Instructions optionnelles pour guider le LLM (vides par défaut).
    extra_instructions = ""
    # 0) Start monitoring timer
    start = time.time()  # <-- Garde ce timer global
    logging.info("[time] build_llm_analysis_by_framework_blocks_stage1 started")


    timestamp = datetime.utcnow().strftime("%y_%m_%d-%H-%M-%S")
    agg_path = None


    # --- Étape 1: Framework ---
    t0 = time.time()
    framework_json, df_fw = get_whitepaper_framework(conf.RWA_API_KEY)
    fw_blocks = split_framework_into_blocks(framework_json)
    logging.info(f"[time] étape Framework load+split {time.time()-t0:.2f}s")

    # --- Étape 2: Split WP ---
    t1 = time.time()
    text = Path(conf.ocr_folder, f"{file_processed}.txt").read_text(encoding="utf-8")
    archive_and_verify_wp_chunks(text, num_wp_blocks, file_processed, conf.report_folder)
    wp_chunks = split_whitepaper_into_chunks(text, num_wp_blocks)
    logging.info(f"[time] étape WP split {time.time()-t1:.2f}s")

    # 3) initialiser all_parsed: Liste finale des entrées à insérer en base de données ;
    # agrège toutes les métriques et métadonnées enrichies issues de chaque bloc et chunk.
    all_parsed: List[Dict[str, Any]] = []


    # 4) boucle sur les blocks and chunks
    t2 = time.time()
    for block_key, metrics in fw_blocks.items():
        logging.info(f"=== Traitement du bloc {block_key} ({len(metrics)} metrics) ===")
        # save block metrics for inspection
        (conf.chunks_dir / f"{file_processed}_{block_key}_metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
        )


        # --- Étape préliminaire : filtrage des métriques pour le LLM ---
        # (ne garde que id, category, llm_prompt)
        metrics_for_prompt = filter_metrics_for_llm(metrics)


        # Convertit le bloc filtré en JSON pour injection dans le prompt
        metrics_str = json.dumps(metrics_for_prompt, ensure_ascii=False, indent=2)



        # Boucle numérotée sur chaque chunk du whitepaper :
        # - `idx` : index commençant à 1, utile pour le logging et nommage des fichiers de sortie
        # - `chunk` : texte correspondant à ce segment du whitepaper
        # Pour chaque bloc de métriques (`block_key`) et chaque `chunk`, on construit le prompt,
        # on appelle le LLM, on enrichit le résultat, puis on stocke les données parsées.
        for idx, chunk in enumerate(wp_chunks, start=1):
            metric_id_block = metrics[0].get("metric_id", "N/A") if metrics else "N/A" # pour récupérer metric_id du block_key et l'afficher en logging
            logging.info(f"→ Chunk {idx}/{len(wp_chunks)} pour bloc {block_key} | metric_id: {metric_id_block}")

            # build prompt
            prompt_stage1 = prompt_rwa.prompt_template_agent_1.substitute(
                metrics_block=metrics_str,
                whitepaper_chunk=chunk,
                instructions=extra_instructions
            )


            # Log prompt entier
            #logging.info(f"[TRACE] Prompt for {block_key}_chunk{idx}: {prompt_stage1}")

            logging.info(
                f"[Stage1][PromptInfo] bloc={block_key} | chunk={idx}/{len(wp_chunks)} | "
                f"metrics_count={len(metrics_for_prompt)} | "
                f"metrics_json_len={len(metrics_str)} | "
                f"chunk_len={len(chunk)} | "
                f"prompt_len={len(prompt_stage1)}"
            )


            messages = [
                SystemMessage(content="You are an RWA framework expert."),
                HumanMessage(content=prompt_stage1)
            ]

            # 1) appel LLM avec x tentatives et forçage de réponse conforme JSON si échec ou non
            # last_text_output juste utile pour debug = dernier txt généré par le LLM -> inutilisé
            data, last_text_output = call_llm_with_failover_stage1_chunk(
                chunk=messages,
                metric_id=metric_id_block,
                file_processed=file_processed,
                framework_label=block_key
            )


            # À ce stade, `data` est toujours non vide :
            # - soit le LLM a répondu
            # - soit une exception a été levée plus haut (failover épuisé)
            # Il n’existe donc plus de cas "data vide" à gérer ici.

            # On construit une table de correspondance {framework_id: réponse_LLM}
            # pour pouvoir réinjecter chaque métrique dans l’ordre original,
            # garantissant une liste parfaitement alignée sur `metrics`.
            parsed_map = {
                e.get("framework_id", e.get("id")): e
                for e in data
            }
            # Reconstruction de `data` pour correspondre exactement à l’ordre de `metrics`
            new_data = []
            for m in metrics:
                fid = m["id"]
                if fid in parsed_map:
                    # Si le LLM a renvoyé un résultat pour cette métrique, on l’utilise
                    e = parsed_map[fid]
                    new_data.append({
                        "framework_id":        fid,
                        "flag":                e["flag"],
                        "page_number":         e["page_number"],
                        "whitepaper_excerpt":  e["whitepaper_excerpt"],
                        "output":              e["output"],
                    })
                else:
                    logging.warning(
                        f"[Stage1][MissingMetric] framework_id={fid} absent de la réponse LLM — "
                        "injection d'une entrée par défaut (flag=red)."
                    )

                    # En cas de métrique manquante dans la réponse LLM,
                    # on crée une entrée « vide » conforme pour cette métrique
                    new_data.append({
                        "framework_id":        fid,
                        "flag":                ["red"],
                        "page_number":         [0],
                        "whitepaper_excerpt":  ["—"],
                        "output":              ["— LLM error on the searched metric"],
                    })
            data = new_data

            if all(e["flag"] == ["red"] for e in data):
                logging.warning(
                    f"[Stage1][AllRedChunk] bloc={block_key} | chunk={idx}/{len(wp_chunks)} | "
                    f"toutes les métriques sont red pour ce chunk."
                )

            # -----------------------------------------------------------------------
            # Redressement strict des flags "red"
            # Objectif :
            # - Garantir que pour chaque index i :
            #     si flag[i] == "red" alors whitepaper_excerpt[i] == "—"
            # - Compléter la liste whitepaper_excerpt si elle est plus courte que flag
            # - Archiver UNE SEULE FOIS par chunk si au moins un redressement a été appliqué
            # -----------------------------------------------------------------------

            chunk_redressment_applied = False
            redressed_entries_snapshot = []

            for entry in data:
                flags = entry.get("flag", []) or []
                excerpts = entry.get("whitepaper_excerpt", []) or []

                # Snapshot des longueurs AVANT correction (utile pour debug)
                before_ln = (len(flags), len(excerpts))

                # Complète excerpts si plus court que flags
                if len(excerpts) < len(flags):
                    excerpts.extend(["—"] * (len(flags) - len(excerpts)))
                    chunk_redressment_applied = True

                # Force "—" pour chaque flag red
                for i, fl in enumerate(flags):
                    if fl == "red" and excerpts[i] != "—":
                        excerpts[i] = "—"
                        chunk_redressment_applied = True

                entry["whitepaper_excerpt"] = excerpts

                after_ln = (len(flags), len(excerpts))
                if before_ln != after_ln or chunk_redressment_applied:
                    redressed_entries_snapshot.append({
                        "framework_id": entry.get("framework_id"),
                        "lengths_before": before_ln,
                        "lengths_after": after_ln,
                    })


            # 3) Enrichissement des entrées : ajout de métadonnées manquantes sans écraser
            for m, e in zip(metrics, data):
                # ne pas écraser le fallback déjà enrichi
                e.setdefault("id", m["id"])
                e.setdefault("category", m.get("category"))
                e.setdefault("llm_prompt", m.get("llm_prompt"))
                e.setdefault("file_processed", file_processed)

            # Sauvegarde JSON du chunk parsé pour audit et debug dans le folder conf.parsed_dir = llm_parsed_chunks
            # => liste de page_number [16,16,18] avec listes excerpt et outpu associées PAR chunk de WP 1 à x
            parsed_file = conf.parsed_dir / f"{file_processed}_{block_key}_chunk{idx}_parsed.json"
            parsed_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            logging.info(f"Saved parsed chunk: {parsed_file}")

            # Ajout à la Liste finale des entrées pour agrégation ultérieure
            all_parsed.extend(data)
    logging.info(f"[time] étape block et chunk {time.time()-t2:.2f}s")


    # 5) Aggregate across all parsed entries (multi-chunks) by framework_id
    t3 = time.time()
    logging.info("Nombre total d’entrées parsées : %d", len(all_parsed))
    all_parsed.sort(key=lambda e: e["framework_id"])

    # 5.a) Construire le dict {framework_id: [entries...]}
    entries_by_framework: dict[int, list[dict]] = {}
    for fid, group in groupby(all_parsed, key=lambda e: e["framework_id"]):
        entries_by_framework[fid] = list(group)

    # 5.b) Agréger selon la priorité de flags (green > amber > red), par sous-point
    entries = aggregate_stage1_entries_by_flag(entries_by_framework)

    logging.info(f"[time] étape aggregation (stage1) {time.time()-t3:.2f}s")


    # 6) Ajustement des numéros de page et contrôle pages/excerpts (Stage 1)
    t4 = time.time()

    # Chemin vers le TXT OCR du whitepaper
    ocr_txt_path = Path(conf.ocr_folder, f"{file_processed}.txt")

    # ✅ Lire le contenu texte (str) attendu par validate_stage1_page_consistency()
    try:
        whitepaper_text = ocr_txt_path.read_text(encoding="utf-8")
    except Exception as e:
        logging.error(f"❌ Impossible de lire le fichier OCR : {ocr_txt_path} → {e}")
        whitepaper_text = ""  # on laisse vide, les anomalies 'excerpt not found' / 'page missing' remonteront

    # ✅ Passer le bon nom de base de fichier (file_processed) à la place de file_base_name
    entries = validate_stage1_page_consistency(
        entries=entries,
        whitepaper_text=whitepaper_text,
        file_base_name=file_processed,
        log_folder=conf.log_folder
    )

    logging.info(f"[time] étape ajustement pages (stage1) {time.time()-t4:.2f}s")

    # 7️⃣ Fusion enrichie framework ↔ entries (déjà préparée par agg_path ci-dessus)
    #   On ne sauvegarde plus de fichier entries_aggregated intermédiaire.
    #   Seule la version finale enrichie sera archivée (llm_aggregated_with_anomalies_stage1.json)
    entries = merge_stage1_framework_traceability(df_fw, entries, agg_path)

    # 💾 Sauvegarde officielle Stage 1 (version enrichie) — archivage direct
    final_stage1_path = conf.report_stage_1_folder / f"{file_processed}_{timestamp}_llm_aggregated_with_anomalies_stage1.json"
    save_json_file(final_stage1_path, entries)
    logging.info(f"📁 Fichier Stage 1 archivé : {final_stage1_path.name}")



    # 8️⃣ Aggregate monitoring et stats
    duration_sec = time.time() - start
    monitoring_data = aggregate_llm_monitoring_files(
        file_processed=file_processed,
        process_duration_sec=duration_sec,
        stage1_final_json_path=final_stage1_path
    )

    return df_fw, entries, monitoring_data




# ──────────────────────────────────────────────────────────────────────────────
# Stage 2 : Appels LLM agent 2 delete redondance sous-points
# ──────────────────────────────────────────────────────────────────────────────

def call_llm_with_failover_stage2_entry(
    messages,
    file_processed: str,
    framework_id: int | str
) -> tuple:
    """
    Stage 2 — Appel LLM avec failover entre providers (par framework_id).

    Fonctionnement :
    - Pour une entrée donnée (identifiée par framework_id), la fonction parcourt
      les providers définis dans STAGE2_PROVIDER_ORDER (ordre de bascule).
    - Pour chaque provider, elle appelle call_llm_with_retry_stage2(), qui gère
      jusqu'à max_retries tentatives (retry) sur le même input (messages).
    - Si un provider échoue après épuisement de ses retry, le traitement bascule
      automatiquement vers le provider suivant.
    - Si tous les providers échouent, une exception est levée afin de signaler
      l'échec du Stage 2 pour ce report.

    Remarques importantes :
    - Il n'existe qu'un seul mécanisme de failover : l'ordre des providers dans
      STAGE2_PROVIDER_ORDER.
    - Aucun traitement spécifique dépendant du provider (GPT ou Gemini) n'est
      appliqué ici.
    - La réussite d'une tentative repose uniquement sur le parsing JSON final
      (réussi) effectué dans call_llm_with_retry_stage2().
    """
    last_error = None

    for provider, model in conf.STAGE2_PROVIDER_ORDER:
        if not is_provider_available_for_stage("stage2", provider):
            logging.warning(
                f"[Stage2][EntryFailover] ⛔ Provider={provider.upper()} SKIP "
                f"(panne simulée active) | framework_id={framework_id}"
            )
            continue
        logging.info(
            f"[Stage2][EntryFailover] ▶ Tentative provider={provider.upper()} | model={model} | framework_id={framework_id}"
        )
        try:
            data, raw_text = call_llm_with_retry_stage2(
                messages=messages,
                file_processed=file_processed,
                provider=provider,
                model=model,
                max_retries=5
            )


            # Si le LLM renvoie quelque chose d'exploitable
            if data:
                logging.info(
                    f"[Stage2][EntryFailover] ✅ Succès provider={provider.upper()} | framework_id={framework_id}"
                )
                return data, raw_text, provider, model

            # Réponse vide => on considère l'appel comme non concluant, on bascule
            logging.warning(
                f"[Stage2][EntryFailover] ⚠ Réponse vide sur {provider.upper()} "
                f"→ bascule provider suivant | framework_id={framework_id}"
            )

        except Exception as e:
            last_error = e
            logging.warning(
                f"[Stage2][EntryFailover] ❌ Échec provider={provider.upper()} "
                f"({type(e).__name__}: {e}) → bascule provider suivant | framework_id={framework_id}"
            )
            continue

    msg = (
        f"[Stage2][EntryFailover] 🛑 Tous les providers ont échoué pour framework_id={framework_id} "
        f"(file={file_processed}). Dernière erreur: {type(last_error).__name__ if last_error else 'N/A'}: {last_error}"
    )
    logging.error(msg)
    raise RuntimeError(msg)




def call_llm_with_retry_stage2(
    messages: List[Dict[str, Any]],
    file_processed: str,
    provider: str = "openai",
    model: str = conf.DEFAULT_LLM_MODEL,
    max_retries: int = 5,
    backoff_seconds: float = 15.0,
) -> Tuple[Dict[str, Any], str]:
    """Appelle un provider Stage 2 avec retry local et parsing JSON strict."""
    validate_file_processed(file_processed)
    last_text_out = ""

    for attempt in range(1, max_retries + 1):
        try:
            if provider.lower() == "gemini":
                response = _gemini_generate_json(
                    model=model,
                    prompt_text=_messages_to_prompt_text(messages),
                    max_output_tokens=16384,
                )
                text_out = (getattr(response, "text", "") or "").strip()
                finish_reason = _gemini_finish_reason(response)
                prompt_tokens, completion_tokens, total_tokens = _gemini_usage_tokens(response)
            elif provider.lower() == "openai":
                if not conf.OPENAI_API_KEY:
                    raise RuntimeError("Clé OpenAI manquante.")
                llm = ChatOpenAI(
                    model_name=model,
                    temperature=0,
                    max_tokens=16384,
                    openai_api_key=conf.OPENAI_API_KEY,
                    timeout=conf.LLM_REQUEST_TIMEOUT_SECONDS,
                    max_retries=0,
                )
                result = llm.generate([messages])
                text_out = result.generations[0][0].text.strip()
                token_usage = (result.llm_output or {}).get("token_usage", {}) or {}
                prompt_tokens = int(token_usage.get("prompt_tokens", 0) or 0)
                completion_tokens = int(token_usage.get("completion_tokens", 0) or 0)
                total_tokens = prompt_tokens + completion_tokens
                finish_reason = "N/A"
            else:
                raise ValueError(f"Provider LLM non supporté : {provider}")

            if not text_out:
                raise ValueError("Stage 2 : réponse LLM vide.")

            last_text_out = text_out
            log_raw_llm_output(file_processed, provider, "stage2", text_out)
            logging.info(
                "[Stage2][%s][Attempt %d/%d] Réponse reçue (%d caractères, tokens=%d/%d/%d, finish=%s)",
                provider.upper(),
                attempt,
                max_retries,
                len(text_out),
                prompt_tokens,
                completion_tokens,
                total_tokens,
                finish_reason,
            )

            clean_text = _clean_json_output(text_out, expect_array=False)
            if clean_text.count("{") >= 1 and clean_text.count("}") >= 1:
                clean_text = clean_text[: clean_text.rfind("}") + 1]

            data = json.loads(clean_text)
            if not isinstance(data, dict):
                raise ValueError(
                    f"Stage 2 : sortie JSON invalide (dict attendu, reçu {type(data).__name__})."
                )
            return data, last_text_out

        except OpenAIRateLimitError as exc:
            retry_after = getattr(exc, "retry_after", None)
            if not retry_after and getattr(exc, "response", None) is not None:
                retry_after = exc.response.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else backoff_seconds * attempt
            logging.warning(
                "[%s %d/%d] Rate limit ; nouvelle tentative dans %.1fs.",
                provider.upper(),
                attempt,
                max_retries,
                wait,
            )
        except Exception as exc:
            wait = backoff_seconds * attempt
            logging.warning(
                "[Stage2][%s %d/%d] Échec %s : %s",
                provider.upper(),
                attempt,
                max_retries,
                type(exc).__name__,
                exc,
            )
            if any(token in str(exc).lower() for token in ("429", "quota", "rate limit")):
                wait = max(wait, 20.0 * attempt)

        if attempt < max_retries:
            time.sleep(wait)

    logging.error("Provider %s épuisé après %d tentatives Stage 2.", provider.upper(), max_retries)
    return {}, last_text_out


def determine_unique_flag(flags: List[str]) -> str:
    """
    Détermine le flag final (unique) à partir d'une liste de flags.
    Règles :
      - Tous "green" → "green"
      - Tous "amber" → "amber"
      - Tous "red"   → "red"
      - Mélange (green/amber/red ou amber/red) → "amber"
    """
    if not flags:
        return "amber"  # sécurité si liste vide

    unique = set(flags)
    if unique == {"green"}:
        return "green"
    if unique == {"amber"}:
        return "amber"
    if unique == {"red"}:
        return "red"
    # mélange
    return "amber"



def convert_flags_in_json(json_path: str) -> Path:
    """
    Convertit et normalise les flags issus des sorties LLM avant publication.

    Rôle :
    - Transformer les flags internes (issus du Stage 1 / Stage 2) en une
      représentation JSON homogène et exploitable côté base de données.
    - Garantir la cohérence des valeurs de flag à l’échelle d’un framework_id.

    Comportement :
    - Applique la logique de priorité définie pour les flags (ex. red > amber > green).
    - Nettoie les valeurs invalides ou vides.
    - Prépare une structure JSON prête à être postée sans dépendance
      aux structures internes du pipeline.

    Remarques :
    - Cette fonction ne réalise aucun appel LLM.
    - Elle n’effectue aucune écriture en base de données.
    - Elle ne doit pas lever d’exception bloquante pour le pipeline.
    """
    src_path = Path(json_path)
    if not src_path.exists():
        raise FileNotFoundError(f"Fichier introuvable : {src_path}")

    logging.info(f"🔍 Lecture du JSON : {src_path.name}")
    with open(src_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    new_data = []
    for entry in data:
        flags = entry.get("flag", [])
        # Pour le JSON final à psoter : ajoute flag_internals et calcul l'unique flag de la metric_id
        entry["flag_internals"] = flags
        entry["flag"] = determine_unique_flag(flags)

        # 🔹 Réordonne les clés pour lisibilité
        ordered_entry = {
            "framework_id": entry.get("framework_id"),
            "flag": entry.get("flag"),
            "flag_internals": entry.get("flag_internals"),
        }
        # ajoute toutes les autres clés après
        for k, v in entry.items():
            if k not in ordered_entry:
                ordered_entry[k] = v

        new_data.append(ordered_entry)

    # Sauvegarde dans le dossier défini pour les rapports post-DB
    out_path = conf.report_post_db_folder / f"{src_path.stem}_stage2_flaginternals_simplified.json"
    save_json_file(out_path, new_data)
    logging.info(f"💾 JSON enrichi enregistré : {out_path.name}")
    return out_path


def post_entries_from_llm(
    api_key: str,
    report_id: str,
    entries: List[Dict[str, Any]],
    ai_model: Optional[str] = None,
    base_url: str = conf.API_BASE_URL
) -> Dict[str, Any]:
    """
    Publie les entrées finales issues du pipeline LLM vers le backend applicatif.

    Cette fonction ne modifie jamais le statut global du rapport. Elle collecte
    le résultat de chaque POST et renvoie un résumé au niveau supérieur. Le
    pipeline principal reste ainsi l'unique propriétaire des transitions
    ``COMPLETED`` / ``ERROR`` et ne peut pas masquer un échec partiel.
    """

    url = f"{base_url}{conf.API_REPORT_UPDATE_PATH}"
    headers = {"X-Api-Key": api_key}
    formatted_entries = []

    for e in entries:
        # --- Récupération des valeurs principales ---
        flag_str = str(e.get("flag", "red"))
        page_numbers = e.get("page_number", [])
        flag_internals = e.get("flag_internals", [])

        # --- Conversion des champs textuels en listes de chaînes ---
        def ensure_list(value):
            """Garantit que la valeur est une liste de chaînes non vides."""
            if value is None:
                return [""]
            if isinstance(value, list):
                return [str(v).strip() for v in value if v is not None]
            return [str(value).strip()]

        excerpt_list = ensure_list(e.get("whitepaper_excerpt"))
        output_list = ensure_list(e.get("output"))

        formatted_entries.append({
            "framework_id":       int(e["framework_id"]),
            "flag":               flag_str,
            "flag_internals":     flag_internals,
            "page_number":        page_numbers,
            "whitepaper_excerpt": excerpt_list,
            "output":             output_list,
        })

    # --- Corps de la requête API ---
    body = {"report_id": report_id, "entries": formatted_entries}
    if ai_model:
        body["ai_model"] = ai_model

    report_id_digest = hashlib.sha256(str(report_id).encode("utf-8")).hexdigest()[:16]
    payload_path = conf.report_post_db_folder / f"payload_post_{report_id_digest}.json"
    save_json_file(payload_path, body)
    logging.info("💾 Payload pré-POST archivé : %s", payload_path.name)


    # --- Envoi à l’API, framework par framework ---
    total = len(formatted_entries)
    success_count = 0
    error_count = 0

    for e in formatted_entries:
        fid = e["framework_id"]
        single_body = {"report_id": report_id, "entries": [e]}
        if ai_model:
            single_body["ai_model"] = ai_model

        try:
            resp = requests.post(url, headers=headers, json=single_body, timeout=conf.API_REQUEST_TIMEOUT)
            status = resp.status_code
            try:
                response_payload = resp.json()
            except ValueError:
                response_payload = {}

            if status == 200 and response_payload.get("message") == "success":
                logging.info("✅ framework_id %s : OK", fid)
                success_count += 1
            else:
                logging.error("❌ framework_id %s : erreur HTTP %s", fid, status)
                error_count += 1

        except requests.RequestException as exc:
            logging.error(f"❌ Exception lors du POST framework_id {fid} : {exc}")
            error_count += 1

    # Le statut global du rapport est volontairement géré par pipeline.py.
    # Cette séparation évite qu'une fonction de transport réseau marque un rapport
    # COMPLETED alors qu'une étape métier supérieure a détecté une anomalie.
    logging.info(
        "📊 Envoi terminé : %s/%s succès, %s erreurs",
        success_count,
        total,
        error_count,
    )
    return {"success": success_count, "error": error_count, "total": total}


# ──────────────────────────────────────────────────────────────────────────────
# Stage 4 : monitoring et métriques d’exécution
# ──────────────────────────────────────────────────────────────────────────────
def compute_prepost_flag_stats(entries: list[dict]) -> dict:
    """
    Calcule des statistiques de flags à partir des entrées FINALES pré-POST (JSON enrichi).

    Deux niveaux de stats sont produits :
    1) framework_stats : comptage des flags uniques (champ `flag`, 1 par framework_id)
       -> correspond au statut final réellement posté sur la DB pour chaque framework_id.
    2) flag_internals_stats : comptage de tous les flags internes (champ `flag_internals`, liste)
       -> correspond à un KPI "par sous-point/citation" (et non par framework_id).

    On calcule aussi `average_citations_per_entry` sur la longueur de `page_number`,
    car c’est le proxy existant de "nombre de citations retenues".
    """
    framework_counts = {"green": 0, "amber": 0, "red": 0}
    internals_counts = {"green": 0, "amber": 0, "red": 0}

    citations_lens: list[int] = []
    total_internals = 0

    for e in entries or []:
        # --- Stats framework-level (1 flag unique par framework_id) ---
        f = str(e.get("flag", "") or "").strip().lower()
        if f not in framework_counts:
            f = "red"
        framework_counts[f] += 1

        # --- Moyenne citations (sur page_number) ---
        pages = e.get("page_number", [])
        citations_lens.append(len(pages) if isinstance(pages, list) else 0)

        # --- Stats internals-level (aplati sur toutes les listes flag_internals) ---
        fi = e.get("flag_internals", [])
        if isinstance(fi, list):
            for x in fi:
                v = str(x or "").strip().lower()
                if v in internals_counts:
                    internals_counts[v] += 1
                    total_internals += 1
        else:
            # Tolérance si jamais un type inattendu apparaît (ne doit pas arriver selon le schéma attendu)
            v = str(fi or "").strip().lower()
            if v in internals_counts:
                internals_counts[v] += 1
                total_internals += 1

    total_framework_ids = sum(framework_counts.values())
    avg_citations = round(sum(citations_lens) / total_framework_ids, 2) if total_framework_ids else 0.0

    return {
        "framework_stats": {
            "total_framework_ids": total_framework_ids,
            **framework_counts,
            "average_citations_per_entry": avg_citations
        },
        "flag_internals_stats": {
            "total_internal_flags": total_internals,
            **internals_counts
        }
    }



def update_latest_monitoring_file(file_processed: str, updates: dict) -> Optional[Path]:
    """
    Met à jour le dernier fichier de monitoring agrégé correspondant à `file_processed`.

    Cas d'usage
    ----------
    Le monitoring global (fichier *_monitoring_*.json) est généré en fin de Stage 1.
    Les chemins Stage 2 et pré-POST n'existent pas encore à ce moment-là.
    Cette fonction permet donc de compléter le monitoring a posteriori, sans refonte du pipeline.

    Paramètres
    ----------
    file_processed : str
        Nom de base du fichier traité (sans extension).
    updates : dict
        Dictionnaire {clé: valeur} à fusionner dans le JSON de monitoring.
        Les clés existantes sont écrasées (comportement volontaire pour "corriger" une valeur).

    Retour
    ------
    Optional[Path]
        Le chemin du fichier monitoring mis à jour, ou None si aucun fichier trouvé.
    """
    file_processed = validate_file_processed(file_processed)

    # On prend le monitoring le plus récent pour ce `file_processed`
    files = sorted(
        glob(str(Path(conf.monitoring_folder, f"{file_processed}_monitoring_*.json"))),
        key=os.path.getmtime,
        reverse=True
    )
    if not files:
        logging.warning(f"⚠️ Aucun fichier de monitoring trouvé pour {file_processed} — update ignorée.")
        return None

    monitoring_path = Path(files[0])

    try:
        with open(monitoring_path, "r+", encoding="utf-8") as f:
            data = json.load(f)

            # Fusion des champs demandés dans le monitoring (écrase si déjà présent)
            for k, v in updates.items():
                data[k] = v

            # Réécriture complète pour garantir un JSON propre (indenté) et éviter des résidus
            f.seek(0)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.truncate()

            # Sécurise l'écriture sur disque (utile en prod Docker/VM)
            f.flush()
            os.fsync(f.fileno())

        logging.info(f"🧾 Monitoring mis à jour : {monitoring_path.name}")
        return monitoring_path

    except Exception as e:
        logging.warning(f"⚠️ Impossible de mettre à jour le monitoring {monitoring_path.name} : {e}")
        return None



def compute_output_with_missing_subpoints(entries: list[dict]) -> list:
    """
    Contrôle de cohérence : vérifie que tous les sous-points '(1)', '(2)', ... présents dans llm_prompt
    existent au moins une fois dans la liste output.

    Règles :
    - Le nombre attendu de sous-points dépend uniquement du plus grand '(n)' trouvé dans llm_prompt.
    - Les items de output sont censés commencer par '(n)'. On fait un parsing strict au début de chaîne.
    - Résultat compact :
        * [None] si aucun sous-point manquant sur l'ensemble des entries
        * sinon une liste de dicts : [{"framework_id": 120, "missing_subpoints": ["(2)", "(4)"]}, ...]

    Important :
    - Fonction volontairement “pure” (ne lit/écrit aucun fichier), pour être appelée depuis le pipeline.
    """
    # Regex : capture (n) dans le prompt ; et capture strictement un préfixe "(n)" au début d’un output
    re_prompt_subpoints = re.compile(r"\((\d+)\)")
    re_output_prefix = re.compile(r"^\(\s*(\d+)\s*\)")

    missing_list: list[dict] = []

    for e in entries:
        llm_prompt = str(e.get("llm_prompt", "") or "")
        prompt_nums = re_prompt_subpoints.findall(llm_prompt)
        if not prompt_nums:
            # Aucun sous-point attendu => rien à contrôler
            continue

        # Nombre attendu = max(n) dans le prompt
        try:
            max_n = max(int(x) for x in prompt_nums)
        except Exception:
            # Si conversion impossible, on n’ajoute pas de bruit dans le monitoring
            continue

        expected = set(range(1, max_n + 1))

        out = e.get("output", [])
        if out is None:
            out_list = []
        elif isinstance(out, list):
            out_list = out
        else:
            # Tolérance : si output n’est pas une liste (cas rare), on le traite comme liste d’un item
            out_list = [out]

        found: set[int] = set()
        for item in out_list:
            s = str(item or "").strip()
            m = re_output_prefix.match(s)
            if m:
                try:
                    found.add(int(m.group(1)))
                except Exception:
                    pass

        missing = sorted(expected - found)
        if missing:
            # framework_id peut être str/int selon les étapes : on évite de casser si conversion échoue
            fid_raw = e.get("framework_id")
            try:
                fid = int(fid_raw)
            except Exception:
                fid = fid_raw

            missing_list.append({
                "framework_id": fid,
                "missing_subpoints": [f"({n})" for n in missing],
            })

    return [None] if not missing_list else missing_list



def aggregate_llm_monitoring_files(
    file_processed: str,
    process_duration_sec: float,
    stage1_final_json_path: Optional[Path] = None
) -> dict:
    """
    Crée le JSON de monitoring global (création en fin de Stage 1).

    Important :
    - On ne suit plus ici les tokens / coûts / call_count.
    - Le monitoring sert désormais uniquement à tracer :
        * provider/model par défaut (info indicative),
        * durée du process,
        * timestamps,
        * chemins des JSON (Stage 1 tout de suite ; Stage 2 + pré-POST complétés plus tard)
        * autres infos ajoutées via update_latest_monitoring_file().

    Les champs Stage 2 / pré-POST seront renseignés a posteriori :
    - pipeline.py : report_id / project_id
    - step_post_entries() : stage2_final_json_path / pre_post_json_path / contrôle sous-points
    """
    file_processed = validate_file_processed(file_processed)
    monitoring_dir = Path(conf.monitoring_folder)
    monitoring_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Format duration mm:ss (aligné avec le format de monitoring attendu)
    dur = str(datetime.utcfromtimestamp(process_duration_sec).strftime("%H:%M:%S"))
    minutes, seconds = dur.split(":")[1:3]
    duration_str = f"{int(minutes):02d}:{int(float(seconds)):02d}"

    # Chemin Stage 1 en absolu (si fourni)
    stage1_path_str = str(stage1_final_json_path.resolve()) if stage1_final_json_path else None

    summary = {
        "file_processed": file_processed,
        "provider": conf.DEFAULT_PROVIDER,
        "model": conf.DEFAULT_LLM_MODEL,

        "process_duration": duration_str,
        "aggregated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),

        # Traçabilité des artefacts JSON (complétés au fil de l'exécution)
        "stage1_final_json_path": stage1_path_str,
        "stage2_final_json_path": None,
        "pre_post_json_path": None,
    }

    utc_now_str = datetime.utcnow().strftime("%Y_%m_%d-%H-%M-%S")
    summary_path = monitoring_dir / f"{file_processed}_monitoring_{conf.DEFAULT_LLM_MODEL}_{utc_now_str}.json"

    save_json_file(summary_path, summary)
    logging.info(f"✅ Monitoring global enregistré : {summary_path.name}")
    return summary





def _clean_json_output(text: str, expect_array: bool | None = None) -> str:
    """
    Nettoie une sortie textuelle LLM afin d’en extraire un JSON exploitable.
    Stage 1 => expect_array = True
    Stage 2 => expect_array = False

    Rôle :
    - Supprimer les préfixes, suffixes ou commentaires parasites
      ajoutés par le LLM autour du JSON attendu.
    - Garantir que le texte retourné soit compatible avec un parsing JSON strict.

    Comportement :
    - Recherche les premières et dernières accolades pertinentes.
    - Élimine tout texte hors du bloc JSON principal.
    - Ne tente aucune correction sémantique du contenu.

    Remarques :
    - Cette fonction est volontairement conservatrice.
    - Toute erreur de parsing ultérieure doit être gérée par l’appelant.
    """
    if not text:
        return ""

    # Supprime balises ```json ou ``` éventuelles
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    # Extrait uniquement le bloc JSON principal (entre le 1er [/{ et le dernier ]/})
    start_idx = min(
        [i for i in [text.find("["), text.find("{")] if i != -1],
        default=0,
    )
    end_idx = max(text.rfind("]"), text.rfind("}"))
    if end_idx != -1 and end_idx > start_idx:
        text = text[start_idx:end_idx + 1]

    # Ne modifie pas la structure interne
    return text.strip()






# -------------------------------------------------------------------------------------
# LOGGING DES SORTIES BRUTES LLM
# -------------------------------------------------------------------------------------

def log_raw_llm_output(file_processed: str, provider: str, stage: str, raw_text: str) -> None:
    """Archive optionnellement une sortie LLM brute sans la recopier dans les logs applicatifs.

    Les sorties brutes peuvent contenir des extraits de documents confidentiels.
    Elles sont donc désactivées par défaut et ne sont écrites que si
    RWA_ENABLE_RAW_LLM_LOGS=true est explicitement configuré.
    """
    if not conf.ENABLE_RAW_LLM_LOGS or not raw_text:
        return

    try:
        safe_name = validate_file_processed(file_processed)
        dump_dir = conf.raw_llm_log_folder
        dump_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            dump_dir.chmod(0o700)
        except OSError:
            pass
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        dump_path = dump_dir / f"{safe_name}_{provider}_{stage}_{timestamp}.txt"
        save_text_file(dump_path, raw_text)
        logging.info(
            f"[LLM OUTPUT] Sortie brute archivée hors Git : {dump_path.name} "
            f"({len(raw_text)} caractères)"
        )
    except Exception as exc:
        logging.warning(f"[LLM OUTPUT] Archivage brut ignoré : {type(exc).__name__}")


# ---------------------------------------------------------
# Préparation de l'entrée Agent 3 :
# nettoyage structurel du JSON final issu du pipeline principal
# pour ne conserver que les champs utiles à la synthèse.
# ---------------------------------------------------------

def remove_unwanted_fields(data: Any) -> Any:
    """
    Supprime récursivement les champs qui ne sont pas utiles pour l'Agent 3.

    """
    keys_to_remove = {
        "anomaly_detected",
        "response_stage_2_delete_redundancy",
        "framework_id",
    }

    if isinstance(data, dict):
        return {
            key: remove_unwanted_fields(value)
            for key, value in data.items()
            if key not in keys_to_remove
        }

    if isinstance(data, list):
        return [remove_unwanted_fields(item) for item in data]

    return data


# ---------------------------------------------------------
# Réduction métier avant Agent 3 :
# - suppression des métriques entièrement green
# - suppression des sous-points internes green
# Objectif : concentrer l'Agent 3 uniquement sur les signaux
# amber / red réellement utiles à la synthèse.
# ---------------------------------------------------------
def remove_green_metrics_and_internal_indexes(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Applique les règles métier de réduction avant injection dans l'Agent 3.

    Règles :
    - si flag == "green", la métrique entière est supprimée,
    - sinon on supprime tous les index internes où flag_internals == "green",
      ainsi que les index correspondants dans page_number, whitepaper_excerpt et output,
    - si une métrique n'a plus aucun flag interne après filtrage, elle est supprimée.

    """
    cleaned_metrics: List[Dict[str, Any]] = []

    for metric in data:
        if not isinstance(metric, dict):
            continue

        if metric.get("flag") == "green":
            continue

        new_metric = deepcopy(metric)

        flag_internals = metric.get("flag_internals", [])
        page_numbers = metric.get("page_number", [])
        whitepaper_excerpts = metric.get("whitepaper_excerpt", [])
        outputs = metric.get("output", [])

        kept_indexes = [
            i for i, internal_flag in enumerate(flag_internals)
            if internal_flag != "green"
        ]

        new_metric["flag_internals"] = [
            flag_internals[i]
            for i in kept_indexes
            if i < len(flag_internals)
        ]
        new_metric["page_number"] = [
            page_numbers[i]
            for i in kept_indexes
            if i < len(page_numbers)
        ]
        new_metric["whitepaper_excerpt"] = [
            whitepaper_excerpts[i]
            for i in kept_indexes
            if i < len(whitepaper_excerpts)
        ]
        new_metric["output"] = [
            outputs[i]
            for i in kept_indexes
            if i < len(outputs)
        ]

        if not new_metric["flag_internals"]:
            continue

        cleaned_metrics.append(new_metric)

    return cleaned_metrics


# =====================================================================
# # Pour agent 3 : Fonctions utilitaires d'archivage
# =====================================================================

def _atomic_write_text(path: Path, text: str) -> None:
    """Écrit un artefact texte de façon atomique dans son dossier cible."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def save_json_file(path: Path, data: Any) -> None:
    """Sauvegarde un JSON lisible de façon atomique."""
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def save_text_file(path: Path, text: str) -> None:
    """Sauvegarde un texte brut de façon atomique."""
    _atomic_write_text(path, text)


# =====================================================================
# Pour agent 3 : Fonctions de redressement de sortie Agent 3
#
# =====================================================================

def extract_json_block_from_text(raw_text: str) -> str:
    """
    Extrait le bloc JSON principal d'un texte potentiellement pollué
    par du markdown ou du texte parasite.

    Cette fonction redresse uniquement la forme d'enveloppe.
    Elle n'invente jamais de contenu.
    """
    if not raw_text:
        return ""

    text = raw_text.strip()

    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    start_candidates = [idx for idx in (text.find("["), text.find("{")) if idx != -1]
    if not start_candidates:
        return text

    start_idx = min(start_candidates)
    end_idx = max(text.rfind("]"), text.rfind("}"))

    if end_idx != -1 and end_idx > start_idx:
        return text[start_idx:end_idx + 1].strip()

    return text.strip()


def normalize_section_name(section_name: str) -> str:
    """
    Normalise les noms de section proches du contrat attendu.
    """
    if not isinstance(section_name, str):
        return section_name

    normalized = section_name.strip().lower()
    normalized = normalized.replace("-", "_").replace(" ", "_")

    mapping = {
        "critical_weaknesses": "critical_weakness",
        "critical_weakness_analysis": "critical_weakness",
        "critical_weakness_top_3": "critical_weakness",
        "criticalweakness": "critical_weakness",
        "strategic_recommendation": "strategic_recommendations",
        "strategic_recommendations_top_4": "strategic_recommendations",
        "key_strategic_recommendations": "strategic_recommendations",
        "recommendations": "strategic_recommendations",
    }

    return mapping.get(normalized, normalized)


def normalize_key_name(key_name: str) -> str:
    """
    Normalise les noms de clés proches du contrat attendu.
    """
    if not isinstance(key_name, str):
        return key_name

    normalized = key_name.strip().lower().replace(" ", "_")

    mapping = {
        "titles": "title",
        "descriptions": "description",
    }

    return mapping.get(normalized, normalized)


def coerce_string_list(value: Any) -> List[str]:
    """
    Convertit une valeur en liste de chaînes sans inventer de contenu.

    Cas couverts :
    - chaîne simple -> [chaîne]
    - liste -> conversion élément par élément si possible
    - sinon -> liste vide
    """
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []

    if isinstance(value, list):
        result: List[str] = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                result.append(text)
        return result

    return []


def normalize_section_object(section_obj: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """
    Redresse un objet section presque conforme.

    Redressements autorisés :
    - renommage de clés proches,
    - renommage de section proche,
    - conversion chaîne -> liste de chaînes.

    Aucun contenu métier n'est inventé.
    """
    repairs: List[str] = []

    normalized_obj: Dict[str, Any] = {}
    for key, value in section_obj.items():
        normalized_key = normalize_key_name(key)
        if normalized_key != key:
            repairs.append(f"key:{key}->{normalized_key}")
        normalized_obj[normalized_key] = value

    if "section" in normalized_obj:
        original_section = normalized_obj["section"]
        normalized_section = normalize_section_name(original_section)
        if normalized_section != original_section:
            repairs.append(f"section:{original_section}->{normalized_section}")
        normalized_obj["section"] = normalized_section

    if "title" in normalized_obj:
        old_value = normalized_obj["title"]
        new_value = coerce_string_list(old_value)
        if new_value != old_value:
            repairs.append("coerce:title_to_list")
        normalized_obj["title"] = new_value

    if "description" in normalized_obj:
        old_value = normalized_obj["description"]
        new_value = coerce_string_list(old_value)
        if new_value != old_value:
            repairs.append("coerce:description_to_list")
        normalized_obj["description"] = new_value

    return normalized_obj, repairs


def convert_root_object_to_section_list(data: Any) -> Tuple[Any, List[str]]:
    """
    Convertit certains objets racine en liste de sections si le modèle
    a renvoyé un format voisin du contrat attendu.

    Cas supportés :
    - objet racine avec clés 'critical_weakness' et 'strategic_recommendations'
    - objet racine avec clé 'sections' contenant une liste
    """
    repairs: List[str] = []

    if isinstance(data, dict):
        if "sections" in data and isinstance(data["sections"], list):
            repairs.append("root:sections_wrapper_removed")
            return data["sections"], repairs

        if "critical_weakness" in data or "strategic_recommendations" in data:
            converted: List[Dict[str, Any]] = []

            if "critical_weakness" in data:
                value = data["critical_weakness"]
                if isinstance(value, dict):
                    converted.append({
                        "section": "critical_weakness",
                        "title": value.get("title", value.get("titles", [])),
                        "description": value.get("description", value.get("descriptions", [])),
                    })

            if "strategic_recommendations" in data:
                value = data["strategic_recommendations"]
                if isinstance(value, dict):
                    converted.append({
                        "section": "strategic_recommendations",
                        "title": value.get("title", value.get("titles", [])),
                        "description": value.get("description", value.get("descriptions", [])),
                    })

            if converted:
                repairs.append("root:section_object_to_list")
                return converted, repairs

    return data, repairs


# ---------------------------------------------------------
# Tolérance faible sur la sortie LLM Agent 3 :
# on corrige uniquement la forme JSON si elle est presque conforme,
# sans jamais inventer de contenu métier.
# ---------------------------------------------------------
def repair_agent3_output(raw_text: str) -> Dict[str, Any]:
    """
    Tente un redressement faible et déterministe de la sortie brute du LLM.

    La philosophie est stricte :
    - on redresse la forme,
    - on n'invente jamais le fond.

    Retour :
    {
        "data": ...,
        "was_repaired": bool,
        "repairs_applied": [...],
        "clean_text": "...",
    }
    """
    repairs_applied: List[str] = []

    clean_text = extract_json_block_from_text(raw_text)
    if clean_text != (raw_text or "").strip():
        repairs_applied.append("outer_text_removed")

    try:
        parsed_data = json.loads(clean_text)
    except Exception as e:
        raise ValueError(f"Sortie LLM non parseable en JSON après extraction : {e}") from e

    parsed_data, root_repairs = convert_root_object_to_section_list(parsed_data)
    repairs_applied.extend(root_repairs)

    if not isinstance(parsed_data, list):
        raise ValueError("La sortie Agent 3 doit être une liste JSON ou un objet racine redressable.")

    normalized_sections: List[Dict[str, Any]] = []
    for idx, item in enumerate(parsed_data):
        if not isinstance(item, dict):
            raise ValueError(f"L'élément d'index {idx} n'est pas un objet JSON.")
        normalized_item, item_repairs = normalize_section_object(item)
        normalized_sections.append(normalized_item)
        repairs_applied.extend(item_repairs)

    return {
        "data": normalized_sections,
        "was_repaired": bool(repairs_applied),
        "repairs_applied": repairs_applied,
        "clean_text": clean_text,
    }


# ---------------------------------------------------------
# Contrat strict de sortie Agent 3 :
# la synthèse doit contenir exactement 2 sections, avec
# 3 faiblesses critiques et 4 recommandations stratégiques.
# Toute sortie non conforme est rejetée.
# ---------------------------------------------------------
def validate_agent3_output(data: Any) -> List[Dict[str, Any]]:
    """
    Valide strictement le contrat de sortie Agent 3.

    Contrat attendu :
    - racine = liste de 2 objets
    - sections autorisées uniquement :
      * critical_weakness
      * strategic_recommendations
    - title et description = listes de chaînes
    - tailles identiques par section
    - critical_weakness = 3 items
    - strategic_recommendations = 4 items
    """
    if not isinstance(data, list):
        raise ValueError("Le JSON final Agent 3 doit être une liste.")

    if len(data) != 2:
        raise ValueError(f"Le JSON final Agent 3 doit contenir exactement 2 sections, reçu : {len(data)}.")

    by_section: Dict[str, Dict[str, Any]] = {}

    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Chaque section doit être un objet JSON.")

        expected_keys = {"section", "title", "description"}
        extra_keys = set(item.keys()) - expected_keys
        missing_keys = expected_keys - set(item.keys())

        if missing_keys:
            raise ValueError(f"Clés manquantes dans une section : {sorted(missing_keys)}.")
        if extra_keys:
            raise ValueError(f"Clés non autorisées dans une section : {sorted(extra_keys)}.")

        section = item["section"]
        title = item["title"]
        description = item["description"]

        if section not in conf.VALID_ISSUER_ANALYSIS_SECTIONS:
            raise ValueError(f"Section invalide : {section}.")

        if section in by_section:
            raise ValueError(f"Section dupliquée : {section}.")

        if not isinstance(title, list) or not all(isinstance(x, str) and x.strip() for x in title):
            raise ValueError(f"La section {section} doit contenir une liste 'title' de chaînes non vides.")

        if not isinstance(description, list) or not all(isinstance(x, str) and x.strip() for x in description):
            raise ValueError(f"La section {section} doit contenir une liste 'description' de chaînes non vides.")

        if len(title) != len(description):
            raise ValueError(f"Les listes 'title' et 'description' doivent avoir la même taille pour {section}.")

        by_section[section] = {
            "section": section,
            "title": [x.strip() for x in title],
            "description": [x.strip() for x in description],
        }

    if "critical_weakness" not in by_section:
        raise ValueError("La section 'critical_weakness' est absente.")

    if "strategic_recommendations" not in by_section:
        raise ValueError("La section 'strategic_recommendations' est absente.")

    if len(by_section["critical_weakness"]["title"]) != 3:
        raise ValueError("La section 'critical_weakness' doit contenir exactement 3 items.")

    if len(by_section["strategic_recommendations"]["title"]) != 4:
        raise ValueError("La section 'strategic_recommendations' doit contenir exactement 4 items.")

    return [
        by_section["critical_weakness"],
        by_section["strategic_recommendations"],
    ]


# =====================================================================
# Pour agent 3 : Appels LLM Agent 3
#
# =====================================================================

def build_agent3_messages(clean_data: List[Dict[str, Any]]) -> Tuple[str, List[Any]]:
    """
    Construit le prompt Agent 3 et les messages LangChain associés.
    """
    json_data_to_analyse = json.dumps(clean_data, ensure_ascii=False, indent=2)

    prompt_text = prompt_rwa.prompt_template_agent_3.substitute(
        json_data_to_analyse=json_data_to_analyse
    )

    messages = [
        SystemMessage(content="You are an RWA regulatory audit synthesis expert."),
        HumanMessage(content=prompt_text),
    ]
    return prompt_text, messages


def call_llm_with_retry_agent3(
    messages: List[Any],
    file_processed: str,
    provider: str,
    model: str,
    max_retries: int = 4,
    backoff_seconds: float = 12.0,
) -> Tuple[List[Dict[str, Any]], str, Dict[str, Any]]:
    """Appelle un provider Agent 3 avec retry puis valide la synthèse."""
    validate_file_processed(file_processed)
    last_error: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            if provider.lower() == "gemini":
                response = _gemini_generate_json(
                    model=model,
                    prompt_text=_messages_to_prompt_text(messages),
                    max_output_tokens=8192,
                )
                raw_text = (getattr(response, "text", "") or "").strip()
            elif provider.lower() == "openai":
                if not conf.OPENAI_API_KEY:
                    raise RuntimeError("Clé OpenAI manquante.")
                llm = ChatOpenAI(
                    model_name=model,
                    temperature=0,
                    max_tokens=8192,
                    openai_api_key=conf.OPENAI_API_KEY,
                    timeout=conf.LLM_REQUEST_TIMEOUT_SECONDS,
                    max_retries=0,
                )
                result = llm.generate([messages])
                raw_text = result.generations[0][0].text.strip()
            else:
                raise ValueError(f"Provider LLM non supporté : {provider}")

            if not raw_text:
                raise ValueError("Agent 3 : réponse LLM vide.")

            log_raw_llm_output(file_processed, provider, "agent3", raw_text)
            repaired = repair_agent3_output(raw_text)
            validated = validate_agent3_output(repaired["data"])
            logging.info(
                "[Agent3][%s][Attempt %d/%d] Succès | repaired=%s | repairs=%s",
                provider.upper(),
                attempt,
                max_retries,
                repaired["was_repaired"],
                repaired["repairs_applied"],
            )
            return validated, raw_text, repaired

        except Exception as exc:
            last_error = exc
            logging.warning(
                "[Agent3][%s][Attempt %d/%d] Échec %s : %s",
                provider.upper(),
                attempt,
                max_retries,
                type(exc).__name__,
                exc,
            )
            if attempt < max_retries:
                time.sleep(backoff_seconds * attempt)

    raise RuntimeError(
        f"[Agent3][{provider.upper()}] Échec après {max_retries} tentatives : "
        f"{type(last_error).__name__ if last_error else 'N/A'}: {last_error}"
    )


def call_llm_with_failover_agent3(
    messages: List[Any],
    file_processed: str,
) -> Tuple[List[Dict[str, Any]], str, str, str, Dict[str, Any]]:
    """
    Orchestre le failover Agent 3 avec ordre local :
    Gemini puis OpenAI.
    """
    last_error: Optional[Exception] = None

    for provider, model in conf.AGENT3_PROVIDER_ORDER:
        logging.info(
            f"[Agent3][Failover] Tentative provider={provider.upper()} | model={model}"
        )

        try:
            validated_data, raw_text, repaired = call_llm_with_retry_agent3(
                messages=messages,
                file_processed=file_processed,
                provider=provider,
                model=model,
            )

            logging.info(
                f"[Agent3][Failover] Succès provider={provider.upper()} | model={model}"
            )
            return validated_data, raw_text, provider, model, repaired

        except Exception as e:
            last_error = e
            logging.warning(
                f"[Agent3][Failover] Provider en échec {provider.upper()} "
                f"({type(e).__name__}: {e}) -> bascule provider suivant."
            )

    raise RuntimeError(
        f"[Agent3][Failover] Tous les providers ont échoué. "
        f"Dernière erreur : {type(last_error).__name__ if last_error else 'N/A'}: {last_error}"
    )


# ---------------------------------------------------------
# Orchestration complète Agent 3 :
# - lecture du JSON enrichi issu du flux principal
# - nettoyage / réduction
# - prompt + appel LLM avec failover
# - validation stricte
# - archivage
# - POST API de la synthèse si activé
# ---------------------------------------------------------

def run_agent3_from_enriched_json(
    enriched_path: Path,
    report_id: str,
    file_processed: str,
    output_root_dir: Optional[Path] = None,
    post_to_api: bool = False,
    base_url: str = conf.API_BASE_URL,
) -> Dict[str, Any]:
    """
    Exécute l'Agent 3 à partir du JSON enrichi pré-POST produit après Stage 2.

    Cette fonction est pensée pour être appelée par pipeline_steps.py.
    Elle ne dépend d'aucun chemin global ni d'aucun report_id codé en dur.
    """
    enriched_path = Path(enriched_path)

    if not enriched_path.exists():
        raise FileNotFoundError(f"Fichier JSON enrichi introuvable : {enriched_path}")

    if not report_id or not isinstance(report_id, str):
        raise ValueError("report_id est obligatoire pour lancer l'Agent 3.")

    if not file_processed or not isinstance(file_processed, str):
        raise ValueError("file_processed est obligatoire pour lancer l'Agent 3.")
    file_processed = validate_file_processed(file_processed)

    if output_root_dir is None:
        output_root_dir = conf.agent3_outputs_folder

    output_root_dir = Path(output_root_dir)
    output_root_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    run_dir = output_root_dir / f"{file_processed}_agent3_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    logging.info("----- Début Agent 3 -----")
    logging.info(f"Source JSON enrichi : {enriched_path}")
    logging.info(f"Dossier Agent 3 : {run_dir}")

    with open(enriched_path, "r", encoding="utf-8") as f:
        entries = json.load(f)

    clean_data_1 = remove_unwanted_fields(entries)
    clean_data_2 = remove_green_metrics_and_internal_indexes(clean_data_1)

    input_json_path_1 = run_dir / "1_agent3_input_after_remove_unwanted_fields.json"
    input_json_path_2 = run_dir / "2_agent3_input_after_remove_green_filters.json"
    prompt_path = run_dir / "3_agent3_prompt.txt"
    raw_output_path = run_dir / "4_agent3_raw_output.txt"
    repaired_output_path = run_dir / "5_agent3_repaired_output.json"
    final_output_path = run_dir / "6_agent3_validated_output.json"
    audit_output_path = run_dir / "7_agent3_run_audit.json"

    save_json_file(input_json_path_1, clean_data_1)
    save_json_file(input_json_path_2, clean_data_2)

    prompt_text, messages = build_agent3_messages(clean_data_2)
    save_text_file(prompt_path, prompt_text)

    validated_data, raw_text, provider_used, model_used, repaired = call_llm_with_failover_agent3(
        messages=messages,
        file_processed=file_processed,
    )

    save_text_file(raw_output_path, raw_text)
    save_json_file(repaired_output_path, repaired)
    save_json_file(final_output_path, validated_data)

    post_results: List[Dict[str, Any]] = []
    if post_to_api:
        post_results = post_agent3_sections_to_api(
            api_key=conf.RWA_API_KEY,
            report_id=report_id,
            agent3_sections=validated_data,
            base_url=base_url,
        )

        expected_posts = len(validated_data)
        actual_posts = len(post_results)

        if actual_posts != expected_posts:
            raise RuntimeError(
                f"POST Agent 3 incomplet : {actual_posts}/{expected_posts} section(s) postée(s) "
                f"pour report_id={report_id}"
            )

    audit_data = {
        "report_id": report_id,
        "source_json_path": str(enriched_path.resolve()),
        "output_dir": str(run_dir.resolve()),
        "provider_order": conf.AGENT3_PROVIDER_ORDER,
        "provider_used": provider_used,
        "model_used": model_used,
        "prompt_path": str(prompt_path.resolve()),
        "input_json_path_1": str(input_json_path_1.resolve()),
        "input_json_path_2": str(input_json_path_2.resolve()),
        "raw_output_path": str(raw_output_path.resolve()),
        "repaired_output_path": str(repaired_output_path.resolve()),
        "final_output_path": str(final_output_path.resolve()),
        "was_repaired": repaired["was_repaired"],
        "repairs_applied": repaired["repairs_applied"],
        "post_to_api": post_to_api,
        "post_results_count": len(post_results),
    }
    save_json_file(audit_output_path, audit_data)

    logging.info("----- Fin Agent 3 -----")
    logging.info(f"Provider retenu : {provider_used}")
    logging.info(f"Modèle retenu : {model_used}")
    logging.info(f"Sortie finale Agent 3 : {final_output_path}")

    return {
        "validated_data": validated_data,
        "provider_used": provider_used,
        "model_used": model_used,
        "run_dir": str(run_dir.resolve()),
        "input_json_path_1": str(input_json_path_1.resolve()),
        "input_json_path_2": str(input_json_path_2.resolve()),
        "prompt_path": str(prompt_path.resolve()),
        "raw_output_path": str(raw_output_path.resolve()),
        "repaired_output_path": str(repaired_output_path.resolve()),
        "final_output_path": str(final_output_path.resolve()),
        "audit_output_path": str(audit_output_path.resolve()),
        "was_repaired": repaired["was_repaired"],
        "repairs_applied": repaired["repairs_applied"],
        "post_results": post_results,
    }


# =====================================================================
# # Pour agent 3 : Préparation éventuelle au POST API
#
# =====================================================================

def upsert_issuer_analysis_content(
    api_key: str,
    report_id: str,
    section: str,
    title: List[str],
    description: List[str],
    base_url: str,
) -> Optional[Dict[str, Any]]:
    """
    Crée ou met à jour une section de synthèse via l'API.
    """
    if not report_id or not isinstance(report_id, str):
        logging.error("❌ report_id est obligatoire et doit être une chaîne non vide")
        return None

    if section not in conf.VALID_ISSUER_ANALYSIS_SECTIONS:
        logging.error(f"❌ section invalide : {section}")
        return None

    if not isinstance(title, list) or not all(isinstance(item, str) for item in title):
        logging.error("❌ title doit être une liste de chaînes")
        return None

    if not isinstance(description, list) or not all(isinstance(item, str) for item in description):
        logging.error("❌ description doit être une liste de chaînes")
        return None

    if len(title) != len(description):
        logging.error("❌ title et description doivent avoir la même longueur")
        return None

    url = f"{base_url.rstrip('/')}{conf.API_ISSUER_ANALYSIS_PATH}"
    headers = {
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "report_id": report_id,
        "section": section,
        "title": title,
        "description": description,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=conf.API_REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()

    except Exception as e:
        logging.error(
            f"❌ Erreur upsert issuer-analysis-content pour section={section}: {e}"
        )
        logging.warning("⚠️ Le payload Agent 3 n’est pas journalisé afin d’éviter une fuite de contenu d’audit.")
        return None


# ---------------------------------------------------------
# Publication API Agent 3 :
# les 2 sections de synthèse sont postées séparément.
# Le run Agent 3 n'est considéré comme complet que si toutes
# les sections attendues sont bien publiées.
# ---------------------------------------------------------
def post_agent3_sections_to_api(
    api_key: str,
    report_id: str,
    agent3_sections: List[Dict[str, Any]],
    base_url: str,
) -> List[Dict[str, Any]]:
    """
    Poste les 2 sections Agent 3 vers l'API, une section après l'autre.
    """
    posted_results: List[Dict[str, Any]] = []

    # Le backend attend un upsert section par section.
    # Le JSON Agent 3 validé est donc publié en 2 appels successifs :
    # - critical_weakness
    # - strategic_recommendations
    for section_data in agent3_sections:
        result = upsert_issuer_analysis_content(
            api_key=api_key,
            report_id=report_id,
            section=section_data["section"],
            title=section_data["title"],
            description=section_data["description"],
            base_url=base_url,
        )
        if result is not None:
            posted_results.append(result)

    return posted_results
