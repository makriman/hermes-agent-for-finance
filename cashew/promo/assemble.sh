#!/usr/bin/env bash
# Assemble the Cashew promo: frames + audio -> cashew-promo.mp4
set -euo pipefail
cd "$(dirname "$0")"
ffmpeg -y -framerate 30 -i frames/f%05d.png -i audio.wav \
  -c:v libx264 -preset slow -crf 20 -pix_fmt yuv420p \
  -c:a aac -b:a 160k -shortest -movflags +faststart \
  cashew-promo.mp4
echo "done:"
ls -lh cashew-promo.mp4
ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 cashew-promo.mp4
