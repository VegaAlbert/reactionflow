"""
text_to_speech.py — Paso 4 del pipeline ReactionFlow: guion -> voz narrada.

Lee los guiones generados en ./scripts (por generate_script.py) y genera un
archivo de audio .mp3 por cada uno usando la API de ElevenLabs, con una voz
en español consistente (el "personaje" del gato).

El audio resultante se usará más adelante para:
    - Sincronizar el movimiento de boca del avatar (sprite-swap por volumen)
    - Generar subtítulos quemados en el vídeo

Requisitos:
    pip install elevenlabs python-dotenv

Credenciales de ElevenLabs (nivel gratis disponible):
    1. Ve a https://elevenlabs.io/ y crea una cuenta
    2. Ve a tu perfil (arriba a la derecha) -> "API Keys" (o directamente
       https://elevenlabs.io/app/settings/api-keys)
    3. Copia la clave y pégala en el .env (ver .env.example)

Elegir una voz:
    1. Ve a https://elevenlabs.io/app/voice-library
    2. Prueba voces en español hasta encontrar una que le pegue al personaje
       (para un gato con carisma, busca algo con energía/personalidad, no
       una voz plana de "locutor de noticias")
    3. Añádela a tu biblioteca ("Add to my voices") y copia su Voice ID
       (aparece en la URL o en los detalles de la voz) al .env

Uso:
    python text_to_speech.py
    python text_to_speech.py --limit 2 --dry-run
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# La consola de Windows por defecto usa cp1252, que no soporta emojis ni
# muchos caracteres. Sin esto, un guion con algún carácter raro revienta el
# script a mitad de proceso.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ----------------------------------------------------------------------------
# CONFIGURACIÓN
# ----------------------------------------------------------------------------

INPUT_DIR = Path("./scripts")
OUTPUT_DIR = Path("./audio")
LOG_FILE = Path("./voiced.json")

# Modelo de voz de ElevenLabs. "eleven_multilingual_v2" tiene muy buen español.
ELEVENLABS_MODEL = "eleven_multilingual_v2"

# Ajustes de estilo de la voz (0.0 a 1.0)
VOICE_STABILITY = 0.4       # más bajo = más expresivo/variable, más alto = más monótono/estable
VOICE_SIMILARITY_BOOST = 0.8
VOICE_STYLE = 0.6           # más alto = más "actuado"/exagerado, útil para dar carisma

# Clips de audio fijos (los "Miau" de sello de identidad) entre los que se
# elige uno al azar para pegar al principio de cada narración. Así el sello
# de identidad se mantiene pero no suena repetitivo en cada vídeo.
# Genéralos tú mismo con generate_miau_options.py, elige tus favoritos y
# ponlos en esta carpeta con cualquier nombre (se cogen todos los .mp3 de aquí).
INTRO_CLIPS_DIR = Path("./assets/gato_intros")

# ----------------------------------------------------------------------------


def load_log() -> dict:
    if LOG_FILE.exists():
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_log(log: dict) -> None:
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def get_elevenlabs_client():
    load_dotenv()
    api_key = os.getenv("ELEVENLABS_API_KEY")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID")

    if not api_key or not voice_id:
        sys.exit(
            "Falta ELEVENLABS_API_KEY o ELEVENLABS_VOICE_ID en el .env. "
            "Revisa .env.example para saber cómo conseguirlos."
        )

    from elevenlabs.client import ElevenLabs
    return ElevenLabs(api_key=api_key), voice_id


def prepend_intro_clip(narration_path: Path) -> None:
    """Pega un 'Miau' elegido al azar (de INTRO_CLIPS_DIR) delante de la
    narración generada, sustituyendo el archivo original por la versión
    combinada. No hace nada si no hay clips de intro disponibles todavía."""
    import random

    available_clips = list(INTRO_CLIPS_DIR.glob("*.mp3")) if INTRO_CLIPS_DIR.exists() else []

    if not available_clips:
        print(f"  [AVISO] No hay clips en {INTRO_CLIPS_DIR}, se deja el audio sin 'Miau' inicial.")
        return

    chosen_clip = random.choice(available_clips)
    print(f"  Intro elegida al azar: {chosen_clip.name}")

    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir=narration_path.parent) as tmp:
        combined_path = Path(tmp.name)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(chosen_clip),
        "-i", str(narration_path),
        "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[out]",
        "-map", "[out]",
        str(combined_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        combined_path.replace(narration_path)
    else:
        print(f"  [ERROR] No se pudo pegar el intro: {result.stderr[-300:]}")
        combined_path.unlink(missing_ok=True)


def generate_speech(client, voice_id: str, text: str, output_path: Path) -> bool:
    try:
        audio = client.text_to_speech.convert(
            voice_id=voice_id,
            model_id=ELEVENLABS_MODEL,
            text=text,
            voice_settings={
                "stability": VOICE_STABILITY,
                "similarity_boost": VOICE_SIMILARITY_BOOST,
                "style": VOICE_STYLE,
                "use_speaker_boost": True,
            },
        )
        with open(output_path, "wb") as f:
            for chunk in audio:
                f.write(chunk)
        return True
    except Exception as e:
        print(f"  [ERROR] No se pudo generar audio: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Genera narración de voz con ElevenLabs")
    parser.add_argument("--limit", type=int, default=None, help="Máximo de guiones a procesar")
    parser.add_argument("--only", type=str, default=None, help="Procesa solo el guion cuyo nombre de archivo empieza así (sin extensión)")
    parser.add_argument("--dry-run", action="store_true", help="Solo muestra qué se generaría, sin llamar a la API")
    args = parser.parse_args()

    if not INPUT_DIR.exists():
        sys.exit(f"No existe la carpeta {INPUT_DIR}. Ejecuta antes generate_script.py.")

    client, voice_id = (None, None) if args.dry_run else get_elevenlabs_client()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log = load_log()
    processed_count = 0

    script_files = sorted(INPUT_DIR.glob("*.json"))
    if args.only:
        script_files = [p for p in script_files if p.stem == args.only]

    if not script_files:
        print(f"No hay guiones en {INPUT_DIR}.")
        return

    for script_path in script_files:
        if args.limit and processed_count >= args.limit:
            break

        key = script_path.name
        if key in log and log[key].get("status") == "ok" and not args.only:
            continue

        with open(script_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        script_text = data["script_es"]
        print(f"\n{script_path.name}")
        print(f"  Texto: \"{script_text[:80]}...\"")

        output_path = OUTPUT_DIR / f"{script_path.stem}.mp3"

        if args.dry_run:
            print(f"  [DRY-RUN] Se generaría audio en {output_path}")
            processed_count += 1
            continue

        print("  Generando voz con ElevenLabs...")
        success = generate_speech(client, voice_id, script_text, output_path)

        if success:
            prepend_intro_clip(output_path)
            print(f"  Guardado en {output_path}")
            log[key] = {"status": "ok", "output": str(output_path)}
            processed_count += 1
        else:
            log[key] = {"status": "error"}

    save_log(log)
    print(f"\nHecho. {processed_count} audios {'validados' if args.dry_run else 'generados'} en esta pasada.")


if __name__ == "__main__":
    main()
