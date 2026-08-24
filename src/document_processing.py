# -*- coding: utf-8 -*-
"""Préparation documentaire : DOC/DOCX, PDF vectoriel et PDF raster/OCR."""

from collections import Counter
from io import BytesIO
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

import enchant
import fitz  # PyMuPDF
from pdf2image import convert_from_path
from PIL import Image, ImageDraw, ImageFont
import pytesseract

import config as conf
from rwa_methods import log_execution_time


def _sanitized_document_subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Construit un environnement explicitement autorisé pour les outils natifs.

    Les parseurs documentaires reçoivent uniquement les variables nécessaires à
    leur exécution. Une allow-list est plus sûre qu'une suppression de quelques
    noms sensibles : une nouvelle clé ajoutée ultérieurement à l'environnement
    de l'application ne sera ainsi pas transmise par défaut au sous-processus.

    Cette mesure réduit l'exposition des secrets mais ne remplace pas un sandbox
    dédié pour le traitement de documents non fiables.
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }
    for name in ("LANG", "LC_ALL", "TZ"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    if extra:
        env.update(extra)
    return env


def _validate_pdf_resource_limits(pdf_path: Path, dpi: int | None = None) -> int:
    """Refuse les PDF vides, trop longs ou dont une page serait trop volumineuse."""
    with fitz.open(str(pdf_path)) as document:
        page_count = document.page_count
        if page_count < 1:
            raise ValueError("Le PDF ne contient aucune page.")
        if page_count > conf.MAX_WHITEPAPER_PAGES:
            raise ValueError(
                f"Le PDF contient {page_count} pages, limite configurée={conf.MAX_WHITEPAPER_PAGES}."
            )

        if dpi is not None:
            for index, page in enumerate(document, start=1):
                width_px = math.ceil(page.rect.width * dpi / 72.0)
                height_px = math.ceil(page.rect.height * dpi / 72.0)
                pixels = width_px * height_px
                if pixels > conf.MAX_RENDER_PIXELS_PER_PAGE:
                    raise ValueError(
                        f"Page {index} trop volumineuse à {dpi} dpi ({pixels} pixels)."
                    )
    return page_count


def _validate_docx_archive(docx_path: Path) -> None:
    """Refuse les archives DOCX anormalement volumineuses ou mal formées."""
    if not zipfile.is_zipfile(docx_path):
        raise ValueError("Le fichier .docx n'est pas une archive ZIP valide.")

    with zipfile.ZipFile(docx_path) as archive:
        members = archive.infolist()
        if len(members) > conf.MAX_DOCX_ARCHIVE_MEMBERS:
            raise ValueError("Le fichier .docx contient trop d'éléments internes.")

        total_uncompressed = 0
        for member in members:
            member_path = Path(member.filename)
            if member.filename.startswith(("/", "\\")) or ".." in member_path.parts:
                raise ValueError("Le fichier .docx contient un chemin interne non sûr.")
            if member.flag_bits & 0x1:
                raise ValueError("Les archives DOCX chiffrées ne sont pas acceptées.")

            total_uncompressed += member.file_size
            if total_uncompressed > conf.MAX_DOCX_UNCOMPRESSED_BYTES:
                raise ValueError("Le contenu décompressé du .docx dépasse la limite autorisée.")


# -----------------------------------------------------------------------------
# Pipeline 1 : docx -> pdf vectoriel => txt sans OCR
# -----------------------------------------------------------------------------


def convert_docx_to_vector_pdf(docx_path: Path, output_pdf: Path) -> None:
    """Convertit un document Office en PDF via LibreOffice avec profil isolé."""
    docx_path = Path(docx_path)
    output_pdf = Path(output_pdf)
    if docx_path.suffix.lower() not in {".doc", ".docx"}:
        raise ValueError("Seuls les documents .doc et .docx sont acceptés.")
    if not docx_path.is_file():
        raise FileNotFoundError(docx_path)
    if docx_path.suffix.lower() == ".docx":
        _validate_docx_archive(docx_path)

    output_pdf.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conf.tmp_folder.mkdir(parents=True, exist_ok=True, mode=0o700)

    with tempfile.TemporaryDirectory(dir=conf.tmp_folder, prefix="libreoffice-") as profile:
        profile_uri = Path(profile).resolve().as_uri()
        subprocess.run(
            [
                "soffice",
                "--headless",
                "--nologo",
                "--nodefault",
                "--nofirststartwizard",
                "--norestore",
                f"-env:UserInstallation={profile_uri}",
                "--convert-to",
                "pdf",
                str(docx_path),
                "--outdir",
                str(output_pdf.parent),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=conf.DOCUMENT_PROCESS_TIMEOUT_SECONDS,
            env=_sanitized_document_subprocess_env({"HOME": profile}),
        )

    generated_pdf = output_pdf.parent / f"{docx_path.stem}.pdf"
    if not generated_pdf.exists():
        raise FileNotFoundError(f"PDF attendu après conversion : {generated_pdf}")
    if generated_pdf != output_pdf:
        os.replace(generated_pdf, output_pdf)

    _validate_pdf_resource_limits(output_pdf)


def add_watermark_vector_pdf(
    src_pdf: Path,
    dst_pdf: Path,
    header_height_ratio: float = 0.05,    # 5 % de la hauteur de page
    header_width_ratio: float = 0.25,     # 25 % de la largeur de page
    watermark_text: str = "--- PAGE {page_num} ---"
) -> None:
    """
    add_watermark_vector_pdf : Injecte en haut à droite de chaque page un watermark numéroté dans un rectangle calculé à partir des dimensions de la page, puis sauvegarde dans dst_pdf.
    fonctions appelées : fitz.open(), page.draw_rect(), page.insert_textbox(), doc.save(), doc.close(), logging.info()

    :param src_pdf: Path, chemin du PDF vectoriel source
    :param dst_pdf: Path, chemin du PDF paginé de sortie
    :param header_height_ratio: float, ratio de hauteur pour la zone de watermark (défaut 0.05)
    :param header_width_ratio: float, ratio de largeur pour la zone de watermark (défaut 0.25)
    :param watermark_text: str, template du texte de watermark (défaut "--- PAGE {page_num} ---")
    :return: None
    """
    logging.info("Pagination d'un PDF vectoriel.")
    _validate_pdf_resource_limits(src_pdf)
    doc = fitz.open(str(src_pdf))
    try:
        for i, page in enumerate(doc, start=1):
            r  = page.rect
            hh = r.height * header_height_ratio
            hw = r.width  * header_width_ratio
            header_rect = fitz.Rect(r.width - hw, 0, r.width, hh)
            txt = watermark_text.format(page_num=i)
            # fond blanc léger pour isoler le watermark
            page.draw_rect(header_rect, fill=(1,1,1), color=None)
            # insérer le texte aligné à droite
            page.insert_textbox(header_rect, txt,
                                fontsize=10,
                                align=fitz.TEXT_ALIGN_RIGHT)
        dst_pdf.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(dst_pdf), deflate=True)
        logging.info(f"add_watermark_vector_pdf: PDF paginé → {dst_pdf}")
    finally:
        doc.close()




def read_vector_pdf_page_to_page(
    pdf_path: Path
) -> list[str]:
    """
    read_vector_pdf_page_to_page : Lit un PDF vectoriel page par page et retourne la liste de textes de chaque page.
    fonctions appelées : fitz.open(), page.get_text(), doc.close(), logging.info()

    :param pdf_path: Path, chemin du PDF vectoriel
    :return: list[str], liste du contenu textuel de chaque page
    """
    logging.info("Extraction texte d'un PDF vectoriel.")
    _validate_pdf_resource_limits(pdf_path)
    pages: list[str] = []
    with fitz.open(str(pdf_path)) as doc:
        for page in doc:
            pages.append(page.get_text())
    return pages


def write_paginated_txt(
    pages: list[str],
    output_txt: Path
) -> None:
    """
    write_paginated_txt : Écrit la liste de textes paginés dans un fichier .txt en préfixant chaque page par '--- PAGE {i} ---'.
    fonctions appelées : open(), f.write(), logging.info()

    :param pages: list[str], liste de blocs de texte à paginer
    :param output_txt: Path, chemin du fichier .txt à écrire
    :return: None
    """
    output_txt = Path(output_txt)
    output_txt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    logging.info("Écriture du fichier TXT paginé : %s", output_txt.name)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{output_txt.name}.", dir=output_txt.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for i, block in enumerate(pages, start=1):
                stream.write(f"--- PAGE {i} ---\n")
                stream.write(block.strip() + "\n\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, output_txt)
    finally:
        tmp_path.unlink(missing_ok=True)


# -----------------------------------------------------------------------------
# Pipeline 2 : pdf vectoriel => txt sans OCR
# -----------------------------------------------------------------------------


# ---------------------------------------------
# 4) Nettoyage simple du texte
# ---------------------------------------------
def clean_page_text(text: str) -> str:
    """
    clean_page_text : Nettoie le texte brut d’une page en supprimant les lignes isolées de chiffres, les espaces redondants et les caractères non imprimables.
    fonctions appelées : re.sub(), logging.debug()

    :param text: str, contenu brut de la page
    :return: str, texte nettoyé et trimé
    """
    logging.debug("clean_page_text: début (%d caractères bruts)", len(text))
    # enlever lignes isolées de chiffres (anciens pieds de page)
    text = re.sub(r"(?m)^[ \t]*\d+[ \t]*$", "", text)
    # espaces redondants
    text = re.sub(r"[ \t]{2,}", " ", text)
    # caractères non imprimables
    text = re.sub(r"[^\x20-\x7E\n]", "", text)
    return text.strip()

#  pour éliminer les lignes redondantes à 90% (entier inf près) comme bas de page txt "confidancial"
def remove_redundant_lines(
    pages: list[str],
    threshold: float = 0.9
) -> list[str]:
    """
    remove_redundant_lines : Détecte et supprime les lignes qui apparaissent sur au moins threshold*100% des pages, considérées comme footers récurrents.
    fonctions appelées : collections.Counter(), math.floor()

    :param pages: list[str], liste de textes de chaque page
    :param threshold: float, ratio minimal de pages pour retirer une ligne (défaut 0.9)
    :return: list[str], pages avec les lignes redondantes retirées
    """
    num_pages = len(pages)
    # Construire un set de lignes par page (sans strip pour conserver espaces internes)
    page_line_sets = [set(page.splitlines()) for page in pages]
    # Compter la présence par page de chaque ligne
    line_counts = Counter(
        line
        for page_set in page_line_sets
        for line in page_set
    )
    # Seuil minimal (nombre de pages)
    min_occurrences = math.floor(num_pages * threshold)
    # Lignes à retirer
    to_remove = {line for line, cnt in line_counts.items() if cnt >= min_occurrences}

    # Filtrer chaque page
    cleaned_pages = []
    for page in pages:
        filtered = [line for line in page.splitlines() if line not in to_remove]
        cleaned_pages.append("\n".join(filtered))
    return cleaned_pages


# ---------------------------------------------
# 5) Écriture TXT paginé
# ---------------------------------------------
# déjà défini dans le Pipeline 1 def write_paginated_txt(pages, output_txt) -> None:



# -----------------------------------------------------------------------------
# Pipeline 3 : pdf raster = image => txt sans OCR
# -----------------------------------------------------------------------------

def is_raster_pdf(pdf_path: Path) -> tuple[bool, int]:
    """Détermine si un PDF est majoritairement raster à partir du texte extrait."""
    page_count = _validate_pdf_resource_limits(pdf_path)
    empty_pages = 0
    with fitz.open(str(pdf_path)) as document:
        for page in document:
            if len(page.get_text().strip()) < 10:
                empty_pages += 1
    return (empty_pages / page_count) > 0.9, page_count


def normalize_resolution(pdf_path: Path, dpi: int = 300) -> list[Image.Image]:
    """Convertit un PDF en images après contrôles de taille et avec timeout."""
    _validate_pdf_resource_limits(pdf_path, dpi=dpi)
    logging.info("Conversion PDF vers images à %d dpi.", dpi)
    return convert_from_path(
        str(pdf_path),
        dpi=dpi,
        thread_count=1,
        timeout=conf.PDF_RENDER_TIMEOUT_SECONDS,
    )


def annotate_page_image(
    img: Image.Image,
    page_num: int,
    dpi: int = 300
) -> Image.Image:
    """
    annotate_page_image : Injecte un watermark numéroté en haut à droite de l’image de la page pour pagination.
    fonctions appelées : ImageDraw.Draw(), draw.rectangle(), draw.text(), ImageFont.truetype(), ImageFont.load_default()

    :param img: Image.Image, image de la page à annoter
    :param page_num: int, numéro de la page
    :param dpi: int, résolution en DPI pour le calcul de la taille de police (défaut 300)
    :return: Image.Image, image annotée
    """
    img = img.copy()
    draw = ImageDraw.Draw(img)
    w, h = img.size

    # 5% hauteur × 25% largeur
    rect_h = int(h * 0.05)
    rect_w = int(w * 0.25)

    # flush top (y0=0) and right (x0=w−rect_w)
    x0, y0 = w - rect_w, 0
    x1, y1 = w, rect_h
    draw.rectangle([x0, y0, x1, y1], fill="white")

    text = f"--- PAGE {page_num} ---"
    font_px = max(12, int(dpi * 10 / 72))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_px)
    except IOError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    # flush right: 1px margin only
    tx = x1 - tw - 1
    ty = (rect_h - th) // 2
    draw.text((tx, ty), text, fill="black", font=font)

    return img





def correct_text(text: str) -> str:
    """
    correct_text :
      - Corrige les mots mal reconnus en utilisant pyenchant
      - Supprime les tokens de 1 à 3 caractères non valides
      - Supprime les caractères uniques d'une liste définie (comme saut de puces) par une ligne vide

    fonctions appelées : enchant.Dict(), d.check(), d.suggest(), re.findall(), logging.debug()

    :param text: str, texte brut à corriger
    :return: str, texte corrigé
    """
    try:
        d = enchant.Dict("en_US")
    except ImportError:
        return text

    # [NETTOYAGE PUCE] Supprime les caractères de puce isolés sur une ligne
    BULLET_CHARS = ["○","o", "•", "◦", "▪", "‣", "∙"]

    # Ligne par ligne : supprimer si ligne ne contient qu’un seul de ces caractères
    cleaned_lines = []
    for line in text.splitlines():
        if line.strip() in BULLET_CHARS:
            cleaned_lines.append("")  # remplace par ligne vide
        else:
            cleaned_lines.append(line)

    text = "\n".join(cleaned_lines)

    # Correction orthographique + suppression de tokens invalides
    tokens = re.findall(r"\w+|\W+", text)
    out = []
    for t in tokens:
        if t.isalpha():
            if 1 <= len(t) <= 3 and not d.check(t):
                continue
            if len(t) > 3 and not d.check(t):
                sug = d.suggest(t)
                if sug:
                    t = sug[0]
        out.append(t)

    return "".join(out)


def reflow_paragraphs(text: str) -> str:
    """
    reflow_paragraphs : Réassemble les lignes cassées et reforme des paragraphes cohérents sans coupures inappropriées.
    fonctions appelées : str.splitlines(), logging.debug()

    :param text: str, texte brut avec retours à la ligne artificiels
    :return: str, texte avec paragraphes réorganisés
    """
    lines = text.splitlines()
    out = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            out.append("")
            continue
        if s.endswith("-") and i+1 < len(lines):
            out.append(s[:-1] + lines[i+1].lstrip())
            continue
        if out and not out[-1].rstrip().endswith((".", "!", "?")) and s[0].islower():
            out[-1] += " " + s
        else:
            out.append(s)
    return "\n".join(out)



def convert_pdf_reduced_weight(
    input_pdf: Path,
    output_pdf_reduce: Path,
    pdf_settings: str = "/ebook",
) -> Path:
    """Compresse un PDF avec Ghostscript dans un sous-processus borné."""
    _validate_pdf_resource_limits(input_pdf)
    output_pdf_reduce.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_pdf = output_pdf_reduce / input_pdf.name
    cmd = [
        "gs",
        "-dSAFER",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS={pdf_settings}",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        f"-sOutputFile={output_pdf}",
        str(input_pdf),
    ]
    subprocess.run(
        cmd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=conf.DOCUMENT_PROCESS_TIMEOUT_SECONDS,
        env=_sanitized_document_subprocess_env(),
    )
    _validate_pdf_resource_limits(output_pdf)
    logging.info("PDF compressé avec succès (%0.2f Mo).", output_pdf.stat().st_size / 1e6)
    return output_pdf


def compress_if_needed(pdf_path: Path, max_size_mb: int = 10, max_attempts: int = 3) -> None:
    """
    Vérifie et compresse un PDF si sa taille dépasse `max_size_mb` (par défaut 10 Mo).
    Réessaie avec une qualité de plus en plus basse si nécessaire.

    Écrase le PDF original avec la version compressée.
    """
    if not pdf_path.exists():
        logging.warning(f"⚠️ Fichier PDF introuvable pour compression : {pdf_path}")
        return

    size_mb = pdf_path.stat().st_size / 1e6
    if size_mb <= max_size_mb:
        logging.info(f"✅ PDF déjà optimisé (< {max_size_mb} Mo) → {pdf_path.name} ({size_mb:.2f} Mo)")
        return

    presets = ["/ebook", "/screen", "/screen"]
    tmp_dir = conf.tmp_folder

    for i in range(min(max_attempts, len(presets))):
        try:
            compressed = convert_pdf_reduced_weight(
                input_pdf=pdf_path,
                output_pdf_reduce=tmp_dir,
                pdf_settings=presets[i]
            )
            size_after = compressed.stat().st_size / 1e6
            if size_after <= max_size_mb:
                shutil.move(str(compressed), str(pdf_path))
                logging.info(f"🎯 Compression réussie en tentative {i+1} : {pdf_path.name} ({size_after:.2f} Mo)")
                return
            else:
                logging.warning(f"Tentative {i+1} : PDF toujours trop gros ({size_after:.2f} Mo) → reessai")
        except Exception as e:
            logging.error(f"Erreur tentative compression PDF : {e}")

    logging.error(f"❌ PDF non compressé sous {max_size_mb} Mo après {max_attempts} tentatives : {pdf_path.name}")



# ----------------------------------------------------------------------------------------





# -----------------------------------------------------------------------------
# Running Pipelines 1 + 2 + 3
# -----------------------------------------------------------------------------



# ---------------------------------------------
# running Pipeline 1
# ---------------------------------------------
# 2) Pipeline 1 : DOCX → PDF vectoriel → watermark en haut → TXT
# --- Pipeline 1 : DOCX → PDF vectoriel → watermark EN-TÊTE → TXT final
@log_execution_time
def pipeline_docx_to_txt(
    docx_path: Path,
    paginated_folder: Path,
    txt_folder: Path
) -> None:
    """
    pipeline_docx_to_txt : Convertit un DOCX en PDF vectoriel, ajoute un watermark de pagination, extrait le texte, nettoie et écrit un fichier .txt paginé.
    fonctions appelées : convert_docx_to_vector_pdf(), add_watermark_vector_pdf(), read_vector_pdf_page_to_page(), clean_page_text(), remove_redundant_lines(), write_paginated_txt(), logging.info()

    :param docx_path: Path, chemin du fichier DOCX source
    :param paginated_folder: Path, dossier pour les PDF intermédiaires paginés
    :param txt_folder: Path, dossier pour le fichier .txt final
    :return: None
    """
    logging.info(f"Démarrage pipeline DOCX pour {docx_path.name}")
    try:
        # 1) Conversion DOCX → PDF vectoriel brut
        pdf_vec = paginated_folder / f"{docx_path.stem}.pdf"
        convert_docx_to_vector_pdf(docx_path, pdf_vec)

        # 2) Pagination EN-TÊTE → nouveau fichier _pagineted sauvegardé
        pdf_paginated = paginated_folder / f"{docx_path.stem}_paginated.pdf"
        add_watermark_vector_pdf(pdf_vec, pdf_paginated)
        pdf_vec.unlink()  # supprime le PDF vectoriel intermédiaire


        # 3) Extraction depuis le PDF paginé
        pages = read_vector_pdf_page_to_page(pdf_paginated)

        # 4) Supprimer le watermark du texte brut
        pages = [
            re.sub(rf"{re.escape('--- PAGE ')}\d+\s*---\s*", "", block)
            for block in pages
        ]

        # 5) Nettoyage initial (numéros isolés, espaces, parasites)
        pages = [clean_page_text(p) for p in pages]

        # 6) Élimination automatique des footers récurrents
        if len(pages) > 1:
            pages = remove_redundant_lines(pages, threshold=0.9)
            # reassemble les lignes cassées
            pages = [reflow_paragraphs(p) for p in pages]


        # 7) Écriture du .txt paginé
        txt_folder.mkdir(parents=True, exist_ok=True)
        out_txt = txt_folder / f"{docx_path.stem}.txt"
        write_paginated_txt(pages, out_txt)
        logging.info(f"Pipeline DOCX terminé pour {docx_path.name}")

    except Exception:
        logging.exception("Échec du pipeline DOC/DOCX.")
        raise





# ---------------------------------------------
# running Pipeline 2
# ---------------------------------------------
# Pipeline 2 : PDF vectoriel → watermark en haut → TXT



@log_execution_time
def pipeline_pdf_vectoriel_to_txt(
    pdf_path: Path,
    paginated_folder: Path,
    txt_folder: Path
) -> None:
    """
    pipeline_pdf_vectoriel_to_txt : Paginer un PDF vectoriel avec watermark, extraire le texte, nettoyer et écrire en .txt.
    fonctions appelées : add_watermark_vector_pdf(), read_vector_pdf_page_to_page(), clean_page_text(), remove_redundant_lines(), write_paginated_txt(), logging.info()

    :param pdf_path: Path, chemin du PDF vectoriel source
    :param paginated_folder: Path, dossier pour stocker le PDF paginé
    :param txt_folder: Path, dossier pour stocker le fichier .txt final
    :return: None
    """
    logging.info(f"Démarrage pipeline PDF pour {pdf_path.name}")
    try:

        # 1) Repagination EN-TÊTE 1→N +  nouveau fichier _pagineted sauvegardé
        pdf_paginated = conf.paginated_folder / f"{pdf_path.stem}_paginated.pdf"
        add_watermark_vector_pdf(pdf_path, pdf_paginated)
        logging.info(f"PDF paginé généré → {pdf_paginated}")

        # 2) Extraction & suppression watermark
        pages = read_vector_pdf_page_to_page(pdf_paginated)
        pages = [
            re.sub(rf"{re.escape('--- PAGE ')}\d+\s*---\s*", "", block)
            for block in pages
        ]

        # 3) Nettoyage initial
        pages = [clean_page_text(p) for p in pages]

        # 4) Suppression des footers récurrents
        if len(pages) > 1: # sinon efface tout car 1 seule page !
            pages = remove_redundant_lines(pages, threshold=0.9)
            # reassemble les lignes cassées
            pages = [reflow_paragraphs(p) for p in pages]


        # 5) Écriture du .txt final
        out_txt = txt_folder / f"{pdf_path.stem}.txt"
        write_paginated_txt(pages, out_txt)
        logging.info(f"Pipeline PDF terminé pour {pdf_path.name}")

    except Exception:
        logging.exception("Échec du pipeline PDF vectoriel.")
        raise




# ---------------------------------------------
# running Pipeline 3
# ---------------------------------------------
@log_execution_time
def pipeline_pdf_raster_to_txt(
    input_pdf: Path,
    paginated_folder: Path,
    ocr_folder: Path,
    dpi: int = 300,
) -> Path | None:
    """Traite un PDF raster page par page afin de borner l'usage mémoire."""
    logging.info("Démarrage du pipeline PDF raster.")
    try:
        is_rast, page_count = is_raster_pdf(input_pdf)
        if not is_rast:
            raise ValueError("Le document fourni au pipeline OCR n'est pas un PDF raster.")

        _validate_pdf_resource_limits(input_pdf, dpi=dpi)
        paginated_folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        ocr_folder.mkdir(parents=True, exist_ok=True, mode=0o700)

        pdf_paginated = paginated_folder / f"{input_pdf.stem}_paginated.pdf"
        out_txt = ocr_folder / f"{input_pdf.stem}.txt"
        pages: list[str] = []
        output_document = fitz.open()

        try:
            for page_number in range(1, page_count + 1):
                rendered = convert_from_path(
                    str(input_pdf),
                    dpi=dpi,
                    first_page=page_number,
                    last_page=page_number,
                    thread_count=1,
                    timeout=conf.PDF_RENDER_TIMEOUT_SECONDS,
                )
                if len(rendered) != 1:
                    raise RuntimeError(f"Rendu inattendu pour la page {page_number}.")

                image = rendered[0]
                try:
                    pixels = image.width * image.height
                    if pixels > conf.MAX_RENDER_PIXELS_PER_PAGE:
                        raise ValueError(
                            f"Page {page_number} trop volumineuse après rendu ({pixels} pixels)."
                        )

                    annotated = annotate_page_image(image, page_number, dpi)
                    try:
                        buffer = BytesIO()
                        annotated.save(buffer, format="PNG")
                        rect = fitz.Rect(
                            0,
                            0,
                            image.width * 72.0 / dpi,
                            image.height * 72.0 / dpi,
                        )
                        pdf_page = output_document.new_page(width=rect.width, height=rect.height)
                        pdf_page.insert_image(rect, stream=buffer.getvalue())
                    finally:
                        annotated.close()

                    gray = image.convert("L")
                    try:
                        raw = pytesseract.image_to_string(
                            gray,
                            lang="eng",
                            config="--oem 3 --psm 1",
                            timeout=conf.OCR_PAGE_TIMEOUT_SECONDS,
                        )
                    finally:
                        gray.close()

                    lines = raw.splitlines()
                    if lines and lines[0].startswith(f"--- PAGE {page_number} ---"):
                        lines = lines[1:]
                    cleaned = correct_text("\n".join(lines))
                    pages.append(reflow_paragraphs(cleaned))
                    logging.info("OCR terminé pour la page %d/%d.", page_number, page_count)
                finally:
                    image.close()

            output_document.save(str(pdf_paginated), deflate=True)
        finally:
            output_document.close()

        compress_if_needed(pdf_paginated)
        if len(pages) > 1:
            pages = remove_redundant_lines(pages, threshold=0.9)

        write_paginated_txt(pages, out_txt)

        logging.info("Pipeline PDF raster terminé avec succès.")
        return out_txt
    except Exception:
        logging.exception("Échec du pipeline PDF raster.")
        return None


