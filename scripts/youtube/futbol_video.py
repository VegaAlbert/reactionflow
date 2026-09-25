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
en la siguiente ejecución: si cambias el texto de un jugador, borra su
audio (o usa --rehacer-voz) para que se regenere.
"""

import argparse
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
NOTA_ANTES_DEL_FINAL = 0.9  # s antes de acabar su frase aparece la nota (la dice al final)
FINAL_EXTRA = 1.5           # s de imagen final tras la última palabra

# Gato: recorte del sprite (2048x2048) al área que ocupa el gato, y tamaño y
# margen en el vídeo final (abajo a la derecha, junto al portero).
GATO_RECORTE = (291, 335, 1460, 1383)  # x, y, ancho, alto
GATO_ANCHO = 370
GATO_MARGEN = 12


def run(cmd: list) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"[ERROR] {' '.join(map(str, cmd[:3]))}...: {result.stderr[-800:]}")


# ----------------------------------------------------------------------------
# 1) Voz
# ----------------------------------------------------------------------------


def generar_voz(frases: list[tuple[str, str]], carpeta: Path, prueba: bool, rehacer: bool) -> list[Path]:
    """Un audio por frase (clave, texto). Devuelve las rutas en el mismo orden."""
    carpeta.mkdir(parents=True, exist_ok=True)
    ext = "wav" if prueba else "mp3"
    rutas = [carpeta / f"{i:02d}_{clave}.{ext}" for i, (clave, _) in enumerate(frases)]
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
# 2) Imágenes de la alineación
# ----------------------------------------------------------------------------


def montar_imagenes(partido: dict, tramos: list, duracion: float, tmp: Path) -> Path:
    """Genera los fotogramas fijos y la lista para el demuxer concat de ffmpeg
    (imagen + cuánto dura en pantalla)."""
    notas = {k: v["nota"] for k, v in partido["jugadores"].items()}
    orden = partido.get("orden", partido["once"])
    base = {k: partido[k] for k in ("equipo", "formacion", "once", "marcador", "competicion") if k in partido}

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
        escena(f"{jugador}_a", orden[:i], jugador, max(inicio + 0.5, fin - NOTA_ANTES_DEL_FINAL))
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
# 3) Gato + composición final
# ----------------------------------------------------------------------------


def animar_gato(narracion: Path, tmp: Path) -> Path:
    envolvente = ca.compute_volume_envelope(narracion, FPS)
    timeline = ca.build_sprite_timeline(envolvente, FPS)
    lista = ca.write_concat_list(timeline, FPS, tmp)
    salida = tmp / "gato.mp4"
    ca.render_avatar_clip(lista, FPS, salida)
    return salida


def componer_video(escenas: Path, gato: Path, narracion: Path, salida: Path) -> None:
    x, y, w, h = GATO_RECORTE
    chroma = f"colorkey={ca.CHROMA_COLOR}:{ca.CHROMA_SIMILARITY}:{ca.CHROMA_BLEND}"
    if ca.USE_DESPILL:
        chroma += ",despill=type=green:mix=0.5:expand=0"
    filtro = (
        f"[0:v]fps={FPS},format=yuv420p[fondo];"
        f"[1:v]crop={w}:{h}:{x}:{y},{chroma},scale={GATO_ANCHO}:-1[gato];"
        f"[fondo][gato]overlay=W-w-{GATO_MARGEN}:H-h-{GATO_MARGEN}:shortest=1[v]"
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
    print("1/4 Voz del gato...")
    audios = generar_voz(frases, tmp / ("voz_prueba" if args.voz_prueba else "voz"), args.voz_prueba, args.rehacer_voz)
    narracion, tramos = montar_narracion(audios, tmp, con_miau=not args.sin_miau)
    duracion = tramos[-1][1] + FINAL_EXTRA
    print(f"   Duración: {duracion:.1f} s")

    print("2/4 Alineaciones con las notas...")
    escenas = montar_imagenes(partido, tramos, duracion, tmp)
    print("3/4 Animando al gato...")
    gato = animar_gato(narracion, tmp)
    print("4/4 Montando el vídeo...")
    componer_video(escenas, gato, narracion, salida)
    print(f"Vídeo listo: {salida}")


if __name__ == "__main__":
    main()
