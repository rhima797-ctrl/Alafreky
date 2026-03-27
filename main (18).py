from flask import Flask, request, jsonify, Response
import requests as http_requests
import re
import sqlite3
import threading
import time
import os
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import hmac as _hmac
import hashlib
import base64 as _b64

import firebase_admin
from firebase_admin import credentials, db as firebase_db

app = Flask(__name__)


# ─── Firebase Init ────────────────────────────────────────────────────────────

_SERVICE_ACCOUNT_PATH = os.path.join(
    os.path.dirname(__file__), "serviceAccount.json", "servicAccount.json"
)

_firebase_db_url = "https://alafreky-20e4c-default-rtdb.europe-west1.firebasedatabase.app"

FIREBASE_ENABLED = False
try:
    cred = credentials.Certificate(_SERVICE_ACCOUNT_PATH)
    firebase_admin.initialize_app(cred, {"databaseURL": _firebase_db_url})
    firebase_db.reference("/_ping").get()
    FIREBASE_ENABLED = True
    print(f"[firebase] ✓ متصل بنجاح → {_firebase_db_url}")
except Exception as _fe:
    print(f"[firebase] ✗ فشل الاتصال: {_fe}")


# ─── Genre mapping ───────────────────────────────────────────────────────────

GENRES = {
    "kleeji":               "خليجي",
    "arabic":               "عربي",
    "turki":                "تركي",
    "farisi":               "فارسي",
    "anmi":                 "أنيمي",
    "foreign-movies":       "أفلام أجنبية",
    "Korean-movies":        "أفلام كورية",
    "Foreign-series":       "مسلسلات أجنبية",
    "Korean-series":        "مسلسلات كورية",
    "asia-series":          "مسلسلات آسيوية",
    "ramadan-kleeji":       "رمضان خليجي",
    "ramadan-arabi":        "رمضان عربي",
    "ramadan-kleeji-2024":  "رمضان خليجي 2024",
    "ramadan-arabi-2024":   "رمضان عربي 2024",
    "ramadan-arabi-2025":   "رمضان عربي 2025",
    "ramadan-kleeji-2025":  "رمضان خليجي 2025",
    "ramadan-kleeji-2026":  "رمضان خليجي 2026",
    "ramadan-arabi-2026":   "رمضان عربي 2026",
}


def _safe_key(text):
    """Firebase keys cannot contain . # $ [ ] /"""
    for ch in ".#$[]/ ":
        text = text.replace(ch, "_")
    return text


def firebase_series_episodes(series_title, genre=None):
    """Return set of episode keys already in Firebase for this series, or empty set."""
    if not FIREBASE_ENABLED:
        return set()
    try:
        series_key = _safe_key(series_title)
        if genre:
            genre_key = _safe_key(genre)
            path = f"genres/{genre_key}/{series_key}/episodes"
        else:
            path = f"content/{series_key}/episodes"
        result = firebase_db.reference(path).get()
        return set(result.keys()) if result else set()
    except Exception:
        return set()


def firebase_sync_episode(series_title, ep_label, vid_url, genre=None):
    """Save a single episode video URL to Firebase, organised by genre."""
    if not FIREBASE_ENABLED:
        return
    try:
        series_key = _safe_key(series_title)
        ep_key     = _safe_key(ep_label)
        if genre:
            genre_key = _safe_key(genre)
            path = f"genres/{genre_key}/{series_key}/episodes/{ep_key}"
        else:
            path = f"content/{series_key}/episodes/{ep_key}"
        firebase_db.reference(path).set(vid_url)
    except Exception as e:
        print(f"[firebase] sync_episode error: {e}")


def firebase_sync_all_episodes(series_title, episodes_dict, genre=None):
    """Upload ALL episodes for a series in a SINGLE Firebase write (batch update).
    episodes_dict = {label: vid_url}
    """
    if not FIREBASE_ENABLED or not episodes_dict:
        return
    try:
        series_key = _safe_key(series_title)
        if genre:
            genre_key = _safe_key(genre)
            path = f"genres/{genre_key}/{series_key}/episodes"
        else:
            path = f"content/{series_key}/episodes"
        data = {_safe_key(label): url for label, url in episodes_dict.items()}
        firebase_db.reference(path).update(data)
        print(f"[firebase] episodes ✓ {series_title} → {len(data)} حلقة")
    except Exception as e:
        print(f"[firebase] sync_all_episodes error: {e}")


def firebase_sync_series_info(series_title, series_url="", poster="",
                               description="", genres=None):
    """Save series metadata to Firebase under every genre it belongs to."""
    if not FIREBASE_ENABLED:
        return
    try:
        series_key = _safe_key(series_title)
        data = {
            "title":   series_title,
            "updated": int(time.time()),
        }
        if series_url:
            data["series_url"] = series_url
        if poster:
            data["poster"] = poster
        if description:
            data["description"] = description

        if genres:
            data["genres"] = genres
            for g in genres:
                genre_key = _safe_key(g)
                firebase_db.reference(
                    f"genres/{genre_key}/{series_key}/info"
                ).update(data)
            print(f"[firebase] info ✓ {series_title} → {genres}")
        else:
            firebase_db.reference(f"content/{series_key}/info").update(data)
            print(f"[firebase] info ✓ {series_title} → content")
    except Exception as e:
        print(f"[firebase] sync_series_info error: {e}")

DB_PATH = "cache.db"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ar,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ─── Database ────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            ep_url   TEXT PRIMARY KEY,
            vid_url  TEXT,
            status   TEXT DEFAULT 'pending',
            updated  INTEGER DEFAULT 0
        )
    """)
    con.commit()
    con.close()


def db_get(ep_url):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT vid_url, status FROM cache WHERE ep_url = ?", (ep_url,)
    ).fetchone()
    con.close()
    return row


def db_set(ep_url, vid_url, status):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT OR REPLACE INTO cache (ep_url, vid_url, status, updated) VALUES (?,?,?,?)",
        (ep_url, vid_url, status, int(time.time())),
    )
    con.commit()
    con.close()


def db_set_pending(ep_url):
    con = sqlite3.connect(DB_PATH)
    existing = con.execute(
        "SELECT status FROM cache WHERE ep_url = ?", (ep_url,)
    ).fetchone()
    if not existing:
        con.execute(
            "INSERT INTO cache (ep_url, vid_url, status, updated) VALUES (?,?,?,?)",
            (ep_url, None, "pending", int(time.time())),
        )
        con.commit()
    con.close()


def db_bulk_status(ep_urls):
    if not ep_urls:
        return {}
    con = sqlite3.connect(DB_PATH)
    placeholders = ",".join("?" * len(ep_urls))
    rows = con.execute(
        f"SELECT ep_url, status FROM cache WHERE ep_url IN ({placeholders})",
        ep_urls,
    ).fetchall()
    con.close()
    return {r[0]: r[1] for r in rows}


# ─── Fast extraction (no Selenium) ───────────────────────────────────────────

def fast_extract(ep_url):
    """Extract video URL directly from the page HTML."""
    try:
        r = http_requests.get(ep_url, headers=HEADERS, timeout=15)
        html = r.text

        # 1. <source type="video/..."> — CDN may serve video with .jpg extension
        #    (bytes are MP4 even though URL ends in .jpg)
        video_sources = re.findall(
            r'<source[^>]+src="(https?://[^"]+)"[^>]*type=["\']video[^"\']*["\']', html
        )
        video_sources += re.findall(
            r'<source[^>]*type=["\']video[^"\']*["\'][^>]*src="(https?://[^"]+)"', html
        )
        # Filter out obvious images (poster/thumbnail paths without a CDN match)
        video_sources = [
            s for s in video_sources
            if "thumb" not in s and "uploads/video_thumb" not in s
        ]
        if video_sources:
            return video_sources[0]

        # 2. Direct .mp4/.m3u8 URLs in <source> or anywhere in HTML
        sources = re.findall(r'<source[^>]+src="(https?://[^"]+\.(?:mp4|m3u8)[^"]*)"', html)
        if sources:
            return sources[0]
        streams = re.findall(r'(https?://[^\s"\'<>!\\]+\.(?:m3u8|mp4)[^\s"\'<>!\\]*)', html)
        streams = [s for s in streams if "thumb" not in s and "poster" not in s]
        if streams:
            return streams[0]
    except Exception as e:
        print(f"[extract] error for {ep_url}: {e}")
    return None


def extract_and_cache(ep_url):
    """Extract and cache a single episode URL (called from background thread)."""
    row = db_get(ep_url)
    if row and row[0] and row[1] == "ready":
        return row[0]
    db_set(ep_url, None, "loading")
    vid = fast_extract(ep_url)
    if vid:
        db_set(ep_url, vid, "ready")
        print(f"[cache] ✓ {ep_url} -> {vid[:60]}")
    else:
        db_set(ep_url, None, "failed")
        print(f"[cache] ✗ {ep_url}")
    return vid


SERIES_DIR = "series_files"


def _safe_fname(name):
    for ch in "/\\ :*?\"<>|":
        name = name.replace(ch, "-")
    return name.strip()


def series_filepath(series_title, genre=None):
    """Return the file path for a series, optionally inside a genre sub-folder."""
    if genre:
        dir_path = os.path.join(SERIES_DIR, _safe_fname(genre))
    else:
        dir_path = SERIES_DIR
    os.makedirs(dir_path, exist_ok=True)
    return os.path.join(dir_path, f"{_safe_fname(series_title)}.txt")


def _parse_episodes_from_file(filepath):
    """Read a series file and return {label: vid_url}."""
    saved = {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("الحلقة") and ": http" in line:
                    parts = line.split(": ", 1)
                    if len(parts) == 2:
                        saved[parts[0].strip()] = parts[1].strip()
    except Exception:
        pass
    return saved


def read_series_file(series_title, genre=None):
    """Read existing file and return dict {label: vid_url}.
    Looks in genre sub-folder first, then falls back to flat SERIES_DIR."""
    if genre:
        fp = series_filepath(series_title, genre)
        if os.path.exists(fp):
            return _parse_episodes_from_file(fp)
    # fallback: flat directory (legacy files)
    fp_flat = os.path.join(SERIES_DIR, f"{_safe_fname(series_title)}.txt")
    if os.path.exists(fp_flat):
        return _parse_episodes_from_file(fp_flat)
    return {}


def write_series_file(series_title, episodes_with_vids, series_url="",
                      poster="", description="", genres=None):
    """Write all episodes to the series file inside the primary genre sub-folder."""
    primary_genre = genres[0] if genres else None
    fp = series_filepath(series_title, primary_genre)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(f"المسلسل: {series_title}\n")
        if series_url:
            f.write(f"الرابط: {series_url}\n")
        if poster:
            f.write(f"الصورة: {poster}\n")
        if description:
            f.write(f"القصة: {description}\n")
        if genres:
            f.write(f"التصنيف: {', '.join(genres)}\n")
        f.write("=" * 50 + "\n\n")
        for label, vid_url in episodes_with_vids:
            f.write(f"{label}: {vid_url}\n")
    print(f"[file] saved → {fp} ({len(episodes_with_vids)} حلقات)")


def read_series_url_from_file(filepath):
    """Extract the series page URL stored in the file header."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("الرابط:"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def extract_series_meta(html):
    """Return (poster_url, description) from series page HTML."""
    poster = ""
    desc = ""
    og_img = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    if not og_img:
        og_img = re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', html, re.IGNORECASE)
    if og_img:
        poster = og_img.group(1).strip()

    og_desc = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    if not og_desc:
        og_desc = re.search(r'<meta[^>]+content="([^"]+)"[^>]+name="description"', html, re.IGNORECASE)
    if og_desc:
        desc = og_desc.group(1).strip()

    return poster, desc


def extract_genres_from_page(html):
    """Extract genre list from a series page HTML.
    Series pages contain links like: href=".../genre/kleeji.html">خليجي<
    Returns a list of Arabic genre names found on the page.
    """
    raw = re.findall(
        r'href="https://b\.alooytv12\.xyz/genre/[^"]+">([^<]+)<',
        html,
        re.IGNORECASE,
    )
    seen = []
    for g in raw:
        g = g.strip()
        if g and g not in seen:
            seen.append(g)
    return seen


def background_cache_series(series_url, genres=None):
    """Fetch series page, only extract new episodes, update the genre-organised file.
    `genres` is a list of Arabic genre names determined by the caller from genre pages.
    """
    try:
        r = http_requests.get(series_url, headers=HEADERS, timeout=15)
        html = r.text

        title_match = re.search(r'<title>([^<]+)</title>', html)
        series_title = title_match.group(1).strip() if title_match else "unknown"
        poster, description = extract_series_meta(html)

        primary_genre = genres[0] if genres else None

        eps_raw = re.findall(
            r'href="(https://b\.alooytv12\.xyz/watch/[^\?"]+\?key=[^"]+)"[^>]*>\s*(Ep#\d+)\s*<',
            html,
        )
        seen = set()
        episodes = []
        for url, label in eps_raw:
            if url not in seen:
                seen.add(url)
                num = label.replace("Ep#", "")
                episodes.append((url, f"الحلقة {num}"))

        already_saved = read_series_file(series_title, primary_genre)
        new_count = sum(1 for _, lbl in episodes if lbl not in already_saved)
        genre_label = primary_genre or "بدون تصنيف"
        print(f"[precache] [{genre_label}] {series_title}: {len(episodes)} حلقات، {new_count} جديدة")

        firebase_sync_series_info(series_title, series_url=series_url,
                                  poster=poster, description=description, genres=genres)

        episodes_with_vids = []
        for ep_url, ep_label in episodes:
            if ep_label in already_saved:
                vid = already_saved[ep_label]
                episodes_with_vids.append((ep_label, vid))
                continue

            row = db_get(ep_url)
            if row and row[0] and row[1] == "ready":
                vid = row[0]
            else:
                vid = extract_and_cache(ep_url)

            if vid:
                episodes_with_vids.append((ep_label, vid))
                print(f"[new] {ep_label} → {vid[:60]}")

        if episodes_with_vids:
            # رفع جميع الحلقات إلى Firebase دفعةً واحدة مباشرةً من بيانات الموقع
            firebase_sync_all_episodes(
                series_title,
                {label: vid for label, vid in episodes_with_vids},
                primary_genre,
            )
            write_series_file(series_title, episodes_with_vids, series_url=series_url,
                              poster=poster, description=description, genres=genres)
    except Exception as e:
        print(f"[precache] error: {e}")


# ─── Search helpers ───────────────────────────────────────────────────────────




def parse_series_page(html):
    """Parse all episodes from a series page HTML."""
    eps_raw = re.findall(
        r'href="(https://b\.alooytv12\.xyz/watch/[^\?"]+\?key=[^"]+)"[^>]*>\s*(Ep#\d+)\s*<',
        html,
    )
    seen = set()
    episodes = []
    for url, label in eps_raw:
        if url not in seen:
            seen.add(url)
            num = label.replace("Ep#", "")
            episodes.append({"label": f"الحلقة {num}", "url": url})
    return episodes


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/watch")
def watch_page():
    import html as _html
    ep_url  = request.args.get("ep", "").strip()
    vid_url = request.args.get("v", "").strip()
    src     = request.args.get("src", "alooytv").strip()
    title   = request.args.get("t", "مشاهدة الفيديو").strip()

    # Re-extract video URL fresh from the episode page (prevents stale/expired CDN links)
    if ep_url:
        if src == "noorplay":
            extracted, status_str = noorplay_get_video_url(ep_url)
            if status_str == "login_required":
                return f"""<!DOCTYPE html>
<html dir="rtl" lang="ar"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>تسجيل الدخول مطلوب</title>
<style>body{{background:#0f0f0f;color:#fff;font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;}}
.box{{background:#1a1a2e;border:1px solid #1a6abf;border-radius:12px;padding:32px;max-width:400px;text-align:center;}}
h2{{color:#1a6abf;margin-bottom:12px;}}p{{color:#aaa;font-size:14px;}}</style></head>
<body><div class="box"><h2>🔐 يرجى تسجيل الدخول إلى NoorPlay</h2>
<p>هذا المحتوى يتطلب تسجيل الدخول إلى NoorPlay لمشاهدته.</p>
<p style="margin-top:16px;"><a href="/" style="color:#7ab3f0;">العودة إلى الصفحة الرئيسية وتسجيل الدخول</a></p></div></body></html>"""
            elif extracted:
                vid_url = extracted
        else:
            # alooytv: check cache first, else extract fresh
            row = db_get(ep_url)
            if row and row[0] and row[1] == "ready":
                vid_url = row[0]
            else:
                vid_url = fast_extract(ep_url)
                if vid_url:
                    db_set(ep_url, vid_url, "ready")

    if not vid_url:
        return "تعذّر استخراج رابط الفيديو", 404

    safe_title = _html.escape(title)
    _tok = _sign(vid_url)
    proxy_url = _html.escape("/proxy?token=" + urllib.parse.quote(_tok, safe=""))
    _is_hls = "m3u8" in vid_url.lower()
    _hls_js = "true" if _is_hls else "false"

    return f"""<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{safe_title}</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.8/dist/hls.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #000; color: #fff; font-family: Arial, sans-serif; height: 100vh; display: flex; flex-direction: column; }}
  .header {{ background: #111; padding: 10px 16px; display: flex; align-items: center; gap: 12px; border-bottom: 1px solid #222; flex-shrink: 0; }}
  .header h1 {{ font-size: 15px; color: #ddd; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .header .logo {{ color: #e50914; font-weight: bold; font-size: 14px; white-space: nowrap; }}
  .player-wrap {{ flex: 1; display: flex; align-items: center; justify-content: center; background: #000; padding: 8px; }}
  video {{ width: 100%; max-height: 100%; border-radius: 6px; }}
  .err {{ color: #f44336; text-align: center; padding: 20px; font-size: 14px; display: none; }}
  .copy-btn {{ background: #333; color: #aaa; border: none; padding: 6px 14px; border-radius: 5px; cursor: pointer; font-size: 12px; white-space: nowrap; }}
  .copy-btn:hover {{ background: #444; }}
</style>
</head>
<body>
<div class="header">
  <span class="logo">🎬 بحث المسلسلات</span>
  <h1 title="{safe_title}">{safe_title}</h1>
  <button class="copy-btn" onclick="navigator.clipboard.writeText(location.href).then(()=>this.textContent='✅ تم النسخ!')">🔗 نسخ الرابط</button>
</div>
<div class="player-wrap">
  <video id="vp" controls autoplay playsinline></video>
</div>
<div style="display:flex;justify-content:center;padding:8px;flex-shrink:0;background:#000;">
  <a href="{proxy_url}" download target="_blank"
     style="display:inline-flex;align-items:center;gap:6px;background:#1a6a2f;color:#fff;text-decoration:none;padding:10px 24px;border-radius:8px;font-size:14px;font-family:Arial,sans-serif;">
    ⬇️ تحميل الفيديو
  </a>
</div>
<div class="err" id="err">⚠️ تعذّر تشغيل الفيديو في المتصفح.</div>
<script>
(function() {{
  var src = "{proxy_url}";
  var isHLS = {_hls_js};
  var vid = document.getElementById('vp');
  var errEl = document.getElementById('err');
  function showErr() {{ errEl.style.display = 'block'; }}
  if (isHLS) {{
    if (typeof Hls !== 'undefined' && Hls.isSupported()) {{
      var hls = new Hls({{ enableWorker: false }});
      hls.loadSource(src);
      hls.attachMedia(vid);
      hls.on(Hls.Events.MANIFEST_PARSED, function() {{ vid.play().catch(function(){{}}); }});
      hls.on(Hls.Events.ERROR, function(ev, d) {{ if (d.fatal) showErr(); }});
    }} else if (vid.canPlayType('application/vnd.apple.mpegurl')) {{
      vid.src = src;
      vid.addEventListener('error', showErr, true);
    }} else {{ showErr(); }}
  }} else {{
    vid.src = src;
    vid.addEventListener('error', showErr, true);
  }}
}})();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return """<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>بحث المسلسلات</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.8/dist/hls.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: Arial, sans-serif; background: #0f0f0f; color: #fff; padding: 16px; }
  h1 { color: #e50914; text-align: center; margin-bottom: 16px; font-size: 22px; }
  .search-bar { display: flex; gap: 8px; margin-bottom: 20px; }
  .search-bar input { flex: 1; padding: 12px; font-size: 16px; border-radius: 8px; border: none; }
  .search-bar button { background: #e50914; color: #fff; border: none; padding: 12px 20px; font-size: 16px; border-radius: 8px; cursor: pointer; white-space: nowrap; }
  .search-bar button:hover { background: #b00610; }
  .results-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; }
  .card { background: #1a1a1a; border-radius: 10px; overflow: hidden; cursor: pointer; transition: transform .2s; position: relative; }
  .card:hover { transform: scale(1.04); }
  .card img { width: 100%; height: 200px; object-fit: cover; display: block; background: #333; }
  .card .card-title { padding: 8px; font-size: 12px; color: #ddd; text-align: center; line-height: 1.4; }
  .card .source-badge { position: absolute; top: 6px; right: 6px; font-size: 10px; font-weight: bold; padding: 2px 6px; border-radius: 4px; }
  .loading { color: #aaa; text-align: center; padding: 30px; font-size: 16px; }
  .status { padding: 12px; border-radius: 8px; margin-bottom: 12px; text-align: center; }
  .success { background: #1a3a1a; color: #4caf50; }
  .error-box { background: #3a1a1a; color: #f44336; padding: 14px; border-radius: 8px; margin-top: 10px; text-align: center; }
  video { width: 100%; border-radius: 10px; margin-top: 12px; background: #000; }
  .btn-row { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
  .btn-row button { flex: 1; padding: 10px; border: none; color: #fff; border-radius: 6px; cursor: pointer; font-size: 14px; min-width: 120px; }
  .back-btn { background: #333; border: none; color: #aaa; padding: 8px 16px; border-radius: 6px; cursor: pointer; margin-bottom: 14px; font-size: 14px; }
  .series-title { font-size: 18px; color: #e50914; margin-bottom: 14px; text-align: center; }
  .ep-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(90px, 1fr)); gap: 8px; }
  .ep-btn {
    border: 1px solid #333; color: #ddd; padding: 12px 6px;
    border-radius: 8px; cursor: pointer; font-size: 13px; text-align: center;
    transition: background .2s; position: relative; background: #1a1a1a;
  }
  .ep-btn:hover { background: #e50914; color: #fff; border-color: #e50914; }
  .ep-btn.ready { background: #1a3a1a; border-color: #4caf50; color: #4caf50; }
  .ep-btn.ready:hover { background: #e50914; color: #fff; border-color: #e50914; }
  .ep-btn.loading { border-color: #888; color: #888; cursor: wait; }
  .ep-btn.failed { border-color: #f44336; color: #f44336; }
  .ep-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-left: 4px; vertical-align: middle; }
  .dot-ready { background: #4caf50; }
  .dot-loading { background: #888; animation: pulse 1s infinite; }
  .dot-failed { background: #f44336; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  #player-view { display: none; }
  #episodes-view { display: none; }
</style>
</head>
<body>
<h1>🎬 بحث المسلسلات</h1>

<!-- Admin Download Panel -->
<div id="adminBox" style="background:#111;border:1px solid #333;border-radius:10px;padding:14px;margin-bottom:18px;display:none;">
  <h3 style="color:#e5b800;font-size:14px;margin-bottom:10px;">⬇️ تحميل خاص (للمشرف فقط)</h3>
  <div style="display:flex;flex-direction:column;gap:8px;">
    <input id="dlUrl" type="text" placeholder="رابط الفيديو (m3u8 أو mp4)" dir="ltr"
      style="padding:9px;border-radius:6px;border:1px solid #333;background:#1a1a1a;color:#fff;font-size:13px;width:100%;">
    <div style="display:flex;gap:8px;align-items:center;">
      <input id="dlWatermark" type="text" value="شاهد الحلقه كامله على تلجرام الرابط في الوصف" placeholder="نص يُكتب على الفيديو (اختياري)"
        style="flex:1;padding:9px;border-radius:6px;border:1px solid #333;background:#1a1a1a;color:#fff;font-size:13px;">
      <input id="dlWmDur" type="number" value="10" min="1" max="3600" title="مدة ظهور النص بالثواني"
        style="width:70px;padding:9px;border-radius:6px;border:1px solid #555;background:#1a1a1a;color:#e5b800;font-size:13px;text-align:center;" placeholder="ثانية">
      <span style="color:#888;font-size:11px;white-space:nowrap;">ثانية</span>
    </div>
    <input id="dlCuts" type="text" placeholder="حذف إعلانات: مثلاً  5:30-7:00, 25:00-26:30  (اختياري)" dir="ltr"
      style="padding:9px;border-radius:6px;border:1px solid #333;background:#1a1a1a;color:#fff;font-size:12px;width:100%;">
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <input id="dlMins" type="number" value="30" min="1" max="240" placeholder="دقائق"
        style="width:90px;padding:9px;border-radius:6px;border:1px solid #333;background:#1a1a1a;color:#fff;font-size:13px;">
      <input id="dlKey" type="password" placeholder="المفتاح السري" dir="ltr"
        style="flex:1;min-width:120px;padding:9px;border-radius:6px;border:1px solid #333;background:#1a1a1a;color:#fff;font-size:13px;">
      <button onclick="startAdminDl()"
        style="background:#e5b800;color:#000;border:none;padding:9px 20px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:bold;white-space:nowrap;">
        تحميل
      </button>
    </div>
    <div id="dlMsg" style="font-size:12px;color:#aaa;"></div>
  </div>
</div>
<div style="text-align:left;margin-bottom:8px;">
  <button onclick="document.getElementById('adminBox').style.display=document.getElementById('adminBox').style.display==='none'?'block':'none';"
    style="background:none;border:none;color:#333;font-size:11px;cursor:pointer;">🔑</button>
</div>

<div id="search-view">
  <div id="noorStatusBar" class="noor-status-bar" style="display:none;">
    <div id="noorDot" class="dot dot-off"></div>
    <span id="noorStatusText">NoorPlay: جاري التحقق...</span>
    <button id="noorActionBtn" onclick="toggleNoorLogin()">تسجيل الدخول</button>
  </div>

  <div id="noorLoginPanel" class="noor-inline-login" style="display:none;">
    <h4>🔐 تسجيل الدخول إلى NoorPlay</h4>
    <form onsubmit="doMainNoorLogin();return false;" class="fields">
      <input type="email" id="noorEmailMain" placeholder="البريد الإلكتروني" dir="ltr" autocomplete="email">
      <input type="password" id="noorPassMain" placeholder="كلمة المرور" dir="ltr" autocomplete="current-password">
      <button type="submit">دخول</button>
    </form>
    <div id="noorLoginMsg" class="hint"></div>
    <div class="hint">سجّل مجاناً على noorplay.com ثم ادخل هنا — يُحفظ تلقائياً ولن تحتاج لإعادته</div>
  </div>

  <div class="search-bar">
    <input type="text" id="searchInput" placeholder="ابحث عن مسلسل..." />
    <button onclick="doSearch()">🔍 بحث</button>
  </div>
  <div id="searchResults"></div>
</div>

<div id="episodes-view">
  <button class="back-btn" onclick="showSearch()">← رجوع للبحث</button>
  <div id="seriesMeta" style="display:none;margin-bottom:16px;">
    <div style="display:flex;gap:14px;align-items:flex-start;">
      <img id="seriesPoster" src="" alt="" style="width:110px;min-width:110px;height:160px;object-fit:cover;border-radius:8px;background:#222;">
      <div style="flex:1;min-width:0;">
        <div class="series-title" id="seriesTitle" style="margin-bottom:8px;text-align:right;"></div>
        <div id="seriesDesc" style="color:#aaa;font-size:13px;line-height:1.7;text-align:right;direction:rtl;"></div>
      </div>
    </div>
  </div>
  <div class="series-title" id="seriesTitleFallback" style="display:none;"></div>
  <div class="loading" id="epLoading" style="display:none;">⏳ جاري تحميل الحلقات...</div>
  <div class="ep-grid" id="epGrid"></div>
</div>

<div id="player-view">
  <button class="back-btn" onclick="showEpisodes()">← رجوع للحلقات</button>
  <div id="playerContent"></div>
</div>

<script>
function startAdminDl() {
  const url = document.getElementById('dlUrl').value.trim();
  const mins = document.getElementById('dlMins').value || '30';
  const key = document.getElementById('dlKey').value.trim();
  const wm    = document.getElementById('dlWatermark').value.trim();
  const wmDur = document.getElementById('dlWmDur').value || '10';
  const msg = document.getElementById('dlMsg');
  if (!url) { msg.textContent = '⚠️ أدخل رابط الفيديو'; return; }
  if (!key) { msg.textContent = '⚠️ أدخل المفتاح السري'; return; }
  msg.textContent = '⏳ جاري بدء التحميل...';
  const cuts = document.getElementById('dlCuts').value.trim();
  let link = `/api/download?url=${encodeURIComponent(url)}&m=${encodeURIComponent(mins)}&k=${encodeURIComponent(key)}&t=video`;
  if (wm) link += `&wm=${encodeURIComponent(wm)}&wmd=${encodeURIComponent(wmDur)}`;
  if (cuts) link += `&cuts=${encodeURIComponent(cuts)}`;
  const a = document.createElement('a');
  a.href = link;
  a.download = 'video.mp4';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => { msg.textContent = '✅ بدأ التحميل — إذا لم يبدأ تحقق من المفتاح'; }, 1000);
}

let currentVideoUrl = '';
let currentSeriesUrl = '';
let currentSeriesTitle = '';
let currentSource = 'alooytv';
let epList = [];
let statusPollTimer = null;
let noorLoggedIn = false;

// ── NoorPlay status bar ──────────────────────────────────────
async function checkNoorStatus() {
  try {
    const res = await fetch('/api/noor-status');
    const data = await res.json();
    noorLoggedIn = data.logged_in;
    const bar = document.getElementById('noorStatusBar');
    const dot = document.getElementById('noorDot');
    const txt = document.getElementById('noorStatusText');
    const btn = document.getElementById('noorActionBtn');
    bar.style.display = 'flex';
    if (noorLoggedIn) {
      dot.className = 'dot dot-on';
      txt.textContent = 'NoorPlay: متصل ✓ يمكنك مشاهدة جميع المسلسلات';
      btn.textContent = 'تسجيل الخروج';
      btn.className = 'logout-btn';
      document.getElementById('noorLoginPanel').style.display = 'none';
    } else {
      dot.className = 'dot dot-off';
      txt.textContent = 'NoorPlay: غير متصل — سجّل الدخول لمشاهدة مسلسلات NoorPlay';
      btn.textContent = 'تسجيل الدخول';
      btn.className = '';
    }
  } catch(e) {}
}

function toggleNoorLogin() {
  if (noorLoggedIn) {
    doNoorLogout();
  } else {
    const panel = document.getElementById('noorLoginPanel');
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
  }
}

async function doMainNoorLogin() {
  const email = document.getElementById('noorEmailMain').value.trim();
  const pass = document.getElementById('noorPassMain').value.trim();
  const msg = document.getElementById('noorLoginMsg');
  if (!email || !pass) { msg.textContent = 'أدخل البريد وكلمة المرور'; msg.style.color='#f44336'; return; }
  msg.textContent = '⏳ جاري تسجيل الدخول...'; msg.style.color='#aaa';
  try {
    const res = await fetch('/api/noor-login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, password: pass})
    });
    const data = await res.json();
    if (data.success) {
      msg.textContent = '✅ تم الدخول بنجاح! جلستك محفوظة.'; msg.style.color='#4caf50';
      noorLoggedIn = true;
      setTimeout(() => {
        document.getElementById('noorLoginPanel').style.display = 'none';
        checkNoorStatus();
      }, 1500);
    } else {
      msg.textContent = '❌ فشل تسجيل الدخول — تحقق من البيانات'; msg.style.color='#f44336';
    }
  } catch(e) {
    msg.textContent = '❌ خطأ في الاتصال'; msg.style.color='#f44336';
  }
}

async function doNoorLogout() {
  try {
    await fetch('/api/noor-logout', {method:'POST'});
    noorLoggedIn = false;
    checkNoorStatus();
  } catch(e) {}
}

// Check NoorPlay status when page loads
window.addEventListener('load', checkNoorStatus);
// ─────────────────────────────────────────────────────────────

function showSearch() {
  clearInterval(statusPollTimer);
  document.getElementById('search-view').style.display = 'block';
  document.getElementById('episodes-view').style.display = 'none';
  document.getElementById('player-view').style.display = 'none';
}

function showEpisodes() {
  document.getElementById('search-view').style.display = 'none';
  document.getElementById('episodes-view').style.display = 'block';
  document.getElementById('player-view').style.display = 'none';
  startStatusPoll();
}

async function doSearch() {
  const q = document.getElementById('searchInput').value.trim();
  if (!q) return;
  document.getElementById('searchResults').innerHTML = '<div class="loading">⏳ جاري البحث...</div>';
  try {
    const res = await fetch('/api/search?q=' + encodeURIComponent(q));
    const data = await res.json();
    if (!data.results || data.results.length === 0) {
      document.getElementById('searchResults').innerHTML = '<div class="loading">لا توجد نتائج</div>';
      return;
    }
    let html = '<div class="results-grid">';
    data.results.forEach(item => {
      const poster = item.poster || '';
      const isNoor = item.source === 'noorplay';
      const cardClass = isNoor ? 'card card-noorplay' : 'card';
      const badge = isNoor
        ? '<span class="source-badge badge-noorplay">NoorPlay</span>'
        : '';
      const onclick = isNoor
        ? `openNoorSeries('${encodeURIComponent(item.url)}','${item.title.replace(/'/g,"\\'")}');`
        : `openSeries('${encodeURIComponent(item.url)}','${item.title.replace(/'/g,"\\'")}')`;

      html += `<div class="${cardClass}" onclick="${onclick}">
        ${badge}
        ${poster ? `<img src="${poster}" onerror="this.style.display='none'" loading="lazy">` : '<div style="height:200px;background:#333;display:flex;align-items:center;justify-content:center;color:#666;font-size:40px">🎬</div>'}
        <div class="card-title">${item.title}</div>
      </div>`;
    });
    html += '</div>';
    document.getElementById('searchResults').innerHTML = html;
  } catch(e) {
    document.getElementById('searchResults').innerHTML = '<div class="loading">❌ خطأ في البحث</div>';
  }
}

async function openSeries(encodedUrl, title) {
  clearInterval(statusPollTimer);
  currentSeriesUrl = decodeURIComponent(encodedUrl);
  currentSeriesTitle = title;
  epList = [];
  document.getElementById('seriesTitle').textContent = title;
  document.getElementById('seriesTitleFallback').textContent = '';
  document.getElementById('seriesMeta').style.display = 'none';
  document.getElementById('seriesTitleFallback').style.display = 'none';
  document.getElementById('epGrid').innerHTML = '';
  document.getElementById('epLoading').style.display = 'block';
  showEpisodes();

  try {
    const res = await fetch('/api/episodes?url=' + encodeURIComponent(currentSeriesUrl));
    const data = await res.json();
    document.getElementById('epLoading').style.display = 'none';
    if (!data.episodes || data.episodes.length === 0) {
      document.getElementById('epGrid').innerHTML = '<div class="loading">لا توجد حلقات</div>';
      return;
    }

    // Show poster + description if available
    const metaEl = document.getElementById('seriesMeta');
    const posterEl = document.getElementById('seriesPoster');
    const descEl = document.getElementById('seriesDesc');
    const fallbackTitle = document.getElementById('seriesTitleFallback');
    if (data.poster || data.description) {
      posterEl.src = data.poster || '';
      posterEl.style.display = data.poster ? '' : 'none';
      document.getElementById('seriesTitle').textContent = data.title || currentSeriesTitle;
      descEl.textContent = data.description || '';
      metaEl.style.display = '';
      fallbackTitle.style.display = 'none';
    } else {
      metaEl.style.display = 'none';
      fallbackTitle.style.display = '';
      fallbackTitle.textContent = currentSeriesTitle;
    }

    epList = data.episodes;
    renderEpisodes(data.statuses || {});

    fetch('/api/precache?url=' + encodeURIComponent(currentSeriesUrl));
    startStatusPoll();
  } catch(e) {
    document.getElementById('epLoading').style.display = 'none';
    document.getElementById('epGrid').innerHTML = '<div class="loading">❌ خطأ في تحميل الحلقات</div>';
  }
}

function renderEpisodes(statuses) {
  let html = '';
  epList.forEach(ep => {
    const st = statuses[ep.url] || 'pending';
    let cls = '';
    let dot = '';
    if (st === 'ready') {
      cls = 'ready';
      dot = '<span class="ep-dot dot-ready"></span>';
    } else if (st === 'loading') {
      cls = 'loading';
      dot = '<span class="ep-dot dot-loading"></span>';
    } else if (st === 'failed') {
      cls = 'failed';
      dot = '<span class="ep-dot dot-failed"></span>';
    }
    html += `<div class="ep-btn ${cls}" data-url="${ep.url}" onclick="playEpisode('${encodeURIComponent(ep.url)}','${ep.label} - ${currentSeriesTitle.replace(/'/g,"\\'")}')">
      ${ep.label}${dot}
    </div>`;
  });
  document.getElementById('epGrid').innerHTML = html;
}

function updateEpisodeStatuses(statuses) {
  epList.forEach(ep => {
    const st = statuses[ep.url];
    if (!st) return;
    const btn = document.querySelector(`.ep-btn[data-url="${ep.url}"]`);
    if (!btn) return;
    btn.className = 'ep-btn' + (st === 'ready' ? ' ready' : st === 'loading' ? ' loading' : st === 'failed' ? ' failed' : '');
    const dot = st === 'ready' ? '<span class="ep-dot dot-ready"></span>'
              : st === 'loading' ? '<span class="ep-dot dot-loading"></span>'
              : st === 'failed' ? '<span class="ep-dot dot-failed"></span>' : '';
    const label = ep.label;
    btn.innerHTML = label + dot;
  });
}

function startStatusPoll() {
  clearInterval(statusPollTimer);
  statusPollTimer = setInterval(async () => {
    if (!epList.length) return;
    const urls = epList.map(e => e.url);
    try {
      const res = await fetch('/api/status', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({urls})
      });
      const statuses = await res.json();
      updateEpisodeStatuses(statuses);
      const allDone = urls.every(u => statuses[u] === 'ready' || statuses[u] === 'failed');
      if (allDone) clearInterval(statusPollTimer);
    } catch(e) {}
  }, 3000);
}

function attachPlayer(proxyUrl, errMsgId, isHLS) {
  const vid = document.getElementById('vp');
  if (!vid) return;

  function showErr() {
    const el = document.getElementById(errMsgId);
    if (el) el.style.display = 'block';
  }

  if (isHLS) {
    if (typeof Hls !== 'undefined' && Hls.isSupported()) {
      const hls = new Hls({ enableWorker: false });
      hls.loadSource(proxyUrl);
      hls.attachMedia(vid);
      hls.on(Hls.Events.MANIFEST_PARSED, () => vid.play().catch(()=>{}));
      hls.on(Hls.Events.ERROR, (ev, d) => { if (d.fatal) showErr(); });
    } else if (vid.canPlayType('application/vnd.apple.mpegurl')) {
      vid.src = proxyUrl;
      vid.addEventListener('error', showErr, true);
    } else {
      showErr();
    }
  } else {
    vid.src = proxyUrl;
    vid.addEventListener('error', showErr, true);
  }
}

async function playEpisode(encodedUrl, title) {
  const url = decodeURIComponent(encodedUrl);
  clearInterval(statusPollTimer);
  document.getElementById('search-view').style.display = 'none';
  document.getElementById('episodes-view').style.display = 'none';
  document.getElementById('player-view').style.display = 'block';

  const row = document.querySelector(`.ep-btn[data-url="${url}"]`);
  const isCached = row && row.classList.contains('ready');

  if (!isCached) {
    document.getElementById('playerContent').innerHTML =
      `<div class="status success">⏳ جاري استخراج رابط "${title}"...</div>`;
  }

  try {
    const res = await fetch('/api/grab?url=' + encodeURIComponent(url));
    const data = await res.json();
    if (data.status === 'Success') {
      currentVideoUrl = data.proxy_url;
      const shareLink = location.origin + '/watch?ep=' + encodeURIComponent(url) + '&t=' + encodeURIComponent(title);
      document.getElementById('playerContent').innerHTML = `
        <div class="status success">✅ ${title}</div>
        <video id="vp" controls autoplay playsinline style="width:100%;border-radius:8px;background:#000;max-height:70vh;"></video>
        <div id="vperr" style="display:none;" class="error-box">⚠️ تعذّر التشغيل</div>
        <div class="btn-row">
          <button onclick="copyShareLink('${shareLink.replace(/'/g,"\\'")}','share-msg-aloo')" style="background:#1a6a2f;">🔗 رابط المشاركة</button>
          <button onclick="navigator.clipboard.writeText(location.origin + currentVideoUrl).then(()=>alert('تم النسخ!'))" style="background:#333;">📋 نسخ رابط الفيديو</button>
          <button onclick="playEpisode('${encodeURIComponent(url)}','${title.replace(/'/g,"\\'")}');" style="background:#555;">🔄 إعادة المحاولة</button>
        </div>
        <div id="share-msg-aloo" style="display:none;text-align:center;color:#4caf50;font-size:13px;margin-top:8px;"></div>`;
      attachPlayer(data.proxy_url, 'vperr', data.is_hls || false);
    } else {
      document.getElementById('playerContent').innerHTML =
        `<div class="error-box">❌ فشل استخراج الرابط</div>`;
    }
  } catch(e) {
    document.getElementById('playerContent').innerHTML =
      `<div class="error-box">❌ خطأ في الاتصال</div>`;
  }
}

document.getElementById('searchInput').addEventListener('keypress', e => { if(e.key==='Enter') doSearch(); });

async function openNoorSeries(encodedUrl, title) {
  clearInterval(statusPollTimer);
  currentSeriesUrl = decodeURIComponent(encodedUrl);
  currentSeriesTitle = title;
  currentSource = 'noorplay';
  epList = [];
  document.getElementById('seriesTitle').textContent = title;
  document.getElementById('seriesMeta').style.display = 'none';
  document.getElementById('seriesTitleFallback').style.display = 'none';
  document.getElementById('epGrid').innerHTML = '';
  document.getElementById('epLoading').style.display = 'block';
  showEpisodes();

  try {
    const res = await fetch('/api/noor-episodes?url=' + encodeURIComponent(currentSeriesUrl));
    const data = await res.json();
    document.getElementById('epLoading').style.display = 'none';

    if (!data.episodes || data.episodes.length === 0) {
      document.getElementById('epGrid').innerHTML = '<div class="loading">لا توجد حلقات</div>';
      return;
    }

    const metaEl = document.getElementById('seriesMeta');
    const posterEl = document.getElementById('seriesPoster');
    const descEl = document.getElementById('seriesDesc');
    const fallbackTitle = document.getElementById('seriesTitleFallback');
    if (data.poster || data.description) {
      posterEl.src = data.poster || '';
      posterEl.style.display = data.poster ? '' : 'none';
      document.getElementById('seriesTitle').textContent = data.title || title;
      descEl.textContent = data.description || '';
      metaEl.style.display = '';
      fallbackTitle.style.display = 'none';
    } else {
      metaEl.style.display = 'none';
      fallbackTitle.style.display = '';
      fallbackTitle.textContent = title;
    }

    epList = data.episodes;
    let html = '';
    epList.forEach(ep => {
      html += `<div class="ep-btn noor" onclick="playNoorEpisode('${encodeURIComponent(ep.url)}','${ep.label.replace(/'/g,"\\'")} - ${title.replace(/'/g,"\\'")}')">
        ${ep.label}
      </div>`;
    });
    document.getElementById('epGrid').innerHTML = html;
  } catch(e) {
    document.getElementById('epLoading').style.display = 'none';
    document.getElementById('epGrid').innerHTML = '<div class="loading">❌ خطأ في تحميل الحلقات</div>';
  }
}

async function playNoorEpisode(encodedUrl, title) {
  const url = decodeURIComponent(encodedUrl);
  clearInterval(statusPollTimer);
  document.getElementById('search-view').style.display = 'none';
  document.getElementById('episodes-view').style.display = 'none';
  document.getElementById('player-view').style.display = 'block';
  document.getElementById('playerContent').innerHTML =
    `<div class="status success">⏳ جاري استخراج رابط "${title}"...</div>`;

  try {
    const res = await fetch('/api/noor-grab?url=' + encodeURIComponent(url));
    const data = await res.json();

    if (data.status === 'login_required') {
      document.getElementById('playerContent').innerHTML = `
        <div class="noor-login-box">
          <h3>🔐 يرجى تسجيل الدخول إلى NoorPlay لمشاهدة هذه الحلقة</h3>
          <input type="email" id="noorEmail" placeholder="البريد الإلكتروني" dir="ltr">
          <input type="password" id="noorPass" placeholder="كلمة المرور" dir="ltr">
          <button onclick="doNoorLogin('${encodeURIComponent(url)}','${title.replace(/'/g,"\\'")}')">تسجيل الدخول ومشاهدة</button>
          <div id="noorLoginStatus" style="margin-top:10px;text-align:center;color:#aaa;font-size:13px;"></div>
        </div>`;
    } else if (data.status === 'Success') {
      currentVideoUrl = data.proxy_url;
      const shareLink = location.origin + '/watch?ep=' + encodeURIComponent(url) + '&src=noorplay&t=' + encodeURIComponent(title);
      document.getElementById('playerContent').innerHTML = `
        <div class="status success">✅ ${title}</div>
        <video id="vp" controls autoplay playsinline style="width:100%;border-radius:8px;background:#000;max-height:70vh;"></video>
        <div id="vperr" style="display:none;" class="error-box">⚠️ تعذّر التشغيل</div>
        <div class="btn-row">
          <button onclick="copyShareLink('${shareLink.replace(/'/g,"\\'")}','share-msg-noor')" style="background:#1a6a2f;">🔗 رابط المشاركة</button>
          <button onclick="navigator.clipboard.writeText(location.origin + currentVideoUrl).then(()=>alert('تم النسخ!'))" style="background:#333;">📋 نسخ رابط الفيديو</button>
        </div>
        <div id="share-msg-noor" style="display:none;text-align:center;color:#4caf50;font-size:13px;margin-top:8px;"></div>`;
      attachPlayer(data.proxy_url, 'vperr', data.is_hls || false);
    } else {
      document.getElementById('playerContent').innerHTML =
        `<div class="error-box">❌ فشل استخراج الرابط</div>`;
    }
  } catch(e) {
    document.getElementById('playerContent').innerHTML =
      `<div class="error-box">❌ خطأ في الاتصال</div>`;
  }
}

function copyShareLink(link, msgId) {
  navigator.clipboard.writeText(link).then(() => {
    const el = document.getElementById(msgId);
    if (el) {
      el.style.display = 'block';
      el.textContent = '✅ تم نسخ رابط المشاركة! شاركه مع أي شخص لمشاهدة الفيديو مباشرة.';
      setTimeout(() => { el.style.display = 'none'; }, 4000);
    }
  }).catch(() => {
    prompt('انسخ الرابط يدوياً:', link);
  });
}

async function doNoorLogin(encodedUrl, title) {
  const email = document.getElementById('noorEmail').value.trim();
  const pass = document.getElementById('noorPass').value.trim();
  const statusEl = document.getElementById('noorLoginStatus');
  if (!email || !pass) { statusEl.textContent = 'يرجى إدخال البريد وكلمة المرور'; return; }
  statusEl.textContent = '⏳ جاري تسجيل الدخول...';
  try {
    const res = await fetch('/api/noor-login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, password: pass})
    });
    const data = await res.json();
    if (data.success) {
      noorLoggedIn = true;
      statusEl.textContent = '✅ تم تسجيل الدخول! جاري استخراج الرابط...';
      setTimeout(() => playNoorEpisode(encodedUrl, title), 500);
    } else {
      statusEl.textContent = '❌ فشل تسجيل الدخول — تحقق من البيانات';
    }
  } catch(e) {
    statusEl.textContent = '❌ خطأ في الاتصال';
  }
}
</script>
</body>
</html>"""


def _search_alooytv(query):
    """Search alooytv and return list of result dicts."""
    try:
        r = http_requests.get(
            f"https://b.alooytv12.xyz/search?q={query}", headers=HEADERS, timeout=15
        )
        html = r.text

        watch_links_raw = re.findall(
            r'href="(https://b\.alooytv12\.xyz/watch/[^"]+)"', html
        )
        seen_links = []
        seen_set = set()
        for l in watch_links_raw:
            if l not in seen_set:
                seen_set.add(l)
                seen_links.append(l)

        thumbs = re.findall(
            r'data-src="(https://b\.alooytv12\.xyz/uploads/video_thumb/[^"]+)"[^>]+alt="([^"]+)"',
            html,
        )

        results = []
        for i, (img, title) in enumerate(thumbs):
            url = seen_links[i] if i < len(seen_links) else ""
            if not url:
                continue
            results.append({"title": title.strip(), "url": url, "poster": img, "source": "alooytv"})
        return results[:20]
    except Exception as e:
        print(f"[alooytv] search error: {e}")
        return []


@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "No query"}), 400
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_aloo  = executor.submit(_search_alooytv, query)
            fut_noor  = executor.submit(noorplay_search, query)
            aloo_results = fut_aloo.result()
            noor_results = fut_noor.result()

        combined = aloo_results + noor_results
        return jsonify({"results": combined})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/episodes")
def episodes_route():
    series_url = request.args.get("url")
    if not series_url:
        return jsonify({"error": "No URL"}), 400
    try:
        r = http_requests.get(series_url, headers=HEADERS, timeout=15)
        html = r.text
        title_match = re.search(r'<title>([^<]+)</title>', html)
        series_title = title_match.group(1).strip() if title_match else ""
        poster, description = extract_series_meta(html)
        eps = parse_series_page(html)
        ep_urls = [e["url"] for e in eps]
        statuses = db_bulk_status(ep_urls)
        return jsonify({"title": series_title, "poster": poster, "description": description,
                        "episodes": eps, "statuses": statuses})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/noor-login", methods=["POST"])
def noor_login_route():
    data = request.get_json(force=True)
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()
    if not email or not password:
        return jsonify({"success": False, "error": "يرجى إدخال البريد الإلكتروني وكلمة المرور"}), 400
    success, resp = noorplay_login(email, password)
    return jsonify({"success": success, "data": resp})


@app.route("/api/noor-status")
def noor_status_route():
    return jsonify({
        "logged_in": _noor_logged_in,
        "session_ready": _noor_session_ready,
    })


@app.route("/api/noor-logout", methods=["POST"])
def noor_logout_route():
    global _noor_logged_in, _noor_session_ready
    try:
        _noor_session.cookies.clear()
        _noor_logged_in = False
        _noor_session_ready = False
        if os.path.exists(NOOR_COOKIES_PATH):
            os.remove(NOOR_COOKIES_PATH)
        print("[noorplay] logged out and cookies cleared")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/noor-episodes")
def noor_episodes_route():
    show_url = request.args.get("url")
    if not show_url:
        return jsonify({"error": "No URL"}), 400
    result = noorplay_get_episodes(show_url)
    return jsonify(result)


@app.route("/api/noor-grab")
def noor_grab_route():
    ep_url = request.args.get("url")
    if not ep_url:
        return jsonify({"error": "No URL"}), 400
    vid_url, status_str = noorplay_get_video_url(ep_url)
    if status_str == "login_required":
        return jsonify({"status": "login_required"})
    if vid_url:
        return jsonify({
            "status": "Success",
            "proxy_url": _make_proxy_url(vid_url),
            "is_hls": "m3u8" in vid_url.lower(),
            "type": status_str,
        })
    return jsonify({"status": "Failed", "message": "لم يتم العثور على رابط الفيديو"}), 404


@app.route("/api/precache")
def precache():
    series_url = request.args.get("url")
    if not series_url:
        return jsonify({"error": "No URL"}), 400
    # Normalise URL (strip query params) to match the watcher cache
    base_url = series_url.split("?")[0]
    genres = URL_GENRES_CACHE.get(base_url) or URL_GENRES_CACHE.get(series_url) or None
    t = threading.Thread(
        target=background_cache_series,
        args=(series_url,),
        kwargs={"genres": genres},
        daemon=True,
    )
    t.start()
    return jsonify({"status": "started", "genres": genres})


@app.route("/api/status", methods=["POST"])
def status():
    data = request.get_json(force=True)
    urls = data.get("urls", [])
    result = db_bulk_status(urls)
    return jsonify(result)


@app.route("/api/grab")
def grab():
    ep_url = request.args.get("url")
    if not ep_url:
        return jsonify({"error": "No URL"}), 400

    row = db_get(ep_url)
    if row and row[0] and row[1] == "ready":
        vid = row[0]
        return jsonify({
            "status": "Success",
            "proxy_url": _make_proxy_url(vid),
            "is_hls": "m3u8" in vid.lower(),
            "cached": True,
        })

    vid = extract_and_cache(ep_url)
    if vid:
        return jsonify({
            "status": "Success",
            "proxy_url": _make_proxy_url(vid),
            "is_hls": "m3u8" in vid.lower(),
            "cached": False,
        })
    return jsonify({"status": "Failed", "message": "لم يتم العثور على رابط الفيديو"}), 404


@app.route("/api/firebase-status")
def firebase_status():
    if not FIREBASE_ENABLED:
        return jsonify({
            "connected": False,
            "message": "قاعدة البيانات غير متصلة — يرجى إنشاء Realtime Database من Firebase Console",
            "project": "alafreky-20e4c"
        })
    try:
        firebase_db.reference("/_ping").set({"alive": True, "time": int(time.time())})
        return jsonify({
            "connected": True,
            "database_url": _firebase_db_url,
            "message": "الاتصال يعمل بنجاح ✓"
        })
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)})


@app.route("/proxy")
def proxy():
    token = request.args.get("token")
    if not token:
        return "غير مصرح", 403
    video_url = _verify(token)
    if not video_url:
        return "رابط منتهي الصلاحية أو غير صالح", 403
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://b.alooytv12.xyz/",
            "Origin": "https://b.alooytv12.xyz",
        }
        range_header = request.headers.get("Range")
        if range_header:
            headers["Range"] = range_header

        r = http_requests.get(video_url, headers=headers, stream=True, timeout=(10, 300))

        if r.status_code == 503:
            return Response("CDN blocked", status=503)

        ct = r.headers.get("Content-Type", "")
        if not ct or ct.strip() == "":
            if ".m3u8" in video_url or "m3u8" in video_url:
                ct = "application/vnd.apple.mpegurl"
            elif ".ts" in video_url:
                ct = "video/mp2t"
            else:
                ct = "video/mp4"

        is_m3u8 = "mpegurl" in ct.lower() or ".m3u8" in video_url or "m3u8" in video_url
        if is_m3u8:
            raw = b"".join(r.iter_content(chunk_size=1024 * 64))
            rewritten = _rewrite_m3u8(raw, video_url)
            return Response(rewritten, status=r.status_code, headers={
                "Content-Type": "application/vnd.apple.mpegurl",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "Range",
                "Access-Control-Expose-Headers": "Content-Length, Content-Range",
            })

        response_headers = {
            "Content-Type": ct,
            "Accept-Ranges": "bytes",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Range",
            "Access-Control-Expose-Headers": "Content-Length, Content-Range",
        }
        if "Content-Range" in r.headers:
            response_headers["Content-Range"] = r.headers["Content-Range"]
        if "Content-Length" in r.headers:
            response_headers["Content-Length"] = r.headers["Content-Length"]

        def generate():
            try:
                for chunk in r.iter_content(chunk_size=1024 * 64):
                    if chunk:
                        yield chunk
            except Exception:
                pass

        return Response(generate(), status=r.status_code, headers=response_headers)
    except Exception as e:
        return str(e), 500


ADMIN_KEY = os.environ.get("ADMIN_KEY", "admin1234")
_SECRET = os.environ.get("SECRET_KEY", "xK9pQ2mLvR7nB3wS")
_TOKEN_TTL = 4 * 3600


def _sign(vid_url: str) -> str:
    exp = int(time.time()) + _TOKEN_TTL
    raw = f"{vid_url}|||{exp}"
    payload = _b64.urlsafe_b64encode(raw.encode()).decode()
    sig = _hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify(token: str):
    try:
        payload, sig = token.rsplit(".", 1)
        expected = _hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(sig, expected):
            return None
        decoded = _b64.urlsafe_b64decode(payload.encode()).decode()
        vid_url, exp_str = decoded.rsplit("|||", 1)
        if int(time.time()) > int(exp_str):
            return None
        return vid_url
    except Exception:
        return None


def _make_proxy_url(vid_url: str) -> str:
    tok = _sign(vid_url)
    return "/proxy?token=" + urllib.parse.quote(tok, safe="")


def _rewrite_m3u8(content: bytes, base_url: str) -> bytes:
    try:
        text = content.decode("utf-8", errors="replace")
        lines = text.split("\n")
        out = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                seg = stripped if stripped.startswith("http") else urllib.parse.urljoin(base_url, stripped)
                out.append(_make_proxy_url(seg))
            else:
                out.append(line)
        return "\n".join(out).encode("utf-8")
    except Exception:
        return content


_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Amiri-Regular.ttf")

def _escape_drawtext(text: str) -> str:
    """Escape text for ffmpeg drawtext filter."""
    text = text.replace("\\", "\\\\")
    text = text.replace("'",  "\u2019")   # replace typographic apostrophe
    text = text.replace(":",  "\\:")
    text = text.replace("=",  "\\=")
    text = text.replace("%",  "\\%")
    return text

@app.route("/api/download")
def download_video():
    """Private admin-only: stream-download a video trimmed to requested minutes."""
    import subprocess as _sp
    if request.args.get("k", "") != ADMIN_KEY:
        return "غير مصرح", 403
    video_url = request.args.get("url", "").strip()
    if not video_url:
        return "No URL", 400
    try:
        minutes = max(1, min(int(request.args.get("m", 120)), 240))
    except ValueError:
        minutes = 120
    duration = minutes * 60
    raw_title = request.args.get("t", "video")
    safe_title = re.sub(r'[^\w\s\-]', '', raw_title)[:60].strip() or "video"
    watermark_raw = request.args.get("wm", "").strip()
    try:
        wm_dur = max(1, int(request.args.get("wmd", 10)))
    except ValueError:
        wm_dur = 10

    def _build_vf(wm_text: str, dur: int) -> str:
        base = "crop=iw-2:ih-2"
        if not wm_text:
            return base
        escaped = _escape_drawtext(wm_text)
        font = _FONT_PATH.replace("\\", "/").replace(":", "\\:")
        dt = (
            f"drawtext=fontfile='{font}'"
            f":text='{escaped}'"
            f":fontsize=48"
            f":fontcolor=white"
            f":x=w-tw-30"
            f":y=h-th-8"
            f":shadowcolor=black@0.85"
            f":shadowx=2"
            f":shadowy=2"
            f":text_shaping=1"
            f":enable='between(t\\,0\\,{dur})'"
        )
        return f"{base},{dt}"

    def generate():
        cmd = [
            "ffmpeg", "-y",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-i", video_url,
            "-t", str(duration),
            # ── Video: full re-encode + crop + optional watermark ──
            "-vf", _build_vf(watermark_raw, wm_dur),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            # ── Audio: re-encode to change audio fingerprint ──
            "-c:a", "aac",
            "-ar", "44100",
            "-b:a", "128k",
            # ── Strip ALL metadata (title, encoder, creation date…) ──
            "-map_metadata", "-1",
            "-fflags", "+bitexact",
            "-f", "mp4",
            "-movflags", "frag_keyframe+empty_moov+faststart",
            "pipe:1",
        ]
        proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.DEVNULL)
        try:
            while True:
                chunk = proc.stdout.read(1024 * 64)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.terminate()
            except Exception:
                pass

    headers = {
        "Content-Type": "video/mp4",
        "Content-Disposition": f'attachment; filename="{safe_title}.mp4"',
        "Access-Control-Allow-Origin": "*",
    }
    return Response(generate(), headers=headers)


WATCH_INTERVAL = 15 * 60  # check every 15 minutes
SITE_HOME = "https://b.alooytv12.xyz/"

# Global cache: series_url → [genre_arabic, ...] — populated by the watcher each cycle
URL_GENRES_CACHE: dict = {}


def crawl_all_series_from_site():
    """Crawl every genre page and build a mapping {series_url: [genre_arabic_name]}.
    Each series is assigned to the genre page(s) it appears on, not the nav menu.
    Also scrapes the home page for any series not covered by genre pages.
    Returns: dict  {series_url: [genre_arabic_name, ...]}
    """
    url_genres: dict[str, list[str]] = {}

    # Genre pages first — these give us accurate per-genre assignment
    for slug, arabic_name in GENRES.items():
        try:
            page_url = f"https://b.alooytv12.xyz/genre/{slug}.html"
            html = http_requests.get(page_url, headers=HEADERS, timeout=15).text
            found = list(dict.fromkeys(re.findall(
                r'href="(https://b\.alooytv12\.xyz/watch/[^"?]+\.html)"', html
            )))
            for u in found:
                url_genres.setdefault(u, [])
                if arabic_name not in url_genres[u]:
                    url_genres[u].append(arabic_name)
            print(f"[crawl] {arabic_name}: {len(found)} مسلسل")
        except Exception as e:
            print(f"[crawl] خطأ في قسم {slug}: {e}")

    # Home page — pick up anything not on a genre page (assign None)
    try:
        html = http_requests.get(SITE_HOME, headers=HEADERS, timeout=15).text
        for u in re.findall(
            r'href="(https://b\.alooytv12\.xyz/watch/[^"?]+\.html)"', html
        ):
            url_genres.setdefault(u, [])
    except Exception as e:
        print(f"[crawl] خطأ في الصفحة الرئيسية: {e}")

    print(f"[crawl] اكتشف {len(url_genres)} مسلسل من الموقع (كل الأقسام)")
    return url_genres


def _run_watcher_once():
    """Run one watcher cycle:
    Only process locally-tracked series (genre page crawling is disabled).
    """
    global URL_GENRES_CACHE
    url_genres: dict[str, list[str]] = {}

    # Only process locally-tracked series in series_files/
    os.makedirs(SERIES_DIR, exist_ok=True)
    for root, dirs, files in os.walk(SERIES_DIR):
        for fname in files:
            if not fname.endswith(".txt"):
                continue
            fp = os.path.join(root, fname)
            su = read_series_url_from_file(fp)
            if su and su not in url_genres:
                url_genres[su] = []

    if not url_genres:
        print("[watcher] لا توجد مسلسلات محفوظة للفحص")
        return

    print(f"[watcher] يفحص {len(url_genres)} مسلسل محفوظ...")
    for series_url, genres in url_genres.items():
        try:
            background_cache_series(series_url, genres=genres if genres else None)
        except Exception as e:
            print(f"[watcher] خطأ في {series_url}: {e}")


def watcher_loop():
    """Background watcher: checks immediately at startup, then every 15 minutes."""
    print("[watcher] بدأ — يفحص الموقع فوراً ثم كل 15 دقيقة")
    _run_watcher_once()
    while True:
        time.sleep(WATCH_INTERVAL)
        _run_watcher_once()


def start_watcher():
    t = threading.Thread(target=watcher_loop, daemon=True)
    t.start()


def _noor_startup_session():
    """On startup: load saved cookies, then verify whether session is still valid."""
    loaded = _noor_load_cookies()
    if loaded:
        _noor_refresh_session()   # also updates _noor_logged_in
    else:
        print("[noorplay] no saved session — user must login manually")


if __name__ == "__main__":
    # Launched directly (e.g. deployment uses "python3 main.py")
    # Re-launch via gunicorn so we always use the production server
    import subprocess, sys
    proc = subprocess.run([
        sys.executable, "-m", "gunicorn",
        "-w", "1", "--threads", "4", "--timeout", "120",
        "-b", "0.0.0.0:5000",
        "main:app",
    ])
    sys.exit(proc.returncode)
else:
    # Imported as a module by gunicorn — run full initialization
    init_db()
    _noor_startup_session()
    start_watcher()
