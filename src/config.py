# -*- coding: utf-8 -*-
"""Configuration de la démonstration publique du pipeline RWA.

Les secrets, URLs d'infrastructure et paramètres propres à la production sont
chargés exclusivement depuis l'environnement. Le dépôt public ne contient
aucune valeur de production.
"""

import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

load_dotenv()

# Les artefacts runtime peuvent contenir des données clients. Les nouveaux fichiers
# sont créés en lecture/écriture pour le propriétaire uniquement (0600) et les
# nouveaux dossiers en accès propriétaire uniquement (0700), sauf override explicite.
os.umask(0o077)


def _env_bool(name: str, default: bool = False) -> bool:
    """Convertit une variable d'environnement courante en booléen."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Lit un entier positif depuis l'environnement avec validation explicite."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} doit contenir un entier valide.") from exc
    if value < minimum:
        raise ValueError(f"{name} doit être supérieur ou égal à {minimum}.")
    return value




def _env_api_path(name: str, default: str) -> str:
    """Lit un chemin d'API relatif sans accepter d'URL absolue."""
    value = os.getenv(name, default).strip()
    if not value.startswith("/") or value.startswith("//"):
        raise ValueError(f"{name} doit être un chemin relatif commençant par '/'.")
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError(f"{name} ne doit pas contenir d'URL absolue ni de fragment.")
    return value

# Racine de travail. /app est la valeur utilisée dans l'image Docker publique.
my_computer_path = Path(os.getenv("RWA_APP_ROOT", "/app")).resolve()

# Dossiers de données et de traçabilité. Ils restent hors Git via .gitignore.
input_whitepaper_folder = my_computer_path / "_data_main" / "origin_WP"
paginated_folder = my_computer_path / "_data_main" / "paginated_WP"
reduced_weight_pdf_folder = my_computer_path / "_data_main" / "reduced_weight_pdf_WP"
ocr_folder = my_computer_path / "_data_main" / "ocr_WP"
problem_folder = my_computer_path / "_data_main" / "ocr_problems"
report_folder = my_computer_path / "_data_main" / "llm_generated_reports"
report_stage_1_folder = my_computer_path / "_data_main" / "llm_generated_reports_stage_1"
report_stage_2_folder = my_computer_path / "_data_main" / "llm_generated_reports_stage_2"
report_post_db_folder = my_computer_path / "_data_main" / "llm_generated_reports_post_db"
llm_prompt_folder = my_computer_path / "_data_main" / "llm_prompt_folder"
excel_reports_folder = my_computer_path / "_data_main" / "generated_excels"
report_postprocessing_folder = my_computer_path / "_data_main" / "llm_postprocessing"
agent3_outputs_folder = report_postprocessing_folder / "agent3_outputs"

tmp_folder = my_computer_path / "_tmp"
log_folder = my_computer_path / "_data_logs"
log_track_dict = log_folder / "track_dict_new_WPs_processed"
monitoring_folder = log_folder / "monitoring_process"
raw_llm_log_folder = log_folder / "llm_raw_outputs"
wp_chunks_folder = report_folder / "wp_chunks"
chunks_dir = report_folder / "framework_chunks"
rerank_dir = report_folder / "rerank_candidates"
raw_dir = report_folder / "llm_raw_chunks"
parsed_dir = report_folder / "llm_parsed_chunks"
metrics_dir = monitoring_folder / "llm_call_metrics"
special_problem_to_solve = log_folder / "special_problem_to_solve"

for folder in (
    input_whitepaper_folder,
    paginated_folder,
    reduced_weight_pdf_folder,
    ocr_folder,
    problem_folder,
    report_folder,
    report_stage_1_folder,
    report_stage_2_folder,
    report_post_db_folder,
    llm_prompt_folder,
    excel_reports_folder,
    report_postprocessing_folder,
    agent3_outputs_folder,
    tmp_folder,
    log_folder,
    log_track_dict,
    monitoring_folder,
    raw_llm_log_folder,
    wp_chunks_folder,
    chunks_dir,
    rerank_dir,
    raw_dir,
    parsed_dir,
    metrics_dir,
    special_problem_to_solve,
):
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        folder.chmod(0o700)
    except OSError:
        # Certains volumes montés peuvent imposer leurs propres permissions.
        # L'umask 0077 continue alors de protéger les nouveaux artefacts.
        logging.warning("Impossible d'appliquer le mode 0700 à %s.", folder)

# Secrets : noms génériques pour le dépôt public.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
RWA_API_KEY = os.getenv("RWA_API_KEY")

# Le domaine de production n'est jamais codé en dur. example.invalid empêche
# tout appel accidentel vers une infrastructure réelle sans configuration.
API_BASE_URL = os.getenv("RWA_API_BASE_URL", "https://example.invalid").rstrip("/")
ALLOW_INSECURE_HTTP = _env_bool("RWA_ALLOW_INSECURE_HTTP", False)

# Les routes ci-dessous sont volontairement génériques dans le dépôt public.
# Une intégration réelle peut fournir ses propres chemins via l'environnement
# sans exposer le contrat de l'API de production dans le code source.
API_FRAMEWORK_PATH = _env_api_path("RWA_API_FRAMEWORK_PATH", "/api/v1/framework")
API_PROJECTS_PATH = _env_api_path("RWA_API_PROJECTS_PATH", "/api/v1/projects")
API_REPORT_UPDATE_PATH = _env_api_path("RWA_API_REPORT_UPDATE_PATH", "/api/v1/reports/update")
API_REFERENCE_UPLOAD_PATH = _env_api_path("RWA_API_REFERENCE_UPLOAD_PATH", "/api/v1/files/reference")
API_ISSUER_ANALYSIS_PATH = _env_api_path("RWA_API_ISSUER_ANALYSIS_PATH", "/api/v1/analysis/summary")

if API_BASE_URL != "https://example.invalid":
    parsed_api_url = urlparse(API_BASE_URL)
    if not parsed_api_url.hostname or parsed_api_url.scheme not in {"http", "https"}:
        raise ValueError("RWA_API_BASE_URL doit être une URL HTTP(S) valide.")
    if parsed_api_url.scheme != "https" and not ALLOW_INSECURE_HTTP:
        raise ValueError(
            "RWA_API_BASE_URL doit utiliser HTTPS. "
            "RWA_ALLOW_INSECURE_HTTP=true est réservé au développement local explicite."
        )

# Timeouts explicites pour éviter les connexions ou traitements bloqués indéfiniment.
API_REQUEST_TIMEOUT = (10, 60)
DOWNLOAD_REQUEST_TIMEOUT = (10, 300)
UPLOAD_REQUEST_TIMEOUT = (10, 300)
LLM_REQUEST_TIMEOUT_SECONDS = _env_int("RWA_LLM_REQUEST_TIMEOUT_SECONDS", 180)
PIPELINE_SUBPROCESS_TIMEOUT_SECONDS = _env_int("RWA_PIPELINE_TIMEOUT_SECONDS", 7200)
DOCUMENT_PROCESS_TIMEOUT_SECONDS = _env_int("RWA_DOCUMENT_PROCESS_TIMEOUT_SECONDS", 300)
PDF_RENDER_TIMEOUT_SECONDS = _env_int("RWA_PDF_RENDER_TIMEOUT_SECONDS", 300)
OCR_PAGE_TIMEOUT_SECONDS = _env_int("RWA_OCR_PAGE_TIMEOUT_SECONDS", 120)

# Protection du téléchargement et du parsing documentaire.
ALLOWED_WHITEPAPER_EXTENSIONS = {".pdf", ".doc", ".docx"}
MAX_WHITEPAPER_BYTES = _env_int("RWA_MAX_WHITEPAPER_BYTES", 250 * 1024 * 1024)
MAX_WHITEPAPER_PAGES = _env_int("RWA_MAX_WHITEPAPER_PAGES", 500)
MAX_RENDER_PIXELS_PER_PAGE = _env_int("RWA_MAX_RENDER_PIXELS_PER_PAGE", 50_000_000)
MAX_DOWNLOAD_REDIRECTS = _env_int("RWA_MAX_DOWNLOAD_REDIRECTS", 3, minimum=0)
MAX_DOCX_ARCHIVE_MEMBERS = _env_int("RWA_MAX_DOCX_ARCHIVE_MEMBERS", 5000)
MAX_DOCX_UNCOMPRESSED_BYTES = _env_int("RWA_MAX_DOCX_UNCOMPRESSED_BYTES", 512 * 1024 * 1024)
REQUIRE_DOWNLOAD_ALLOWLIST = _env_bool("RWA_REQUIRE_DOWNLOAD_ALLOWLIST", True)
ALLOWED_DOWNLOAD_HOSTS = {
    item.strip().lower()
    for item in os.getenv("RWA_ALLOWED_DOWNLOAD_HOSTS", "").split(",")
    if item.strip()
}

# Les sorties LLM peuvent contenir du texte client. Elles sont désactivées par
# défaut dans cette version publique et ne doivent jamais être commitées.
ENABLE_RAW_LLM_LOGS = _env_bool("RWA_ENABLE_RAW_LLM_LOGS", False)

FLAG_PRIORITY = {"green": 0, "amber": 1, "red": 2}
ENABLE_STAGE2 = True

# Simulation de panne conservée pour les tests de failover ; désactivée par défaut.
_SIMULATED_PROVIDER_OUTAGE = {
    "stage1": {"active": False, "armed_at": None, "trigger_after_sec": None, "provider_to_fail": None, "triggered": False},
    "stage2": {"active": False, "armed_at": None, "trigger_after_sec": None, "provider_to_fail": None, "triggered": False},
}

DEFAULT_LLM_MODEL = os.getenv("RWA_DEFAULT_LLM_MODEL", "gpt-4.1-2025-04-14")
MODEL_PROVIDER_MAP = {
    "gpt-4.1-2025-04-14": "openai",
    "gpt-4o-mini": "openai",
    "o3-mini": "openai",
    "gemini-2.5-pro": "gemini",
    "gemini-2.5-flash": "gemini",
    "gemini-2.5-flash-lite": "gemini",
}
DEFAULT_PROVIDER = MODEL_PROVIDER_MAP.get(DEFAULT_LLM_MODEL, "openai")

if DEFAULT_PROVIDER == "openai":
    STAGE1_PROVIDER_ORDER = [("openai", DEFAULT_LLM_MODEL), ("gemini", "gemini-2.5-pro")]
    STAGE2_PROVIDER_ORDER = [("gemini", "gemini-2.5-pro"), ("openai", DEFAULT_LLM_MODEL)]
else:
    STAGE1_PROVIDER_ORDER = [("gemini", DEFAULT_LLM_MODEL), ("openai", "gpt-4.1-2025-04-14")]
    STAGE2_PROVIDER_ORDER = [("openai", "gpt-4.1-2025-04-14"), ("gemini", DEFAULT_LLM_MODEL)]

AGENT3_PROVIDER_ORDER = [("gemini", "gemini-2.5-pro"), ("openai", DEFAULT_LLM_MODEL)]
VALID_ISSUER_ANALYSIS_SECTIONS = {"critical_weakness", "strategic_recommendations"}

if API_BASE_URL == "https://example.invalid":
    logging.info("RWA_API_BASE_URL non configurée : les appels au backend sont volontairement neutralisés.")


def validate_runtime_configuration() -> None:
    """Vérifie les paramètres indispensables avant tout traitement réel."""
    missing = [
        name
        for name, value in (
            ("RWA_API_KEY", RWA_API_KEY),
            ("RWA_API_BASE_URL", None if API_BASE_URL == "https://example.invalid" else API_BASE_URL),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Configuration runtime incomplète : " + ", ".join(missing)
        )
    if REQUIRE_DOWNLOAD_ALLOWLIST and not ALLOWED_DOWNLOAD_HOSTS:
        raise RuntimeError(
            "RWA_ALLOWED_DOWNLOAD_HOSTS doit être configurée lorsque "
            "RWA_REQUIRE_DOWNLOAD_ALLOWLIST=true."
        )
    if not OPENAI_API_KEY and not GEMINI_API_KEY:
        raise RuntimeError("Au moins une clé LLM (OpenAI ou Gemini) est requise.")
