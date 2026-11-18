"""
voz_gato_libre.py — Voz de gato gratuita, sin ElevenLabs ni claves ("GATO_1").

Lee el texto con Kokoro (modelo de voz libre, licencia Apache 2.0, voz
española "em_alex") y lo convierte en voz de gato de dibujos con ffmpeg:
tono 5 semitonos más agudo (sin acelerar) y un vibrato suave.

Requisitos (una vez):
    pip install kokoro soundfile
    (instala también PyTorch; la primera vez descarga el modelo, ~330 MB)

Uso desde otros scripts:
    import voz_gato_libre
    voz_gato_libre.generar("¡Miau! Hola", Path("salida.wav"))
"""

import subprocess
import tempfile
from pathlib import Path

VOZ_KOKORO = "em_alex"
VELOCIDAD = 1.05
SEMITONOS = 5
EFECTO = "vibrato=f=5.5:d=0.08,highpass=f=120,acompressor"

# Cómo leer palabras en inglés que Kokoro pronuncia a la española. Solo
# cambia el audio: los subtítulos siguen mostrando el texto original.
PRONUNCIACION = {
    "Vice City": "Bais Síti",
    "Jason": "Yéison",
    "Rockstar": "Róckstar",
    "team": "tim",
    "Ultimate": "Últimeit",
    "Vintage": "Víntach",
    "Ocarina of Time": "Ocarina of Taim",
    "Metacritic": "Métacritic",
    "IGN": "Ai Yi En",
    "Game Informer": "Guéim Infórmer",
    "Game Awards": "Guéim Awórds",
    "Windows Central": "Uíndous Séntral",
    "Digital Foundry": "Díyital Fáundri",
    "Sensor Tower": "Sénsor Táuer",
    "PlayStation": "Pléisteishon",
    "Xbox": "Ékisbox",
    "streamer": "estrímer",
    "MeowSparky": "Miau Spárki",
    "Remote": "Rimóut",
}

_pipeline = None


def para_leer(texto: str) -> str:
    import re

    for palabra, lectura in PRONUNCIACION.items():
        texto = re.sub(rf"\b{re.escape(palabra)}\b", lectura, texto, flags=re.IGNORECASE)
    return texto


def _kokoro():
    global _pipeline
    if _pipeline is None:
        try:
            from kokoro import KPipeline
        except ImportError:
            raise SystemExit("Falta Kokoro para la voz libre: instálalo con  pip install kokoro soundfile")
        _pipeline = KPipeline(lang_code="e", repo_id="hexgrad/Kokoro-82M")
    return _pipeline


def generar(texto: str, salida: Path) -> None:
    """Genera `texto` con la voz de gato libre en `salida` (.wav, 44,1 kHz)."""
    import numpy as np
    import soundfile as sf

    partes = [audio for _, _, audio in _kokoro()(para_leer(texto), voice=VOZ_KOKORO, speed=VELOCIDAD)]
    if not partes:
        raise SystemExit(f"Kokoro no generó audio para: {texto!r}")
    with tempfile.TemporaryDirectory() as tmp:
        cruda = Path(tmp) / "kokoro.wav"
        sf.write(cruda, np.concatenate([np.asarray(p) for p in partes]), 24000)
        factor = 2 ** (SEMITONOS / 12)
        filtro = (f"asetrate=24000*{factor:.6f},aresample=44100,atempo={1 / factor:.6f},"
                  f"{EFECTO},aformat=sample_fmts=s16")
        resultado = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(cruda), "-af", filtro, str(salida)],
            capture_output=True, text=True,
        )
        if resultado.returncode != 0:
            raise SystemExit(f"ffmpeg no pudo aplicar la voz de gato: {resultado.stderr[-300:]}")
