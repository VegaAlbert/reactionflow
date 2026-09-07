"""
menu.py — Programa de escritorio (menú en terminal) de ReactionFlow.

Un único punto de entrada para generar vídeos de reacción del gato, en vez
de ir ejecutando cada script del pipeline a mano. No duplica nada: para los
pasos que se ejecutan una vez por vídeo invoca los scripts ya existentes
como subprocesos (mismo Python con el que arranques este menú); para el
guion (que hay que poder rechazar y regenerar varias veces sin repetir
transcripción) importa generate_script.py como módulo y reutiliza sus
funciones directamente.

Uso (desde la carpeta scripts/youtube):
    python menu.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
if sys.stdin.encoding and sys.stdin.encoding.lower() != "utf-8":
    # Sin esto, escribir "sí"/"guión"/etc. con input() en Windows guarda
    # caracteres mal decodificados (p.ej. "sí" queda como "sÃ­").
    sys.stdin.reconfigure(encoding="utf-8")

import generate_script

PYTHON = sys.executable

RAW_CLIPS_DIR = Path("./raw_clips")
PROCESSED_CLIPS_DIR = Path("./processed_clips")
SCRIPTS_DIR = Path("./scripts")
AVATAR_CLIPS_DIR = Path("./avatar_clips")
FINAL_DIR = Path("./final")
AUDIO_DIR = Path("./audio")
IMAGES_DIR = Path("./imagen_video")
DOWNLOADED_LOG = Path("./downloaded.json")
SCRIPTED_LOG = Path("./scripted.json")
AVATAR_REPLICATE_LOG = Path("./avatar_replicate.json")
COMPOSED_LOG = Path("./composed.json")
IMAGES_COMPOSED_LOG = Path("./images_composed.json")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".avif"}

REPLICATE_COST_PER_SECOND = 0.02  # wan-video/wan-2.2-s2v

# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------


def run_step(script_name: str, *args: str) -> bool:
    """Ejecuta uno de los scripts del pipeline como subproceso, mostrando su
    salida en directo (misma consola). Devuelve True si terminó sin error."""
    cmd = [PYTHON, script_name, *args]
    result = subprocess.run(cmd)
    return result.returncode == 0


YES_ANSWERS = ("s", "si", "sí", "y", "yes")
NO_ANSWERS = ("n", "no")


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    """Pregunta sí/no. Vacío (Enter) usa el valor por defecto; cualquier otra
    respuesta que no sea un sí/no reconocido vuelve a preguntar, en vez de
    asumir silenciosamente lo que el usuario quiso decir."""
    while True:
        ans = input(prompt).strip().lower()
        if not ans:
            return default
        if ans in YES_ANSWERS:
            return True
        if ans in NO_ANSWERS:
            return False
        print(f"  No te he entendido (\"{ans}\"). Responde s o n"
              f"{' (Enter = sí)' if default else ' (Enter = no)'}.")


def ask_choice(prompt: str, options: dict, default: str | None = None) -> str:
    """Pregunta de opción múltiple: 'options' mapea cada respuesta válida (ya
    en minúsculas) al valor que se devuelve. Vacío (Enter) devuelve
    options[default] si se dio 'default'; cualquier respuesta que no esté en
    'options' vuelve a preguntar en vez de caer en un valor por defecto sin
    que el usuario se entere."""
    while True:
        ans = input(prompt).strip().lower()
        if not ans and default is not None:
            return options[default]
        if ans in options:
            return options[ans]
        valid = "/".join(options.keys())
        hint = f" (o Enter = {default})" if default is not None else ""
        print(f"  No te he entendido (\"{ans}\"). Elige una de estas opciones: {valid}{hint}.")


def pick_from_list(items: list, prompt: str):
    if not items:
        print("  (no hay ninguno)")
        return None
    for i, item in enumerate(items, start=1):
        print(f"  {i}. {item.name}")
    while True:
        choice = input(prompt).strip()
        if not choice:
            return None  # Enter vacío = cancelar, siempre válido
        if choice.isdigit() and 1 <= int(choice) <= len(items):
            return items[int(choice) - 1]
        print(f"  No te he entendido (\"{choice}\"). Escribe un número del 1 al {len(items)}, "
              "o Enter para cancelar.")


def list_final_videos() -> list:
    if not FINAL_DIR.exists():
        return []
    return sorted(FINAL_DIR.glob("*.mp4"))


def _load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_log_entry(log_path: Path, key: str) -> None:
    """Borra una entrada de un log de progreso, para forzar que el paso
    correspondiente se repita la próxima vez que se ejecute ese script."""
    log = _load_json(log_path)
    if key in log:
        del log[key]
        _save_json(log_path, log)


def natural_sort_key(path: Path):
    """Ordena '2.jpg' antes que '10.jpg' (a diferencia del orden alfabético
    puro), para que las imágenes numeradas se muestren en el orden esperado."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def get_media_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def find_pending_stems() -> list:
    """Vídeos con guion generado que todavía no tienen vídeo final."""
    log = _load_json(SCRIPTED_LOG)
    stems = []
    for filename, entry in log.items():
        if entry.get("status") != "ok":
            continue
        stem = Path(filename).stem
        if not (FINAL_DIR / f"{stem}.mp4").exists():
            stems.append(stem)
    return sorted(stems)


VLC_CANDIDATES = [
    Path(r"C:\Program Files\VideoLAN\VLC\vlc.exe"),
    Path(r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"),
]
MEDIA_EXTENSIONS = {".mp4", ".mp3", ".wav", ".webm", ".mkv", ".mov", ".m4a"}


def _find_vlc() -> str | None:
    found = shutil.which("vlc")
    if found:
        return found
    for candidate in VLC_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    return None


VLC_PATH = _find_vlc()


def open_file(path: Path) -> None:
    """Abre vídeos/audios con VLC si está instalado; cualquier otro tipo de
    archivo (o si no se encuentra VLC) usa el programa por defecto de Windows."""
    if VLC_PATH and path.suffix.lower() in MEDIA_EXTENSIONS:
        try:
            subprocess.Popen([VLC_PATH, str(path.resolve())])
            return
        except Exception:
            pass
    try:
        os.startfile(path)  # Windows
    except Exception:
        print(f"  Ábrelo manualmente: {path.resolve()}")


# ----------------------------------------------------------------------------
# Revisión y regeneración interactiva del guion
# ----------------------------------------------------------------------------


def write_own_script(stem: str, video_path: Path, duration: float):
    """Deja que el usuario escriba su propio guion, ayudándole a que la
    longitud encaje con la duración real del vídeo (mismo ritmo de habla que
    usa generate_script.py: WORDS_PER_SECOND). Devuelve el texto final, o
    None si el usuario no escribió nada (para volver al menú anterior)."""
    target_words = max(10, round(duration * generate_script.WORDS_PER_SECOND))
    print(f"\n  El vídeo dura {duration:.1f}s. A un ritmo de habla natural "
          f"(~{generate_script.WORDS_PER_SECOND} palabras/segundo), un guion de "
          f"~{target_words} palabras encajará bien con la duración.")

    if ask_yes_no("  ¿Abro el vídeo para que lo veas mientras escribes? [S/n]: ", default=True):
        open_file(video_path)

    while True:
        custom_text = input("\n  Escribe tú el guion (lo que dirá y aparecerá en subtítulos):\n  > ").strip()
        if not custom_text:
            print("  (vacío, no se ha cambiado nada)")
            return None

        word_count = len(custom_text.split())
        est_seconds = word_count / generate_script.WORDS_PER_SECOND
        diff = est_seconds - duration
        print(f"\n  Tu guion tiene {word_count} palabras -> ~{est_seconds:.1f}s narrado "
              f"(el vídeo dura {duration:.1f}s).")

        # Margen de tolerancia: al menos 3s, o el 25% de la duración del vídeo,
        # lo que sea mayor (para vídeos cortos no tiene sentido exigir un
        # ajuste al segundo).
        tolerance = max(3.0, duration * 0.25)
        if abs(diff) <= tolerance:
            print("  Encaja bien con la duración del vídeo.")
            return custom_text

        if diff > 0:
            print(f"  Aviso: se pasa unos {diff:.1f}s de la duración del vídeo — "
                  f"la narración podría quedar cortada o acelerada.")
        else:
            print(f"  Aviso: se queda unos {abs(diff):.1f}s corto — "
                  f"puede que sobre tiempo de vídeo sin narración.")

        retry = input("  [r] reescribirlo / [Enter] dejarlo así de todas formas: ").strip().lower()
        if retry != "r":
            return custom_text
        # si elige "r", vuelve a preguntar desde el principio del bucle


PREVIEW_AUDIO_PATH = Path("./_preview_audio.mp3")


def preview_and_confirm_audio(script_text: str, duration: float) -> str:
    """Genera un audio de prueba (sin el 'miau' inicial, solo para comprobar
    cómo suena de verdad la pronunciación) y deja escuchar y corregir el
    texto tantas veces como haga falta antes de dar el guion por bueno.
    Esto es lo que detecta cosas como números romanos mal leídos o
    anglicismos que el sintetizador pronuncia distinto a como se escriben."""
    import text_to_speech

    try:
        client, voice_id = text_to_speech.get_elevenlabs_client()
    except SystemExit as e:
        print(f"\n  [AVISO] No se pudo preparar la vista previa de audio ({e}).")
        print("  Se sigue con el guion tal cual, sin comprobar la pronunciación.")
        return script_text

    while True:
        print("\n  Generando un audio de prueba para comprobar cómo suena...")
        ok = text_to_speech.generate_speech(client, voice_id, script_text, PREVIEW_AUDIO_PATH)
        if not ok:
            print("  No se pudo generar el audio de prueba. Se sigue con el texto tal cual.")
            return script_text

        open_file(PREVIEW_AUDIO_PATH)
        options = {
            "s": "ok", "si": "ok", "sí": "ok", "y": "ok", "yes": "ok",
            "e": "edit", "editar": "edit", "corregir": "edit", "n": "edit", "no": "edit",
        }
        choice = ask_choice(
            "\n  ¿Suena bien tal y como se pronuncia? [s = sí / e = corregir el texto y volver a escuchar]: ",
            options, default="s",
        )

        if choice == "ok":
            return script_text

        print(f"\n  Texto actual:\n  \"{script_text}\"")
        new_text = input(
            "\n  Escribe el texto corregido (p.ej. cambia \"VI\" por \"seis\", o respeta la "
            "ortografía para forzar otra pronunciación):\n  > "
        ).strip()
        if new_text:
            script_text = new_text


def ask_video_title() -> tuple:
    """Pregunta si se quiere poner un título en pantalla al vídeo (y cuántos
    segundos se ve, Enter = todo el vídeo). Se llama siempre antes de
    generar/revisar el guion (en las 4 formas de crear un vídeo), para que
    el título quede guardado junto al guion desde el principio. Devuelve
    (título, duración) — ambos None si no se quiere título."""
    if not ask_yes_no("\n  ¿Quieres ponerle un título en pantalla al vídeo? [s/N]: "):
        return None, None

    title = input("  Escribe el título: ").strip() or None
    if not title:
        return None, None

    while True:
        dur_str = input(
            "  ¿Cuántos segundos quieres que aparezca el título? (Enter = todo el vídeo): "
        ).strip().replace(",", ".")
        if not dur_str:
            return title, None
        try:
            title_duration = float(dur_str)
            if title_duration <= 0:
                raise ValueError
        except ValueError:
            print(f"  No te he entendido (\"{dur_str}\"). Escribe un número de segundos, "
                  "o Enter para que dure todo el vídeo.")
            continue
        return title, title_duration


DEFAULT_OUTRO_TEXT = "Suscríbete para más"


def ask_outro_text() -> str | None:
    """Pregunta si el gato debe despedirse al final del vídeo (p.ej.
    'Suscríbete para más'), superpuesto en los últimos segundos del clip (que
    se sigue reproduciendo entero, no se alarga). Devuelve el texto, o None
    si no se quiere despedida."""
    if not ask_yes_no(
        f"\n  ¿Quieres que el gato diga algo al final (p.ej. \"{DEFAULT_OUTRO_TEXT}\")? [s/N]: "
    ):
        return None
    text = input(f"  Texto de la despedida (Enter = \"{DEFAULT_OUTRO_TEXT}\"): ").strip()
    return text or DEFAULT_OUTRO_TEXT


def generate_silence(duration: float, output_path: Path) -> bool:
    """Genera un .mp3 de silencio real (no solo "volumen bajo") de la
    duración pedida, para el hueco entre el título y la despedida en
    build_sparse_narration — el silencio real es lo que deja al avatar en
    reposo ese tramo (su animación se basa en el volumen real del audio)."""
    if duration <= 0.05:
        return False
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", f"{duration:.3f}", "-q:a", "9", str(output_path)],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def build_sparse_narration(stem: str, intro_text: str | None, outro_text: str | None,
                            video_duration: float) -> tuple:
    """Para los modos donde el clip se reproduce ENTERO pero el gato solo
    habla al principio (título) y/o al final (despedida): genera un único
    ./audio/<stem>.mp3 con [narración inicial][silencio real][narración
    final], exactamente tan largo como el propio clip. Así el resto del
    pipeline (envolvente de volumen -> boca del avatar, subtítulos alineados
    con whisper, duración del vídeo final) funciona sin ningún cambio más:
    el silencio real ya dentro del audio es lo que deja al gato en reposo
    en ese tramo, y las palabras de guion solo existen donde de verdad se
    habla, así que whisper las alinea justo ahí.

    Devuelve (texto_conjunto, segmentos) — segmentos = lista de (inicio, fin)
    en segundos donde SÍ hay narración, para que compose_avatar.py no atenúe
    el audio original del clip en los tramos donde el gato está callado.
    Ambos son None si no hay ni título ni despedida que decir, o si falla
    la generación."""
    if not intro_text and not outro_text:
        return None, None

    import text_to_speech

    try:
        client, voice_id = text_to_speech.get_elevenlabs_client()
    except SystemExit as e:
        print(f"  {e}")
        return None, None

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    tmp_prefix = str(AUDIO_DIR / f"_tmp_{stem}")

    intro_path = None
    intro_dur = 0.0
    if intro_text:
        intro_path = Path(f"{tmp_prefix}_intro.mp3")
        if not text_to_speech.generate_speech(client, voice_id, intro_text, intro_path):
            print("  Fallo generando la narración inicial.")
            return None, None
        text_to_speech.prepend_intro_clip(intro_path)  # el "Miau" de sello, solo aquí
        intro_dur = get_media_duration(intro_path)

    outro_path = None
    outro_dur = 0.0
    if outro_text:
        outro_path = Path(f"{tmp_prefix}_outro.mp3")
        if not text_to_speech.generate_speech(client, voice_id, outro_text, outro_path):
            print("  Fallo generando la narración final.")
            if intro_path:
                intro_path.unlink(missing_ok=True)
            return None, None
        outro_dur = get_media_duration(outro_path)

    silence_dur = video_duration - intro_dur - outro_dur
    if silence_dur < 0:
        print(f"  [AVISO] El título + la despedida duran más que el propio clip "
              f"({intro_dur + outro_dur:.1f}s vs {video_duration:.1f}s) — se pegan "
              "seguidas, sin hueco de silencio en medio.")
        silence_dur = 0.0

    parts = []
    segments = []
    t = 0.0
    if intro_path:
        parts.append(intro_path)
        segments.append((0.0, intro_dur))
        t = intro_dur

    silence_path = None
    if silence_dur > 0.05:
        silence_path = Path(f"{tmp_prefix}_silence.mp3")
        if generate_silence(silence_dur, silence_path):
            parts.append(silence_path)
            t += silence_dur

    if outro_path:
        parts.append(outro_path)
        segments.append((t, t + outro_dur))

    final_path = AUDIO_DIR / f"{stem}.mp3"
    ok = concatenate_audio_files(parts, final_path)

    for p in (intro_path, silence_path, outro_path):
        if p and p != final_path:
            p.unlink(missing_ok=True)

    if not ok:
        return None, None

    combined_text = " ".join(text for text in (intro_text, outro_text) if text)
    return combined_text, segments


def ask_dialogue_mode(title: str | None) -> str:
    """Pregunta si el vídeo lleva diálogo del gato (lo normal, genera/edita un
    guion como siempre), o si va sin diálogo: solo leyendo el título en voz
    alta, o directamente sin narración (usa el audio original del clip, o
    queda mudo si se quitó al procesarlo). Se pregunta siempre, justo después
    del título, en las 4 formas de crear un vídeo. Devuelve 'script',
    'title_only' o 'no_narration'."""
    print("\n  ¿Diálogo del gato en este vídeo?")
    print("  [s = sí, el gato habla (por defecto) / t = no, que solo lea el título en voz alta / "
          "o = no, sin narración (audio original del clip)]")
    options = {
        "s": "script", "si": "script", "sí": "script", "y": "script", "yes": "script",
        "t": "title_only", "titulo": "title_only", "título": "title_only",
        "o": "no_narration", "original": "no_narration",
        "sin narracion": "no_narration", "sin narración": "no_narration",
    }
    mode = ask_choice("  Elige s/t/o [s]: ", options, default="s")

    if mode == "title_only" and not title:
        print("  No has puesto título, no hay nada que leer. Se usará diálogo normal.")
        return "script"

    return mode


AVATAR_POSITION_CHOICES = {
    "1": "top-right",
    "2": "top-left",
    "3": "bottom-right",
    "4": "bottom-left",
}


def ask_avatar_position(video_path: Path) -> str:
    """Pregunta en qué esquina poner al gato — para cuando la esquina por
    defecto (arriba-derecha) tapa una cara real del vídeo (facecam de un
    streamer, etc.). Se pregunta siempre, una vez por vídeo, porque cada
    clip puede tener la cara en un sitio distinto. Primero deja abrir el
    vídeo para ver dónde está la cara antes de decidir."""
    if ask_yes_no(
        "\n  ¿Abro el vídeo para ver dónde está la cara antes de elegir la esquina? [S/n]: ",
        default=True,
    ):
        open_file(video_path)

    print("\n  ¿En qué esquina pongo al gato? (para no tapar ninguna cara del vídeo)")
    print("  [1 = arriba-derecha, por defecto / 2 = arriba-izquierda / "
          "3 = abajo-derecha / 4 = abajo-izquierda]")
    return ask_choice("  Elige 1/2/3/4 [1]: ", AVATAR_POSITION_CHOICES, default="1")


def ask_avatar_choice(stem: str) -> bool | None:
    """Pregunta cómo animar al gato (con IA, gastando créditos de Replicate,
    o con los sprites locales, gratis) y si toca IA la genera ya mismo.
    Necesita que ./audio/<stem>.mp3 exista de antemano. Devuelve True si hay
    que forzar sprites, False si el avatar de IA se generó bien, o None si
    la generación con IA falló."""
    options = {"i": "ai", "ia": "ai", "s": "sprites", "sprites": "sprites"}
    avatar_choice = ask_choice(
        "\n  ¿Cómo animo al gato? [i = con IA, gasta créditos de Replicate (~$0.02/s) / "
        "s = con sprites, gratis]: ",
        options, default="s",
    )

    if avatar_choice == "ai":
        print("  Animando el avatar con IA (tarda varios minutos)...")
        if not run_step("generate_avatar_replicate.py", f"--only={stem}"):
            print("  Fallo animando el avatar.")
            return None
        return False

    print("  Usando la animación local por sprites (gratis).")
    return True


REVIEW_CHOICE_OPTIONS = {
    "s": "use", "si": "use", "sí": "use", "y": "use", "yes": "use",
    "n": "regenerate", "no": "regenerate",
    "t": "edit", "tu": "edit", "tú": "edit", "e": "edit", "editar": "edit", "escribir": "edit",
    "saltar": "skip", "skip": "skip",
    "parar": "stop", "stop": "stop", "salir": "stop",
}


def review_and_maybe_regenerate_script(stem: str, video_path: Path, data: dict):
    """Pregunta siempre primero quién escribe el diálogo (el gato con IA, o
    tú mismo). Si es la IA, muestra el borrador y deja aprobar/regenerar/
    escribirlo tú/saltar/parar. Una vez aceptado un texto (de cualquier
    origen), lo pasa por preview_and_confirm_audio() para comprobar cómo
    suena de verdad antes de dar el guion por definitivo. Guarda cada
    aprobación/rechazo en el perfil de estilo para que futuros guiones
    tengan en cuenta el gusto real del usuario.

    Devuelve True si se aprobó un guion, None si el usuario quiere saltar
    este vídeo, o "stop" si quiere parar del todo.
    """
    duration = data["duration"]
    transcript = data.get("transcript_original", "")
    script_text = data["script_es"]  # borrador que ya generó generate_script.py

    open_file(video_path)  # para poder comprobar de un vistazo si el guion encaja con el vídeo

    print(f"\n  El gato ya tiene un borrador para este vídeo:\n  \"{script_text}\"\n")
    choice = ask_choice(
        "  [s = usar este / n = que el gato genere otro / t = escribirlo/editarlo yo mismo / "
        "saltar / parar]: ",
        REVIEW_CHOICE_OPTIONS, default="s",
    )

    if choice == "edit":
        custom_text = write_own_script(stem, video_path, duration)
        if custom_text is not None:
            script_text = custom_text
        generate_script.record_feedback(stem, script_text, liked=True)

    elif choice == "skip":
        return None

    elif choice == "stop":
        return "stop"

    elif choice == "regenerate":
        # "no me gusta este, que el gato genere otro" — entra en el bucle de
        # regeneración con IA
        import anthropic
        from dotenv import load_dotenv

        load_dotenv()
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            print("  Falta ANTHROPIC_API_KEY en el .env.")
            return "stop"
        client = anthropic.Anthropic(api_key=api_key)

        rejected_texts = [script_text]
        reason = input("  ¿Por qué no te gusta el borrador? (opcional, Enter para dejarlo en blanco): ").strip()
        generate_script.record_feedback(stem, script_text, liked=False, reason=reason or None)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            frame_paths = generate_script.extract_frames(video_path, tmp_dir, generate_script.FRAMES_PER_VIDEO)

            while True:
                print("  Generando un guion nuevo...")
                style_context = generate_script.build_style_context(generate_script.load_style_profile())
                script_text = generate_script.generate_reaction_script(
                    client, frame_paths, transcript, duration, stem,
                    style_context=style_context, avoid_texts=rejected_texts,
                )

                print(f"\n  Guion nuevo:\n  \"{script_text}\"\n")
                choice = ask_choice(
                    "  ¿Te gusta? [s = sí / n = no, otro / t = escribirlo yo / saltar / parar]: ",
                    REVIEW_CHOICE_OPTIONS, default="s",
                )

                if choice == "use":
                    generate_script.record_feedback(stem, script_text, liked=True)
                    break
                if choice == "edit":
                    custom_text = write_own_script(stem, video_path, duration)
                    if custom_text is not None:
                        script_text = custom_text
                    generate_script.record_feedback(stem, script_text, liked=True)
                    break
                if choice == "skip":
                    return None
                if choice == "stop":
                    return "stop"
                # choice == "regenerate": sigue el bucle, pide el motivo y prueba otra vez

                reason = input("  ¿Por qué no te gusta? (opcional, Enter para dejarlo en blanco): ").strip()
                generate_script.record_feedback(stem, script_text, liked=False, reason=reason or None)
                rejected_texts.append(script_text)
    else:
        generate_script.record_feedback(stem, script_text, liked=True)

    # Guion aceptado (por IA o escrito a mano): se comprueba cómo suena de
    # verdad antes de darlo por definitivo y gastar en avatar/vídeo final.
    script_text = preview_and_confirm_audio(script_text, duration)

    data["script_es"] = script_text
    _save_json(SCRIPTS_DIR / f"{stem}.json", data)
    return True


# ----------------------------------------------------------------------------
# Terminar un vídeo que ya tiene guion (voz + avatar + composición)
# ----------------------------------------------------------------------------


def finish_video_interactive(stem: str) -> bool:
    """Guía la aprobación del guion y termina el vídeo. Devuelve False si el
    usuario quiere parar del todo (para cortar un bucle de varios vídeos)."""
    script_path = SCRIPTS_DIR / f"{stem}.json"
    if not script_path.exists():
        print(f"  [AVISO] No encuentro el guion de {stem}, lo salto.")
        return True

    data = _load_json(script_path)
    video_path = PROCESSED_CLIPS_DIR / f"{stem}.mp4"

    print(f"\n=== {stem} ===")
    data["title"], data["title_duration"] = ask_video_title()
    data["avatar_position"] = ask_avatar_position(video_path)
    dialogue_mode = ask_dialogue_mode(data["title"])

    if dialogue_mode in ("no_narration", "title_only"):
        # En estos dos modos el clip se reproduce SIEMPRE entero (no se
        # recorta a la duración de la narración, a diferencia del modo con
        # guion): el gato como mucho habla al principio (título) y/o al
        # final (despedida), con el resto del clip de fondo tal cual.
        video_duration = data.get("duration") or get_media_duration(video_path)
        outro_text = ask_outro_text()
        if outro_text:
            outro_text = preview_and_confirm_audio(outro_text, video_duration)

        intro_text = None
        if dialogue_mode == "title_only":
            intro_text = preview_and_confirm_audio(data["title"], video_duration)

        if intro_text or outro_text:
            print("\n  Generando narración (título/despedida)...")
        combined_text, segments = build_sparse_narration(stem, intro_text, outro_text, video_duration)

        if combined_text is None:
            print("\n  Sin diálogo: se usará el audio original del clip (o quedará mudo si "
                  "se quitó al procesarlo). El gato se queda en reposo, sin animar con IA "
                  "(no hay narración que lipsync-ear).")
            data["no_narration"] = True
            data.pop("script_es", None)
            data.pop("narration_segments", None)
            _save_json(script_path, data)
            force_sprites = True
        else:
            print(f"\n  El clip se reproducirá entero ({video_duration:.1f}s); el gato solo "
                  "habla en el título/despedida.")
            data["no_narration"] = False
            data["script_es"] = combined_text
            data["narration_segments"] = segments
            _save_json(script_path, data)
            force_sprites = ask_avatar_choice(stem)
            if force_sprites is None:
                return True
    else:
        result = review_and_maybe_regenerate_script(stem, video_path, data)
        if result is None:
            return True
        if result == "stop":
            return False

        print("\n  Generando narración de voz...")
        if not run_step("text_to_speech.py", f"--only={stem}"):
            print("  Fallo generando la voz.")
            return True

        force_sprites = ask_avatar_choice(stem)
        if force_sprites is None:
            return True

    print("\n  Componiendo el vídeo final...")
    compose_args = [f"--only={stem}"]
    if force_sprites:
        compose_args.append("--force-sprites")
    if not run_step("compose_avatar.py", *compose_args):
        print("  Fallo en la composición final.")
        return True

    final_path = FINAL_DIR / f"{stem}.mp4"
    if final_path.exists():
        print(f"\n  Vídeo terminado: {final_path.resolve()}")
        open_file(final_path)
    return True


# ----------------------------------------------------------------------------
# Opción 1 — vídeo completo buscando en internet
# ----------------------------------------------------------------------------


def flow_generate_from_internet():
    print("\n--- Generar vídeo completo (buscar en internet) ---")
    n_str = input("¿Cuántos vídeos nuevos buscar? [8]: ").strip()
    n = int(n_str) if n_str.isdigit() else 8

    print("\n1/3 Buscando y descargando vídeos nuevos...")
    if not run_step("youtube_downloader.py", "--limit", str(n)):
        print("Fallo en la descarga.")
        return

    print("\n2/3 Convirtiendo a 9:16...")
    if not run_step("process_clips.py"):
        print("Fallo en el procesado.")
        return

    print("\n3/3 Analizando vídeos y generando guiones (descarta los que no sirven)...")
    if not run_step("generate_script.py"):
        print("Fallo generando guiones.")
        return

    candidates = find_pending_stems()
    if not candidates:
        print("\nNinguno de los vídeos nuevos sirvió (reactor/montaje/etc.).")
        print("Prueba a buscar más, o cambia las palabras de búsqueda en youtube_downloader.py.")
        return

    print(f"\n{len(candidates)} vídeo(s) pasaron el filtro. Vamos uno a uno.")
    for stem in candidates:
        if not finish_video_interactive(stem):
            break


# ----------------------------------------------------------------------------
# Opción 2 — subir un vídeo del PC
# ----------------------------------------------------------------------------


def flow_upload_local():
    print("\n--- Subir un vídeo desde tu PC ---")
    path_str = input("Ruta completa del archivo de vídeo: ").strip().strip('"')
    src = Path(path_str)
    if not src.exists():
        print("  No encuentro ese archivo.")
        return

    RAW_CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    video_id = f"local_{int(time.time())}"
    dest = RAW_CLIPS_DIR / f"{video_id}{src.suffix}"
    shutil.copy(src, dest)
    print(f"  Copiado a {dest}")

    print("\n  Convirtiendo a 9:16...")
    if not run_step("process_clips.py"):
        print("  Fallo en el procesado.")
        return

    stem = f"{video_id}_9x16"
    print("\n  Generando guion (sin filtro de reactor/montaje: lo elegiste tú a propósito)...")
    if not run_step("generate_script.py", f"--only={stem}", "--skip-filter"):
        print("  Fallo generando el guion.")
        return

    if not (SCRIPTS_DIR / f"{stem}.json").exists():
        print("  No se generó ningún guion.")
        return

    finish_video_interactive(stem)


# ----------------------------------------------------------------------------
# Opción 3 — descargar de una URL (TikTok/YouTube/etc.)
# ----------------------------------------------------------------------------


def add_download_credit(video_id: str, title: str, channel: str, url: str) -> None:
    log = _load_json(DOWNLOADED_LOG)
    log[video_id] = {
        "title": title,
        "channel": channel,
        "url": url,
        "license": "Sin verificar - comprueba que tienes permiso para reutilizar este vídeo",
    }
    _save_json(DOWNLOADED_LOG, log)


def ask_time_range() -> str | None:
    """Pregunta un inicio y un fin para descargar solo ese fragmento en vez
    del vídeo/directo entero, usando --download-sections de yt-dlp. Clave
    para directos de Twitch (u otros vídeos largos): descarga solo el trozo
    pedido en vez de horas enteras. Devuelve el argumento ya listo para
    yt-dlp (p.ej. "*10:15-15:30"), o None si no se quiere recortar."""
    if not ask_yes_no(
        "\n  ¿Quieres recortar un fragmento (indicando inicio y fin) en vez de descargar "
        "el vídeo/directo entero? Útil para clips de un directo de Twitch. [s/N]: "
    ):
        return None

    start = input("  Inicio del fragmento (mm:ss o hh:mm:ss): ").strip()
    end = input("  Fin del fragmento (mm:ss o hh:mm:ss): ").strip()
    if not start or not end:
        print("  Inicio/fin vacíos, se descargará el vídeo/directo completo.")
        return None
    return f"*{start}-{end}"


def flow_download_url():
    print("\n--- Descargar un vídeo o clip desde una URL (YouTube/TikTok/Twitch/etc.) ---")
    url = input("URL del vídeo (o del directo/VOD/clip de Twitch): ").strip()
    if not url:
        return

    section = ask_time_range()

    RAW_CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    print("\n  Descargando con yt-dlp" + (" (solo el fragmento indicado)..." if section else "..."))
    # yt-dlp no garantiza el orden entre varios --print separados (algunos
    # campos se imprimen antes de descargar, otros después de mover el
    # archivo) — por eso el intento anterior con 3 --print salía desordenado.
    # Con un único --print de una sola línea, separado por tabulador, no hay
    # ambigüedad posible.
    cmd = [
        "yt-dlp", "--no-simulate",
        "-o", str(RAW_CLIPS_DIR / "%(id)s.%(ext)s"),
        "--print", "%(id)s\t%(title)s\t%(uploader)s",
    ]
    if section:
        # --force-keyframes-at-cuts hace un recorte más preciso (evita que el
        # fragmento empiece/acabe en un fotograma "de más" por el keyframe
        # más cercano), a costa de tener que reencodear ese trozo.
        cmd += ["--download-sections", section, "--force-keyframes-at-cuts"]
    cmd += [url]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("  Fallo al descargar:")
        print(result.stderr[-800:])
        return

    line = next((l for l in result.stdout.strip().splitlines() if l), "")
    parts = line.split("\t")
    if len(parts) < 3:
        print("  No se pudo leer la información del vídeo descargado.")
        return
    video_id, title, uploader = parts[0], parts[1], parts[2]

    matches = list(RAW_CLIPS_DIR.glob(f"{video_id}.*"))
    if not matches:
        print(f"  No encuentro el archivo descargado para el id {video_id}.")
        return
    downloaded_path = matches[0]
    print(f"  Descargado: {downloaded_path}  (por {uploader})")

    add_download_credit(video_id, title, uploader, url)
    print("  Aviso: si este vídeo no es tuyo ni tiene licencia CC-BY verificada, "
          "confirma que tienes permiso para reutilizarlo antes de publicarlo.")

    print("\n  Convirtiendo a 9:16...")
    if not run_step("process_clips.py"):
        print("  Fallo en el procesado.")
        return

    stem = f"{downloaded_path.stem}_9x16"
    print("\n  Generando guion...")
    if not run_step("generate_script.py", f"--only={stem}", "--skip-filter"):
        print("  Fallo generando el guion.")
        return

    if not (SCRIPTS_DIR / f"{stem}.json").exists():
        print("  No se generó ningún guion.")
        return

    finish_video_interactive(stem)


# ----------------------------------------------------------------------------
# Opción 4 — vídeo a partir de imágenes fijas + guion narrado
# ----------------------------------------------------------------------------


def read_text_or_file(prompt: str) -> str | None:
    """Pide una línea de texto, o la ruta de un .txt para cargarlo (útil si
    la narración de una imagen es larga y ya la tienes escrita aparte)."""
    raw = input(prompt).strip()
    if not raw:
        return None

    candidate = Path(raw.strip('"'))
    if candidate.suffix.lower() == ".txt" and candidate.exists():
        text = candidate.read_text(encoding="utf-8").strip()
        print(f"  Cargado desde {candidate} ({len(text.split())} palabras).")
        return text or None

    return raw


def ask_scene_for_image(img: Path, index: int, total: int) -> dict | None:
    """Pregunta, para una imagen concreta, qué se narra mientras se ve y qué
    texto (si alguno) aparece en pantalla. La duración de esa imagen en el
    vídeo final no se pide: será justo la duración real de su narración
    (una vez generada la voz), así no hay que calcular nada a mano ni
    arriesgarse a que guion e imagen se desajusten. Devuelve None si se opta
    por omitir la imagen."""
    print(f"\n  --- Imagen {index}/{total}: {img.name} ---")
    if ask_yes_no("  ¿Abro la imagen para verla mientras escribes? [S/n]: ", default=True):
        open_file(img)

    narration = read_text_or_file("  Narración para esta imagen (lo que se dirá): ")
    if not narration:
        if ask_yes_no("  Vacío — ¿omitir esta imagen del vídeo? [S/n]: ", default=True):
            return None
        return ask_scene_for_image(img, index, total)

    caption = input("  Texto en pantalla para esta imagen (Enter para ninguno): ").strip() or None
    return {"image": img, "narration": narration, "caption": caption}


def concatenate_audio_files(paths: list, output_path: Path) -> bool:
    """Une varios .mp3 en uno solo, en el orden dado (la narración de cada
    imagen, una detrás de otra, sin huecos)."""
    if len(paths) == 1:
        shutil.copy(paths[0], output_path)
        return True

    inputs = []
    for p in paths:
        inputs += ["-i", str(p)]
    filter_inputs = "".join(f"[{i}:a]" for i in range(len(paths)))
    filter_complex = f"{filter_inputs}concat=n={len(paths)}:v=0:a=1[out]"
    result = subprocess.run(
        ["ffmpeg", "-y", *inputs, "-filter_complex", filter_complex, "-map", "[out]", str(output_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  [ERROR] No se pudo unir las narraciones: {result.stderr[-400:]}")
        return False
    return True


def flow_generate_from_images():
    print("\n--- Generar vídeo a partir de imágenes ---")
    if not IMAGES_DIR.exists():
        print(f"  No existe la carpeta {IMAGES_DIR.resolve()}. Créala y pon ahí las imágenes.")
        return

    images = sorted(
        (p for p in IMAGES_DIR.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
        key=natural_sort_key,
    )
    if not images:
        print(f"  No hay imágenes en {IMAGES_DIR.resolve()} ({', '.join(sorted(IMAGE_EXTENSIONS))}).")
        return

    print(f"  {len(images)} imagen(es) encontradas en {IMAGES_DIR}, en este orden:")
    for img in images:
        print(f"    - {img.name}")

    title, title_duration = ask_video_title()

    print("\n  Ahora, para cada imagen: qué se narra mientras se ve, y (opcional) qué texto "
          "aparece en pantalla sobre ella. Cada imagen dura en el vídeo final lo mismo que "
          "tarde en decirse su narración — no hace falta fijar segundos a mano.")

    scenes = []
    for i, img in enumerate(images, start=1):
        scene = ask_scene_for_image(img, i, len(images))
        if scene:
            scenes.append(scene)

    if not scenes:
        print("  No se ha añadido ninguna imagen con narración. Cancelado.")
        return

    import text_to_speech
    try:
        client, voice_id = text_to_speech.get_elevenlabs_client()
    except SystemExit as e:
        print(f"  {e}")
        return

    stem = f"images_{int(time.time())}"
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    seg_paths = []
    durations = []
    for i, scene in enumerate(scenes, start=1):
        print(f"\n  --- Narración {i}/{len(scenes)} ({scene['image'].name}) ---")
        estimated = max(1.0, len(scene["narration"].split()) / generate_script.WORDS_PER_SECOND)
        scene["narration"] = preview_and_confirm_audio(scene["narration"], estimated)

        seg_path = AUDIO_DIR / f"{stem}_seg{i}.mp3"
        if not text_to_speech.generate_speech(client, voice_id, scene["narration"], seg_path):
            print("  Fallo generando esta narración.")
            return
        seg_paths.append(seg_path)
        durations.append(get_media_duration(seg_path))

    print("\n  Uniendo las narraciones en el audio final...")
    audio_path = AUDIO_DIR / f"{stem}.mp3"
    if not concatenate_audio_files(seg_paths, audio_path):
        return
    if len(seg_paths) > 1:
        for seg_path in seg_paths:
            seg_path.unlink(missing_ok=True)

    # Mismo "Miau" de sello de identidad que llevan los demás vídeos, pegado
    # al principio del audio ya unido. Como se añade tiempo por delante, la
    # primera imagen se alarga esa misma cantidad para que la imagen y el
    # audio seguan encajando (el resto de imágenes no se ven afectadas).
    before_intro = get_media_duration(audio_path)
    text_to_speech.prepend_intro_clip(audio_path)
    after_intro = get_media_duration(audio_path)
    if after_intro - before_intro > 0.05:
        durations[0] += after_intro - before_intro

    script_text = " ".join(scene["narration"] for scene in scenes)

    force_sprites = ask_avatar_choice(stem)
    if force_sprites is None:
        return

    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    job = {
        "source": "images",
        "script_es": script_text,
        "title": title,
        "title_duration": title_duration,
        "images": [str(scene["image"].resolve()) for scene in scenes],
        "image_durations": durations,
        "captions": [scene["caption"] for scene in scenes],
    }
    _save_json(SCRIPTS_DIR / f"{stem}.json", job)

    print("\n  Componiendo el vídeo final...")
    compose_args = [f"--only={stem}"]
    if force_sprites:
        compose_args.append("--force-sprites")
    if not run_step("compose_images.py", *compose_args):
        print("  Fallo en la composición del vídeo.")
        return

    final_path = FINAL_DIR / f"{stem}.mp4"
    if final_path.exists():
        print(f"\n  Vídeo terminado: {final_path.resolve()}")
        open_file(final_path)


# ----------------------------------------------------------------------------
# Opción 5 — ver / publicar vídeos terminados
# ----------------------------------------------------------------------------


def flow_publish_menu():
    print("\n--- Vídeos terminados ---")
    videos = list_final_videos()
    video = pick_from_list(videos, "¿Cuál quieres abrir/publicar? (número, Enter para cancelar): ")
    if not video:
        return

    open_file(video)

    if not ask_yes_no(
        "  ¿Publicarlo en TikTok ahora (modo Sandbox = privado, hasta que apruebe la API)? [s/N]: "
    ):
        return

    title = input("  Título para TikTok (Enter para el de por defecto): ").strip()
    args = [str(video.resolve())]
    if title:
        args.append(title)
    tiktok_post_path = Path("../tiktok_post.py")
    result = subprocess.run([PYTHON, str(tiktok_post_path), *args])
    if result.returncode != 0:
        print("  Fallo al publicar (revisa que scripts/.env tenga TIKTOK_ACCESS_TOKEN).")


# ----------------------------------------------------------------------------
# Opción 6 — regenerar avatar o recomponer un vídeo
# ----------------------------------------------------------------------------


def flow_regenerate_menu():
    print("\n--- Regenerar avatar o recomponer un vídeo ---")
    videos = list_final_videos()
    video = pick_from_list(videos, "¿Cuál quieres tocar? (número, Enter para cancelar): ")
    if not video:
        return
    stem = video.stem

    script_path = SCRIPTS_DIR / f"{stem}.json"
    is_images = script_path.exists() and _load_json(script_path).get("source") == "images"

    print("  a) Regenerar el avatar con IA otra vez (gasta crédito de Replicate)")
    print("  b) Solo recomponer el vídeo final (subtítulos/overlay), sin tocar el avatar "
          "(usa sprites si no hay ya un avatar de IA generado)")
    if is_images:
        print("  c) Dividir este vídeo en 2 (por si dura demasiado para un short) — cada "
              "parte queda como un vídeo independiente y completo, con su propio Miau")
    regen_options = {"a": "a", "b": "b"}
    if is_images:
        regen_options["c"] = "c"
    choice = ask_choice(f"  Elige a/b{'/c' if is_images else ''} [b]: ", regen_options, default="b")

    if choice == "c" and is_images:
        data = _load_json(script_path)
        images = data["images"]
        durations = data["image_durations"]
        n = len(images)
        print(f"\n  Este vídeo tiene {n} imágenes:")
        for i, (img, dur) in enumerate(zip(images, durations), start=1):
            print(f"    {i}. {Path(img).name} ({dur:.1f}s)")

        split_str = input(f"  ¿Cuántas imágenes van en la primera parte? (1-{n - 1}): ").strip()
        if not split_str.isdigit() or not (1 <= int(split_str) <= n - 1):
            print("  Valor no válido.")
            return
        split_at = int(split_str)

        avatar_choice = ask_choice(
            "\n  ¿Cómo animo al gato en las dos partes? [i = con IA, gasta créditos de Replicate "
            "(~$0.02/s cada parte) / s = con sprites, gratis]: ",
            {"i": "ai", "ia": "ai", "s": "sprites", "sprites": "sprites"}, default="s",
        )
        force_sprites = avatar_choice != "ai"

        print("\n  Dividiendo y componiendo las dos partes (puede tardar)...")
        split_args = [f"--only={stem}", f"--split-at={split_at}"]
        if force_sprites:
            split_args.append("--force-sprites")
        if not run_step("compose_images.py", *split_args):
            print("  Fallo al dividir el vídeo.")
            return

        for suffix in ("_p1", "_p2"):
            part_path = FINAL_DIR / f"{stem}{suffix}.mp4"
            if part_path.exists():
                print(f"  Vídeo terminado: {part_path.resolve()}")
                open_file(part_path)
        return

    if choice == "a":
        avatar_path = AVATAR_CLIPS_DIR / f"{stem}.mp4"
        avatar_path.unlink(missing_ok=True)
        clear_log_entry(AVATAR_REPLICATE_LOG, f"{stem}.mp3")
        if not run_step("generate_avatar_replicate.py", f"--only={stem}"):
            print("  Fallo animando el avatar.")
            return

    # Un vídeo de imágenes se recompone con compose_images.py (imágenes +
    # avatar), uno normal de reacción con compose_avatar.py (vídeo de fondo +
    # avatar) — cada uno tiene su propio log de progreso.
    if is_images:
        clear_log_entry(IMAGES_COMPOSED_LOG, f"{stem}.mp3")
        if not run_step("compose_images.py", f"--only={stem}"):
            print("  Fallo en la composición final.")
            return
    else:
        if ask_yes_no("\n  ¿Cambio la esquina del gato (p.ej. si tapa una cara)? [s/N]: "):
            data = _load_json(script_path)
            data["avatar_position"] = ask_avatar_position(video)
            _save_json(script_path, data)

        clear_log_entry(COMPOSED_LOG, stem)
        if not run_step("compose_avatar.py", f"--only={stem}"):
            print("  Fallo en la composición final.")
            return

    print(f"\n  Hecho: {video.resolve()}")
    open_file(video)


# ----------------------------------------------------------------------------
# Opción 7 — gasto estimado en Replicate
# ----------------------------------------------------------------------------


def flow_show_spend():
    print("\n--- Gasto estimado en Replicate ---")
    if not AVATAR_CLIPS_DIR.exists() or not any(AVATAR_CLIPS_DIR.glob("*.mp4")):
        print("  Todavía no has generado ningún avatar con IA.")
        return

    total_seconds = 0.0
    count = 0
    for clip in AVATAR_CLIPS_DIR.glob("*.mp4"):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(clip)],
            capture_output=True, text=True,
        )
        try:
            total_seconds += float(result.stdout.strip())
            count += 1
        except ValueError:
            pass

    cost = total_seconds * REPLICATE_COST_PER_SECOND
    print(f"  {count} vídeo(s) de avatar generados, {total_seconds:.1f}s en total.")
    print(f"  Coste estimado: ${cost:.2f} (a ${REPLICATE_COST_PER_SECOND}/segundo de wan-2.2-s2v).")
    print("  Esto es una estimación local a partir de los archivos que tienes — el saldo")
    print("  exacto siempre está en https://replicate.com/account/billing")


# ----------------------------------------------------------------------------
# Menú principal
# ----------------------------------------------------------------------------


def main_menu():
    while True:
        print("\n" + "=" * 50)
        print("  ReactionFlow - Generador de vídeos del gato")
        print("=" * 50)
        print("1. Generar un vídeo completo (buscar en internet)")
        print("2. Subir un vídeo desde tu PC")
        print("3. Descargar un vídeo o clip desde una URL (YouTube/TikTok/Twitch)")
        print("4. Generar un vídeo a partir de imágenes (guion narrado)")
        print("5. Ver / publicar vídeos terminados")
        print("6. Regenerar avatar o recomponer un vídeo ya hecho")
        print("7. Ver gasto estimado en Replicate")
        print("0. Salir")

        choice = input("\nElige una opción: ").strip()

        try:
            if choice == "1":
                flow_generate_from_internet()
            elif choice == "2":
                flow_upload_local()
            elif choice == "3":
                flow_download_url()
            elif choice == "4":
                flow_generate_from_images()
            elif choice == "5":
                flow_publish_menu()
            elif choice == "6":
                flow_regenerate_menu()
            elif choice == "7":
                flow_show_spend()
            elif choice == "0":
                print("Hasta luego.")
                break
            else:
                print("Opción no válida.")
        except KeyboardInterrupt:
            print("\n(cancelado)")
        except Exception as e:
            print(f"\n[ERROR inesperado] {e}")


if __name__ == "__main__":
    main_menu()
