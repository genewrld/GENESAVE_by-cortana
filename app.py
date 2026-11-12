import os, re, uuid, time, shutil, threading, subprocess
import requests, yt_dlp
from flask import Flask, jsonify, request, send_from_directory, Response

BASE = os.path.dirname(os.path.abspath(__file__))
SD = os.path.expanduser("~/storage/downloads")
SAVE = os.path.join(SD, "GeneSave") if os.path.isdir(SD) else os.path.join(BASE, "downloads")
os.makedirs(SAVE, exist_ok=True)
app = Flask(__name__)
JOBS = {}
TYPES = {"mp3": "audio", "m4a": "audio", "opus": "audio", "mp4": "video", "mkv": "video",
         "webm": "video", "jpg": "image", "jpeg": "image", "png": "image", "webp": "image"}

LOGO = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#a855f7"/></linearGradient></defs><rect width="100" height="100" rx="26" fill="url(#g)"/><path d="M50 22v38M34 46l16 16 16-16" stroke="#fff" stroke-width="9" stroke-linecap="round" stroke-linejoin="round" fill="none"/><path d="M26 76h48" stroke="#fff" stroke-width="9" stroke-linecap="round"/></svg>'''

def safe(s):
    return re.sub(r'[\\/:*?"<>|]+', "", s or "file").strip()[:80] or "file"

def scan(path):
    try: subprocess.run(["termux-media-scan", path], timeout=10)
    except Exception: pass

def est(f, dur):
    s = f.get("filesize") or f.get("filesize_approx")
    if not s and f.get("tbr") and dur: s = f["tbr"] * 125 * dur
    return int(s or 0)

def yt_search(q, n=12):
    with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "skip_download": True}) as y:
        info = y.extract_info(f"ytsearch{n}:{q}", download=False)
    out = []
    for e in info.get("entries") or []:
        if not e: continue
        vid = e.get("id")
        out.append({"kind": "yt", "title": e.get("title"), "by": e.get("uploader") or e.get("channel") or "",
                    "dur": e.get("duration"), "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                    "url": f"https://www.youtube.com/watch?v={vid}", "embed": f"https://www.youtube.com/embed/{vid}"})
    return out

def archive_search(q):
    qq = f"title:({q}) AND mediatype:movies" if q else "collection:feature_films AND mediatype:movies"
    r = requests.get("https://archive.org/advancedsearch.php", timeout=20, params={
        "q": qq, "fl[]": ["identifier", "title"], "rows": 14, "output": "json", "sort[]": "downloads desc"}).json()
    return [{"kind": "mv", "title": d.get("title", d["identifier"]), "by": "Internet Archive · free to watch",
             "thumb": f"https://archive.org/services/img/{d['identifier']}",
             "url": f"https://archive.org/details/{d['identifier']}",
             "embed": f"https://archive.org/embed/{d['identifier']}"} for d in r["response"]["docs"]]

def wall_search(q):
    p = {"purity": "100", "categories": "111", "sorting": "relevance" if q else "toplist"}
    if q: p["q"] = q
    r = requests.get("https://wallhaven.cc/api/v1/search", params=p, timeout=20).json()
    return [{"kind": "wp", "id": w["id"], "title": f"Wallpaper {w['id']}", "by": f"{w['resolution']} · ♥ {w.get('favorites', 0)}",
             "thumb": w["thumbs"]["large"], "url": w["path"], "full": w["path"], "size": w.get("file_size", 0)}
            for w in r.get("data", [])[:20]]

def run_job(jid, url, mode, h, kbps, title, size):
    job = JOBS[jid]; base = [0]; path = None
    def hook(d):
        if job["cancel"]: raise Exception("cancelled")
        if d["status"] == "downloading":
            cur = d.get("downloaded_bytes") or 0
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            job["done"] = base[0] + cur
            job["total"] = size or (base[0] + tot)
            if job["total"]: job["pct"] = min(99, round(job["done"] * 100 / job["total"], 1))
            job["speed"] = d.get("speed") or 0
            job["eta"] = d.get("eta") or 0
            job["stage"] = "downloading"
        elif d["status"] == "finished":
            base[0] += d.get("total_bytes") or d.get("downloaded_bytes") or 0
            job["stage"] = "processing"
    try:
        if mode == "wall":
            ext = url.split("?")[0].rsplit(".", 1)[-1]
            path = os.path.join(SAVE, safe(title) + "." + ext)
            job["stage"] = "downloading"
            with requests.get(url, stream=True, timeout=60) as r, open(path, "wb") as f:
                tot = int(r.headers.get("content-length", 0)) or size; got = 0; t0 = time.time()
                for ch in r.iter_content(65536):
                    if job["cancel"]: raise Exception("cancelled")
                    f.write(ch); got += len(ch)
                    job.update(done=got, total=tot, speed=got / max(time.time() - t0, .1))
                    if tot: job["pct"] = min(99, round(got * 100 / tot, 1))
        else:
            o = {"outtmpl": os.path.join(SAVE, "%(title).80s.%(ext)s"), "noplaylist": True, "quiet": True,
                 "progress_hooks": [hook], "retries": 5, "concurrent_fragment_downloads": 4}
            if mode == "mp3":
                o["format"] = "bestaudio/best"
                o["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                        "preferredquality": str(kbps or 192)}, {"key": "FFmpegMetadata"}]
            else:
                hh = int(h or 1080)
                o["format"] = f"bv*[height<={hh}][ext=mp4]+ba[ext=m4a]/bv*[height<={hh}]+ba/b[height<={hh}]/b"
                o["merge_output_format"] = "mp4"
            with yt_dlp.YoutubeDL(o) as y:
                info = y.extract_info(url, download=True)
                rd = info.get("requested_downloads")
                path = rd[0]["filepath"] if rd else y.prepare_filename(info)
        job.update(stage="done", pct=100, file=os.path.basename(path), size=os.path.getsize(path))
        scan(path)
    except Exception as e:
        if "cancelled" in str(e):
            job["stage"] = "cancelled"
            if mode == "wall" and path and os.path.exists(path): os.remove(path)
        else:
            job.update(stage="error", error=str(e)[:160])

@app.get("/")
def home(): return send_from_directory(BASE, "index.html")

@app.get("/icon.svg")
def icon(): return Response(LOGO, mimetype="image/svg+xml")

@app.get("/manifest.json")
def manifest():
    return jsonify(name="Gene Save", short_name="Gene Save", start_url="/", display="standalone",
                   background_color="#070910", theme_color="#070910",
                   icons=[{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}])

@app.get("/api/suggest")
def suggest():
    try:
        r = requests.get("https://suggestqueries.google.com/complete/search", timeout=5,
                         params={"client": "firefox", "ds": "yt", "q": request.args.get("q", "")})
        return jsonify(items=r.json()[1][:7])
    except Exception:
        return jsonify(items=[])

@app.get("/api/search")
def search():
    q = request.args.get("q", "").strip(); src = request.args.get("src", "yt")
    try:
        items = yt_search(q or "new songs 2026") if src == "yt" else archive_search(q) if src == "mv" else wall_search(q)
        return jsonify(items=items)
    except Exception as e:
        return jsonify(items=[], error="Search failed: " + str(e)[:100])

@app.get("/api/link")
def link():
    url = request.args.get("url", "")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as y:
            i = y.extract_info(url, download=False)
        emb = f"https://www.youtube.com/embed/{i['id']}" if i.get("extractor_key") == "Youtube" else None
        return jsonify(items=[{"kind": "yt", "title": i.get("title") or "Video", "by": i.get("uploader") or "",
                               "dur": i.get("duration"), "thumb": i.get("thumbnail") or "", "url": url, "embed": emb}])
    except Exception:
        return jsonify(items=[], error="Couldn't read that link")

@app.get("/api/info")
def api_info():
    url = request.args.get("url", "")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as y:
            i = y.extract_info(url, download=False)
    except Exception:
        return jsonify(error="Couldn't read this item. Try another one.")
    dur = i.get("duration") or 0
    fm = i.get("formats") or []
    aud = [f for f in fm if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    ab = max(aud, key=lambda f: f.get("abr") or 0, default=None)
    asz = est(ab, dur) if ab else 0
    by = {}
    for f in fm:
        hh = f.get("height")
        if not hh or f.get("vcodec") in (None, "none"): continue
        sz = est(f, dur)
        if f.get("acodec") in (None, "none"): sz += asz
        sc = (f.get("ext") == "mp4", sz)
        if hh not in by or sc > by[hh][0]: by[hh] = (sc, sz)
    vids = [{"h": hh, "size": by[hh][1]} for hh in sorted(by, reverse=True) if hh >= 144][:7]
    mp3 = [{"kbps": k, "size": int(dur * k * 125)} for k in (128, 192, 320)]
    return jsonify(title=i.get("title"), by=i.get("uploader") or "", dur=dur, video=vids, mp3=mp3)

@app.post("/api/download")
def download():
    d = request.get_json(force=True)
    jid = uuid.uuid4().hex[:8]
    JOBS[jid] = {"id": jid, "title": d.get("title", "file"), "mode": d["mode"], "stage": "starting", "pct": 0,
                 "done": 0, "total": d.get("size", 0), "speed": 0, "eta": 0, "file": None, "error": None,
                 "size": 0, "cancel": False, "t": time.time()}
    threading.Thread(target=run_job, daemon=True, args=(
        jid, d["url"], d["mode"], d.get("h"), d.get("kbps"), d.get("title", "file"), d.get("size", 0))).start()
    return jsonify(id=jid)

@app.get("/api/jobs")
def jobs():
    return jsonify(items=sorted(JOBS.values(), key=lambda j: -j["t"]))

@app.post("/api/cancel/<jid>")
def cancel(jid):
    if jid in JOBS: JOBS[jid]["cancel"] = True
    return jsonify(ok=True)

@app.post("/api/clear")
def clear():
    for k in [k for k, v in JOBS.items() if v["stage"] in ("done", "error", "cancelled")]: del JOBS[k]
    return jsonify(ok=True)

@app.get("/api/library")
def library():
    out = []; used = 0
    for n in os.listdir(SAVE):
        p = os.path.join(SAVE, n)
        if not os.path.isfile(p) or n.endswith((".part", ".ytdl", ".temp")) or re.search(r"\.f\d+\.", n): continue
        s = os.path.getsize(p); used += s
        out.append({"name": n, "size": s, "t": os.path.getmtime(p),
                    "type": TYPES.get(n.rsplit(".", 1)[-1].lower(), "other")})
    out.sort(key=lambda x: -x["t"])
    return jsonify(items=out, used=used, free=shutil.disk_usage(SAVE).free, folder=SAVE)

@app.get("/files/<path:name>")
def files(name): return send_from_directory(SAVE, name)

@app.delete("/api/file/<path:name>")
def delete(name):
    p = os.path.join(SAVE, os.path.basename(name))
    if os.path.isfile(p): os.remove(p)
    return jsonify(ok=True)

def tapi(cmd, name):
    p = os.path.join(SAVE, os.path.basename(name))
    try: return subprocess.run(cmd + [p], capture_output=True, timeout=15).returncode == 0
    except Exception: return False

@app.post("/api/share")
def share(): return jsonify(ok=tapi(["termux-share", "-a", "send"], request.get_json(force=True)["name"]))

@app.post("/api/setwall")
def setwall(): return jsonify(ok=tapi(["termux-wallpaper", "-f"], request.get_json(force=True)["name"]))

if __name__ == "__main__":
    print("\n  GENE SAVE running -> open http://127.0.0.1:5000 in Chrome\n  Saving to:", SAVE, "\n")
    app.run(host="127.0.0.1", port=5000, threaded=True)
