"""
futbol_partido.py — Busca un partido y prepara las notas y el guion del gato.

1. Consulta en ESPN los últimos partidos jugados del equipo (LaLiga,
   Champions, Copa y Supercopa).
2. Del partido elegido descarga alineación, cambios, goles, estadísticas,
   narración minuto a minuto y la crónica.
3. Se lo pasa a Claude (ANTHROPIC_API_KEY del .env) para que ponga notas
   duras y justificadas a cada jugador y escriba lo que dirá el gato, con el
   mismo estilo que los guiones de reacción (style_profile.json).
4. Guarda assets/futbol/<equipo>/partidos/<fecha>_<rival>.json, que es lo
   que luego usa futbol_video.py para montar el vídeo.

Uso (desde la carpeta scripts/youtube; normalmente lo lanza menu.py):
    python futbol_partido.py listar  --equipo barca
    python futbol_partido.py generar --equipo barca                # el último partido
    python futbol_partido.py generar --equipo barca --evento 401882859
    python futbol_partido.py generar --equipo barca --evento 401882859 --motivo "Pedri merece más"
"""

import argparse
import html
import json
import os
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

import futbol_jugadores as fj  # noqa: E402

ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"
LIGAS = {
    "esp.1": "LaLiga",
    "uefa.champions": "Champions League",
    "esp.copa_del_rey": "Copa del Rey",
    "esp.super_cup": "Supercopa",
}

# Criterio de las notas: se le pasa tal cual a Claude y se guarda en el JSON.
CRITERIO = (
    "Escala dura: 5 = cumplió sin más. Gol o asistencia suman; un error que acaba "
    "en gol en contra resta mucho; el 10 queda reservado a un partido perfecto. "
    "Solo puntúa quien juega 15 minutos o más."
)

# Filas de la alineación: cuanto más alto, más adelantado. Y lado del campo:
# cuanto más bajo, más a la izquierda de la pantalla.
FILA_POSICION = {"G": 0, "SW": 1, "CD": 1, "LB": 1, "RB": 1, "LWB": 2, "RWB": 2,
                 "DM": 2, "CDM": 2, "CM": 3, "LM": 3, "RM": 3,
                 "AM": 4, "CAM": 4, "LW": 5, "RW": 5, "F": 5, "CF": 5, "ST": 5, "LF": 5, "RF": 5}


def get_json(url: str) -> dict:
    # Sin User-Agent propio: la API de ESPN devuelve 403 a los de navegador
    # y a los inventados, pero acepta el de Python por defecto.
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def slug(texto: str) -> str:
    texto = unicodedata.normalize("NFD", texto.lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", texto).strip("_")


def espn_id_equipo(plantilla: dict, equipo: str) -> str:
    if not plantilla.get("espn_id"):
        sys.exit(f"Falta \"espn_id\" en {equipo}/plantilla.json (Barça = 83, Real Madrid = 86).")
    return str(plantilla["espn_id"])


# ----------------------------------------------------------------------------
# Partidos
# ----------------------------------------------------------------------------


def partidos_recientes(espn_id: str, cuantos: int = 6) -> list[dict]:
    partidos = []
    for liga, nombre_liga in LIGAS.items():
        try:
            datos = get_json(f"{ESPN}/{liga}/teams/{espn_id}/schedule")
        except Exception:
            continue
        for e in datos.get("events", []):
            comp = e["competitions"][0]
            if not comp["status"]["type"]["completed"]:
                continue
            equipos = {c["homeAway"]: c for c in comp["competitors"]}
            marcador = lambda c: (c.get("score") or {}).get("displayValue", "?")  # noqa: E731
            partidos.append({
                "evento": e["id"],
                "liga": liga,
                "competicion": nombre_liga,
                "fecha": e["date"][:10],
                "titulo": f"{equipos['home']['team']['displayName']} {marcador(equipos['home'])}"
                          f"-{marcador(equipos['away'])} {equipos['away']['team']['displayName']}",
            })
    partidos.sort(key=lambda p: p["fecha"], reverse=True)
    return partidos[:cuantos]


def buscar_liga(espn_id: str, evento: str) -> str:
    for p in partidos_recientes(espn_id, cuantos=50):
        if p["evento"] == evento:
            return p["liga"]
    return "esp.1"


def minutos_por_jugador(resumen: dict) -> dict[str, tuple[int, int]]:
    """{nombre: (entra, sale)} a partir de los cambios ("X replaces Y")."""
    entra, sale = {}, {}
    for ev in resumen.get("keyEvents", []):
        if ev.get("type", {}).get("text") != "Substitution":
            continue
        m = re.search(r"\. (.+?) replaces (.+?)(?: because|\.|$)", ev.get("text", ""))
        minuto = int(re.match(r"\d+", ev.get("clock", {}).get("displayValue", "0") or "0").group())
        if m:
            entra[m[1].strip()], sale[m[2].strip()] = minuto, minuto
    return {"entra": entra, "sale": sale}


def ordenar_once(titulares: list[dict], formacion: str) -> list[str]:
    """Ordena el once del portero a la delantera y, en cada línea, de
    izquierda a derecha, según las posiciones de ESPN (p.ej. CD-L, RB, LF)."""

    def fila(pos: str) -> int:
        return FILA_POSICION.get(pos.split("-")[0], 3)

    def lado(pos: str) -> int:
        if pos.endswith("-L") or pos in ("LB", "LWB", "LM", "LW", "LF"):
            return 0 if pos in ("LB", "LWB", "LM", "LW", "LF") else 1
        if pos.endswith("-R") or pos in ("RB", "RWB", "RM", "RW", "RF"):
            return 4 if pos in ("RB", "RWB", "RM", "RW", "RF") else 3
        return 2

    portero = [t for t in titulares if t["posicion"] == "G"][:1]
    resto = sorted((t for t in titulares if t not in portero), key=lambda t: (fila(t["posicion"]), lado(t["posicion"])))
    orden = [t["id"] for t in portero]
    i = 0
    for n in (int(x) for x in formacion.split("-")):
        linea = sorted(resto[i: i + n], key=lambda t: lado(t["posicion"]))
        orden += [t["id"] for t in linea]
        i += n
    return orden


def texto_plano(html_texto: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html_texto or ""))).strip()


def datos_del_partido(equipo: str, plantilla: dict, evento: str) -> dict:
    espn_id = espn_id_equipo(plantilla, equipo)
    liga = buscar_liga(espn_id, evento)
    resumen = get_json(f"{ESPN}/{liga}/summary?event={evento}")

    comp = resumen["header"]["competitions"][0]
    lados = {c["homeAway"]: c for c in comp["competitors"]}
    nuestro = next(c for c in comp["competitors"] if c["team"]["id"] == espn_id)
    rival = next(c for c in comp["competitors"] if c["team"]["id"] != espn_id)
    rival_slug = slug(rival["team"]["displayName"])

    # Escudo del rival para el marcador (el nuestro ya está en escudos/).
    escudo = fj.FUTBOL_DIR / "escudos" / f"{rival_slug}.png"
    logos = rival["team"].get("logos") or [{}]
    if not escudo.exists() and logos[0].get("href"):
        escudo.write_bytes(urllib.request.urlopen(logos[0]["href"], timeout=30).read())

    roster = next(r for r in resumen["rosters"] if r["team"]["id"] == espn_id)
    por_dorsal = {str(j["dorsal"]): j for j in plantilla["jugadores"]}
    cambios = minutos_por_jugador(resumen)

    titulares, suplentes, extras = [], [], {}
    for p in roster["roster"]:
        nombre_espn = p["athlete"]["displayName"]
        j = por_dorsal.get(str(p.get("jersey")))
        jid = j["id"] if j else f"espn_{p['athlete']['id']}"
        if not j:
            extras[jid] = {"nombre": nombre_espn}
        stats = {s["name"]: s.get("displayValue") for s in p.get("stats", [])}
        stats = {k: v for k, v in stats.items() if v not in (None, "0") and k != "appearances"}
        info = {
            "id": jid,
            "nombre": j["nombre"] if j else nombre_espn,
            "posicion": p.get("position", {}).get("abbreviation", ""),
            "estadisticas": stats,
        }
        if p.get("starter"):
            info["minutos"] = f"0-{cambios['sale'].get(nombre_espn, 90)}"
            titulares.append(info)
        elif p.get("subbedIn"):
            info["minutos"] = f"{cambios['entra'].get(nombre_espn, '?')}-90"
            suplentes.append(info)

    nombres = [t["nombre"] for t in titulares + suplentes] + [p["athlete"]["displayName"] for p in roster["roster"]]
    narracion = [
        f"{c.get('time', {}).get('displayValue', '')} {c.get('text', '')}"
        for c in resumen.get("commentary", [])
        if any(n.split()[-1] in c.get("text", "") for n in nombres) or "Goal" in c.get("text", "")
    ]
    estadisticas_equipos = {
        t["team"]["displayName"]: {s["label"]: s["displayValue"] for s in t.get("statistics", [])}
        for t in resumen.get("boxscore", {}).get("teams", [])
    }
    goles = [int(lados["home"].get("score", 0)), int(lados["away"].get("score", 0))]
    local_slug = equipo if lados["home"] is nuestro else rival_slug
    visitante_slug = equipo if lados["away"] is nuestro else rival_slug

    return {
        "evento": evento,
        "liga": liga,
        "fecha": resumen["header"]["competitions"][0]["date"][:10],
        "titulo": f"{lados['home']['team']['displayName']} {goles[0]}-{goles[1]} {lados['away']['team']['displayName']}",
        "competicion": LIGAS.get(liga, liga),
        "rival": rival["team"]["displayName"],
        "rival_slug": rival_slug,
        "marcador": {"local": local_slug, "visitante": visitante_slug, "goles": goles},
        "formacion": roster.get("formation") or "4-3-3",
        "titulares": titulares,
        "suplentes": suplentes,
        "extras": extras,
        "estadisticas_equipos": estadisticas_equipos,
        "eventos": [
            f"{e.get('clock', {}).get('displayValue', '')} {e.get('text') or e.get('type', {}).get('text', '')}"
            for e in resumen.get("keyEvents", []) if e.get("text")
        ],
        "narracion": narracion[:220],
        "cronica": texto_plano((resumen.get("article") or {}).get("story", ""))[:6000],
    }


# ----------------------------------------------------------------------------
# Notas y guion con Claude
# ----------------------------------------------------------------------------

PROMPT = """Eres el guionista de un gato comentarista de TikTok que puntúa a los jugadores
del {equipo} tras cada partido. Tienes que poner las notas y escribir lo que dirá el gato.

CRITERIO DE NOTAS (obligatorio):
- {criterio}
- Sé crítico, tirando a duro. Notas de 0 a 10 en múltiplos de 0,5.
- Cada nota se justifica SOLO con hechos de los datos de abajo (goles, asistencias,
  errores en goles, tiros, ocasiones falladas, paradas, faltas, lesiones, crónica).
  No inventes jugadas ni datos que no estén en los datos.

ESTILO DEL GATO:
- Español, vocabulario juvenil y con gracia, frases cortas; puede decir "manito".
- Cada texto de jugador: 25 a 45 palabras, dice por qué y TERMINA SIEMPRE con la nota
  en palabras, así: "Un seis." / "Un cuatro y medio." / "Un nueve y medio."
- Los números del texto en palabras (se leen en voz alta): "cincuenta y dos", no "52".
- Intro: 25 a 45 palabras, empieza con "¡Miau!", resultado y la idea del partido.
- Cierre: nota de los suplentes que jugaron 15 minutos o más, los demás "sin tiempo
  para nota", una conclusión y pide opinión en comentarios. Termina con "¡Miau!".
{estilo}
DATOS DEL PARTIDO (JSON):
{datos}
{feedback}
Responde SOLO con un JSON válido, sin texto antes ni después, con esta forma:
{{
  "intro": "...",
  "jugadores": {{"<id de titular>": {{"nota": 6.5, "datos": "hechos que justifican la nota", "texto": "lo que dice el gato"}}, ...}},
  "suplentes": {{"<id de suplente>": {{"nota": 6 o null, "datos": "..."}}, ...}},
  "cierre": "..."
}}
Usa exactamente los "id" de los datos. En "jugadores" van los {n} titulares, todos."""


def pedir_notas(datos: dict, equipo_nombre: str, anterior: dict | None, motivo: str | None) -> dict:
    load_dotenv()
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("Falta ANTHROPIC_API_KEY en el .env. Revisa .env.example.")
    import anthropic
    import generate_script as gs

    feedback = ""
    if anterior and motivo:
        previo = {k: anterior[k] for k in ("intro", "jugadores", "suplentes", "cierre") if k in anterior}
        feedback = (
            f"\nVERSIÓN ANTERIOR (no gustó):\n{json.dumps(previo, ensure_ascii=False)}\n"
            f"MOTIVO: {motivo}\nCorrige eso manteniendo el criterio duro y basado en datos.\n"
        )
    para_claude = {k: v for k, v in datos.items() if k not in ("extras", "marcador", "rival_slug", "liga")}
    prompt = PROMPT.format(
        equipo=equipo_nombre,
        criterio=CRITERIO,
        estilo=gs.build_style_context(gs.load_style_profile()),
        datos=json.dumps(para_claude, ensure_ascii=False),
        feedback=feedback,
        n=len(datos["titulares"]),
    )
    client = anthropic.Anthropic(api_key=api_key)
    ids = {t["id"] for t in datos["titulares"]}
    for intento in range(3):
        respuesta = client.messages.create(
            model=gs.CLAUDE_MODEL,
            max_tokens=6000,
            messages=[{"role": "user", "content": prompt}],
        )
        texto = "".join(b.text for b in respuesta.content if b.type == "text")
        try:
            resultado = json.loads(texto[texto.index("{"): texto.rindex("}") + 1])
            faltan = ids - set(resultado["jugadores"])
            if faltan:
                raise ValueError(f"faltan titulares: {faltan}")
            for j in resultado["jugadores"].values():
                j["nota"] = round(float(j["nota"]) * 2) / 2
            return resultado
        except (ValueError, KeyError) as e:
            print(f"  Respuesta no válida de Claude ({e}), reintentando ({intento + 1}/3)...")
    sys.exit("Claude no devolvió unas notas válidas tras 3 intentos.")


def generar(equipo: str, evento: str | None, motivo: str | None) -> Path:
    plantilla = fj.load_plantilla(equipo)
    if not evento:
        recientes = partidos_recientes(espn_id_equipo(plantilla, equipo), cuantos=1)
        if not recientes:
            sys.exit("No encuentro partidos jugados en ESPN.")
        evento = recientes[0]["evento"]

    print("Descargando datos del partido...")
    datos = datos_del_partido(equipo, plantilla, evento)
    destino = fj.FUTBOL_DIR / equipo / "partidos" / f"{datos['fecha']}_{datos['rival_slug']}.json"
    anterior = json.loads(destino.read_text(encoding="utf-8")) if destino.exists() else None

    print(f"{datos['titulo']} ({datos['competicion']}, {datos['fecha']})")
    print("Claude está poniendo las notas...")
    notas = pedir_notas(datos, plantilla["equipo"], anterior, motivo)

    once = ordenar_once(datos["titulares"], datos["formacion"])
    # Se comenta del portero a la delantera, dejando al mejor para el final.
    mejor = max(once, key=lambda j: notas["jugadores"][j]["nota"])
    orden = [j for j in once if j != mejor] + [mejor]

    partido = {
        "equipo": equipo,
        "partido": datos["titulo"],
        "fecha": datos["fecha"],
        "competicion": datos["competicion"],
        "marcador": datos["marcador"],
        "fuentes": [f"ESPN, evento {evento} ({datos['liga']})"],
        "criterio": CRITERIO,
        "formacion": datos["formacion"],
        "once": once,
        "orden": orden,
        "extras": datos["extras"],
        "intro": notas["intro"],
        "jugadores": notas["jugadores"],
        "suplentes": notas.get("suplentes", {}),
        "cierre": notas["cierre"],
    }
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(partido, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destino


def imprimir_notas(ruta: Path) -> None:
    partido = json.loads(ruta.read_text(encoding="utf-8"))
    plantilla = {j["id"]: j["nombre"] for j in fj.load_plantilla(partido["equipo"])["jugadores"]}
    nombre = lambda jid: plantilla.get(jid) or partido.get("extras", {}).get(jid, {}).get("nombre", jid)  # noqa: E731
    print(f"\n{partido['partido']} — {partido['competicion']}\n")
    print(f"  INTRO: {partido['intro']}\n")
    for jid in partido["orden"]:
        j = partido["jugadores"][jid]
        print(f"  {fj.formatear_nota(j['nota']):>4}  {nombre(jid)}: {j['datos']}")
    for jid, s in partido.get("suplentes", {}).items():
        nota = fj.formatear_nota(s["nota"]) if s.get("nota") is not None else "S.C."
        print(f"  {nota:>4}  {nombre(jid)} (suplente): {s.get('datos', '')}")
    print(f"\n  CIERRE: {partido['cierre']}")


def main():
    parser = argparse.ArgumentParser(description="Notas y guion del gato para un partido.")
    sub = parser.add_subparsers(dest="comando", required=True)
    p = sub.add_parser("listar", help="Últimos partidos jugados")
    p.add_argument("--equipo", required=True)
    p = sub.add_parser("generar", help="Descarga el partido y pide las notas a Claude")
    p.add_argument("--equipo", required=True)
    p.add_argument("--evento", help="Id de ESPN del partido (por defecto, el último)")
    p.add_argument("--motivo", help="Qué no te gustó de la versión anterior, para rehacerla")
    args = parser.parse_args()

    if args.comando == "listar":
        plantilla = fj.load_plantilla(args.equipo)
        for p in partidos_recientes(espn_id_equipo(plantilla, args.equipo)):
            print(f"  {p['evento']}  {p['fecha']}  {p['titulo']}  ({p['competicion']})")
    else:
        ruta = generar(args.equipo, args.evento, args.motivo)
        imprimir_notas(ruta)
        print(f"\nGuardado en {ruta}")


if __name__ == "__main__":
    main()
