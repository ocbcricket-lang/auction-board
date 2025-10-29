import os, io, json, zipfile, mimetypes, csv, time
import pandas as pd
from datetime import datetime
from flask import (
    Flask, request, redirect, url_for, render_template_string,
    make_response, send_file, abort
)
from flask import redirect, url_for, flash
from supabase import create_client, Client  # pip install supabase

# ---------- CONFIG ----------
BUDGET = 10_000
IMAGE_EXTS = [".jpg", ".jpeg", ".png", ".webp"]
MAX_IMAGE_NUM = 120

# Storage bucket and object layout
BUCKET      = "auction"
PLAYERS_XLS = "Players.xlsx"               # Preferred; will fall back to Players.xls automatically
TEAM_XLS    = "Teamnames.xlsx"             # Preferred; will fall back to Teamnames.xls
TEAM_COL    = "TeamName"
STATE_JSON  = "auction_state.json"
IMAGES_DIR  = "images"                     # e.g., images/12.jpg

# ---------- ENV (Render ? Environment) ----------
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")  # REQUIRED: service_role key
    or os.getenv("SUPABASE_KEY")            # optional alias if you used this name before
    or ""
)
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

FLASK_SECRET = os.environ.get("FLASK_SECRET", "change-me")
VIEW_TOKEN   = os.environ.get("VIEW_TOKEN", "")                  # if set, /view requires ?token=
MAX_UPLOAD_MB= int(os.environ.get("MAX_UPLOAD_MB", "50"))        # per request cap

# ---------- INIT ----------
app = Flask(__name__)
app.secret_key = FLASK_SECRET
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------- AUTH ----------
def _authed(): return request.cookies.get("auth") == "ok"

LOGIN_FORM = """
<!DOCTYPE html><html><head>
<title>Login</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto;background:#f3f4f6}
  .box{background:#fff;max-width:320px;margin:10vh auto;padding:24px;border-radius:12px;box-shadow:0 8px 20px rgba(0,0,0,.08);text-align:center}
  input{display:block;width:100%;margin:10px 0;padding:10px;border-radius:8px;border:1px solid #ccc;font-size:14px}
  button{background:#2563eb;color:#fff;padding:10px;border:none;border-radius:8px;font-weight:600;cursor:pointer;width:100%}
</style></head><body>
  <div class="box">
    <h2>?? Login</h2>
    <form method="POST">
      <input name="username" placeholder="User ID" required>
      <input name="password" type="password" placeholder="Password" required>
      <button type="submit">Login</button>
    </form>
  </div>
</body></html>
"""

@app.route("/", methods=["GET","POST"])
def login():
    if request.method == "POST":
        if request.form.get("username")=="admin" and request.form.get("password")==load_admin_password():
            resp = redirect(url_for("main"))
            resp.set_cookie("auth", "ok", httponly=True, samesite="Lax")
            return resp
        return "<h3 style='color:red;text-align:center;'>Invalid credentials</h3>" + LOGIN_FORM
    return LOGIN_FORM

@app.route("/logout")
def logout():
    resp = redirect(url_for("login"))
    resp.delete_cookie("auth")
    return resp

# ---------- STORAGE HELPERS ----------
def put_object(path: str, data: bytes, content_type: str | None = None) -> bool:
    """
    Upload bytes to Supabase Storage. If object already exists, fall back to update().
    Always return True on success, False on error.
    """
    file_options = {"content-type": content_type} if content_type else None
    try:
        # Try normal upload (set upsert if your SDK supports it)
        supabase.storage.from_(BUCKET).upload(path=path, file=data, file_options=file_options)
        return True
    except Exception as e1:
        try:
            # If it already exists, overwrite via update()
            supabase.storage.from_(BUCKET).update(path=path, file=data, file_options=file_options)
            return True
        except Exception as e2:
            print("put_object error:", e1, "| update fallback error:", e2)
            return False

def get_object(path: str) -> bytes | None:
    """
    Download object from Supabase Storage and normalize the return type.
    Ensures it always returns bytes on success.
    """
    try:
        res = supabase.storage.from_(BUCKET).download(path)
        # Normalize all possible SDK return shapes
        if isinstance(res, (bytes, bytearray)):
            return bytes(res)
        if hasattr(res, "content"):
            return res.content
        if isinstance(res, dict):
            for k in ("data", "file", "content", "body"):
                b = res.get(k)
                if isinstance(b, (bytes, bytearray)):
                    return bytes(b)
        print("get_object: unexpected type", type(res))
        return None
    except Exception as e:
        print("get_object error:", e)
        return None

def sign_url(path: str, expires_sec: int = 3600) -> str|None:
    try:
        res = supabase.storage.from_(BUCKET).create_signed_url(path, expires_in=expires_sec)
        if isinstance(res, dict):
            return res.get("signedURL") or res.get("signed_url")
        return None
    except Exception as e:
        print("sign_url error:", e)
        return None

# ===== Upload password from private bucket or env (extra password for uploads) =====
SECURE_BUCKET = os.environ.get("SECURE_BUCKET", "auction-secure")  # private bucket name
UPLOAD_PW_FILE = "pwdupload.txt"  # one-line text file with the upload password
ADMIN_PW_FILE = "pwdadmin.txt"  # one-line text file with the admin password

def get_private_object(path: str) -> bytes | None:
    """Download from PRIVATE bucket (no public fallback). Normalizes return to bytes."""
    try:
        res = supabase.storage.from_(SECURE_BUCKET).download(path)
        # Normalize common SDK return shapes
        if isinstance(res, (bytes, bytearray)):
            return bytes(res)
        if hasattr(res, "content"):               # e.g., Response-like object
            return res.content
        if isinstance(res, dict):                 # e.g., {"data": b"..."} or similar
            for k in ("data", "file", "content", "body"):
                b = res.get(k)
                if isinstance(b, (bytes, bytearray)):
                    return bytes(b)
        print("get_private_object: unexpected return type:", type(res))
        return None
    except Exception as e:
        print("get_private_object error:", e)
        return None

def load_upload_password() -> str:
    """
    Load the upload password in this order:
      1) auction-secure/pwdupload.txt (private bucket)
      2) env ADMIN_UPLOAD_PASSWORD
      3) default "upload@123"
    Handles SDK return shapes and BOM/newlines.
    """
    # --- private bucket first ---
    try:
        res = supabase.storage.from_(SECURE_BUCKET).download(UPLOAD_PW_FILE)
        b = None
        if isinstance(res, (bytes, bytearray)):
            b = bytes(res)
        elif hasattr(res, "content"):
            b = res.content
        elif isinstance(res, dict):
            for k in ("content", "data", "file", "body"):
                v = res.get(k)
                if isinstance(v, (bytes, bytearray)):
                    b = bytes(v)
                    break
        if b:
            # use utf-8-sig to auto-strip BOM if present
            pw = b.decode("utf-8-sig").strip(" \t\r\n")
            if pw:
                return pw
    except Exception as e:
        print("load_upload_password: private bucket load error:", e)

    # --- env fallback ---
    pw_env = os.environ.get("ADMIN_UPLOAD_PASSWORD")
    if pw_env:
        pw_env = pw_env.encode("utf-8", "ignore").decode("utf-8-sig").strip(" \t\r\n")
        if pw_env:
            return pw_env

    # --- final fallback ---
    return "upload@123"

def load_admin_password() -> str:
    """
    Load admin password from auction-secure/pwdadmin.txt, then env, else default.
    """
    try:
        # Re-use the same normalization logic as upload
        res = supabase.storage.from_(SECURE_BUCKET).download(ADMIN_PW_FILE)
        content = None
        if isinstance(res, (bytes, bytearray)):
            content = res
        elif hasattr(res, "content"):
            content = res.content
        elif isinstance(res, dict):
            content = res.get("content") or res.get("data") or res.get("file") or res.get("body")
        if content:
            pw = content.decode("utf-8-sig").strip(" \r\n\t")
            if pw:
                return pw
    except Exception as e:
        print("load_admin_password: private bucket load error:", e)

    # env fallback
    pw_env = os.environ.get("ADMIN_PASSWORD")
    if pw_env:
        return pw_env.encode("utf-8", "ignore").decode("utf-8-sig").strip(" \r\n\t")

    # final fallback
    return "vatta@A10"

def list_images(prefix: str = "", limit: int = 50):
    """List image names under images/ that start with <prefix>."""
    try:
        entries = supabase.storage.from_(BUCKET).list(path=IMAGES_DIR, limit=1000)
        names = [e["name"] for e in entries if isinstance(e, dict) and "name" in e]
        if prefix:
            names = [n for n in names if n.lower().startswith(prefix.lower())]
        names.sort()
        return names[:limit]
    except Exception as e:
        print("list_images error:", e)
        return []

def _enforce_upload_size(request_obj):
    length = request_obj.content_length or 0
    if length > MAX_UPLOAD_MB * 1024 * 1024:
        abort(413, f"Upload too large. Max {MAX_UPLOAD_MB} MB per request.")

# ---------- DATA: Players / Teams / State ----------
import io as _io

def _read_excel_bytes(data: bytes, filename: str):
    """Read Excel bytes, supporting .xlsx (openpyxl) and .xls (requires xlrd==1.2.0)."""
    name = filename.lower()
    if name.endswith(".xls"):
        # .xls needs xlrd 1.2.0
        return pd.read_excel(_io.BytesIO(data), engine="xlrd")
    # .xlsx (default engine)
    return pd.read_excel(_io.BytesIO(data))

# ---- Players (robust loader + cache)
_df_players_cache = None

def read_players_df():
    """Try Players.xlsx, then Players.xls; normalize columns & types (more tolerant headers)."""
    global _df_players_cache
    tried = []
    for cand in (PLAYERS_XLS, "Players.xls" if PLAYERS_XLS.lower().endswith(".xlsx") else "Players.xlsx"):
        tried.append(cand)
        data = get_object(cand)
        if not data:
            continue
        try:
            df = _read_excel_bytes(data, cand)
            # Normalize header names (case-insensitive)
            cols_lower = {c.lower().strip(): c for c in df.columns}
            pno_key = next((cols_lower[k] for k in cols_lower if "no" in k and "player" in k), None)
            pname_key = next((cols_lower[k] for k in cols_lower if "name" in k), None)

            if not pno_key or not pname_key:
                print("⚠️  Players sheet missing expected columns. Found:", list(df.columns))
                continue

            df = df.rename(columns={pno_key: "PlayerNo", pname_key: "PlayerName"})
            df["PlayerNo"] = pd.to_numeric(df["PlayerNo"], errors="coerce")
            df = df.dropna(subset=["PlayerNo"])
            df["PlayerNo"] = df["PlayerNo"].astype(int)
            df["PlayerName"] = df["PlayerName"].astype(str).str.strip()
            _df_players_cache = df
            print(f"✅ Players loaded from {cand}: {len(df)} rows")
            return df
        except Exception as e:
            print(f"❌ Players read error for {cand}: {e}")
    print("❌ Players file not found or unreadable. Tried:", tried)
    _df_players_cache = None
    return None

def get_player_name(num: int) -> str:
    """Return PlayerName for a given player number (int)."""
    global _df_players_cache
    if _df_players_cache is None:
        _df_players_cache = read_players_df()
        if _df_players_cache is None:
            return f"Unknown_{num}"
    try:
        n = int(num)
    except:
        return f"Unknown_{num}"
    row = _df_players_cache.loc[_df_players_cache["PlayerNo"] == n]
    if not row.empty:
        return str(row.iloc[0]["PlayerName"]).strip()
    print(f"?? No match in Players for PlayerNo={n}")
    return f"Unknown_{num}"

# ---- Teamnames (robust loader + cache)
_team_df_cache = None

def read_teamnames_df():
    """Try Teamnames.xlsx, then Teamnames.xls; return DataFrame or None."""
    global _team_df_cache
    tried = []
    for cand in (TEAM_XLS, "Teamnames.xls" if TEAM_XLS.lower().endswith(".xlsx") else "Teamnames.xlsx"):
        tried.append(cand)
        data = get_object(cand)
        if not data:
            continue
        try:
            df = _read_excel_bytes(data, cand)
            _team_df_cache = df
            return df
        except Exception as e:
            print(f"Teamnames read error for {cand}:", e)
    print("?? Teamnames file not found or unreadable. Tried:", tried)
    _team_df_cache = None
    return None

def load_team_names(default_if_missing=True):
    df = read_teamnames_df()
    if df is not None and TEAM_COL in df.columns:
        names = [str(x).strip() for x in df[TEAM_COL].dropna().tolist() if str(x).strip()]
        seen=set(); ordered=[]
        for n in names:
            if n not in seen:
                seen.add(n); ordered.append(n)
        if ordered:
            return ordered
    if default_if_missing:
        return [
            "OCB", "SUPER TITANZ", "HEROES VALLIKUNNAM", "MJ ELEVEN", "EMINS 11",
            "3K ELEVEN", "ADAMZ", "VIKINGS", "PATH XI", "BROTHERS ADINADU", "GJ BOYS", "SALALA VTML"
        ]
    return []

def find_image_key(num: int) -> str|None:
    # Probe common extensions by attempting to sign; failure => None
    for ext in IMAGE_EXTS:
        key = f"{IMAGES_DIR}/{num}{ext}"
        url = sign_url(key, 5)
        if url:
            return key
    return None

def save_state():
    """Persist in-memory state to Supabase."""
    try:
        payload = {
            "budget": BUDGET,
            "team_state": team_state,
            "current_card": current_card,
            "saved_at": datetime.now().isoformat(timespec="seconds")
        }
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        put_object(STATE_JSON, data, "application/json")
        print("💾 State saved.")
        return True
    except Exception as e:
        print("❌ save_state error:", e)
        return False

def load_state(force_reload=False):
    """Load auction state from Supabase once, unless forced."""
    global team_state
    if team_state and not force_reload:
        return team_state

    data = get_object(STATE_JSON)
    if not data:
        print("⚠️ No auction_state.json found. Creating fresh.")
        team_state = {name: {"left": BUDGET, "players": []} for name in TEAM_NAMES}
        save_state()
        return team_state

    try:
        payload = json.loads(data.decode("utf-8"))
        ts = payload.get("team_state", {})
        for name in TEAM_NAMES:
            ts.setdefault(name, {"left": BUDGET, "players": []})
        current_card.update(payload.get("current_card", {}))
        team_state = ts
        print(f"✅ Loaded state: {sum(len(v['players']) for v in ts.values())} players assigned.")
        return ts
    except Exception as e:
        print("⚠️ load_state error:", e)
        team_state = {name: {"left": BUDGET, "players": []} for name in TEAM_NAMES}
        return team_state

def reconcile_team_state(new_team_names):
    global team_state
    for name in new_team_names:
        team_state.setdefault(name, {"left": BUDGET, "players": []})
    try:
        save_state()
    except Exception as e:
        # Avoid failing the whole deploy if bucket/creds arent ready yet
        print("Startup save_state skipped:", e)
import json, time

_state_cache = None  # keep with your other caches

def reset_auction_state():
    """Fully clear both memory and Supabase state."""
    global team_state, current_card, _state_cache
    print("⚠️ Resetting auction state...")

    team_state = {t: {"left": BUDGET, "players": []} for t in TEAM_NAMES}
    current_card = {"player": None, "name": None, "image_key": None}
    _state_cache = None

    ok = save_state()
    if ok:
        print("✅ Auction reset successful.")
    else:
        print("❌ Failed to reset auction state.")
    return ok


# Load teams, then state, then reconcile
TEAM_NAMES = load_team_names(default_if_missing=True)
current_card = {"player": None, "name": None, "image_key": None}
team_state   = load_state()
reconcile_team_state(TEAM_NAMES)

def _reindex_team(team_name: str):
    players = team_state[team_name]["players"]
    for i, p in enumerate(players, start=1):
        p["idx"] = i

# ---------- TEMPLATES ----------
TEMPLATE = r"""<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>OCB Auction Board</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto;background:#f3f4f6;margin:0;color:#111827;}
  .wrap{max-width:1200px;margin:24px auto;padding:0 16px;}
  h1{font-size:24px;margin-bottom:6px;}
  .btn{background:#2563eb;color:#fff;border:0;border-radius:10px;padding:10px 14px;cursor:pointer;font-weight:600;}
  .input,.select{padding:10px;border-radius:8px;border:1px solid #ccc;width:100%;}
  .panel{background:white;padding:16px;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,0.06);}
  .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:16px;}
  .team{background:white;border:1px solid #e5e7eb;border-radius:16px;padding:12px;}
  th,td{padding:6px;border-bottom:1px solid #eee;text-align:left;}
  .pill{background:#eff6ff;color:#1d4ed8;padding:2px 8px;border-radius:999px;font-weight:700;font-size:12px;}
  .subtle{color:#6b7280;font-size:12px;}
  .topbar{display:flex;gap:12px;justify-content:space-between;align-items:center;}
</style></head><body>
<div class="wrap">
  <div class="topbar">
    <div><h1>?? OCB Auction Board</h1></div>
    <div>
      <a href="{{ url_for('export_csv') }}" class="subtle" style="margin-right:12px;">Export CSV</a>
      <a href="{{ url_for('admin') }}" class="subtle" style="margin-right:12px;">Admin Upload</a>
      <a href="{{ url_for('logout') }}" style="color:#2563eb;font-weight:600;">Logout</a>
    </div>
  </div>
  <div class="panel">
    <form method="get" action="{{ url_for('main') }}">
      <label>Next Player:</label>
      <div style="position:relative">
        <input class="input" id="playerInput" type="number" name="player" min="1" max="{{ max_image_num }}" placeholder="Enter player # (1-{{max_image_num}})" value="{{ player or '' }}" required>
        <div id="suggestBox" style="position:absolute;left:0;right:0;top:100%;background:white;border:1px solid #e5e7eb;border-radius:8px;display:none;z-index:10;max-height:220px;overflow:auto"></div>
      </div>
      <button class="btn" type="submit" style="margin-top:8px;">Show</button>
    </form>
    <script>
      const inp = document.getElementById('playerInput');
      const box = document.getElementById('suggestBox');
      let timer;
      inp.addEventListener('input', () => {
        clearTimeout(timer);
        const val = (inp.value || '').trim();
        if (!val) { box.style.display = 'none'; return; }
        timer = setTimeout(async () => {
          try {
            const r = await fetch(`{{ url_for('api_suggest') }}?prefix=${encodeURIComponent(val)}`, {credentials:'same-origin'});
            if (!r.ok) { box.style.display='none'; return; }
            const data = await r.json();
            if (!data.items || !data.items.length) { box.style.display='none'; return; }
            box.innerHTML = data.items.map(it => `<div style="padding:8px;cursor:pointer" data-p="${it.player}">#${it.player}  ${it.file}</div>`).join('');
            Array.from(box.children).forEach(el => { el.onclick = () => { inp.value = el.dataset.p; box.style.display='none'; }; });
            box.style.display = 'block';
          } catch(e) { box.style.display='none'; }
        }, 200);
      });
      document.addEventListener('click', (e) => { if (!box.contains(e.target) && e.target !== inp) box.style.display = 'none'; });
    </script>
    <hr>
    {% if player %}
      <h3>Showing Player {{ player }}  {{ player_name }}</h3>
      {% if image_url %}
        <img src="{{ image_url }}" alt="Player {{player}}" style="max-width:300px;border-radius:12px;">
      {% else %}
        <div class="subtle">No image found for Player {{player}}.</div>
      {% endif %}
      <form method="post" action="{{ url_for('assign') }}">
        <label>Assign to Team:</label>
        <select class="select" name="team" required>
          <option value="" disabled selected>Select team</option>
          {% for t in team_names %}<option value="{{t}}">{{t}}</option>{% endfor %}
        </select>
        <label>Amount Received:</label>
        <input class="input" type="number" name="amount" min="0" placeholder="e.g. 500" required>
        <input type="hidden" name="player" value="{{player}}">
        <button class="btn" type="submit" style="margin-top:8px;">Submit</button>
      </form>
      <form method="post" action="{{ url_for('undo') }}" style="margin-top:8px;">
        <input type="hidden" name="player" value="{{ player }}">
        <button class="btn" type="submit">Undo assignment for this player</button>
      </form>
    {% endif %}
  </div>
  <div class="grid">
    {% for t in team_names %}
      <div class="team">
        <h3>{{t}} <span class="pill">?{{team_state[t]['left']}}</span></h3>
        <table><thead><tr><th>#</th><th>Player</th><th>Prize</th></tr></thead>
          <tbody>
            {% for p in team_state[t]['players'] %}
              <tr><td>{{p['idx']}}</td><td>{{p['name']}}</td><td>{{p['prize']}}</td></tr>
            {% endfor %}
            {% if not team_state[t]['players'] %}
              <tr><td colspan="3" class="subtle">No players yet.</td></tr>
            {% endif %}
          </tbody></table>
      </div>
    {% endfor %}
  </div>
</div></body></html>
"""

VIEW_ONLY_TEMPLATE = r"""<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>OCB Auction Board  View Only</title>
<meta http-equiv="refresh" content="5">
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto;background:#f3f4f6;margin:0;color:#111827;}
  .wrap{max-width:1200px;margin:24px auto;padding:0 16px;}
  h1{font-size:24px;margin-bottom:6px;}
  .panel{background:white;padding:16px;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,0.06);}
  .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:16px;}
  .team{background:white;border:1px solid #e5e7eb;border-radius:16px;padding:12px;}
  th,td{padding:6px;border-bottom:1px solid #eee;text-align:left;}
  .pill{background:#eff6ff;color:#1d4ed8;padding:2px 8px;border-radius:999px;font-weight:700;font-size:12px;}
  .subtle{color:#6b7280;font-size:12px;}
</style></head><body>
<div class="wrap">
  <h1>?? OCB Auction Board</h1>
  <div class="panel">
    {% if player %}
      <h3>Showing Player {{ player }}  {{ player_name }}</h3>
      {% if image_url %}
        <img src="{{ image_url }}" alt="Player {{player}}" style="max-width:300px;border-radius:12px;">
      {% else %}
        <div class="subtle">No image found for Player {{player}}.</div>
      {% endif %}
    {% else %}
      <div class="subtle">Tip: append <code>?player=12</code> to this URL or wait for main page updates.</div>
    {% endif %}
  </div>
  <div class="grid">
    {% for t in team_names %}
      <div class="team">
        <h3>{{t}} <span class="pill">?{{team_state[t]['left']}}</span></h3>
        <table><thead><tr><th>#</th><th>Player</th><th>Prize</th></tr></thead>
          <tbody>
            {% for p in team_state[t]['players'] %}
              <tr><td>{{p['idx']}}</td><td>{{p['name']}}</td><td>{{p['prize']}}</td></tr>
            {% endfor %}
            {% if not team_state[t]['players'] %}
              <tr><td colspan="3" class="subtle">No players yet.</td></tr>
            {% endif %}
          </tbody></table>
      </div>
    {% endfor %}
  </div>
</div></body></html>
"""

ADMIN_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Admin Upload</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto;background:#f3f4f6;margin:0;color:#111827;}
    .wrap{max-width:800px;margin:24px auto;padding:0 16px;}
    .panel{background:white;padding:16px;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,0.06);}
    .btn{background:#2563eb;color:#fff;border:0;border-radius:10px;padding:10px 14px;cursor:pointer;font-weight:600;}
    input[type=file]{padding:10px;border-radius:8px;border:1px solid #ccc;width:100%;}
    .subtle{color:#6b7280;font-size:12px;}
    .danger{background:#dc2626;}
    hr{border:0;border-top:1px solid #e5e7eb;margin:16px 0;}
    h2,h3{margin:8px 0 12px;}
    a{color:#2563eb;text-decoration:none;}
    a:hover{text-decoration:underline;}
  </style>
</head>
<body>
<div class="wrap">
  <div class="panel">
    <h2>Admin Uploads</h2>
    <p class="subtle">
      Upload a ZIP (images + Players.xls/xlsx + Teamnames.xls/xlsx) <b>or</b> select multiple files.
    </p>

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

    <hr>

    <h3>Danger zone</h3>
    <p class="subtle">This will clear all current assignments, reset team balances, and start the auction fresh.</p>
    <form action="{{ url_for('admin_reset') }}" method="post"
          onsubmit="return confirm('This will clear ALL assignments and restart the auction. Continue?');">
      <button class="btn danger" type="submit">?? Reset Auction (start fresh)</button>
    </form>

    <p style="margin-top:12px">
      <a href="{{ url_for('main') }}">? Back to Board</a>
    </p>
  </div>
</div>
</body>
</html>
"""

# ---------- ROUTES ----------
@app.route("/admin")
def admin():
    if not _authed(): return redirect(url_for("login"))
    return render_template_string(ADMIN_HTML)

@app.route("/admin/reset", methods=["POST"])
def admin_reset():
    if not _authed():              # reuse your existing auth check
        return ("Unauthorized", 401)
    ok = reset_auction_state()
    if ok:
        try:
            flash("Auction cleared. Fresh state created.", "success")
        except Exception:
            pass
        return redirect(url_for("admin"))
    return ("Failed to reset auction (see logs).", 500)

@app.route("/upload_zip", methods=["POST"])
def upload_zip():
    if not _authed(): return redirect(url_for("login"))

    # NEW: require the extra upload password
    submitted = (request.form.get("upload_pw") or "").strip()
    if submitted != load_upload_password():
        return ("Unauthorized: bad upload password", 401)

    _enforce_upload_size(request)
    zf = request.files.get("zipfile")
    ...
    if not zf: abort(400, "No zip uploaded")
    try:
        buf = io.BytesIO(zf.read())
        with zipfile.ZipFile(buf) as z:
            for name in z.namelist():
                if name.endswith("/"): continue
                base = os.path.basename(name)
                base = base.replace("\\", "/").split("/")[-1].strip()
                base_lower = base.lower()
                data = z.read(name)
                if base_lower in ("players.xlsx", "players.xls"):
                    put_object("Players.xlsx" if base_lower.endswith("xlsx") else "Players.xls",
                               data, "application/vnd.ms-excel")
                elif base_lower in ("teamnames.xlsx", "teamnames.xls"):
                    put_object("Teamnames.xlsx" if base_lower.endswith("xlsx") else "Teamnames.xls",
                               data, "application/vnd.ms-excel")
                else:
                    path = f"{IMAGES_DIR}/{base}"
                    ctype = mimetypes.guess_type(base)[0] or "application/octet-stream"
                    put_object(path, data, ctype)
        # refresh caches + reconcile teams
        global _df_players_cache, _team_df_cache, TEAM_NAMES, team_state
        _df_players_cache = None
        _team_df_cache = None
        TEAM_NAMES = load_team_names(default_if_missing=True)
        reconcile_team_state(TEAM_NAMES)
        return redirect(url_for("admin"))
    except Exception as e:
        abort(400, f"Bad ZIP: {e}")

@app.route("/upload_multi", methods=["POST"])
def upload_multi():
    if not _authed(): return redirect(url_for("login"))

    # NEW: require the extra upload password
    submitted = (request.form.get("upload_pw") or "").strip()
    if submitted != load_upload_password():
        return ("Unauthorized: bad upload password", 401)

    _enforce_upload_size(request)
    files = request.files.getlist("files")
    ...
    if not files: abort(400, "No files uploaded")
    for f in files:
        base = os.path.basename(f.filename)
        base = base.replace("\\", "/").split("/")[-1].strip()
        base_lower = base.lower()
        data = f.read()
        if base_lower in ("players.xlsx", "players.xls"):
            put_object("Players.xlsx" if base_lower.endswith("xlsx") else "Players.xls",
                       data, "application/vnd.ms-excel")
        elif base_lower in ("teamnames.xlsx", "teamnames.xls"):
            put_object("Teamnames.xlsx" if base_lower.endswith("xlsx") else "Teamnames.xls",
                       data, "application/vnd.ms-excel")
        else:
            path = f"{IMAGES_DIR}/{base}"
            ctype = mimetypes.guess_type(base)[0] or "application/octet-stream"
            put_object(path, data, ctype)
    global _df_players_cache, _team_df_cache, TEAM_NAMES
    _df_players_cache = None
    _team_df_cache = None
    TEAM_NAMES = load_team_names(default_if_missing=True)
    reconcile_team_state(TEAM_NAMES)
    return redirect(url_for("admin"))

@app.route("/api/suggest")
def api_suggest():
    if not _authed(): abort(401)
    prefix = (request.args.get("prefix") or "").strip()
    if not prefix or not all(c.isdigit() for c in prefix):
        return {"items": []}
    names = list_images(prefix=prefix, limit=20)
    items = []
    for n in names:
        try:
            base = n.rsplit(".", 1)[0]
            pnum = int(base)
            items.append({"player": pnum, "file": n})
        except:
            pass
    uniq = {}
    for it in items:
        uniq.setdefault(it["player"], it)
    return {"items": list(uniq.values())[:10]}

@app.route("/api/diag")
def api_diag():
    """Quick check of loaded players data."""
    df = _df_players_cache or read_players_df()
    info = {
        "players_file_candidates": [PLAYERS_XLS, "Players.xls" if PLAYERS_XLS.lower().endswith(".xlsx") else "Players.xlsx"],
        "loaded": df is not None,
        "row_count": (0 if df is None else int(len(df))),
        "columns": (None if df is None else list(df.columns)),
        "first_rows": (None if df is None else df.head(5).to_dict(orient="records")),
    }
    return info

@app.route("/main")
def main():
    if not _authed(): return redirect(url_for("login"))
    raw = (request.args.get("player") or "").strip()
    player=None; image_url=None; player_name=None; key=None
    if raw:
        try:
            n = int(raw)
            if 1 <= n <= MAX_IMAGE_NUM:
                player = n
                key = find_image_key(n)
                image_url = sign_url(key) if key else None
                player_name = get_player_name(n)
        except: pass
    if player is not None:
        current_card.update({"player": player, "name": player_name, "image_key": key if image_url else None})
        save_state()
    return render_template_string(
        TEMPLATE,
        player=player, player_name=player_name, image_url=image_url,
        team_names=TEAM_NAMES, team_state=team_state, max_image_num=MAX_IMAGE_NUM, budget=BUDGET
    )

@app.route("/assign", methods=["POST"])
def assign():
    if not _authed(): return redirect(url_for("login"))
    team  = request.form.get("team")
    amount= int(request.form.get("amount", 0))
    player= request.form.get("player")
    if team not in team_state or not player: return redirect(url_for("main"))
    num = int(player)
    pname = get_player_name(num)
    display = f"{num}_{pname}"
    if amount > team_state[team]["left"]:
        return f"<h3 style='color:red'>Not enough budget in {team}. <a href='{url_for('main')}'>Back</a></h3>"
    idx = len(team_state[team]["players"]) + 1
    team_state[team]["players"].append({"idx": idx, "name": display, "prize": amount, "player_num": num})
    team_state[team]["left"] -= amount
    save_state()
    return redirect(url_for("main", player=player))

@app.route("/undo", methods=["POST"])
def undo():
    if not _authed(): return redirect(url_for("login"))
    raw = (request.form.get("player") or "").strip()
    try: target = int(raw)
    except: return redirect(url_for("main"))
    for team, state in team_state.items():
        for i, p in enumerate(list(state["players"])):
            match = (p.get("player_num")==target) or (isinstance(p.get("name"), str) and p["name"].startswith(f"{target}_"))
            if match:
                state["left"] += int(p.get("prize", 0))
                state["players"].pop(i)
                _reindex_team(team)
                save_state()
                return redirect(url_for("main", player=target))
    return redirect(url_for("main", player=raw))

@app.route("/view")
def view():
    # token protection if configured
    if VIEW_TOKEN:
        token = request.args.get("token", "")
        if token != VIEW_TOKEN:
            abort(401)
    raw = (request.args.get("player") or "").strip()
    player=None; image_url=None; player_name=None; key=None
    if raw:
        try:
            n = int(raw)
            if 1 <= n <= MAX_IMAGE_NUM:
                player = n
                key = find_image_key(n)
                image_url = sign_url(key) if key else None
                player_name = get_player_name(n)
        except: pass
    if player is None and current_card.get("player") is not None:
        player = current_card["player"]
        key    = current_card["image_key"]
        image_url = sign_url(key) if key else None
        player_name = current_card["name"] or get_player_name(player)
    html = render_template_string(
        VIEW_ONLY_TEMPLATE,
        player=player, player_name=player_name, image_url=image_url,
        team_names=TEAM_NAMES, team_state=team_state, max_image_num=MAX_IMAGE_NUM, budget=BUDGET
    )
    resp = make_response(html); resp.headers["Cache-Control"]="no-store"; return resp

@app.route("/export.csv")
def export_csv():
    if not _authed(): abort(401)
    rows = []
    for team in TEAM_NAMES:
        left = team_state[team]["left"]
        for p in team_state[team]["players"]:
            num = int(p.get("player_num", -1))
            pname = get_player_name(num)
            rows.append({
                "Team": team,
                "Idx": p.get("idx"),
                "PlayerNum": num,
                "PlayerName": pname,
                "DisplayName": p.get("name"),
                "Prize": p.get("prize"),
                "TeamBudgetLeft": left
            })
    si = io.StringIO()
    writer = csv.DictWriter(si, fieldnames=["Team","Idx","PlayerNum","PlayerName","DisplayName","Prize","TeamBudgetLeft"])
    writer.writeheader()
    for r in rows: writer.writerow(r)
    out = io.BytesIO(si.getvalue().encode("utf-8"))
    return send_file(out, mimetype="text/csv", as_attachment=True, download_name=f"auction_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")

# Local dev
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
