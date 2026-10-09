"""
futbol_video.py — Vídeo del gato puntuando a los jugadores de un partido.

A partir del JSON de un partido (assets/futbol/<equipo>/partidos/*.json, con
el once, las notas y lo que dice el gato de cada jugador) genera un .mp4
vertical 1080x1920:

    1. Intro: la alineación con las elipses vacías y el marcador arriba.
    2. Jugador a jugador (en el "orden" del JSON): el jugador se resalta con
       un halo dorado mientras el gato lo comenta y, justo cuando dice la
       nota (al final de su frase), la nota aparece en su elipse.
    3. Cierre: todas las notas a la vista mientras el gato comenta el
       banquillo y se despide.

La voz es la del gato en ElevenLabs (mismas credenciales y ajustes que
text_to_speech.py) y la animación del gato es el mismo sprite-swap por
volumen de compose_avatar.py, recortado y colocado abajo a la derecha.

Uso (desde la carpeta scripts/youtube):
    python futbol_video.py assets/futbol/barca/partidos/2026-09-19_sevilla.json
    python futbol_video.py <partido.json> --voz-prueba    # espeak-ng, sin gastar ElevenLabs
    python futbol_video.py <partido.json> --rehacer-voz   # vuelve a pedir todos los audios

Los audios de cada frase se guardan en futbol_tmp/<partido>/ y se reutilizan
en la siguiente ejecución mientras el texto no cambie (así no se gasta
ElevenLabs de más al rehacer el vídeo).
"""

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# compose_avatar usa rutas relativas (./assets/avatar): trabajamos siempre
# desde la carpeta del script, se lance desde donde se lance.
BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

import compose_avatar as ca  # noqa: E402
import futbol_alineacion as fa  # noqa: E402

TMP_DIR = Path("./futbol_tmp")
OUTPUT_DIR = Path("./final")
INTRO_CLIPS_DIR = Path("./assets/gato_intros")

FPS = 30
SAMPLE_RATE = 44100
PAUSA_ENTRE_FRASES = 0.45   # s de silencio entre un jugador y el siguiente
NOTA_ANTES_DEL_FINAL = 0.9  # si no hay whisper: s antes de acabar la frase aparece la nota
FINAL_EXTRA = 1.5           # s de imagen final tras la última palabra

# Gato: tamaño y margen en el vídeo final (abajo a la derecha, junto al
# portero). El recorte del sprite al área que ocupa el gato se mide en las
# propias imágenes de assets/avatar (ver recorte_gato), así vale para sprites
# de cualquier tamaño.
GATO_ANCHO = 370

# Subtítulos: abajo a la izquierda, en la franja libre bajo el portero y al
# lado del gato (MarginR deja sitio al gato).
SUB_FUENTE = "Arial"
SUB_TAMANO = 58
SUB_MARGEN_IZQ = 30
SUB_MARGEN_DER = GATO_ANCHO + 40
SUB_MARGEN_ABAJO = 60
SUB_MAX_PALABRAS = 4
SUB_MAX_CARACTERES = 22
GATO_MARGEN = 12


def run(cmd: list) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        # Las líneas con la causa real suelen quedar lejos del final del log.
        causas = [l for l in result.stderr.splitlines() if any(
            k in l for k in ("Error", "Invalid", "No such", "not found", "Failed", "Unable")
        )]
        sys.exit(f"[ERROR] {' '.join(map(str, cmd[:3]))}...:\n" + "\n".join(causas[:8] or [result.stderr[-800:]]))


# ----------------------------------------------------------------------------
# 1) Voz
# ----------------------------------------------------------------------------


def generar_voz(frases: list[tuple[str, str]], carpeta: Path, prueba: bool, rehacer: bool) -> list[Path]:
    """Un audio por frase (clave, texto). Devuelve las rutas en el mismo orden."""
    carpeta.mkdir(parents=True, exist_ok=True)
    ext = "wav" if prueba else "mp3"
    # El nombre lleva un resumen del texto: si cambias lo que dice el gato de
    # un jugador, se genera un audio nuevo en vez de reutilizar el viejo.
    rutas = [
        carpeta / f"{i:02d}_{clave}_{hashlib.sha1(texto.encode('utf-8')).hexdigest()[:8]}.{ext}"
        for i, (clave, texto) in enumerate(frases)
    ]
    pendientes = [(r, t) for r, (_, t) in zip(rutas, frases) if rehacer or not r.exists()]
    if not pendientes:
        return rutas

    if prueba:
        if not shutil.which("espeak-ng"):
            sys.exit("--voz-prueba necesita espeak-ng instalado.")
        for ruta, texto in pendientes:
            run(["espeak-ng", "-v", "es", "-s", "165", "-w", str(ruta), texto])
        return rutas

    import text_to_speech as tts

    client, voice_id = tts.get_elevenlabs_client()
    for ruta, texto in pendientes:
        print(f"  Voz: {ruta.name}")
        if not tts.generate_speech(client, voice_id, texto, ruta):
            sys.exit(f"No se pudo generar la voz de {ruta.name}.")
    return rutas


def leer_como_array(ruta: Path, tmp: Path) -> np.ndarray:
    """Decodifica cualquier audio a mono 16 bits a SAMPLE_RATE."""
    wav = tmp / f"_dec_{ruta.stem}.wav"
    run(["ffmpeg", "-y", "-i", str(ruta), "-ac", "1", "-ar", str(SAMPLE_RATE), str(wav)])
    with wave.open(str(wav), "rb") as wf:
        datos = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    wav.unlink()
    return datos


def montar_narracion(audios: list[Path], tmp: Path, con_miau: bool) -> tuple[Path, list[tuple[float, float]]]:
    """Une todas las frases con una pausa entre medias. Devuelve el .wav y el
    (inicio, fin) en segundos de cada frase dentro de la narración."""
    silencio = np.zeros(int(PAUSA_ENTRE_FRASES * SAMPLE_RATE), dtype=np.int16)
    partes, tramos, t = [], [], 0.0

    miaus = sorted(INTRO_CLIPS_DIR.glob("*.mp3")) if con_miau and INTRO_CLIPS_DIR.exists() else []
    if miaus:
        miau = leer_como_array(random.choice(miaus), tmp)
        partes += [miau, silencio]
        t += (len(miau) + len(silencio)) / SAMPLE_RATE

    for ruta in audios:
        voz = leer_como_array(ruta, tmp)
        tramos.append((t, t + len(voz) / SAMPLE_RATE))
        partes += [voz, silencio]
        t += (len(voz) + len(silencio)) / SAMPLE_RATE

    partes.append(np.zeros(int(FINAL_EXTRA * SAMPLE_RATE), dtype=np.int16))
    salida = tmp / "narracion.wav"
    with wave.open(str(salida), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(np.concatenate(partes).tobytes())
    return salida, tramos


# ----------------------------------------------------------------------------
# 2) Tiempos de cada palabra: subtítulos y momento exacto de la nota
# ----------------------------------------------------------------------------


def tiempos_palabras(frases: list[tuple[str, str]], narracion: Path, tramos: list) -> list[list]:
    """Para cada frase, [(palabra, inicio, fin), ...] en segundos de la
    narración. Usa faster-whisper si está instalado (tiempos reales) y, si
    no, reparte el tiempo de cada frase según la longitud de las palabras."""
    whisper = ca.transcribe_words_whisper(narracion)
    if whisper is None:
        print("   [AVISO] faster-whisper no está instalado: subtítulos con tiempos aproximados.")
    resultado = []
    for (_, texto), (inicio, fin) in zip(frases, tramos):
        palabras = texto.split()
        tiempos = []
        if whisper:
            cerca = [w for w in whisper if inicio - 0.3 <= w[1] <= fin + 0.3]
            tiempos = ca.align_script_to_timestamps(palabras, cerca) if cerca else []
        if not tiempos:
            tiempos = [(w, a + inicio, b + inicio) for w, a, b in ca.proportional_fallback_timing(palabras, fin - inicio)]
        resultado.append(tiempos)
    return resultado


def momento_nota(tiempos: list, fin: float) -> float:
    """Cuándo dice el gato la nota: la frase acaba en "Un seis y medio.",
    así que es el inicio del último "un"."""
    for palabra, inicio, _ in reversed(tiempos):
        if ca.normalize_word(palabra) == "un":
            return max(0.0, inicio - 0.05)
    return fin - NOTA_ANTES_DEL_FINAL


def escribir_subtitulos(tiempos_por_frase: list[list], ruta: Path) -> Path:
    ca.SUB_MAX_WORDS_PER_CUE, ca.SUB_MAX_CHARS_PER_CUE = SUB_MAX_PALABRAS, SUB_MAX_CARACTERES
    cabecera = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{SUB_FUENTE},{SUB_TAMANO},&H00FFFFFF,&H000000FF,&H00101010,&H00000000,1,0,0,0,100,100,0,0,1,5,0,2,{SUB_MARGEN_IZQ},{SUB_MARGEN_DER},{SUB_MARGEN_ABAJO},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(cabecera)
        for tiempos in tiempos_por_frase:  # por frase, para no mezclar dos jugadores
            for inicio, fin, texto in ca.group_into_cues(tiempos):
                f.write(
                    f"Dialogue: 0,{ca.format_ass_timestamp(inicio)},{ca.format_ass_timestamp(fin)},"
                    f"Default,,0,0,0,,{texto.upper()}\n"
                )
    return ruta


# ----------------------------------------------------------------------------
# 3) Imágenes de la alineación
# ----------------------------------------------------------------------------


def montar_imagenes(partido: dict, tramos: list, notas_en: list[float], duracion: float, tmp: Path) -> Path:
    """Genera los fotogramas fijos y la lista para el demuxer concat de ffmpeg
    (imagen + cuánto dura en pantalla)."""
    notas = {k: v["nota"] for k, v in partido["jugadores"].items()}
    orden = partido.get("orden", partido["once"])
    base = {k: partido[k] for k in ("equipo", "formacion", "once", "marcador", "competicion", "extras") if k in partido}

    escenas = []  # (png, hasta_segundo)

    def escena(nombre: str, visibles: list[str], destacado: str | None, hasta: float) -> None:
        ruta = tmp / f"{len(escenas):02d}_{nombre}.png"
        datos = {**base, "notas": {j: notas[j] for j in visibles}}
        fa.componer(datos, destacado=destacado).convert("RGB").save(ruta)
        escenas.append((ruta, hasta))

    # tramos[0] = intro, tramos[1..n] = jugadores, tramos[-1] = cierre
    escena("intro", [], None, tramos[1][0])
    for i, jugador in enumerate(orden):
        inicio, fin = tramos[i + 1]
        siguiente = tramos[i + 2][0]
        escena(f"{jugador}_a", orden[:i], jugador, min(fin, max(inicio + 0.5, notas_en[i])))
        escena(f"{jugador}_b", orden[: i + 1], jugador, siguiente)
    escena("final", orden, None, duracion)

    lista = tmp / "escenas.txt"
    with open(lista, "w", encoding="utf-8") as f:
        t = 0.0
        for ruta, hasta in escenas:
            f.write(f"file '{ruta.resolve().as_posix()}'\nduration {hasta - t:.3f}\n")
            t = hasta
        f.write(f"file '{escenas[-1][0].resolve().as_posix()}'\n")
    return lista


# ----------------------------------------------------------------------------
# 4) Gato + composición final
# ----------------------------------------------------------------------------


def animar_gato(narracion: Path, tmp: Path) -> Path:
    envolvente = ca.compute_volume_envelope(narracion, FPS)
    timeline = ca.build_sprite_timeline(envolvente, FPS)
    lista = ca.write_concat_list(timeline, FPS, tmp)
    salida = tmp / "gato.mp4"
    ca.render_avatar_clip(lista, FPS, salida)
    return salida


def recorte_gato() -> str:
    """Filtro crop de ffmpeg con el rectángulo que ocupa el gato en sus
    sprites (unión de todos, sin el fondo verde), en proporción al tamaño de
    la imagen (iw/ih) para que no dependa de la resolución de los sprites."""
    from PIL import Image

    # Solo los sprites que usa la animación (gato_perfil.png no, y además
    # tiene sombras en el fondo verde).
    usados = {v for k, v in vars(ca).items() if k.startswith("SPRITE_") and "PERFIL" not in k and isinstance(v, str)}
    caja = None
    for sprite in sorted(ca.AVATAR_DIR / u for u in usados if (ca.AVATAR_DIR / u).exists()):
        img = np.asarray(Image.open(sprite).convert("RGB")).astype(int)
        verde = (img[..., 1] > 150) & (img[..., 0] < 120) & (img[..., 2] < 120)
        ys, xs = np.nonzero(~verde)
        if xs.size == 0:
            continue
        alto, ancho = verde.shape
        c = (xs.min() / ancho, ys.min() / alto, (xs.max() + 1) / ancho, (ys.max() + 1) / alto)
        caja = c if caja is None else (min(caja[0], c[0]), min(caja[1], c[1]), max(caja[2], c[2]), max(caja[3], c[3]))
    if caja is None:
        return "null"
    margen = 0.01
    x0, y0 = max(0.0, caja[0] - margen), max(0.0, caja[1] - margen)
    x1, y1 = min(1.0, caja[2] + margen), min(1.0, caja[3] + margen)
    return f"crop=iw*{x1 - x0:.4f}:ih*{y1 - y0:.4f}:iw*{x0:.4f}:ih*{y0:.4f}"


def componer_video(escenas: Path, gato: Path, narracion: Path, subtitulos: Path, salida: Path) -> None:
    chroma = f"colorkey={ca.CHROMA_COLOR}:{ca.CHROMA_SIMILARITY}:{ca.CHROMA_BLEND}"
    if ca.USE_DESPILL:
        chroma += ",despill=type=green:mix=0.5:expand=0"
    filtro = (
        f"[0:v]fps={FPS},format=yuv420p[fondo];"
        f"[1:v]{recorte_gato()},{chroma},scale={GATO_ANCHO}:-2[gato];"
        f"[fondo][gato]overlay=W-w-{GATO_MARGEN}:H-h-{GATO_MARGEN}:shortest=1[comp];"
        f"[comp]subtitles='{ca.escape_for_filter(subtitulos.resolve())}'[v]"
    )
    run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(escenas),
        "-i", str(gato),
        "-i", str(narracion),
        "-filter_complex", filtro,
        "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-shortest",
        str(salida),
    ])


def main():
    parser = argparse.ArgumentParser(description="Vídeo del gato puntuando un partido.")
    parser.add_argument("partido", type=Path, help="JSON del partido (notas y textos del gato)")
    parser.add_argument("--salida", type=Path, help="MP4 de salida (por defecto final/futbol_<partido>.mp4)")
    parser.add_argument("--voz-prueba", action="store_true", help="Voz robótica local (espeak-ng) para probar sin ElevenLabs")
    parser.add_argument("--rehacer-voz", action="store_true", help="Regenera todos los audios aunque existan")
    parser.add_argument("--sin-miau", action="store_true", help="No poner un 'Miau' de assets/gato_intros al principio")
    args = parser.parse_args()

    partido_path = args.partido if args.partido.is_absolute() else Path.cwd() / args.partido
    partido = json.loads(partido_path.read_text(encoding="utf-8"))
    orden = partido.get("orden", partido["once"])
    faltan = [j for j in orden if j not in partido["jugadores"]]
    if faltan:
        sys.exit(f"Faltan nota y texto de: {', '.join(faltan)}")

    nombre = partido_path.stem
    tmp = TMP_DIR / nombre
    tmp.mkdir(parents=True, exist_ok=True)
    salida = args.salida or OUTPUT_DIR / f"futbol_{nombre}.mp4"
    salida.parent.mkdir(parents=True, exist_ok=True)

    frases = [("intro", partido["intro"])]
    frases += [(j, partido["jugadores"][j]["texto"]) for j in orden]
    frases.append(("cierre", partido["cierre"]))

    print(f"{partido.get('partido', nombre)}: {len(frases)} frases")
    print("1/5 Voz del gato...")
    audios = generar_voz(frases, tmp / ("voz_prueba" if args.voz_prueba else "voz"), args.voz_prueba, args.rehacer_voz)
    narracion, tramos = montar_narracion(audios, tmp, con_miau=not args.sin_miau)
    duracion = tramos[-1][1] + FINAL_EXTRA
    print(f"   Duración: {duracion:.1f} s")

    print("2/5 Subtítulos (whisper)...")
    tiempos = tiempos_palabras(frases, narracion, tramos)
    subtitulos = escribir_subtitulos(tiempos, tmp / "subtitulos.ass")
    notas_en = [momento_nota(t, fin) for t, (_, fin) in zip(tiempos[1:-1], tramos[1:-1])]
    print("3/5 Alineaciones con las notas...")
    escenas = montar_imagenes(partido, tramos, notas_en, duracion, tmp)
    print("4/5 Animando al gato...")
    gato = animar_gato(narracion, tmp)
    print("5/5 Montando el vídeo...")
    componer_video(escenas, gato, narracion, subtitulos, salida)
    print(f"Vídeo listo: {salida}")


if __name__ == "__main__":
    main()
