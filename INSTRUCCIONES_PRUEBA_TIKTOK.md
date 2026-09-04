# Prueba de login + publicación en TikTok (ReactionFlow)

Esto valida de extremo a extremo: login OAuth con tu cuenta de TikTok (@vegzz) +
publicación de un vídeo mediante la Content Posting API, en modo Sandbox (privado).

Archivos creados:
- `web/netlify/functions/auth-callback.js` — recibe el redirect de TikTok y cambia el
  código por un access_token.
- `web/netlify.toml` — hace que `/auth/callback` apunte a esa función.
- `scripts/tiktok_login.py` — abre el login de TikTok en tu navegador.
- `scripts/tiktok_post.py` — publica un vídeo local usando el access_token.
- `scripts/.env.example` — plantilla de configuración (cópiala a `scripts/.env`).
- `test/test_video.mp4` — vídeo de prueba de 6s en 9:16 para esta prueba.

## 1. Desplegar la función en Netlify

La función tiene que vivir en el mismo sitio (deft-hummingbird-07156c.netlify.app) que
ya tenéis desplegado, porque el Redirect URI registrado en TikTok apunta ahí.

```
npm install -g netlify-cli
cd ruta/a/AutomaticVideos/web
netlify login          # abre el navegador, inicia sesión con tu cuenta de Netlify
netlify link            # elige el sitio "deft-hummingbird-07156c" existente
netlify deploy --prod   # sube index.html, terms/privacy Y la función auth-callback
```

Luego, en el panel de Netlify (app.netlify.com) -> tu sitio -> Site configuration ->
Environment variables, añade estas 3 variables (los valores de Client key/secret están
en TikTok for Developers -> ReactionFlow -> Credentials, dale al icono del ojo para verlos):

- `TIKTOK_CLIENT_KEY` = (tu Client key)
- `TIKTOK_CLIENT_SECRET` = (tu Client secret)
- `TIKTOK_REDIRECT_URI` = `https://deft-hummingbird-07156c.netlify.app/auth/callback`

Después de añadirlas, vuelve a desplegar (`netlify deploy --prod`) para que la función
las recoja.

## 2. Configurar los scripts de Python

```
cd ruta/a/AutomaticVideos/scripts
pip install -r requirements.txt
copy .env.example .env
```

(en Windows es `copy`; en Mac/Linux sería `cp .env.example .env`)

Edita `.env` y rellena `TIKTOK_CLIENT_KEY` (el mismo valor que pusiste en Netlify).

## 3. Probar el login

```
python tiktok_login.py
```

Se abrirá el navegador. Inicia sesión con la cuenta @vegzz (la que añadiste como
Target User del Sandbox) y acepta los permisos. TikTok te redirigirá a una página que
muestra un TIKTOK_ACCESS_TOKEN y un TIKTOK_REFRESH_TOKEN. Copia esos dos valores a
`scripts/.env`.

## 4. Publicar el vídeo de prueba

```
python tiktok_post.py "../test/test_video.mp4"
```

Si todo va bien, verás el publish_id y el estado de la publicación. Como la app aún
no está auditada, el vídeo se publica en modo privado (SELF_ONLY) — solo lo verás tú
entrando a la app de TikTok con la cuenta @vegzz.

## Si algo falla

- Error de "invalid client" o similar al hacer login -> revisa que
  TIKTOK_CLIENT_KEY/TIKTOK_REDIRECT_URI en scripts/.env coincidan exactamente con
  los de la app en TikTok for Developers.
- La página de callback muestra "Server misconfigured" -> faltan las variables de
  entorno en Netlify, o falta volver a desplegar tras añadirlas.
- tiktok_post.py da error de permisos/scope -> confirma que en Sandbox, Content
  Posting API tiene "Direct Post" activado y que video.publish aparece en Scopes.

Cuando esto funcione de punta a punta, grabamos la pantalla haciendo estos 4 pasos:
ese será el vídeo demo que pide TikTok para la auditoría de Production.
