#!/data/data/com.termux/files/usr/bin/bash
# One-time setup inside Termux
set -e
echo "== Updating packages =="
pkg update -y && pkg upgrade -y
echo "== Installing Python, ffmpeg, Pillow =="
pkg install -y python ffmpeg python-pillow termux-api
echo "== Installing Python libraries =="
pip install -r requirements.txt
# optional: makes uploads much faster (needs a compiler, ok if it fails)
pip install cryptg || echo "(cryptg skipped - uploads still work, just slower)"
pip install pillow-heif || echo "(pillow-heif skipped - HEIC photos will be sent as files)"
echo "== Allow access to phone storage (tap ALLOW) =="
termux-setup-storage
chmod +x run.sh
echo
echo "Setup done. Start with:   ./run.sh --dry-run    (test, nothing is uploaded or deleted)"
echo "Then for real:            ./run.sh"
