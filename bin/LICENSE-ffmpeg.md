# `bin/ffmpeg`: de dónde sale y bajo qué licencia

`bin/ffmpeg` es la herramienta de línea de órdenes de **FFmpeg**, compilada sin cambios a partir
del código oficial. No es un binario descargado de terceros: se compiló aquí con
[`build-ffmpeg.sh`](../build-ffmpeg.sh), que cualquiera puede volver a ejecutar para obtener el
mismo programa.

## Origen

| | |
|---|---|
| Proyecto | FFmpeg, <https://ffmpeg.org> |
| Código | <https://github.com/FFmpeg/FFmpeg>, espejo oficial de <https://git.ffmpeg.org/ffmpeg.git> |
| Versión | etiqueta [`n9.0.2`](https://github.com/FFmpeg/FFmpeg/tree/n9.0.2), la última versión publicada cuando se compiló (13-11-2026) |
| Etiqueta (objeto) | `ce8f11b9fac4cd7ccfb41ca7c01bc17f5cda8cc3` |
| Commit | [`946fcce07b6dcd0331c8cc609192aeff5e1924f8`](https://github.com/FFmpeg/FFmpeg/commit/946fcce07b6dcd0331c8cc609192aeff5e1924f8), «Update for 9.0.2», 18-09-2026 |
| Código fuente completo | <https://github.com/FFmpeg/FFmpeg/archive/refs/tags/n9.0.2.tar.gz> |
| Cambios propios | ninguno |

## Cómo se compiló

Con `./build-ffmpeg.sh n9.0.2` (Apple clang 21.0.0, macOS 27.2.1, Apple M5 Pro). Opciones de
`configure`, tal como las muestra `bin/ffmpeg -version`:

```
configuration: --disable-autodetect --enable-zlib --enable-bzlib --enable-iconv --extra-libs=-liconv --enable-videotoolbox --enable-audiotoolbox --disable-ffplay --disable-ffprobe --disable-doc --disable-debug --enable-static --disable-shared --extra-cflags='-mmacosx-version-min=13.0' --extra-ldflags='-mmacosx-version-min=13.0'
```

- Ejecutable Mach-O arm64 para macOS 13 o posterior en Apple Silicon.
- Estático: las librerías de FFmpeg van dentro. Solo usa lo que trae macOS: `libSystem`,
  `libz`, `libbz2`, `libiconv` y los frameworks AudioToolbox, CoreAudio, VideoToolbox,
  CoreFoundation, CoreMedia, CoreVideo y CoreServices.
- SHA-256: `056f453a9e5daecd9dfd19d1a8a91641b3f4b0039a86c23e2d9d2271256c10a4`

## Licencia

FFmpeg se distribuye bajo la **GNU Lesser General Public License (LGPL), versión 2.1 o
posterior**. Esta compilación no activa ninguna parte GPL ni «nonfree» (sin `--enable-gpl` ni
`--enable-nonfree`), así que el binario entero queda bajo LGPL 2.1 o posterior, como confirma
`bin/ffmpeg -L`. El texto de la licencia está en [`COPYING.LGPLv2.1`](COPYING.LGPLv2.1), copiado
tal cual del código de FFmpeg.

Esta licencia solo afecta a `bin/ffmpeg`. El resto del repositorio sigue bajo la licencia
[MIT](../LICENSE).

Aviso de licencia, tal como lo imprime `bin/ffmpeg -L`:

```
ffmpeg is free software; you can redistribute it and/or
modify it under the terms of the GNU Lesser General Public
License as published by the Free Software Foundation; either
version 2.1 of the License, or (at your option) any later version.

ffmpeg is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public
License along with ffmpeg; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
```

FFmpeg es una marca registrada de Fabrice Bellard, creador del proyecto FFmpeg.

## Mantenerlo al día

```bash
uv run --script server.py --is-ffmpeg-updated   # compara con la última etiqueta de github.com/FFmpeg/FFmpeg
./build-ffmpeg.sh <etiqueta>                    # recompila bin/ffmpeg con la versión que diga
```

Si se recompila con otra versión, hay que actualizar la tabla de origen y el SHA-256 de este
archivo.
