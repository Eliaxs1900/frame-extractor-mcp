#!/bin/sh
# build-ffmpeg.sh [etiqueta]: compila bin/ffmpeg desde el código oficial de FFmpeg
# (https://github.com/FFmpeg/FFmpeg, espejo de https://git.ffmpeg.org/ffmpeg.git).
#
# - Solo componentes LGPL: sin --enable-gpl ni --enable-nonfree, así que el binario es
#   LGPL 2.1 o posterior (ver bin/LICENSE-ffmpeg.md).
# - Estático y sin autodetección: solo enlaza con librerías y frameworks que trae macOS
#   (zlib, bzip2, iconv, VideoToolbox, AudioToolbox), así que funciona en cualquier Mac con
#   Apple Silicon y macOS 13 o posterior sin instalar nada.
#
# Uso:
#   ./build-ffmpeg.sh            compila la etiqueta por defecto (la que lleva bin/ffmpeg)
#   ./build-ffmpeg.sh n9.0.3     compila otra etiqueta oficial
#   SRC=/ruta/al/codigo ./build-ffmpeg.sh n9.0.2
#                                usa un clon ya descargado de esa etiqueta en vez de clonar
#
# Para saber si hay una versión más nueva: uv run --script server.py --is-ffmpeg-updated

set -eu

TAG="${1:-n9.0.2}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ -n "${SRC:-}" ]; then
  cd "$SRC"
  test "$(git describe --tags --exact-match)" = "$TAG" || { echo "$SRC no está en la etiqueta $TAG" >&2; exit 1; }
else
  WORK="$(mktemp -d)"
  git clone --quiet --depth 1 --branch "$TAG" https://github.com/FFmpeg/FFmpeg.git "$WORK/ffmpeg"
  cd "$WORK/ffmpeg"
fi

# iconv vive en /usr/lib/libiconv.dylib, no en libSystem: sin -liconv el enlazado de ffmpeg falla
# con _iconv_open sin definir, aunque configure lo dé por bueno.
./configure \
  --disable-autodetect \
  --enable-zlib --enable-bzlib --enable-iconv --extra-libs="-liconv" \
  --enable-videotoolbox --enable-audiotoolbox \
  --disable-ffplay --disable-ffprobe --disable-doc --disable-debug \
  --enable-static --disable-shared \
  --extra-cflags="-mmacosx-version-min=13.0" \
  --extra-ldflags="-mmacosx-version-min=13.0"

make -j"$(sysctl -n hw.ncpu)" ffmpeg

mkdir -p "$HERE/bin"
cp ffmpeg "$HERE/bin/ffmpeg"
cp COPYING.LGPLv2.1 "$HERE/bin/COPYING.LGPLv2.1"

echo "commit: $(git rev-parse HEAD)"
"$HERE/bin/ffmpeg" -hide_banner -L | head -3
"$HERE/bin/ffmpeg" -hide_banner -version | head -1
