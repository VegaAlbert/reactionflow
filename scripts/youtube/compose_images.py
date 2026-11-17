"""
compose_images.py — Composición de vídeo a partir de imágenes fijas + narración.

Variante de compose_avatar.py para el caso en que no hay un vídeo de fondo
(reacción a un clip), sino una serie de imágenes puestas por el usuario en
./imagen_video que se muestran una tras otra, cada una el tiempo indicado en
el guion (./scripts/<id>.json, generado interactivamente por menu.py), con la
narración de voz ya generada (./audio/<id>.mp3, con el "Miau" de intro igual
que el resto de vídeos) y subtítulos quemados sincronizados con el audio real.
El avatar del gato (con IA en ./avatar_clips, o por sprites en
./assets/avatar) se superpone igual que en compose_avatar.py — reutiliza sus
mismas funciones (chroma key, animación por volumen, subtítulos, marca de
agua) en vez de duplicarlas.

Formato esperado en ./scripts/<id>.json para este script (lo escribe
menu.py):
    {
        "source": "images",
        "script_es": "...",                   # narración completa (todas las imágenes, concatenada)
        "title": "..." | null,                # título general opcional
        "title_duration": 5.0 | null,         # segundos que se ve el título; null = todo el vídeo
        "images": ["C:/.../imagen_video/1.jpg", ...],
        "image_durations": [3.2, 3.2, ...],   # segundos, mismo orden que "images" (duración real de
                                               # la narración de cada imagen)
        "captions": ["10 AÑOS DE ESPERA", null, ...]  # texto en pantalla por imagen, opcional
    }

Uso:
    python compose_images.py --only images_1234567890
    python compose_images.py --only images_1234567890 --force-sprites
    python compose_images.py
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

import compose_avatar as ca

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ----------------------------------------------------------------------------
# CONFIGURACIÓN
# ----------------------------------------------------------------------------

AUDIO_DIR = Path("./audio")
SCRIPT_DIR = Path("./scripts")
OUTPUT_DIR = Path("./final")
LOG_FILE = Path("./images_composed.json")

# Texto en pantalla propio de cada imagen (p.ej. "10 AÑOS DE ESPERA"),
# distinto del título general del vídeo: solo se ve mientras esa imagen está
# en pantalla. Va más abajo que TITLE_MARGIN_TOP para no pisar un título
# general si también lo hay.
CAPTION_FONT_SIZE = 64
CAPTION_MARGIN_TOP = 260

# ----------------------------------------------------------------------------


def load_log() -> dict:
    if LOG_FILE.exists():
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_log(log: dict) -> None:
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def check_tools() -> None:
    # Mismas comprobaciones que compose_avatar.py (ffmpeg/ffprobe + sprites
    # del avatar, con el mismo aviso si solo hay avatares de IA generados).
    ca.check_tools()


def build_slideshow(images: list, durations: list, narration_duration: float,
                     ass_subs: Path, output_path: Path, audio_path: Path,
                     title: str | None = None, title_duration: float | None = None,
                     captions: list | None = None, avatar_clip: Path | None = None,
                     avatar_position: str = ca.AVATAR_POSITION) -> None:
    n = len(images)
    captions = captions or [None] * n

    # No usamos "-loop 1 -t D -i img" (que sí funciona con jpg/png/webp vía el
    # demuxer image2): un .avif es en realidad un contenedor MOV/ISOBMFF, que
    # ffmpeg abre con el demuxer "mov" en vez de "image2", y ese demuxer no
    # admite la opción "-loop". Para que todos los formatos de imagen se
    # comporten igual, cada imagen se abre normal (un único fotograma) y se
    # "sostiene" la duración deseada con el filtro loop+trim, que no depende
    # del demuxer usado.
    image_inputs = []
    for img in images:
        image_inputs += ["-i", str(img)]

    audio_input_index = n
    extra_inputs = []  # rutas añadidas tras el audio, en orden (avatar, marca de agua)
    next_input_index = n + 1

    avatar_input_index = None
    if avatar_clip:
        avatar_input_index = next_input_index
        extra_inputs.append(str(avatar_clip))
        next_input_index += 1

    filter_parts = []
    for i, dur in enumerate(durations):
        filter_parts.append(
            f"[{i}:v]scale={ca.OUTPUT_WIDTH}:{ca.OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={ca.OUTPUT_WIDTH}:{ca.OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            f"loop=loop=-1:size=1:start=0,trim=duration={dur:.3f},fps={ca.FPS},setpts=PTS-STARTPTS[v{i}]"
        )
    concat_inputs = "".join(f"[v{i}]" for i in range(n))
    filter_parts.append(f"{concat_inputs}concat=n={n}:v=1:a=0[vraw]")
    last_label = "vraw"

    if avatar_clip:
        # Mismo chroma key + overlay que compose_avatar.py (ver ahí el
        # porqué de cada paso: colorkey+despill quita el verde, split +
        # alphaextract + erosion se come el halo del borde del recorte).
        chroma_filter = f"colorkey={ca.CHROMA_COLOR}:{ca.CHROMA_SIMILARITY}:{ca.CHROMA_BLEND}"
        if ca.USE_DESPILL:
            chroma_filter += ",despill=type=green:mix=0.5:expand=0"
        erosion_chain = ",".join(["erosion"] * ca.CHROMA_EDGE_EROSION_PASSES)
        filter_parts.append(
            f"[{avatar_input_index}:v]{chroma_filter}[keyed];"
            f"[keyed]split[keyed1][keyed2];"
            f"[keyed2]alphaextract,{erosion_chain}[matte];"
            f"[keyed1][matte]alphamerge,scale={ca.AVATAR_WIDTH}:-2[avatar]"
        )
        filter_parts.append(
            f"[{last_label}][avatar]overlay={ca.avatar_overlay_position(avatar_position)}:shortest=0[withavatar]"
        )
        last_label = "withavatar"

    filter_parts.append(f"[{last_label}]subtitles='{ca.escape_for_filter(ass_subs)}'[vsub]")
    last_label = "vsub"

    # Texto en pantalla propio de cada imagen, solo visible durante su
    # propio tramo (calculado a partir de las duraciones, en el mismo orden
    # que las imágenes).
    caption_font_path = Path(ca.TITLE_FONT_FILE)
    if any(captions) and not caption_font_path.exists():
        print(f"  [AVISO] No se encontró la fuente {ca.TITLE_FONT_FILE}, se omiten los textos en pantalla.")
    t = 0.0
    for i, (dur, caption) in enumerate(zip(durations, captions)):
        start, end = t, t + dur
        t = end
        if not caption or not caption_font_path.exists():
            continue
        caption_text = ca.escape_drawtext(caption)
        caption_font_arg = ca.escape_for_filter(caption_font_path)
        new_label = f"vcap{i}"
        filter_parts.append(
            f"[{last_label}]drawtext=text='{caption_text}':fontfile='{caption_font_arg}':"
            f"fontsize={CAPTION_FONT_SIZE}:fontcolor=white:box=1:boxcolor=black@0.55:"
            f"boxborderw=16:x=(w-text_w)/2:y={CAPTION_MARGIN_TOP}:"
            f"enable='between(t,{start:.3f},{end:.3f})'[{new_label}]"
        )
        last_label = new_label

    if title:
        title_font_path = Path(ca.TITLE_FONT_FILE)
        if title_font_path.exists():
            title_text = ca.escape_drawtext(title)
            title_font_arg = ca.escape_for_filter(title_font_path)
            title_enable = f":enable='lt(t,{title_duration:.3f})'" if title_duration else ""
            filter_parts.append(
                f"[{last_label}]drawtext=text='{title_text}':fontfile='{title_font_arg}':"
                f"fontsize={ca.TITLE_FONT_SIZE}:fontcolor=white:box=1:boxcolor=black@0.55:"
                f"boxborderw=14:x=(w-text_w)/2:y={ca.TITLE_MARGIN_TOP}{title_enable}[vtitle]"
            )
            last_label = "vtitle"
        else:
            print(f"  [AVISO] No se encontró la fuente {ca.TITLE_FONT_FILE}, se omite el título en pantalla.")

    use_watermark = ca.SHOW_WATERMARK and ca.WATERMARK_IMAGE.exists()
    if use_watermark:
        watermark_index = next_input_index
        next_input_index += 1
        extra_inputs.append(str(ca.WATERMARK_IMAGE))
        filter_parts.append(
            f"[{watermark_index}:v]scale={ca.WATERMARK_WIDTH}:-1,format=rgba,"
            f"colorchannelmixer=aa={ca.WATERMARK_OPACITY}[wm]"
        )
        filter_parts.append(
            f"[{last_label}][wm]overlay={ca.overlay_position(ca.opposite_corner(avatar_position), ca.WATERMARK_MARGIN)}[vout]"
        )
        last_label = "vout"
    else:
        if ca.SHOW_WATERMARK:
            print(f"  [AVISO] No se encontró la marca de agua en {ca.WATERMARK_IMAGE}, se omite.")
        filter_parts.append(f"[{last_label}]null[vout]")
        last_label = "vout"

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *image_inputs,
        "-i", str(audio_path),
        *[arg for path in extra_inputs for arg in ("-i", path)],
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", f"{audio_input_index}:a",
        "-t", f"{narration_duration:.3f}",
        "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    result = ca.run(cmd)
    if result.returncode != 0:
        sys.exit(f"[ERROR ffmpeg] No se pudo componer el vídeo de imágenes: {result.stderr[-1500:]}")


def resolve_avatar_clip(stem: str, audio_path: Path, tmp_dir: Path, force_sprites: bool) -> Path:
    """Mismo criterio que compose_avatar.py: si ya hay un avatar generado con
    IA para este stem (y no se ha pedido forzar sprites), se usa directamente;
    si no, se anima localmente por sprite-swap a partir del volumen de la
    narración."""
    ai_avatar_path = ca.AI_AVATAR_DIR / f"{stem}.mp4"
    if force_sprites and ai_avatar_path.exists():
        print(f"  Ignorando avatar de IA existente en {ai_avatar_path} (se pidió sprites).")

    if ai_avatar_path.exists() and not force_sprites:
        print(f"  Usando avatar generado con IA: {ai_avatar_path}")
        return ai_avatar_path

    print("  Analizando volumen de la narración...")
    wav_path = ca.decode_to_mono_wav(audio_path, tmp_dir)
    envelope = ca.compute_volume_envelope(wav_path, ca.FPS)

    print("  Construyendo animación del avatar (sprite-swap)...")
    timeline = ca.build_sprite_timeline(envelope, ca.FPS)
    concat_list = ca.write_concat_list(timeline, ca.FPS, tmp_dir)
    avatar_clip = tmp_dir / f"avatar_raw_{stem}.mp4"
    ca.render_avatar_clip(concat_list, ca.FPS, avatar_clip)
    return avatar_clip


def process_one(stem: str, keep_temp: bool = False, force_sprites: bool = False) -> None:
    script_path = SCRIPT_DIR / f"{stem}.json"
    audio_path = AUDIO_DIR / f"{stem}.mp3"
    output_path = OUTPUT_DIR / f"{stem}.mp4"

    for p in (script_path, audio_path):
        if not p.exists():
            print(f"  [SALTADO] Falta {p}")
            return

    with open(script_path, "r", encoding="utf-8") as f:
        job = json.load(f)

    images = [Path(p) for p in job["images"]]
    durations = job["image_durations"]
    missing = [str(p) for p in images if not p.exists()]
    if missing:
        sys.exit(f"Faltan imágenes: {', '.join(missing)}")

    narration_duration = ca.get_duration(audio_path)
    print(f"  Narración: {narration_duration:.2f}s, {len(images)} imagen(es)")

    tmp_ctx = tempfile.TemporaryDirectory(dir=".", prefix=f"_tmp_{stem}_")
    tmp_dir = Path(tmp_ctx.name)
    try:
        avatar_clip = resolve_avatar_clip(stem, audio_path, tmp_dir, force_sprites)

        print("  Generando subtítulos...")
        script_words = job["script_es"].split()
        whisper_words = ca.transcribe_words_whisper(audio_path)
        if whisper_words:
            print("  Subtítulos: usando faster-whisper para el timing real del audio.")
            word_timings = ca.align_script_to_timestamps(script_words, whisper_words)
        else:
            print("  [AVISO] faster-whisper no está instalado — el timing de los "
                  "subtítulos será proporcional, no exacto. Instálalo con: "
                  "pip install faster-whisper")
            word_timings = ca.proportional_fallback_timing(script_words, narration_duration, lead_in=1.0)
        ass_subs = ca.build_subtitles(word_timings, tmp_dir)

        print("  Componiendo vídeo final (imágenes + avatar + subtítulos + audio)...")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        build_slideshow(images, durations, narration_duration, ass_subs, output_path, audio_path,
                         title=job.get("title"), title_duration=job.get("title_duration"),
                         captions=job.get("captions"), avatar_clip=avatar_clip,
                         avatar_position=job.get("avatar_position") or ca.AVATAR_POSITION)

        print(f"  Guardado en {output_path}")
    finally:
        if keep_temp:
            print(f"  (temporales conservados en {tmp_dir})")
        else:
            tmp_ctx.cleanup()


def build_master_cues(audio_path: Path, job: dict, narration_duration: float) -> list:
    """Igual que ca.build_subtitles, pero devuelve la lista de cues (start,
    end, texto) en vez de escribir directamente el .ass — para poder
    recortarla y repartirla entre las dos partes al dividir un vídeo."""
    script_words = job["script_es"].split()
    whisper_words = ca.transcribe_words_whisper(audio_path)
    if whisper_words:
        print("  Subtítulos: usando faster-whisper para el timing real del audio.")
        word_timings = ca.align_script_to_timestamps(script_words, whisper_words)
    else:
        print("  [AVISO] faster-whisper no está instalado — el corte entre las dos partes "
              "será menos preciso (reparto proporcional). Instálalo con: pip install faster-whisper")
        word_timings = ca.proportional_fallback_timing(script_words, narration_duration, lead_in=1.0)
    return ca.group_into_cues(word_timings)


def cut_audio(src: Path, t0: float, t1: float, dst: Path) -> None:
    result = ca.run(["ffmpeg", "-y", "-ss", f"{t0:.3f}", "-i", str(src), "-t", f"{t1 - t0:.3f}", str(dst)])
    if result.returncode != 0:
        sys.exit(f"[ERROR ffmpeg] No se pudo cortar el audio: {result.stderr[-500:]}")


def split_and_compose(stem: str, split_at: int, force_sprites: bool = False, keep_temp: bool = False) -> None:
    """Divide un vídeo de imágenes ya generado en dos, cortando por la
    imagen `split_at` (0-indexed: cuántas imágenes van en la primera parte).
    Cada parte se compone como un vídeo independiente y completo: con su
    propio "Miau" de intro (recién añadido, no el de la narración original,
    que puede que ni lo tuviera), su propio avatar y sus propios subtítulos
    — recortados del timing real ya calculado por whisper sobre el audio
    completo, para no perder precisión al partir."""
    script_path = SCRIPT_DIR / f"{stem}.json"
    audio_path = AUDIO_DIR / f"{stem}.mp3"
    for p in (script_path, audio_path):
        if not p.exists():
            sys.exit(f"Falta {p}")

    with open(script_path, "r", encoding="utf-8") as f:
        job = json.load(f)

    images = job["images"]
    durations = job["image_durations"]
    captions = job.get("captions") or [None] * len(images)
    n = len(images)
    if not (0 < split_at < n):
        sys.exit(f"--split-at debe estar entre 1 y {n - 1} (hay {n} imágenes).")

    narration_duration = ca.get_duration(audio_path)
    split_time = sum(durations[:split_at])

    tmp_ctx = tempfile.TemporaryDirectory(dir=".", prefix=f"_tmp_split_{stem}_")
    tmp_dir = Path(tmp_ctx.name)
    try:
        master_cues = build_master_cues(audio_path, job, narration_duration)

        parts = [
            (f"{stem}_p1", 0, split_at, 0.0, split_time),
            (f"{stem}_p2", split_at, n, split_time, narration_duration),
        ]

        for part_stem, i0, i1, t0, t1 in parts:
            print(f"\n  --- {part_stem} ---")
            part_images = images[i0:i1]
            part_durations = list(durations[i0:i1])
            part_captions = captions[i0:i1]

            part_audio = AUDIO_DIR / f"{part_stem}.mp3"
            cut_audio(audio_path, t0, t1, part_audio)

            print("  Añadiendo el Miau de intro...")
            before_intro = ca.get_duration(part_audio)
            import text_to_speech
            text_to_speech.prepend_intro_clip(part_audio)
            intro_added = ca.get_duration(part_audio) - before_intro
            if intro_added > 0.05:
                part_durations[0] += intro_added

            # Cues que caen dentro de esta parte, recortadas al origen de la
            # parte y desplazadas lo que se ha alargado por el Miau.
            part_cues = []
            for start, end, text in master_cues:
                if end <= t0 or start >= t1:
                    continue
                s = max(start, t0) - t0 + intro_added
                e = min(end, t1) - t0 + intro_added
                part_cues.append((s, e, text))
            ass_path = tmp_dir / f"{part_stem}_subs.ass"
            ca.write_ass_file(part_cues, ass_path)

            if not force_sprites:
                print(f"  Animando el avatar con IA para {part_stem} (tarda varios minutos)...")
                result = ca.subprocess.run([sys.executable, "generate_avatar_replicate.py", f"--only={part_stem}"])
                if result.returncode != 0:
                    print(f"  Fallo animando el avatar de IA de {part_stem}, se usarán sprites en su lugar.")

            avatar_clip = resolve_avatar_clip(part_stem, part_audio, tmp_dir, force_sprites)

            part_narration_duration = ca.get_duration(part_audio)
            part_job = {
                "source": "images",
                "script_es": job.get("script_es", ""),
                "title": job.get("title"),
                "title_duration": job.get("title_duration"),
                "images": part_images,
                "image_durations": part_durations,
                "captions": part_captions,
                "split_from": stem,
            }
            with open(SCRIPT_DIR / f"{part_stem}.json", "w", encoding="utf-8") as f:
                json.dump(part_job, f, ensure_ascii=False, indent=2)

            print("  Componiendo vídeo final de esta parte...")
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            output_path = OUTPUT_DIR / f"{part_stem}.mp4"
            build_slideshow([Path(p) for p in part_images], part_durations, part_narration_duration,
                             ass_path, output_path, part_audio,
                             title=job.get("title"), title_duration=job.get("title_duration"),
                             captions=part_captions, avatar_clip=avatar_clip,
                             avatar_position=job.get("avatar_position") or ca.AVATAR_POSITION)
            print(f"  Guardado en {output_path}")
    finally:
        if keep_temp:
            print(f"  (temporales conservados en {tmp_dir})")
        else:
            tmp_ctx.cleanup()


def main():
    parser = argparse.ArgumentParser(description="Compone un vídeo a partir de imágenes + narración")
    parser.add_argument("--limit", type=int, default=None, help="Máximo de vídeos a procesar en esta pasada")
    parser.add_argument("--only", type=str, default=None, help="Procesa solo el id/stem indicado (ej. images_1234567890)")
    parser.add_argument("--keep-temp", action="store_true", help="No borra los ficheros temporales (útil para depurar)")
    parser.add_argument("--force-sprites", action="store_true",
                         help="Usa la animación local por sprites aunque ya exista un avatar de IA generado antes")
    parser.add_argument("--split-at", type=int, default=None,
                         help="En vez de componer normal, divide --only en dos vídeos: esta cantidad de "
                              "imágenes en la primera parte, el resto en la segunda")
    args = parser.parse_args()

    check_tools()

    if args.split_at is not None:
        if not args.only:
            sys.exit("--split-at necesita --only <stem>")
        split_and_compose(args.only, args.split_at, force_sprites=args.force_sprites, keep_temp=args.keep_temp)
        return

    if not SCRIPT_DIR.exists():
        sys.exit(f"No existe la carpeta {SCRIPT_DIR}.")

    log = load_log()
    processed_count = 0

    script_files = sorted(SCRIPT_DIR.glob("*.json"))
    if args.only:
        script_files = [p for p in script_files if p.stem == args.only]

    # Solo los jobs de este script (guiones "normales" de vídeo+avatar no
    # tienen la clave "images" y no deben procesarse aquí).
    jobs = []
    for p in script_files:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("source") == "images":
            jobs.append(p.stem)

    if not jobs:
        print("No hay vídeos de imágenes pendientes.")
        return

    for stem in jobs:
        if args.limit and processed_count >= args.limit:
            break

        key = f"{stem}.mp3"
        if key in log and log[key].get("status") == "ok" and not args.only:
            continue

        print(f"\n{stem}")
        try:
            process_one(stem, args.keep_temp, force_sprites=args.force_sprites)
            log[key] = {"status": "ok", "output": str(OUTPUT_DIR / f"{stem}.mp4")}
            processed_count += 1
        except SystemExit as e:
            print(f"  [ERROR] {e}")
            log[key] = {"status": "error", "error": str(e)}
            broken_output = OUTPUT_DIR / f"{stem}.mp4"
            broken_output.unlink(missing_ok=True)

    save_log(log)
    print(f"\nHecho. {processed_count} vídeo(s) compuestos en esta pasada.")

    if args.only and log.get(f"{args.only}.mp3", {}).get("status") == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
