"""
compose_avatar.py — Paso 5 del pipeline ReactionFlow: animación del avatar +
composición final del vídeo.

Para cada narración generada en ./audio (por text_to_speech.py):
    1. Si generate_avatar_replicate.py ya generó un vídeo con IA en
       ./avatar_clips/<id>.mp4, se usa directamente (más realista y fluido).
       Si no, anima el avatar localmente por sprite-swap: analiza el volumen
       de la narración y elige para cada instante qué sprite del gato mostrar
       (boca cerrada/media/abierta, parpadeo, sorpresa en picos de volumen).
       En ambos casos el resultado conserva el fondo verde.
    2. Quita el fondo verde (chroma key) del avatar y lo superpone sobre el
       vídeo de fondo ya procesado en ./processed_clips.
    3. Genera subtítulos quemados, sincronizados con el audio real:
        - Si tienes faster-whisper instalado, transcribe la narración y
          alinea las palabras reconocidas con el texto exacto del guion
          (./scripts/<id>.json) para tener subtítulos con la ORTOGRAFÍA
          correcta pero el TIMING real del audio.
        - Si no lo tienes instalado, reparte el texto del guion de forma
          proporcional a la duración del audio (menos preciso, pero
          funciona sin dependencias extra).
    4. Mezcla el audio original del clip (bajito, de ambiente) con la
       narración, y renderiza el .mp4 final en ./final.

Requisitos:
    - ffmpeg / ffprobe con soporte de libass y colorkey (los builds
      estándar de ffmpeg para Windows/Mac/Linux los traen).
    - pip install numpy
    - pip install faster-whisper   (opcional pero MUY recomendado, ver arriba)

Imágenes del avatar necesarias en ./assets/avatar/ (fondo verde uniforme,
todas con el gato en el mismo encuadre/tamaño para que el "sprite-swap" no
salte de posición):
    gato_boca_cerrada.png   <- la imagen base (reposo)
    gato_boca_media.png
    gato_boca_abierta.png
    gato_ojos_cerrados.png  <- parpadeo (boca cerrada + ojos cerrados)
    gato_sorpresa.png       <- pico de volumen repentino
    gato_perfil.png         <- no se usa automáticamente en esta v1, queda
                               reservada por si más adelante se añaden
                               cambios de plano manuales.

Uso:
    python compose_avatar.py
    python compose_avatar.py --limit 1 --keep-temp
    python compose_avatar.py --only 5Jy7uRcOpl4_9x16
"""

import argparse
import json
import math
import random
import subprocess
import sys
import tempfile
import unicodedata
import wave
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

# La consola de Windows por defecto usa cp1252, que no soporta emojis ni
# muchos caracteres. Sin esto, algún carácter raro en un guion o título
# revienta el script a mitad de proceso.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ----------------------------------------------------------------------------
# CONFIGURACIÓN
# ----------------------------------------------------------------------------

VIDEO_DIR = Path("./processed_clips")
AUDIO_DIR = Path("./audio")
SCRIPT_DIR = Path("./scripts")
OUTPUT_DIR = Path("./final")
LOG_FILE = Path("./composed.json")
AVATAR_DIR = Path("./assets/avatar")

# Si generate_avatar_replicate.py ya dejó aquí un vídeo con el nombre del
# audio (p.ej. avatar_clips/5Jy7uRcOpl4_9x16.mp4), se usa directamente en vez
# de animar por sprite-swap: la IA anima con más realismo y fluidez, y sigue
# trayendo el fondo verde para el mismo chroma key de siempre. Si no existe,
# se cae automáticamente al sprite-swap local de toda la vida.
AI_AVATAR_DIR = Path("./avatar_clips")

SPRITE_BOCA_CERRADA = "gato_boca_cerrada.png"
SPRITE_BOCA_MEDIA = "gato_boca_media.png"
SPRITE_BOCA_ABIERTA = "gato_boca_abierta.png"
SPRITE_OJOS_CERRADOS = "gato_ojos_cerrados.png"
SPRITE_SORPRESA = "gato_sorpresa.png"
# SPRITE_PERFIL no se usa en la animación automática de esta v1.

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
FPS = 30

# Tamaño y posición del avatar sobre el vídeo final.
AVATAR_WIDTH = 620  # px, sobre un lienzo de 1080 de ancho
AVATAR_MARGIN = 24  # px de margen respecto al borde
# "top-right" | "top-left" | "top-center" | "bottom-right" | "bottom-left" | "bottom-center"
AVATAR_POSITION = "top-right"

# Suavizado de la animación del avatar: mezcla cada fotograma con los
# anteriores para que el cambio entre sprites no sea un corte seco. Más
# SMOOTH_FRAMES = transición más larga/suave (pero también más "fantasma"
# residual del sprite anterior). Los pesos dan más importancia al fotograma
# más reciente para que no se quede "flotando" la imagen vieja.
SMOOTH_FRAMES = 5
SMOOTH_WEIGHTS = "1 2 3 4 5"

# Chroma key (fondo verde -> transparente)
CHROMA_COLOR = "0x00FF00"
CHROMA_SIMILARITY = 0.20
CHROMA_BLEND = 0.08
USE_DESPILL = True  # quita el "tinte verde" que queda en el borde del pelaje

# Umbrales de volumen (normalizados 0..1 sobre el propio pico de cada narración)
# para decidir qué sprite de boca usar en cada instante.
VOL_LOW_THRESHOLD = 0.16   # por debajo de esto -> boca cerrada
VOL_HIGH_THRESHOLD = 0.42  # por encima de esto -> boca abierta (si no, boca media)

# "Sorpresa": un pico de volumen repentino tras un momento más flojo.
SPIKE_LEVEL = 0.72         # el volumen normalizado debe superar esto...
SPIKE_RISE = 0.30          # ...y subir al menos esto respecto al frame anterior
SPIKE_MIN_GAP = 1.2        # segundos mínimos entre dos "sorpresas" para que no se abuse del gesto

# Parpadeo periódico (independiente del volumen)
BLINK_MIN_INTERVAL = 2.5
BLINK_MAX_INTERVAL = 5.5
BLINK_DURATION = 0.12

# Mezcla de audio
NARRATION_VOLUME = 1.0
ORIGINAL_CLIP_VOLUME = 0.12  # audio original del vídeo de fondo, bajo, de ambiente

# Subtítulos
SUB_FONT_NAME = "Arial"
SUB_FONT_SIZE = 66           # en unidades de PlayRes (=~ píxeles a 1080x1920)
SUB_MAX_WORDS_PER_CUE = 5
SUB_MAX_CHARS_PER_CUE = 30
# separación desde abajo, en píxeles de PlayRes. Con el avatar arriba, los
# subtítulos van abajo del todo (estilo TikTok clásico) sin que se solapen.
# Si cambias AVATAR_POSITION a una de las de "bottom-*", súbela para que no
# quede tapada por el avatar (algo como AVATAR_WIDTH + AVATAR_MARGIN + 40).
SUB_MARGIN_V = 140
WHISPER_MODEL_SIZE = "small"

VIDEO_EXTENSIONS = {".mp4"}

# Crédito del canal original de YouTube, exigido por la licencia CC-BY (ver
# youtube_downloader.py). Se muestra solo los primeros segundos.
SHOW_ATTRIBUTION = True
ATTRIBUTION_FONT_SIZE = 34
ATTRIBUTION_DURATION_S = 4.0
# El filtro "drawtext" (a diferencia de "subtitles") no usa el sistema de
# fuentes de Windows y necesita una ruta explícita a un archivo .ttf, o el
# ffmpeg de gyan.dev revienta buscando un fontconfig.conf que no existe.
ATTRIBUTION_FONT_FILE = "C:/Windows/Fonts/arial.ttf"
DOWNLOAD_LOG_FILE = Path("./downloaded.json")
VIDEO_ID_SUFFIX = "_9x16"

# ----------------------------------------------------------------------------


def load_log() -> dict:
    if LOG_FILE.exists():
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_log(log: dict) -> None:
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def check_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        try:
            subprocess.run([tool, "-version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            sys.exit(f"No se encuentra '{tool}'. Instala ffmpeg y añádelo al PATH.")

    missing_sprites = [
        name for name in (
            SPRITE_BOCA_CERRADA, SPRITE_BOCA_MEDIA, SPRITE_BOCA_ABIERTA,
            SPRITE_OJOS_CERRADOS, SPRITE_SORPRESA,
        )
        if not (AVATAR_DIR / name).exists()
    ]
    if missing_sprites:
        if AI_AVATAR_DIR.exists() and any(AI_AVATAR_DIR.glob("*.mp4")):
            # No pasa nada: si ya usas generate_avatar_replicate.py para todos
            # los clips, el sprite-swap local no hace falta.
            print(f"  [AVISO] Faltan sprites en {AVATAR_DIR} ({', '.join(missing_sprites)}), "
                  "pero hay avatares de IA en avatar_clips/ — se usarán esos.")
        else:
            sys.exit(
                f"Faltan sprites del avatar en {AVATAR_DIR}: {', '.join(missing_sprites)}. "
                "Genera/coloca esas imágenes, o genera los avatares con "
                "generate_avatar_replicate.py antes de ejecutar este script."
            )


def get_channel_credit(stem: str) -> str | None:
    """Busca el canal original de YouTube en downloaded.json para dar crédito CC-BY."""
    if not DOWNLOAD_LOG_FILE.exists():
        return None

    with open(DOWNLOAD_LOG_FILE, "r", encoding="utf-8") as f:
        downloaded = json.load(f)

    base_id = stem[: -len(VIDEO_ID_SUFFIX)] if stem.endswith(VIDEO_ID_SUFFIX) else stem
    entry = downloaded.get(base_id)
    return entry.get("channel") if entry else None


def escape_drawtext(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "’")  # apóstrofo tipográfico, evita romper el filtro
        .replace("%", "\\%")
    )


def get_duration(filepath: Path) -> float:
    result = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(filepath),
    ])
    return float(json.loads(result.stdout)["format"]["duration"])


# ----------------------------------------------------------------------------
# 1) Envolvente de volumen de la narración -> qué sprite usar en cada frame
# ----------------------------------------------------------------------------

def decode_to_mono_wav(audio_path: Path, tmp_dir: Path, sample_rate: int = 22050) -> Path:
    wav_path = tmp_dir / "narration_mono.wav"
    result = run([
        "ffmpeg", "-y", "-i", str(audio_path),
        "-ac", "1", "-ar", str(sample_rate), str(wav_path),
    ])
    if result.returncode != 0:
        sys.exit(f"[ERROR ffmpeg] No se pudo decodificar el audio: {result.stderr[-400:]}")
    return wav_path


def compute_volume_envelope(wav_path: Path, fps: int) -> np.ndarray:
    """Devuelve un array con el volumen RMS normalizado (0..1) de la narración,
    con un valor por cada frame de vídeo (a `fps` fotogramas por segundo)."""
    with wave.open(str(wav_path), "rb") as wf:
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return np.zeros(1, dtype=np.float32)

    window = max(1, int(sample_rate / fps))
    n_windows = math.ceil(samples.size / window)
    padded = np.pad(samples, (0, n_windows * window - samples.size))
    windows = padded.reshape(n_windows, window)
    rms = np.sqrt(np.mean(windows ** 2, axis=1))

    peak = np.percentile(rms, 95) or 1.0
    normalized = np.clip(rms / peak, 0.0, 1.0)
    return normalized


@dataclass
class FrameChoice:
    sprite: str


def build_sprite_timeline(envelope: np.ndarray, fps: int) -> list:
    """Convierte la envolvente de volumen en una lista de sprites, uno por
    frame, aplicando los umbrales de boca + parpadeos periódicos + sorpresas
    en picos repentinos de volumen."""
    n = len(envelope)
    timeline = [SPRITE_BOCA_CERRADA] * n

    last_spike_frame = -10 ** 9
    for i, level in enumerate(envelope):
        prev_level = envelope[i - 1] if i > 0 else 0.0

        is_spike = (
            level >= SPIKE_LEVEL
            and (level - prev_level) >= SPIKE_RISE
            and (i - last_spike_frame) / fps >= SPIKE_MIN_GAP
        )

        if is_spike:
            timeline[i] = SPRITE_SORPRESA
            last_spike_frame = i
        elif level >= VOL_HIGH_THRESHOLD:
            timeline[i] = SPRITE_BOCA_ABIERTA
        elif level >= VOL_LOW_THRESHOLD:
            timeline[i] = SPRITE_BOCA_MEDIA
        else:
            timeline[i] = SPRITE_BOCA_CERRADA

    # Parpadeos periódicos: se superponen sobre lo anterior, salvo en medio
    # de una "sorpresa" (para no pisar el gesto más importante).
    duration = n / fps
    t = random.uniform(BLINK_MIN_INTERVAL, BLINK_MAX_INTERVAL)
    while t < duration:
        start_frame = int(t * fps)
        end_frame = int((t + BLINK_DURATION) * fps)
        for i in range(start_frame, min(end_frame, n)):
            if timeline[i] != SPRITE_SORPRESA:
                timeline[i] = SPRITE_OJOS_CERRADOS
        t += random.uniform(BLINK_MIN_INTERVAL, BLINK_MAX_INTERVAL)

    return timeline


def write_concat_list(timeline: list, fps: int, tmp_dir: Path) -> Path:
    """Agrupa frames consecutivos con el mismo sprite en segmentos, y escribe
    un fichero de lista para el demuxer `concat` de ffmpeg (formato
    imagen + duración). El último fichero se repite sin duración, truco
    necesario para que el demuxer `concat` respete la duración total."""
    segments = []  # (sprite_filename, n_frames)
    for sprite in timeline:
        if segments and segments[-1][0] == sprite:
            segments[-1] = (sprite, segments[-1][1] + 1)
        else:
            segments.append((sprite, 1))

    list_path = tmp_dir / "avatar_concat.txt"
    avatar_dir_abs = AVATAR_DIR.resolve()
    with open(list_path, "w", encoding="utf-8") as f:
        for sprite, n_frames in segments:
            duration = n_frames / fps
            sprite_path = (avatar_dir_abs / sprite).as_posix()
            f.write(f"file '{sprite_path}'\n")
            f.write(f"duration {duration:.6f}\n")
        # repetir el último fichero, sin duration (requisito del demuxer concat)
        last_sprite_path = (avatar_dir_abs / segments[-1][0]).as_posix()
        f.write(f"file '{last_sprite_path}'\n")

    return list_path


def render_avatar_clip(concat_list: Path, fps: int, output_path: Path) -> None:
    # El demuxer concat produce cortes secos entre sprites (una imagen fija
    # cambia a otra de golpe). "tmix" mezcla cada fotograma con los N-1
    # anteriores: en un tramo sostenido (misma imagen repetida) no cambia nada,
    # pero justo después de un cambio de sprite crea una transición suave tipo
    # crossfade durante SMOOTH_FRAMES fotogramas, sin necesitar más imágenes.
    smooth_filter = f"fps={fps},tmix=frames={SMOOTH_FRAMES}:weights={SMOOTH_WEIGHTS}"
    result = run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-vf", smooth_filter,
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
        str(output_path),
    ])
    if result.returncode != 0:
        sys.exit(f"[ERROR ffmpeg] No se pudo generar el vídeo del avatar: {result.stderr[-500:]}")


# ----------------------------------------------------------------------------
# 2) Subtítulos: alinear el texto real del guion con el timing del audio
# ----------------------------------------------------------------------------

def normalize_word(word: str) -> str:
    word = word.strip().lower()
    word = "".join(c for c in unicodedata.normalize("NFD", word) if unicodedata.category(c) != "Mn")
    return "".join(c for c in word if c.isalnum())


def transcribe_words_whisper(audio_path: Path):
    """Devuelve [(word, start, end), ...] transcribiendo el audio real con
    faster-whisper (con timestamps por palabra). None si no está instalado."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None

    model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(audio_path), word_timestamps=True, language="es")

    words = []
    for segment in segments:
        for w in (segment.words or []):
            words.append((w.word, w.start, w.end))
    return words


def align_script_to_timestamps(script_words: list, whisper_words: list) -> list:
    """Alinea las palabras EXACTAS del guion (ortografía correcta, es lo que
    de verdad se envió a ElevenLabs) con los timestamps reales que devolvió
    whisper al transcribir el audio. Así los subtítulos usan el texto bueno
    pero el tiempo real (evita que la introducción del "Miau" cuele texto
    donde no toca, y evita errores de transcripción de whisper)."""
    norm_script = [normalize_word(w) for w in script_words]
    norm_whisper = [normalize_word(w[0]) for w in whisper_words]

    matcher = SequenceMatcher(a=norm_whisper, b=norm_script, autojunk=False)
    aligned = [None] * len(script_words)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag not in ("equal", "replace"):
            continue
        length = min(i2 - i1, j2 - j1)
        for k in range(length):
            _, start, end = whisper_words[i1 + k]
            aligned[j1 + k] = (start, end)

    # Para las palabras del guion que no se pudieron alinear (huecos),
    # interpolamos entre los vecinos alineados más cercanos.
    known_indices = [i for i, v in enumerate(aligned) if v is not None]
    if not known_indices:
        return []

    for i in range(len(aligned)):
        if aligned[i] is not None:
            continue
        prev_idx = max([k for k in known_indices if k < i], default=None)
        next_idx = min([k for k in known_indices if k > i], default=None)
        if prev_idx is None and next_idx is None:
            continue
        if prev_idx is None:
            aligned[i] = aligned[next_idx]
        elif next_idx is None:
            aligned[i] = aligned[prev_idx]
        else:
            t0, t1 = aligned[prev_idx][1], aligned[next_idx][0]
            span = (t1 - t0) / (next_idx - prev_idx)
            aligned[i] = (t0 + span * (i - prev_idx - 1), t0 + span * (i - prev_idx))

    return [(script_words[i], *aligned[i]) for i in range(len(aligned)) if aligned[i] is not None]


def proportional_fallback_timing(script_words: list, total_duration: float, lead_in: float = 0.0) -> list:
    """Si no hay faster-whisper instalado: reparte el tiempo disponible entre
    las palabras del guion proporcionalmente a su longitud en caracteres.
    Menos preciso que whisper, pero no depende de nada más."""
    available = max(0.1, total_duration - lead_in)
    weights = [max(1, len(w)) for w in script_words]
    total_weight = sum(weights)

    result = []
    t = lead_in
    for word, weight in zip(script_words, weights):
        dur = available * (weight / total_weight)
        result.append((word, t, t + dur))
        t += dur
    return result


def group_into_cues(word_timings: list) -> list:
    """Agrupa palabras con timestamp en cues de subtítulo cortas (por número
    de palabras y de caracteres), sin cortar por la mitad si hay un salto de
    silencio grande entre dos palabras."""
    cues = []
    current_words = []
    current_start = None
    last_end = None

    for word, start, end in word_timings:
        gap = (start - last_end) if last_end is not None else 0
        would_be_text = " ".join(current_words + [word])

        should_flush = current_words and (
            len(current_words) >= SUB_MAX_WORDS_PER_CUE
            or len(would_be_text) > SUB_MAX_CHARS_PER_CUE
            or gap > 0.6
        )

        if should_flush:
            cues.append((current_start, last_end, " ".join(current_words)))
            current_words = []
            current_start = None

        if current_start is None:
            current_start = start
        current_words.append(word)
        last_end = end

    if current_words:
        cues.append((current_start, last_end, " ".join(current_words)))

    return cues


def format_ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def write_ass_file(cues: list, path: Path) -> None:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUTPUT_WIDTH}
PlayResY: {OUTPUT_HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{SUB_FONT_NAME},{SUB_FONT_SIZE},&H00FFFFFF,&H000000FF,&H00101010,&H00000000,1,0,0,0,100,100,0,0,1,5,0,2,60,60,{SUB_MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        for start, end, text in cues:
            escaped = text.replace("\n", "\\N")
            f.write(
                f"Dialogue: 0,{format_ass_timestamp(start)},{format_ass_timestamp(end)},"
                f"Default,,0,0,0,,{escaped.upper()}\n"
            )


def build_subtitles(audio_path: Path, script_data: dict, total_duration: float, tmp_dir: Path) -> Path:
    script_text = script_data["script_es"]
    script_words = script_text.split()

    whisper_words = transcribe_words_whisper(audio_path)

    if whisper_words:
        print("  Subtítulos: usando faster-whisper para el timing real del audio.")
        word_timings = align_script_to_timestamps(script_words, whisper_words)
    else:
        print("  [AVISO] faster-whisper no está instalado — subtítulos con timing "
              "aproximado (proporcional a la longitud del texto). "
              "Instálalo con: pip install faster-whisper")
        # dejamos ~1s de margen al principio por si hay un "Miau" de intro
        word_timings = proportional_fallback_timing(script_words, total_duration, lead_in=1.0)

    cues = group_into_cues(word_timings)
    ass_path = tmp_dir / "subs.ass"
    write_ass_file(cues, ass_path)
    return ass_path


# ----------------------------------------------------------------------------
# 3) Composición final con ffmpeg
# ----------------------------------------------------------------------------

def avatar_overlay_position() -> str:
    top = AVATAR_POSITION.startswith("top")
    y = str(AVATAR_MARGIN) if top else f"H-h-{AVATAR_MARGIN}"

    if AVATAR_POSITION in ("top-left", "bottom-left"):
        x = str(AVATAR_MARGIN)
    elif AVATAR_POSITION in ("top-center", "bottom-center"):
        x = "(W-w)/2"
    else:  # top-right / bottom-right
        x = f"W-w-{AVATAR_MARGIN}"

    return f"{x}:{y}"


def escape_for_filter(path: Path) -> str:
    """Escapa una ruta para poder meterla dentro de -filter_complex
    (los ':' y '\\' de Windows dan problemas dentro del filtro subtitles=...)."""
    p = str(path).replace("\\", "/")
    p = p.replace(":", "\\:")
    return p


def compose_final_video(background_video: Path, avatar_clip: Path, narration_audio: Path,
                         ass_subs: Path, narration_duration: float, output_path: Path,
                         channel: str | None = None) -> None:
    chroma_filter = f"colorkey={CHROMA_COLOR}:{CHROMA_SIMILARITY}:{CHROMA_BLEND}"
    if USE_DESPILL:
        chroma_filter += ",despill=type=green:mix=0.5:expand=0"

    video_chain = (
        f"[0:v]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1[bg];"
        f"[1:v]{chroma_filter},scale={AVATAR_WIDTH}:-2[avatar];"
        f"[bg][avatar]overlay={avatar_overlay_position()}:shortest=0[comp];"
        f"[comp]subtitles='{escape_for_filter(ass_subs)}',fps={FPS}[withsubs]"
    )

    font_file_path = Path(ATTRIBUTION_FONT_FILE)
    if SHOW_ATTRIBUTION and channel and font_file_path.exists():
        credit_text = escape_drawtext(f"Video original: {channel}")
        font_arg = escape_for_filter(font_file_path)
        # Alineado a la izquierda (no centrado): con el avatar en una esquina
        # superior, un texto centrado arriba chocaría con él.
        credit_x = f"W-w-{AVATAR_MARGIN}" if AVATAR_POSITION == "top-left" else str(AVATAR_MARGIN)
        video_chain += (
            f";[withsubs]drawtext=text='{credit_text}':fontfile='{font_arg}':"
            f"fontsize={ATTRIBUTION_FONT_SIZE}:fontcolor=white@0.9:box=1:boxcolor=black@0.45:"
            f"boxborderw=10:x={credit_x}:y=28:enable='lt(t,{ATTRIBUTION_DURATION_S})'[vout]"
        )
    else:
        if SHOW_ATTRIBUTION and channel:
            print(f"  [AVISO] No se encontró la fuente {ATTRIBUTION_FONT_FILE}, se omite el crédito en pantalla.")
        video_chain += ";[withsubs]null[vout]"

    filter_complex = (
        f"{video_chain};"
        f"[0:a]volume={ORIGINAL_CLIP_VOLUME}[origaud];"
        f"[2:a]volume={NARRATION_VOLUME}[narraud];"
        f"[origaud][narraud]amix=inputs=2:duration=first:dropout_transition=0[aout]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(background_video),
        "-i", str(avatar_clip),
        "-i", str(narration_audio),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-t", f"{narration_duration:.3f}",
        "-c:v", "libx264", "-crf", "20", "-preset", "medium",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    result = run(cmd)
    if result.returncode != 0:
        sys.exit(f"[ERROR ffmpeg] No se pudo componer el vídeo final: {result.stderr[-1500:]}")


# ----------------------------------------------------------------------------
# Orquestación
# ----------------------------------------------------------------------------

def process_one(stem: str, keep_temp: bool) -> None:
    video_path = VIDEO_DIR / f"{stem}.mp4"
    audio_path = AUDIO_DIR / f"{stem}.mp3"
    script_path = SCRIPT_DIR / f"{stem}.json"
    output_path = OUTPUT_DIR / f"{stem}.mp4"

    for p in (video_path, audio_path, script_path):
        if not p.exists():
            print(f"  [SALTADO] Falta {p}")
            return

    with open(script_path, "r", encoding="utf-8") as f:
        script_data = json.load(f)

    narration_duration = get_duration(audio_path)
    print(f"  Narración: {narration_duration:.2f}s")

    ai_avatar_path = AI_AVATAR_DIR / f"{stem}.mp4"

    tmp_ctx = tempfile.TemporaryDirectory(dir=".", prefix=f"_tmp_{stem}_")
    tmp_dir = Path(tmp_ctx.name)
    try:
        if ai_avatar_path.exists():
            print(f"  Usando avatar generado con IA: {ai_avatar_path}")
            avatar_clip = ai_avatar_path
        else:
            print("  Analizando volumen de la narración...")
            wav_path = decode_to_mono_wav(audio_path, tmp_dir)
            envelope = compute_volume_envelope(wav_path, FPS)

            print("  Construyendo animación del avatar (sprite-swap)...")
            timeline = build_sprite_timeline(envelope, FPS)
            concat_list = write_concat_list(timeline, FPS, tmp_dir)
            avatar_clip = tmp_dir / "avatar_raw.mp4"
            render_avatar_clip(concat_list, FPS, avatar_clip)

        print("  Generando subtítulos...")
        ass_subs = build_subtitles(audio_path, script_data, narration_duration, tmp_dir)

        channel = get_channel_credit(stem)
        if SHOW_ATTRIBUTION and not channel:
            print("  [AVISO] No se encontró el canal original en downloaded.json, no se mostrará crédito.")

        print("  Componiendo vídeo final (chroma key + overlay + subtítulos + audio)...")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        compose_final_video(video_path, avatar_clip, audio_path, ass_subs, narration_duration, output_path, channel)

        print(f"  Guardado en {output_path}")
    finally:
        if keep_temp:
            print(f"  (temporales conservados en {tmp_dir})")
        else:
            tmp_ctx.cleanup()


def main():
    parser = argparse.ArgumentParser(description="Anima el avatar y compone el vídeo final de ReactionFlow")
    parser.add_argument("--limit", type=int, default=None, help="Máximo de vídeos a procesar en esta pasada")
    parser.add_argument("--only", type=str, default=None, help="Procesa solo el id/stem indicado (ej. 5Jy7uRcOpl4_9x16)")
    parser.add_argument("--keep-temp", action="store_true", help="No borra los ficheros temporales (útil para depurar)")
    args = parser.parse_args()

    check_tools()

    if not AUDIO_DIR.exists():
        sys.exit(f"No existe la carpeta {AUDIO_DIR}. Ejecuta antes text_to_speech.py.")

    log = load_log()
    processed_count = 0

    audio_files = sorted(AUDIO_DIR.glob("*.mp3"))
    if args.only:
        audio_files = [p for p in audio_files if p.stem == args.only]

    if not audio_files:
        print(f"No hay narraciones en {AUDIO_DIR}.")
        return

    for audio_path in audio_files:
        if args.limit and processed_count >= args.limit:
            break

        stem = audio_path.stem
        key = audio_path.name
        if key in log and log[key].get("status") == "ok" and not args.only:
            continue

        print(f"\n{stem}")
        try:
            process_one(stem, args.keep_temp)
            log[key] = {"status": "ok", "output": str(OUTPUT_DIR / f'{stem}.mp4')}
            processed_count += 1
        except SystemExit as e:
            print(f"  [ERROR] {e}")
            log[key] = {"status": "error", "error": str(e)}

    save_log(log)
    print(f"\nHecho. {processed_count} vídeos compuestos en esta pasada.")


if __name__ == "__main__":
    main()
