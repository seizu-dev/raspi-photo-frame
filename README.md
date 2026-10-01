# raspi-photo-frame

**English** | [日本語](README-ja.md)

A digital photo frame built with a Raspberry Pi Zero 2 W and [Immich](https://immich.app/).

It pulls photos from Immich and shows them as a slideshow you can control by touch.
A motion sensor turns the display off when nobody is around, and it wakes up when
someone approaches or touches the screen. There is no desktop environment — the app
draws directly through SDL2's KMSDRM driver.

**Running continuously on a Zero 2 W with 512 MB of RAM is the single hardest
constraint in this design.** The number of image buffers and the shape of the image
pipeline were both worked backwards from that limit.

## Features

- Pulls photos from an Immich album, your favorites, or a daily pickup rotation
  (a shared Google Drive folder, a local folder, or S3-compatible storage work too — see the appendices below)
- Four transition effects (crossfade, fade to black, slide, wipe) plus random selection
- Three ways to fit a photo to the screen (contain, cover, or cover only when the
  orientation matches)
- Overlays for the clock, capture date, photo counter, and a countdown gauge to the
  next slide
- Touch to advance photos, with three screens: menu, settings, and album picker
- Display sleep and wake driven by an AM312 motion sensor and by touch (DRM DPMS)
- Switchable UI language (Japanese / English) and selectable date and time formats
- Images are cached to disk already sized for the display, so nothing is resized at runtime

## What you need

| | |
|---|---|
| Board | Raspberry Pi Zero 2 W (512 MB RAM / VideoCore IV / 2.4 GHz Wi-Fi only) |
| OS | Raspberry Pi OS Lite, **ARM64 (64-bit)** |
| Display | A mini HDMI display plus a USB touch panel (1024x600) |
| Sensor | AM312 PIR motion sensor (GPIO 18, optional) |
| Other | microSD card, 16 GB or larger, and a power supply |
| Server | A self-hosted Immich instance (or a shared Google Drive folder — see the appendix) |

**The Zero 2 W has no DSI connector.** The official Raspberry Pi touch display
(which connects over DSI) cannot be used, so this project assumes mini HDMI plus a
USB touch panel.

## If nothing appears on screen

Two settings are behind a blank screen. The first is always required and is already
in place; the second depends on your hardware.

### 1. `SDL_RENDER_DRIVER=opengles2`

The VideoCore IV only supports OpenGL ES 2.0. SDL2 picks `opengl` by default, and
**that renderer is created successfully and then silently ignores every draw call.**
You end up in a confusing state where `Renderer.clear()` fills the screen but no
texture is ever drawn.

This is already set in `docker-compose.yml`. Do not drop it while tidying up
environment variables during debugging.

### 2. A mode line in `cmdline.txt`

Depending on your mini HDMI adapter and panel, `/boot/firmware/cmdline.txt` may need
a mode line. The hardware this was developed against needed this one:

```
video=HDMI-A-1:1024x600MR@50e
```

There, the mini HDMI adapter could not carry a pixel clock at or above 40 MHz, so the
standard 1024x600@60 (51.5 MHz) produced no picture at all. With the line above it
runs at 1024x600 @ 49.61 Hz / 36.36 MHz.

If your display works without it, you don't need it. If it doesn't, the value you
need depends on your adapter and panel. Section 9-9 of
[SPECIFICATION.md](SPECIFICATION.md) walks through how this was isolated.

## Setup

This section covers the default path: Immich as the photo source. If you want to use
a shared Google Drive folder instead, finish this section first, then see the
[appendix](#appendix-using-google-drive-as-the-photo-source) below — it only changes
a couple of environment variables.

### Preparing the host

1. Check that `/boot/firmware/config.txt` contains `dtoverlay=vc4-kms-v3d` (Full KMS).
   It is enabled by default on current Raspberry Pi OS, but rendering and display
   sleep both depend on it
2. Only if nothing appears on screen: add a `video=` line to `cmdline.txt`
   (see [the mode line section](#2-a-mode-line-in-cmdlinetxt) above)
3. Detach the framebuffer console, so console text does not flash on screen every
   time the display sleeps or wakes

```bash
sudo install -m 755 tools/host-setup/pf-fbcon-off.sh /usr/local/sbin/pf-fbcon-off.sh
sudo install -m 644 tools/host-setup/pf-fbcon-off.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pf-fbcon-off.service
```

### Credentials and configuration

```bash
cp .env.sample .env                              # put your Immich URL and API key here
cp config/settings.sample.json config/settings.json
```

Your Immich API key only needs three permissions (confirmed against Immich 2.7.5's OpenAPI spec):

| Permission | API it backs | Used for |
|---|---|---|
| `album.read` | `GET /api/albums`, `GET /api/albums/{id}` | Album list, album photos, daily pickup |
| `asset.read` | `POST /api/search/metadata` | Favorites (`source: "favorites"`) |
| `asset.view` | `GET /api/assets/{id}/thumbnail` | Photo bodies (`preview`) and thumbnails |

Nothing else is used, including `asset.download`. If a permission is missing, Immich
responds with `403` and `Missing required permission: <name>`
(`tools/verification/immich_probe.py` can help narrow it down).

`GID_*` in `.env` holds device group IDs, and **these differ from host to host.**
Check them on your own machine before filling them in:

```bash
getent group video render input gpio
```

### Running it

The image is published to GHCR by a GitHub Actions release workflow that builds a
native arm64 image on tag push, so pulling it is the normal path:

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

Startup on boot is handled by `restart: unless-stopped`. There is no systemd unit.

**Building natively on the device is a fallback** (e.g. if the GHCR image for a tag
isn't published yet, or you're testing a local change):

```bash
docker compose build     # builds natively on the ARM64 device; no QEMU needed
docker compose up -d
```

**A build takes more than ten minutes on a Zero 2 W** and saturates the CPU while it
runs. If you are working over SSH, detach it with `setsid nohup` and poll the log
file for the result.

## Controls

| Gesture | Action |
|---|---|
| Tap the left 20% of the screen | Previous photo |
| Tap the right 20% of the screen | Next photo |
| Tap the center | Briefly show the clock, caption, and gear button |
| Tap the gear button (top left) | Open the menu |

The menu leads to the settings screen and the album picker. Changes take effect
immediately.

## Configuration

Settings live in `config/settings.json`. Most of them can also be changed from the
settings screen.

| Key | Default | Description |
|---|---|---|
| `language` | `ja` | UI language (`ja` / `en`) |
| `time_format` | `24h` | Clock format (`24h` / `12h`) |
| `date_format` | `ymd_slash` | Capture date format (`ymd_slash` / `mdy_slash` / `dmy_slash` / `long`) |
| `interval` | `10` | Seconds each photo is shown |
| `transition` | `crossfade` | Transition effect (`crossfade` / `fade_black` / `slide` / `wipe` / `random`) |
| `transition_duration` | `1.0` | Seconds a transition takes |
| `display_mode` | `sequential` | Playback order (`sequential` / `random`) |
| `photo_fit` | `contain` | How a photo is fitted (`contain` / `cover` / `smart`) |
| `source` | `favorites` | Photo source (`favorites` / `album` / `daily_pickup`) |
| `album_id` | `''` | Album ID, used when `source` is `album` |
| `show_clock` | `true` | Show the clock |
| `show_comment` | `true` | Show the capture date and caption |
| `show_countdown` | `true` | Show the countdown gauge to the next slide |
| `comment_font_size` | `24` | Caption font size in pixels |
| `power_saving_enabled` | `true` | Turn the display off after a period of inactivity |
| `power_saving_timeout` | `300` | Seconds of inactivity before sleeping |
| `display_wakeup_delay` | `3.0` | Seconds to wait after waking before accepting input |
| `motion_sensor_enabled` | `true` | Enable or disable the motion sensor |
| `power_schedule_enabled` | `false` | Keep the display always on during a daily time window (for signage use) |
| `power_schedule_start` | `540` | Start of the always-on window, in minutes since midnight (`540` = 09:00) |
| `power_schedule_end` | `1080` | End of the always-on window, in minutes since midnight (`1080` = 18:00) |
| `power_schedule_off_hours` | `normal` | Behavior outside the window (`normal` / `force_off`) |
| `photo_cache_max_mb` | `512` | Image cache limit in MB (`0` means unlimited) |
| `cache_lifetime_hours` | `24` | How long a cached photo list stays valid |
| `daily_pickup_count` | `3` | Albums picked per day in daily pickup mode |

The default of 3.0 seconds for `display_wakeup_delay` comes from measuring how long
the panel actually takes to show an image (roughly 2.0 to 2.4 seconds), with some
headroom. **Completing the DPMS=On call does not mean the panel is displaying yet.**

### Fitting photos (`photo_fit`)

| Value | Behavior |
|---|---|
| `contain` | Fit the whole photo on screen. Photos with a different aspect ratio get black bars |
| `cover` | Fill the screen. Anything outside the frame is cropped |
| `smart` | Use `cover` only when the photo and the screen share the same orientation, otherwise `contain` |

Each mode keeps its own cache files, so **the first pass after switching modes runs at
a lower frame rate** while that mode's cache fills. Once it is populated, the frame
rate sits at vsync again.

## Development

Development happens in VS Code Dev Containers. It runs on x86, and you view the GUI
over VNC:

```
http://localhost:6080/vnc.html?show_dot=true
```

The app hides the mouse cursor (so no cursor appears on the real touch panel), which
means you will not see a cursor in noVNC unless you add `show_dot`.

The resolution matches the device at 1024x600. Python dependencies are baked into the
image, so pygame-ce, SDL, and Python are the same versions as on the device.

**There are three verification tiers, and it matters which one a result came from.**

| Tier | Environment | SDL driver | What it can verify |
|---|---|---|---|
| 1 | Dev Container (x86 / VNC) | `x11` | UI layout, logic layer, Immich connectivity |
| 2 | Directly on the device (ARM64) | `kmsdrm` | KMSDRM rendering, touch, GPIO, display sleep, fps |
| 3 | Container on the device (ARM64 / Docker) | `kmsdrm` | Everything, in its final deployed form |

**Performance and memory numbers are only trusted from tier 3.** The architecture,
SDL driver, GPU, and available RAM all differ, so something working in tier 1 says
nothing about how the device behaves.

## Documentation

- [SPECIFICATION.md](SPECIFICATION.md) — the full specification and measurements taken
  on real hardware
- [.claude/architecture.md](.claude/architecture.md) — settled technical decisions,
  forbidden patterns, and the list of places that must be changed together
- [.claude/coding-style.md](.claude/coding-style.md) — coding conventions
- [.claude/workflows.md](.claude/workflows.md) — operational procedures for the device
  (SSH, deployment, measurement commands) and the three-tier verification environment
- [tools/verification/README.md](tools/verification/README.md) — verification scripts

**These documents are written in Japanese.** This README is the only English
document in the repository.

`.claude/workflows.md` is part of this repository, but the actual connection
details it uses are placeholder variables (`$PF_HOST`, `$PF_HOST_TUNNEL`,
`$PF_REMOTE_DIR`, `$PF_REMOTE_HOME`). If you use it against your own device, define
those same variable names for your own connection details.

Only the development log and the actual connection values are private: under
`.claude/context/`, `current-sprint.md` (working context), `known-issues.md` (known
issues and measurements taken), and `environment.md` (the actual connection values).
These contain details about a home network, so they are kept in a separate
repository and cloned into `.claude/context/` to overlay on top of this one.
Comments and documents here still refer to `.claude/context/known-issues.md` as a
source; that file does not exist unless you have cloned it. **Building and running
the app is unaffected either way** — `.claude/context/` is not required for either.

## Design notes

- **The GUI is pygame-ce.** It uses `Renderer` and `Texture` from
  `pygame._sdl2.video` so the GPU composites transitions. Doing full-screen alpha
  compositing on the CPU (`Surface.blit`) does not hold up on a Zero 2 W
- **At most three textures stay resident** (current photo, next photo, UI overlay),
  and prefetching never goes beyond the next single photo
- **Images are cached to disk already sized for the display.** Nothing is resized in
  the drawing path
- **Display sleep goes through DRM DPMS via ctypes.** `vcgencmd display_power` does
  nothing under Full KMS, and `/sys/class/graphics/fb0/blank` stops working once SDL
  holds DRM master. Only one process can hold DRM master, so the app tears down SDL
  to release it before sleeping
- **While the display is off there is no SDL, so pygame events are unavailable.**
  Waking on touch is done by reading `/dev/input` directly

## Appendix: Using Google Drive as the photo source

Instead of Immich, the app can show photos from a shared Google Drive folder. It
authenticates as a service account (SA), not a personal Google login, and reads
subfolders of a root folder as albums.

### Setup

1. In the Google Cloud console, create (or pick) a project and enable the
   **Google Drive API**.
2. Create a **service account** and download its JSON key.
3. Place the key file where the container can read it (e.g. inside `config/`, the
   same bind-mounted directory as `settings.json`). Keep the permissions tight
   (`chmod 600`) and make sure it's readable by uid 1000. **Do not commit this
   file** — `.gitignore` already excludes JSON files in `config/` other than the
   sample settings file.
4. Share the folder you want to display with the service account's email address
   (found in the JSON key as `client_email`), with **Viewer** access.
5. Note that folder's ID. Open the folder in Google Drive in a browser; the ID is the
   part of the address bar after `/folders/`:

   ```
   https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz_EXAMPLE?usp=sharing
                                          └─────────── folder ID ───────────┘
   ```

   Leave out anything after it, such as `?usp=sharing`. The same ID also appears in
   the link you get from "Share" → "Copy link".

This repository's `docker-compose.yml` already bind-mounts `./config:/config`, so
placing the key file under `config/` lets the container read it without any change
to volumes.

### `.env`

```bash
PF_PHOTO_PROVIDER=gdrive
GDRIVE_ROOT_FOLDER_ID=<the folder ID from step 5>
# Only needed if the key isn't at the default path (config/gdrive-service-account.json):
# GDRIVE_SA_KEY_FILE=/config/gdrive-service-account.json
```

### Folder layout

- Each subfolder directly under the root is treated as one album.
- Photos placed directly in the root (not inside a subfolder) are grouped into a
  single virtual "Unsorted" album.
- Only the immediate contents of a folder are read — nested sub-subfolders and
  shortcuts are ignored.

### Limitations

- Supported formats: JPEG, PNG, WebP, HEIC, HEIF.
- There is no "favorites" source with Google Drive — only album and daily-pickup
  selection are available.
- An album's cover is the first image in the folder, sorted by name.

Details on how HEIC/HEIF are handled (and why) are in section 9-11 of
[SPECIFICATION.md](SPECIFICATION.md).

## Appendix: Using a local folder as the photo source

Instead of Immich, the app can show photos from a folder on the device itself, with
no server and no network access. Subfolders of the root are read as albums.

### Setup

1. Create a folder on the host and put photos in it (e.g. `~/photos`). Create it
   yourself first (if the path doesn't exist, Docker creates it owned by root). The
   container mounts it read-only and never writes or deletes.
2. Set these in `.env`:

   ```bash
   PF_PHOTO_PROVIDER=local
   PF_LOCAL_PHOTO_DIR=/home/you/photos   # host path; defaults to ./photos
   ```

3. Restart with `docker compose up -d`.

Add or remove photos on the host side (`scp` / `rsync` / a Samba share, etc.).

### Folder layout

- Each subfolder directly under the root is treated as one album.
- Photos placed directly in the root (not inside a subfolder) are grouped into a
  single virtual "Unsorted" album.
- Only the immediate contents of a folder are read — nested sub-subfolders and files
  or folders whose names start with `.` are ignored.

### Limitations

- Supported formats: JPEG, PNG, WebP. HEIC/HEIF, and PNG files over 12 MP or WebP
  files over 8 MP, are not shown.
- There is no "favorites" source with a local folder — only album and daily-pickup
  selection are available.
- The capture date is shown only when the photo has it in EXIF.
- Added or removed photos are picked up the next time the photo list is loaded (at
  startup, after changing the album, or when the date rolls over).

The reasons for the per-format limits are in section 9-11 of
[SPECIFICATION.md](SPECIFICATION.md).

## Appendix: Using S3-compatible storage as the photo source

Instead of Immich, the app can show photos from an S3-compatible object store
(Google Cloud Storage via its S3-compatible XML API, MinIO, Cloudflare R2, ...).
Only Google Cloud Storage has been tried. Folders under a bucket (or a prefix inside
it) are read as albums.

### Setup (Google Cloud Storage)

1. Put photos in a bucket, one folder per album.
2. Create a service account and give it read access to the bucket (it must be able to
   list and read objects). Note that HMAC keys inherit the permissions of the service
   account they are tied to.
3. Create an HMAC key for that service account: in the Cloud Storage console, open
   **Settings**, then the **Interoperability** tab, then **Create a key for a service
   account**. The secret is shown only once. (You need `storage.hmacKeys.*` permissions
   for the project to do this.)
4. Set these in `.env` (see `.env.sample`):

   ```bash
   PF_PHOTO_PROVIDER=s3
   S3_ENDPOINT_URL=https://storage.googleapis.com
   S3_BUCKET=<your bucket>
   S3_PREFIX=photos/            # optional, with a trailing slash
   S3_ACCESS_KEY_ID=<HMAC access key>
   S3_SECRET_ACCESS_KEY=<HMAC secret>
   # S3_REGION=auto             # default
   ```

5. Restart with `docker compose up -d`.

Only path-style URLs (`<endpoint>/<bucket>/<key>`) are supported.

### Folder layout

- Each folder directly under the bucket (or under `S3_PREFIX`) is one album.
- Photos placed directly there are grouped into a virtual "Unsorted" album.
- Only the immediate contents of a folder are read; deeper nesting is ignored.

### Limitations

- Supported formats: JPEG, PNG, WebP. HEIC/HEIF are skipped, and so are files over
  the per-format pixel limits and originals over 40 MB (same reasons as the local
  folder; there is no server-side downsizing).
- There is no "favorites" source — only album and daily-pickup selection.
- The photo list is cached for `cache_lifetime_hours`, so photos you add to the
  bucket appear after the cache expires.
- The capture date comes from the original's EXIF and is read when the photo is
  cached. It is therefore missing for a photo until it has been downloaded, and may
  not show on the first pass if the next photo was not ready in time.
- **Every photo is downloaded as the original. Downloading from a cloud provider can
  incur egress charges, so check the provider's current pricing before using it.**
  Photos are downloaded once and then served from the local cache.

## License

MIT License. See [LICENSE](LICENSE).
