"""
entorno.py — Carga las claves del .env de scripts/youtube de forma robusta.

load_dotenv() a secas falla en silencio en algunos Windows:
  - si el .env se guardó en UTF-16 ("Unicode" en el Bloc de notas, o creado
    con `echo ... > .env` en PowerShell), no lee ninguna clave;
  - si existe una variable de entorno de Windows vacía con el mismo nombre
    (p.ej. ANTHROPIC_API_KEY=""), la deja vacía y no usa la del .env.

cargar_env() lee siempre el .env que está junto a este archivo, en
cualquiera de esas codificaciones, y rellena las variables que falten o
estén vacías. clave() devuelve una clave o explica exactamente qué pasa.
"""

import io
import os
from pathlib import Path

from dotenv import dotenv_values

ENV_PATH = Path(__file__).resolve().parent / ".env"


def _leer_texto(ruta: Path) -> str:
    datos = ruta.read_bytes()
    if datos.startswith((b"\xff\xfe", b"\xfe\xff")):
        return datos.decode("utf-16")
    try:
        return datos.decode("utf-8-sig")
    except UnicodeDecodeError:
        return datos.decode("cp1252", errors="replace")


def cargar_env() -> dict:
    """Carga ENV_PATH en os.environ (sin pisar variables que ya tengan un
    valor). Devuelve lo leído del archivo ({} si no existe)."""
    if not ENV_PATH.exists():
        return {}
    valores = dotenv_values(stream=io.StringIO(_leer_texto(ENV_PATH)))
    for nombre, valor in valores.items():
        if valor and not os.environ.get(nombre, "").strip():
            os.environ[nombre] = valor.strip()
    return valores


def clave(nombre: str) -> str:
    """Valor de una clave del .env, o sale con un mensaje que dice qué falla."""
    valores = cargar_env()
    valor = os.environ.get(nombre, "").strip()
    if valor:
        return valor
    import sys

    if not ENV_PATH.exists():
        otros = sorted(p.name for p in ENV_PATH.parent.glob("*env*") if p.is_file())
        sys.exit(
            f"Falta {nombre}: no existe el archivo {ENV_PATH}.\n"
            f"  En esa carpeta hay: {', '.join(otros) or '(ningún archivo env)'}.\n"
            "  El archivo tiene que llamarse exactamente .env (no env.example ni .env.txt)."
        )
    sys.exit(
        f"Falta {nombre} en {ENV_PATH}.\n"
        f"  Claves que sí he encontrado en ese archivo: {', '.join(valores) or '(ninguna)'}.\n"
        f"  Añade una línea así, sin espacios ni comillas: {nombre}=tu_clave"
    )
