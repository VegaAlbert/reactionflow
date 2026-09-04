"""
generate_script.py — Paso 3 del pipeline ReactionFlow: análisis + guion de reacción.

Para cada vídeo procesado en ./processed_clips:
    1. Extrae varios fotogramas representativos con ffmpeg.
    2. Transcribe el audio con faster-whisper (local, sin API externa).
    3. Envía fotogramas + transcripción a la API de Claude (visión) para que
       escriba un guion corto de reacción en español, ajustado a la duración
       real del vídeo.
    4. Guarda el guion en ./scripts/<video_id>.json, listo para el siguiente
       paso (texto a voz / avatar).

Requisitos:
    pip install anthropic faster-whisper python-dotenv
    - ffmpeg y ffprobe instalados (ya los tienes del paso anterior)

Credenciales:
    Rellena ANTHROPIC_API_KEY en el .env (ver .env.example)

Uso:
    python generate_script.py
    python generate_script.py --limit 2 --dry-run
"""

import argparse
import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
import os

# La consola de Windows por defecto usa cp1252, que no soporta emojis ni
# muchos caracteres de títulos reales de YouTube. Sin esto, un título con
# un emoji revienta el script a mitad de proceso.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ----------------------------------------------------------------------------
# CONFIGURACIÓN
# ----------------------------------------------------------------------------

INPUT_DIR = Path("./processed_clips")
OUTPUT_DIR = Path("./scripts")
LOG_FILE = Path("./scripted.json")

FRAMES_PER_VIDEO = 6  # fotogramas repartidos a lo largo del vídeo para dar contexto a la IA
CLAUDE_MODEL = "claude-sonnet-5"

# Palabras por segundo aproximadas al narrar en español de forma natural.
# Se usa para calcular cuántas palabras debe tener el guion según la duración del vídeo.
WORDS_PER_SECOND = 2.3

# Tamaño del modelo de Whisper local: tiny/base/small/medium/large-v3
# "small" es un buen equilibrio velocidad/precisión en CPU normal.
WHISPER_MODEL_SIZE = "small"

# Perfil de estilo: guiones aprobados/rechazados por el usuario, para que el
# gato aprenda su gusto con el tiempo. Lo actualiza menu.py cuando apruebas o
# rechazas un guion; generate_reaction_script() lo usa como ejemplos en el
# prompt (no es "entrenar un modelo", es enseñarle ejemplos reales cada vez).
STYLE_PROFILE_FILE = Path("./style_profile.json")
STYLE_PROFILE_MAX_EXAMPLES = 8  # cuántos ejemplos de cada tipo se guardan/usan como máximo

# Banco de expresiones juveniles/de internet en español para que el gato
# suene actual, no como un narrador neutro de manual. Es solo un banco de
# donde elegir — la instrucción en el prompt le pide usar unas pocas, no
# todas de golpe (sonaría forzado y artificial).
SLANG_BANK = [
    "manito", "mano", "bro", "mi loco", "pana", "máquina", "fiera", "jefe",
    "rey", "crack", "qué lo qué", "qué fue", "qué me cuentas", "qué trama",
    "no puede ser", "me muero", "estoy llorando", "no doy crédito",
    "esto es cine", "tremendo", "icónico", "se lió", "estamos cocinando",
    "let him cook", "qué nivel", "está tocho", "está brutal", "está on fire",
    "nivel dios", "una pasada", "me quiero desinstalar", "necesito contexto",
    "¿pero esto qué es?", "quién te ha dado permiso", "lore", "plot twist",
    "main character", "skill issue", "menudo W", "menuda L", "cringe",
    "based", "mid", "peak", "brainrot", "sus", "aura", "+1000 de aura",
    "flipo", "qué tela", "qué fuerte", "menuda movida", "a tope", "full equip",
]

# ----------------------------------------------------------------------------


def load_log() -> dict:
    if LOG_FILE.exists():
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_log(log: dict) -> None:
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def load_style_profile() -> dict:
    if STYLE_PROFILE_FILE.exists():
        with open(STYLE_PROFILE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"liked": [], "disliked": []}


def save_style_profile(profile: dict) -> None:
    with open(STYLE_PROFILE_FILE, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)


def record_feedback(stem: str, script_text: str, liked: bool, reason: str | None = None) -> None:
    """Guarda un guion aprobado o rechazado en el perfil de estilo, para que
    futuros guiones tengan en cuenta el gusto real del usuario. Se llama
    desde menu.py cada vez que el usuario aprueba/rechaza un guion."""
    profile = load_style_profile()
    bucket = "liked" if liked else "disliked"
    entry = {"stem": stem, "script_es": script_text}
    if not liked and reason:
        entry["reason"] = reason
    profile[bucket].append(entry)
    profile[bucket] = profile[bucket][-STYLE_PROFILE_MAX_EXAMPLES:]
    save_style_profile(profile)


def build_style_context(profile: dict) -> str:
    """Convierte el perfil de estilo en un bloque de texto para el prompt,
    con ejemplos reales de guiones que gustaron/no gustaron. Vacío si
    todavía no hay suficiente historial."""
    liked = profile.get("liked", [])
    disliked = profile.get("disliked", [])
    if not liked and not disliked:
        return ""

    parts = []
    if liked:
        parts.append(
            "GUIONES QUE LE GUSTARON AL USUARIO ANTES (imita ese tono/estilo cuando encaje, "
            "no los copies literalmente):\n"
            + "\n".join(f'- "{e["script_es"]}"' for e in liked)
        )
    if disliked:
        parts.append(
            "GUIONES QUE NO LE GUSTARON (evita repetir estos problemas):\n"
            + "\n".join(
                f'- "{e["script_es"]}"' + (f' — Motivo: {e["reason"]}' if e.get("reason") else "")
                for e in disliked
            )
        )
    return "\n\n".join(parts)


def get_video_duration(filepath: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", str(filepath),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def extract_frames(filepath: Path, tmp_dir: Path, count: int) -> list:
    """Extrae `count` fotogramas repartidos uniformemente por el vídeo."""
    duration = get_video_duration(filepath)
    frame_paths = []

    for i in range(count):
        timestamp = duration * (i + 0.5) / count  # puntos equidistantes
        frame_path = tmp_dir / f"frame_{i}.jpg"
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", str(timestamp), "-i", str(filepath),
                "-frames:v", "1", "-q:v", "2", str(frame_path),
            ],
            capture_output=True, check=True,
        )
        frame_paths.append(frame_path)

    return frame_paths


def extract_audio(filepath: Path, tmp_dir: Path) -> Path:
    audio_path = tmp_dir / "audio.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(filepath),
            "-vn", "-ac", "1", "-ar", "16000", str(audio_path),
        ],
        capture_output=True, check=True,
    )
    return audio_path


def transcribe_audio(audio_path: Path, whisper_model) -> str:
    segments, _ = whisper_model.transcribe(str(audio_path), beam_size=5)
    text = " ".join(segment.text.strip() for segment in segments)
    return text.strip()


def image_to_base64_block(image_path: Path) -> dict:
    with open(image_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
    }


def classify_source_suitability(client, frame_paths: list) -> str | None:
    """Llamada dedicada y aislada (sin la tarea de generar guion de por medio,
    que en pruebas eclipsaba comprobaciones como esta y hacía que el modelo
    las ignorase) para detectar si el clip sirve como metraje en bruto.
    Devuelve None si sirve, o un motivo de descarte: "reactor" (ya trae a una
    persona real reaccionando/comentando, en cualquier disposición visual: un
    recuadro pequeño de cámara web superpuesto estilo streamer, o la cara de
    esa persona ocupando la mayor parte del encuadre con el vídeo de
    referencia en una ventana más pequeña) o "montado" (ya viene editado con
    títulos, numeración, marcas de agua u otros gráficos añadidos a propósito,
    en vez de ser metraje sin tocar)."""
    content = [image_to_base64_block(p) for p in frame_paths]
    content.append({
        "type": "text",
        "text": (
            "Vas a ver varios fotogramas de un vídeo corto. Responde ÚNICAMENTE "
            "con una de estas tres palabras, sin nada más: REACTOR, MONTADO, u OK.\n\n"
            "Responde REACTOR si aparece una persona real e identificable, con su "
            "cara ocupando una parte significativa del encuadre, mientras reacciona "
            "o comenta en directo un contenido que se reproduce al lado, de fondo, "
            "o en una ventana/recuadro más pequeño incrustado. Cuenta tanto un "
            "recuadro pequeño de cámara web superpuesto sobre un vídeo más grande, "
            "como la cara de esa persona ocupando la mayor parte del encuadre con "
            "el vídeo de referencia en una ventana más pequeña al lado. NO cuenta "
            "si las personas que aparecen son simplemente parte de la acción "
            "grabada en el propio vídeo (gente en la calle, un incidente, etc.) "
            "sin que nadie esté mirando y reaccionando hacia la cámara en un "
            "plano/recuadro aparte.\n\n"
            "Si no aplica REACTOR, responde MONTADO si el vídeo ya viene editado a "
            "propósito: títulos o rótulos superpuestos, numeración de ranking/lista, "
            "marca de agua o logo de la cuenta que lo subió, u otros gráficos "
            "añadidos claramente en postproducción. No cuentan los subtítulos "
            "automáticos mínimos de la plataforma original.\n\n"
            "Si no aplica ninguno de los dos casos anteriores (metraje en bruto, "
            "sin nadie reaccionando y sin edición añadida evidente), responde OK.\n\n"
            "Responde solo con una palabra: REACTOR, MONTADO u OK."
        ),
    })

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=10,
        messages=[{"role": "user", "content": content}],
    )
    answer = "".join(block.text for block in response.content if block.type == "text").strip().upper()

    if "REACTOR" in answer:
        return "reactor"
    if "MONTADO" in answer:
        return "montado"
    return None


def generate_reaction_script(
    client, frame_paths: list, transcript: str, duration: float, title: str,
    style_context: str = "", avoid_texts: list | None = None,
) -> str:
    max_words = max(10, int(duration * WORDS_PER_SECOND))
    slang_examples = ", ".join(SLANG_BANK)

    extra_sections = ""
    if style_context:
        extra_sections += f"\n\n{style_context}"
    if avoid_texts:
        avoid_list = "\n".join(f'- "{t}"' for t in avoid_texts)
        extra_sections += (
            f"\n\nEl usuario ya rechazó estos intentos anteriores para ESTE MISMO vídeo — "
            f"escribe algo claramente distinto, no una variación mínima:\n{avoid_list}"
        )

    content = [image_to_base64_block(p) for p in frame_paths]
    content.append({
        "type": "text",
        "text": (
            f"Estas imágenes son fotogramas de un vídeo corto (título original: \"{title}\", "
            f"duración {duration:.1f} segundos) que se va a usar en una cuenta de TikTok de "
            f"reacciones en español, narrada por un personaje: un gato carismático, ingenioso "
            f"y coqueto.\n\n"
            f"Transcripción del audio original (puede estar en otro idioma, o vacía si no hay diálogo): "
            f"\"{transcript}\"\n\n"
            f"REGLAS DE FORMATO (obligatorias):\n"
            f"1. NUNCA escribas risas deletreadas como \"jajaja\", \"jeje\", \"lol\" o similares — "
            f"al convertirse a voz suenan robóticas y artificiales, no como una risa real. Si el "
            f"momento da risa, exprésalo con palabras y entonación (exclamaciones, comentarios "
            f"incrédulos) en vez de transcribir la risa.\n"
            f"2. No empieces el guion con saludos genéricos (\"Hola a todos\", \"Miau\", etc.) — "
            f"eso se añade aparte automáticamente. Empieza directo con la reacción al vídeo.\n\n"
            f"Escribe un guion de reacción en ESPAÑOL, en un tono juvenil y de internet — nada de "
            f"narrador neutro de manual. Puedes tirar de expresiones tipo (elige solo 2-4 por "
            f"guion, las que encajen mejor con el momento, sin forzarlas todas de golpe): "
            f"{slang_examples}. También puedes usar variantes/inventos con esa misma energía si "
            f"encajan mejor.\n\n"
            f"Reacciona como si lo estuvieras viendo por primera vez EN DIRECTO, con la reacción "
            f"genuina del momento (sorpresa, gracia, tensión) segundo a segundo según lo que va "
            f"pasando en las imágenes. NO expliques ni resumas el vídeo desde fuera como un "
            f"narrador (evita frases tipo \"esto es un vídeo de...\", \"aquí vemos cómo...\", "
            f"\"este es un ranking de...\"): eso rompe la inmersión. En su lugar, habla como si "
            f"estuvieras ahí mismo comentándolo con un amigo.\n\n"
            f"PERSONALIDAD del gato (esto es lo más importante del guion, no un simple narrador "
            f"neutro):\n"
            f"- Ingenioso: busca el comentario inteligente o el doble sentido antes que la "
            f"reacción obvia. Sorprende con la ocurrencia, no solo con la exclamación.\n"
            f"- Gracioso: usa el humor como eje del guion (ironía, exageración cómica, "
            f"comparaciones absurdas), pero sin forzar el chiste si el momento no lo pide.\n"
            f"- Coqueto: un puntito de chulería/seducción felina, confiado y juguetón, "
            f"como quien lo sabe todo y encima presume de ello con gracia — sin pasarse de "
            f"cursi ni resultar incómodo, y sin comentarios subidos de tono."
            f"{extra_sections}\n\n"
            f"Debe caber en aproximadamente {max_words} palabras "
            f"para que encaje con la duración del vídeo al narrarlo en voz alta. "
            f"Devuelve SOLO el texto del guion, sin comillas, sin explicaciones, sin acotaciones "
            f"entre paréntesis."
        ),
    })

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": content}],
    )

    return "".join(block.text for block in response.content if block.type == "text").strip()


def main():
    parser = argparse.ArgumentParser(description="Genera guiones de reacción en español con Claude")
    parser.add_argument("--limit", type=int, default=None, help="Máximo de vídeos a procesar en esta pasada")
    parser.add_argument("--only", type=str, default=None, help="Procesa solo el vídeo cuyo nombre de archivo empieza así (sin extensión)")
    parser.add_argument("--skip-filter", action="store_true", help="No comprobar si el clip trae reactor/está montado (para vídeos que ya has elegido tú a propósito)")
    parser.add_argument("--dry-run", action="store_true", help="Solo transcribe y muestra info, sin llamar a Claude")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key and not args.dry_run:
        sys.exit("Falta ANTHROPIC_API_KEY en el .env. Revisa .env.example.")

    if not INPUT_DIR.exists():
        sys.exit(f"No existe la carpeta {INPUT_DIR}. Ejecuta antes process_clips.py.")

    print("Cargando modelo de transcripción local (whisper)... (puede tardar la primera vez)")
    from faster_whisper import WhisperModel
    whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")

    client = None
    if not args.dry_run:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

    style_context = "" if args.dry_run else build_style_context(load_style_profile())

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log = load_log()
    processed_count = 0

    video_files = sorted(INPUT_DIR.glob("*.mp4"))
    if args.only:
        video_files = [p for p in video_files if p.stem == args.only]

    for filepath in video_files:
        if args.limit and processed_count >= args.limit:
            break

        key = filepath.name
        already_done = ("ok", "discarded_reactor", "discarded_montado")
        if key in log and log[key].get("status") in already_done and not args.only:
            continue

        print(f"\n{filepath.name}")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            duration = get_video_duration(filepath)

            print("  Extrayendo fotogramas...")
            frame_paths = extract_frames(filepath, tmp_dir, FRAMES_PER_VIDEO)

            print("  Transcribiendo audio...")
            audio_path = extract_audio(filepath, tmp_dir)
            transcript = transcribe_audio(audio_path, whisper_model)
            print(f"  Transcripción: \"{transcript[:80]}\"" if transcript else "  (sin diálogo detectado)")

            if args.dry_run:
                processed_count += 1
                continue

            if not args.skip_filter:
                print("  Comprobando si el clip sirve como metraje en bruto...")
                discard_reason = classify_source_suitability(client, frame_paths)
                if discard_reason == "reactor":
                    print("  [DESCARTADO] El clip ya trae a alguien reaccionando (facecam/streamer).")
                    log[key] = {"status": "discarded_reactor"}
                    continue
                if discard_reason == "montado":
                    print("  [DESCARTADO] El clip ya viene editado (títulos, ranking, marca de agua...).")
                    log[key] = {"status": "discarded_montado"}
                    continue

            print("  Generando guion con Claude...")
            script_text = generate_reaction_script(
                client, frame_paths, transcript, duration, filepath.stem,
                style_context=style_context,
            )

            print(f"  Guion: \"{script_text[:100]}...\"")

            output_path = OUTPUT_DIR / f"{filepath.stem}.json"
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "source_video": str(filepath),
                        "duration": duration,
                        "transcript_original": transcript,
                        "script_es": script_text,
                    },
                    f, ensure_ascii=False, indent=2,
                )

            log[key] = {"status": "ok", "output": str(output_path)}
            processed_count += 1

    save_log(log)
    print(f"\nHecho. {processed_count} vídeos {'analizados' if args.dry_run else 'con guion generado'} en esta pasada.")


if __name__ == "__main__":
    main()
