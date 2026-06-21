#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webapp.py — Interface web pour langmux (FR/EN).

Réutilise le moteur de langmux.py. La commande `langmux` reste disponible dans
le shell du container.

Lancement :  waitress-serve --listen=0.0.0.0:8080 webapp:app
Variables  :  LANGMUX_ROOT  (racine autorisée, défaut: /media)
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

jobs = {}
jobs_lock = threading.Lock()

# Messages générés côté serveur (affichés dans le journal et les tuiles)
SRV = {
    "fr": {
        "arrow": "E{ep:02d}  →  {name}",
        "src_embedded": "   français : piste déjà présente dans le fichier VO (synchronisée)",
        "src_external": "   français : depuis le fichier VF séparé",
        "notfound": "fichier source introuvable",
        "readfail": "lecture impossible : {e}",
        "merged": "fusionné",
        "merged_warn": "fusionné (avertissements)",
        "mkvfail": "échec mkvmerge",
        "interrupted": "   ⃠ E{ep:02d} interrompu",
        "orphan": "orphelin réencapsulé",
        "mkdirfail": "Impossible de créer {d} : {e}",
        "sync_start": "   analyse sync…",
        "sync_ok": "   sync : {offset}s  {label}",
        "sync_low": "   sync : confiance insuffisante, ignoré",
        "sync_skip_embedded": "   sync : ignoré (piste FR intégrée utilisée)",
        "sync_err": "   sync : erreur ({e}), ignoré",
        "sync_nodep": "   sync : numpy/scipy non installés, ignoré",
        "src_multi_ext": "   français : piste intégrée remplacée par le fichier VF",
        "src_multi_both": "   français : piste intégrée + fichier VF externe",
    },
    "en": {
        "arrow": "E{ep:02d}  →  {name}",
        "src_embedded": "   french: track already in the VO file (in sync)",
        "src_external": "   french: from the separate VF file",
        "notfound": "source file not found",
        "readfail": "cannot read file: {e}",
        "merged": "merged",
        "merged_warn": "merged (with warnings)",
        "mkvfail": "mkvmerge failed",
        "interrupted": "   ⃠ E{ep:02d} cancelled",
        "orphan": "orphan repackaged",
        "mkdirfail": "cannot create {d}: {e}",
        "sync_start": "   analyzing sync…",
        "sync_ok": "   sync: {offset}s  {label}",
        "sync_low": "   sync: low confidence, skipped",
        "sync_skip_embedded": "   sync: skipped (using embedded FR track)",
        "sync_err": "   sync: error ({e}), skipped",
        "sync_nodep": "   sync: numpy/scipy not installed, skipped",
        "src_multi_ext": "   french: embedded track replaced by external VF file",
        "src_multi_both": "   french: embedded track + external VF file",
    },
}


def M(job, key, **kw):
    return SRV.get(job.get("lang", "fr"), SRV["fr"])[key].format(**kw)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def safe_dir(directory):
    if not directory:
        return None
    rp = os.path.realpath(directory)
    if rp == ROOT or rp.startswith(ROOT + os.sep):
        return rp if os.path.isdir(rp) else None
    return None


def safe_file(directory, name):
    rp = os.path.join(directory, os.path.basename(name))
    return rp if os.path.isfile(rp) else None


def format_name(template, title, season, ep):
    return (template.replace("{title}", title)
                    .replace("{s}", f"{season:02d}")
                    .replace("{e}", f"{ep:02d}"))


def scan_dir(directory):
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
            has_emb_fr = any(l in L.FRA for l in vost["alangs"])
            items.append({"ep": ep, "season": vost["season"] or fr["season"],
                          "status": status, "files": [a, b],
                          "vostfr": vost["name"], "french": fr["name"],
                          "has_embedded_fr": has_emb_fr})
        else:
            toomany.append({"ep": ep, "files": group})

    seasons = [it["season"] for it in items if it["season"]]
    default_season = max(set(seasons), key=seasons.count) if seasons else 1
    return {"directory": directory, "guess": L.guess_name(directory, files),
            "default_season": default_season, "files_count": len(files),
            "items": items, "orphans": orphans, "toomany": toomany,
            "unparsed": unparsed}


# --------------------------------------------------------------------------- #
#  Job de fusion
# --------------------------------------------------------------------------- #

def _log(job, line):
    with jobs_lock:
        job["log"].append(line)


def _record(job, ep, name, ok, msg):
    with jobs_lock:
        job["results"].append({"ep": ep, "name": name, "ok": ok, "msg": msg})
        job["done"] += 1
    _log(job, f"   {'✓' if ok else '✗'} {msg}")


def _run_mux(job, out_path, cmd):
    """Lance mkvmerge ; renvoie (rc, stderr) ou (None, stderr) si annulé."""
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
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except OSError:
            pass
        return None, stderr
    return rc, stderr


def _analyze_sync_job(job, opts, vpath, fpath, embedded, embedded_fr_mode="auto"):
    """Lance l'analyse de synchronisation si demandée. Retourne sync_info ou None."""
    if not opts.get("sync"):
        return None
    # Pas besoin de sync si on utilise la piste FR intégrée (déjà calée sur la vidéo)
    if embedded and embedded_fr_mode not in ("external", "both"):
        _log(job, M(job, "sync_skip_embedded"))
        return None
    try:
        import audio_sync
    except ImportError:
        _log(job, M(job, "sync_nodep"))
        return None
    _log(job, M(job, "sync_start"))
    try:
        result = audio_sync.analyze(vpath, fpath)
    except Exception as e:
        _log(job, M(job, "sync_err", e=str(e)[:120]))
        return None
    if not result["reliable"]:
        _log(job, M(job, "sync_low"))
        return None
    _log(job, M(job, "sync_ok",
                offset=f"{result['offset']:+.3f}",
                label=result["drift_label"]))
    return result


def run_job(job_id, directory, items, orphans, opts):
    job = jobs[job_id]
    out_dir = os.path.join(directory, opts["output_dir"])
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        with jobs_lock:
            job["status"] = "error"
            job["log"].append(M(job, "mkdirfail", d=out_dir, e=e))
        return

    with jobs_lock:
        job["output_dir"] = out_dir
        job["total"] = len(items) + len(orphans)

    cancelled = False

    # ---- Paires VF + VOSTFR ----
    for it in items:
        with jobs_lock:
            cancelled = job["cancel"]
        if cancelled:
            break
        ep = it["ep"]
        name = L.sanitize(format_name(opts["template"], opts["title"], opts["season"], ep)) + ".mkv"
        with jobs_lock:
            job["current"] = {"ep": ep, "name": name}
        _log(job, M(job, "arrow", ep=ep, name=name))

        vpath = safe_file(directory, it["vostfr"])
        fpath = safe_file(directory, it["french"])
        if not vpath or not fpath:
            _record(job, ep, name, False, M(job, "notfound"))
            continue
        try:
            vost = {"path": vpath, "tracks": L.probe_tracks(vpath)}
            fr = {"path": fpath, "tracks": L.probe_tracks(fpath)}
        except Exception as e:
            _record(job, ep, name, False, M(job, "readfail", e=e))
            continue

        embedded = L.has_embedded_french(vost["tracks"])
        efm = it.get("embedded_fr_mode", "auto")
        if embedded:
            src_key = {"external": "src_multi_ext", "both": "src_multi_both"}.get(efm, "src_embedded")
        else:
            src_key = "src_external"
        _log(job, M(job, src_key))

        sync_info = _analyze_sync_job(job, opts, vpath, fpath, embedded, efm)

        out_path = os.path.join(out_dir, name)
        cmd = L.build_mux_cmd(out_path, L.sanitize(name[:-4]), vost, fr,
                              opts["primary"], opts["primary"] != "fre",
                              sync_info=sync_info, embedded_fr_mode=efm)
        rc, stderr = _run_mux(job, out_path, cmd)
        if rc is None:
            _log(job, M(job, "interrupted", ep=ep))
            cancelled = True
            break
        if rc == 0:
            _record(job, ep, name, True, M(job, "merged"))
        elif rc == 1:
            _record(job, ep, name, True, M(job, "merged_warn"))
        else:
            _record(job, ep, name, False, M(job, "mkvfail") + ": " + (stderr.strip()[:300]))

    # ---- Épisodes orphelins (si demandés) ----
    if not cancelled:
        for o in orphans:
            with jobs_lock:
                cancelled = job["cancel"]
            if cancelled:
                break
            ep = o["ep"]
            name = L.sanitize(format_name(opts["template"], opts["title"], opts["season"], ep)) + ".mkv"
            with jobs_lock:
                job["current"] = {"ep": ep, "name": name}
            _log(job, M(job, "arrow", ep=ep, name=name))

            spath = safe_file(directory, o["file"])
            if not spath:
                _record(job, ep, name, False, M(job, "notfound"))
                continue
            try:
                src = {"path": spath, "tracks": L.probe_tracks(spath)}
            except Exception as e:
                _record(job, ep, name, False, M(job, "readfail", e=e))
                continue
            cls = o.get("cls") or L.classify(o["file"],
                                             [t["lang"] for t in src["tracks"] if t["type"] == "audio"])
            out_path = os.path.join(out_dir, name)
            cmd = L.build_single_cmd(out_path, L.sanitize(name[:-4]), src, cls,
                                     opts["primary"], opts["primary"] != "fre")
            rc, stderr = _run_mux(job, out_path, cmd)
            if rc is None:
                _log(job, M(job, "interrupted", ep=ep))
                cancelled = True
                break
            if rc in (0, 1):
                _record(job, ep, name, True, M(job, "orphan"))
            else:
                _record(job, ep, name, False, M(job, "mkvfail") + ": " + (stderr.strip()[:300]))

    with jobs_lock:
        job["status"] = "cancelled" if job["cancel"] else "done"
        job["current"] = None


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
    parent_path = parent if (rp != ROOT and safe_dir(parent)) else None
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
        return jsonify({"error": f"{e}", "results": []}), 200
    for r in results:
        r["imdb_url"] = f"https://www.imdb.com/title/{r['imdb']}/" if r.get("imdb") else None
    return jsonify({"results": results})


@app.get("/api/seasons")
def api_seasons():
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


@app.post("/api/merge")
def api_merge():
    data = request.get_json(force=True) or {}
    directory = safe_dir(data.get("directory", ""))
    if not directory:
        return jsonify({"error": "Dossier invalide."}), 400
    items = data.get("items", [])
    orphans = data.get("orphans", [])
    if not items and not orphans:
        return jsonify({"error": "Aucun épisode sélectionné."}), 400

    lang = data.get("lang") if data.get("lang") in ("fr", "en") else "fr"
    opts = {
        "lang": lang,
        "output_dir": (data.get("output_dir") or "merged").strip() or "merged",
        "title": L.sanitize(data.get("title") or "Serie"),
        "season": int(data.get("season") or 1),
        "template": data.get("template") or "{title} S{s}E{e}",
        "primary": "fre" if data.get("primary") == "fre" else "jpn",
        "sync": bool(data.get("sync", False)),
    }

    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"id": job_id, "status": "running",
                    "total": len(items) + len(orphans), "done": 0,
                    "current": None, "results": [], "log": [],
                    "output_dir": None, "cancel": False, "proc": None,
                    "lang": lang}
    threading.Thread(target=run_job,
                     args=(job_id, directory, items, orphans, opts),
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
#  Page (HTML + CSS + JS, FR/EN, offline-safe)
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>langmux</title>
<style>
  :root{
    --bg:#14131c; --surface:#1d1b29; --surface-2:#252335; --line:#322f45;
    --ink:#ecebf5; --muted:#9a96b5; --faint:#6f6b8c;
    --jp:#4fd6c2; --fr:#f2b65a; --accent:#ff6b8b;
    --ok:#5fd07a; --warn:#f2b65a; --bad:#ff6b6b; --radius:14px;
    --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
       line-height:1.5;-webkit-font-smoothing:antialiased}
  a{color:var(--jp)}
  .wrap{max-width:980px;margin:0 auto;padding:28px 20px 80px}
  header{display:flex;align-items:center;gap:16px;margin-bottom:26px}
  .title{flex:1}
  .title h1{margin:0;font-size:26px;font-weight:700;letter-spacing:-.01em}
  .title p{margin:2px 0 0;color:var(--muted);font-size:13.5px}
  .title b.jp{color:var(--jp)} .title b.fr{color:var(--fr)}
  #langsw{display:flex;gap:4px;border:1px solid var(--line);border-radius:9px;padding:3px}
  #langsw button{border:none;background:transparent;color:var(--muted);
    padding:5px 10px;border-radius:6px;font-size:12.5px;font-weight:700;cursor:pointer}
  #langsw button.on{background:var(--surface-2);color:var(--ink)}

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
    color:var(--ink);border-radius:9px;padding:9px 11px;font-size:14px;font-family:var(--sans)}
  input:focus,select:focus{outline:2px solid var(--jp);outline-offset:-1px}
  .row{display:flex;gap:12px;flex-wrap:wrap}
  .row > div{flex:1;min-width:150px}
  .path-mono{font-family:var(--mono)}

  button{font-family:var(--sans);font-size:14px;font-weight:600;cursor:pointer;
    white-space:nowrap;border-radius:9px;padding:9px 16px;border:1px solid var(--line);
    background:var(--surface-2);color:var(--ink);transition:.12s}
  button:hover{border-color:var(--faint)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#1b0e14}
  button.primary:hover{filter:brightness(1.08)}
  button:disabled{opacity:.4;cursor:not-allowed}
  .ghost{background:transparent}

  .browser{font-family:var(--mono);font-size:13px}
  .crumbs{display:flex;align-items:center;gap:8px;color:var(--muted);margin-bottom:10px;flex-wrap:wrap}
  .dirlist{display:flex;flex-direction:column;gap:4px;max-height:230px;overflow:auto;
           border:1px solid var(--line);border-radius:10px;padding:6px}
  .diritem{text-align:left;background:transparent;border:none;padding:7px 9px;border-radius:7px;
           color:var(--ink);display:flex;gap:9px;align-items:center}
  .diritem:hover{background:var(--surface-2)}
  .diritem .ico{color:var(--faint)}

  .results{display:flex;flex-direction:column;gap:8px;margin-top:10px}
  .res{display:flex;gap:12px;align-items:center;padding:10px 12px;border:1px solid var(--line);
       border-radius:10px;background:var(--surface-2);cursor:pointer}
  .res.sel{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
  .res .yr{color:var(--jp);font-family:var(--mono);font-size:13px}
  .res .meta{color:var(--faint);font-size:12px;margin-left:auto;text-align:right}

  .epcount{margin-top:14px}
  .epcount .num{font-family:var(--mono);color:var(--ink);font-weight:700}
  .epcount .ok2{color:var(--ok)} .epcount .miss{color:var(--warn)}
  .pill{display:inline-flex;gap:7px;align-items:center;font-size:12.5px;
        background:var(--surface-2);border:1px solid var(--line);border-radius:999px;padding:5px 11px}

  .plan{display:flex;flex-direction:column;gap:8px}
  .ep{border:1px solid var(--line);border-radius:11px;background:var(--surface-2);padding:11px 13px}
  .ep.amb{border-color:var(--warn)} .ep.off{opacity:.45}
  .ep-top{display:flex;align-items:center;gap:11px}
  .epno{font-family:var(--mono);font-size:13px;color:var(--muted);min-width:38px}
  .outname{font-weight:600;font-size:14px}
  .ep-top .right{margin-left:auto;display:flex;gap:8px;align-items:center}
  .lanes{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:9px}
  .lane{font-family:var(--mono);font-size:12px;color:var(--muted);background:var(--surface);
        border:1px solid var(--line);border-radius:8px;padding:7px 9px;display:flex;gap:8px;
        align-items:center;overflow:hidden}
  .lane .fn{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto}
  .dot.jp{background:var(--jp)} .dot.fr{background:var(--fr)}
  .tag{font-family:var(--mono);font-size:10.5px;letter-spacing:.04em;padding:2px 6px;
       border-radius:5px;text-transform:uppercase}
  .tag.jp{color:var(--jp);border:1px solid var(--jp)}
  .tag.fr{color:var(--fr);border:1px solid var(--fr)}
  .tag.warn{color:var(--warn);border:1px solid var(--warn)}
  .swap{font-family:var(--mono);font-size:12px;margin-top:9px;color:var(--muted)}
  .swap select{display:inline-block;width:auto;padding:4px 8px;font-size:12px}
  .chk{width:17px;height:17px;accent-color:var(--accent);vertical-align:middle}

  .muted{color:var(--muted)} .small{font-size:12.5px}
  .seg{display:flex;border:1px solid var(--line);border-radius:9px;overflow:hidden}
  .seg button{border:none;border-radius:0;flex:1;background:var(--surface-2)}
  .seg button.on{color:#1b0e14}
  .seg button.on.jp{background:var(--jp)} .seg button.on.fr{background:var(--fr)}
  .note{font-size:12.5px;color:var(--faint);margin-top:8px}
  .bad{color:var(--bad)} .ok{color:var(--ok)} .warnc{color:var(--warn)}

  .plan-hdr{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px;
            padding:10px 12px;background:var(--surface-2);border:1px solid var(--line);border-radius:10px}
  .plan-hdr .ph-label{font-size:12.5px;color:var(--muted);white-space:nowrap}
  .plan-hdr .ph-sep{width:1px;background:var(--line);align-self:stretch;margin:0 4px}
  .fr-mode{margin-top:9px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .tag.multi{color:#c47ef5;border-color:#c47ef5}
  .cls-sel{display:inline-block;width:auto;padding:3px 7px;font-size:12px;
           background:var(--surface);border:1px solid var(--line);color:var(--ink);border-radius:7px}
  .orphbox{margin-top:14px;border:1px dashed var(--line);border-radius:11px;padding:12px}
  .orphbox .head{display:flex;gap:9px;align-items:center;font-size:13px;color:var(--ink)}
  .orphbox .olist{margin-top:8px;display:flex;flex-direction:column;gap:4px}
  .orphbox .orow{font-family:var(--mono);font-size:12px;color:var(--muted)}
  .orphbox .orow .arr{color:var(--accent)}

  .runhead{display:flex;align-items:center;gap:18px;margin-bottom:16px}
  .runstats{flex:1}
  .runstats .big{font-size:22px;font-weight:700}
  .runstats .sub2{color:var(--muted);font-size:13px;margin-top:2px}
  .runstats .counts{display:flex;gap:14px;margin-top:8px;font-size:12.5px}
  .runstats .counts b{font-family:var(--mono)}
  .c-ok{color:var(--ok)} .c-bad{color:var(--bad)} .c-pend{color:var(--faint)}

  .tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
  .tile{border:1px solid var(--line);border-radius:12px;background:var(--surface-2);padding:12px;
        display:flex;flex-direction:column;gap:8px;position:relative;transition:border-color .2s}
  .tile .tg{width:100%;height:30px}
  .tile .tep{font-family:var(--mono);font-size:12px;color:var(--muted);display:flex;
             justify-content:space-between;align-items:center}
  .tile .tep .ot{font-size:9.5px;letter-spacing:.04em;text-transform:uppercase;color:var(--faint)}
  .tile .tname{font-size:12.5px;font-weight:600;line-height:1.3;
        display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .tile .tstate{font-size:11.5px;color:var(--faint);display:flex;align-items:center;gap:6px}
  .tile .tstate .ic{font-weight:700}
  .tile .gJ{stroke:var(--jp)} .tile .gF{stroke:var(--fr)}
  .tile .gO{stroke:#3a3750;transition:stroke .3s} .tile .gd{fill:#3a3750;transition:fill .3s}
  .tile[data-state=pending]{opacity:.6}
  .tile[data-state=active]{border-color:var(--accent);
        box-shadow:0 0 0 1px var(--accent),0 0 22px -6px var(--accent);animation:pulse 1.3s ease-in-out infinite}
  .tile[data-state=active] .tstate{color:var(--accent)}
  .tile[data-state=done]{border-color:var(--ok)} .tile[data-state=done] .tstate{color:var(--ok)}
  .tile[data-state=done] .gO{stroke:var(--accent)} .tile[data-state=done] .gd{fill:var(--accent)}
  .tile[data-state=failed]{border-color:var(--bad)} .tile[data-state=failed] .tstate{color:var(--bad)}
  .tile[data-state=cancelled]{border-color:var(--faint);opacity:.55}
  @keyframes pulse{0%,100%{box-shadow:0 0 0 1px var(--accent),0 0 18px -8px var(--accent)}
                   50%{box-shadow:0 0 0 1px var(--accent),0 0 26px -2px var(--accent)}}
  .logwrap{margin-top:14px;border:1px solid var(--line);border-radius:10px;background:#100f17;padding:4px 12px}
  .logwrap summary{cursor:pointer;color:var(--muted);font-size:12.5px;padding:8px 0}
  .log{font-family:var(--mono);font-size:12px;max-height:240px;overflow:auto;white-space:pre-wrap;margin:0 0 10px}
  .hidden{display:none}
  @media (prefers-reduced-motion:reduce){.tile[data-state=active]{animation:none}}
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
      <p data-i18n-html="tagline"></p>
    </div>
    <div id="langsw">
      <button data-l="fr">FR</button><button data-l="en">EN</button>
    </div>
  </header>

  <section class="card" id="card-folder">
    <div class="step"><span class="n">01</span><h2 data-i18n="step1_title"></h2>
      <span class="sub"><span data-i18n="root_label"></span> <span id="root" class="path-mono"></span></span></div>
    <div class="browser">
      <div class="crumbs"><button class="ghost" id="up" data-i18n="up"></button><span id="cwd"></span></div>
      <div class="dirlist" id="dirs"></div>
      <div class="row" style="margin-top:12px;align-items:center">
        <div style="flex:2"><span class="small muted" id="folderinfo"></span></div>
        <button class="primary" id="scan" style="flex:0" data-i18n="scan_btn"></button>
      </div>
    </div>
  </section>

  <section class="card dim" id="card-config">
    <div class="step"><span class="n">02</span><h2 data-i18n="step2_title"></h2>
      <span class="sub" id="scaninfo"></span></div>
    <label data-i18n="query_label"></label>
    <div class="row" style="align-items:center">
      <div style="flex:3"><input type="text" id="query"></div>
      <button id="search" style="flex:0" data-i18n="search_btn"></button>
    </div>
    <div class="results" id="results"></div>
    <p class="note" data-i18n="imdb_note"></p>
    <div class="row" style="margin-top:14px">
      <div><label data-i18n="title_label"></label><input type="text" id="title"></div>
      <div style="max-width:120px"><label data-i18n="season_label"></label>
        <input type="number" id="season" min="0" value="1"></div>
    </div>
    <div class="epcount" id="epcount"></div>
    <div class="row" style="margin-top:14px">
      <div>
        <label data-i18n="primary_label"></label>
        <div class="seg" id="primary">
          <button data-v="jpn" class="on jp" data-i18n="primary_jp"></button>
          <button data-v="fre" data-i18n="primary_fr"></button>
        </div>
        <p class="note" id="subnote"></p>
      </div>
      <div>
        <label><span data-i18n="template_label"></span> <span class="muted small">{title} {s} {e}</span></label>
        <input type="text" id="template" value="{title} S{s}E{e}">
        <div class="row" style="margin-top:10px">
          <div><label data-i18n="outdir_label"></label><input type="text" id="outdir" value="merged"></div>
          <div style="max-width:130px"><label data-i18n="tol_label"></label><input type="number" id="tol" value="10" min="0"></div>
        </div>
        <div style="margin-top:12px;display:flex;align-items:center;gap:9px">
          <input type="checkbox" id="do-sync" class="chk">
          <span class="small" data-i18n="sync_opt"></span>
        </div>
        <p class="note" id="syncnote" data-i18n="sync_note"></p>
      </div>
    </div>
  </section>

  <section class="card dim" id="card-plan">
    <div class="step"><span class="n">03</span><h2 data-i18n="step3_title"></h2>
      <span class="sub" id="planinfo"></span></div>
    <div id="plan-header"></div>
    <div class="plan" id="plan"></div>
    <div id="extras"></div>
    <div class="row" style="margin-top:16px;align-items:center">
      <div style="flex:2"><span class="small muted" id="selinfo"></span></div>
      <button class="primary" id="run" style="flex:0" data-i18n="run_btn"></button>
    </div>
  </section>

  <section class="card dim" id="card-run">
    <div class="step"><span class="n">04</span><h2 data-i18n="step4_title"></h2>
      <span class="sub" id="runinfo"></span></div>
    <div class="runhead">
      <span class="ring">
        <svg width="76" height="76" viewBox="0 0 76 76">
          <circle cx="38" cy="38" r="32" fill="none" stroke="#322f45" stroke-width="7"/>
          <circle id="ringfg" cx="38" cy="38" r="32" fill="none" stroke="#ff6b8b" stroke-width="7"
                  stroke-linecap="round" stroke-dasharray="201" stroke-dashoffset="201"
                  transform="rotate(-90 38 38)" style="transition:stroke-dashoffset .35s"/>
          <text id="ringtxt" x="38" y="43" text-anchor="middle"
                font-family="ui-monospace,monospace" font-size="17" fill="#ecebf5">0%</text>
        </svg>
      </span>
      <div class="runstats">
        <div class="big" id="runbig"></div>
        <div class="sub2" id="runsub"></div>
        <div class="counts">
          <span class="c-ok">&#10003; <b id="cdone">0</b> <span data-i18n="c_ok"></span></span>
          <span class="c-bad">&#10007; <b id="cfail">0</b> <span data-i18n="c_fail"></span></span>
          <span class="c-pend">&bull; <b id="cpend">0</b> <span data-i18n="c_pend"></span></span>
        </div>
      </div>
      <button id="cancel" class="ghost" data-i18n="cancel_btn"></button>
    </div>
    <div class="tiles" id="tiles"></div>
    <details class="logwrap"><summary data-i18n="log_summary"></summary><div class="log" id="log"></div></details>
  </section>
</div>

<script>
const I18N={
 fr:{
  tagline:'Réunit une piste <b class="jp">VO japonaise</b> et un <b class="fr">doublage français</b> du même épisode en un seul .mkv.',
  step1_title:'Choisir le dossier', root_label:'racine :', up:'↑ dossier parent',
  scan_btn:'Analyser ce dossier', analyzing:'Analyse…',
  step2_title:'Série, audio et nommage', query_label:'Nom de la série à rechercher', search_btn:'Rechercher',
  imdb_note:"Vérifie l'année et ouvre la fiche IMDB pour confirmer. Tu peux aussi garder un titre saisi à la main.",
  title_label:'Titre retenu (sert au nommage)', season_label:'Saison',
  primary_label:'Piste audio principale (lue par défaut)', primary_jp:'Japonais (VO)', primary_fr:'Français (VF)',
  template_label:'Modèle de nom', outdir_label:'Sous-dossier de sortie', tol_label:'Écart toléré (s)',
  step3_title:'Vérifier le plan', run_btn:'Lancer la fusion',
  step4_title:'Fusion', cancel_btn:'Annuler', log_summary:'Journal détaillé',
  c_ok:'réussis', c_fail:'échecs', c_pend:'en attente',
  folder_ready:'{n} fichier(s) vidéo ici — prêt à analyser',
  folder_none:'Aucune vidéo dans ce dossier (descends dans un sous-dossier).',
  no_subdir:'Aucun sous-dossier.',
  scaninfo:'{f} fichier(s) · {p} paire(s)',
  searching:'Recherche…', search_none:'Rien trouvé — garde le titre saisi à la main.',
  search_err:'Recherche indisponible : {e}', no_imdb:"pas d'IMDB",
  subnote_jp:"Sous-titres FR forcés (affichés par défaut) car l'audio n'est pas le français.",
  subnote_fr:'Sous-titres FR présents mais non forcés (audio français principal).',
  epcount_checking:"Vérification du nombre d'épisodes…",
  epcount_unknown:"Saison {s} : nombre d'épisodes inconnu sur TVMaze",
  epcount_main:'Saison {s} : {off} épisodes officiels',
  epcount_all:' — tu as les {n} épisodes ✓', epcount_missing:' — {have} ici, {miss} manquant(s)',
  epcount_more:' — {have} paires ici',
  tag_confirm:'à confirmer', swap_label:'Piste VF : ',
  orphan_opt:'Traiter aussi les épisodes orphelins (réencapsuler le fichier unique)',
  orphans_head:'Épisodes orphelins (une seule langue)',
  toomany:'Épisodes avec plus de 2 fichiers (ignorés) : {x}',
  unparsed:'Numéro non détecté : {x}',
  dur_unknown:'durée ?',
  selinfo:'{n} épisode(s) sélectionné(s) pour la fusion',
  tile_pending:'en attente', tile_active:'fusion en cours…', tile_cancelled:'annulé',
  ot_orphan:'orphelin',
  run_prep:'Préparation…', run_running:'Fusion en cours', run_cancelling:'Annulation…',
  run_cancelled:'Annulé', run_done:'Terminé',
  run_sub_cancelled:"{ok} épisode(s) fusionné(s) avant l'arrêt", run_sub_done:'{ok} réussi(s) · {bad} échec(s)',
  run_relaunch:'Relancer', run_inprogress:'Fusion en cours…',
  sync_opt:'Synchronisation automatique VF/VOSTFR (correction offset + vitesse)',
  sync_note:'Analyse les pistes audio par corrélation croisée pour aligner le doublage sur la VO. Ajoute ~1-2 min par épisode. Nécessite numpy et scipy.',
  tag_multi:'MULTI',
  swap_all:'Inverser tous',
  ph_multi_label:'Piste FR intégrée :',
  efm_auto:'FR intégré (sync garanti)',
  efm_external:'Remplacer par le fichier VF',
  efm_both:'Garder les deux',
  apply_all:'Appliquer à tous',
  orphan_cls_label:'Type :',
  cls_vostfr:'VOSTFR',cls_french:'VF',cls_multi:'MULTI'
 },
 en:{
  tagline:'Combines a <b class="jp">Japanese original</b> and a <b class="fr">French dub</b> of the same episode into one .mkv.',
  step1_title:'Choose the folder', root_label:'root:', up:'↑ parent folder',
  scan_btn:'Analyze this folder', analyzing:'Analyzing…',
  step2_title:'Series, audio and naming', query_label:'Series name to search', search_btn:'Search',
  imdb_note:'Check the year and open the IMDB page to confirm. You can also keep a manually typed title.',
  title_label:'Chosen title (used for naming)', season_label:'Season',
  primary_label:'Main audio track (played by default)', primary_jp:'Japanese (orig.)', primary_fr:'French (dub)',
  template_label:'Naming template', outdir_label:'Output sub-folder', tol_label:'Tolerated gap (s)',
  step3_title:'Review the plan', run_btn:'Start the merge',
  step4_title:'Merge', cancel_btn:'Cancel', log_summary:'Detailed log',
  c_ok:'done', c_fail:'failed', c_pend:'pending',
  folder_ready:'{n} video file(s) here — ready to analyze',
  folder_none:'No video in this folder (go into a sub-folder).',
  no_subdir:'No sub-folder.',
  scaninfo:'{f} file(s) · {p} pair(s)',
  searching:'Searching…', search_none:'Nothing found — keep the manually typed title.',
  search_err:'Search unavailable: {e}', no_imdb:'no IMDB',
  subnote_jp:'French subtitles forced (shown by default) since the audio is not French.',
  subnote_fr:'French subtitles present but not forced (French audio is primary).',
  epcount_checking:'Checking episode count…',
  epcount_unknown:'Season {s}: episode count unknown on TVMaze',
  epcount_main:'Season {s}: {off} official episodes',
  epcount_all:' — you have all {n} episodes ✓', epcount_missing:' — {have} here, {miss} missing',
  epcount_more:' — {have} pairs here',
  tag_confirm:'to confirm', swap_label:'French track: ',
  orphan_opt:'Also process orphan episodes (repackage the single file)',
  orphans_head:'Orphan episodes (single language)',
  toomany:'Episodes with more than 2 files (ignored): {x}',
  unparsed:'Unrecognized number: {x}',
  dur_unknown:'duration?',
  selinfo:'{n} episode(s) selected for the merge',
  tile_pending:'pending', tile_active:'merging…', tile_cancelled:'cancelled',
  ot_orphan:'orphan',
  run_prep:'Preparing…', run_running:'Merging', run_cancelling:'Cancelling…',
  run_cancelled:'Cancelled', run_done:'Done',
  run_sub_cancelled:'{ok} episode(s) merged before stopping', run_sub_done:'{ok} done · {bad} failed',
  run_relaunch:'Restart', run_inprogress:'Merging…',
  sync_opt:'Auto-sync VF/VOSTFR (offset + speed correction)',
  sync_note:'Analyses both audio tracks via cross-correlation to align the dub with the original. Adds ~1-2 min per episode. Requires numpy and scipy.',
  tag_multi:'MULTI',
  swap_all:'Swap all',
  ph_multi_label:'Embedded FR track:',
  efm_auto:'Embedded FR (already in sync)',
  efm_external:'Replace with external VF',
  efm_both:'Keep both',
  apply_all:'Apply to all',
  orphan_cls_label:'Type:',
  cls_vostfr:'VOSTFR',cls_french:'VF',cls_multi:'MULTI'
 }
};
let LANG=localStorage.getItem('langmux_lang')||((navigator.language||'fr').slice(0,2)==='en'?'en':'fr');
function t(k,p){let s=(I18N[LANG]&&I18N[LANG][k])||I18N.fr[k]||k;
  if(p)for(const kk in p)s=s.split('{'+kk+'}').join(p[kk]);return s;}

const $=s=>document.querySelector(s);
const ce=(t2,c)=>{const e=document.createElement(t2);if(c)e.className=c;return e};
let state={dir:null,scan:null,primary:'jpn',rows:[],tiles:{},lastBrowse:null,showId:null,seasonCounts:{}};

function applyLang(lang){
  LANG=lang;localStorage.setItem('langmux_lang',lang);document.documentElement.lang=lang;
  document.querySelectorAll('[data-i18n]').forEach(e=>e.textContent=t(e.dataset.i18n));
  document.querySelectorAll('[data-i18n-html]').forEach(e=>e.innerHTML=t(e.dataset.i18nHtml));
  document.querySelectorAll('#langsw button').forEach(b=>b.classList.toggle('on',b.dataset.l===lang));
  refreshDynamic();
}
function refreshDynamic(){
  if(state.lastBrowse)renderFolderInfo(state.lastBrowse);
  setSubnote();
  if(state.scan){
    $('#scaninfo').textContent=t('scaninfo',{f:state.scan.files_count,p:state.scan.items.length});
    const prev={};state.rows.forEach(r=>prev[r.ep]={include:r.include,french:r.french});
    buildPlan(prev);renderEpCount();
  }
}
document.querySelectorAll('#langsw button').forEach(b=>b.onclick=()=>applyLang(b.dataset.l));

function activate(id){$('#'+id).classList.remove('dim')}

/* ---- 1. Dossiers ---- */
function renderFolderInfo(d){
  $('#root').textContent=d.root;$('#cwd').textContent=d.path;
  $('#folderinfo').textContent=d.video_count?t('folder_ready',{n:d.video_count}):t('folder_none');
  $('#scan').disabled=!d.video_count;
}
async function browse(path){
  const r=await fetch('/api/browse'+(path?('?path='+encodeURIComponent(path)):''));
  const d=await r.json();state.dir=d.path;state.lastBrowse=d;
  $('#up').disabled=!d.parent;$('#up').onclick=()=>browse(d.parent);
  const list=$('#dirs');list.innerHTML='';
  if(!d.dirs.length){const e=ce('div','small muted');e.style.padding='8px';e.textContent=t('no_subdir');list.appendChild(e);}
  d.dirs.forEach(x=>{const b=ce('button','diritem');b.innerHTML='<span class="ico">▸</span>'+esc(x.name);
    b.onclick=()=>browse(x.path);list.appendChild(b);});
  renderFolderInfo(d);
}
$('#scan').onclick=scan;

/* ---- 2. Analyse ---- */
async function scan(){
  $('#scan').disabled=true;$('#scan').textContent=t('analyzing');
  const r=await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({directory:state.dir})});
  const d=await r.json();
  $('#scan').textContent=t('scan_btn');$('#scan').disabled=false;
  if(d.error){alert(d.error);return;}
  state.scan=d;
  $('#scaninfo').textContent=t('scaninfo',{f:d.files_count,p:d.items.length});
  $('#query').value=d.guess;$('#title').value=d.guess;$('#season').value=d.default_season;
  activate('card-config');buildPlan();activate('card-plan');doSearch();
  $('#card-config').scrollIntoView({behavior:'smooth',block:'start'});
}

/* ---- 2b. TVMaze ---- */
$('#search').onclick=doSearch;
$('#query').addEventListener('keydown',e=>{if(e.key==='Enter')doSearch()});
async function doSearch(){
  const q=$('#query').value.trim();if(!q)return;
  const box=$('#results');box.innerHTML='<div class="small muted">'+t('searching')+'</div>';
  const r=await fetch('/api/search?q='+encodeURIComponent(q));const d=await r.json();box.innerHTML='';
  if(d.error){box.innerHTML='<div class="small warnc">'+t('search_err',{e:esc(d.error)})+'</div>';}
  if(!d.results.length){const e=ce('div','small muted');e.textContent=t('search_none');box.appendChild(e);return;}
  d.results.forEach(x=>{
    const row=ce('div','res');
    const imdb=x.imdb_url?('<a href="'+x.imdb_url+'" target="_blank" rel="noopener">IMDB ↗</a>'):t('no_imdb');
    row.innerHTML='<div>'+esc(x.name)+'</div><span class="yr">('+(x.year||'????')+')</span>'+
      '<span class="meta">'+esc(x.language||'')+'<br>'+imdb+'</span>';
    row.onclick=()=>{document.querySelectorAll('.res').forEach(e=>e.classList.remove('sel'));
      row.classList.add('sel');$('#title').value=x.name;state.showId=x.id;refreshNames();updateEpCount();};
    box.appendChild(row);
  });
}

/* ---- audio principal ---- */
function setSubnote(){$('#subnote').textContent=state.primary==='jpn'?t('subnote_jp'):t('subnote_fr');}
$('#primary').querySelectorAll('button').forEach(b=>{
  b.onclick=()=>{state.primary=b.dataset.v;
    $('#primary').querySelectorAll('button').forEach(x=>{x.className=''});
    b.className='on '+(b.dataset.v==='jpn'?'jp':'fr');setSubnote();};
});

['title','season','template'].forEach(id=>$('#'+id).addEventListener('input',refreshNames));
$('#season').addEventListener('input',renderEpCount);
$('#tol').addEventListener('input',refreshNames);

/* ---- nombre d'épisodes (TVMaze) ---- */
async function updateEpCount(){
  const el=$('#epcount');if(!state.showId){el.innerHTML='';return;}
  el.innerHTML='<span class="pill">'+t('epcount_checking')+'</span>';
  let counts={};try{counts=(await (await fetch('/api/seasons?show_id='+state.showId)).json()).counts||{};}catch(e){}
  state.seasonCounts=counts;renderEpCount();
}
function renderEpCount(){
  const el=$('#epcount');const counts=state.seasonCounts||{};
  if(!state.showId){el.innerHTML='';return;}
  const s=parseInt($('#season').value||'1');const off=counts[String(s)];
  const have=state.scan?state.scan.items.length:0;
  if(off==null){el.innerHTML='<span class="pill">'+t('epcount_unknown',{s:s})+'</span>';return;}
  let cmp='';
  if(have===off)cmp='<span class="ok2">'+t('epcount_all',{n:have})+'</span>';
  else if(have<off)cmp='<span class="miss">'+t('epcount_missing',{have:have,miss:off-have})+'</span>';
  else cmp=t('epcount_more',{have:have});
  const main=t('epcount_main',{s:'<span class="num">'+s+'</span>',off:'<span class="num">'+off+'</span>'});
  el.innerHTML='<span class="pill">'+main+cmp+'</span>';
}

/* ---- 3. Plan ---- */
function buildPlan(prev){
  const plan=$('#plan');plan.innerHTML='';state.rows=[];
  state.scan.items.forEach(it=>{
    const keep=prev&&prev[it.ep];
    const row={ep:it.ep,files:it.files,vostfr:it.vostfr,french:(keep?keep.french:it.french),
               status:it.status,include:(keep?keep.include:true),
               hasEmbeddedFr:it.has_embedded_fr||false,
               embeddedFrMode:(keep?keep.embeddedFrMode:'auto')};
    if(row.french!==it.french){row.vostfr=it.files.find(f=>f.name!==row.french).name;}
    state.rows.push(row);
    const el=ce('div','ep'+(it.status==='ambiguous'?' amb':'')+(row.include?'':' off'));
    const top=ce('div','ep-top');
    const chk=ce('input');chk.type='checkbox';chk.className='chk';chk.checked=row.include;
    chk.onchange=()=>{row.include=chk.checked;el.classList.toggle('off',!chk.checked);updateSel();};
    const epno=ce('span','epno');epno.textContent='E'+String(it.ep).padStart(2,'0');
    const outn=ce('span','outname');
    const right=ce('div','right');
    if(it.status==='ambiguous'){const tg=ce('span','tag warn');tg.textContent=t('tag_confirm');right.appendChild(tg);}
    const dw=ce('span');right.appendChild(dw);
    top.append(chk,epno,outn,right);
    const lanes=ce('div','lanes');
    const multiTag=row.hasEmbeddedFr?'<span class="tag multi" style="margin-left:6px">'+t('tag_multi')+'</span>':'';
    lanes.innerHTML='<div class="lane"><span class="dot jp"></span><span class="tag jp">JP</span>'+multiTag+'<span class="fn" data-v></span></div>'+
      '<div class="lane"><span class="dot fr"></span><span class="tag fr">VF</span><span class="fn" data-f></span></div>';
    const swap=ce('div','swap');const lbl=ce('span');lbl.dataset.swap='1';lbl.textContent=t('swap_label');
    const sel=ce('select');it.files.forEach(f=>{const o=ce('option');o.value=f.name;o.textContent=f.name;sel.appendChild(o);});
    sel.value=row.french;
    sel.onchange=()=>{row.french=sel.value;row.vostfr=it.files.find(f=>f.name!==sel.value).name;
      lanes.querySelector('[data-v]').textContent=row.vostfr;lanes.querySelector('[data-f]').textContent=row.french;};
    swap.append(lbl,sel);
    lanes.querySelector('[data-v]').textContent=row.vostfr;lanes.querySelector('[data-f]').textContent=row.french;
    el.append(top,lanes,swap);
    // Contrôle FR intégré (visible seulement pour les fichiers MULTI)
    if(row.hasEmbeddedFr){
      const fm=ce('div','fr-mode');
      const fml=ce('span','ph-label');fml.textContent=t('ph_multi_label');
      const fmSeg=ce('div','seg');fmSeg.style.cssText='display:inline-flex;width:auto';
      [['auto',t('efm_auto')],['external',t('efm_external')],['both',t('efm_both')]].forEach(([v,lb])=>{
        const b=ce('button');b.textContent=lb;b.dataset.v=v;
        b.className=(row.embeddedFrMode===v)?'on fr':'';
        b.onclick=()=>{row.embeddedFrMode=v;
          fmSeg.querySelectorAll('button').forEach(x=>{x.className=x.dataset.v===v?'on fr':''});};
        fmSeg.appendChild(b);
      });
      fm.append(fml,fmSeg);el.appendChild(fm);
      row._fmSeg=fmSeg;
    }
    plan.appendChild(el);
    row._out=outn;row._dur=dw;
  });
  renderPlanHeader();renderExtras();refreshNames();updateSel();
}

function renderPlanHeader(){
  const hdr=$('#plan-header');hdr.innerHTML='';
  if(!state.rows.length)return;
  const ph=ce('div','plan-hdr');
  // Bouton "Inverser tous"
  const swBtn=ce('button','ghost');swBtn.textContent=t('swap_all');swBtn.style.fontSize='12.5px';
  swBtn.onclick=()=>{
    state.rows.forEach(row=>{
      const tmp=row.french;row.french=row.vostfr;row.vostfr=tmp;
      // Mettre à jour le select et les lanes
      const ep=document.querySelector('.ep-top .epno');
      // on passe par refreshNames qui relit row.vostfr/french
    });
    // Resync UI : reconstruire le plan en conservant les états
    const prev={};state.rows.forEach(r=>prev[r.ep]={include:r.include,french:r.french,embeddedFrMode:r.embeddedFrMode});
    buildPlan(prev);
  };
  ph.appendChild(swBtn);
  // Contrôles "appliquer à tous" pour les épisodes MULTI
  const multiRows=state.rows.filter(r=>r.hasEmbeddedFr);
  if(multiRows.length){
    const sep=ce('div','ph-sep');ph.appendChild(sep);
    const lbl=ce('span','ph-label');lbl.textContent=t('ph_multi_label');
    const gSel=ce('select','cls-sel');
    [['auto',t('efm_auto')],['external',t('efm_external')],['both',t('efm_both')]].forEach(([v,lb])=>{
      const o=ce('option');o.value=v;o.textContent=lb;gSel.appendChild(o);
    });
    const applyBtn=ce('button','ghost');applyBtn.textContent=t('apply_all');applyBtn.style.fontSize='12.5px';
    applyBtn.onclick=()=>{
      const v=gSel.value;
      const prev={};state.rows.forEach(r=>prev[r.ep]={include:r.include,french:r.french,embeddedFrMode:r.hasEmbeddedFr?v:r.embeddedFrMode});
      buildPlan(prev);
    };
    ph.append(lbl,gSel,applyBtn);
  }
  hdr.appendChild(ph);
}

function renderExtras(){
  const ex=$('#extras');ex.innerHTML='';
  const orphans=state.scan.orphans||[];
  if(orphans.length){
    const box=ce('div','orphbox');
    const head=ce('div','head');
    const chk=ce('input');chk.type='checkbox';chk.className='chk';chk.id='do-orphans';
    chk.checked=state.doOrphans||false;
    chk.onchange=()=>{state.doOrphans=chk.checked;renderOrphanList();updateSel();};
    const lab=ce('span');lab.textContent=t('orphan_opt');
    head.append(chk,lab);box.appendChild(head);
    const list=ce('div','olist');list.id='olist';box.appendChild(list);
    ex.appendChild(box);renderOrphanList();
  }
  if(state.scan.toomany.length){const d=ce('div','small muted');d.style.marginTop='10px';
    d.textContent=t('toomany',{x:state.scan.toomany.map(x=>'E'+String(x.ep).padStart(2,'0')).join(', ')});ex.appendChild(d);}
  if(state.scan.unparsed.length){const d=ce('div','small muted');d.style.marginTop='6px';
    d.textContent=t('unparsed',{x:state.scan.unparsed.join(', ')});ex.appendChild(d);}
}
function renderOrphanList(){
  const list=$('#olist');if(!list)return;list.innerHTML='';
  const title=$('#title').value||'Serie',s=parseInt($('#season').value||'1'),tpl=$('#template').value||'{title} S{s}E{e}';
  (state.scan.orphans||[]).forEach(o=>{
    if(!state.orphanCls)state.orphanCls={};
    if(!state.orphanCls[o.ep])state.orphanCls[o.ep]=o.file.cls||'vostfr';
    const row=ce('div','orow');
    const info=ce('span');
    info.textContent='E'+String(o.ep).padStart(2,'0')+'  '+o.file.name;
    const clsLbl=ce('span','ph-label');clsLbl.style.margin='0 6px 0 10px';clsLbl.textContent=t('orphan_cls_label');
    const clsSel=ce('select','cls-sel');
    [['vostfr',t('cls_vostfr')],['french',t('cls_french')],['multi',t('cls_multi')]].forEach(([v,lb])=>{
      const op=ce('option');op.value=v;op.textContent=lb;clsSel.appendChild(op);
    });
    clsSel.value=state.orphanCls[o.ep];
    clsSel.onchange=()=>{state.orphanCls[o.ep]=clsSel.value;};
    row.append(info,clsLbl,clsSel);
    if(state.doOrphans){const arr=ce('span','arr');arr.textContent='   → '+fmt(tpl,title,s,o.ep)+'.mkv';row.appendChild(arr);}
    list.appendChild(row);
  });
}

function fmt(t2,title,s,ep){return t2.replace('{title}',title).replace('{s}',String(s).padStart(2,'0')).replace('{e}',String(ep).padStart(2,'0'));}
function refreshNames(){
  const title=$('#title').value||'Serie',s=parseInt($('#season').value||'1'),tpl=$('#template').value||'{title} S{s}E{e}';
  const tol=parseFloat($('#tol').value||'10');
  state.rows.forEach(r=>{
    r._out.textContent=fmt(tpl,title,s,r.ep)+'.mkv';
    const a=r.files[0],b=r.files[1];r._dur.innerHTML='';
    if(a.duration!=null&&b.duration!=null){
      const diff=Math.abs(a.duration-b.duration);
      if(diff>tol){const tg=ce('span','tag warn');tg.textContent='Δ '+diff.toFixed(1)+'s';r._dur.appendChild(tg);}
      else{const tg=ce('span','small muted');tg.textContent=diff.toFixed(1)+'s';r._dur.appendChild(tg);}
      if(a.fps&&b.fps&&Math.abs(a.fps-b.fps)>0.05){const tg=ce('span','tag warn');tg.style.marginLeft='6px';tg.textContent='fps≠';r._dur.appendChild(tg);}
    }else{const tg=ce('span','tag warn');tg.textContent=t('dur_unknown');r._dur.appendChild(tg);}
  });
  renderOrphanList();
}
function updateSel(){
  let n=state.rows.filter(r=>r.include).length;
  if(state.doOrphans)n+=(state.scan.orphans||[]).length;
  $('#selinfo').textContent=t('selinfo',{n:n});
  $('#run').disabled=!n;
}

/* ---- 4. Fusion ---- */
$('#run').onclick=run;
$('#cancel').onclick=async()=>{if(!state.jobId)return;
  $('#cancel').disabled=true;$('#cancel').textContent=t('run_cancelling');$('#runbig').textContent=t('run_cancelling');
  try{await fetch('/api/cancel/'+state.jobId,{method:'POST'});}catch(e){}};

function glyphSVG(){return '<svg class="tg" viewBox="0 0 76 30" preserveAspectRatio="xMidYMid meet">'+
  '<path class="gJ" d="M4 9 H40 Q52 9 52 15 Q52 21 40 21 H4" fill="none" stroke-width="2.4"/>'+
  '<path class="gF" d="M4 21 H40 Q52 21 52 15 Q52 9 40 9 H4" fill="none" stroke-width="2.4"/>'+
  '<path class="gO" d="M52 15 H68" stroke-width="2.4" fill="none"/><circle class="gd" cx="68" cy="15" r="2.6"/></svg>';}
function buildTiles(list){
  const box=$('#tiles');box.innerHTML='';state.tiles={};
  list.forEach(x=>{
    const tl=ce('div','tile');tl.dataset.state='pending';
    tl.innerHTML=glyphSVG()+
      '<div class="tep"><span>E'+String(x.ep).padStart(2,'0')+'</span>'+(x.orphan?('<span class="ot">'+t('ot_orphan')+'</span>'):'')+'</div>'+
      '<div class="tname">'+esc(x.name)+'</div>'+
      '<div class="tstate"><span class="ic">•</span><span class="msg">'+t('tile_pending')+'</span></div>';
    box.appendChild(tl);state.tiles[x.ep]=tl;
  });
  $('#cpend').textContent=list.length;$('#cdone').textContent=0;$('#cfail').textContent=0;
}
function setTile(ep,st,msg){const tl=state.tiles[ep];if(!tl)return;tl.dataset.state=st;
  const ic={active:'⟳',done:'✓',failed:'✗',cancelled:'⃠',pending:'•'}[st]||'•';
  tl.querySelector('.ic').textContent=ic;tl.querySelector('.msg').textContent=msg;}
function setRing(pct){$('#ringfg').setAttribute('stroke-dashoffset',Math.round(201*(1-pct/100)));$('#ringtxt').textContent=pct+'%';}

async function run(){
  const title=$('#title').value||'Serie',s=parseInt($('#season').value||'1'),tpl=$('#template').value||'{title} S{s}E{e}';
  const incPairs=state.rows.filter(r=>r.include);
  const items=incPairs.map(r=>({ep:r.ep,vostfr:r.vostfr,french:r.french,
    embedded_fr_mode:r.embeddedFrMode||'auto'}));
  const orphans=state.doOrphans?(state.scan.orphans||[]).map(o=>({ep:o.ep,file:o.file.name,
    cls:(state.orphanCls&&state.orphanCls[o.ep])||o.file.cls||'vostfr'})):[];
  const tileList=incPairs.map(r=>({ep:r.ep,name:fmt(tpl,title,s,r.ep)+'.mkv',orphan:false}))
    .concat(orphans.map(o=>({ep:o.ep,name:fmt(tpl,title,s,o.ep)+'.mkv',orphan:true})));
  buildTiles(tileList);setRing(0);$('#runbig').textContent=t('run_prep');$('#runsub').textContent='';
  $('#cancel').disabled=false;$('#cancel').textContent=t('cancel_btn');
  $('#run').disabled=true;$('#run').textContent=t('run_inprogress');
  activate('card-run');$('#card-run').scrollIntoView({behavior:'smooth',block:'start'});
  const body={directory:state.dir,output_dir:$('#outdir').value,title,season:s,template:tpl,
    primary:state.primary,lang:LANG,items,orphans,sync:$('#do-sync').checked};
  const d=await (await fetch('/api/merge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.error){alert(d.error);$('#run').disabled=false;$('#run').textContent=t('run_btn');return;}
  state.jobId=d.job_id;poll(d.job_id);
}
async function poll(id){
  let d;try{d=await (await fetch('/api/status/'+id)).json();}catch(e){setTimeout(()=>poll(id),1200);return;}
  const total=d.total||1,done=d.done;setRing(Math.round(100*done/total));
  const ok=d.results.filter(x=>x.ok).length,bad=d.results.filter(x=>!x.ok).length;
  $('#cdone').textContent=ok;$('#cfail').textContent=bad;$('#cpend').textContent=Math.max(total-done,0);
  d.results.forEach(x=>setTile(x.ep,x.ok?'done':'failed',x.msg));
  if(d.status==='running'&&d.current){setTile(d.current.ep,'active',t('tile_active'));
    $('#runbig').textContent=t('run_running');$('#runsub').textContent='E'+String(d.current.ep).padStart(2,'0')+' — '+d.current.name;}
  $('#log').textContent=d.log.join('\n');$('#log').scrollTop=$('#log').scrollHeight;
  if(d.status==='running'){setTimeout(()=>poll(id),900);return;}
  finishRun(d,ok,bad);
}
function finishRun(d,ok,bad){
  Object.keys(state.tiles).forEach(ep=>{const st=state.tiles[ep].dataset.state;
    if(st==='pending'||st==='active')setTile(parseInt(ep),'cancelled',t('tile_cancelled'));});
  $('#cancel').disabled=true;$('#cancel').textContent=t('cancel_btn');
  $('#run').disabled=false;$('#run').textContent=t('run_relaunch');
  const dir=d.output_dir?(' — '+d.output_dir):'';
  if(d.status==='cancelled'){$('#runbig').textContent=t('run_cancelled');$('#runsub').textContent=t('run_sub_cancelled',{ok:ok})+dir;}
  else{$('#runbig').textContent=t('run_done');$('#runsub').textContent=t('run_sub_done',{ok:ok,bad:bad})+dir;}
}

function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

applyLang(LANG);
browse(null);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
