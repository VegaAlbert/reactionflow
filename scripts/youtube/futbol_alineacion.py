"""
futbol_alineacion.py — Coloca una alineación encima del fondo del campo.

Lee un JSON con el once (y, opcionalmente, las notas de cada jugador) y
genera una imagen vertical 1080x1920 para TikTok con el escudo del club,
las fotos de los jugadores de assets/futbol/<equipo>/ y su nombre debajo.
Si un jugador tiene nota, se escribe dentro de su elipse; si no, la elipse
sale vacía (así el vídeo puede ir mostrando la nota jugador a jugador).

Fondo: la primera imagen que haya en assets/futbol/fondos/ (png, jpg, webp
o avif), recortada para llenar 1080x1920. Por defecto es fondos/campo.png,
el campo verde que dibuja este script (python futbol_alineacion.py --campo
lo regenera). Si la carpeta está vacía se dibuja al vuelo.

Ejemplo de alineacion.json:
    {
      "equipo": "barca",
      "formacion": "4-3-3",
      "once": ["joan_garcia",
               "alex_balde", "pau_cubarsi", "eric_garcia", "jules_kounde",
               "pedri", "frenkie_de_jong", "fermin_lopez",
               "raphinha", "gabriel_jesus", "lamine_yamal"],
      "notas": {"pedri": 8, "lamine_yamal": 9.5}
    }
Los jugadores van del portero a la delantera y, dentro de cada línea, de
izquierda a derecha tal como se verán en pantalla.

Uso (desde la carpeta scripts/youtube):
    python futbol_alineacion.py alineacion.json [--salida final/alineacion.png]
    python futbol_alineacion.py alineacion.json --sin-notas
"""

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

import futbol_jugadores as fj

ANCHO, ALTO = 1080, 1920
FONDOS_DIR = fj.FUTBOL_DIR / "fondos"
ESCUDOS_DIR = fj.FUTBOL_DIR / "escudos"

# Centro vertical de cada línea (portero abajo, delantera arriba) según
# cuántas líneas tenga la formación (sin contar al portero).
FILAS_Y = {
    3: [1640, 1235, 830, 425],
    4: [1660, 1310, 1000, 690, 380],
}
ANCHO_MAX_JUGADOR = 270
MARGEN_LATERAL = 20


def cargar_fondo() -> Image.Image:
    fondos = sorted(
        p for p in FONDOS_DIR.iterdir()
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".avif")
    ) if FONDOS_DIR.exists() else []
    if fondos:
        img = Image.open(fondos[0]).convert("RGBA")
        return ImageOps.fit(img, (ANCHO, ALTO), Image.LANCZOS)
    return dibujar_campo()


def dibujar_campo() -> Image.Image:
    img = Image.new("RGBA", (ANCHO, ALTO), (46, 160, 67, 255))
    d = ImageDraw.Draw(img)
    for i in range(0, ALTO, 160):
        if (i // 160) % 2:
            d.rectangle([0, i, ANCHO, i + 160], fill=(40, 146, 60, 255))
    blanco, g = (235, 245, 235, 255), 6
    d.rectangle([40, 60, ANCHO - 40, ALTO - 60], outline=blanco, width=g)
    d.line([40, ALTO // 2, ANCHO - 40, ALTO // 2], fill=blanco, width=g)
    d.ellipse([ANCHO // 2 - 150, ALTO // 2 - 150, ANCHO // 2 + 150, ALTO // 2 + 150], outline=blanco, width=g)
    for y0, y1 in ((60, 330), (ALTO - 330, ALTO - 60)):
        d.rectangle([220, y0, ANCHO - 220, y1], outline=blanco, width=g)
    for y0, y1 in ((60, 150), (ALTO - 150, ALTO - 60)):
        d.rectangle([370, y0, ANCHO - 370, y1], outline=blanco, width=g)
    return img


def partir_en_lineas(once: list[str], formacion: str) -> list[list[str]]:
    tamanos = [1] + [int(n) for n in formacion.split("-")]
    if sum(tamanos) != len(once):
        sys.exit(f"La formación {formacion} necesita {sum(tamanos)} jugadores y hay {len(once)}.")
    lineas, i = [], 0
    for n in tamanos:
        lineas.append(once[i: i + n])
        i += n
    return lineas


def etiqueta_nombre(nombre: str, ancho_max: int) -> Image.Image:
    fuente = fj.cargar_fuente(30)
    caja = fuente.getbbox(nombre)
    w, h = caja[2] - caja[0] + 24, caja[3] - caja[1] + 16
    while w > ancho_max + 30 and fuente.size > 18:
        fuente = fj.cargar_fuente(fuente.size - 2)
        caja = fuente.getbbox(nombre)
        w, h = caja[2] - caja[0] + 24, caja[3] - caja[1] + 16
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=8, fill=(18, 90, 45, 225))
    d.text((w / 2, h / 2), nombre, font=fuente, fill="white", anchor="mm")
    return img


def componer(datos: dict, con_notas: bool = True) -> Image.Image:
    config = fj.load_config()
    equipo = datos["equipo"]
    plantilla = fj.load_plantilla(equipo)
    notas = datos.get("notas", {}) if con_notas else {}
    lineas = partir_en_lineas(datos["once"], datos.get("formacion", "4-3-3"))
    filas_y = FILAS_Y.get(len(lineas) - 1)
    if filas_y is None:
        sys.exit("Solo se admiten formaciones de 3 o 4 líneas (p.ej. 4-3-3 o 4-2-3-1).")

    lienzo = cargar_fondo()
    for linea, cy in zip(lineas, filas_y):
        n = len(linea)
        w = min(ANCHO_MAX_JUGADOR, (ANCHO - 2 * MARGEN_LATERAL) // n)
        h = round(w * config["lienzo"]["alto"] / config["lienzo"]["ancho"])
        hueco = (ANCHO - 2 * MARGEN_LATERAL - n * w) / (n + 1)
        for k, jugador_id in enumerate(linea):
            j = fj.buscar_jugador(plantilla, jugador_id)
            ruta = fj.FUTBOL_DIR / equipo / f"{jugador_id}.png"
            if not ruta.exists():
                sys.exit(f"Falta la foto {ruta}. Ejecuta futbol_jugadores.py preparar --equipo {equipo}.")
            foto = Image.open(ruta).convert("RGBA")
            if jugador_id in notas:
                foto = fj.pintar_nota(foto, float(notas[jugador_id]), config)
            foto = foto.resize((w, h), Image.LANCZOS)
            x = round(MARGEN_LATERAL + hueco * (k + 1) + w * k)
            y = cy - h // 2
            lienzo.alpha_composite(foto, (x, y))
            nombre = etiqueta_nombre(j["nombre"], w)
            lienzo.alpha_composite(nombre, (x + (w - nombre.width) // 2, y + h + 4))

    escudo_path = ESCUDOS_DIR / f"{equipo}.png"
    if escudo_path.exists():
        escudo = Image.open(escudo_path).convert("RGBA")
        escudo.thumbnail((170, 170), Image.LANCZOS)
        lienzo.alpha_composite(escudo, (40, 40))
    return lienzo


def main():
    parser = argparse.ArgumentParser(description="Alineación sobre el fondo del campo.")
    parser.add_argument("json", type=Path, nargs="?", help="Archivo con equipo, formación, once y notas")
    parser.add_argument("--campo", action="store_true", help="(Re)genera fondos/campo.png y sale")
    parser.add_argument("--salida", type=Path, help="PNG de salida (por defecto, junto al JSON)")
    parser.add_argument("--sin-notas", action="store_true", help="Elipses vacías aunque haya notas")
    args = parser.parse_args()

    if args.campo:
        FONDOS_DIR.mkdir(parents=True, exist_ok=True)
        dibujar_campo().convert("RGB").save(FONDOS_DIR / "campo.png")
        print(f"Fondo guardado en {FONDOS_DIR / 'campo.png'}")
        return
    if args.json is None:
        parser.error("falta el archivo JSON de la alineación")

    datos = json.loads(args.json.read_text(encoding="utf-8"))
    salida = args.salida or args.json.with_suffix(".png")
    salida.parent.mkdir(parents=True, exist_ok=True)
    componer(datos, con_notas=not args.sin_notas).convert("RGB").save(salida)
    print(f"Alineación guardada en {salida}")


if __name__ == "__main__":
    main()
