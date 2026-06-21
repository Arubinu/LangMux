#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
langmux — Fusionne les pistes audio/sous-titres d'épisodes provenant de
sources différentes (ex: une version FRENCH/VF et une version VOSTFR d'un
même anime) en un seul .mkv propre et correctement nommé.

Workflow :
  1. Scanne le dossier courant, regroupe les fichiers par épisode.
  2. Identifie chaque fichier comme VF (FRENCH) ou VOSTFR.
  3. Cherche la série sur TVMaze (gratuit, renvoie le lien IMDB pour vérif).
  4. Pose UNE SEULE FOIS les questions (série, piste principale, saison,
   gestion des écarts de durée, modèle de nommage) pour tout le dossier.
  5. Vérifie que les deux fichiers d'un épisode ont ~la même durée (sync).
  6. Fusionne avec mkvmerge : vidéo + audio JAP (VOSTFR) + audio FR (VF)
   + sous-titres FR, avec la piste principale de ton choix par défaut.

Dépendances système :  mkvtoolnix (mkvmerge) et ffmpeg (ffprobe)
  Debian/Proxmox :  apt install mkvtoolnix ffmpeg

Usage :
  langmux [DOSSIER] [options]
  langmux --self-test          # teste le parseur de noms de fichiers
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------- #
#  Configuration / constantes
# --------------------------------------------------------------------------- #

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".webm", ".mov"}

# Jetons "qualité/source" à neutraliser pour ne pas polluer la détection
QUALITY = re.compile(
  r"\b(1080p|720p|2160p|480p|4k|x264|x265|h264|h265|hevc|10bits?|8bits?|"
  r"aac|ac3|eac3|flac|dts|truehd|opus|bluray|brrip|bdrip|web[- ]?dl|webrip|"
  r"web|hdtv|dvdrip|remux|264|265|hi10p|fansub)\b",
  re.I,
)

# Codes langue tels que reconnus par mkvmerge / ffprobe
JPN = ("jpn", "ja", "jp")
FRA = ("fre", "fra", "fr")

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
CYAN, GREEN, YELL, RED = "\033[36m", "\033[32m", "\033[33m", "\033[31m"


def c(txt, color):
  return f"{color}{txt}{RESET}" if sys.stdout.isatty() else txt


# --------------------------------------------------------------------------- #
#  Détection épisode / saison à partir du nom de fichier
# --------------------------------------------------------------------------- #

def parse_episode(filename):
  """Retourne (saison, episode) ; saison peut être None si non trouvée."""
  base = os.path.splitext(filename)[0]

  # 1) S01E01 / s1.e1 / S01 E01
  m = re.search(r"[Ss](\d{1,2})[ ._-]*[Ee](\d{1,4})", base)
  if m:
    return int(m.group(1)), int(m.group(2))

  # 2) 1x01
  m = re.search(r"(?<!\d)(\d{1,2})x(\d{1,4})(?!\d)", base)
  if m:
    return int(m.group(1)), int(m.group(2))

  # On nettoie qualité + années pour réduire les faux positifs
  cleaned = QUALITY.sub(" ", base)
  cleaned = re.sub(r"\b(19|20)\d{2}\b", " ", cleaned)  # années
  cleaned = re.sub(r"\b\d{3,4}p\b", " ", cleaned)      # résolutions résiduelles

  # 3) "Episode 01", "Ep.01", "E01"
  m = re.search(r"\b[Ee](?:p(?:isode)?)?[ ._-]*(\d{1,4})(?!\d)", cleaned)
  if m:
    return None, int(m.group(1))

  # 4) "Nom - 01"
  m = re.search(r"[-–—][ ._]*(\d{1,3})(?!\d)(?!\s*[xp])", cleaned)
  if m:
    return None, int(m.group(1))

  # 5) dernier recours : un nombre isolé de 1 à 3 chiffres
  nums = re.findall(r"(?<!\d)(\d{1,3})(?!\d)", cleaned)
  if nums:
    return None, int(nums[-1])

  return None, None


# --------------------------------------------------------------------------- #
#  Classification VF / VOSTFR
# --------------------------------------------------------------------------- #

def tokens_of(name):
  return {t.lower() for t in re.split(r"[^a-zA-Z0-9]+", name) if t}


def classify(filename, audio_langs=None):
  """Retourne 'vostfr', 'french' ou None (indéterminé)."""
  tk = tokens_of(filename)

  # Les marqueurs explicites VOSTFR priment (vostfr contient "fr" !)
  if {"vostfr", "vosta", "vost"} & tk:
    return "vostfr"
  if {"french", "truefrench", "vff", "vfq", "vfi", "vf2", "vf"} & tk:
    return "french"
  if {"vo", "jpn", "jap", "japonais", "sub", "subbed", "original"} & tk:
    return "vostfr"
  if {"fr", "fre", "fra", "vostf"} & tk:
    # "fr" seul est ambigu : on le considère VF faute de mieux
    return "french"

  # MULTI = fichier bi-langue contenant une piste FR → c'est la source VF dans une paire
  if {"multi", "multic", "multilang", "multivf"} & tk:
    if audio_langs and any(l in FRA for l in audio_langs):
      return "french"
    return "vostfr"

  # Repli : on regarde la langue audio réelle
  if audio_langs:
    if any(l in JPN for l in audio_langs):
      return "vostfr"
    if any(l in FRA for l in audio_langs) and not any(l in JPN for l in audio_langs):
      return "french"
  return None


# --------------------------------------------------------------------------- #
#  Sondage des fichiers (mkvmerge -J + ffprobe)
# --------------------------------------------------------------------------- #

def run_json(argv):
  out = subprocess.run(argv, capture_output=True, text=True)
  if out.returncode != 0:
    raise RuntimeError(out.stderr.strip() or f"échec : {' '.join(argv)}")
  return json.loads(out.stdout)


def probe_tracks(path):
  """Liste des pistes via mkvmerge -J (les TID correspondent à ceux du mux)."""
  data = run_json(["mkvmerge", "-J", path])
  tracks = []
  for t in data.get("tracks", []):
    props = t.get("properties", {})
    tracks.append({
      "id": t["id"],
      "type": t["type"],                       # video / audio / subtitles
      "lang": (props.get("language") or "und").lower(),
      "codec": t.get("codec", ""),
    })
  return tracks


def probe_media(path):
  """Retourne (durée_secondes, fps) via ffprobe."""
  data = run_json([
    "ffprobe", "-v", "error",
    "-select_streams", "v:0",
    "-show_entries", "format=duration:stream=avg_frame_rate",
    "-of", "json", path,
  ])
  dur = float(data.get("format", {}).get("duration", 0) or 0)
  fps = None
  streams = data.get("streams", [])
  if streams:
    rate = streams[0].get("avg_frame_rate", "0/0")
    try:
      num, den = rate.split("/")
      fps = float(num) / float(den) if float(den) else None
    except (ValueError, ZeroDivisionError):
      fps = None
  return dur, fps


def pick_track(tracks, ttype, prefer):
  """Renvoie le TID de la piste de type `ttype` dont la langue est dans
  `prefer` ; sinon la première piste de ce type ; sinon None."""
  candidates = [t for t in tracks if t["type"] == ttype]
  for t in candidates:
    if t["lang"] in prefer:
      return t["id"]
  return candidates[0]["id"] if candidates else None


def find_audio_by_lang(tracks, langs):
  """TID de la 1re piste audio dont la langue est dans `langs`, sinon None.
  Contrairement à pick_track, ne renvoie PAS de piste par défaut : sert à
  savoir si une langue est réellement présente dans le fichier."""
  for t in tracks:
    if t["type"] == "audio" and t["lang"] in langs:
      return t["id"]
  return None


def has_embedded_french(vost_tracks):
  """Le fichier VO contient-il déjà une piste audio française (fichier MULTI) ?"""
  return find_audio_by_lang(vost_tracks, FRA) is not None


# --------------------------------------------------------------------------- #
#  Recherche TVMaze (gratuit, renvoie l'ID IMDB)
# --------------------------------------------------------------------------- #

def tvmaze_search(query):
  url = "https://api.tvmaze.com/search/shows?q=" + urllib.parse.quote(query)
  req = urllib.request.Request(url, headers={"User-Agent": "langmux/1.0"})
  with urllib.request.urlopen(req, timeout=20) as r:
    data = json.load(r)
  results = []
  for item in data:
    s = item.get("show", {}) or {}
    results.append({
      "id": s.get("id"),
      "name": s.get("name") or "?",
      "year": (s.get("premiered") or "????")[:4],
      "language": s.get("language") or "?",
      "imdb": (s.get("externals") or {}).get("imdb"),
    })
  return results


# --------------------------------------------------------------------------- #
#  Petites aides interactives
# --------------------------------------------------------------------------- #

def ask(prompt, default=None):
  suffix = f" [{default}]" if default is not None else ""
  try:
    val = input(f"{c('?', CYAN)} {prompt}{suffix} : ").strip()
  except (EOFError, KeyboardInterrupt):
    print()
    sys.exit("Annulé.")
  return val or (default if default is not None else "")


def ask_choice(prompt, options, default=1):
  """options = liste de libellés ; renvoie l'index (1-based) choisi."""
  print(f"{c('?', CYAN)} {prompt}")
  for i, opt in enumerate(options, 1):
    print(f"    {c(str(i), BOLD)}) {opt}")
  while True:
    val = ask("Choix", default)
    try:
      n = int(val)
      if 1 <= n <= len(options):
        return n
    except ValueError:
      pass
    print(c("  Entrée invalide.", RED))


def sanitize(name):
  name = re.sub(r'[\\/:*?"<>|]', " ", name)
  return re.sub(r"\s+", " ", name).strip()


# --------------------------------------------------------------------------- #
#  Synchronisation audio (optionnelle, nécessite numpy + scipy)
# --------------------------------------------------------------------------- #

def _analyze_sync(vost_path, fr_path):
  """Analyse le décalage entre la piste VO et le fichier VF.
  Retourne le dict audio_sync ou None en cas d'erreur / module absent."""
  try:
    import audio_sync
  except ImportError:
    print(c("  [sync] numpy/scipy introuvables — pip install numpy scipy", YELL))
    return None
  print(f"    analyse sync… (peut prendre 1-2 min)")
  try:
    result = audio_sync.analyze(vost_path, fr_path)
  except Exception as e:
    print(c(f"    [sync] analyse échouée : {e}", YELL))
    return None
  tag = "OK" if result["reliable"] else c("FAIBLE CONFIANCE", YELL)
  print(f"    sync : décalage {result['offset']:+.3f}s  "
      f"vitesse {result['drift_label']}  "
      f"[{result.get('n_reliable', '?')}/{result.get('n_total', '?')} fenêtres fiables]  [{tag}]")
  if not result["reliable"]:
    print(c("    -> sync ignoré (confiance insuffisante)", YELL))
    return None
  return result


# --------------------------------------------------------------------------- #
#  Programme principal
# --------------------------------------------------------------------------- #

def check_deps():
  missing = [b for b in ("mkvmerge", "ffprobe") if not shutil.which(b)]
  if missing:
    sys.exit(
      c(f"Outils manquants : {', '.join(missing)}\n", RED)
      + "  Installe-les : apt install mkvtoolnix ffmpeg"
    )


def collect_files(directory):
  files = [
    f for f in sorted(os.listdir(directory))
    if not f.startswith(".")
    and os.path.splitext(f)[1].lower() in VIDEO_EXTS
    and os.path.isfile(os.path.join(directory, f))
  ]
  return files


def guess_name(directory, files):
  """Devine le titre de la série depuis les noms de fichiers.
  Stratégie : nettoyage de chaque fichier → préfixe commun entre tous.
  Repli sur le nom du dossier si le préfixe est trop court ou absent.
  """
  def _clean(name):
    base = os.path.splitext(name)[0]
    base = re.sub(r"[._]+", " ", base)
    base = re.sub(r"[\[(][^\])\n]{1,40}[\])]", " ", base)    # [Group] (info)
    # Tronquer au premier marqueur d'épisode — tout ce qui suit est du bruit
    base = re.sub(r"\s*[Ss]\d{1,2}[\s._-]*[Ee]\d{1,4}.*$", "", base)
    base = re.sub(r"\s*\b[Ee]p?(?:isode)?[\s._-]*\d{1,4}\b.*$", "", base, flags=re.I)
    base = re.sub(r"\s*\b\d{1,2}x\d{1,4}\b.*$", "", base)
    base = re.sub(r"\s*\bsa?isons?\s*\d+.*$", "", base, flags=re.I)
    base = re.sub(r"\s*(?<!\w)[-–—]\s*\d{1,3}(?!\d).*$", "", base)
    base = re.sub(r"^\s*\d{1,4}[\s._-]+", "", base)           # 01 titre
    base = QUALITY.sub(" ", base)
    base = re.sub(r"\b(19|20)\d{2}\b", " ", base)
    base = re.sub(
      r"\b(vostfr|vosta?|vost|french|truefrench|multi|vff?[qi2]?|vf\d?|"
      r"integrale?|subbed?|vo|jpn|jap|japonais)\b", " ", base, flags=re.I)
    return re.sub(r"\s+", " ", base).strip(" -_–—")

  cands = []
  for f in files:
    c = _clean(f)
    if c and len(c) > 2:
      cands.append(c)

  if cands:
    common = cands[0].split()
    for c in cands[1:]:
      ws = c.split()
      common = [a for a, b in zip(common, ws) if a.lower() == b.lower()]
      if not common:
        break
    result = " ".join(common).strip(" -_–—")
    if len(result) > 2:
      return result

  # Repli : nom du dossier
  cand = os.path.basename(os.path.abspath(directory))
  cand = re.sub(r"[._]+", " ", cand)
  cand = QUALITY.sub(" ", cand)
  cand = re.sub(r"[Ss]\d{1,2}([Ee]\d{1,3})?|sa?ison\s*\d+|season\s*\d+", " ", cand, flags=re.I)
  cand = re.sub(r"\b(vostfr|vost|french|truefrench|multi|vff?|integrale?)\b", " ", cand, flags=re.I)
  cand = re.sub(r"[\[\](){}]", " ", cand)
  return re.sub(r"\s+", " ", cand).strip(" -_") or "série"


def _has_chapters(path):
  """Retourne True si le fichier possède des chapitres (mkvmerge -J)."""
  try:
    return bool(run_json(["mkvmerge", "-J", path]).get("chapters", []))
  except Exception:
    return False


def measure_lufs(path, stream_index=0):
  """Mesure le niveau sonore intégrée EBU R128 (LUFS) d'un flux audio via ffmpeg.
  Retourne float ou None si indisponible (silence, erreur, etc.).
  """
  proc = subprocess.run(
    ["ffmpeg", "-i", path, "-map", f"0:a:{stream_index}",
     "-filter:a", "loudnorm=print_format=json", "-f", "null", "-"],
    capture_output=True, text=True,
  )
  m = re.search(r'"input_i"\s*:\s*"(-?[\d.]+)"', proc.stderr)
  if not m:
    return None
  val = m.group(1)
  return float(val) if val not in ("inf", "-inf") else None


def normalize_levels(mkv_path, method="replaygain"):
  """Normalise les niveaux audio du MKV.

  method = 'replaygain' : écrit REPLAYGAIN_TRACK_GAIN via mkvpropedit (non destructif).
           'reencode'   : réencode la piste la plus faible en AAC 192 kbps (compatible partout).

  Retourne list[(track_id, lufs, gain_appliqué_dB)].
  Ne modifie rien si la différence est < 0.5 dB.
  Lève RuntimeError si une mesure ou l'application échoue.
  """
  data = run_json(["mkvmerge", "-J", mkv_path])
  audio = [t for t in data.get("tracks", []) if t["type"] == "audio"]
  if len(audio) < 2:
    return []

  lufs_vals = []
  for i in range(len(audio)):
    l = measure_lufs(mkv_path, i)
    if l is None:
      raise RuntimeError(f"mesure impossible pour la piste audio {i}")
    lufs_vals.append(l)

  target = max(lufs_vals)

  results = []
  adjustments = []  # (audio_index, gain_db, track_uid)
  for i, t in enumerate(audio):
    gain = target - lufs_vals[i]
    results.append((t["id"], lufs_vals[i], gain if abs(gain) >= 0.5 else 0.0))
    if abs(gain) >= 0.5:
      adjustments.append((i, gain, t["properties"]["uid"]))

  if not adjustments:
    return results

  if method == "replaygain":
    tag_blocks = [
      f'<Tag><Targets><TrackUID>{uid}</TrackUID></Targets>'
      f'<Simple><Name>REPLAYGAIN_TRACK_GAIN</Name>'
      f'<String>{gain:+.2f} dB</String></Simple></Tag>'
      for _, gain, uid in adjustments
    ]
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<!DOCTYPE Tags SYSTEM "matroskatags.dtd">'
           '<Tags>' + "".join(tag_blocks) + "</Tags>")
    fd, tmp = tempfile.mkstemp(suffix=".xml")
    try:
      os.write(fd, xml.encode("utf-8"))
      os.close(fd)
      r = subprocess.run(["mkvpropedit", mkv_path, "--tags", f"all:{tmp}"],
                         capture_output=True, text=True)
      if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200])
    finally:
      try: os.unlink(tmp)
      except OSError: pass

  elif method == "reencode":
    for audio_index, gain, _ in adjustments:
      _reencode_audio_track(mkv_path, audio_index, gain, audio)

  return results


def _reencode_audio_track(mkv_path, audio_index, gain_db, audio_tracks):
  """Réencode la piste audio audio_index avec le gain donné, remplace le fichier en place."""
  import shutil
  fd, tmp = tempfile.mkstemp(suffix=".mkv", dir=os.path.dirname(mkv_path))
  os.close(fd)
  try:
    cmd = ["ffmpeg", "-y", "-i", mkv_path, "-map_metadata", "0",
           "-map", "0", "-c:v", "copy", "-c:s", "copy"]
    for i, t in enumerate(audio_tracks):
      props = t.get("properties", {})
      if i == audio_index:
        cmd += [f"-filter:a:{i}", f"volume={gain_db}dB",
                f"-c:a:{i}", "aac", f"-b:a:{i}", "192k"]
      else:
        cmd += [f"-c:a:{i}", "copy"]
      lang = props.get("language", "und")
      name = props.get("track_name", "")
      cmd += [f"-metadata:s:a:{i}", f"language={lang}"]
      if name:
        cmd += [f"-metadata:s:a:{i}", f"title={name}"]
    cmd.append(tmp)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
      raise RuntimeError(r.stderr.strip()[-300:])
    shutil.move(tmp, mkv_path)
  except Exception:
    try: os.unlink(tmp)
    except OSError: pass
    raise


def build_mux_cmd(out_path, title, vost, fr, primary, sub_default,
                  sync_info=None, embedded_fr_mode="auto"):
  """Construit la ligne de commande mkvmerge.
     vost / fr          = {'path':..., 'tracks':[...]}
     primary            = 'jpn' ou 'fre'
     sync_info          = dict retourné par audio_sync.analyze() ou None
     embedded_fr_mode   = 'auto'     → utilise le FR intégré si présent (défaut)
                          'external' → ignore le FR intégré, utilise le fichier VF
                          'both'     → conserve le FR intégré ET ajoute le fichier VF
  """
  argv = ["mkvmerge", "-o", out_path, "--title", title]

  jpn_aid = find_audio_by_lang(vost["tracks"], JPN)
  vost_fre_aid = find_audio_by_lang(vost["tracks"], FRA)
  if jpn_aid is None and vost_fre_aid is None:
    jpn_aid = pick_track(vost["tracks"], "audio", JPN)
  sub_id = pick_track(vost["tracks"], "subtitles", FRA)
  sub_forced = (primary != "fre")

  # Décide quelles sources FR utiliser
  use_embedded_fr = (vost_fre_aid is not None) and (embedded_fr_mode != "external")
  add_external_vf = (not use_embedded_fr) or (embedded_fr_mode == "both")

  keep = [a for a in (jpn_aid, vost_fre_aid if use_embedded_fr else None)
      if a is not None]
  argv += ["--audio-tracks", ",".join(str(a) for a in keep)] if keep else ["--no-audio"]

  if jpn_aid is not None:
    argv += ["--language", f"{jpn_aid}:jpn",
         "--track-name", f"{jpn_aid}:Japonais",
         "--default-track", f"{jpn_aid}:{'yes' if primary == 'jpn' else 'no'}"]
  if use_embedded_fr:
    # En mode "both", l'intégrée n'est pas la piste par défaut (la VF externe prime)
    emb_default = "yes" if (primary == "fre" and not add_external_vf) else "no"
    emb_label = "Français (VF intégré)" if embedded_fr_mode == "both" else "Français (VF)"
    argv += ["--language", f"{vost_fre_aid}:fre",
         "--track-name", f"{vost_fre_aid}:{emb_label}",
         "--default-track", f"{vost_fre_aid}:{emb_default}"]
  if sub_id is not None:
    argv += ["--language", f"{sub_id}:fre",
         "--track-name", f"{sub_id}:Français (sous-titres)",
         "--default-track", f"{sub_id}:{'yes' if sub_default else 'no'}",
         "--forced-track", f"{sub_id}:{'yes' if sub_forced else 'no'}"]
  argv += [vost["path"]]

  # ----- Fichier VF externe -----
  if add_external_vf and fr is not None:
    fre_aid = pick_track(fr["tracks"], "audio", FRA)
    # Chapitres : si le fichier de référence n'en a pas mais le VF en a, on les importe
    vost_has_chap = _has_chapters(vost["path"])
    vf_has_chap   = _has_chapters(fr["path"])
    argv += ["--no-video", "--no-subtitles", "--no-buttons"]
    if vost_has_chap or not vf_has_chap:
      argv += ["--no-chapters"]
    if fre_aid is not None:
      argv += ["--audio-tracks", str(fre_aid),
           "--language", f"{fre_aid}:fre",
           "--track-name", f"{fre_aid}:Français (VF)",
           "--default-track", f"{fre_aid}:{'yes' if primary == 'fre' else 'no'}"]
      if sync_info:
        import audio_sync
        flag = audio_sync.mkvmerge_sync_flag(fre_aid, sync_info)
        if flag:
          argv += ["--sync", flag]
    argv += [fr["path"]]
  return argv


def build_single_cmd(out_path, title, src, cls, primary, sub_default):
  """Réencapsule un fichier orphelin au nom standard avec étiquetage cohérent.
  cls = 'vostfr', 'french' ou 'multi' (conserve les pistes JP et FR).
  """
  argv = ["mkvmerge", "-o", out_path, "--title", title]
  tracks = src["tracks"]
  jpn_aid = find_audio_by_lang(tracks, JPN)
  fre_aid = find_audio_by_lang(tracks, FRA)
  sub_id = pick_track(tracks, "subtitles", FRA)
  sub_forced = (primary != "fre")
  audios = [t["id"] for t in tracks if t["type"] == "audio"]

  # Sans tag de langue : on déduit de la classe
  if jpn_aid is None and fre_aid is None and audios:
    if cls == "french":
      fre_aid = audios[0]
    elif cls == "multi" and len(audios) >= 2:
      jpn_aid, fre_aid = audios[0], audios[1]
    else:
      jpn_aid = audios[0]

  if jpn_aid is not None:
    argv += ["--language", f"{jpn_aid}:jpn", "--track-name", f"{jpn_aid}:Japonais"]
  if fre_aid is not None:
    argv += ["--language", f"{fre_aid}:fre", "--track-name", f"{fre_aid}:Français (VF)"]

  # Piste audio par défaut : la langue principale si présente, sinon ce qu'on a
  if primary == "jpn" and jpn_aid is not None:
    default_id = jpn_aid
  elif primary == "fre" and fre_aid is not None:
    default_id = fre_aid
  else:
    default_id = jpn_aid or fre_aid or (audios[0] if audios else None)
  for aid in audios:
    argv += ["--default-track", f"{aid}:{'yes' if aid == default_id else 'no'}"]

  if sub_id is not None:
    argv += ["--language", f"{sub_id}:fre",
         "--track-name", f"{sub_id}:Français (sous-titres)",
         "--default-track", f"{sub_id}:{'yes' if sub_default else 'no'}",
         "--forced-track", f"{sub_id}:{'yes' if sub_forced else 'no'}"]
  argv += [src["path"]]
  return argv


def main():
  ap = argparse.ArgumentParser(description="Fusionne VF + VOSTFR d'un même épisode.")
  ap.add_argument("directory", nargs="?", default=".", help="Dossier à traiter (défaut: courant)")
  ap.add_argument("--output-dir", default="merged", help="Sous-dossier de sortie (défaut: merged)")
  ap.add_argument("--tolerance", type=float, default=10.0,
          help="Écart de durée toléré en secondes (défaut: 10)")
  ap.add_argument("--dry-run", action="store_true", help="Affiche le plan sans fusionner")
  ap.add_argument("--process-orphans", action="store_true",
          help="Traite aussi les épisodes orphelins (réencapsule le fichier unique)")
  ap.add_argument("--sync", action="store_true",
          help="Détecte et corrige automatiquement le décalage audio VF/VOSTFR "
               "(nécessite numpy + scipy ; peut prendre 1-2 min par épisode)")
  ap.add_argument("--embedded-fr-mode", default="auto",
          choices=["auto", "external", "both"],
          help="Gestion du FR intégré dans un fichier MULTI : "
               "auto = l'utilise directement (défaut), "
               "external = le remplace par le fichier VF, "
               "both = conserve les deux")
  ap.add_argument("--self-test", action="store_true", help="Teste le parseur de noms")
  args = ap.parse_args()

  if args.self_test:
    return self_test()

  check_deps()
  directory = os.path.abspath(args.directory)
  if not os.path.isdir(directory):
    sys.exit(c(f"Dossier introuvable : {directory}", RED))

  files = collect_files(directory)
  if not files:
    sys.exit(c("Aucun fichier vidéo trouvé dans ce dossier.", RED))

  print(c(f"\n{len(files)} fichier(s) vidéo détecté(s) dans {directory}\n", DIM))

  # ---- 1. Regroupement par épisode -------------------------------------- #
  episodes = {}      # ep -> list of {'name','path','season'}
  unparsed = []
  for f in files:
    season, ep = parse_episode(f)
    if ep is None:
      unparsed.append(f)
      continue
    episodes.setdefault(ep, []).append(
      {"name": f, "path": os.path.join(directory, f), "season": season}
    )

  if unparsed:
    print(c("Numéro d'épisode non détecté pour :", YELL))
    for f in unparsed:
      print(f"    - {f}")
    if ask_choice("Continuer en ignorant ces fichiers ?",
            ["Oui, les ignorer", "Non, j'arrête pour corriger les noms"]) == 2:
      sys.exit("Arrêté.")

  # ---- 2. Classification VF / VOSTFR + appairage ------------------------ #
  pairs = {}          # ep -> {'vostfr':..., 'french':...}
  problems = []       # (ep, raison, fichiers) — ambigus / >2 fichiers
  orphans = []        # (ep, fichier) — une seule langue présente
  for ep, group in sorted(episodes.items()):
    if len(group) == 1:
      orphans.append((ep, group[0]))
      continue

    # On sonde les pistes une fois pour la classification + le mux
    for g in group:
      try:
        g["tracks"] = probe_tracks(g["path"])
      except RuntimeError as e:
        g["tracks"] = []
        print(c(f"  Lecture impossible: {g['name']} ({e})", RED))
      g["alangs"] = [t["lang"] for t in g.get("tracks", []) if t["type"] == "audio"]
      g["class"] = classify(g["name"], g["alangs"])

    if len(group) > 2:
      problems.append((ep, f"{len(group)} fichiers pour cet épisode", group))
      continue

    a, b = group
    # Résolution de l'appairage
    classes = {a["class"], b["class"]}
    if classes == {"vostfr", "french"}:
      vost = a if a["class"] == "vostfr" else b
      fr = b if vost is a else a
    else:
      # Ambigu : on demande UNE fois en montrant les langues audio
      print(c(f"\nÉpisode {ep:02d} : impossible de distinguer VF/VOSTFR automatiquement.", YELL))
      print(f"    1) {a['name']}   (audio: {', '.join(a['alangs']) or '?'})")
      print(f"    2) {b['name']}   (audio: {', '.join(b['alangs']) or '?'})")
      n = ask_choice("Lequel est la VF (français doublé) ?",
               [a["name"], b["name"]])
      fr = a if n == 1 else b
      vost = b if fr is a else a

    pairs[ep] = {"vostfr": vost, "french": fr, "season": vost["season"] or fr["season"]}

  if not pairs:
    print(c("\nAucune paire VF+VOSTFR exploitable.", RED))
    for ep, reason, _ in problems:
      print(f"    - Épisode {ep:02d} : {reason}")
    for ep, g in orphans:
      print(f"    - Épisode {ep:02d} : orphelin ({g['name']})")
    sys.exit(1)

  # ---- 3. Choix UNIQUES pour toute la série ----------------------------- #
  print(c("\n══════════ Configuration (une seule fois pour le dossier) ══════════", BOLD))

  # 3a. Série / nom
  guess = guess_name(directory, files)
  query = ask("Nom de la série à rechercher (modifiable)", guess)
  title = query
  try:
    results = tvmaze_search(query)
  except Exception as e:
    results = []
    print(c(f"  Recherche en ligne indisponible ({e}). Saisie manuelle.", YELL))

  if results:
    print(c("\nRésultats trouvés (vérifie l'année et le lien IMDB) :", BOLD))
    for i, r in enumerate(results[:8], 1):
      imdb = f"https://www.imdb.com/title/{r['imdb']}/" if r["imdb"] else "pas d'IMDB"
      print(f"    {c(str(i), BOLD)}) {r['name']}  {c('('+r['year']+')', GREEN)}"
          f"  [{r['language']}]  {c(imdb, DIM)}")
    print(f"    {c('m', BOLD)}) Saisir un titre manuellement")
    choice = ask("Quel est le bon ?", "1")
    if choice.lower() == "m":
      title = ask("Titre à utiliser", query)
    else:
      try:
        title = results[int(choice) - 1]["name"]
      except (ValueError, IndexError):
        title = query
  else:
    title = ask("Titre officiel à utiliser pour le nommage", query)
  title = sanitize(title)

  # 3b. Saison
  seasons = {p["season"] for p in pairs.values() if p["season"]}
  if len(seasons) == 1:
    default_season = seasons.pop()
  else:
    default_season = 1
  season_no = int(ask("Numéro de saison", str(default_season)) or default_season)

  # 3c. Piste audio principale
  primary = "jpn" if ask_choice(
    "Piste audio principale (lecture par défaut) ?",
    ["Japonais (VOSTFR)", "Français (VF)"]) == 1 else "fre"
  # Sous-titres FR par défaut si la VO japonaise est principale
  sub_default = (primary == "jpn")

  # 3d. Gestion des écarts de durée
  mismatch_policy = ["ask", "skip", "force"][ask_choice(
    f"Si les durées diffèrent de plus de {args.tolerance:.0f}s ?",
    ["Me demander à chaque fois", "Ignorer l'épisode", "Fusionner quand même"]) - 1]

  # 3e. Modèle de nommage
  print(c(f"\nModèle de nommage. Variables : {{title}} {{s}} {{e}}", DIM))
  template = ask("Modèle", "{title} S{s:02d}E{e:02d}")

  # ---- 4. Plan + confirmation ------------------------------------------- #
  out_dir = os.path.join(directory, args.output_dir)
  print(c("\n══════════════════════════ Plan ══════════════════════════", BOLD))
  plan = []
  for ep in sorted(pairs):
    p = pairs[ep]
    name = template.format(title=title, s=season_no, e=ep) + ".mkv"
    name = sanitize(name)
    plan.append((ep, p, os.path.join(out_dir, name), name))
    print(f"  E{ep:02d}  ->  {c(name, GREEN)}")
    print(f"         VOSTFR: {p['vostfr']['name']}")
    print(f"         VF    : {p['french']['name']}")
  for ep, reason, _ in problems:
    print(c(f"  E{ep:02d}  -> IGNORÉ ({reason})", YELL))
  for ep, g in orphans:
    if args.process_orphans:
      oname = sanitize(template.format(title=title, s=season_no, e=ep)) + ".mkv"
      print(c(f"  E{ep:02d}  -> ORPHELIN, réencapsulé : {oname}", CYAN))
    else:
      print(c(f"  E{ep:02d}  -> ORPHELIN, non traité ({g['name']})", YELL))
  print(f"\n  Audio principal : {c('Japonais' if primary=='jpn' else 'Français', BOLD)}"
      f"   |   Sortie : {out_dir}")

  if args.dry_run:
    print(c("\n[dry-run] Aucune fusion effectuée.", DIM))
    return

  if ask_choice("\nLancer la fusion ?", ["Oui", "Non"]) == 2:
    sys.exit("Annulé.")

  os.makedirs(out_dir, exist_ok=True)

  # ---- 5. Traitement ---------------------------------------------------- #
  ok, skipped, failed = 0, 0, 0
  for ep, p, out_path, name in plan:
    print(c(f"\n── Épisode {ep:02d} : {name}", BOLD))

    # Vérification de synchro par la durée (et fps)
    try:
      d_v, fps_v = probe_media(p["vostfr"]["path"])
      d_f, fps_f = probe_media(p["french"]["path"])
    except RuntimeError as e:
      print(c(f"  ffprobe a échoué : {e} -> ignoré", RED))
      failed += 1
      continue

    diff = abs(d_v - d_f)
    print(f"    durée VOSTFR={d_v:7.1f}s  VF={d_f:7.1f}s  écart={diff:.1f}s")
    if fps_v and fps_f and abs(fps_v - fps_f) > 0.05:
      print(c(f"    ⚠ fps différents ({fps_v:.3f} vs {fps_f:.3f}) : "
          "risque de désync même si durées proches.", YELL))

    if diff > args.tolerance:
      if mismatch_policy == "skip":
        print(c(f"    écart > {args.tolerance:.0f}s -> épisode ignoré", YELL))
        skipped += 1
        continue
      elif mismatch_policy == "ask":
        if ask_choice(f"    Écart de {diff:.1f}s. Que faire ?",
                ["Fusionner quand même", "Ignorer cet épisode"]) == 2:
          skipped += 1
          continue
      # 'force' -> on continue

    efm = args.embedded_fr_mode
    has_emb = has_embedded_french(p["vostfr"]["tracks"])
    need_ext_vf = not has_emb or efm in ("external", "both")
    sync_info = None
    if args.sync and need_ext_vf:
      sync_info = _analyze_sync(p["vostfr"]["path"], p["french"]["path"])

    cmd = build_mux_cmd(out_path, sanitize(name[:-4]),
              p["vostfr"], p["french"], primary, sub_default,
              sync_info=sync_info, embedded_fr_mode=efm)
    res = subprocess.run(cmd, capture_output=True, text=True)
    # mkvmerge: 0 = ok, 1 = warnings (résultat utilisable), 2 = erreur
    if res.returncode == 0:
      print(c("    ✓ fusionné", GREEN))
      ok += 1
    elif res.returncode == 1:
      print(c("    ✓ fusionné (avec avertissements)", YELL))
      ok += 1
    else:
      print(c(f"    ✗ échec mkvmerge :\n{res.stderr.strip()}", RED))
      failed += 1

  # ---- 5b. Épisodes orphelins (si demandé) ------------------------------ #
  if args.process_orphans and orphans:
    for ep, g in sorted(orphans):
      oname = sanitize(template.format(title=title, s=season_no, e=ep)) + ".mkv"
      print(c(f"\n── Orphelin {ep:02d} : {oname}", BOLD))
      try:
        src = {"path": g["path"], "tracks": probe_tracks(g["path"])}
      except RuntimeError as e:
        print(c(f"    ✗ lecture impossible : {e}", RED))
        failed += 1
        continue
      cls = classify(g["name"], [t["lang"] for t in src["tracks"] if t["type"] == "audio"])
      cmd = build_single_cmd(os.path.join(out_dir, oname), sanitize(oname[:-4]),
                   src, cls, primary, sub_default)
      res = subprocess.run(cmd, capture_output=True, text=True)
      if res.returncode in (0, 1):
        print(c("    ✓ réencapsulé", GREEN))
        ok += 1
      else:
        print(c(f"    ✗ échec mkvmerge :\n{res.stderr.strip()}", RED))
        failed += 1

  # ---- 6. Bilan --------------------------------------------------------- #
  print(c("\n══════════════════════════ Bilan ══════════════════════════", BOLD))
  print(f"  {c(str(ok), GREEN)} réussi(s) · {c(str(skipped), YELL)} ignoré(s) · "
      f"{c(str(failed), RED)} échec(s)")
  print(f"  Fichiers dans : {out_dir}")

  if orphans and not args.process_orphans:
    print(c(f"\n  Épisodes orphelins non traités (une seule langue) — {len(orphans)} :", YELL))
    for ep, g in sorted(orphans):
      cls = {"french": "VF", "vostfr": "VOSTFR"}.get(classify(g["name"]), "langue ?")
      print(f"    E{ep:02d}  [{cls}]  {g['name']}")

  if problems:
    print(c(f"\n  Autres épisodes non traités — {len(problems)} :", YELL))
    for ep, reason, group in sorted(problems):
      print(f"    E{ep:02d}  ({reason})")
      for g in group:
        print(f"          {g['name']}")


# --------------------------------------------------------------------------- #
#  Auto-test du parseur (langmux --self-test)
# --------------------------------------------------------------------------- #

def self_test():
  cases = [
    ("Sword Art Online S01E01 VOSTFR 1080p.mkv", (1, 1)),
    ("[Group] Sword.Art.Online.-.01.[1080p][x264].mkv", (None, 1)),
    ("SAO 1x12 FRENCH.mp4", (1, 12)),
    ("Naruto Episode 24 VF.mkv", (None, 24)),
    ("One.Piece.E1075.VOSTFR.WEB-DL.1080p.mkv", (None, 1075)),
    ("Demon Slayer - Ep.05 - TrueFrench.mkv", (None, 5)),
    ("Mon.Anime.2023.S02E07.MULTI.1080p.x265.mkv", (2, 7)),
    ("serie 720p episode 003.mkv", (None, 3)),
  ]
  ok = 0
  for name, expected in cases:
    got = parse_episode(name)
    status = "OK " if got == expected else "FAIL"
    if got == expected:
      ok += 1
    print(f"[{status}] {name:55s} -> {got}  (attendu {expected})")
  print(f"\n{ok}/{len(cases)} tests réussis")

  print("\nClassification VF/VOSTFR :")
  cl = [
    ("Sword Art Online S01E01 VOSTFR.mkv", "vostfr"),
    ("Sword Art Online S01E01 FRENCH.mkv", "french"),
    ("SAO 01 TrueFrench.mkv", "french"),
    ("SAO 01 VF.mkv", "french"),
    ("SAO 01 VOST.mkv", "vostfr"),
  ]
  for name, exp in cl:
    got = classify(name)
    print(f"[{'OK ' if got == exp else 'FAIL'}] {name:45s} -> {got}")


if __name__ == "__main__":
  main()
