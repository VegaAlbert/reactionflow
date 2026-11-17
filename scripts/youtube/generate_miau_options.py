"""
generate_miau_options.py — Utilidad puntual: generar varias variantes del
"Miau" de sello de identidad del gato, para escucharlas y elegir la favorita.

No forma parte del pipeline automático — se ejecuta una sola vez (o las veces
que quieras hasta encontrar el Miau perfecto). El resultado final hay que
guardarlo a mano como ./assets/gato_intro.mp3 para que lo use text_to_speech.py.

Requisitos:
    pip install elevenlabs python-dotenv
    (ya deberías tenerlos instalados del paso anterior)

Uso:
    python generate_miau_options.py
    python generate_miau_options.py --voice-id OTRO_VOICE_ID --output-dir ./assets/gato_intros_voice2
"""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# CONFIGURACIÓN — cambia estos textos/ajustes si quieres probar más variantes
# ----------------------------------------------------------------------------

# Distintas formas de escribir el "Miau" para variar el resultado.
# La ortografía influye en cómo lo pronuncia el modelo (vocales alargadas,
# repeticiones, signos de exclamación, etc.)
# Distintas formas de escribir el "Miau", usando etiquetas de interpretación
# entre corchetes (soportadas por el modelo v3) para dar más carisma: el
# modelo las usa como dirección de actuación, no las pronuncia como texto.
TEXT_VARIANTS = [
    "[coqueto, juguetón] Miauuuu~",
    "[con mucha actitud, un poco creído] Miaaauu~",
    "[teatral, exagerado] Miau, miauuu~",
    "[pícaro, con retintín] Mmmiauuu~",
    "[entusiasmado, dramático] ¡Miauu!",
    "[relamido, con mucho estilo] Miauuuu... miau~",
]

# Distintas combinaciones de estilo, para cruzar con cada texto.
# stability bajo = más expresivo/variable | style alto = más "actuado"
STYLE_VARIANTS = [
    {"stability": 0.2, "style": 1.0, "label": "muy_actuado"},
    {"stability": 0.35, "style": 0.75, "label": "equilibrado"},
]

OUTPUT_DIR = Path("./assets/miau_opciones")
ELEVENLABS_MODEL = "eleven_v3"

# ----------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Genera variantes del 'Miau' de sello con ElevenLabs")
    parser.add_argument("--voice-id", type=str, default=None,
                         help="Voice ID a usar en vez del ELEVENLABS_VOICE_ID del .env (para el Miau de una voz secundaria)")
    parser.add_argument("--output-dir", type=str, default=None,
                         help="Carpeta de salida en vez de ./assets/miau_opciones")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.getenv("ELEVENLABS_API_KEY")
    voice_id = args.voice_id or os.getenv("ELEVENLABS_VOICE_ID")

    if not api_key or not voice_id:
        raise SystemExit("Falta ELEVENLABS_API_KEY o ELEVENLABS_VOICE_ID en el .env")

    output_dir = Path(args.output_dir) if args.output_dir else OUTPUT_DIR

    from elevenlabs.client import ElevenLabs
    client = ElevenLabs(api_key=api_key)

    output_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    for text in TEXT_VARIANTS:
        for style_cfg in STYLE_VARIANTS:
            count += 1
            filename = f"{count:02d}_{style_cfg['label']}_{text[:10].replace(' ', '_').replace('~', '')}.mp3"
            output_path = output_dir / filename

            print(f"Generando [{count}] \"{text}\" ({style_cfg['label']})...")

            try:
                audio = client.text_to_speech.convert(
                    voice_id=voice_id,
                    model_id=ELEVENLABS_MODEL,
                    text=text,
                    voice_settings={
                        "stability": style_cfg["stability"],
                        "similarity_boost": 0.8,
                        "style": style_cfg["style"],
                        "use_speaker_boost": True,
                    },
                )
                with open(output_path, "wb") as f:
                    for chunk in audio:
                        f.write(chunk)
                print(f"   Guardado: {output_path}")
            except Exception as e:
                print(f"   [ERROR] {e}")

    print(f"\nListo. {count} variantes generadas en {output_dir}")
    if output_dir == OUTPUT_DIR:
        print("Escúchalas todas y cuando elijas tu favorita, cópiala como:")
        print("  assets/gato_intro.mp3")


if __name__ == "__main__":
    main()
