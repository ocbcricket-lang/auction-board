# app.py
# Toy Auction Board (single-file) – names from Players.xlsx, admin + upload password, uploader page, admin reset
# Requires: supabase>=2.6.0, Flask, pandas, openpyxl

import os, io, json, time, zipfile
from typing import Optional

from flask import Flask, request, redirect, url_for, render_template_string, make_response, jsonify
from werkzeug.exceptions import RequestEntityTooLarge

import pandas as pd
from supabase import create_client, Client
import requests

# -----------------------------
# CONFIG
# -----------------------------
APP_VERSION = "Auction Board v2.0 (names+uploader+reset+private pw)"
BUCKET = os.environ.get("BUCKET", "auction")               # public bucket (images, Players.xlsx)
SECURE_BUCKET = os.environ.get("SECURE_BUCKET", "auction-secure")  # private bucket (pwd files)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")  # service role for server-side
SUPABASE_PUBLIC_URL = os.environ.get("SUPABASE_PUBLIC_URL", SUPABASE_URL).rstrip("/")
IMAGES_PREFIX = os.environ.get("IMAGES_PREFIX", "images/")       # where images live in public bucket
PLAYERS_XLS = "Players.xlsx"  # tries .xlsx first; then .xls
BUDGET = int(os.environ.get("BUDGET", "10000"))

TEAM_NAMES = [
    "Thunder Bolts", "Warriors", "Mighty Eagles", "Tigers", "Falcons",
    "Sharks", "Panthers", "Rangers", "Titans", "Lions", "Dragons", "Vikings"
]

# Flask
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB max upload

# -----------------------------
# SUPABASE CLIENT
# -----------------------------
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in environment.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

print(f"### Running {APP_VERSION} ###")


# -----------------------------
# STORAGE HELPERS
# -----------------------------
def get_object(path: str) -> Optional[bytes]:
    """
    Download object from PUBLIC bucket. Try SDK first; fallback to public URL.
    """
    try:
        return supabase.storage.from_(BUCKET).download(path)
    except Exception as e:
        # try public URL fallback (no auth)
        try:
            if not SUPABASE_PUBLIC_URL:
                return None
            url = f"{SUPABASE_PUBLIC_URL}/storage/v1/object/public/{BUCKET}/{path.lstrip('/')}"
            r = requests.get(url, timeout=15)
            if r.ok:
                return r.content
            return None
        except Exception as e2:
            print("get_object public fallback error:", e, "|", e2)
            return None


def get_private_object(path: str) -> Optional[bytes]:
    """
    Download from PRIVATE bucket; NO public fallback.
    """
    try:
        return supabase.storage.from_(SECURE_BUCKET).download(path)
    except Exception as e:
        print("get_private_object error:", e)
        return None


def put_object(path: str, data: bytes, content_type: Optional[str] = None) -> bool:
    """
    Upload to PUBLIC bucket; overwrite if exists.
    """
    file_options = {"content-type": content_type} if content_type else None
    try:
        supabase.storage.from_(BUCKET).upload(path=path, file=data, file_options=file_options)
        return True
    except Exception as e1:
        try:
            supabase.storage.from_(BUCKET).update(path=path, file=data, file_options=file_options)
            return True
        except Exception as e2:
            print("put_object error:", e1, "| update fallback error:", e2)
            return False


# -----------------------------
# PASSWORDS
# -----------------------------
def load_admin_password() -> str:
    # 1) secure bucket file
    data = get_private_object("pwdadmin.txt")
    if data:
        try:
            pw = data.decode("utf-8").strip()
            if pw:
                return pw
        except Exception as e:
            print("admin pw decode error:", e)
    # 2) env fallback
    env_pw = os.environ.get("ADMIN_PASSWORD")
    if env_pw:
        return env_pw.strip()
    # 3) final fallback
    return "om@OM1"


def load_upload_password() -> str:
    data = get_private_object("pwdupload.txt")
    if data:
        try:
            pw = data.decode("utf-8").strip()
            if pw:
                return pw
        except Exception as e:
            print("upload pw decode error:", e)
    env_pw = os.environ.get("UPLOAD_PASSWORD")
    if env_pw:
        return env_pw.strip()
    return "upload@123"


# -----------------------------
# PLAYERS XLS LOADING + LOOKUP
# -----------------------------
_df_players_cache = None
_playername_by_no = {}

def _read_excel_bytes(data: bytes, filename: str) -> pd.DataFrame:
    if filename.lower().endswith(".xlsx"):
        return pd.read_excel(io.BytesIO(data), engine="openpyxl")
    else:
        # .xls support requires xlrd<=1.2.0; recommend .xlsx
        try:
            return pd.read_excel(io.BytesIO(data), engine="xlrd")
        except Exception:
            # try letting pandas guess
            return pd.read_excel(io.BytesIO(data))

def read_players_df() -> Optional[pd.DataFrame]:
    global _df_players_cache, _playername_by_no
    for cand in (PLAYERS_XLS, "Players.xls"):
        data = get_object(cand)
        if not data:
            continue
        try:
            df = _read_excel_bytes(data, cand)
            cols_lower = {str(c).strip().lower(): c for c in df.columns}
            # tolerate variations
            pno_key = None
            for key in cols_lower:
                norm = key.replace(" ", "")
                if "player" in norm and ("no" in norm or "number" in norm) or norm in ("playerno", "playernumber", "id", "no"):
                    pno_key = cols_lower[key]
                    break
            pname_key = None
            for key in cols_lower:
                norm = key.replace(" ", "")
                if "name" in norm:
                    pname_key = cols_lower[key]
                    break

            if not pno_key or not pname_key:
                print("⚠️ Players sheet missing expected columns. Found:", list(df.columns))
                continue

            df = df.rename(columns={pno_key: "PlayerNo", pname_key: "PlayerName"})
            df["PlayerNo"] = pd.to_numeric(df["PlayerNo"], errors="coerce").round()
            df = df.dropna(subset=["PlayerNo"])
            df["PlayerNo"] = df["PlayerNo"].astype(int)
            df["PlayerName"] = df["PlayerName"].astype(str).str.strip()
            df = df.drop_duplicates(subset=["PlayerNo"])
            _df_players_cache = df
            _playername_by_no = {int(n): nm for n, nm in zip(df["PlayerNo"].astype(int), df["PlayerName"])}
            print(f"✅ Players loaded: {len(df)} rows from {cand}")
            return df
        except Exception as e:
            print("Players read error for", cand, ":", e)
    _df_players_cache = None
    _playername_by_no = {}
    print("❌ Players file not found/unreadable in bucket root (Players.xlsx / Players.xls).")
    return None

def get_player_name(num: int) -> str:
    try:
        n = int(num)
    except Exception:
        return f"Unknown_{num}"
    if not _playername_by_no:
        read_players_df()
    return _playername_by_no.get(n, f"Unknown_{n}")


# -----------------------------
# AUCTION STATE (simple JSON)
# -----------------------------
_state_cache = None

def _read_state() -> dict:
    global _state_cache
    if _state_cache is not None:
        return _state_cache
    data = get_object("auction_state.json")
    if not data:
        # initialize if missing
        return reset_auction_state() or {}
    try:
        _state_cache = json.loads(data.decode("utf-8"))
        return _state_cache
    except Exception:
        return reset_auction_state() or {}

def _write_state(state: dict) -> bool:
    global _state_cache
    buf = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ok = put_object("auction_state.json", buf, "application/json")
    if ok:
        _state_cache = state
    return ok

def reset_auction_state() -> dict:
    """Overwrite auction_state.json with a fresh state."""
    fresh = {
        "version": 1,
        "reset_ts": int(time.time()),
        "next_player": 1,
        "sales": [],
        "assignments": {},
        "teams": {t: {"players": [], "spent": 0, "balance": BUDGET} for t in TEAM_NAMES},
    }
    _write_state(fresh)
    print("🧹 Auction state reset.")
    return fresh


# -----------------------------
# AUTH & UPLOAD GATE
# -----------------------------
def _authed() -> bool:
    return request.cookies.get("auth") == "ok"

def _can_upload() -> bool:
    # Allow if admin, else require upload password in form field 'upload_pw'
    if _authed():
        return True
    submitted = (request.form.get("upload_pw") or "").strip()
    return bool(submitted and submitted == load_upload_password())


# -----------------------------
# HTML TEMPLATES
# -----------------------------
ADMIN_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Admin Upload</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto;background:#f3f4f6;margin:0;color:#111827;}
  .wrap{max-width:800px;margin:24px auto;padding:0 16px;}
  .panel{background:white;padding:16px;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,0.06);}
  .btn{background:#2563eb;color:#fff;border:0;border-radius:10px;padding:10px 14px;cursor:pointer;font-weight:600;}
  input[type=file]{padding:10px;border-radius:8px;border:1px solid #ccc;width:100%;}
  .subtle{color:#6b7280;font-size:12px;}
  hr{border:0;border-top:1px solid #e5e7eb;margin:16px 0;}
  a{color:#2563eb;text-decoration:none;} a:hover{text-decoration:underline;}
  .danger{background:#dc2626;}
</style></head><body>
<div class="wrap">
  <div class="panel">
    <h2>Admin Uploads</h2>
    <p class="subtle">Upload a ZIP (images + Players.xls/xlsx) <b>or</b> select multiple files.</p>

    <h3>Upload ZIP</h3>
    <form method="post" action="{{ url_for('upload_zip') }}" enctype="multipart/form-data">
      <input type="file" name="zipfile" accept=".zip" required>
      <button class="btn" type="submit">Upload ZIP</button>
    </form>

    <hr>

    <h3>Upload Multiple Files</h3>
    <form method="post" action="{{ url_for('upload_multi') }}" enctype="multipart/form-data">
      <input type="file" name="files" multiple required>
      <button class="btn" type="submit">Upload Files</button>
    </form>

    <hr>
    <h3>Danger zone</h3>
    <p class="subtle">This will clear all current assignments, reset team balances, and start fresh.</p>
    <form action="{{ url_for('admin_reset') }}" method="post"
          onsubmit="return confirm('This will clear ALL assignments and restart the auction. Continue?');">
      <button class="btn danger" type="submit">🧹 Reset Auction (start fresh)</button>
    </form>

    <p style="margin-top:12px">
      <a href="{{ url_for('main') }}">↩ Back to Board</a> |
      <a href="{{ url_for('uploader') }}">Uploader page</a>
    </p>
  </div>
</div>
</body></html>
"""

UPLOADER_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Upload</title>
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto;background:#f3f4f6;margin:0;color:#111827;}
 .wrap{max-width:800px;margin:24px auto;padding:0 16px;}
 .panel{background:white;padding:16px;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,0.06);}
 .btn{background:#2563eb;color:#fff;border:0;border-radius:10px;padding:10px 14px;cursor:pointer;font-weight:600;}
 input[type=file],input[type=password]{padding:10px;border-radius:8px;border:1px solid #ccc;width:100%;}
 hr{border:0;border-top:1px solid #e5e7eb;margin:16px 0;}
</style></head><body>
<div class="wrap"><div class="panel">
  <h2>Upload Images / Players</h2>
  <p>Enter the upload password to submit files.</p>

  <h3>Upload ZIP</h3>
  <form method="post" action="{{ url_for('upload_zip') }}" enctype="multipart/form-data">
    <input type="password" name="upload_pw" placeholder="Upload password" required>
    <input type="file" name="zipfile" accept=".zip" required>
    <button class="btn" type="submit">Upload ZIP</button>
  </form>

  <hr>

  <h3>Upload Multiple Files</h3>
  <form method="post" action="{{ url_for('upload_multi') }}" enctype="multipart/form-data">
    <input type="password" name="upload_pw" placeholder="Upload password" required>
    <input type="file" name="files" multiple required>
    <button class="btn" type="submit">Upload Files</button>
  </form>

  <p style="margin-top:12px"><a href="{{ url_for('main') }}">↩ Back to Board</a></p>
</div></div></body></html>
"""

LOGIN_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Login</title>
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto;background:#f3f4f6;margin:0;color:#111827;}
 .wrap{max-width:420px;margin:80px auto;padding:0 16px;}
 .panel{background:white;padding:16px;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,0.06);}
 input[type=password]{padding:10px;border-radius:8px;border:1px solid #ccc;width:100%;}
 .btn{background:#2563eb;color:#fff;border:0;border-radius:10px;padding:10px 14px;cursor:pointer;font-weight:600;margin-top:10px;}
 .subtle{color:#6b7280;font-size:12px;}
</style></head><body>
<div class="wrap"><div class="panel">
  <h2>Admin Login</h2>
  <form method="post">
    <input type="password" name="pw" placeholder="Admin password" required>
    <button class="btn" type="submit">Login</button>
  </form>
  <p class="subtle" style="margin-top:12px"><a href="{{ url_for('uploader') }}">Uploader (for end-users)</a></p>
</div></div></body></html>
"""

MAIN_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>Auction Board</title>
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto;background:#f3f4f6;margin:0;color:#111827;}
 .wrap{max-width:720px;margin:24px auto;padding:0 16px;}
 .card{background:white;padding:16px;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,0.06);}
 input[type=number]{padding:10px;border-radius:8px;border:1px solid #ccc;width:120px;}
 .btn{background:#2563eb;color:#fff;border:0;border-radius:10px;padding:10px 14px;cursor:pointer;font-weight:600;margin-left:8px;}
 .subtle{color:#6b7280;font-size:12px;}
 .name{font-size:20px;font-weight:700;margin-top:12px;}
</style></head><body>
<div class="wrap">
  <div class="card">
    <h2>Auction Board</h2>
    <form method="get">
      <label>Player number:</label>
      <input type="number" name="n" min="1" step="1" value="{{ n or '' }}">
      <button class="btn" type="submit">Show</button>
    </form>
    {% if n %}
      <div class="name">#{{n}} — {{ name }}</div>
      <p class="subtle">Tip: place <b>Players.xlsx</b> at bucket root and images under <b>{{ images_prefix }}</b> in public bucket <b>{{ bucket }}</b>.</p>
    {% endif %}
    <p style="margin-top:12px"><a href="{{ url_for('admin') }}">Admin</a> | <a href="{{ url_for('uploader') }}">Uploader</a></p>
  </div>
</div>
</body></html>
"""

# -----------------------------
# ERROR HANDLER
# -----------------------------
@app.errorhandler(RequestEntityTooLarge)
def too_big(_):
    return ("File too large. Please upload a smaller file.", 413)

# -----------------------------
# ROUTES
# -----------------------------
@app.route("/")
def login():
    return render_template_string(LOGIN_HTML)

@app.route("/", methods=["POST"])
def do_login():
    if (request.form.get("pw") or "").strip() == load_admin_password():
        resp = make_response(redirect(url_for("admin")))
        resp.set_cookie("auth", "ok", max_age=2*24*3600, httponly=True, samesite="Lax")
        return resp
    return ("Unauthorized", 401)

@app.route("/logout")
def logout():
    resp = make_response(redirect(url_for("login")))
    resp.delete_cookie("auth")
    return resp

@app.route("/admin")
def admin():
    if not _authed():
        return redirect(url_for("login"))
    return render_template_string(ADMIN_HTML)

@app.route("/uploader")
def uploader():
    return render_template_string(UPLOADER_HTML)

@app.route("/admin/reset", methods=["POST"])
def admin_reset():
    if not _authed():
        return ("Unauthorized", 401)
    try:
        reset_auction_state()
        return redirect(url_for("admin"))
    except Exception as e:
        print("admin_reset error:", e)
        return ("Failed to reset auction (see logs)", 500)

@app.route("/upload_zip", methods=["POST"])
def upload_zip():
    if not _can_upload():
        return ("Unauthorized", 401)
    f = request.files.get("zipfile")
    if not f or not f.filename.lower().endswith(".zip"):
        return ("Zip file required", 400)

    zdata = io.BytesIO(f.read())
    with zipfile.ZipFile(zdata) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            data = z.read(name)
            # Decide destination path in public bucket
            lower = name.lower()
            dest = None
            if lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
                # images go under images/
                base = name.split("/")[-1]
                dest = f"{IMAGES_PREFIX}{base}"
                put_object(dest, data, content_type="image/jpeg")
            elif lower.endswith((".xlsx", ".xls")) and "players" in lower:
                dest = "Players.xlsx" if lower.endswith(".xlsx") else "Players.xls"
                put_object(dest, data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            else:
                # other files: place at root
                base = name.split("/")[-1]
                dest = base
                put_object(dest, data, content_type="application/octet-stream")
            if dest:
                print("uploaded:", dest)

    # refresh players cache
    read_players_df()
    return redirect(request.referrer or url_for("uploader"))

@app.route("/upload_multi", methods=["POST"])
def upload_multi():
    if not _can_upload():
        return ("Unauthorized", 401)
    files = request.files.getlist("files")
    if not files:
        return ("No files", 400)

    for f in files:
        fname = f.filename or ""
        if not fname:
            continue
        data = f.read()
        lower = fname.lower()
        dest = None
        if lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
            dest = f"{IMAGES_PREFIX}{fname.split('/')[-1]}"
            put_object(dest, data, content_type="image/jpeg")
        elif lower.endswith((".xlsx", ".xls")) and "players" in lower:
            dest = "Players.xlsx" if lower.endswith(".xlsx") else "Players.xls"
            put_object(dest, data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        else:
            dest = fname.split("/")[-1]
            put_object(dest, data, content_type="application/octet-stream")
        if dest:
            print("uploaded:", dest)

    # refresh players cache
    read_players_df()
    return redirect(request.referrer or url_for("uploader"))

@app.route("/main")
def main():
    n = request.args.get("n")
    name = None
    if n:
        name = get_player_name(n)
    return render_template_string(MAIN_HTML, n=n, name=name, images_prefix=IMAGES_PREFIX, bucket=BUCKET)

@app.route("/api/name/<int:num>")
def api_name(num: int):
    return jsonify({"num": num, "name": get_player_name(num)})

@app.route("/version")
def version():
    return {"version": APP_VERSION}
