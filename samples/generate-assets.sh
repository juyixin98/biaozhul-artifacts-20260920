#!/usr/bin/env bash
# Generates sample assets (images + videos) with ffmpeg into samples/assets/.
# Each asset is listed in samples/assets/tags.tsv as: <file>\t<name>\t<comma-separated tags>
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/assets"
mkdir -p "$DIR"

gen_image() { # name, color, text
  ffmpeg -v error -f lavfi -i "color=c=$2:s=1280x720:d=1" \
    -vf "drawtext=text='$3':fontsize=72:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2" \
    -frames:v 1 -y "$DIR/$1.png"
}

gen_video() { # name, color, seconds, text
  ffmpeg -v error -f lavfi -i "color=c=$2:s=1280x720:r=30:d=$3" \
    -vf "drawtext=text='$4':fontsize=72:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2" \
    -c:v libx264 -pix_fmt yuv420p -y "$DIR/$1.mp4"
}

gen_image sunset   orange     "SUNSET"
gen_video ocean    blue       20 "OCEAN WAVES"
gen_image forest   darkgreen  "FOREST"
gen_video city     purple     20 "CITY NIGHT"
gen_image mountain slategray  "MOUNTAIN"

cat > "$DIR/tags.tsv" <<'EOF'
sunset.png	sunset	sunset,sky,evening,orange
ocean.mp4	ocean	ocean,waves,sea,water,blue
forest.png	forest	forest,trees,nature,green
city.mp4	city	city,night,neon,urban
mountain.png	mountain	mountain,snow,peak,hiking
EOF

echo "sample assets written to $DIR"
