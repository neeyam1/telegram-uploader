# Telegram Media Uploader for Android

This script uploads every image, video and GIF on your phone to a Telegram group.
It deletes each file from the phone after the upload succeeds.
It keeps a log, so if the same media shows up again later it is skipped and deleted automatically.

## What it does

- **Scans the whole phone.** That means internal storage and SD cards, every folder, whatever it's called.
  It also looks in WhatsApp's folders (`Android/media/...`). Folders hidden from the gallery (they contain a `.nomedia` file) are skipped by default.
- **Folders become blocks in the group.** For each folder:
  1. A header message: `📂 Folder name`, its path, and the number of files
  2. All the media
  3. `✅ End of Folder name — N media`, followed by your **sticker** (or 🏁)
- **Media looks normal in the group.**
  - Photos are sent as real photos.
  - Videos are sent as streamable videos you tap to play, with a thumbnail and duration.
  - GIFs autoplay.
  - `mkv/mov/avi…` files are repackaged as mp4 so they play inside Telegram.
  - `webp/bmp/heic` files are converted to normal photos.
- **Deletes after upload.** A file is removed only after Telegram confirms it was uploaded.
- **Finds duplicates by content.** Each file gets a SHA-256 fingerprint, so a renamed copy or a copy in another folder is still recognised.
  It is skipped and deleted.
- **Keeps logs.**
  - `uploaded.db` lists everything uploaded or deleted.
  - `uploader.log` is a readable text log.

## Setup (one time)

1. Install **Termux** from **F-Droid** (https://f-droid.org/packages/com.termux/).
   The Play Store version is outdated.
   For the optional sticker and wake-lock features, also install **Termux:API** from F-Droid.
2. Copy this folder to the phone, for example to `Download/telegram-uploader`.
3. In Termux:
   ```bash
   termux-setup-storage                      # tap Allow
   cp -r ~/storage/downloads/telegram-uploader ~/
   cd ~/telegram-uploader
   bash setup.sh
   ```
4. If you're on **Android 11 or newer**: open Settings → Apps → Termux → Permissions → Files and media, and choose **Allow management of all files**.
   Without this, the script can't delete files.
5. Get an **API ID** and **API Hash** from https://my.telegram.org → *API development tools*.

## Run

```bash
cd ~/telegram-uploader
./run.sh --dry-run    # TEST: lists what would be uploaded, touches nothing
./run.sh              # real run (asks once for confirmation)
./run.sh --yes --loop 30   # keep running, check for new media every 30 min
./run.sh --setup      # change target group
```

On the first run it asks for your API ID and API Hash, then your phone number and the login code Telegram sends you.
Then it shows your groups so you can pick one.
All of this is saved, so you only do it once.

**Custom end-of-folder sticker:** send the sticker you want to your own **Saved Messages** in Telegram.
The script uses the most recent sticker it finds there.

Keep Termux open, or pull down the notification and tap "Acquire wakelock".
`run.sh` does this for you.

## Settings (`config.json`, created on first run)

| key | default | meaning |
|---|---|---|
| `exclude_paths` | Android/data, Android/obb, Telegram folders | folders never scanned |
| `skip_hidden_folders` | `true` | skip `.thumbnails`, `.trash` and other hidden folders, plus `.trashed-` files (already in the bin) |
| `skip_nomedia_folders` | `true` | skip folders hidden from the gallery, e.g. WhatsApp "Sent" and "Private". Set to `false` to upload those too |
| `min_file_size_kb` | `0` | set to e.g. `20` to ignore tiny app icons |
| `max_file_size_mb` | `2000` | use `4000` if you have Telegram Premium |
| `delete_after_upload` | `true` | delete the original after upload |
| `delete_duplicates` | `true` | delete files that are already in the group |
| `caption_with_filename` | `false` | put the file name under each media |
| `oversized_images` | `"document"` | photos over 10 MB: `"document"` keeps full quality, `"resize"` sends a smaller photo |
| `end_emoji` | `🏁` | marker used when there is no sticker in Saved Messages |

> ⚠️ **Telegram's own download folders are excluded on purpose.** Telegram recompresses photos, so the copies it downloads to your phone are not identical to the originals.
> If those folders were scanned, the script would upload the same pictures again in an endless loop.

> ⚠️ **Test with `--dry-run` first.** Deleting files from the phone cannot be undone.
