"""
generate_avatar_replicate.py — Paso 5 del pipeline ReactionFlow: animación del
avatar del gato con IA generativa (Replicate: wan-video/wan-2.2-s2v).

Sustituye a la animación local por sprite-swap (pocas imágenes fijas
intercambiadas según el volumen) por un vídeo generado fotograma a fotograma:
se sube la imagen de referencia del gato (fondo verde) + la narración de audio
a un modelo de IA que anima boca, ojos y expresión de forma realista, y se
descarga el resultado. El modelo conserva el fondo verde de la imagen de
referencia, así que compose_avatar.py le sigue haciendo chroma key exactamente
igual que a los sprites.

El vídeo generado dura lo mismo que el audio de entrada — no hay que indicar
duración por separado.

Coste: ~$0.02 por segundo de vídeo (wan-2.2-s2v). Un audio de 20s cuesta unos
$0.40. Necesitas crédito cargado en tu cuenta de Replicate.

Imagen de referencia del gato:
    Por defecto usa la base (solo cabeza) — el modelo anima solo lo que ve en
    la imagen, no inventa cuerpo. Si en el futuro consigues una imagen con
    medio cuerpo (hombros/torso visibles, mismo fondo verde y mismo estilo),
    cambia REFERENCE_IMAGE más abajo: el resto del script no necesita tocarse.

Requisitos:
    pip install replicate
    Cuenta en https://replicate.com con crédito cargado, y tu token en el .env
    (ver REPLICATE_API_TOKEN en env.example) — consíguelo en
    https://replicate.com/account/api-tokens

Uso:
    python generate_avatar_replicate.py
    python generate_avatar_replicate.py --limit 1 --dry-run
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# La consola de Windows por defecto usa cp1252, que no soporta emojis ni
# muchos caracteres. Sin esto, algún mensaje de error de la API con un
# carácter raro podría reventar el script a mitad de proceso.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ----------------------------------------------------------------------------
# CONFIGURACIÓN
# ----------------------------------------------------------------------------

INPUT_AUDIO_DIR = Path("./audio")
OUTPUT_DIR = Path("./avatar_clips")
LOG_FILE = Path("./avatar_replicate.json")

# Imagen de referencia que se anima. Debe tener fondo verde uniforme, igual
# que el resto de sprites del avatar.
REFERENCE_IMAGE = Path("./assets/avatar/gato_boca_cerrada.png")

REPLICATE_MODEL = "wan-video/wan-2.2-s2v"

# Describe al personaje para guiar el estilo de animación. En inglés: el
# modelo entiende mejor las instrucciones en ese idioma aunque el audio esté
# en español.
PROMPT = "a gray cat with glasses talking, subtle natural head and mouth movement"

# Fotogramas por bloque interno del modelo (no es la duración total del
# vídeo, que siempre coincide con la del audio). El valor por defecto del
# modelo (81) funciona bien; no hace falta tocarlo salvo que investigues más.
NUM_FRAMES_PER_CHUNK = 81

# Cada cuántos segundos se consulta el estado de la predicción mientras se
# genera el vídeo (puede tardar varios minutos).
POLL_INTERVAL_SECONDS = 10

# ----------------------------------------------------------------------------


def load_log() -> dict:
    if LOG_FILE.exists():
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_log(log: dict) -> None:
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def get_replicate_client():
    load_dotenv()
    api_token = os.getenv("REPLICATE_API_TOKEN")
    if not api_token:
        sys.exit(
            "Falta REPLICATE_API_TOKEN en el .env. Consíguelo en "
            "https://replicate.com/account/api-tokens y carga crédito en "
            "https://replicate.com/account/billing (ver env.example)."
        )
    import replicate
    return replicate.Client(api_token=api_token)


def download_output(output, dest: Path) -> None:
    """El cliente de replicate puede devolver una URL, una lista de URLs, o un
    objeto FileOutput según la versión instalada; esto cubre los tres casos."""
    if isinstance(output, list):
        output = output[0]
    url = getattr(output, "url", None) or str(output)

    import requests
    response = requests.get(url, timeout=300)
    response.raise_for_status()
    with open(dest, "wb") as f:
        f.write(response.content)


def generate_avatar_clip(client, audio_path: Path, output_path: Path) -> bool:
    # No se usa client.run(): su prediction.wait() interno se queda esperando
    # con un comportamiento propio que en la práctica revienta con "read
    # timed out" en audios largos, AUNQUE la predicción siga corriendo (y
    # cobrando) del lado de Replicate igualmente. En su lugar, se envía la
    # predicción y se sondea a mano cada pocos segundos: así se sabe siempre
    # el estado real y nunca se pierde una predicción ya pagada.
    try:
        with open(REFERENCE_IMAGE, "rb") as image_file, open(audio_path, "rb") as audio_file:
            prediction = client.predictions.create(
                model=REPLICATE_MODEL,
                input={
                    "prompt": PROMPT,
                    "image": image_file,
                    "audio": audio_file,
                    "num_frames_per_chunk": NUM_FRAMES_PER_CHUNK,
                },
            )

        print(f"  Predicción enviada ({prediction.id}), esperando...")
        while prediction.status not in ("succeeded", "failed", "canceled"):
            time.sleep(POLL_INTERVAL_SECONDS)
            prediction = client.predictions.get(prediction.id)

        if prediction.status != "succeeded":
            print(f"  [ERROR] La predicción {prediction.id} terminó como '{prediction.status}': {prediction.error}")
            return False

        download_output(prediction.output, output_path)
        return True
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Anima el avatar del gato con IA (Replicate)")
    parser.add_argument("--limit", type=int, default=None, help="Máximo de audios a procesar en esta pasada")
    parser.add_argument("--only", type=str, default=None, help="Procesa solo el audio cuyo nombre de archivo empieza así (sin extensión)")
    parser.add_argument("--dry-run", action="store_true", help="Solo muestra qué se generaría, sin llamar a la API")
    args = parser.parse_args()

    if not REFERENCE_IMAGE.exists():
        sys.exit(f"No existe la imagen de referencia {REFERENCE_IMAGE}.")

    if not INPUT_AUDIO_DIR.exists():
        sys.exit(f"No existe la carpeta {INPUT_AUDIO_DIR}. Ejecuta antes text_to_speech.py.")

    client = None if args.dry_run else get_replicate_client()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log = load_log()
    processed_count = 0

    audio_files = sorted(INPUT_AUDIO_DIR.glob("*.mp3"))
    if args.only:
        audio_files = [p for p in audio_files if p.stem == args.only]

    if not audio_files:
        print(f"No hay audios en {INPUT_AUDIO_DIR}.")
        return

    for audio_path in audio_files:
        if args.limit and processed_count >= args.limit:
            break

        key = audio_path.name
        if key in log and log[key].get("status") == "ok" and not args.only:
            continue

        print(f"\n{audio_path.name}")
        output_path = OUTPUT_DIR / f"{audio_path.stem}.mp4"

        if args.dry_run:
            print(f"  [DRY-RUN] Se generaría {output_path} vía Replicate ({REPLICATE_MODEL})")
            processed_count += 1
            continue

        print(f"  Generando avatar con IA (Replicate: {REPLICATE_MODEL})...")
        success = generate_avatar_clip(client, audio_path, output_path)

        if success:
            print(f"  Guardado en {output_path}")
            log[key] = {"status": "ok", "output": str(output_path)}
            processed_count += 1
        else:
            log[key] = {"status": "error"}

    save_log(log)
    print(f"\nHecho. {processed_count} avatares {'analizados' if args.dry_run else 'generados'} en esta pasada.")


if __name__ == "__main__":
    main()
