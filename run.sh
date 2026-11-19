#!/data/data/com.termux/files/usr/bin/bash
# Keeps the phone awake while uploading, then runs the uploader.
cd "$(dirname "$0")"
command -v termux-wake-lock >/dev/null && termux-wake-lock
python uploader.py "$@"
command -v termux-wake-unlock >/dev/null && termux-wake-unlock
