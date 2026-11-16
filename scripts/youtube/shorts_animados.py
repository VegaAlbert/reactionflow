"""
shorts_animados.py — Packs de vídeos cortos animados (p.ej. GTA 6) a partir de
un guiones.json: voz del gato, fondo animado, textos en movimiento,
subtítulos y, si se quiere, el gato en la esquina.

Cada pack es una carpeta dentro de shorts/ con un guiones.json (ver
shorts/gta6/guiones.json). Cada vídeo es una lista de escenas, y cada escena
tiene lo que dice el gato ("voz") y qué se ve mientras lo dice ("tipo"):

    titular   texto grande + subtítulo       {"texto", "sub"}
    dato      cifra o palabra gigante        {"grande", "texto"}  (los números cuentan hacia arriba)
    lista     puntos que van apareciendo     {"titulo", "items", "marca": "si"|"no"}
    comparar  dos tarjetas enfrentadas       {"izq": {...}, "der": {...}} con "titulo", "grande", "nota"
    cita      frase que se escribe sola      {"texto"}
    cierre    pregunta final + "comenta"     {"texto"}

Cada escena dura lo que tarda el gato en decir su "voz". Los fondos se
dibujan por código (atardecer, noche, playa): no se usan imágenes de nadie.

Uso (desde la carpeta scripts/youtube; normalmente desde menu.py, opción 9):
    python shorts_animados.py shorts/gta6
    python shorts_animados.py shorts/gta6 --solo 01_hora
    python shorts_animados.py shorts/gta6 --voz-prueba   # voz robótica local, sin gastar ElevenLabs

Salida: final/<pack>/<id>.mp4 y final/<pack>/publicar.txt (títulos,
descripciones y hashtags listos para copiar).
"""

import argparse
import json
import math
import random
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import futbol_jugadores as fj
import futbol_video as fv  # hace chdir a la carpeta del script

W, H, FPS = 1080, 1920, 30
PAN = 220  # px que se desplaza el fondo hacia arriba a lo largo del vídeo
TMP_DIR = Path("./shorts_tmp")
FUENTE_TITULAR = Path("./assets/fonts/Anton-Regular.ttf")

# Zona útil: TikTok/Shorts tapan los bordes (arriba el menú, abajo el texto
# de la publicación y a la derecha los botones), así que el contenido va en
# el centro y los subtítulos un poco por encima del borde inferior.
CENTRO_Y = 860
SUB_ABAJO_SIN_GATO = 300
SUB_ABAJO_CON_GATO = 420

PALETAS = {
    "atardecer": {
        "cielo": [(34, 8, 64), (120, 24, 112), (236, 84, 110), (255, 170, 104)],
        "sol": [(255, 222, 120), (255, 92, 150)],
        "mar": [(48, 16, 82), (14, 6, 34)],
        "acento": (255, 64, 170),
        "acento2": (70, 230, 255),
    },
    "noche": {
        "cielo": [(6, 6, 26), (24, 12, 70), (92, 26, 128), (196, 54, 150)],
        "sol": [(255, 120, 220), (120, 70, 255)],
        "mar": [(22, 10, 58), (4, 4, 18)],
        "acento": (70, 230, 255),
        "acento2": (255, 64, 170),
    },
    "playa": {
        "cielo": [(16, 52, 112), (44, 150, 196), (250, 160, 150), (255, 214, 150)],
        "sol": [(255, 246, 180), (255, 150, 110)],
        "mar": [(18, 96, 132), (6, 34, 58)],
        "acento": (255, 84, 150),
        "acento2": (255, 226, 90),
    },
}

BLANCO = (255, 255, 255)
OSCURO = (12, 8, 26)


# ----------------------------------------------------------------------------
# Utilidades de dibujo
# ----------------------------------------------------------------------------


def fuente_titular(tam: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FUENTE_TITULAR), tam)
    except OSError:
        return fj.cargar_fuente(tam)


def fuente_texto(tam: int) -> ImageFont.FreeTypeFont:
    return fj.cargar_fuente(tam)


def ease_out(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return 1 - (1 - x) ** 3


def ease_back(x: float) -> float:
    """Pasa un poco de 1 y vuelve: efecto 'pop'."""
    x = min(1.0, max(0.0, x))
    c = 1.70158
    return 1 + (c + 1) * (x - 1) ** 3 + c * (x - 1) ** 2


def degradado(ancho: int, alto: int, paradas: list) -> Image.Image:
    ys = np.linspace(0, 1, alto)[:, None]
    n = len(paradas) - 1
    idx = np.minimum((ys * n).astype(int), n - 1)
    frac = ys * n - idx
    c = np.array(paradas, dtype=float)
    fila = c[idx[:, 0]] * (1 - frac) + c[idx[:, 0] + 1] * frac
    arr = np.repeat(fila[:, None, :], ancho, axis=1)
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def ajustar_lineas(texto: str, fuente, ancho_max: int) -> list[str]:
    palabras, lineas, actual = texto.split(), [], ""
    for p in palabras:
        prueba = f"{actual} {p}".strip()
        if fuente.getlength(prueba) <= ancho_max or not actual:
            actual = prueba
        else:
            lineas.append(actual)
            actual = p
    if actual:
        lineas.append(actual)
    return lineas


def capa_texto(texto: str, fuente, color=BLANCO, ancho_max: int = 940, brillo=None,
               interlineado: float = 1.08, contorno: int = 0) -> Image.Image:
    """Texto (varias líneas, centrado) en una capa RGBA ajustada, con brillo
    de neón opcional detrás."""
    lineas = ajustar_lineas(texto, fuente, ancho_max)
    asc, desc = fuente.getmetrics()
    alto_linea = int((asc + desc) * interlineado)
    ancho = int(max(fuente.getlength(l) for l in lineas)) + 80 + 2 * contorno
    alto = alto_linea * len(lineas) + 80 + 2 * contorno
    capa = Image.new("RGBA", (ancho, alto), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    for i, l in enumerate(lineas):
        d.text((ancho / 2, 40 + contorno + i * alto_linea), l, font=fuente, fill=color + (255,),
               anchor="ma", stroke_width=contorno, stroke_fill=OSCURO + (255,))
    if brillo:
        halo = Image.new("RGBA", capa.size, brillo + (0,))
        halo.putalpha(capa.getchannel("A").filter(ImageFilter.GaussianBlur(14)).point(lambda a: min(255, a * 2)))
        capa = Image.alpha_composite(halo, capa)
    return capa


def ajustar_fuente(texto: str, ancho_max: int, tam_max: int, tam_min: int = 60):
    tam = tam_max
    while tam > tam_min and fuente_titular(tam).getlength(texto) > ancho_max:
        tam -= 6
    return fuente_titular(tam)


def panel(ancho: int, alto: int, borde=None, radio: int = 36, opacidad: int = 150) -> Image.Image:
    capa = Image.new("RGBA", (ancho, alto), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    d.rounded_rectangle([0, 0, ancho - 1, alto - 1], radius=radio, fill=OSCURO + (opacidad,),
                        outline=(borde + (255,)) if borde else None, width=5 if borde else 0)
    if borde:
        halo = Image.new("RGBA", capa.size, borde + (0,))
        aro = Image.new("L", capa.size, 0)
        ImageDraw.Draw(aro).rounded_rectangle([0, 0, ancho - 1, alto - 1], radius=radio, outline=200, width=10)
        halo.putalpha(aro.filter(ImageFilter.GaussianBlur(8)))
        capa = Image.alpha_composite(halo, capa)
    return capa


# ----------------------------------------------------------------------------
# Fondo: atardecer retro con sol, ciudad, mar y palmeras (dibujado por código)
# ----------------------------------------------------------------------------


def palmera(d: ImageDraw.ImageDraw, base_x: int, base_y: int, altura: int, inclinacion: float, color, rnd):
    puntos = []
    for i in range(41):
        t = i / 40
        x = base_x + inclinacion * altura * t ** 1.6
        y = base_y - altura * t
        puntos.append((x, y))
    for i in range(len(puntos) - 1):
        grosor = int(34 - 20 * i / len(puntos))
        d.line([puntos[i], puntos[i + 1]], fill=color, width=grosor)
    cx, cy = puntos[-1]
    for k in range(9):
        ang = math.radians(-170 + k * 40 + rnd.uniform(-8, 8))
        largo = altura * rnd.uniform(0.32, 0.46)
        hoja = []
        for j in range(21):
            t = j / 20
            caida = 0.55 * largo * t ** 2
            px = cx + math.cos(ang) * largo * t
            py = cy + math.sin(ang) * largo * t + caida
            hoja.append((px, py))
        arriba = [(x, y - 26 * math.sin(math.pi * j / 20)) for j, (x, y) in enumerate(hoja)]
        abajo = [(x, y + 10 * math.sin(math.pi * j / 20)) for j, (x, y) in enumerate(hoja)]
        d.polygon(arriba + abajo[::-1], fill=color)


def dibujar_fondo(nombre: str) -> Image.Image:
    pal = PALETAS[nombre]
    rnd = random.Random(nombre)
    alto = H + PAN
    horizonte = int(alto * 0.66)
    img = degradado(W, alto, pal["cielo"]).convert("RGBA")
    d = ImageDraw.Draw(img)

    # Estrellas tenues arriba
    for _ in range(140):
        x, y = rnd.randint(0, W), rnd.randint(0, int(horizonte * 0.55))
        a = rnd.randint(60, 180)
        d.ellipse([x, y, x + 3, y + 3], fill=(255, 255, 255, a))

    # Sol retro con franjas
    r, cx, cy = 330, W // 2, horizonte - 120
    sol = degradado(2 * r, 2 * r, pal["sol"]).convert("RGBA")
    mascara = Image.new("L", (2 * r, 2 * r), 0)
    md = ImageDraw.Draw(mascara)
    md.ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)
    for k in range(7):
        y0 = int(r * 1.05 + k * k * 7 + k * 16)
        md.rectangle([0, y0, 2 * r, y0 + 6 + k * 3], fill=0)
    sol.putalpha(mascara)
    brillo_sol = Image.new("RGBA", (2 * r + 240, 2 * r + 240), pal["sol"][1] + (0,))
    bm = Image.new("L", brillo_sol.size, 0)
    ImageDraw.Draw(bm).ellipse([120, 120, 2 * r + 120, 2 * r + 120], fill=140)
    brillo_sol.putalpha(bm.filter(ImageFilter.GaussianBlur(60)))
    img.alpha_composite(brillo_sol, (cx - r - 120, cy - r - 120))
    img.alpha_composite(sol, (cx - r, cy - r))

    # Skyline
    ciudad = Image.new("RGBA", (W, alto), (0, 0, 0, 0))
    cd = ImageDraw.Draw(ciudad)
    x = -20
    oscuro = tuple(int(c * 0.35) for c in pal["mar"][0])
    while x < W:
        ancho = rnd.randint(40, 120)
        alto_ed = rnd.randint(60, 380) if abs(x - W / 2) > 200 else rnd.randint(40, 160)
        cd.rectangle([x, horizonte - alto_ed, x + ancho, horizonte], fill=oscuro + (255,))
        for wy in range(horizonte - alto_ed + 14, horizonte - 10, 22):
            for wx in range(x + 8, x + ancho - 8, 18):
                if rnd.random() < 0.18:
                    cd.rectangle([wx, wy, wx + 6, wy + 8], fill=pal["acento2"] + (150,))
        x += ancho + rnd.randint(0, 14)
    img.alpha_composite(ciudad)

    # Mar con reflejos del sol
    mar = degradado(W, alto - horizonte, pal["mar"]).convert("RGBA")
    img.alpha_composite(mar, (0, horizonte))
    reflejo = Image.new("RGBA", (W, alto - horizonte), (0, 0, 0, 0))
    rd = ImageDraw.Draw(reflejo)
    for k in range(26):
        y = int(10 + k * k * 1.3 + k * 10)
        media = int(260 * (1 - k / 30)) + rnd.randint(-30, 30)
        a = int(200 * (1 - k / 26))
        rd.rectangle([cx - media, y, cx + media, y + 4 + k // 6], fill=pal["sol"][0] + (a,))
    img.alpha_composite(reflejo.filter(ImageFilter.GaussianBlur(1.5)), (0, horizonte))

    # Palmeras en primer plano (siluetas)
    plano = Image.new("RGBA", (W, alto), (0, 0, 0, 0))
    pd = ImageDraw.Draw(plano)
    sil = (10, 4, 20, 255)
    palmera(pd, -40, alto + 40, 1250, 0.32, sil, rnd)
    palmera(pd, 90, alto + 60, 900, 0.18, sil, rnd)
    palmera(pd, W + 40, alto + 40, 1150, -0.30, sil, rnd)
    img.alpha_composite(plano)

    # Viñeta para que el texto se lea
    vin = Image.new("L", (W, alto), 0)
    ImageDraw.Draw(vin).ellipse([-W * 0.35, -alto * 0.15, W * 1.35, alto * 1.15], fill=255)
    vin = vin.filter(ImageFilter.GaussianBlur(160))
    oscuridad = Image.new("RGBA", (W, alto), (0, 0, 0, 255))
    oscuridad.putalpha(vin.point(lambda v: int((255 - v) * 0.75)))
    img.alpha_composite(oscuridad)
    return img.convert("RGB")


# ----------------------------------------------------------------------------
# Escenas: elementos animados
# ----------------------------------------------------------------------------


class Elemento:
    """Capa RGBA con su posición final (centro) y una animación de entrada."""

    def __init__(self, capa, cx, cy, t_in, anim="subir", dur=0.45):
        self.capa, self.cx, self.cy, self.t_in, self.anim, self.dur = capa, cx, cy, t_in, anim, dur

    def pintar(self, lienzo: Image.Image, t: float):
        if t < self.t_in:
            return
        p = (t - self.t_in) / self.dur
        capa, dx, dy, alfa = self.capa, 0, 0, ease_out(p)
        if self.anim == "subir":
            dy = int(90 * (1 - ease_out(p)))
        elif self.anim == "izquierda":
            dx = int(-700 * (1 - ease_out(p)))
        elif self.anim == "derecha":
            dx = int(700 * (1 - ease_out(p)))
        elif self.anim == "pop" and p < 1:
            s = max(0.05, 0.55 + 0.45 * ease_back(p))
            capa = capa.resize((max(1, int(capa.width * s)), max(1, int(capa.height * s))), Image.BILINEAR)
            alfa = min(1.0, p * 3)
        if alfa < 1:
            capa = capa.copy()
            capa.putalpha(capa.getchannel("A").point([int(v * alfa) for v in range(256)]))
        lienzo.alpha_composite(capa, (int(self.cx - capa.width / 2 + dx), int(self.cy - capa.height / 2 + dy)))


class Escena:
    def __init__(self, datos: dict, pal: dict, duracion: float):
        self.d, self.pal, self.dur = datos, pal, duracion
        self.elementos: list[Elemento] = []
        self.dinamicos = []  # funciones (lienzo, t) para lo que cambia cada frame
        getattr(self, "montar_" + datos["tipo"])()

    def pintar(self, lienzo: Image.Image, t: float):
        for e in self.elementos:
            e.pintar(lienzo, t)
        for f in self.dinamicos:
            f(lienzo, t)

    # --- tipos ---------------------------------------------------------------

    def montar_titular(self):
        a, a2 = self.pal["acento"], self.pal["acento2"]
        tit = capa_texto(self.d["texto"], fuente_titular(150), ancho_max=900, brillo=a, contorno=4)
        self.elementos.append(Elemento(tit, W / 2, CENTRO_Y - 40, 0.05, "subir"))
        base = CENTRO_Y - 40 + tit.height / 2
        if self.d.get("sub"):
            sub = capa_texto(self.d["sub"], fuente_texto(58), color=a2, contorno=3)
            self.elementos.append(Elemento(sub, W / 2, base + 70, 0.4, "subir"))

        def subrayado(lienzo, t):
            ancho = int(640 * ease_out((t - 0.25) / 0.5))
            if ancho > 0:
                ImageDraw.Draw(lienzo).rounded_rectangle(
                    [W / 2 - ancho / 2, base + 4, W / 2 + ancho / 2, base + 16], radius=6, fill=a + (255,))
        self.dinamicos.append(subrayado)

    def montar_dato(self):
        a, a2 = self.pal["acento"], self.pal["acento2"]
        grande = self.d["grande"]
        fuente = ajustar_fuente(grande, 940, 280)
        alto_grande = sum(fuente.getmetrics()) + 80
        fondo = panel(1000, alto_grande + 190, borde=a)
        self.elementos.append(Elemento(fondo, W / 2, CENTRO_Y, 0.0, "pop", 0.35))
        y_grande = CENTRO_Y - 60
        m = re.match(r"^(\+?)(\d[\d.]*)(\D*)$", grande)
        numero = int(m.group(2).replace(".", "")) if m and ":" not in grande else None
        if numero is not None and numero >= 2:
            pre, suf, con_punto = m.group(1), m.group(3), "." in m.group(2)

            def contador(lienzo, t):
                p = ease_out((t - 0.1) / 0.9)
                if t < 0.1:
                    return
                v = int(round(numero * p))
                txt = f"{v:,}".replace(",", ".") if con_punto else str(v)
                capa = capa_texto(pre + txt + suf, fuente, brillo=a, contorno=3)
                lienzo.alpha_composite(capa, (int(W / 2 - capa.width / 2), int(y_grande - capa.height / 2)))
            self.dinamicos.append(contador)
        else:
            capa = capa_texto(grande, fuente, brillo=a, contorno=3)
            self.elementos.append(Elemento(capa, W / 2, y_grande, 0.1, "pop", 0.4))
        if self.d.get("texto"):
            txt = capa_texto(self.d["texto"], fuente_texto(54), color=a2, ancho_max=900, contorno=2)
            self.elementos.append(Elemento(txt, W / 2, y_grande + alto_grande / 2 + 40, 0.45, "subir"))

    def montar_lista(self):
        a, a2 = self.pal["acento"], self.pal["acento2"]
        items = self.d["items"]
        no = self.d.get("marca") == "no"
        tit = capa_texto(self.d["titulo"].upper(), fuente_titular(110), color=a2, brillo=a2, contorno=3)
        alto_item, sep = 130, 34
        total = tit.height + len(items) * (alto_item + sep)
        y = CENTRO_Y - total / 2 + tit.height / 2
        self.elementos.append(Elemento(tit, W / 2, y, 0.05, "subir"))
        y += tit.height / 2 + sep + alto_item / 2
        paso = min(0.9, max(0.35, self.dur * 0.7 / max(1, len(items))))
        for i, item in enumerate(items):
            fila = panel(940, alto_item, radio=30, opacidad=170)
            fd = ImageDraw.Draw(fila)
            color = (255, 70, 90) if no else a
            fd.ellipse([28, 25, 108, 105], fill=color + (255,))
            if no:
                fd.line([50, 47, 86, 83], fill=BLANCO, width=10)
                fd.line([86, 47, 50, 83], fill=BLANCO, width=10)
            else:
                fd.line([46, 67, 62, 84, 92, 48], fill=BLANCO, width=10, joint="curve")
            f = fuente_texto(56)
            while f.getlength(item) > 780 and f.size > 34:
                f = fuente_texto(f.size - 4)
            fd.text((136, alto_item / 2), item, font=f, fill=BLANCO, anchor="lm")
            self.elementos.append(Elemento(fila, W / 2, y, 0.4 + i * paso, "izquierda", 0.4))
            y += alto_item + sep

    def montar_comparar(self):
        a, a2 = self.pal["acento"], self.pal["acento2"]
        ancho_t, medio = 440, 220
        for lado, borde, cx, anim, t_in in (("izq", a2, W / 2 - 246, "izquierda", 0.1), ("der", a, W / 2 + 246, "derecha", 0.55)):
            info = self.d[lado]
            tarjeta = panel(ancho_t, 560, borde=borde)
            td = ImageDraw.Draw(tarjeta)
            td.text((medio, 70), info["titulo"].upper(), font=fuente_texto(48), fill=borde + (255,), anchor="mm")
            # La cifra no puede pasar del hueco libre a los lados del "VS".
            f = ajustar_fuente(info["grande"], 330, 140, 44)
            capa = capa_texto(info["grande"], f, ancho_max=330, brillo=borde, contorno=3)
            tarjeta.alpha_composite(capa, (int(medio - capa.width / 2), int(300 - capa.height / 2)))
            if info.get("nota"):
                nota = capa_texto(info["nota"], fuente_texto(38), color=(225, 225, 235), ancho_max=380)
                tarjeta.alpha_composite(nota, (int(medio - nota.width / 2), int(470 - nota.height / 2)))
            self.elementos.append(Elemento(tarjeta, cx, CENTRO_Y, t_in, anim, 0.45))
        vs = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
        vd = ImageDraw.Draw(vs)
        vd.ellipse([4, 4, 116, 116], fill=OSCURO + (255,), outline=BLANCO + (255,), width=5)
        vd.text((60, 62), "VS", font=fuente_titular(52), fill=BLANCO, anchor="mm")
        # El "VS" va en el hueco de arriba, sobre los títulos, para no tapar cifras.
        self.elementos.append(Elemento(vs, W / 2, CENTRO_Y - 330, 0.35, "pop", 0.35))

    def montar_cita(self):
        a, a2 = self.pal["acento"], self.pal["acento2"]
        texto = "«" + self.d["texto"] + "»"
        fuente = fuente_texto(72)
        lineas = ajustar_lineas(texto, fuente, 880)
        alto_linea = int(sum(fuente.getmetrics()) * 1.15)
        fondo = panel(1000, alto_linea * len(lineas) + 200, borde=a2)
        self.elementos.append(Elemento(fondo, W / 2, CENTRO_Y, 0.0, "pop", 0.35))
        total = sum(len(l) for l in lineas)
        y0 = CENTRO_Y - alto_linea * len(lineas) / 2

        def maquina(lienzo, t):
            n = int(total * ease_out((t - 0.2) / max(0.5, self.dur * 0.7)))
            d = ImageDraw.Draw(lienzo)
            for i, l in enumerate(lineas):
                visible = l[:max(0, n)]
                n -= len(l)
                if visible:
                    d.text((W / 2 - fuente.getlength(l) / 2, y0 + i * alto_linea), visible, font=fuente,
                           fill=BLANCO, stroke_width=2, stroke_fill=OSCURO)
        self.dinamicos.append(maquina)

    def montar_cierre(self):
        a, a2 = self.pal["acento"], self.pal["acento2"]
        tit = capa_texto(self.d["texto"].upper(), fuente_titular(140), ancho_max=900, brillo=a, contorno=4)
        self.elementos.append(Elemento(tit, W / 2, CENTRO_Y - 80, 0.05, "pop", 0.45))
        boton = Image.new("RGBA", (520, 150), (0, 0, 0, 0))
        bd = ImageDraw.Draw(boton)
        bd.rounded_rectangle([0, 0, 519, 149], radius=75, fill=a2 + (255,))
        bd.text((210, 75), "COMENTA", font=fuente_titular(76), fill=OSCURO, anchor="mm")
        bd.polygon([(400, 52), (460, 52), (430, 100)], fill=OSCURO)
        y_boton = CENTRO_Y - 80 + tit.height / 2 + 120

        def latido(lienzo, t):
            if t < 0.5:
                return
            s = 1 + 0.05 * math.sin((t - 0.5) * 6)
            b = boton.resize((int(520 * s), int(150 * s)), Image.BILINEAR)
            lienzo.alpha_composite(b, (int(W / 2 - b.width / 2), int(y_boton - b.height / 2)))
        self.dinamicos.append(latido)


# ----------------------------------------------------------------------------
# Render del vídeo
# ----------------------------------------------------------------------------


def etiqueta_marca(pal: dict, texto: str) -> Image.Image:
    f = fuente_titular(44)
    ancho = int(f.getlength(texto)) + 60
    capa = Image.new("RGBA", (ancho, 76), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    d.rounded_rectangle([0, 0, ancho - 1, 75], radius=38, fill=OSCURO + (190,), outline=pal["acento"] + (255,), width=3)
    d.text((ancho / 2, 39), texto, font=f, fill=BLANCO, anchor="mm")
    return capa


def renderizar(video: dict, tramos: list, duracion: float, salida: Path, marca: str) -> None:
    pal = PALETAS[video.get("paleta", "atardecer")]
    fondo = dibujar_fondo(video.get("paleta", "atardecer"))
    escenas_d = video["escenas"]
    inicios = [0.0] + [tramos[i][0] for i in range(1, len(escenas_d))]
    finales = inicios[1:] + [duracion]
    escenas = [Escena(e, pal, fin - ini) for e, ini, fin in zip(escenas_d, inicios, finales)]
    sello = etiqueta_marca(pal, marca)

    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", str(salida)],
        stdin=subprocess.PIPE,
    )
    total = int(math.ceil(duracion * FPS))
    k = 0
    for n in range(total):
        t = n / FPS
        while k + 1 < len(escenas) and t >= inicios[k + 1]:
            k += 1
        y = int(PAN * t / max(duracion, 0.1))
        lienzo = fondo.crop((0, y, W, y + H)).convert("RGBA")
        escenas[k].pintar(lienzo, t - inicios[k])
        lienzo.alpha_composite(sello, (int(W / 2 - sello.width / 2), 120))
        d = ImageDraw.Draw(lienzo)
        d.rectangle([0, 0, int(W * t / duracion), 10], fill=pal["acento"] + (255,))
        destello = 1 - (t - inicios[k]) / 0.15
        if 0 < destello <= 1 and k > 0:
            capa = Image.new("RGBA", (W, H), (255, 255, 255, int(55 * destello)))
            lienzo.alpha_composite(capa)
        proc.stdin.write(lienzo.convert("RGB").tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        sys.exit(f"[ERROR] ffmpeg no pudo crear {salida}")


def componer(video_mudo: Path, gato: Path | None, narracion: Path, subtitulos: Path, salida: Path) -> None:
    filtros = ["[0:v]format=yuv420p[fondo]"]
    entradas = ["-i", str(video_mudo)]
    if gato:
        chroma = f"colorkey={fv.ca.CHROMA_COLOR}:{fv.ca.CHROMA_SIMILARITY}:{fv.ca.CHROMA_BLEND}"
        if fv.ca.USE_DESPILL:
            chroma += ",despill=type=green:mix=0.5:expand=0"
        entradas += ["-i", str(gato)]
        filtros.append(f"[1:v]{fv.recorte_gato()},{chroma},scale={fv.GATO_ANCHO}:-2[gato]")
        filtros.append(f"[fondo][gato]overlay=W-w-{fv.GATO_MARGEN}:H-h-{fv.GATO_MARGEN}:shortest=1[comp]")
    else:
        filtros.append("[fondo]null[comp]")
    filtros.append(f"[comp]subtitles='{fv.ca.escape_for_filter(subtitulos.resolve())}'[v]")
    entradas += ["-i", str(narracion)]
    audio = "2:a" if gato else "1:a"
    fv.run(["ffmpeg", "-y", *entradas, "-filter_complex", ";".join(filtros), "-map", "[v]", "-map", audio,
            "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-shortest", str(salida)])


def hacer_video(pack: dict, video: dict, carpeta_salida: Path, prueba: bool, rehacer: bool, sin_gato: bool) -> Path:
    vid = video["id"]
    con_gato = video.get("gato", False) and not sin_gato
    tmp = TMP_DIR / carpeta_salida.name / vid
    tmp.mkdir(parents=True, exist_ok=True)
    frases = [(f"escena{i + 1}", e["voz"]) for i, e in enumerate(video["escenas"])]

    print(f"\n=== {vid}: {video.get('titulo_publicacion', '')} ===")
    print("  1/5 Voz...")
    audios = fv.generar_voz(frases, tmp / ("voz_prueba" if prueba else "voz"), prueba, rehacer)
    narracion, tramos = fv.montar_narracion(audios, tmp, con_miau=con_gato)
    duracion = tramos[-1][1] + fv.FINAL_EXTRA
    if duracion > fv.DURACION_MAXIMA:
        narracion, tramos, duracion = fv.acelerar(narracion, tramos, duracion, tmp)
    print(f"     {duracion:.1f} s")

    print("  2/5 Subtítulos...")
    # Subtítulos por encima de la franja que tapa TikTok abajo; con gato, por
    # encima del gato (que va abajo a la derecha).
    fv.SUB_MARGEN_IZQ, fv.SUB_MARGEN_DER = 60, 140
    fv.SUB_MARGEN_ABAJO = SUB_ABAJO_CON_GATO if con_gato else SUB_ABAJO_SIN_GATO
    tiempos = fv.tiempos_palabras(frases, narracion, tramos)
    subtitulos = fv.escribir_subtitulos(tiempos, tmp / "subtitulos.ass")

    print("  3/5 Animación...")
    mudo = tmp / "animacion.mp4"
    renderizar(video, tramos, duracion, mudo, pack.get("marca", "GTA VI · 19 NOV"))

    gato = None
    if con_gato:
        print("  4/5 Gato...")
        gato = fv.animar_gato(narracion, tmp)
    print("  5/5 Montaje final...")
    salida = carpeta_salida / f"{vid}.mp4"
    componer(mudo, gato, narracion, subtitulos, salida)
    print(f"  Listo: {salida}")
    return salida


def escribir_publicar(pack: dict, carpeta_salida: Path) -> Path:
    ruta = carpeta_salida / "publicar.txt"
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(f"{pack.get('pack', '')}\n{'=' * 60}\n\n")
        for v in pack["videos"]:
            f.write(f"{v['id']}.mp4\n")
            f.write(f"TÍTULO: {v.get('titulo_publicacion', '')}\n")
            f.write(f"DESCRIPCIÓN: {v.get('descripcion', '')}\n\n{v.get('hashtags', '')}\n")
            f.write("-" * 60 + "\n\n")
        if pack.get("fuentes"):
            f.write("FUENTES DE LOS DATOS\n" + "\n".join(f"- {x}" for x in pack["fuentes"]) + "\n")
    return ruta


def main():
    parser = argparse.ArgumentParser(description="Pack de vídeos cortos animados a partir de un guiones.json.")
    parser.add_argument("pack", type=Path, help="Carpeta del pack (p.ej. shorts/gta6)")
    parser.add_argument("--solo", help="Solo el vídeo con este id (p.ej. 01_hora)")
    parser.add_argument("--voz-prueba", action="store_true", help="Voz robótica local (espeak-ng), sin ElevenLabs")
    parser.add_argument("--rehacer-voz", action="store_true", help="Regenera los audios aunque existan")
    parser.add_argument("--sin-gato", action="store_true", help="Ningún vídeo lleva al gato")
    args = parser.parse_args()

    carpeta = args.pack if args.pack.is_absolute() else Path.cwd() / args.pack
    pack = json.loads((carpeta / "guiones.json").read_text(encoding="utf-8"))
    videos = [v for v in pack["videos"] if not args.solo or v["id"] == args.solo]
    if not videos:
        sys.exit(f"No hay ningún vídeo '{args.solo}' en {carpeta / 'guiones.json'}.")
    salida = fv.OUTPUT_DIR / carpeta.name
    salida.mkdir(parents=True, exist_ok=True)

    hechos = [hacer_video(pack, v, salida, args.voz_prueba, args.rehacer_voz, args.sin_gato) for v in videos]
    publicar = escribir_publicar(pack, salida)
    print(f"\n{len(hechos)} vídeo(s) en {salida.resolve()}")
    print(f"Títulos y descripciones: {publicar.resolve()}")


if __name__ == "__main__":
    main()
