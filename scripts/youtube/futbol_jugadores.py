"""
futbol_jugadores.py — Fotos de jugadores (PNG transparente + elipse de nota)
para los vídeos del gato puntuando partidos.

Todas las fotos salen con el MISMO formato que la foto de ejemplo de Pau
Cubarsí: lienzo de 670x790, fondo transparente, la cara siempre del mismo
tamaño y en el mismo sitio, y la misma elipse (assets/futbol/elipse.png)
pegada en el mismo punto de la cintura. Así luego se pueden colocar encima
del fondo del campo por coordenadas sin ajustar nada a mano.

Estructura (dentro de assets/futbol):
    config.json                 medidas comunes (lienzo, cara, elipse, nota)
    elipse.png                  capa de la elipse (se genera con "elipse")
    fondos/                     fondo del campo sobre el que irá la alineación
    escudos/                    escudos de los clubes (barca.png, madrid.png...)
    <equipo>/plantilla.json     jugadores de la temporada (id, nombre, dorsal...)
    <equipo>/originales/        fotos de partida, una por jugador: <id>.jpg/.png/.webp
    <equipo>/<id>.png           RESULTADO: jugador sin fondo + elipse vacía
    <equipo>/sin_elipse/<id>.png  el mismo jugador sin la elipse
    <equipo>/notas/<id>_<nota>.png  jugador con la nota escrita en la elipse

Uso (desde la carpeta scripts/youtube):
    python futbol_jugadores.py elipse
    python futbol_jugadores.py estado   --equipo barca
    python futbol_jugadores.py preparar --equipo barca [--jugador pedri] [--forzar]
    python futbol_jugadores.py nota     --equipo barca --jugador pedri --nota 7.5

Ajuste fino: si a un jugador la detección de cara lo deja un poco
desplazado, añade en su entrada de plantilla.json
    "ajuste": {"escala": 1.05, "dx": 0, "dy": -10}
y vuelve a ejecutar "preparar --jugador <id> --forzar".
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FUTBOL_DIR = Path(__file__).resolve().parent / "assets" / "futbol"
CONFIG_PATH = FUTBOL_DIR / "config.json"
ELIPSE_PATH = FUTBOL_DIR / "elipse.png"
EXTENSIONES = (".png", ".webp", ".jpg", ".jpeg")

# Se dibuja a 4x y se reduce para que el borde de la elipse salga suave.
SUPERSAMPLING = 4


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_plantilla(equipo: str) -> dict:
    path = FUTBOL_DIR / equipo / "plantilla.json"
    if not path.exists():
        sys.exit(f"No existe {path}. Crea la plantilla del equipo primero.")
    return json.loads(path.read_text(encoding="utf-8"))


def buscar_jugador(plantilla: dict, jugador_id: str) -> dict:
    for j in plantilla["jugadores"]:
        if j["id"] == jugador_id:
            return j
    ids = ", ".join(j["id"] for j in plantilla["jugadores"])
    sys.exit(f"Jugador '{jugador_id}' no está en la plantilla. Ids válidos: {ids}")


def buscar_original(equipo: str, jugador_id: str) -> Path | None:
    carpeta = FUTBOL_DIR / equipo / "originales"
    for ext in EXTENSIONES:
        path = carpeta / f"{jugador_id}{ext}"
        if path.exists():
            return path
    return None


# ----------------------------------------------------------------------------
# Elipse
# ----------------------------------------------------------------------------


def generar_elipse(config: dict) -> Image.Image:
    """Capa transparente del tamaño del lienzo con la elipse ya colocada en su
    sitio: basta con pegarla encima de cualquier jugador."""
    ancho, alto = config["lienzo"]["ancho"], config["lienzo"]["alto"]
    e = config["elipse"]
    s = SUPERSAMPLING
    capa = Image.new("RGBA", (ancho * s, alto * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(capa)
    caja = [
        (e["centro_x"] - e["radio_x"]) * s,
        (e["centro_y"] - e["radio_y"]) * s,
        (e["centro_x"] + e["radio_x"]) * s,
        (e["centro_y"] + e["radio_y"]) * s,
    ]
    draw.ellipse(
        caja,
        fill=tuple(e["color_relleno"]) + (255,),
        outline=tuple(e["color_borde"]) + (255,),
        width=e["grosor_borde"] * s,
    )
    return capa.resize((ancho, alto), Image.LANCZOS)


def cargar_elipse(config: dict) -> Image.Image:
    if not ELIPSE_PATH.exists():
        generar_elipse(config).save(ELIPSE_PATH)
        print(f"Elipse creada: {ELIPSE_PATH}")
    return Image.open(ELIPSE_PATH).convert("RGBA")


# ----------------------------------------------------------------------------
# Recorte del jugador
# ----------------------------------------------------------------------------

_rembg_session = None


def quitar_fondo(img: Image.Image) -> Image.Image:
    """Devuelve la imagen en RGBA sin fondo. Si ya viene con transparencia
    (p.ej. un PNG oficial recortado) se respeta tal cual."""
    img = img.convert("RGBA")
    alpha = np.array(img)[..., 3]
    if (alpha < 10).mean() > 0.05:
        return img

    global _rembg_session
    from rembg import new_session, remove

    if _rembg_session is None:
        # Modelo entrenado específicamente para recortar personas.
        _rembg_session = new_session("u2net_human_seg")
    return remove(img, session=_rembg_session).convert("RGBA")


def detectar_cara(img: Image.Image) -> tuple[int, int, int, int] | None:
    """(x, y, ancho, alto) de la cara más grande, o None si no encuentra."""
    import cv2

    rgb = np.array(img.convert("RGB"))
    gris = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    detector = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    lado_min = max(40, min(gris.shape) // 12)
    caras = detector.detectMultiScale(gris, 1.1, 5, minSize=(lado_min, lado_min))
    if len(caras) == 0:
        return None
    x, y, w, h = max(caras, key=lambda c: c[2] * c[3])
    return int(x), int(y), int(w), int(h)


def encuadre_sin_cara(recorte: Image.Image, config: dict) -> tuple[float, float, float]:
    """Plan B si no se detecta la cara: se estima su tamaño a partir del
    ancho de hombros (en la foto de ejemplo, hombros ≈ 2.45 x cara)."""
    alpha = np.array(recorte)[..., 3] > 128
    filas = np.nonzero(alpha.any(axis=1))[0]
    arriba, abajo = filas.min(), filas.max()
    zona = alpha[arriba: arriba + max(1, (abajo - arriba) // 2)]
    cols = np.nonzero(zona.any(axis=0))[0]
    ancho_cara = (cols.max() - cols.min()) / 2.45
    centro_x = (cols.min() + cols.max()) / 2
    # En la foto de ejemplo la cara empieza ~0.17 caras por debajo del pelo.
    arriba_cara = arriba + 0.17 * ancho_cara
    return ancho_cara, centro_x, arriba_cara


def encuadrar(recorte: Image.Image, config: dict, ajuste: dict) -> Image.Image:
    """Escala y coloca al jugador para que su cara quede con el mismo tamaño y
    en la misma posición que en la plantilla común."""
    cara = detectar_cara(recorte)
    if cara:
        x, y, w, _ = cara
        ancho_cara, centro_x, arriba_cara = w, x + w / 2, y
    else:
        print("   ⚠ No se detectó la cara; encuadre estimado por los hombros. Revísalo.")
        ancho_cara, centro_x, arriba_cara = encuadre_sin_cara(recorte, config)

    obj = config["cara"]
    escala = obj["ancho"] / ancho_cara * ajuste.get("escala", 1.0)
    if escala > 2.5:
        print(f"   ⚠ La foto original es pequeña (se amplía x{escala:.1f}); saldrá borrosa.")

    nuevo = recorte.resize(
        (round(recorte.width * escala), round(recorte.height * escala)), Image.LANCZOS
    )
    dx = round(obj["centro_x"] - centro_x * escala) + ajuste.get("dx", 0)
    dy = round(obj["arriba_y"] - arriba_cara * escala) + ajuste.get("dy", 0)

    lienzo = Image.new("RGBA", (config["lienzo"]["ancho"], config["lienzo"]["alto"]), (0, 0, 0, 0))
    lienzo.paste(nuevo, (dx, dy))  # lienzo vacío: copia directa, alfa incluido
    return lienzo


def preparar_jugador(equipo: str, jugador: dict, config: dict, elipse: Image.Image) -> bool:
    original = buscar_original(equipo, jugador["id"])
    if original is None:
        print(f" - {jugador['nombre']}: falta originales/{jugador['id']}.jpg|png|webp")
        return False

    print(f" - {jugador['nombre']} ({original.name})")
    recorte = quitar_fondo(Image.open(original))
    jugador_png = encuadrar(recorte, config, jugador.get("ajuste", {}))

    carpeta = FUTBOL_DIR / equipo
    (carpeta / "sin_elipse").mkdir(exist_ok=True)
    jugador_png.save(carpeta / "sin_elipse" / f"{jugador['id']}.png")
    Image.alpha_composite(jugador_png, elipse).save(carpeta / f"{jugador['id']}.png")
    return True


# ----------------------------------------------------------------------------
# Nota dentro de la elipse
# ----------------------------------------------------------------------------


def cargar_fuente(tamano: int) -> ImageFont.FreeTypeFont:
    candidatas = [
        "arialbd.ttf",  # Windows
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    for f in candidatas:
        try:
            return ImageFont.truetype(f, tamano)
        except OSError:
            continue
    return ImageFont.load_default(tamano)


def formatear_nota(nota: float) -> str:
    return f"{nota:g}".replace(".", ",")


def pintar_nota(img: Image.Image, nota: float, config: dict) -> Image.Image:
    img = img.copy()
    e, n = config["elipse"], config["nota"]
    draw = ImageDraw.Draw(img)
    draw.text(
        (e["centro_x"], e["centro_y"]),
        formatear_nota(nota),
        font=cargar_fuente(n["tamano"]),
        fill=tuple(n["color"]) + (255,),
        anchor="mm",
    )
    return img


# ----------------------------------------------------------------------------
# Comandos
# ----------------------------------------------------------------------------


def cmd_elipse(args, config):
    generar_elipse(config).save(ELIPSE_PATH)
    print(f"Elipse guardada en {ELIPSE_PATH}")


def cmd_estado(args, config):
    plantilla = load_plantilla(args.equipo)
    carpeta = FUTBOL_DIR / args.equipo
    print(f"{plantilla['equipo']} {plantilla['temporada']}")
    for j in plantilla["jugadores"]:
        hecho = (carpeta / f"{j['id']}.png").exists()
        original = buscar_original(args.equipo, j["id"])
        marca = "✅" if hecho else ("🟡 original listo" if original else "❌ falta foto")
        print(f"  {j['dorsal']:>2} {j['nombre']:<20} {marca}   (originales/{j['id']}.*)")


def cmd_preparar(args, config):
    plantilla = load_plantilla(args.equipo)
    elipse = cargar_elipse(config)
    jugadores = (
        [buscar_jugador(plantilla, args.jugador)] if args.jugador else plantilla["jugadores"]
    )
    hechos = 0
    for j in jugadores:
        destino = FUTBOL_DIR / args.equipo / f"{j['id']}.png"
        if destino.exists() and not args.forzar and not args.jugador:
            continue
        hechos += preparar_jugador(args.equipo, j, config, elipse)
    print(f"Listo: {hechos} foto(s) generada(s) en {FUTBOL_DIR / args.equipo}")


def cmd_nota(args, config):
    plantilla = load_plantilla(args.equipo)
    j = buscar_jugador(plantilla, args.jugador)
    base = FUTBOL_DIR / args.equipo / f"{j['id']}.png"
    if not base.exists():
        sys.exit(f"Primero genera la foto: python futbol_jugadores.py preparar --equipo {args.equipo} --jugador {j['id']}")
    salida = FUTBOL_DIR / args.equipo / "notas" / f"{j['id']}_{formatear_nota(args.nota)}.png"
    salida.parent.mkdir(exist_ok=True)
    pintar_nota(Image.open(base).convert("RGBA"), args.nota, config).save(salida)
    print(f"Guardado: {salida}")


def main():
    parser = argparse.ArgumentParser(description="Fotos de jugadores para los vídeos de notas.")
    sub = parser.add_subparsers(dest="comando", required=True)

    sub.add_parser("elipse", help="(Re)genera assets/futbol/elipse.png")

    p = sub.add_parser("estado", help="Qué jugadores tienen ya su foto")
    p.add_argument("--equipo", required=True)

    p = sub.add_parser("preparar", help="Quita el fondo, encuadra y pone la elipse")
    p.add_argument("--equipo", required=True)
    p.add_argument("--jugador", help="Solo este id (por defecto, todos los pendientes)")
    p.add_argument("--forzar", action="store_true", help="Rehacer también los ya generados")

    p = sub.add_parser("nota", help="Escribe una nota en la elipse de un jugador")
    p.add_argument("--equipo", required=True)
    p.add_argument("--jugador", required=True)
    p.add_argument("--nota", required=True, type=float)

    args = parser.parse_args()
    config = load_config()
    {"elipse": cmd_elipse, "estado": cmd_estado, "preparar": cmd_preparar, "nota": cmd_nota}[
        args.comando
    ](args, config)


if __name__ == "__main__":
    main()
