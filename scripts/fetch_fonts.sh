#!/usr/bin/env bash
# Download the open-licence fonts faxgen draws with (Google Fonts, OFL/Apache) into data/fonts.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)/data/fonts"
mkdir -p "$DIR"
BASE=https://github.com/google/fonts/raw/main
for p in apache/homemadeapple/HomemadeApple-Regular.ttf \
         ofl/nothingyoucoulddo/NothingYouCouldDo.ttf \
         ofl/courierprime/CourierPrime-Regular.ttf \
         ofl/courierprime/CourierPrime-Bold.ttf \
         "ofl/opensans/OpenSans%5Bwdth,wght%5D.ttf"; do
  f="$DIR/$(basename "$p")"
  [ -s "$f" ] || curl -fsSL -o "$f" "$BASE/$p"
done
echo "Fonts in $DIR"
