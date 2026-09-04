"""
youtube_downloader.py — Paso 1 del pipeline ReactionFlow: descarga de vídeos fuente.

Busca vídeos en YouTube que el propio creador ha marcado con licencia
Creative Commons (CC-BY) — es decir, con permiso explícito de reutilización,
siempre que se dé atribución al autor original. Filtra por duración y
descarga con yt-dlp. Lleva un registro (downloaded.json) para no descargar
el mismo vídeo dos veces.

IMPORTANTE sobre atribución: la licencia CC-BY de YouTube exige citar al
autor original. Este script guarda esa info en metadata.json junto a cada
vídeo descargado — asegúrate de incluir el crédito en la publicación final
(por ejemplo, en la descripción o como texto superpuesto).

Requisitos:
    pip install google-api-python-client yt-dlp python-dotenv

Credenciales de YouTube (gratis):
    1. Ve a https://console.cloud.google.com/
    2. Crea un proyecto nuevo (o usa uno existente)
    3. Ve a "APIs y servicios" -> "Biblioteca" -> busca "YouTube Data API v3" -> Habilitar
    4. Ve a "APIs y servicios" -> "Credenciales" -> "Crear credenciales" -> "Clave de API"
    5. Copia la clave y pégala en el .env (ver .env.example)

Uso:
    python youtube_downloader.py
    python youtube_downloader.py --limit 5 --dry-run
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from googleapiclient.discovery import build

# La consola de Windows por defecto usa cp1252, que no soporta emojis ni
# muchos caracteres que aparecen en títulos reales de YouTube. Sin esto, un
# título con un emoji (🥹, 😭, etc.) revienta el script a mitad de búsqueda.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ----------------------------------------------------------------------------
# CONFIGURACIÓN — ajusta esto a tu gusto
# ----------------------------------------------------------------------------

# Términos de búsqueda, agrupados por nicho. Cuantos más pongas, más variedad.
#
# Se evitan a propósito palabras como "compilation", "ranking", "fail
# compilation" o "try not to laugh": ese tipo de búsqueda trae sobre todo
# canales de "ranking"/compilación ya montados (con títulos y numeración
# quemados), que generate_script.py descarta de todas formas por no ser
# metraje en bruto. Se prioriza contenido de cámaras "crudas" (dashcam,
# timbre, seguridad, bodycam): un incidente grabado tal cual, sin editar y
# sin nadie reaccionando encima, que es justo lo que necesita el pipeline.
SEARCH_QUERIES = [
    # Cámaras de vehículo / calle
    "dashcam catches",
    "dashcam footage",
    "crazy dashcam moment",
    # Cámaras de casa / timbre / seguridad
    "doorbell camera catches",
    "ring camera catches",
    "security camera catches",
    # Otras cámaras fijas
    "trail camera footage",
    "bodycam footage",
]

RESULTS_PER_QUERY = 15  # cuántos resultados revisar por búsqueda (no cuántos se descargan)

# Filtros de duración (en segundos)
MIN_DURATION_SECONDS = 10
MAX_DURATION_SECONDS = 90

# Si el título contiene alguna de estas palabras (sin importar mayúsculas), se descarta.
# Útil para filtrar contenido médico/desagradable que a veces cuela en búsquedas
# de "satisfying" o "amazing" (ej. vídeos de granos, cirugías, etc.), y también
# para evitar vídeos que ya son de "reacción" (con la cara/facecam de otra
# persona comentando) — el objetivo es que el gato sea el único que reacciona,
# no doblar la reacción sobre la reacción de otro. Esto es solo un filtro por
# título; generate_script.py hace además una comprobación visual con Claude
# por si el facecam no se menciona en el título.
EXCLUDE_KEYWORDS = [
    "acne", "pimple", "surgery", "cyst", "blackhead", "removal",
    "gore", "graphic", "warning",
    "reaction", "reacts", "reacting", "react to",
]

# Región para adaptar resultados a tu mercado (ES = España). Cambia si quieres otro país.
REGION_CODE = "ES"

# Solo busca vídeos publicados en los últimos N días, para priorizar contenido
# de actualidad en vez de vídeos antiguos genéricos.
PUBLISHED_WITHIN_DAYS = 15

# Carpetas
OUTPUT_DIR = Path("./raw_clips")
LOG_FILE = Path("./downloaded.json")

# ----------------------------------------------------------------------------


def load_downloaded_log() -> dict:
    """Carga el diccionario {video_id: info} de vídeos ya descargados."""
    if LOG_FILE.exists():
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_downloaded_log(log: dict) -> None:
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def get_youtube_client():
    load_dotenv()
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        sys.exit(
            "Falta YOUTUBE_API_KEY en el .env. "
            "Revisa .env.example para saber cómo obtenerlo."
        )
    return build("youtube", "v3", developerKey=api_key)


def parse_iso8601_duration(duration: str) -> int:
    """Convierte una duración ISO 8601 (ej. 'PT1M30S') a segundos."""
    import re

    match = re.match(
        r"PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?", duration
    )
    if not match:
        return 0
    parts = match.groupdict()
    hours = int(parts["hours"] or 0)
    minutes = int(parts["minutes"] or 0)
    seconds = int(parts["seconds"] or 0)
    return hours * 3600 + minutes * 60 + seconds


def search_cc_videos(youtube, query: str, max_results: int) -> list:
    """Busca vídeos con licencia Creative Commons para una query dada,
    priorizando los más vistos y publicados recientemente."""
    from datetime import datetime, timedelta, timezone

    published_after = (
        datetime.now(timezone.utc) - timedelta(days=PUBLISHED_WITHIN_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    response = (
        youtube.search()
        .list(
            part="snippet",
            q=query,
            type="video",
            videoLicense="creativeCommon",  # solo vídeos reutilizables
            maxResults=max_results,
            order="viewCount",  # prioriza lo que ya está funcionando bien
            publishedAfter=published_after,  # solo contenido reciente
            regionCode=REGION_CODE,
            safeSearch="moderate",
        )
        .execute()
    )
    return response.get("items", [])


def get_video_details(youtube, video_ids: list) -> dict:
    """Pide duración y licencia exacta para una lista de IDs (máx 50 por llamada)."""
    response = (
        youtube.videos()
        .list(part="contentDetails,status,snippet,statistics", id=",".join(video_ids))
        .execute()
    )
    return {item["id"]: item for item in response.get("items", [])}


def is_valid_cc_video(details: dict) -> bool:
    """Comprueba licencia CC, duración dentro de rango y ausencia de palabras excluidas."""
    if details.get("status", {}).get("license") != "creativeCommon":
        return False

    duration = parse_iso8601_duration(details["contentDetails"]["duration"])
    if duration < MIN_DURATION_SECONDS or duration > MAX_DURATION_SECONDS:
        return False

    title_lower = details["snippet"]["title"].lower()
    if any(keyword in title_lower for keyword in EXCLUDE_KEYWORDS):
        return False

    return True


def download_with_ytdlp(video_id: str, output_dir: Path) -> Path | None:
    """Descarga un vídeo de YouTube usando yt-dlp. Devuelve la ruta del archivo o None si falla."""
    import yt_dlp

    output_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    filename_template = str(output_dir / f"{video_id}.%(ext)s")

    ydl_opts = {
        "outtmpl": filename_template,
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info)).with_suffix(".mp4")
    except Exception as e:
        print(f"  [ERROR] No se pudo descargar {url}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Descarga vídeos CC de YouTube para ReactionFlow")
    parser.add_argument("--limit", type=int, default=None, help="Máximo de vídeos a descargar en esta pasada")
    parser.add_argument("--dry-run", action="store_true", help="Solo muestra qué se descargaría, sin descargar")
    args = parser.parse_args()

    youtube = get_youtube_client()
    downloaded_log = load_downloaded_log()
    new_downloads = []

    for query in SEARCH_QUERIES:
        if args.limit and len(new_downloads) >= args.limit:
            break

        print(f"\n=== Búsqueda: \"{query}\" ===")
        candidates = search_cc_videos(youtube, query, RESULTS_PER_QUERY)
        candidate_ids = [c["id"]["videoId"] for c in candidates if c["id"]["videoId"] not in downloaded_log]

        if not candidate_ids:
            continue

        details_map = get_video_details(youtube, candidate_ids)

        for video_id, details in details_map.items():
            if args.limit and len(new_downloads) >= args.limit:
                break

            if not is_valid_cc_video(details):
                continue

            title = details["snippet"]["title"]
            channel = details["snippet"]["channelTitle"]
            views = int(details.get("statistics", {}).get("viewCount", 0))
            print(f"  -> [{views:,} vistas] {title[:55]} (por {channel})")

            entry = {
                "title": title,
                "channel": channel,
                "channel_id": details["snippet"]["channelId"],
                "license": "Creative Commons - Attribution (CC-BY)",
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }

            if args.dry_run:
                downloaded_log[video_id] = entry
                new_downloads.append(video_id)
                continue

            filepath = download_with_ytdlp(video_id, OUTPUT_DIR)

            if filepath is not None:
                entry["local_path"] = str(filepath)
                downloaded_log[video_id] = entry
                new_downloads.append(video_id)
                print(f"     Guardado en {filepath}  |  Créditos: {channel}")

    save_downloaded_log(downloaded_log)
    print(f"\nHecho. {len(new_downloads)} vídeos nuevos {'detectados' if args.dry_run else 'descargados'}.")
    print("Recuerda: la licencia CC-BY exige dar crédito al canal original en la publicación final.")


if __name__ == "__main__":
    main()
