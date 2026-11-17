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

# Voz secundaria opcional (menu.py pregunta cuál usar por vídeo, ver
# ask_elevenlabs_voice): cada voz tiene su propia carpeta de "Miau", grabados
# con esa misma voz — si no, sonaría el sello con la voz 1 encima de una
# narración con la voz 2. Genera el set de una voz nueva con:
#   python generate_miau_options.py --voice-id TU_VOICE_ID --output-dir ./assets/gato_intros_TU_NOMBRE
# y añade la entrada aquí.
SECONDARY_ELEVENLABS_VOICE_ID = "DUnzBkwtjRWXPr6wRbmL"

# Voz de gato gratuita "GATO_1" (Kokoro, ver voz_gato_libre.py): es la voz por
# defecto, sin ElevenLabs ni claves. Para volver a ElevenLabs, pon en el .env:
#   VOZ_GATO=elevenlabs
LIBRE_VOICE_ID = "kokoro-gato1"
INTRO_CLIPS_DIR_BY_VOICE = {
    SECONDARY_ELEVENLABS_VOICE_ID: Path("./assets/gato_intros_voice2"),
    LIBRE_VOICE_ID: Path("./assets/gato_intros_libre"),
}


def usa_voz_libre() -> bool:
    """True salvo que el .env diga VOZ_GATO=elevenlabs."""
    load_dotenv()
    import entorno
    entorno.cargar_env()
    return os.getenv("VOZ_GATO", "libre").strip().lower() != "elevenlabs"


def intro_clips_dir_for(voice_id: str | None) -> Path:
    if usa_voz_libre():
        return INTRO_CLIPS_DIR_BY_VOICE[LIBRE_VOICE_ID]
    return INTRO_CLIPS_DIR_BY_VOICE.get(voice_id, INTRO_CLIPS_DIR)

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
    """(cliente, voice_id). Con la voz libre (por defecto) no hace falta
    ElevenLabs: devuelve (None, LIBRE_VOICE_ID) y generate_speech usa Kokoro."""
    if usa_voz_libre():
        return None, LIBRE_VOICE_ID
    load_dotenv()
    import entorno  # además del load_dotenv: .env en UTF-16 o variables vacías de Windows
    entorno.cargar_env()
    api_key = os.getenv("ELEVENLABS_API_KEY")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID")

    if not api_key or not voice_id:
        sys.exit(
            "Falta ELEVENLABS_API_KEY o ELEVENLABS_VOICE_ID en el .env. "
            "Revisa .env.example para saber cómo conseguirlos."
        )

    from elevenlabs.client import ElevenLabs
    return ElevenLabs(api_key=api_key), voice_id


def prepend_intro_clip(narration_path: Path, voice_id: str | None = None) -> None:
    """Pega un 'Miau' elegido al azar delante de la narración generada,
    sustituyendo el archivo original por la versión combinada. Usa la
    carpeta de Miaus de la voz indicada (intro_clips_dir_for) para que el
    sello suene coherente con quien narra — por defecto INTRO_CLIPS_DIR (voz
    de siempre). No hace nada si no hay clips de intro disponibles todavía."""
    import random

    intro_dir = intro_clips_dir_for(voice_id)
    available_clips = list(intro_dir.glob("*.mp3")) if intro_dir.exists() else []

    if not available_clips:
        print(f"  [AVISO] No hay clips en {intro_dir}, se deja el audio sin 'Miau' inicial.")
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
    if client is None or voice_id == LIBRE_VOICE_ID:
        # Voz libre: aunque el menú pida otra voz de ElevenLabs para un vídeo,
        # sin cliente se usa siempre la voz de gato gratuita.
        import voz_gato_libre
        try:
            voz_gato_libre.generar(text, Path(output_path))
            return True
        except SystemExit as e:
            print(f"  [ERROR] No se pudo generar audio con la voz libre: {e}")
            Path(output_path).unlink(missing_ok=True)
            return False
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
        # audio.convert() puede fallar A MITAD de iterar los chunks (p.ej.
        # cuota agotada de ElevenLabs) — para entonces el archivo ya está
        # creado (aunque sea de 0 bytes o a medias). Sin borrarlo, el resto
        # del pipeline lo confunde con una narración válida y explota más
        # adelante con un error críptico.
        output_path.unlink(missing_ok=True)
        return False


def main():
    parser = argparse.ArgumentParser(description="Genera narración de voz con ElevenLabs")
    parser.add_argument("--limit", type=int, default=None, help="Máximo de guiones a procesar")
    parser.add_argument("--only", type=str, default=None, help="Procesa solo el guion cuyo nombre de archivo empieza así (sin extensión)")
    parser.add_argument("--dry-run", action="store_true", help="Solo muestra qué se generaría, sin llamar a la API")
    parser.add_argument("--voice-id", type=str, default=None,
                         help="Voice ID de ElevenLabs a usar en vez del ELEVENLABS_VOICE_ID del .env (para probar otra voz en un vídeo concreto)")
    args = parser.parse_args()

    if not INPUT_DIR.exists():
        sys.exit(f"No existe la carpeta {INPUT_DIR}. Ejecuta antes generate_script.py.")

    client, voice_id = (None, None) if args.dry_run else get_elevenlabs_client()
    if args.voice_id:
        voice_id = args.voice_id

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
            prepend_intro_clip(output_path, voice_id)
            print(f"  Guardado en {output_path}")
            log[key] = {"status": "ok", "output": str(output_path)}
            processed_count += 1
        else:
            log[key] = {"status": "error"}

    save_log(log)
    print(f"\nHecho. {processed_count} audios {'validados' if args.dry_run else 'generados'} en esta pasada.")

    if args.only and log.get(f"{args.only}.json", {}).get("status") == "error":
        sys.exit(1)  # para que quien invoque este script (menu.py) sepa que falló


if __name__ == "__main__":
    main()
