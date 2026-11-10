# video-mcp

Servidor **MCP** (Model Context Protocol) que deja a un asistente como Claude **ver un vídeo**: lo resume en una hoja de contactos con los fotogramas **más nítidos** y saca los fotogramas que quieras a tamaño completo.

Va en un único archivo, [`server.py`](server.py). Se ejecuta con [`uv`](https://docs.astral.sh/uv/), que instala las dependencias solo. **No hace falta ffmpeg**: decodifica con [PyAV](https://pyav.org), que ya trae las librerías de FFmpeg.

---

## Por qué existe

Los modelos no pueden reproducir un vídeo. La forma habitual de enseñárselo es sacar unos cuantos fotogramas repartidos de forma uniforme, pero en un vídeo grabado a mano muchos de esos fotogramas salen **movidos**. Justo esos son los que acaban en la muestra.

`video-mcp` divide el vídeo en *N* tramos iguales y, de cada tramo, se queda con el fotograma **más nítido**. La nitidez se mide como la varianza de la imagen de bordes, una medida clásica de desenfoque al estilo Laplaciano. El resultado es una sola imagen que cubre todo el vídeo, con fotogramas legibles, cada uno etiquetado con su número, su tiempo y su nitidez. Con eso puedes pedir después los que te interesen a resolución completa.

## Herramientas

| Herramienta | Qué hace | Parámetros principales |
|---|---|---|
| `video_info` | Duración, fps, número de fotogramas, tamaño, códec y rotación | `video_path` |
| `video_contact_sheet` | Hoja de contactos (cuadrícula) de todo el vídeo. Con `sharpest=true` coge el fotograma más nítido de cada tramo; con `false`, fotogramas equiespaciados | `video_path`, `out_png`, `count` (20), `columns` (4), `thumb_width` (480), `sharpest` (true), `save_frames_dir` (opcional: guarda también esos fotogramas a tamaño completo) |
| `video_frames` | Guarda fotogramas concretos a tamaño completo (JPEG) | `video_path`, `out_dir`, `frames` (números) y/o `times_s` (segundos) |

Todas devuelven JSON con las rutas generadas, para que el asistente abra las imágenes después.

**Flujo típico**
1. `video_info` para saber cuánto dura el vídeo.
2. `video_contact_sheet` para ver el vídeo entero de un vistazo.
3. `video_frames` con los números de fotograma que interesan, para verlos con detalle.

## Requisitos

- macOS, Linux o Windows.
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/). La primera ejecución descarga `mcp`, `av` y `pillow` en un entorno aislado.

## Instalación

```bash
git clone https://github.com/Eliaxs1900/video-mcp.git
```

### Claude Code

```bash
claude mcp add --scope user video -- uv run --script /RUTA/A/video-mcp/server.py
```

### Claude Desktop y otros clientes MCP

Añade esto a la configuración de servidores MCP (en Claude Desktop, `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "video": {
      "command": "uv",
      "args": ["run", "--script", "/RUTA/A/video-mcp/server.py"]
    }
  }
}
```

## Uso de ejemplo

> «Mira `~/Movies/IMG_4349.mov` y dime cómo es la peana de la TV.»

El asistente llama primero a esta herramienta:

```json
video_contact_sheet({"video_path": "~/Movies/IMG_4349.mov", "out_png": "/tmp/hoja.jpg", "count": 20})
```

Después abre `/tmp/hoja.jpg` y, si necesita detalle, llama a esta otra:

```json
video_frames({"video_path": "~/Movies/IMG_4349.mov", "out_dir": "/tmp/frames", "frames": [197, 557]})
```

## Probar sin cliente MCP

`server.py` también se puede importar como módulo normal:

```bash
uv run --with "mcp>=1.2,<2" --with av --with pillow python -c "
import importlib.util as u; s=u.spec_from_file_location('s','server.py'); m=u.module_from_spec(s); s.loader.exec_module(m)
print(m.video_info('mi_video.mov'))
print(m.video_contact_sheet('mi_video.mov', 'hoja.jpg'))"
```

## Notas y limitaciones

- **Rotación:** los vídeos de iPhone guardan la rotación en los metadatos. Los fotogramas se giran para que se vean como se grabaron.
- **Rendimiento:** `video_contact_sheet` con `sharpest=true` decodifica el vídeo entero (≈15 s para 30 s de vídeo 720p en un Mac con Apple Silicon). Con `sharpest=false` también recorre el vídeo, pero sin medir la nitidez.
- **Memoria:** solo guarda en memoria el mejor fotograma de cada tramo, no el vídeo completo.
- **Problema conocido:** si ejecutas el servidor desde una carpeta que tenga archivos con nombres de módulos estándar de Python (`inspect.py`, `json.py`…), la importación de PyAV puede fallar o incluso cerrar el proceso. Ejecútalo con la ruta absoluta de `server.py`, como en los ejemplos.

## Licencia

[MIT](LICENSE)
