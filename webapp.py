#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webapp.py — Interface web pour langmux.

Réutilise tel quel le moteur de langmux.py (analyse des noms, classification
VF/VOSTFR, sondage mkvmerge/ffprobe, construction de la commande de fusion).
La commande `langmux` reste disponible dans le shell du container.

Lancement (dans le container) :
  waitress-serve --listen=0.0.0.0:8080 webapp:app
Variables d'environnement :
  LANGMUX_ROOT   racine autorisée pour la navigation (défaut: /media)
"""

import os
import json
import threading
import subprocess
import urllib.request
import uuid

from flask import Flask, request, jsonify, Response

import langmux as L

app = Flask(__name__)

ROOT = os.path.realpath(os.environ.get("LANGMUX_ROOT", "/media"))

# ---- Jobs de fusion en arrière-plan -------------------------------------- #
jobs = {}
jobs_lock = threading.Lock()


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def safe_dir(directory):
  """Renvoie le chemin réel si situé sous ROOT et que c'est un dossier."""
  if not directory:
    return None
  rp = os.path.realpath(directory)
  if rp == ROOT or rp.startswith(ROOT + os.sep):
    return rp if os.path.isdir(rp) else None
  return None


def safe_file(directory, name):
  """Renvoie le chemin d'un fichier (basename) présent dans directory."""
  rp = os.path.join(directory, os.path.basename(name))
  return rp if os.path.isfile(rp) else None


def format_name(template, title, season, ep):
  """Formate le nom de sortie. Placeholders: {title} {s} {e} (s/e zéro-padés)."""
  return (template
      .replace("{title}", title)
      .replace("{s}", f"{season:02d}")
      .replace("{e}", f"{ep:02d}"))


def scan_dir(directory):
  """Analyse un dossier et renvoie une structure JSON-sérialisable."""
  files = L.collect_files(directory)
  episodes = {}
  unparsed = []

  for f in files:
    season, ep = L.parse_episode(f)
    if ep is None:
      unparsed.append(f)
      continue
    path = os.path.join(directory, f)
    info = {"name": f, "season": season, "error": None,
        "alangs": [], "has_subs": False, "duration": None, "fps": None}
    try:
      tracks = L.probe_tracks(path)
      info["alangs"] = [t["lang"] for t in tracks if t["type"] == "audio"]
      info["has_subs"] = any(t["type"] == "subtitles" for t in tracks)
    except Exception as e:
      info["error"] = str(e)
    try:
      d, fps = L.probe_media(path)
      info["duration"] = round(d, 1)
      info["fps"] = round(fps, 3) if fps else None
    except Exception:
      pass
    info["cls"] = L.classify(f, info["alangs"])
    episodes.setdefault(ep, []).append(info)

  def is_jpn(x):
    return any(l in L.JPN for l in x["alangs"])

  items, orphans, toomany = [], [], []
  for ep, group in sorted(episodes.items()):
    if len(group) == 1:
      orphans.append({"ep": ep, "file": group[0]})
    elif len(group) == 2:
      a, b = group
      classes = {a["cls"], b["cls"]}
      if classes == {"vostfr", "french"}:
        vost = a if a["cls"] == "vostfr" else b
        fr = b if vost is a else a
        status = "pair"
      else:
        if is_jpn(a) and not is_jpn(b):
          vost, fr = a, b
        elif is_jpn(b) and not is_jpn(a):
          vost, fr = b, a
        else:
          vost, fr = a, b
        status = "ambiguous"
      items.append({
        "ep": ep,
        "season": vost["season"] or fr["season"],
        "status": status,
        "files": [a, b],
        "vostfr": vost["name"],
        "french": fr["name"],
      })
    else:
      toomany.append({"ep": ep, "files": group})

  # Saison par défaut : la plus fréquente parmi les paires
  seasons = [it["season"] for it in items if it["season"]]
  default_season = max(set(seasons), key=seasons.count) if seasons else 1

  return {
    "directory": directory,
    "guess": L.guess_name(directory, files),
    "default_season": default_season,
    "files_count": len(files),
    "items": items,
    "orphans": orphans,
    "toomany": toomany,
    "unparsed": unparsed,
  }


# --------------------------------------------------------------------------- #
#  Job de fusion
# --------------------------------------------------------------------------- #

def _log(job, line):
  with jobs_lock:
    job["log"].append(line)


def run_job(job_id, directory, items, opts):
  job = jobs[job_id]
  out_dir = os.path.join(directory, opts["output_dir"])
  try:
    os.makedirs(out_dir, exist_ok=True)
  except OSError as e:
    with jobs_lock:
      job["status"] = "error"
      job["log"].append(f"Impossible de créer {out_dir} : {e}")
    return

  with jobs_lock:
    job["output_dir"] = out_dir
    job["total"] = len(items)

  for it in items:
    with jobs_lock:
      cancelled = job["cancel"]
    if cancelled:
      break

    ep = it["ep"]
    name = L.sanitize(format_name(opts["template"], opts["title"],
                    opts["season"], ep)) + ".mkv"
    with jobs_lock:
      job["current"] = {"ep": ep, "name": name}
    _log(job, f"E{ep:02d}  →  {name}")

    vpath = safe_file(directory, it["vostfr"])
    fpath = safe_file(directory, it["french"])
    if not vpath or not fpath:
      _record(job, ep, name, False, "fichier source introuvable")
      continue
    try:
      vost = {"path": vpath, "tracks": L.probe_tracks(vpath)}
      fr = {"path": fpath, "tracks": L.probe_tracks(fpath)}
    except Exception as e:
      _record(job, ep, name, False, f"lecture impossible : {e}")
      continue

    if L.has_embedded_french(vost["tracks"]):
      _log(job, "   français : piste déjà présente dans le fichier VO (synchronisée)")
    else:
      _log(job, "   français : depuis le fichier VF séparé")

    out_path = os.path.join(out_dir, name)
    cmd = L.build_mux_cmd(out_path, L.sanitize(name[:-4]), vost, fr,
                opts["primary"], opts["primary"] != "fre")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
    with jobs_lock:
      job["proc"] = proc
    _, stderr = proc.communicate()
    rc = proc.returncode
    with jobs_lock:
      job["proc"] = None
      cancelled = job["cancel"]

    if cancelled:
      # mkvmerge interrompu : on supprime le fichier partiel
      try:
        if os.path.exists(out_path):
          os.remove(out_path)
      except OSError:
        pass
      _log(job, f"   ⃠ E{ep:02d} interrompu")
      break

    if rc == 0:
      _record(job, ep, name, True, "fusionné")
    elif rc == 1:
      _record(job, ep, name, True, "fusionné (avertissements)")
    else:
      _record(job, ep, name, False, (stderr.strip() or "échec mkvmerge")[:400])

  with jobs_lock:
    job["status"] = "cancelled" if job["cancel"] else "done"
    job["current"] = None


def _record(job, ep, name, ok, msg):
  with jobs_lock:
    job["results"].append({"ep": ep, "name": name, "ok": ok, "msg": msg})
    job["done"] += 1
  _log(job, f"   {'✓' if ok else '✗'} {msg}")


# --------------------------------------------------------------------------- #
#  Routes API
# --------------------------------------------------------------------------- #

@app.get("/api/browse")
def api_browse():
  path = request.args.get("path") or ROOT
  rp = safe_dir(path) or ROOT
  try:
    entries = [e for e in sorted(os.listdir(rp)) if not e.startswith(".")]
  except OSError:
    entries = []
  dirs = [{"name": e, "path": os.path.join(rp, e)}
      for e in entries if os.path.isdir(os.path.join(rp, e))]
  video_count = sum(1 for e in entries
            if os.path.splitext(e)[1].lower() in L.VIDEO_EXTS)
  parent = os.path.dirname(rp)
  if not (rp == ROOT) and safe_dir(parent):
    parent_path = parent
  else:
    parent_path = None
  return jsonify({"root": ROOT, "path": rp, "parent": parent_path,
          "dirs": dirs, "video_count": video_count})


@app.post("/api/scan")
def api_scan():
  data = request.get_json(force=True) or {}
  directory = safe_dir(data.get("directory", ""))
  if not directory:
    return jsonify({"error": "Dossier invalide ou hors de la racine autorisée."}), 400
  try:
    return jsonify(scan_dir(directory))
  except Exception as e:
    return jsonify({"error": f"Échec de l'analyse : {e}"}), 500


@app.get("/api/search")
def api_search():
  q = (request.args.get("q") or "").strip()
  if not q:
    return jsonify({"results": []})
  try:
    results = L.tvmaze_search(q)[:8]
  except Exception as e:
    return jsonify({"error": f"Recherche indisponible : {e}", "results": []}), 200
  for r in results:
    r["imdb_url"] = f"https://www.imdb.com/title/{r['imdb']}/" if r.get("imdb") else None
  return jsonify({"results": results})


@app.post("/api/merge")
def api_merge():
  data = request.get_json(force=True) or {}
  directory = safe_dir(data.get("directory", ""))
  if not directory:
    return jsonify({"error": "Dossier invalide."}), 400
  items = data.get("items", [])
  if not items:
    return jsonify({"error": "Aucun épisode sélectionné."}), 400

  opts = {
    "output_dir": (data.get("output_dir") or "merged").strip() or "merged",
    "title": L.sanitize(data.get("title") or "Serie"),
    "season": int(data.get("season") or 1),
    "template": data.get("template") or "{title} S{s}E{e}",
    "primary": "fre" if data.get("primary") == "fre" else "jpn",
  }

  job_id = uuid.uuid4().hex[:12]
  jobs[job_id] = {"id": job_id, "status": "running", "total": len(items),
          "done": 0, "current": None, "results": [], "log": [],
          "output_dir": None, "cancel": False, "proc": None}
  threading.Thread(target=run_job, args=(job_id, directory, items, opts),
           daemon=True).start()
  return jsonify({"job_id": job_id})


@app.post("/api/cancel/<job_id>")
def api_cancel(job_id):
  job = jobs.get(job_id)
  if not job:
    return jsonify({"error": "job inconnu"}), 404
  with jobs_lock:
    job["cancel"] = True
    proc = job.get("proc")
  if proc and proc.poll() is None:
    proc.terminate()
    try:
      proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
      proc.kill()
  return jsonify({"ok": True})


@app.get("/api/seasons")
def api_seasons():
  """Nombre d'épisodes par saison d'après TVMaze (pour vérifier la complétude)."""
  show_id = request.args.get("show_id")
  if not show_id:
    return jsonify({"counts": {}})
  try:
    url = f"https://api.tvmaze.com/shows/{int(show_id)}/episodes"
    req = urllib.request.Request(url, headers={"User-Agent": "langmux/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
      eps = json.load(resp)
  except Exception as e:
    return jsonify({"counts": {}, "error": str(e)})
  counts = {}
  for e in eps:
    s = e.get("season")
    if s is not None:
      counts[str(s)] = counts.get(str(s), 0) + 1
  return jsonify({"counts": counts})


@app.get("/api/status/<job_id>")
def api_status(job_id):
  job = jobs.get(job_id)
  if not job:
    return jsonify({"error": "job inconnu"}), 404
  with jobs_lock:
    return jsonify({k: job[k] for k in
            ("id", "status", "total", "done", "current",
             "results", "log", "output_dir")})


@app.get("/")
def index():
  return Response(PAGE, mimetype="text/html")


# --------------------------------------------------------------------------- #
#  Page (HTML + CSS + JS, sans dépendance externe / offline-safe)
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>langmux — fusion des pistes</title>
<style>
  :root{
  --bg:#14131c; --surface:#1d1b29; --surface-2:#252335; --line:#322f45;
  --ink:#ecebf5; --muted:#9a96b5; --faint:#6f6b8c;
  --jp:#4fd6c2; --fr:#f2b65a; --accent:#ff6b8b;
  --ok:#5fd07a; --warn:#f2b65a; --bad:#ff6b6b;
  --radius:14px;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
     line-height:1.5;-webkit-font-smoothing:antialiased}
  a{color:var(--jp)}
  .wrap{max-width:980px;margin:0 auto;padding:28px 20px 80px}

  /* Header / signature ---------------------------------------------------- */
  header{display:flex;align-items:center;gap:16px;margin-bottom:26px}
  .glyph{flex:0 0 auto}
  .title h1{margin:0;font-size:26px;font-weight:700;letter-spacing:-.01em}
  .title p{margin:2px 0 0;color:var(--muted);font-size:13.5px}
  .title b.jp{color:var(--jp)} .title b.fr{color:var(--fr)}

  .card{background:var(--surface);border:1px solid var(--line);
    border-radius:var(--radius);padding:20px;margin:0 0 18px}
  .card.dim{opacity:.5;pointer-events:none;filter:saturate(.6)}
  .step{display:flex;align-items:baseline;gap:10px;margin:0 0 14px}
  .step .n{font-family:var(--mono);font-size:12px;color:var(--accent);
       border:1px solid var(--line);border-radius:6px;padding:1px 7px}
  .step h2{margin:0;font-size:16px;font-weight:650}
  .step .sub{color:var(--faint);font-size:12.5px;margin-left:auto}

  label{display:block;font-size:12.5px;color:var(--muted);margin:0 0 5px}
  input[type=text],input[type=number],select{
  width:100%;background:var(--surface-2);border:1px solid var(--line);
  color:var(--ink);border-radius:9px;padding:9px 11px;font-size:14px;
  font-family:var(--sans)}
  input:focus,select:focus{outline:2px solid var(--jp);outline-offset:-1px}
  .row{display:flex;gap:12px;flex-wrap:wrap}
  .row > div{flex:1;min-width:150px}
  .path-mono{font-family:var(--mono)}

  button{font-family:var(--sans);font-size:14px;font-weight:600;cursor:pointer;
  white-space:nowrap;
  border-radius:9px;padding:9px 16px;border:1px solid var(--line);
  background:var(--surface-2);color:var(--ink);transition:.12s}
  button:hover{border-color:var(--faint)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#1b0e14}
  button.primary:hover{filter:brightness(1.08)}
  button:disabled{opacity:.4;cursor:not-allowed}
  .ghost{background:transparent}

  .browser{font-family:var(--mono);font-size:13px}
  .crumbs{display:flex;align-items:center;gap:8px;color:var(--muted);
      margin-bottom:10px;flex-wrap:wrap}
  .dirlist{display:flex;flex-direction:column;gap:4px;max-height:230px;
       overflow:auto;border:1px solid var(--line);border-radius:10px;padding:6px}
  .diritem{text-align:left;background:transparent;border:none;padding:7px 9px;
       border-radius:7px;color:var(--ink);display:flex;gap:9px;align-items:center}
  .diritem:hover{background:var(--surface-2)}
  .diritem .ico{color:var(--faint)}

  .results{display:flex;flex-direction:column;gap:8px;margin-top:10px}
  .res{display:flex;gap:12px;align-items:center;padding:10px 12px;
     border:1px solid var(--line);border-radius:10px;background:var(--surface-2);
     cursor:pointer}
  .res.sel{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
  .res .yr{color:var(--jp);font-family:var(--mono);font-size:13px}
  .res .meta{color:var(--faint);font-size:12px;margin-left:auto;text-align:right}

  /* Plan ------------------------------------------------------------------ */
  .plan{display:flex;flex-direction:column;gap:8px}
  .ep{border:1px solid var(--line);border-radius:11px;background:var(--surface-2);
    padding:11px 13px}
  .ep.amb{border-color:var(--warn)}
  .ep.off{opacity:.45}
  .ep-top{display:flex;align-items:center;gap:11px}
  .epno{font-family:var(--mono);font-size:13px;color:var(--muted);min-width:38px}
  .outname{font-weight:600;font-size:14px}
  .ep-top .right{margin-left:auto;display:flex;gap:8px;align-items:center}
  .lanes{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:9px}
  .lane{font-family:var(--mono);font-size:12px;color:var(--muted);
    background:var(--surface);border:1px solid var(--line);border-radius:8px;
    padding:7px 9px;display:flex;gap:8px;align-items:center;overflow:hidden}
  .lane .fn{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto}
  .dot.jp{background:var(--jp)} .dot.fr{background:var(--fr)}
  .tag{font-family:var(--mono);font-size:10.5px;letter-spacing:.04em;
     padding:2px 6px;border-radius:5px;text-transform:uppercase}
  .tag.jp{color:var(--jp);border:1px solid var(--jp)}
  .tag.fr{color:var(--fr);border:1px solid var(--fr)}
  .tag.warn{color:var(--warn);border:1px solid var(--warn)}
  .swap{font-family:var(--mono);font-size:12px;margin-top:9px;color:var(--muted)}
  .swap select{display:inline-block;width:auto;padding:4px 8px;font-size:12px}
  .chk{width:17px;height:17px;accent-color:var(--accent)}

  .muted{color:var(--muted)} .small{font-size:12.5px}
  .pill{display:inline-flex;gap:7px;align-items:center;font-size:12.5px;
    background:var(--surface-2);border:1px solid var(--line);
    border-radius:999px;padding:5px 11px}
  .seg{display:flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}
  .seg button{border:none;border-radius:0;flex:1;background:var(--surface-2)}
  .seg button.on{color:#1b0e14}
  .seg button.on.jp{background:var(--jp)} .seg button.on.fr{background:var(--fr)}

  .note{font-size:12.5px;color:var(--faint);margin-top:8px}
  .bad{color:var(--bad)} .ok{color:var(--ok)} .warnc{color:var(--warn)}

  /* Run ------------------------------------------------------------------- */
  .bar{height:8px;background:var(--surface-2);border-radius:99px;overflow:hidden;
     border:1px solid var(--line)}
  .bar > i{display:block;height:100%;background:var(--accent);width:0;
       transition:width .3s}
  .log{font-family:var(--mono);font-size:12px;background:#100f17;
     border:1px solid var(--line);border-radius:10px;padding:11px;
     max-height:260px;overflow:auto;white-space:pre-wrap;margin-top:12px}
  .hidden{display:none}

  /* compteur d'épisodes saison */
  .epcount{margin-top:14px}
  .epcount .pill .num{font-family:var(--mono);color:var(--ink);font-weight:700}
  .epcount .ok2{color:var(--ok)} .epcount .miss{color:var(--warn)}

  /* Partie 4 — vue visuelle */
  .runhead{display:flex;align-items:center;gap:18px;margin-bottom:16px}
  .ring{flex:0 0 auto}
  .runstats{flex:1}
  .runstats .big{font-size:22px;font-weight:700}
  .runstats .sub2{color:var(--muted);font-size:13px;margin-top:2px}
  .runstats .counts{display:flex;gap:14px;margin-top:8px;font-size:12.5px}
  .runstats .counts b{font-family:var(--mono)}
  .c-ok{color:var(--ok)} .c-bad{color:var(--bad)} .c-pend{color:var(--faint)}
  #cancel{flex:0 0 auto}

  .tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
  .tile{border:1px solid var(--line);border-radius:12px;background:var(--surface-2);
    padding:12px;display:flex;flex-direction:column;gap:8px;position:relative;
    transition:border-color .2s,background .2s}
  .tile .tg{width:100%;height:30px}
  .tile .tep{font-family:var(--mono);font-size:12px;color:var(--muted)}
  .tile .tname{font-size:12.5px;font-weight:600;line-height:1.3;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .tile .tstate{font-size:11.5px;color:var(--faint);display:flex;align-items:center;gap:6px}
  .tile .tstate .ic{font-weight:700}
  /* lanes du glyphe par état */
  .tile .gJ{stroke:var(--jp)} .tile .gF{stroke:var(--fr)}
  .tile .gO{stroke:#3a3750;transition:stroke .3s} .tile .gd{fill:#3a3750;transition:fill .3s}

  .tile[data-state=pending]{opacity:.6}
  .tile[data-state=active]{border-color:var(--accent);
    box-shadow:0 0 0 1px var(--accent),0 0 22px -6px var(--accent);
    animation:pulse 1.3s ease-in-out infinite}
  .tile[data-state=active] .tstate{color:var(--accent)}
  .tile[data-state=done]{border-color:var(--ok)}
  .tile[data-state=done] .tstate{color:var(--ok)}
  .tile[data-state=done] .gO{stroke:var(--accent)} .tile[data-state=done] .gd{fill:var(--accent)}
  .tile[data-state=failed]{border-color:var(--bad)}
  .tile[data-state=failed] .tstate{color:var(--bad)}
  .tile[data-state=cancelled]{border-color:var(--faint);opacity:.55}
  @keyframes pulse{0%,100%{box-shadow:0 0 0 1px var(--accent),0 0 18px -8px var(--accent)}
           50%{box-shadow:0 0 0 1px var(--accent),0 0 26px -2px var(--accent)}}

  .logwrap{margin-top:14px;border:1px solid var(--line);border-radius:10px;
       background:#100f17;padding:4px 12px}
  .logwrap summary{cursor:pointer;color:var(--muted);font-size:12.5px;padding:8px 0}
  .logwrap .log{margin:0 0 10px;border:none;padding:0;background:transparent}
  @media (prefers-reduced-motion:reduce){.tile[data-state=active]{animation:none}}
  @media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<div class="wrap">

  <header>
  <span class="glyph">
    <svg width="46" height="46" viewBox="0 0 46 46" fill="none" aria-hidden="true">
    <path d="M4 13 H22 Q31 13 31 23 Q31 33 22 33 H4" stroke="#4fd6c2" stroke-width="3" fill="none"/>
    <path d="M4 33 H22 Q31 33 31 23 Q31 13 22 13 H4" stroke="#f2b65a" stroke-width="3" fill="none"/>
    <path d="M31 23 H42" stroke="#ff6b8b" stroke-width="3"/>
    <circle cx="42" cy="23" r="3" fill="#ff6b8b"/>
    </svg>
  </span>
  <div class="title">
    <h1>langmux</h1>
    <p>Réunit une piste <b class="jp">VO japonaise</b> et un <b class="fr">doublage français</b> du même épisode en un seul .mkv.</p>
  </div>
  </header>

  <!-- 1. Dossier -->
  <section class="card" id="card-folder">
  <div class="step"><span class="n">01</span><h2>Choisir le dossier</h2>
    <span class="sub">racine : <span id="root" class="path-mono"></span></span></div>
  <div class="browser">
    <div class="crumbs">
    <button class="ghost" id="up">↑ dossier parent</button>
    <span id="cwd"></span>
    </div>
    <div class="dirlist" id="dirs"></div>
    <div class="row" style="margin-top:12px;align-items:center">
    <div style="flex:2"><span class="small muted" id="folderinfo"></span></div>
    <button class="primary" id="scan" style="flex:0">Analyser ce dossier</button>
    </div>
  </div>
  </section>

  <!-- 2. Série + options -->
  <section class="card dim" id="card-config">
  <div class="step"><span class="n">02</span><h2>Série, audio et nommage</h2>
    <span class="sub" id="scaninfo"></span></div>

  <label>Nom de la série à rechercher</label>
  <div class="row" style="align-items:center">
    <div style="flex:3"><input type="text" id="query"></div>
    <button id="search" style="flex:0">Rechercher</button>
  </div>
  <div class="results" id="results"></div>
  <p class="note">Vérifie l'année et ouvre la fiche IMDB pour confirmer. Tu peux aussi garder un titre saisi à la main.</p>

  <div class="row" style="margin-top:14px">
    <div>
    <label>Titre retenu (sert au nommage)</label>
    <input type="text" id="title">
    </div>
    <div style="max-width:120px">
    <label>Saison</label>
    <input type="number" id="season" min="0" value="1">
    </div>
  </div>
  <div class="epcount" id="epcount"></div>

  <div class="row" style="margin-top:14px">
    <div>
    <label>Piste audio principale (lue par défaut)</label>
    <div class="seg" id="primary">
      <button data-v="jpn" class="on jp">Japonais (VO)</button>
      <button data-v="fre">Français (VF)</button>
    </div>
    <p class="note" id="subnote"></p>
    </div>
    <div>
    <label>Modèle de nom &nbsp;<span class="muted small">{title} {s} {e}</span></label>
    <input type="text" id="template" value="{title} S{s}E{e}">
    <div class="row" style="margin-top:10px">
      <div><label>Sous-dossier de sortie</label><input type="text" id="outdir" value="merged"></div>
      <div style="max-width:130px"><label>Écart toléré (s)</label><input type="number" id="tol" value="10" min="0"></div>
    </div>
    </div>
  </div>
  </section>

  <!-- 3. Plan -->
  <section class="card dim" id="card-plan">
  <div class="step"><span class="n">03</span><h2>Vérifier le plan</h2>
    <span class="sub" id="planinfo"></span></div>
  <div class="plan" id="plan"></div>
  <div id="extras"></div>
  <div class="row" style="margin-top:16px;align-items:center">
    <div style="flex:2"><span class="small muted" id="selinfo"></span></div>
    <button class="primary" id="run" style="flex:0">Lancer la fusion</button>
  </div>
  </section>

  <!-- 4. Exécution -->
  <section class="card dim" id="card-run">
  <div class="step"><span class="n">04</span><h2>Fusion</h2>
    <span class="sub" id="runinfo"></span></div>

  <div class="runhead">
    <span class="ring">
    <svg width="76" height="76" viewBox="0 0 76 76">
      <circle cx="38" cy="38" r="32" fill="none" stroke="#322f45" stroke-width="7"/>
      <circle id="ringfg" cx="38" cy="38" r="32" fill="none" stroke="#ff6b8b"
          stroke-width="7" stroke-linecap="round"
          stroke-dasharray="201" stroke-dashoffset="201"
          transform="rotate(-90 38 38)" style="transition:stroke-dashoffset .35s"/>
      <text id="ringtxt" x="38" y="43" text-anchor="middle"
        font-family="ui-monospace,monospace" font-size="17" fill="#ecebf5">0%</text>
    </svg>
    </span>
    <div class="runstats">
    <div class="big" id="runbig">Préparation…</div>
    <div class="sub2" id="runsub"></div>
    <div class="counts">
      <span class="c-ok">✓ <b id="cdone">0</b> réussis</span>
      <span class="c-bad">✗ <b id="cfail">0</b> échecs</span>
      <span class="c-pend">• <b id="cpend">0</b> en attente</span>
    </div>
    </div>
    <button id="cancel" class="ghost">Annuler</button>
  </div>

  <div class="tiles" id="tiles"></div>

  <details class="logwrap">
    <summary>Journal détaillé</summary>
    <div class="log" id="log"></div>
  </details>
  </section>

</div>

<script>
const $=s=>document.querySelector(s);
const ce=(t,c)=>{const e=document.createElement(t);if(c)e.className=c;return e};
let state={dir:null,scan:null,primary:'jpn',rows:[]};

function activate(id){$('#'+id).classList.remove('dim')}
function deactivate(id){$('#'+id).classList.add('dim')}

/* ---- 1. Navigation dossiers ---- */
async function browse(path){
  const r=await fetch('/api/browse'+(path?('?path='+encodeURIComponent(path)):''));
  const d=await r.json();
  state.dir=d.path;
  $('#root').textContent=d.root;
  $('#cwd').textContent=d.path;
  $('#up').disabled=!d.parent;
  $('#up').onclick=()=>browse(d.parent);
  const list=$('#dirs');list.innerHTML='';
  if(!d.dirs.length){const e=ce('div','small muted');e.style.padding='8px';e.textContent='Aucun sous-dossier.';list.appendChild(e);}
  d.dirs.forEach(x=>{
  const b=ce('button','diritem');
  b.innerHTML='<span class="ico">▸</span>'+x.name;
  b.onclick=()=>browse(x.path);
  list.appendChild(b);
  });
  $('#folderinfo').textContent=d.video_count?(d.video_count+' fichier(s) vidéo ici — prêt à analyser'):'Aucune vidéo dans ce dossier (descends dans un sous-dossier).';
  $('#scan').disabled=!d.video_count;
}
$('#scan').onclick=scan;

/* ---- 2. Analyse ---- */
async function scan(){
  $('#scan').disabled=true;$('#scan').textContent='Analyse…';
  const r=await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({directory:state.dir})});
  const d=await r.json();
  $('#scan').textContent='Analyser ce dossier';$('#scan').disabled=false;
  if(d.error){alert(d.error);return;}
  state.scan=d;
  $('#scaninfo').textContent=d.files_count+' fichier(s) · '+d.items.length+' paire(s)';
  $('#query').value=d.guess;
  $('#title').value=d.guess;
  $('#season').value=d.default_season;
  activate('card-config');
  buildPlan();
  activate('card-plan');
  doSearch();
  $('#card-config').scrollIntoView({behavior:'smooth',block:'start'});
}

/* ---- 2b. Recherche TVMaze ---- */
$('#search').onclick=doSearch;
$('#query').addEventListener('keydown',e=>{if(e.key==='Enter')doSearch()});
async function doSearch(){
  const q=$('#query').value.trim();if(!q)return;
  const box=$('#results');box.innerHTML='<div class="small muted">Recherche…</div>';
  const r=await fetch('/api/search?q='+encodeURIComponent(q));
  const d=await r.json();box.innerHTML='';
  if(d.error){box.innerHTML='<div class="small warnc">'+d.error+'</div>';}
  if(!d.results.length){const e=ce('div','small muted');e.textContent='Rien trouvé — garde le titre saisi à la main.';box.appendChild(e);return;}
  d.results.forEach(x=>{
  const row=ce('div','res');
  const imdb=x.imdb_url?('<a href="'+x.imdb_url+'" target="_blank" rel="noopener">IMDB ↗</a>'):'pas d\'IMDB';
  row.innerHTML='<div><div>'+esc(x.name)+'</div></div>'+
    '<span class="yr">('+(x.year||'????')+')</span>'+
    '<span class="meta">'+esc(x.language||'')+'<br>'+imdb+'</span>';
  row.onclick=()=>{document.querySelectorAll('.res').forEach(e=>e.classList.remove('sel'));
    row.classList.add('sel');$('#title').value=x.name;state.showId=x.id;refreshNames();updateEpCount();};
  box.appendChild(row);
  });
}

/* ---- audio principal ---- */
$('#primary').querySelectorAll('button').forEach(b=>{
  b.onclick=()=>{
  state.primary=b.dataset.v;
  $('#primary').querySelectorAll('button').forEach(x=>{x.className=''});
  b.className='on '+(b.dataset.v==='jpn'?'jp':'fr');
  $('#subnote').textContent = state.primary==='jpn'
    ? 'Sous-titres FR forcés (affichés par défaut) car l\'audio n\'est pas le français.'
    : 'Sous-titres FR présents mais non forcés (audio français principal).';
  };
});
$('#primary').querySelector('button').click();

['title','season','template'].forEach(id=>$('#'+id).addEventListener('input',refreshNames));
$('#season').addEventListener('input',renderEpCount);

/* ---- Nombre d'épisodes de la saison (TVMaze) ---- */
async function updateEpCount(){
  const el=$('#epcount');
  if(!state.showId){el.innerHTML='';return;}
  el.innerHTML='<span class="pill">Vérification du nombre d\'épisodes…</span>';
  let counts={};
  try{const r=await fetch('/api/seasons?show_id='+state.showId);counts=(await r.json()).counts||{};}catch(e){}
  state.seasonCounts=counts;renderEpCount();
}
function renderEpCount(){
  const el=$('#epcount');const counts=state.seasonCounts||{};
  if(!state.showId){el.innerHTML='';return;}
  const s=parseInt($('#season').value||'1');
  const off=counts[String(s)];
  const have=state.scan?state.scan.items.length:0;
  if(off==null){el.innerHTML='<span class="pill">Saison '+s+' : nombre d\'épisodes inconnu sur TVMaze</span>';return;}
  let cmp='';
  if(have===off)cmp='<span class="ok2"> — tu as les '+have+' épisodes ✓</span>';
  else if(have<off)cmp='<span class="miss"> — '+have+' ici, '+(off-have)+' manquant'+((off-have)>1?'s':'')+'</span>';
  else cmp=' — '+have+' paires ici';
  el.innerHTML='<span class="pill">Saison <span class="num">'+s+'</span> : <span class="num">'+off+
  '</span> épisodes officiels'+cmp+'</span>';
}

/* ---- 3. Construction du plan ---- */
function buildPlan(){
  const plan=$('#plan');plan.innerHTML='';state.rows=[];
  state.scan.items.forEach(it=>{
  const row={ep:it.ep,files:it.files,vostfr:it.vostfr,french:it.french,
         status:it.status,include:true};
  state.rows.push(row);
  const el=ce('div','ep'+(it.status==='ambiguous'?' amb':''));
  el.dataset.ep=it.ep;
  // top
  const top=ce('div','ep-top');
  const chk=ce('input');chk.type='checkbox';chk.className='chk';chk.checked=true;
  chk.onchange=()=>{row.include=chk.checked;el.classList.toggle('off',!chk.checked);updateSel();};
  const epno=ce('span','epno');epno.textContent='E'+String(it.ep).padStart(2,'0');
  const outn=ce('span','outname');outn.dataset.out='1';
  const right=ce('div','right');
  if(it.status==='ambiguous'){const t=ce('span','tag warn');t.textContent='à confirmer';right.appendChild(t);}
  const dw=ce('span');dw.dataset.dur='1';right.appendChild(dw);
  top.append(chk,epno,outn,right);
  // lanes
  const lanes=ce('div','lanes');
  lanes.innerHTML=
    '<div class="lane"><span class="dot jp"></span><span class="tag jp">JP</span><span class="fn" data-v>'+esc(it.vostfr)+'</span></div>'+
    '<div class="lane"><span class="dot fr"></span><span class="tag fr">VF</span><span class="fn" data-f>'+esc(it.french)+'</span></div>';
  // swap
  const swap=ce('div','swap');
  const sel=ce('select');
  it.files.forEach(f=>{const o=ce('option');o.value=f.name;o.textContent=f.name;sel.appendChild(o);});
  sel.value=it.french;
  sel.onchange=()=>{row.french=sel.value;row.vostfr=it.files.find(f=>f.name!==sel.value).name;
    lanes.querySelector('[data-v]').textContent=row.vostfr;
    lanes.querySelector('[data-f]').textContent=row.french;};
  swap.append(document.createTextNode('Piste VF : '),sel);

  el.append(top,lanes,swap);
  plan.appendChild(el);
  row._el=el;row._out=outn;row._dur=dw;
  });
  // extras
  const ex=$('#extras');ex.innerHTML='';
  if(state.scan.orphans.length){
  const d=ce('div','card');d.style.margin='14px 0 0';d.style.background='transparent';
  d.innerHTML='<div class="small warnc" style="margin-bottom:6px">⚠ Épisodes orphelins (une seule langue) — non fusionnés :</div>';
  state.scan.orphans.forEach(o=>{const p=ce('div','small muted path-mono');
    p.textContent='E'+String(o.ep).padStart(2,'0')+'  '+o.file.name;d.appendChild(p);});
  ex.appendChild(d);
  }
  if(state.scan.toomany.length){
  const d=ce('div','small muted');d.style.marginTop='10px';
  d.textContent='Épisodes avec plus de 2 fichiers (ignorés) : '+
    state.scan.toomany.map(t=>'E'+String(t.ep).padStart(2,'0')).join(', ');
  ex.appendChild(d);
  }
  if(state.scan.unparsed.length){
  const d=ce('div','small muted');d.style.marginTop='6px';
  d.textContent='Numéro non détecté : '+state.scan.unparsed.join(', ');
  ex.appendChild(d);
  }
  refreshNames();updateSel();
}

function fmt(t,title,s,ep){return t.replace('{title}',title).replace('{s}',String(s).padStart(2,'0')).replace('{e}',String(ep).padStart(2,'0'));}
function refreshNames(){
  const title=$('#title').value||'Serie',s=parseInt($('#season').value||'1'),tpl=$('#template').value||'{title} S{s}E{e}';
  const tol=parseFloat($('#tol').value||'10');
  state.rows.forEach(r=>{
  r._out.textContent=fmt(tpl,title,s,r.ep)+'.mkv';
  // warning durée / fps
  const a=r.files[0],b=r.files[1];r._dur.innerHTML='';
  if(a.duration!=null&&b.duration!=null){
    const diff=Math.abs(a.duration-b.duration);
    const fpsDiff=(a.fps&&b.fps&&Math.abs(a.fps-b.fps)>0.05);
    if(diff>tol){const t=ce('span','tag warn');t.textContent='Δ '+diff.toFixed(1)+'s';r._dur.appendChild(t);}
    else{const t=ce('span','small muted');t.textContent=diff.toFixed(1)+'s';r._dur.appendChild(t);}
    if(fpsDiff){const t=ce('span','tag warn');t.style.marginLeft='6px';t.textContent='fps≠';r._dur.appendChild(t);}
  }else{const t=ce('span','tag warn');t.textContent='durée ?';r._dur.appendChild(t);}
  });
}
$('#tol').addEventListener('input',refreshNames);
function updateSel(){
  const n=state.rows.filter(r=>r.include).length;
  $('#selinfo').textContent=n+' épisode(s) sélectionné(s) pour la fusion';
  $('#run').disabled=!n;
}

/* ---- 4. Lancement + suivi visuel ---- */
$('#run').onclick=run;
$('#cancel').onclick=async()=>{
  if(!state.jobId)return;
  $('#cancel').disabled=true;$('#cancel').textContent='Annulation…';
  $('#runbig').textContent='Annulation…';
  try{await fetch('/api/cancel/'+state.jobId,{method:'POST'});}catch(e){}
};

function glyphSVG(){return '<svg class="tg" viewBox="0 0 76 30" preserveAspectRatio="xMidYMid meet">'+
  '<path class="gJ" d="M4 9 H40 Q52 9 52 15 Q52 21 40 21 H4" fill="none" stroke-width="2.4"/>'+
  '<path class="gF" d="M4 21 H40 Q52 21 52 15 Q52 9 40 9 H4" fill="none" stroke-width="2.4"/>'+
  '<path class="gO" d="M52 15 H68" stroke-width="2.4" fill="none"/>'+
  '<circle class="gd" cx="68" cy="15" r="2.6"/></svg>';}

function buildTiles(rows,title,s,tpl){
  const box=$('#tiles');box.innerHTML='';state.tiles={};
  rows.forEach(r=>{
  const name=fmt(tpl,title,s,r.ep)+'.mkv';
  const t=ce('div','tile');t.dataset.state='pending';
  t.innerHTML=glyphSVG()+
    '<div class="tep">E'+String(r.ep).padStart(2,'0')+'</div>'+
    '<div class="tname">'+esc(name)+'</div>'+
    '<div class="tstate"><span class="ic">•</span><span class="msg">en attente</span></div>';
  box.appendChild(t);state.tiles[r.ep]=t;
  });
  $('#cpend').textContent=rows.length;$('#cdone').textContent=0;$('#cfail').textContent=0;
}
function setTile(ep,st,msg){
  const t=state.tiles[ep];if(!t)return;
  t.dataset.state=st;
  const ic={active:'⟳',done:'✓',failed:'✗',cancelled:'⃠',pending:'•'}[st]||'•';
  t.querySelector('.ic').textContent=ic;t.querySelector('.msg').textContent=msg;
}
function setRing(pct){
  const C=201;$('#ringfg').setAttribute('stroke-dashoffset',Math.round(C*(1-pct/100)));
  $('#ringtxt').textContent=pct+'%';
}

async function run(){
  const inc=state.rows.filter(r=>r.include);
  const items=inc.map(r=>({ep:r.ep,vostfr:r.vostfr,french:r.french}));
  const title=$('#title').value||'Serie',s=parseInt($('#season').value||'1'),tpl=$('#template').value||'{title} S{s}E{e}';
  buildTiles(inc,title,s,tpl);
  setRing(0);$('#runbig').textContent='Préparation…';$('#runsub').textContent='';
  $('#cancel').disabled=false;$('#cancel').textContent='Annuler';
  $('#run').disabled=true;$('#run').textContent='Fusion en cours…';
  activate('card-run');$('#card-run').scrollIntoView({behavior:'smooth',block:'start'});
  const body={directory:state.dir,output_dir:$('#outdir').value,title,season:s,template:tpl,primary:state.primary,items};
  const r=await fetch('/api/merge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(d.error){alert(d.error);$('#run').disabled=false;$('#run').textContent='Lancer la fusion';return;}
  state.jobId=d.job_id;poll(d.job_id);
}

async function poll(id){
  let d;try{d=await (await fetch('/api/status/'+id)).json();}catch(e){setTimeout(()=>poll(id),1200);return;}
  const total=d.total||1,done=d.done;
  setRing(Math.round(100*done/total));
  const ok=d.results.filter(x=>x.ok).length,bad=d.results.filter(x=>!x.ok).length;
  $('#cdone').textContent=ok;$('#cfail').textContent=bad;$('#cpend').textContent=Math.max(total-done,0);
  d.results.forEach(x=>setTile(x.ep,x.ok?'done':'failed',x.msg));
  if(d.status==='running'&&d.current){
  setTile(d.current.ep,'active','fusion en cours…');
  $('#runbig').textContent='Fusion en cours';
  $('#runsub').textContent='E'+String(d.current.ep).padStart(2,'0')+' — '+d.current.name;
  $('#runinfo').textContent=done+'/'+total;
  }
  $('#log').textContent=d.log.join('\n');$('#log').scrollTop=$('#log').scrollHeight;
  if(d.status==='running'){setTimeout(()=>poll(id),900);return;}
  finishRun(d,ok,bad);
}

function finishRun(d,ok,bad){
  Object.keys(state.tiles).forEach(ep=>{const st=state.tiles[ep].dataset.state;
  if(st==='pending'||st==='active')setTile(parseInt(ep),'cancelled','annulé');});
  $('#cancel').disabled=true;$('#cancel').textContent='Annuler';
  $('#run').disabled=false;$('#run').textContent='Relancer';
  $('#runinfo').textContent='';
  if(d.status==='cancelled'){
  $('#runbig').textContent='Annulé';
  $('#runsub').textContent=ok+' épisode(s) fusionné(s) avant l\'arrêt'+(d.output_dir?(' — '+d.output_dir):'');
  }else{
  $('#runbig').textContent='Terminé';
  $('#runsub').textContent=ok+' réussi(s) · '+bad+' échec(s)'+(d.output_dir?(' — '+d.output_dir):'');
  }
}

function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
browse(null);
</script>
</body>
</html>
"""


if __name__ == "__main__":
  app.run(host="0.0.0.0", port=8080)
