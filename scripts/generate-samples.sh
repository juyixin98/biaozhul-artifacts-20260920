#!/usr/bin/env bash
#
# Generates self-contained sample materials with the local ffmpeg — no
# external services or downloads required. Output lands in ./sample-assets.
#
#   scripts/generate-samples.sh [output-dir]
#
set -euo pipefail

OUT="${1:-sample-assets}"
mkdir -p "$OUT"

need() { command -v "$1" >/dev/null 2>&1 || { echo "error: $1 is required" >&2; exit 1; }; }
need ffmpeg

gen_image() { # name color
  local name="$1" color="$2"
  ffmpeg -loglevel error -y \
    -f lavfi -i "color=c=${color}:s=1280x720:d=1" \
    -frames:v 1 -y "$OUT/$name"
}

gen_video() { # name color seconds
  local name="$1" color="$2" seconds="$3"
  ffmpeg -loglevel error -y \
    -f lavfi -i "color=c=${color}:s=1280x720:r=30:d=${seconds}" \
    -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=48000" \
    -shortest -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 96k \
    "$OUT/$name"
}

echo ">> generating images..."
gen_image ocean.png      blue
gen_image sunset.png     orange
gen_image forest.png     green
gen_image city.png       gray
gen_image desert.png     yellow

echo ">> generating short videos..."
gen_video ocean-wave.mp4  cyan    3
gen_image snow.png       white
gen_image night.png      black

echo ">> done:"
ls -lh "$OUT"
