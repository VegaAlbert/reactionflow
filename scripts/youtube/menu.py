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
DOWNLOADED_LOG = Path("./downloaded.json")
SCRIPTED_LOG = Path("./scripted.json")
AVATAR_REPLICATE_LOG = Path("./avatar_replicate.json")
COMPOSED_LOG = Path("./composed.json")

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


def pick_from_list(items: list, prompt: str):
    if not items:
        print("  (no hay ninguno)")
        return None
    for i, item in enumerate(items, start=1):
        print(f"  {i}. {item.name}")
    choice = input(prompt).strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(items)):
        return None
    return items[int(choice) - 1]


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


def open_file(path: Path) -> None:
    try:
        os.startfile(path)  # Windows
    except Exception:
        print(f"  Ábrelo manualmente: {path.resolve()}")


# ----------------------------------------------------------------------------
# Revisión y regeneración interactiva del guion
# ----------------------------------------------------------------------------


def review_and_maybe_regenerate_script(stem: str, video_path: Path, data: dict):
    """Muestra el guion actual y pregunta si gusta. Si no, pide un motivo
    (opcional) y genera uno nuevo evitando los mismos fallos, hasta que se
    apruebe. Guarda cada aprobación/rechazo en el perfil de estilo para que
    futuros guiones tengan en cuenta el gusto real del usuario.

    Devuelve True si se aprobó un guion, None si el usuario quiere saltar
    este vídeo, o "stop" si quiere parar del todo.
    """
    import anthropic
    from dotenv import load_dotenv

    load_dotenv()
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("  Falta ANTHROPIC_API_KEY en el .env.")
        return "stop"
    client = anthropic.Anthropic(api_key=api_key)

    duration = data["duration"]
    transcript = data.get("transcript_original", "")
    script_text = data["script_es"]
    rejected_texts = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        frame_paths = generate_script.extract_frames(video_path, tmp_dir, generate_script.FRAMES_PER_VIDEO)

        while True:
            print(f"\n  Guion actual:\n  \"{script_text}\"\n")
            choice = input(
                "  ¿Te gusta? [s = sí / n = no, regenerar / e = escribirlo yo mismo / saltar / parar]: "
            ).strip().lower()

            if choice in ("s", "si", "sí", "y", "yes", ""):
                generate_script.record_feedback(stem, script_text, liked=True)
                data["script_es"] = script_text
                _save_json(SCRIPTS_DIR / f"{stem}.json", data)
                return True

            if choice in ("e", "editar", "escribir"):
                custom_text = input("  Escribe tú el guion (lo que dirá y aparecerá en subtítulos):\n  > ").strip()
                if not custom_text:
                    print("  (vacío, no se ha cambiado nada)")
                    continue
                # Un guion escrito por el propio usuario es la señal de gusto
                # más fuerte posible: se guarda como "liked" para que futuros
                # guiones generados por IA se acerquen más a este estilo.
                generate_script.record_feedback(stem, custom_text, liked=True)
                data["script_es"] = custom_text
                _save_json(SCRIPTS_DIR / f"{stem}.json", data)
                return True

            if choice in ("saltar", "skip"):
                return None

            if choice in ("parar", "stop", "salir"):
                return "stop"

            reason = input("  ¿Por qué no te gusta? (opcional, Enter para dejarlo en blanco): ").strip()
            generate_script.record_feedback(stem, script_text, liked=False, reason=reason or None)
            rejected_texts.append(script_text)

            print("  Generando un guion nuevo...")
            style_context = generate_script.build_style_context(generate_script.load_style_profile())
            script_text = generate_script.generate_reaction_script(
                client, frame_paths, transcript, duration, stem,
                style_context=style_context, avoid_texts=rejected_texts,
            )


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
    result = review_and_maybe_regenerate_script(stem, video_path, data)
    if result is None:
        return True
    if result == "stop":
        return False

    print("\n  Generando narración de voz...")
    if not run_step("text_to_speech.py", f"--only={stem}"):
        print("  Fallo generando la voz.")
        return True

    print("\n  Animando el avatar con IA (tarda varios minutos, ~$0.02/segundo de vídeo)...")
    if not run_step("generate_avatar_replicate.py", f"--only={stem}"):
        print("  Fallo animando el avatar.")
        return True

    print("\n  Componiendo el vídeo final...")
    if not run_step("compose_avatar.py", f"--only={stem}"):
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


def flow_download_url():
    print("\n--- Descargar un vídeo desde una URL (TikTok/YouTube/etc.) ---")
    url = input("URL del vídeo: ").strip()
    if not url:
        return

    RAW_CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    print("\n  Descargando con yt-dlp...")
    # yt-dlp no garantiza el orden entre varios --print separados (algunos
    # campos se imprimen antes de descargar, otros después de mover el
    # archivo) — por eso el intento anterior con 3 --print salía desordenado.
    # Con un único --print de una sola línea, separado por tabulador, no hay
    # ambigüedad posible.
    result = subprocess.run(
        [
            "yt-dlp", "-o", str(RAW_CLIPS_DIR / "%(id)s.%(ext)s"),
            "--print", "%(id)s\t%(title)s\t%(uploader)s",
            url,
        ],
        capture_output=True, text=True,
    )
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
# Opción 4 — ver / publicar vídeos terminados
# ----------------------------------------------------------------------------


def flow_publish_menu():
    print("\n--- Vídeos terminados ---")
    videos = list_final_videos()
    video = pick_from_list(videos, "¿Cuál quieres abrir/publicar? (número, Enter para cancelar): ")
    if not video:
        return

    open_file(video)

    do_publish = input("  ¿Publicarlo en TikTok ahora (modo Sandbox = privado, hasta que apruebe la API)? [s/N]: ").strip().lower()
    if do_publish not in ("s", "si", "sí", "y", "yes"):
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
# Opción 5 — regenerar avatar o recomponer un vídeo
# ----------------------------------------------------------------------------


def flow_regenerate_menu():
    print("\n--- Regenerar avatar o recomponer un vídeo ---")
    videos = list_final_videos()
    video = pick_from_list(videos, "¿Cuál quieres tocar? (número, Enter para cancelar): ")
    if not video:
        return
    stem = video.stem

    print("  a) Regenerar el avatar con IA otra vez (gasta crédito de Replicate)")
    print("  b) Solo recomponer el vídeo final (subtítulos/overlay), sin tocar el avatar")
    choice = input("  Elige a/b: ").strip().lower()

    if choice == "a":
        avatar_path = AVATAR_CLIPS_DIR / f"{stem}.mp4"
        avatar_path.unlink(missing_ok=True)
        clear_log_entry(AVATAR_REPLICATE_LOG, f"{stem}.mp3")
        if not run_step("generate_avatar_replicate.py", f"--only={stem}"):
            print("  Fallo animando el avatar.")
            return

    clear_log_entry(COMPOSED_LOG, stem)
    if not run_step("compose_avatar.py", f"--only={stem}"):
        print("  Fallo en la composición final.")
        return

    print(f"\n  Hecho: {video.resolve()}")
    open_file(video)


# ----------------------------------------------------------------------------
# Opción 6 — gasto estimado en Replicate
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
        print("3. Descargar un vídeo desde una URL (TikTok/YouTube)")
        print("4. Ver / publicar vídeos terminados")
        print("5. Regenerar avatar o recomponer un vídeo ya hecho")
        print("6. Ver gasto estimado en Replicate")
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
                flow_publish_menu()
            elif choice == "5":
                flow_regenerate_menu()
            elif choice == "6":
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
