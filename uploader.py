#!/usr/bin/env python3
"""
Telegram Media Uploader for Android (Termux)
============================================

* Scans the WHOLE phone storage (internal + SD cards) for images, videos and GIFs,
  no matter what the folders or files are called.
* For every folder that contains media:
      1. posts a header message with the folder name
      2. uploads every media file as a real photo / playable, streamable video / GIF
         (with thumbnail + duration so it previews like normal Telegram media)
      3. posts an "end of folder" marker (your sticker, or a big animated emoji)
* Deletes each file from the phone right after Telegram confirms the upload.
* Remembers every uploaded file by its CONTENT (SHA-256) in uploaded.db,
  so if the same media shows up again later (even renamed / in another folder)
  it is skipped and deleted automatically.

Usage:
    python uploader.py              # normal run (asks for confirmation)
    python uploader.py --dry-run    # only show what would happen, touch nothing
    python uploader.py --yes        # no confirmation prompt
    python uploader.py --loop 30    # keep running, re-scan every 30 minutes
    python uploader.py --setup      # re-pick the target group
"""

import argparse
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from telethon import TelegramClient, errors
    from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeFilename
except ImportError:
    print("Telethon is not installed. Run:  pip install -r requirements.txt")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import pillow_heif  # optional, lets us convert HEIC/HEIF photos
    pillow_heif.register_heif_opener()
    HEIF_OK = True
except Exception:
    HEIF_OK = False


# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
DB_FILE = BASE_DIR / "uploaded.db"
LOG_FILE = BASE_DIR / "uploader.log"
SESSION_FILE = str(BASE_DIR / "tg_session")
TMP_DIR = BASE_DIR / "tmp"

IMAGE_EXT = {".jpg", ".jpeg", ".png"}                       # sent as Telegram photos directly
CONVERT_IMAGE_EXT = {".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".jfif"}
GIF_EXT = {".gif"}
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".mkv", ".webm", ".avi",
             ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".mts", ".m2ts"}
STREAMABLE_VIDEO_EXT = {".mp4", ".m4v"}
MEDIA_EXT = IMAGE_EXT | CONVERT_IMAGE_EXT | GIF_EXT | VIDEO_EXT

PHOTO_MAX_BYTES = 10 * 1024 * 1024   # Telegram limit for "photo" uploads

DEFAULT_CONFIG = {
    "api_id": 0,
    "api_hash": "",
    "group": None,                     # numeric id, @username or invite link
    "group_title": "",
    "scan_roots": ["/storage/emulated/0"],
    "scan_sd_cards": True,             # also scan /storage/XXXX-XXXX
    "exclude_paths": [
        "/storage/emulated/0/Android/data",
        "/storage/emulated/0/Android/obb",
        # Telegram's own download folders - otherwise media you view in the group
        # would be downloaded again by the Telegram app and re-uploaded forever.
        "/storage/emulated/0/Telegram",
        "/storage/emulated/0/Android/media/org.telegram.messenger",
        "/storage/emulated/0/Android/media/org.telegram.plus",
        "/storage/emulated/0/Android/media/org.thunderdog.challegram",
    ],
    "skip_hidden_folders": True,       # .thumbnails, .trash, .cache ... and .trashed-* files
    "skip_nomedia_folders": True,      # skip folders hidden from the gallery (.nomedia),
                                       # e.g. WhatsApp "Sent"/"Private" - media you can't see
    "min_file_size_kb": 0,             # raise (e.g. 20) to ignore tiny app icons
    "min_file_age_seconds": 30,        # skip files still being written / recorded
    "max_file_size_mb": 2000,          # 2000 normal account, 4000 for Premium
    "delete_after_upload": True,
    "delete_duplicates": True,
    "caption_with_filename": False,
    "convert_videos_to_mp4": True,     # remux mkv/mov/avi... to mp4 so they stream in-app
    "oversized_images": "document",    # "document" (keep full quality) or "resize"
    "end_sticker_from_saved_messages": True,
    "end_emoji": "🏁",
    "delay_between_uploads_sec": 1.0,
}


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
try:
    sys.stdout.reconfigure(errors="replace")  # never crash on emoji in odd terminals
except Exception:
    pass
log = logging.getLogger("uploader")
log.setLevel(logging.INFO)
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s"))
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_fh)
log.addHandler(_sh)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def ask_api_credentials(cfg: dict) -> None:
    if cfg.get("api_id") and cfg.get("api_hash"):
        return
    print("\n=== First time setup ===")
    print("Get your API ID and API Hash from https://my.telegram.org -> API development tools\n")
    while True:
        try:
            cfg["api_id"] = int(input("API ID   : ").strip())
            break
        except ValueError:
            print("API ID must be a number.")
    cfg["api_hash"] = input("API Hash : ").strip()
    save_config(cfg)


# --------------------------------------------------------------------------- #
# Database (upload log)
# --------------------------------------------------------------------------- #
class UploadDB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS uploads (
                sha256      TEXT PRIMARY KEY,
                size        INTEGER,
                file_name   TEXT,
                folder      TEXT,
                message_id  INTEGER,
                uploaded_at TEXT
            )""")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                ts      TEXT,
                action  TEXT,
                path    TEXT,
                sha256  TEXT,
                detail  TEXT
            )""")
        self.conn.commit()

    def is_uploaded(self, sha: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM uploads WHERE sha256=?", (sha,)).fetchone() is not None

    def add_upload(self, sha, size, name, folder, msg_id):
        self.conn.execute(
            "INSERT OR REPLACE INTO uploads VALUES (?,?,?,?,?,?)",
            (sha, size, name, folder, msg_id, now()))
        self.conn.commit()

    def event(self, action, path, sha="", detail=""):
        self.conn.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                          (now(), action, str(path), sha, detail))
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def find_roots(cfg: dict) -> list:
    roots = [r for r in cfg["scan_roots"] if os.path.isdir(r)]
    if cfg.get("scan_sd_cards") and os.path.isdir("/storage"):
        try:
            for name in os.listdir("/storage"):
                p = os.path.join("/storage", name)
                if name in ("emulated", "self") or not os.path.isdir(p):
                    continue
                if re.fullmatch(r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}", name) and p not in roots:
                    roots.append(p)
        except PermissionError:
            pass
    return roots


def scan_media(cfg: dict) -> dict:
    """Return {folder_path: [file_path, ...]} for every folder that holds media."""
    excludes = [os.path.normpath(p) for p in cfg["exclude_paths"]]
    excludes.append(str(BASE_DIR))
    min_size = cfg["min_file_size_kb"] * 1024
    min_age = cfg["min_file_age_seconds"]
    t_now = time.time()
    folders = {}
    hidden_skipped = []

    def excluded(path):
        path = os.path.normpath(path)
        return any(path == e or path.startswith(e + os.sep) for e in excludes)

    for root in find_roots(cfg):
        log.info(f"Scanning {root} ...")
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False,
                                                    onerror=lambda e: None):
            # prune folders we must not enter
            keep = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if cfg["skip_hidden_folders"] and d.startswith("."):
                    continue
                if excluded(full):
                    continue
                keep.append(d)
            dirnames[:] = keep

            # folder hidden from the gallery -> skip it and everything below it
            if cfg.get("skip_nomedia_folders", True) and ".nomedia" in filenames:
                dirnames[:] = []
                hidden_skipped.append(dirpath)
                continue

            files = []
            for fn in filenames:
                if Path(fn).suffix.lower() not in MEDIA_EXT:
                    continue
                if cfg["skip_hidden_folders"] and fn.startswith("."):
                    continue  # .trashed-123-IMG.jpg = already in Android's bin
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_size == 0 or st.st_size < min_size:
                    continue
                if t_now - st.st_mtime < min_age:
                    continue
                files.append(full)
            if files:
                files.sort(key=lambda p: natural_key(os.path.basename(p)))
                folders[dirpath] = files
    if hidden_skipped:
        log.info(f"Skipped {len(hidden_skipped)} gallery-hidden folders (.nomedia).")
    return folders


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Media preparation (ffmpeg / Pillow)
# --------------------------------------------------------------------------- #
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def run(cmd: list, timeout=600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout)


def probe_video(path: str):
    """Return (duration_sec, width, height) with phone rotation applied, or None."""
    if not HAS_FFMPEG:
        return None
    try:
        r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_streams", "-show_format", "-of", "json", path], timeout=60)
        data = json.loads(r.stdout or b"{}")
        streams = data.get("streams") or []
        if not streams:
            return None
        s = streams[0]
        w, h = int(s.get("width", 0)), int(s.get("height", 0))
        dur = s.get("duration") or data.get("format", {}).get("duration") or 0
        rot = 0
        if "tags" in s and "rotate" in s["tags"]:
            rot = int(float(s["tags"]["rotate"]))
        for sd in s.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
        if abs(rot) % 180 == 90:
            w, h = h, w
        return float(dur), w, h
    except Exception as e:
        log.debug(f"ffprobe failed for {path}: {e}")
        return None


def make_thumbnail(path: str, duration: float, out: Path):
    if not HAS_FFMPEG:
        return None
    ss = min(1.0, duration / 2) if duration else 0
    r = run(["ffmpeg", "-y", "-v", "error", "-ss", f"{ss:.2f}", "-i", path,
             "-frames:v", "1",
             "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
             "-q:v", "5", str(out)], timeout=120)
    if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
        return str(out)
    return None


def remux_to_mp4(path: str, out: Path):
    """Fast container change (no re-encoding) so Telegram can stream the video."""
    if not HAS_FFMPEG:
        return None
    r = run(["ffmpeg", "-y", "-v", "error", "-i", path,
             "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
             "-movflags", "+faststart", str(out)], timeout=1800)
    if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
        return str(out)
    if out.exists():
        out.unlink()
    return None


def convert_image(path: str, out_stem: Path, max_side=None):
    """Convert webp/bmp/tiff/heic (or an oversized photo) to JPEG/PNG for a photo upload."""
    if Image is None:
        return None
    try:
        with Image.open(path) as im:
            if getattr(im, "is_animated", False):
                return None  # animated webp etc. -> send as-is
            if max_side:
                im.thumbnail((max_side, max_side))
            if im.mode in ("RGBA", "LA", "P"):
                out = out_stem.with_suffix(".png")
                im.save(out, "PNG", optimize=True)
            else:
                out = out_stem.with_suffix(".jpg")
                im.convert("RGB").save(out, "JPEG", quality=92)
        return str(out)
    except Exception as e:
        log.debug(f"image convert failed {path}: {e}")
        return None


def photo_ok(path: str) -> bool:
    """Telegram photo rules: <=10 MB, w+h <= 10000, aspect ratio <= 20."""
    if os.path.getsize(path) > PHOTO_MAX_BYTES:
        return False
    if Image is None:
        return True
    try:
        with Image.open(path) as im:
            w, h = im.size
        return (w + h) <= 10000 and max(w, h) / max(1, min(w, h)) <= 20
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# Uploader
# --------------------------------------------------------------------------- #
class Uploader:
    def __init__(self, client: TelegramClient, cfg: dict, db: UploadDB, dry_run: bool):
        self.client = client
        self.cfg = cfg
        self.db = db
        self.dry = dry_run
        self.group = None
        self.end_sticker = None
        self.cur_line = ""
        self.stats = dict(uploaded=0, duplicates=0, failed=0, skipped=0, folders=0)

    # ---------- helpers ----------
    async def send(self, coro_factory, what: str):
        """Run a Telegram call with FloodWait handling + retries."""
        for attempt in range(5):
            try:
                return await coro_factory()
            except errors.FloodWaitError as e:
                log.warning(f"  Telegram rate limit, waiting {e.seconds}s ...")
                await asyncio.sleep(e.seconds + 2)
            except (ConnectionError, asyncio.TimeoutError, OSError) as e:
                log.warning(f"  Network problem ({e}), retry {attempt + 1}/5 ...")
                await asyncio.sleep(10 * (attempt + 1))
        raise RuntimeError(f"giving up on {what}")

    def remove_file(self, path: str, sha: str, reason: str):
        if self.dry:
            return
        try:
            os.remove(path)
            self.db.event(reason, path, sha)
        except OSError as e:
            log.warning(f"  Could not delete {path}: {e}")
            self.db.event("delete_failed", path, sha, str(e))

    async def load_end_sticker(self):
        if not self.cfg.get("end_sticker_from_saved_messages"):
            return
        try:
            async for m in self.client.iter_messages("me", limit=300):
                if m.sticker:
                    self.end_sticker = m.media.document
                    log.info("Using the latest sticker from your Saved Messages as end marker.")
                    return
        except Exception as e:
            log.debug(f"sticker lookup failed: {e}")
        log.info(f"No sticker in Saved Messages - using {self.cfg['end_emoji']} as end marker.")

    @staticmethod
    def folder_label(folder: str) -> str:
        name = os.path.basename(folder.rstrip("/"))
        if folder.rstrip("/") in ("/storage/emulated/0",) or re.fullmatch(
                r"/storage/[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}", folder.rstrip("/")):
            return "Phone storage (root)"
        return name or folder


    # ---------- upload a single file ----------
    async def upload_one(self, path: str, idx: int, total: int):
        ext = Path(path).suffix.lower()
        name = os.path.basename(path)
        caption = html.escape(name) if self.cfg["caption_with_filename"] else None
        tmp_files = []
        TMP_DIR.mkdir(exist_ok=True)
        stem = TMP_DIR / f"up_{os.getpid()}_{idx}"

        last_pct = [-1]

        def progress(sent, total_bytes):
            pct = int(sent * 100 / total_bytes) if total_bytes else 100
            if pct // 5 != last_pct[0] // 5:
                last_pct[0] = pct
                print(f"\r\033[K{self.cur_line}  {pct}%", end="", flush=True)

        try:
            kwargs = dict(caption=caption, parse_mode="html", progress_callback=progress)

            if ext in VIDEO_EXT:
                send_path = path
                if ext not in STREAMABLE_VIDEO_EXT and self.cfg["convert_videos_to_mp4"]:
                    mp4 = remux_to_mp4(path, stem.with_suffix(".mp4"))
                    if mp4:
                        send_path = mp4
                        tmp_files.append(mp4)
                info = probe_video(send_path)
                if info:
                    dur, w, h = info
                    kwargs["attributes"] = [
                        DocumentAttributeVideo(duration=int(round(dur)), w=w, h=h,
                                               supports_streaming=True),
                        DocumentAttributeFilename(Path(name).stem + Path(send_path).suffix),
                    ]
                    thumb = make_thumbnail(send_path, dur, stem.with_suffix(".thumb.jpg"))
                    if thumb:
                        kwargs["thumb"] = thumb
                        tmp_files.append(thumb)
                kwargs["supports_streaming"] = True
                return await self.send(
                    lambda: self.client.send_file(self.group, send_path, **kwargs), name)

            if ext in GIF_EXT:
                # Telegram turns an image/gif document into an auto-playing GIF
                return await self.send(
                    lambda: self.client.send_file(self.group, path, **kwargs), name)

            # ---- still images ----
            send_path = path
            if ext in CONVERT_IMAGE_EXT:
                conv = convert_image(path, stem)
                if conv:
                    send_path = conv
                    tmp_files.append(conv)
                else:
                    # can't convert (e.g. animated webp / heic without plugin): send as file
                    return await self.send(lambda: self.client.send_file(
                        self.group, path, force_document=True, **kwargs), name)

            if not photo_ok(send_path):
                if self.cfg["oversized_images"] == "resize":
                    conv = convert_image(send_path, stem.with_name(stem.name + "_r"),
                                         max_side=4096)
                    if conv and photo_ok(conv):
                        tmp_files.append(conv)
                        send_path = conv
                    else:
                        return await self.send(lambda: self.client.send_file(
                            self.group, path, force_document=True, **kwargs), name)
                else:
                    # full quality as a file (still shows an image preview)
                    return await self.send(lambda: self.client.send_file(
                        self.group, path, force_document=True, **kwargs), name)

            try:
                return await self.send(
                    lambda: self.client.send_file(self.group, send_path, **kwargs), name)
            except errors.RPCError as e:
                log.warning(f"  Photo rejected ({e.__class__.__name__}), sending as file.")
                return await self.send(lambda: self.client.send_file(
                    self.group, path, force_document=True, **kwargs), name)
        finally:
            print("\r\033[K", end="", flush=True)
            for t in tmp_files:
                try:
                    os.remove(t)
                except OSError:
                    pass

    # ---------- process one folder ----------
    async def process_folder(self, folder: str, files: list):
        max_bytes = self.cfg["max_file_size_mb"] * 1024 * 1024
        pending = []   # (path, sha, size)
        seen_now = set()

        # 1) hash + de-duplicate BEFORE posting anything for this folder
        for p in files:
            try:
                size = os.path.getsize(p)
                if size > max_bytes:
                    log.warning(f"  SKIP (too big {size / 1e6:.0f} MB): {p}")
                    self.db.event("skipped_too_big", p, "", str(size))
                    self.stats["skipped"] += 1
                    continue
                sha = sha256_file(p)
            except OSError as e:
                log.warning(f"  Cannot read {p}: {e}")
                self.stats["skipped"] += 1
                continue

            if self.db.is_uploaded(sha) or sha in seen_now:
                self.stats["duplicates"] += 1
                log.info(f"  duplicate, already in group -> deleted: {os.path.basename(p)}")
                if self.cfg["delete_duplicates"]:
                    self.remove_file(p, sha, "deleted_duplicate")
                continue
            seen_now.add(sha)
            pending.append((p, sha, size))

        if not pending:
            return

        label = self.folder_label(folder)
        log.info(f"\n📂 {label}  ({len(pending)} new)")
        if self.dry:
            for p, _, size in pending:
                log.info(f"   would upload: {os.path.basename(p)}  ({size / 1e6:.1f} MB)")
            self.stats["uploaded"] += len(pending)
            self.stats["folders"] += 1
            return

        # 2) folder header message
        header = f"📂 <b>{html.escape(label)}</b>\n🖼 {len(pending)} media"
        await self.send(lambda: self.client.send_message(
            self.group, header, parse_mode="html"), "header")

        # 3) upload every file, delete right after success
        done = 0
        for i, (p, sha, size) in enumerate(pending, 1):
            name = os.path.basename(p)
            self.cur_line = f"  [{i}/{len(pending)}] {name}  ({size / 1e6:.1f} MB)"
            try:
                if not os.path.exists(p):
                    log.warning(f"{self.cur_line}  - file vanished, skipped")
                    continue
                msg = await self.upload_one(p, i, len(pending))
                msg_id = msg.id if msg else None
                self.db.add_upload(sha, size, name, folder, msg_id)
                self.db.event("uploaded", p, sha, f"msg {msg_id}")
                self.stats["uploaded"] += 1
                done += 1
                log.info(f"{self.cur_line}  ✔")
                if self.cfg["delete_after_upload"]:
                    self.remove_file(p, sha, "deleted_after_upload")
            except Exception as e:
                self.stats["failed"] += 1
                log.error(f"{self.cur_line}  ✘ FAILED: {e}")
                self.db.event("failed", p, sha, repr(e))
            await asyncio.sleep(self.cfg["delay_between_uploads_sec"])

        # 4) end-of-folder marker
        footer = f"✅ <b>End of {html.escape(label)}</b> — {done} media"
        await self.send(lambda: self.client.send_message(
            self.group, footer, parse_mode="html"), "footer")
        if self.end_sticker is not None:
            await self.send(lambda: self.client.send_file(self.group, self.end_sticker),
                            "sticker")
        else:
            await self.send(lambda: self.client.send_message(
                self.group, self.cfg["end_emoji"]), "end emoji")
        self.stats["folders"] += 1

    # ---------- full pass ----------
    async def run_once(self, folders: dict):
        for k in self.stats:
            self.stats[k] = 0
        for folder in sorted(folders, key=natural_key):
            await self.process_folder(folder, folders[folder])
        s = self.stats
        log.info(f"\nDone. folders: {s['folders']}  uploaded: {s['uploaded']}  "
                 f"duplicates deleted: {s['duplicates']}  failed: {s['failed']}  "
                 f"skipped: {s['skipped']}  (total in log: {self.db.count()})")


# --------------------------------------------------------------------------- #
# Group selection
# --------------------------------------------------------------------------- #
async def pick_group(client: TelegramClient, cfg: dict):
    print("\nYour groups / channels:")
    dialogs = [d async for d in client.iter_dialogs() if d.is_group or d.is_channel]
    for i, d in enumerate(dialogs[:60], 1):
        print(f"  {i:2d}. {d.name}")
    print("\nType a number from the list, or paste an @username / t.me invite link.")
    while True:
        ans = input("Target group: ").strip()
        try:
            if ans.isdigit() and 1 <= int(ans) <= len(dialogs[:60]):
                d = dialogs[int(ans) - 1]
                cfg["group"], cfg["group_title"] = d.id, d.name
            else:
                ent = await client.get_entity(ans)
                cfg["group"] = ans
                cfg["group_title"] = getattr(ent, "title", ans)
            save_config(cfg)
            print(f"Saved target: {cfg['group_title']}")
            return
        except Exception as e:
            print(f"Could not use that ({e}). Try again.")


def ask_phone() -> str:
    while True:
        p = re.sub(r"[\s\-()]", "", input(
            "Phone number WITH country code (e.g. +91XXXXXXXXXX): ").strip())
        if re.fullmatch(r"\d{10}", p):
            print("  That looks like a number without country code.")
        elif re.fullmatch(r"\+?\d{8,15}", p):
            return p if p.startswith("+") else "+" + p
        print("  Please include your country code, e.g. +91 for India -> +91XXXXXXXXXX")


async def login(client: TelegramClient):
    """Log in, re-asking for the phone number if Telegram rejects it."""
    await client.connect()
    if await client.is_user_authorized():
        return
    print("\n=== Telegram login (only needed once) ===")
    while True:
        try:
            await client.start(phone=ask_phone)
            return
        except errors.PhoneNumberInvalidError:
            print("  Telegram says that phone number is invalid. Try again.")
        except errors.PhoneNumberBannedError:
            sys.exit("This phone number is banned by Telegram.")


async def resolve_group(client: TelegramClient, cfg: dict):
    g = cfg["group"]
    try:
        return await client.get_entity(g)
    except (ValueError, TypeError):
        await client.get_dialogs()   # fill entity cache, then retry
        return await client.get_entity(g)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
async def main():
    ap = argparse.ArgumentParser(description="Upload all phone media to a Telegram group.")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen only")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--loop", type=int, metavar="MIN", help="re-scan every MIN minutes")
    ap.add_argument("--setup", action="store_true", help="choose the target group again")
    args = ap.parse_args()

    cfg = load_config()
    ask_api_credentials(cfg)
    save_config(cfg)  # writes any new default keys

    if not HAS_FFMPEG:
        log.warning("⚠ ffmpeg not found - videos will upload without thumbnails/duration. "
                    "Install with:  pkg install ffmpeg")
    if Image is None:
        log.warning("⚠ Pillow not found - webp/bmp/heic won't be converted. "
                    "Install with:  pkg install python-pillow")

    client = TelegramClient(SESSION_FILE, cfg["api_id"], cfg["api_hash"])
    await login(client)

    if args.setup or not cfg.get("group"):
        await pick_group(client, cfg)

    db = UploadDB(DB_FILE)
    up = Uploader(client, cfg, db, args.dry_run)
    up.group = await resolve_group(client, cfg)
    log.info(f"Target group: {cfg.get('group_title') or cfg['group']}")
    await up.load_end_sticker()

    confirmed = args.yes or args.dry_run
    while True:
        folders = scan_media(cfg)
        n_files = sum(len(v) for v in folders.values())
        log.info(f"Found {n_files} media files in {len(folders)} folders.")

        if n_files and not confirmed:
            action = "upload them and DELETE each file from the phone after upload" \
                if cfg["delete_after_upload"] else "upload them"
            ans = input(f"\nAbout to {action}. Continue? [y/N]: ").strip().lower()
            if ans != "y":
                print("Cancelled.")
                break
            confirmed = True

        if n_files:
            await up.run_once(folders)

        if not args.loop:
            break
        log.info(f"Sleeping {args.loop} min ... (Ctrl+C to stop)")
        await asyncio.sleep(args.loop * 60)

    shutil.rmtree(TMP_DIR, ignore_errors=True)
    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
