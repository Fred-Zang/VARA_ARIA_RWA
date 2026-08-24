# -*- coding: utf-8 -*-
"""Ordonne les rapports PENDING et lance un pipeline isolé par document."""

import fcntl
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import IO, Optional

import config as conf
from pipeline_steps import step_recover_WPs_pending
from rwa_methods import update_report_status, validate_file_processed

PIPELINE_SCRIPT = Path(__file__).with_name("pipeline.py")


class JsonLineFormatter(logging.Formatter):
    """Produit une ligne JSON valide par événement."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "level": record.levelname,
                "event": record.getMessage(),
            },
            ensure_ascii=False,
        )


def setup_logging(log_path: Path) -> None:
    """Configure le journal mensuel JSONL lorsqu'un traitement est à lancer."""
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = logging.FileHandler(log_path, encoding="utf-8", mode="a")
    handler.setFormatter(JsonLineFormatter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def acquire_lock(lock_path: Path) -> Optional[IO[str]]:
    """Acquiert un verrou noyau non bloquant valable pendant tout le processus.

    `flock` évite les verrous « périmés » basés sur une durée arbitraire : le
    verrou est automatiquement libéré par le noyau si le processus disparaît.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def release_lock(handle: IO[str]) -> None:
    """Libère proprement le verrou CRON."""
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def main() -> int:
    conf.validate_runtime_configuration()
    lock_handle = acquire_lock(conf.log_folder / "cron-run.lock")
    if lock_handle is None:
        logging.info("Un autre traitement détient déjà le verrou CRON.")
        return 0

    try:
        logging.disable(logging.DEBUG)
        try:
            new_wps = step_recover_WPs_pending()
            if not new_wps:
                return 0

            now = time.localtime()
            monthly = f"cron_run_{now.tm_year % 100:02d}-{now.tm_mon:02d}.jsonl"
            setup_logging(conf.log_folder / monthly)

            for report_id in new_wps:
                if not update_report_status(
                    api_key=conf.RWA_API_KEY,
                    report_id=report_id,
                    status="PROCESSING",
                    base_url=conf.API_BASE_URL,
                ):
                    logging.error("Impossible de passer un rapport en PROCESSING.")
                    continue
                time.sleep(2)
        except Exception as exc:
            logging.error(
                "Erreur lors de la récupération des rapports PENDING : %s: %s",
                type(exc).__name__,
                exc,
            )
            return 1
        finally:
            logging.disable(logging.NOTSET)

        logging.info("Lancement de %d traitement(s).", len(new_wps))
        had_error = False

        for report_id, meta in new_wps.items():
            try:
                file_processed = validate_file_processed(meta.get("file_processed") or report_id)
            except ValueError as exc:
                had_error = True
                logging.error("Identifiant de document refusé : %s", exc)
                update_report_status(conf.RWA_API_KEY, report_id, "ERROR", conf.API_BASE_URL)
                continue

            logging.info("Démarrage d'un pipeline document.")
            try:
                result = subprocess.run(
                    [sys.executable, str(PIPELINE_SCRIPT), "--file_processed", file_processed],
                    timeout=conf.PIPELINE_SUBPROCESS_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                had_error = True
                logging.error("Pipeline interrompu après timeout.")
                update_report_status(conf.RWA_API_KEY, report_id, "ERROR", conf.API_BASE_URL)
                continue

            if result.returncode == 0:
                logging.info("Pipeline terminé avec succès.")
            else:
                had_error = True
                # La sortie du sous-processus n'est pas dupliquée dans le processus CRON :
                # elle peut contenir des informations métier et devenir volumineuse.
                logging.error("Pipeline en erreur ; code de sortie=%s", result.returncode)
                update_report_status(conf.RWA_API_KEY, report_id, "ERROR", conf.API_BASE_URL)

        return 1 if had_error else 0
    finally:
        release_lock(lock_handle)


if __name__ == "__main__":
    raise SystemExit(main())
