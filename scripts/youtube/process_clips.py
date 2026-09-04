"""
process_clips.py — Paso 2 del pipeline ReactionFlow: filtrado + conversión a 9:16.

Recorre los vídeos descargados en ./raw_clips (generados por youtube_downloader.py),
valida que cada uno esté en buen estado (no corrupto, resolución mínima), y los
convierte a formato vertical 9:16 (1080x1920) con el vídeo centrado y un fondo
difuminado (blur) del mismo vídeo rellenando arriba y abajo.

El resultado deja el centro del encuadre despejado para añadir más adelante texto,
subtítulos o el avatar de reacción, sin tapar el vídeo original.

Requisitos:
    - ffmpeg y ffprobe instalados y accesibles desde la terminal (prueba: ffmpeg -version)
    - pip install python-dotenv  (no imprescindible aquí, pero mantiene consistencia)

Uso:
    python process_clips.py
    python process_clips.py --limit 3 --dry-run
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# La consola de Windows por defecto usa cp1252, que no soporta emojis ni
# muchos caracteres de nombres de archivo/títulos reales. Sin esto, algunos
# vídeos pueden reventar el script a mitad de proceso.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ----------------------------------------------------------------------------
# CONFIGURACIÓN
# ----------------------------------------------------------------------------

INPUT_DIR = Path("./raw_clips")
OUTPUT_DIR = Path("./processed_clips")
LOG_FILE = Path("./processed.json")

# Resolución de salida (formato TikTok estándar)
OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920

# Intensidad del difuminado de fondo (más alto = más borroso)
BLUR_STRENGTH = 20

# Resolución mínima de entrada aceptada (para descartar vídeos de mala calidad)
MIN_INPUT_WIDTH = 480
MIN_INPUT_HEIGHT = 360

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov"}

# ----------------------------------------------------------------------------


def load_log() -> dict:
    if LOG_FILE.exists():
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_log(log: dict) -> None:
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def check_ffmpeg_available() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        try:
            subprocess.run([tool, "-version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            sys.exit(
                f"No se encuentra '{tool}' en el sistema. Instala ffmpeg "
                "(https://ffmpeg.org/download.html) y asegúrate de que esté en el PATH."
            )


def probe_video(filepath: Path) -> dict | None:
    """Obtiene resolución y duración de un vídeo con ffprobe. None si está corrupto."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-show_entries", "format=duration",
                "-of", "json",
                str(filepath),
            ],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        return {
            "width": stream["width"],
            "height": stream["height"],
            "duration": float(data["format"]["duration"]),
        }
    except (subprocess.CalledProcessError, KeyError, IndexError, json.JSONDecodeError):
        return None


def is_valid_input(info: dict) -> bool:
    return info["width"] >= MIN_INPUT_WIDTH and info["height"] >= MIN_INPUT_HEIGHT


def convert_to_vertical(input_path: Path, output_path: Path) -> bool:
    """Convierte un vídeo a 9:16 con fondo difuminado usando ffmpeg. True si tuvo éxito."""
    filter_complex = (
        f"[0:v]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},boxblur={BLUR_STRENGTH}:1[bg];"
        f"[0:v]scale={OUTPUT_WIDTH}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "0:a?",  # incluye audio si existe, sin fallar si no hay
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [ERROR ffmpeg] {result.stderr[-500:]}")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Filtra y convierte clips a 9:16 para ReactionFlow")
    parser.add_argument("--limit", type=int, default=None, help="Máximo de vídeos a procesar en esta pasada")
    parser.add_argument("--dry-run", action="store_true", help="Solo valida y muestra qué se procesaría")
    args = parser.parse_args()

    check_ffmpeg_available()

    if not INPUT_DIR.exists():
        sys.exit(f"No existe la carpeta {INPUT_DIR}. Ejecuta antes youtube_downloader.py.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log = load_log()
    processed_count = 0

    video_files = sorted(
        f for f in INPUT_DIR.iterdir() if f.suffix.lower() in VIDEO_EXTENSIONS
    )

    if not video_files:
        print(f"No hay vídeos en {INPUT_DIR}.")
        return

    for filepath in video_files:
        if args.limit and processed_count >= args.limit:
            break

        key = filepath.name
        if key in log and log[key].get("status") == "ok":
            continue

        print(f"\n{filepath.name}")
        info = probe_video(filepath)

        if info is None:
            print("  [DESCARTADO] Archivo corrupto o ilegible")
            log[key] = {"status": "corrupt"}
            continue

        if not is_valid_input(info):
            print(f"  [DESCARTADO] Resolución insuficiente ({info['width']}x{info['height']})")
            log[key] = {"status": "low_resolution", "info": info}
            continue

        print(f"  Resolución: {info['width']}x{info['height']}  |  Duración: {info['duration']:.1f}s")

        output_path = OUTPUT_DIR / f"{filepath.stem}_9x16.mp4"

        if args.dry_run:
            print("  [DRY-RUN] Se convertiría a 9:16")
            processed_count += 1
            continue

        print("  Convirtiendo a 9:16 con fondo difuminado...")
        success = convert_to_vertical(filepath, output_path)

        if success:
            print(f"  Guardado en {output_path}")
            log[key] = {"status": "ok", "output": str(output_path), "info": info}
            processed_count += 1
        else:
            log[key] = {"status": "ffmpeg_error"}

    save_log(log)
    print(f"\nHecho. {processed_count} vídeos {'validados' if args.dry_run else 'procesados'} en esta pasada.")


if __name__ == "__main__":
    main()
