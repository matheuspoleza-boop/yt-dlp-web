# Clip.AI Worker

Worker HTTP self-hosted que provee descarga de videos de YouTube y procesamiento con ffmpeg para el template **[Clip.AI · Implementa AI](https://implamentaai.lovable.app)**.

> Sin este worker, Clip.AI no puede importar videos de YouTube. Es una dependencia obligatoria del template.

---

## Qué hace

Es un servidor Flask + gunicorn que envuelve `yt-dlp` y `ffmpeg`, exponiendo endpoints HTTP que el frontend de Clip.AI consume:

| Endpoint | Para qué se usa |
|---|---|
| `GET /health` | Health check (botón "Probar conexión" en Configuración) |
| `POST /download` | Inicia descarga de un video de YouTube (async, devuelve `job_id`) |
| `GET /status/<job_id>` | Estado del job: `downloading` / `done` / `error` |
| `GET /get/<job_id>` | Descarga el archivo final una vez que el job está `done` |
| `POST /extract-frames` | Extrae frames a timestamps específicos (auto-encuadre con tracking facial) |
| `POST /extract-audio` | Extrae audio del video (input para transcripción ElevenLabs) |
| `POST /trim` | Corta, reframea y quema subtítulos en un clip final |

---

## Arquitectura

El worker es 1 de 3 servicios que se deployan juntos en Railway:

```
+------------------------------------------------+
|  Clip.AI (Lovable Cloud)                       |
|  <- la app que ve el usuario final             |
+----------------+-------------------------------+
                 |  HTTP
                 v
+------------------------------------------------+
|  Clip.AI Worker (este repo, en Railway)        |
|  Python + Flask + gunicorn + yt-dlp + ffmpeg   |
+----------------+-------------------------------+
                 |  HTTP a :4416 para tokens POT
                 v
+------------------------------------------------+
|  bgutil-ytdlp-pot-provider (Railway, sidecar)  |
|  Imagen Docker brainicism/bgutil-ytdlp-pot-    |
|  provider · genera tokens para bypassear bot   |
|  check de YouTube                              |
+------------------------------------------------+
```

**Por qué se necesita el sidecar `bgutil-pot-provider`:**
YouTube bloquea descargas desde IPs de datacenters (incluido Railway) con el mensaje "Sign in to confirm you're not a bot". El POT provider genera tokens de prueba de origen que hacen que el tráfico parezca legítimo. Sin él, las descargas fallan con `n challenge solving failed`.

---

## Deploy rápido en Railway

### Opción A — Railway Template (1 click)

> Configurá un Railway Template oficial desde tu primer deploy. Reemplazá este link cuando lo tengas:
>
> **`https://railway.app/template/<tu-template-id>`**

El Template deploya **los 2 servicios juntos** (worker + sidecar) ya conectados por la red interna de Railway.

### Opción B — Deploy manual paso a paso

**1. Deployar el sidecar `bgutil-pot-provider` primero**

En Railway → New Project → Empty Project → Add Service → Docker Image:
- Image: `brainicism/bgutil-ytdlp-pot-provider:latest`
- Service name: `bgutil-pot-provider` (este nombre tiene que coincidir con el host por default que usa el worker)
- Expose internal port: `4416`

**2. Deployar este worker**

En el mismo proyecto Railway → New Service → Deploy from GitHub repo → seleccioná `ecossistemapd-bit/clipai-worker`:
- Railway detecta el `Dockerfile` y el `railway.json` automáticamente
- Espera 3-5 minutos al primer build (compila ffmpeg y descarga 16 Google Fonts)

**3. Configurar variables de entorno del worker**

En Railway → tu servicio `clipai-worker` → Variables:

| Variable | Valor | Obligatoria |
|---|---|---|
| `YTDLP_COOKIES_BASE64` | Cookies de YouTube en base64 (ver sección abajo) | Sí |
| `BGUTIL_POT_BASE_URL` | `http://bgutil-pot-provider.railway.internal:4416` | Solo si renombraste el sidecar |

**4. Obtener el URL público del worker**

En Railway → tu servicio `clipai-worker` → Settings → Networking → Generate Domain.
Te queda algo como: `https://clipai-worker-production-abc1.up.railway.app`

---

## Cookies de YouTube (paso obligatorio)

Sin cookies, el bot check de YouTube bloquea las descargas en ~80% de los videos. Hay que extraer cookies de una cuenta YouTube (idealmente una cuenta descartable, no tu cuenta principal).

### Cómo extraerlas

1. Instalá la extensión **[Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)** en Chrome.
2. Iniciá sesión en YouTube con la cuenta descartable.
3. Andá a https://www.youtube.com (cualquier video).
4. Click en la extensión → "Export As" → "Netscape" → guardá el archivo `cookies.txt`.

### Cómo convertir a base64

En Mac/Linux:
```bash
base64 -i cookies.txt | tr -d '\n' | pbcopy
```

En Windows (PowerShell):
```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.txt")) | Set-Clipboard
```

Eso te copia la versión base64 al portapapeles. Pegala en Railway → tu servicio `clipai-worker` → Variables → `YTDLP_COOKIES_BASE64`.

> **Importante:** las cookies expiran. Si después de unas semanas las descargas empiezan a fallar con bot check, re-extraé las cookies y actualizá la variable.

---

## Conectar con Clip.AI

Una vez deployado el worker:

1. Abrí tu instancia de Clip.AI (`https://<tu-proyecto>.lovable.app`)
2. Login → andá a **Configuración → Integraciones**
3. En el card "RailWay - Servidor de Video":
   - Click "Cambiar URL"
   - Pegá el URL público del worker (ej: `https://clipai-worker-production-abc1.up.railway.app`)
   - Click "Guardar"
   - Click "Probar conexión" → debería decir "Servidor RailWay accesible"

Listo. Ahora podés crear un proyecto en Clip.AI con un link de YouTube y el worker va a descargarlo.

---

## Variables de entorno (referencia completa)

| Variable | Default | Descripción |
|---|---|---|
| `PORT` | `5000` | Puerto donde gunicorn escucha (Railway lo setea automáticamente) |
| `BGUTIL_POT_BASE_URL` | `http://bgutil-pot-provider.railway.internal:4416` | URL interna del sidecar POT provider |
| `YTDLP_COOKIES_PATH` | `/app/cookies.txt` | Path local donde se escribe el cookies.txt (decodificado desde base64) |
| `YTDLP_COOKIES_BASE64` | (vacío) | Contenido del cookies.txt en base64. El worker lo decodifica al boot. |

---

## Endpoints — detalle técnico

### `POST /download`
```json
{ "url": "https://www.youtube.com/watch?v=...", "format": "video" }
```
- `format`: `"video"` (mp4) o `"audio"` (mp3)
- Devuelve: `{ "job_id": "abc123" }`
- Async: el download corre en background, hay que pollear `/status/<job_id>`

### `GET /status/<job_id>`
Devuelve: `{ "status": "downloading" | "done" | "error", "filename": "...", "error": "..." }`

### `GET /get/<job_id>`
Una vez `status=done`, descarga el archivo final como `Content-Disposition: attachment`.

### `POST /extract-frames`
2 modos:

**Modo URL (preferido, usado por Clip.AI):**
```json
{ "url": "https://...", "timestamps": [0.0, 0.5, 1.0], "quality": 80 }
```
Devuelve: `{ "frames": ["<base64 jpg>", ...] }` (1 frame por timestamp)

**Modo job_id (legacy):**
```json
{ "job_id": "abc123" }
```
Extrae 1 frame por segundo del video previamente descargado. Devuelve frames como PNG base64 con timestamps.

### `POST /extract-audio`
2 modos similares al de extract-frames. Devuelve MP3 streaming o como file attachment.

### `POST /trim`
```json
{
  "url": "https://...",
  "start": 12.5,
  "duration": 30.0,
  "vf": "crop=...,scale=720:1280",
  "padding_before": 0.5,
  "padding_after": 0.5,
  "subtitle_url": "https://...subs.ass",
  "subtitle_style": "karaoke"
}
```
- Sincrónico (no devuelve job_id, espera y devuelve el MP4 final)
- `vf`: ffmpeg video filter chain (Clip.AI lo construye con crop + scale + smart-crop)
- `subtitle_url`: si está, descarga el `.ass` y lo quema en el video con `subtitles=` filter
- Timeout: 10 minutos

---

## Troubleshooting

### "n challenge solving failed"
- Causa: el sidecar `bgutil-pot-provider` no está corriendo, o `BGUTIL_POT_BASE_URL` apunta a la URL incorrecta
- Fix: verificá que el sidecar esté `Active` en Railway. El nombre del servicio tiene que ser `bgutil-pot-provider` o ajustá `BGUTIL_POT_BASE_URL` manualmente

### "Sign in to confirm you're not a bot"
- Causa: cookies no configuradas o expiradas
- Fix: re-extraé las cookies con la extensión, actualizá `YTDLP_COOKIES_BASE64`, reiniciá el servicio en Railway

### "Job not found" después de un deploy
- Causa: la DB SQLite vive en `/tmp/jobs.db` y se borra cuando Railway reinicia el contenedor
- Fix: esperá a que el video se vuelva a descargar (Clip.AI maneja el retry automático)

### Frame extraction es lento (más de 30s para 60 frames)
- Causa: el modo "URL fallback" usa 1 ffmpeg call por frame (lento). El modo "FPS path" es rápido pero requiere timestamps espaciados uniformemente.
- Fix: Clip.AI ya envía timestamps uniformes — si esto pasa, abrir un issue.

---

## Costos estimados

**Railway:**
- Free tier: $5 USD crédito gratis/mes (alcanza para empezar)
- Hobby tier: $5/mes por servicio activo = $10/mes los 2 servicios
- Pro tier: $20/mes con más recursos

**Para uso real (~10 videos/día):** estimado $10-15 USD/mes en Railway + costos de ElevenLabs (transcripción, ~$5/mes Starter) = **$15-20 USD/mes total**.

---

## Stack técnico

- **Runtime:** Python 3.12 (slim)
- **Server:** Flask + gunicorn (4 workers, 2 threads cada uno)
- **Download:** yt-dlp (latest)
- **Video processing:** ffmpeg
- **Token provider:** bgutil-ytdlp-pot-provider (sidecar Docker)
- **State:** SQLite en `/tmp/jobs.db` (compartido entre workers via WAL)
- **Fonts:** 16 Google Fonts pre-instaladas (Liberation + Montserrat + Roboto + Oswald + Raleway + Nunito + PlayfairDisplay + OpenSans + Poppins + Lato + BebasNeue + Anton + Bangers + PermanentMarker + Lobster)

---

## Atribución

Este proyecto deriva de [yt-dlp](https://github.com/yt-dlp/yt-dlp) (License: Unlicense — public domain) y del wrapper Flask original de [matheuspoleza-boop/yt-dlp-web](https://github.com/matheuspoleza-boop/yt-dlp-web).

El sidecar [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) es mantenido por brainicism (License: GPL-3.0).

Este fork es parte del template **Clip.AI** del catálogo [Implementa AI](https://implamentaai.lovable.app).
