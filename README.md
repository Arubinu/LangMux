<p align="center">
  <img width="678" height="678" alt="LangMux" src="https://github.com/user-attachments/assets/3e54c44a-d52c-4733-b9d0-edb2fa65d660" />
</p>

*__Read this in:__ English | [Français](README.fr.md)*

Merge a **Japanese original (VOSTFR)** and a **French dub (VF)** of the same
episode into a single, cleanly tagged `.mkv` — from a web interface or the
command line. Built for anime, but works for any pair of files in different
languages.

For each episode, the output file contains:
- the video,
- the **Japanese** audio (default track, unless you choose otherwise),
- the **French** audio,
- the **French subtitles**, *forced only when the main audio is not French*.

Two behaviors worth knowing:
- **In-sync French is preferred.** If the VO file already contains a French
  audio track (a "MULTI" release), that track is used — it is guaranteed in
  sync with its own video — and the separate VF file is skipped. Otherwise the
  French audio is taken from the separate VF file.
- **Sync check.** Before each merge, the duration (and frame rate) of the two
  files are compared to flag a likely desync.

---

## Requirements

Just **Docker**. `mkvtoolnix` (mkvmerge) and `ffmpeg` (ffprobe) are bundled in
the image.

## Quick start

Edit the volumes in `docker-compose.yml`, then:

```bash
docker compose up -d --build
```

Open the interface at **http://YOUR-SERVER:8080**.

> On Proxmox, run this inside the VM/LXC where Docker lives. Everything mounted
> under `/media` is the **root** the interface can browse.

### docker-compose.yml example

Mount as many sources as you like under `/media` — each one shows up as a
sub-folder at the root of the interface. Keep the lines that match your host OS:

```yaml
services:
  langmux:
    build: https://github.com/Arubinu/LangMux.git
    image: langmux:latest
    container_name: langmux
    ports:
      - "8080:8080"
    volumes:
      - ./media:/media/Docker            # local folder next to the compose file (any OS)
      - /media:/media/Medias             # Linux: a host path
      - C:/Users:/media/Utilisateurs     # Windows: a Windows folder
    environment:
      - LANGMUX_ROOT=/media
    restart: unless-stopped
```

`LANGMUX_ROOT` must match the internal mount point (`/media` here): that is
where the interface's file browser starts, and it is deliberately locked so it
cannot go above it.

### Or with plain Docker

```bash
docker build -t langmux .
docker run -d --name langmux -p 8080:8080 -v /path/to/your/anime:/media langmux
```

On Windows (PowerShell), use forward slashes and quotes:

```powershell
docker run -d --name langmux -p 8080:8080 -v "D:/Anime:/media" langmux
```

---

## Using the web interface

1. **Choose the folder** — browse to the season folder (the one holding both
   the VF and VOSTFR files), then *Analyze*. Hidden files and folders are not
   shown.
2. **Series & options** — search the series (results show the **year** and an
   **IMDB** link to verify; the **official episode count** of the detected
   season is displayed so you can check you have them all). Pick the main audio,
   the naming template, the season, the output sub-folder and the tolerated
   duration gap.
3. **Review the plan** — each episode shows its output name, both sources
   (JP/VF badges), the duration gap (a warning if it is too large or if the
   frame rates differ) and lets you swap VF/VOSTFR if needed. **Orphan**
   episodes (only one language) are listed but never merged.
4. **Merge** — a visual view with a progress ring, per-episode tiles that light
   up as they complete, live counters, and a **Cancel** button that stops the
   run cleanly (the in-progress file is removed, remaining episodes are left
   untouched). A detailed log stays available below.

Originals are never modified; results are written to the output sub-folder
(`merged/` by default).

## Command line (always available)

The same engine is usable as a CLI inside the container:

```bash
docker exec -it langmux langmux /media/Some.Series
docker exec -it langmux langmux --dry-run /media/Some.Series
docker exec -it langmux langmux --self-test
```

---

## Notes

- Series lookup uses **TVMaze** (free, no key) and returns the IMDB link for
  verification. IMDB has no free public API, so this is the most reliable
  no-config source.
- Merge progress is kept **in memory**: the server runs as a **single process**
  (waitress). Do not switch it to multi-worker.
- "Forced subtitles" (MKV) means *shown even when subtitles are turned off*. On
  a full French subtitle track in VOSTFR, that is the intended effect.
- Equal durations do not guarantee perfect sync (a constant offset can remain):
  it is a strong hint, not a certainty.
- If the VO file already contains a French track, langmux uses it and **skips**
  the separate VF file. Tell us if you'd rather always force the separate file.

---

## License

[MIT](LICENSE)
