"""FormSign — a self-hosted e-signature app in the spirit of DocuSign.

Upload PDFs, place signature and form fields for each recipient, send the
envelope, and recipients sign in routing order. When everyone has signed, the
fields are flattened onto the PDF with a Certificate of Completion appended.

Run locally:  ADMIN_PASSWORD=changeme python app.py
"""
import base64
import hmac
import json
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file,
                   session, url_for)
from werkzeug.utils import secure_filename

import mailer
import pdfengine

# ------------------------------------------------------------------ config
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
DB_PATH = os.path.join(DATA_DIR, "formsign.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "documents")
PAGE_DIR = os.path.join(DATA_DIR, "pages")
SIG_DIR = os.path.join(DATA_DIR, "signatures")
DONE_DIR = os.path.join(DATA_DIR, "completed")
for _d in (DATA_DIR, UPLOAD_DIR, PAGE_DIR, SIG_DIR, DONE_DIR):
    os.makedirs(_d, exist_ok=True)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "FormSign")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
LINK_DAYS = int(os.environ.get("LINK_EXPIRY_DAYS", "30"))
DATE_FORMAT = os.environ.get("DATE_FORMAT", "%b %d, %Y")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
if BASE_URL.startswith("https"):
    app.config["SESSION_COOKIE_SECURE"] = True

FIELD_TYPES = {
    "signature": {"label": "Signature", "w": 0.24, "h": 0.05},
    "initials": {"label": "Initials", "w": 0.08, "h": 0.045},
    "date_signed": {"label": "Date signed", "w": 0.16, "h": 0.025},
    "name": {"label": "Full name", "w": 0.24, "h": 0.025},
    "text": {"label": "Text", "w": 0.24, "h": 0.025},
    "checkbox": {"label": "Checkbox", "w": 0.025, "h": 0.018},
}

# ------------------------------------------------------------------ database
SCHEMA = """
CREATE TABLE IF NOT EXISTS envelopes (
  id INTEGER PRIMARY KEY,
  ref TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  message TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  docs_json TEXT NOT NULL DEFAULT '[]',
  fields_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  sent_at TEXT, completed_at TEXT, voided_at TEXT, void_reason TEXT,
  declined_at TEXT, expires_at TEXT,
  completed_file TEXT, completed_sha256 TEXT,
  template_id INTEGER
);
CREATE TABLE IF NOT EXISTS recipients (
  id INTEGER PRIMARY KEY,
  envelope_id INTEGER NOT NULL REFERENCES envelopes(id) ON DELETE CASCADE,
  rkey TEXT NOT NULL,
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'signer',
  routing_order INTEGER NOT NULL DEFAULT 1,
  token TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL DEFAULT 'created',
  sent_at TEXT, viewed_at TEXT, consented_at TEXT, signed_at TEXT, declined_at TEXT,
  decline_reason TEXT, ip TEXT, user_agent TEXT,
  adopted_name TEXT, adopted_initials TEXT,
  signature_file TEXT, initials_file TEXT, signature_kind TEXT
);
CREATE TABLE IF NOT EXISTS field_values (
  envelope_id INTEGER NOT NULL REFERENCES envelopes(id) ON DELETE CASCADE,
  field_id TEXT NOT NULL,
  recipient_id INTEGER NOT NULL,
  value TEXT,
  filled_at TEXT,
  PRIMARY KEY (envelope_id, field_id)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  envelope_id INTEGER NOT NULL REFERENCES envelopes(id) ON DELETE CASCADE,
  recipient_id INTEGER,
  type TEXT NOT NULL,
  text TEXT NOT NULL,
  ip TEXT,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS templates (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  message TEXT DEFAULT '',
  docs_json TEXT NOT NULL DEFAULT '[]',
  roles_json TEXT NOT NULL DEFAULT '[]',
  fields_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT,
  archived INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_recipients_env ON recipients(envelope_id);
CREATE INDEX IF NOT EXISTS idx_events_env ON events(envelope_id);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.close()


init_db()


# ------------------------------------------------------------------ helpers
def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or ""


def log(env_id, type_, text, recipient_id=None):
    db().execute("INSERT INTO events (envelope_id, recipient_id, type, text, ip, at) VALUES (?,?,?,?,?,?)",
                 (env_id, recipient_id, type_, text, client_ip() if request else "", now()))


def public_base():
    return BASE_URL or request.url_root.rstrip("/")


def sign_link(token):
    return f"{public_base()}/sign/{token}"


def new_ref():
    return str(uuid.uuid4()).upper()


def short_ref(ref):
    return ref.split("-")[0]


def get_env(eid):
    return db().execute("SELECT * FROM envelopes WHERE id=?", (eid,)).fetchone() or abort(404)


def recipients_of(eid):
    return db().execute("SELECT * FROM recipients WHERE envelope_id=? ORDER BY routing_order, id", (eid,)).fetchall()


def env_state(e):
    if e["status"] == "sent" and e["expires_at"] and e["expires_at"] < now():
        return "expired"
    return e["status"]


STATE_LABELS = {"draft": "Draft", "sent": "Waiting for others", "completed": "Completed",
                "declined": "Declined", "voided": "Voided", "expired": "Expired"}
RECIPIENT_LABELS = {"created": "Not sent yet", "sent": "Sent", "viewed": "Viewed",
                    "signed": "Signed", "declined": "Declined", "cc": "Gets a copy"}


@app.template_filter("when")
def f_when(v):
    if not v:
        return "—"
    try:
        return datetime.fromisoformat(v).strftime("%b %d, %Y · %H:%M UTC")
    except ValueError:
        return v


@app.template_filter("day")
def f_day(v):
    if not v:
        return "—"
    try:
        return datetime.fromisoformat(v).strftime("%b %d, %Y")
    except ValueError:
        return v


@app.context_processor
def _globals():
    return {"business_name": BUSINESS_NAME, "csrf_token": csrf_token, "email_enabled": mailer.enabled(),
            "STATE_LABELS": STATE_LABELS, "RECIPIENT_LABELS": RECIPIENT_LABELS, "short_ref": short_ref}


# ------------------------------------------------------------------ auth + csrf
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


@app.before_request
def _csrf():
    if request.method == "POST" and request.endpoint != "verify":
        sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            if request.is_json:
                return jsonify(error="Your session expired. Reload the page and try again."), 400
            abort(400, "Your session expired. Reload the page and try again.")


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            if request.is_json:
                return jsonify(error="Signed out. Sign in again."), 401
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if not ADMIN_PASSWORD:
        error = "Set the ADMIN_PASSWORD environment variable before signing in."
    elif request.method == "POST":
        if hmac.compare_digest(request.form.get("password", "").encode(), ADMIN_PASSWORD.encode()):
            csrf = session.get("csrf")
            session.clear()
            session["admin"] = True
            session["csrf"] = csrf or secrets.token_urlsafe(24)
            session.permanent = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        error = "That password is incorrect."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------ documents
def store_uploads(files):
    """Normalize uploaded PDFs; return docs list or raise PdfError."""
    docs = []
    for f in files:
        if not f or not f.filename:
            continue
        raw = f.read()
        if not raw.startswith(b"%PDF-"):
            raise pdfengine.PdfError(f"“{f.filename}” isn't a PDF. Save it as PDF and upload it again.")
        tmp = os.path.join(UPLOAD_DIR, f"tmp-{secrets.token_hex(6)}.pdf")
        with open(tmp, "wb") as fh:
            fh.write(raw)
        name = os.path.splitext(f.filename)[0][:120] or "Document"
        fname = f"{secrets.token_hex(8)}-{secure_filename(f.filename) or 'document.pdf'}"
        try:
            sizes = pdfengine.normalize_upload(tmp, os.path.join(UPLOAD_DIR, fname))
        except pdfengine.PdfError as exc:
            raise pdfengine.PdfError(f"{f.filename}: {exc}") from exc
        finally:
            os.remove(tmp)
        nsha = pdfengine.sha256_file(os.path.join(UPLOAD_DIR, fname))
        pdfengine.render_pages(os.path.join(UPLOAD_DIR, fname), page_pattern(nsha))  # pre-render previews
        docs.append({"id": "d" + secrets.token_hex(4), "name": name, "file": fname,
                     "sha256": pdfengine.sha256_bytes(raw), "nsha": nsha, "pages": sizes})
    return docs


def page_pattern(nsha):
    return os.path.join(PAGE_DIR, f"{nsha[:32]}-{{}}.png")


def page_png(doc, n):
    if n < 0 or n >= len(doc["pages"]):
        abort(404)
    out = page_pattern(doc["nsha"]).format(n)
    if not os.path.exists(out):
        pdfengine.render_pages(os.path.join(UPLOAD_DIR, doc["file"]), page_pattern(doc["nsha"]), only=n)
    return send_file(out, mimetype="image/png", max_age=86400)


def find_doc(docs, doc_id):
    return next((d for d in docs if d["id"] == doc_id), None) or abort(404)


# ------------------------------------------------------------------ dashboard
@app.route("/")
@login_required
def dashboard():
    show = request.args.get("show", "all")
    rows = db().execute("SELECT * FROM envelopes ORDER BY COALESCE(completed_at, sent_at, created_at) DESC").fetchall()
    items, counts = [], {"waiting": 0, "completed": 0, "draft": 0, "attention": 0}
    for e in rows:
        st = env_state(e)
        rs = recipients_of(e["id"])
        signers = [r for r in rs if r["kind"] == "signer"]
        signed = sum(1 for r in signers if r["status"] == "signed")
        waiting_on = next((r for r in signers if r["status"] in ("sent", "viewed")), None)
        if st == "sent":
            counts["waiting"] += 1
        elif st == "completed":
            counts["completed"] += 1
        elif st == "draft":
            counts["draft"] += 1
        else:
            counts["attention"] += 1
        items.append({"e": e, "state": st, "signers": signers, "signed": signed, "waiting_on": waiting_on})
    groups = {"waiting": ("sent",), "completed": ("completed",), "draft": ("draft",),
              "attention": ("declined", "voided", "expired")}
    if show in groups:
        items = [i for i in items if i["state"] in groups[show]]
    return render_template("dashboard.html", items=items, counts=counts, show=show)


# ------------------------------------------------------------------ new envelope
@app.route("/envelopes/new", methods=["GET", "POST"])
@login_required
def new_envelope():
    templates = db().execute("SELECT * FROM templates WHERE archived=0 ORDER BY name COLLATE NOCASE").fetchall()
    if request.method == "POST":
        try:
            docs = store_uploads(request.files.getlist("files"))
        except pdfengine.PdfError as exc:
            flash(str(exc), "error")
            return redirect(url_for("new_envelope"))
        if not docs:
            flash("Choose at least one PDF to upload.", "error")
            return redirect(url_for("new_envelope"))
        title = request.form.get("title", "").strip() or f"Please sign: {docs[0]['name']}"
        cur = db().execute("INSERT INTO envelopes (ref, title, docs_json, created_at) VALUES (?,?,?,?)",
                           (new_ref(), title[:200], json.dumps(docs), now()))
        log(cur.lastrowid, "created", "Envelope created")
        db().commit()
        return redirect(url_for("edit_envelope", eid=cur.lastrowid))
    return render_template("envelope_new.html", templates=[{**dict(t), "roles": json.loads(t["roles_json"]),
                                                            "docs": json.loads(t["docs_json"])} for t in templates])


@app.route("/templates/<int:tid>/use", methods=["GET", "POST"])
@login_required
def use_template(tid):
    t = db().execute("SELECT * FROM templates WHERE id=? AND archived=0", (tid,)).fetchone() or abort(404)
    roles = json.loads(t["roles_json"])
    if request.method == "POST":
        people, errors = [], []
        for r in roles:
            name = request.form.get(f"name_{r['key']}", "").strip()
            email = request.form.get(f"email_{r['key']}", "").strip()
            if not name or "@" not in email:
                errors.append(r["role"])
            people.append((r, name, email))
        if errors:
            flash("Add a name and email for: " + ", ".join(errors), "error")
            return render_template("template_use.html", t=t, roles=roles, form=request.form)
        title = request.form.get("title", "").strip() or t["name"]
        cur = db().execute(
            "INSERT INTO envelopes (ref, title, message, docs_json, fields_json, created_at, template_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (new_ref(), title, request.form.get("message", t["message"] or "").strip(), t["docs_json"],
             t["fields_json"], now(), tid))
        eid = cur.lastrowid
        for r, name, email in people:
            db().execute("INSERT INTO recipients (envelope_id, rkey, name, email, kind, routing_order, token) "
                         "VALUES (?,?,?,?,?,?,?)", (eid, r["key"], name, email, r["kind"], r["order"],
                                                     secrets.token_urlsafe(24)))
        log(eid, "created", f"Envelope created from template “{t['name']}”")
        db().commit()
        if request.form.get("action") == "send":
            ok, msg = send_envelope(eid)
            flash(msg, "ok" if ok else "error")
            return redirect(url_for("envelope_detail", eid=eid) if ok else url_for("edit_envelope", eid=eid))
        return redirect(url_for("edit_envelope", eid=eid))
    return render_template("template_use.html", t=t, roles=roles, form={})


# ------------------------------------------------------------------ editor
def editor_payload(docs, recipients, fields):
    return {"docs": [{"id": d["id"], "name": d["name"], "pages": d["pages"]} for d in docs],
            "recipients": recipients, "fields": fields, "types": FIELD_TYPES, "emailEnabled": mailer.enabled()}


@app.route("/envelopes/<int:eid>/edit")
@login_required
def edit_envelope(eid):
    e = get_env(eid)
    if e["status"] != "draft":
        return redirect(url_for("envelope_detail", eid=eid))
    recips = [{"key": r["rkey"], "name": r["name"], "email": r["email"], "kind": r["kind"],
               "order": r["routing_order"]} for r in recipients_of(eid)]
    payload = editor_payload(json.loads(e["docs_json"]), recips, json.loads(e["fields_json"]))
    payload.update(mode="envelope", title=e["title"], message=e["message"],
                   saveUrl=url_for("save_envelope", eid=eid),
                   pageUrl=url_for("admin_page", eid=eid, doc_id="__DOC__", n=0)[:-5],
                   addDocUrl=url_for("add_document", eid=eid))
    return render_template("editor.html", e=e, payload=payload, mode="envelope")


def clean_fields(raw, valid_keys, docs):
    pages = {d["id"]: len(d["pages"]) for d in docs}
    out = []
    for f in raw if isinstance(raw, list) else []:
        try:
            t = f["type"]
            if t not in FIELD_TYPES or f["recipient"] not in valid_keys or f["doc"] not in pages:
                continue
            page = int(f["page"])
            if not 0 <= page < pages[f["doc"]]:
                continue
            x, y, w, h = (min(max(float(f[k]), 0.0), 1.0) for k in ("x", "y", "w", "h"))
            out.append({"id": str(f.get("id") or "f" + secrets.token_hex(4))[:24], "type": t,
                        "recipient": f["recipient"], "doc": f["doc"], "page": page,
                        "x": round(x, 5), "y": round(y, 5), "w": round(max(w, 0.01), 5), "h": round(max(h, 0.008), 5),
                        "required": bool(f.get("required", t not in ("checkbox",))),
                        "label": str(f.get("label", ""))[:80]})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def clean_recipients(raw, need_email=True):
    out, seen = [], set()
    for r in raw if isinstance(raw, list) else []:
        key = str(r.get("key", ""))[:24]
        if not key or key in seen:
            continue
        seen.add(key)
        name = str(r.get("name", "")).strip()[:120]
        email = str(r.get("email", "")).strip()[:200]
        kind = "cc" if r.get("kind") == "cc" else "signer"
        try:
            order = max(1, min(int(r.get("order", 1)), 20))
        except (TypeError, ValueError):
            order = 1
        out.append({"key": key, "name": name, "email": email, "kind": kind, "order": order,
                    "role": str(r.get("role", name)).strip()[:80]})
    return out


@app.route("/envelopes/<int:eid>/save", methods=["POST"])
@login_required
def save_envelope(eid):
    e = get_env(eid)
    if e["status"] != "draft":
        return jsonify(error="This envelope was already sent and can't be edited."), 409
    data = request.get_json(silent=True) or {}
    docs = json.loads(e["docs_json"])
    # Documents: allow reorder/rename/remove (but not add — that's a separate upload)
    if isinstance(data.get("docs"), list):
        by_id = {d["id"]: d for d in docs}
        new_docs = []
        for d in data["docs"]:
            if d.get("id") in by_id:
                doc = dict(by_id[d["id"]])
                doc["name"] = (str(d.get("name") or doc["name"]).strip())[:120]
                new_docs.append(doc)
        if new_docs:
            docs = new_docs
    recips = clean_recipients(data.get("recipients"))
    fields = clean_fields(data.get("fields"), {r["key"] for r in recips}, docs)
    title = (str(data.get("title") or e["title"]).strip())[:200]
    message = str(data.get("message") or "").strip()[:2000]
    existing = {r["rkey"]: r for r in recipients_of(eid)}
    db().execute("DELETE FROM recipients WHERE envelope_id=?", (eid,))
    for r in recips:
        tok = existing[r["key"]]["token"] if r["key"] in existing else secrets.token_urlsafe(24)
        db().execute("INSERT INTO recipients (envelope_id, rkey, name, email, kind, routing_order, token) "
                     "VALUES (?,?,?,?,?,?,?)", (eid, r["key"], r["name"], r["email"], r["kind"], r["order"], tok))
    db().execute("UPDATE envelopes SET title=?, message=?, docs_json=?, fields_json=? WHERE id=?",
                 (title, message, json.dumps(docs), json.dumps(fields), eid))
    db().commit()
    if data.get("send"):
        ok, msg = send_envelope(eid)
        if not ok:
            return jsonify(error=msg), 422
        flash(msg, "ok")
        return jsonify(ok=True, redirect=url_for("envelope_detail", eid=eid))
    if data.get("template_name"):
        tid = save_as_template(str(data["template_name"]).strip()[:120], message, docs, recips, fields)
        return jsonify(ok=True, message=f"Saved as template. Find it under Templates.", template_id=tid)
    return jsonify(ok=True, message="Draft saved")


@app.route("/envelopes/<int:eid>/documents", methods=["POST"])
@login_required
def add_document(eid):
    e = get_env(eid)
    if e["status"] != "draft":
        return jsonify(error="This envelope was already sent."), 409
    try:
        new = store_uploads(request.files.getlist("files"))
    except pdfengine.PdfError as exc:
        return jsonify(error=str(exc)), 422
    docs = json.loads(e["docs_json"]) + new
    db().execute("UPDATE envelopes SET docs_json=? WHERE id=?", (json.dumps(docs), eid))
    db().commit()
    return jsonify(ok=True, docs=[{"id": d["id"], "name": d["name"], "pages": d["pages"]} for d in new])


@app.route("/envelopes/<int:eid>/page/<doc_id>/<int:n>.png")
@login_required
def admin_page(eid, doc_id, n):
    e = get_env(eid)
    return page_png(find_doc(json.loads(e["docs_json"]), doc_id), n)


@app.route("/envelopes/<int:eid>/delete", methods=["POST"])
@login_required
def delete_envelope(eid):
    e = get_env(eid)
    if e["status"] != "draft":
        abort(409)
    db().execute("DELETE FROM envelopes WHERE id=?", (eid,))
    db().commit()
    flash("Draft deleted.", "ok")
    return redirect(url_for("dashboard"))


# ------------------------------------------------------------------ sending + routing
def validate_for_send(e, recips, fields):
    docs = json.loads(e["docs_json"])
    if not docs:
        return "Add at least one document."
    signers = [r for r in recips if r["kind"] == "signer"]
    if not signers:
        return "Add at least one signer."
    for r in recips:
        if not r["name"] or "@" not in r["email"]:
            return "Every recipient needs a name and a valid email address."
    for r in signers:
        if not any(f["recipient"] == r["rkey"] and f["type"] == "signature" for f in fields):
            return f"Place at least one signature field for {r['name']}."
    return None


def send_envelope(eid):
    e = get_env(eid)
    recips = recipients_of(eid)
    fields = json.loads(e["fields_json"])
    problem = validate_for_send(e, recips, fields)
    if problem:
        return False, problem
    exp = (datetime.now(timezone.utc) + timedelta(days=LINK_DAYS)).replace(microsecond=0).isoformat()
    db().execute("UPDATE envelopes SET status='sent', sent_at=?, expires_at=? WHERE id=?", (now(), exp, eid))
    db().execute("UPDATE recipients SET status='cc' WHERE envelope_id=? AND kind='cc'", (eid,))
    log(eid, "sent", "Envelope sent")
    db().commit()
    activated = advance_routing(eid)
    names = ", ".join(r["name"] for r in activated)
    if mailer.enabled():
        return True, f"Sent. {names} {'has' if len(activated) == 1 else 'have'} been emailed."
    return True, f"Ready to sign. Copy the signing link for {names} and send it to them."


def advance_routing(eid):
    """Activate the next routing-order group if the current one is finished. Returns newly activated."""
    recips = recipients_of(eid)
    pending = [r for r in recips if r["kind"] == "signer" and r["status"] not in ("signed",)]
    if not pending:
        return []
    order = min(r["routing_order"] for r in pending)
    group = [r for r in pending if r["routing_order"] == order and r["status"] == "created"]
    e = get_env(eid)
    for r in group:
        db().execute("UPDATE recipients SET status='sent', sent_at=? WHERE id=?", (now(), r["id"]))
        log(eid, "delivered", f"Sent to {r['name']} <{r['email']}>", r["id"])
        if mailer.enabled():
            try:
                mailer.signing_request(r["email"], r["name"], BUSINESS_NAME, e["title"], e["message"],
                                       sign_link(r["token"]))
            except Exception as exc:  # noqa: BLE001
                log(eid, "email_failed", f"Email to {r['email']} failed: {str(exc)[:120]}", r["id"])
    db().commit()
    return group


def complete_envelope(eid):
    e = get_env(eid)
    docs = json.loads(e["docs_json"])
    fields = json.loads(e["fields_json"])
    recips = {r["rkey"]: dict(r) for r in recipients_of(eid)}
    values = {v["field_id"]: v["value"] for v in
              db().execute("SELECT * FROM field_values WHERE envelope_id=?", (eid,)).fetchall()}
    by_doc = {}
    for f in fields:
        r = recips.get(f["recipient"])
        if not r:
            continue
        val = values.get(f["id"])
        if f["type"] == "signature":
            val = os.path.join(SIG_DIR, r["signature_file"]) if r["signature_file"] else None
        elif f["type"] == "initials":
            val = os.path.join(SIG_DIR, r["initials_file"]) if r["initials_file"] else None
        elif f["type"] == "date_signed":
            val = datetime.fromisoformat(r["signed_at"]).strftime(DATE_FORMAT) if r["signed_at"] else ""
        elif f["type"] == "name":
            val = r["adopted_name"] or r["name"]
        elif f["type"] == "checkbox":
            val = val == "true"
        by_doc.setdefault(f["doc"], {}).setdefault(f["page"], []).append({**f, "value": val})
    completed_at = now()
    db().execute("UPDATE envelopes SET status='completed', completed_at=? WHERE id=?", (completed_at, eid))
    log(eid, "completed", "Envelope completed: all signers have signed")
    db().commit()
    e = get_env(eid)
    events = [dict(x) for x in db().execute("SELECT * FROM events WHERE envelope_id=? ORDER BY at, id", (eid,))]
    rlist = []
    for r in recipients_of(eid):
        d = dict(r)
        d["signature_file"] = os.path.join(SIG_DIR, r["signature_file"]) if r["signature_file"] else None
        rlist.append(d)
    out_name = f"{e['ref']}.pdf"
    digest = pdfengine.build_completed(os.path.join(DONE_DIR, out_name), dict(e), docs, by_doc, rlist,
                                       events, BUSINESS_NAME, UPLOAD_DIR)
    db().execute("UPDATE envelopes SET completed_file=?, completed_sha256=? WHERE id=?", (out_name, digest, eid))
    db().commit()
    if mailer.enabled():
        attach = (f"{secure_filename(e['title']) or 'document'}-completed.pdf", os.path.join(DONE_DIR, out_name))
        for r in recipients_of(eid):
            try:
                mailer.completed(r["email"], r["name"], BUSINESS_NAME, e["title"], e["ref"],
                                 sign_link(r["token"]), attach)
            except Exception as exc:  # noqa: BLE001
                log(eid, "email_failed", f"Completed copy to {r['email']} failed: {str(exc)[:120]}", r["id"])
        try:
            mailer.notify_sender(f"Completed: {e['title']}", f"Everyone has signed “{e['title']}”.",
                                 public_base() + url_for("envelope_detail", eid=eid))
        except Exception:  # noqa: BLE001
            pass
        log(eid, "copies_sent", "Completed copies emailed to all recipients")
        db().commit()


# ------------------------------------------------------------------ envelope detail + actions
@app.route("/envelopes/<int:eid>")
@login_required
def envelope_detail(eid):
    e = get_env(eid)
    if e["status"] == "draft":
        return redirect(url_for("edit_envelope", eid=eid))
    recips = recipients_of(eid)
    pending = [r for r in recips if r["kind"] == "signer" and r["status"] != "signed"]
    current_order = min((r["routing_order"] for r in pending), default=None)
    events = db().execute("SELECT * FROM events WHERE envelope_id=? ORDER BY at DESC, id DESC", (eid,)).fetchall()
    docs = json.loads(e["docs_json"])
    fields = json.loads(e["fields_json"])
    return render_template("envelope_detail.html", e=e, state=env_state(e), recips=recips, events=events,
                           docs=docs, current_order=current_order, sign_link=sign_link,
                           field_count={r["rkey"]: sum(1 for f in fields if f["recipient"] == r["rkey"]) for r in recips})


@app.route("/envelopes/<int:eid>/recipients/<int:rid>/resend", methods=["POST"])
@login_required
def resend(eid, rid):
    e = get_env(eid)
    r = db().execute("SELECT * FROM recipients WHERE id=? AND envelope_id=?", (rid, eid)).fetchone() or abort(404)
    if env_state(e) != "sent" or r["status"] not in ("sent", "viewed"):
        flash("This recipient can't be reminded right now.", "error")
    elif mailer.enabled():
        try:
            mailer.signing_request(r["email"], r["name"], BUSINESS_NAME, e["title"], e["message"],
                                   sign_link(r["token"]), reminder=True)
            log(eid, "reminder", f"Reminder emailed to {r['name']}", rid)
            flash(f"Reminder sent to {r['email']}.", "ok")
        except Exception as exc:  # noqa: BLE001
            flash(f"The email didn't go out: {exc}", "error")
    db().commit()
    return redirect(url_for("envelope_detail", eid=eid))


@app.route("/envelopes/<int:eid>/recipients/<int:rid>/update", methods=["POST"])
@login_required
def correct_recipient(eid, rid):
    """Correct a recipient's name/email before they sign (issues a new link)."""
    e = get_env(eid)
    r = db().execute("SELECT * FROM recipients WHERE id=? AND envelope_id=?", (rid, eid)).fetchone() or abort(404)
    name, email = request.form.get("name", "").strip(), request.form.get("email", "").strip()
    if env_state(e) != "sent" or r["status"] in ("signed", "declined") or not name or "@" not in email:
        flash("Enter a name and valid email. Recipients who already signed can't be changed.", "error")
        return redirect(url_for("envelope_detail", eid=eid))
    token = secrets.token_urlsafe(24)
    db().execute("UPDATE recipients SET name=?, email=?, token=? WHERE id=?", (name, email, token, rid))
    log(eid, "corrected", f"Recipient corrected: {r['name']} <{r['email']}> → {name} <{email}>; old link disabled", rid)
    db().commit()
    if r["status"] in ("sent", "viewed") and mailer.enabled():
        try:
            mailer.signing_request(email, name, BUSINESS_NAME, e["title"], e["message"], sign_link(token))
        except Exception:  # noqa: BLE001
            pass
    flash("Recipient updated. Their previous link no longer works.", "ok")
    return redirect(url_for("envelope_detail", eid=eid))


@app.route("/envelopes/<int:eid>/extend", methods=["POST"])
@login_required
def extend(eid):
    get_env(eid)
    exp = (datetime.now(timezone.utc) + timedelta(days=LINK_DAYS)).replace(microsecond=0).isoformat()
    db().execute("UPDATE envelopes SET expires_at=? WHERE id=? AND status='sent'", (exp, eid))
    log(eid, "extended", f"Expiry extended to {exp[:10]}")
    db().commit()
    flash(f"Links now work until {exp[:10]}.", "ok")
    return redirect(url_for("envelope_detail", eid=eid))


@app.route("/envelopes/<int:eid>/void", methods=["POST"])
@login_required
def void(eid):
    e = get_env(eid)
    reason = request.form.get("reason", "").strip()[:300]
    if e["status"] != "sent":
        abort(409)
    if not reason:
        flash("Give a reason for voiding. Recipients will see it.", "error")
        return redirect(url_for("envelope_detail", eid=eid))
    db().execute("UPDATE envelopes SET status='voided', voided_at=?, void_reason=? WHERE id=?", (now(), reason, eid))
    log(eid, "voided", f"Envelope voided: {reason}")
    db().commit()
    flash("Envelope voided. Signing links no longer work.", "ok")
    return redirect(url_for("envelope_detail", eid=eid))


@app.route("/envelopes/<int:eid>/completed.pdf")
@login_required
def admin_completed(eid):
    e = get_env(eid)
    if not e["completed_file"]:
        abort(404)
    return send_file(os.path.join(DONE_DIR, e["completed_file"]), mimetype="application/pdf",
                     download_name=f"{secure_filename(e['title']) or 'document'}-completed.pdf")


@app.route("/envelopes/<int:eid>/original/<doc_id>.pdf")
@login_required
def admin_original(eid, doc_id):
    e = get_env(eid)
    d = find_doc(json.loads(e["docs_json"]), doc_id)
    return send_file(os.path.join(UPLOAD_DIR, d["file"]), mimetype="application/pdf",
                     download_name=f"{secure_filename(d['name'])}.pdf")


@app.route("/envelopes/<int:eid>/copy", methods=["POST"])
@login_required
def copy_envelope(eid):
    e = get_env(eid)
    cur = db().execute("INSERT INTO envelopes (ref, title, message, docs_json, fields_json, created_at) "
                       "VALUES (?,?,?,?,?,?)", (new_ref(), e["title"], e["message"], e["docs_json"],
                                                e["fields_json"], now()))
    nid = cur.lastrowid
    for r in recipients_of(eid):
        db().execute("INSERT INTO recipients (envelope_id, rkey, name, email, kind, routing_order, token) "
                     "VALUES (?,?,?,?,?,?,?)", (nid, r["rkey"], r["name"], r["email"], r["kind"],
                                                 r["routing_order"], secrets.token_urlsafe(24)))
    log(nid, "created", f"Copied from envelope {short_ref(e['ref'])}")
    db().commit()
    return redirect(url_for("edit_envelope", eid=nid))


# ------------------------------------------------------------------ templates
def save_as_template(name, message, docs, recips, fields, tid=None):
    roles = [{"key": r["key"], "role": r.get("role") or r["name"] or f"Signer {i+1}", "kind": r["kind"],
              "order": r["order"]} for i, r in enumerate(recips)]
    if tid:
        db().execute("UPDATE templates SET name=?, message=?, docs_json=?, roles_json=?, fields_json=?, "
                     "updated_at=? WHERE id=?", (name, message, json.dumps(docs), json.dumps(roles),
                                                 json.dumps(fields), now(), tid))
    else:
        tid = db().execute("INSERT INTO templates (name, message, docs_json, roles_json, fields_json, created_at) "
                           "VALUES (?,?,?,?,?,?)", (name or "Untitled template", message, json.dumps(docs),
                                                    json.dumps(roles), json.dumps(fields), now())).lastrowid
    db().commit()
    return tid


@app.route("/templates")
@login_required
def templates_list():
    rows = db().execute("SELECT * FROM templates WHERE archived=0 ORDER BY name COLLATE NOCASE").fetchall()
    items = [{**dict(t), "roles": json.loads(t["roles_json"]), "docs": json.loads(t["docs_json"]),
              "nfields": len(json.loads(t["fields_json"]))} for t in rows]
    return render_template("templates.html", items=items)


@app.route("/templates/new", methods=["POST"])
@login_required
def new_template():
    try:
        docs = store_uploads(request.files.getlist("files"))
    except pdfengine.PdfError as exc:
        flash(str(exc), "error")
        return redirect(url_for("templates_list"))
    if not docs:
        flash("Choose at least one PDF.", "error")
        return redirect(url_for("templates_list"))
    roles = [{"key": "r1", "name": "Client", "email": "", "kind": "signer", "order": 1, "role": "Client"}]
    tid = save_as_template(request.form.get("name", "").strip() or docs[0]["name"], "", docs, roles, [])
    return redirect(url_for("edit_template", tid=tid))


@app.route("/templates/<int:tid>/edit")
@login_required
def edit_template(tid):
    t = db().execute("SELECT * FROM templates WHERE id=? AND archived=0", (tid,)).fetchone() or abort(404)
    roles = [{"key": r["key"], "name": r["role"], "email": "", "kind": r["kind"], "order": r["order"]}
             for r in json.loads(t["roles_json"])]
    payload = editor_payload(json.loads(t["docs_json"]), roles, json.loads(t["fields_json"]))
    payload.update(mode="template", title=t["name"], message=t["message"] or "",
                   saveUrl=url_for("save_template", tid=tid),
                   pageUrl=url_for("template_page", tid=tid, doc_id="__DOC__", n=0)[:-5],
                   addDocUrl=url_for("template_add_document", tid=tid),
                   useUrl=url_for("use_template", tid=tid))
    return render_template("editor.html", t=t, payload=payload, mode="template")


@app.route("/templates/<int:tid>/save", methods=["POST"])
@login_required
def save_template(tid):
    t = db().execute("SELECT * FROM templates WHERE id=? AND archived=0", (tid,)).fetchone() or abort(404)
    data = request.get_json(silent=True) or {}
    docs = json.loads(t["docs_json"])
    if isinstance(data.get("docs"), list):
        by_id = {d["id"]: d for d in docs}
        nd = [dict(by_id[d["id"]], name=str(d.get("name") or by_id[d["id"]]["name"])[:120])
              for d in data["docs"] if d.get("id") in by_id]
        docs = nd or docs
    recips = clean_recipients(data.get("recipients"))
    for r in recips:
        r["role"] = r["name"] or "Signer"
    if not recips:
        return jsonify(error="A template needs at least one role, such as Client."), 422
    fields = clean_fields(data.get("fields"), {r["key"] for r in recips}, docs)
    save_as_template(str(data.get("title") or t["name"]).strip()[:120], str(data.get("message") or "")[:2000],
                     docs, recips, fields, tid)
    return jsonify(ok=True, message="Template saved")


@app.route("/templates/<int:tid>/documents", methods=["POST"])
@login_required
def template_add_document(tid):
    t = db().execute("SELECT * FROM templates WHERE id=? AND archived=0", (tid,)).fetchone() or abort(404)
    try:
        new = store_uploads(request.files.getlist("files"))
    except pdfengine.PdfError as exc:
        return jsonify(error=str(exc)), 422
    docs = json.loads(t["docs_json"]) + new
    db().execute("UPDATE templates SET docs_json=? WHERE id=?", (json.dumps(docs), tid))
    db().commit()
    return jsonify(ok=True, docs=[{"id": d["id"], "name": d["name"], "pages": d["pages"]} for d in new])


@app.route("/templates/<int:tid>/page/<doc_id>/<int:n>.png")
@login_required
def template_page(tid, doc_id, n):
    t = db().execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone() or abort(404)
    return page_png(find_doc(json.loads(t["docs_json"]), doc_id), n)


@app.route("/templates/<int:tid>/delete", methods=["POST"])
@login_required
def delete_template(tid):
    db().execute("UPDATE templates SET archived=1 WHERE id=?", (tid,))
    db().commit()
    flash("Template deleted. Envelopes already sent from it are unaffected.", "ok")
    return redirect(url_for("templates_list"))


# ------------------------------------------------------------------ signing (recipient side)
def load_recipient(token):
    r = db().execute("SELECT * FROM recipients WHERE token=?", (token,)).fetchone()
    if not r:
        abort(404)
    return r, get_env(r["envelope_id"])


def can_sign_now(e, r):
    if env_state(e) != "sent" or r["kind"] != "signer" or r["status"] not in ("sent", "viewed"):
        return False
    return True


@app.route("/sign/<token>")
def sign(token):
    r, e = load_recipient(token)
    st = env_state(e)
    if st == "voided":
        return render_template("sign_closed.html", e=e, r=r, reason="voided")
    if st == "declined":
        return render_template("sign_closed.html", e=e, r=r, reason="declined")
    if st == "expired":
        return render_template("sign_closed.html", e=e, r=r, reason="expired")
    if st == "completed" or r["status"] == "signed":
        return redirect(url_for("sign_done", token=token))
    if r["kind"] == "cc":
        return render_template("sign_closed.html", e=e, r=r, reason="cc")
    if r["status"] == "created":
        return render_template("sign_closed.html", e=e, r=r, reason="waiting")
    if not r["viewed_at"]:
        db().execute("UPDATE recipients SET viewed_at=?, status='viewed' WHERE id=?", (now(), r["id"]))
        log(e["id"], "viewed", f"Viewed by {r['name']}", r["id"])
        db().commit()
        r, e = load_recipient(token)
    if not r["consented_at"]:
        return render_template("sign_consent.html", e=e, r=r)
    docs = json.loads(e["docs_json"])
    fields = json.loads(e["fields_json"])
    recips = {x["rkey"]: x for x in recipients_of(e["id"])}
    values = {v["field_id"]: v["value"] for v in
              db().execute("SELECT field_id, value FROM field_values WHERE envelope_id=?", (e["id"],))}
    mine, others = [], []
    for f in fields:
        if f["recipient"] == r["rkey"]:
            mine.append(f)
        else:
            o = recips.get(f["recipient"])
            if o and o["status"] == "signed":
                shown = {**f, "done": True}
                if f["type"] == "signature":
                    shown["img"] = url_for("sign_asset", token=token, rid=o["id"], kind="signature")
                elif f["type"] == "initials":
                    shown["img"] = url_for("sign_asset", token=token, rid=o["id"], kind="initials")
                elif f["type"] == "date_signed":
                    shown["text"] = datetime.fromisoformat(o["signed_at"]).strftime(DATE_FORMAT)
                elif f["type"] == "name":
                    shown["text"] = o["adopted_name"] or o["name"]
                elif f["type"] == "checkbox":
                    shown["checked"] = values.get(f["id"]) == "true"
                else:
                    shown["text"] = values.get(f["id"]) or ""
                others.append(shown)
    payload = {"docs": [{"id": d["id"], "name": d["name"], "pages": d["pages"]} for d in docs],
               "fields": mine, "others": others, "name": r["name"], "dateText": datetime.now(timezone.utc).strftime(DATE_FORMAT),
               "pageUrl": url_for("sign_page", token=token, doc_id="__DOC__", n=0)[:-5],
               "finishUrl": url_for("sign_finish", token=token), "doneUrl": url_for("sign_done", token=token)}
    return render_template("sign.html", e=e, r=r, payload=payload)


@app.route("/sign/<token>/consent", methods=["POST"])
def sign_consent(token):
    r, e = load_recipient(token)
    if not can_sign_now(e, r):
        return redirect(url_for("sign", token=token))
    if request.form.get("agree") != "on":
        flash("Tick the box to agree before continuing.", "error")
        return redirect(url_for("sign", token=token))
    db().execute("UPDATE recipients SET consented_at=? WHERE id=?", (now(), r["id"]))
    log(e["id"], "consented", f"{r['name']} agreed to use electronic records and signatures", r["id"])
    db().commit()
    return redirect(url_for("sign", token=token))


@app.route("/sign/<token>/page/<doc_id>/<int:n>.png")
def sign_page(token, doc_id, n):
    r, e = load_recipient(token)
    if env_state(e) in ("voided", "expired"):
        abort(410)
    return page_png(find_doc(json.loads(e["docs_json"]), doc_id), n)


@app.route("/sign/<token>/asset/<int:rid>/<kind>.png")
def sign_asset(token, rid, kind):
    r, e = load_recipient(token)
    o = db().execute("SELECT * FROM recipients WHERE id=? AND envelope_id=?", (rid, e["id"])).fetchone() or abort(404)
    fname = o["signature_file"] if kind == "signature" else o["initials_file"] if kind == "initials" else None
    if not fname:
        abort(404)
    return send_file(os.path.join(SIG_DIR, fname), mimetype="image/png")


def decode_png(data_url):
    if not isinstance(data_url, str) or not data_url.startswith("data:image/png;base64,"):
        return None
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except ValueError:
        return None
    if not raw.startswith(b"\x89PNG") or not 100 < len(raw) < 1_500_000:
        return None
    return raw


@app.route("/sign/<token>/finish", methods=["POST"])
def sign_finish(token):
    r, e = load_recipient(token)
    if not can_sign_now(e, r) or not r["consented_at"]:
        return jsonify(error="This document can't be signed right now. Reload the page."), 409
    data = request.get_json(silent=True) or {}
    fields = [f for f in json.loads(e["fields_json"]) if f["recipient"] == r["rkey"]]
    values = data.get("values") or {}
    sig = decode_png(data.get("signature"))
    ini = decode_png(data.get("initials"))
    adopted_name = str(data.get("adoptedName") or r["name"]).strip()[:120]
    adopted_initials = str(data.get("adoptedInitials") or "").strip()[:8]
    missing = []
    for f in fields:
        t = f["type"]
        if t == "signature" and not sig:
            missing.append("signature")
        elif t == "initials" and not ini:
            missing.append("initials")
        elif t == "text" and f.get("required") and not str(values.get(f["id"], "")).strip():
            missing.append(f.get("label") or "a text field")
        elif t == "checkbox" and f.get("required") and values.get(f["id"]) is not True:
            missing.append(f.get("label") or "a required checkbox")
    if missing:
        return jsonify(error="Complete the remaining required fields: " + ", ".join(sorted(set(missing)))), 422
    ts = now()
    sig_file = ini_file = None
    if sig:
        sig_file = f"{e['id']}-{r['id']}-sig-{secrets.token_hex(4)}.png"
        with open(os.path.join(SIG_DIR, sig_file), "wb") as fh:
            fh.write(sig)
    if ini:
        ini_file = f"{e['id']}-{r['id']}-ini-{secrets.token_hex(4)}.png"
        with open(os.path.join(SIG_DIR, ini_file), "wb") as fh:
            fh.write(ini)
    for f in fields:
        if f["type"] == "text":
            v = str(values.get(f["id"], "")).strip()[:500]
        elif f["type"] == "checkbox":
            v = "true" if values.get(f["id"]) is True else "false"
        else:
            continue
        db().execute("INSERT OR REPLACE INTO field_values (envelope_id, field_id, recipient_id, value, filled_at) "
                     "VALUES (?,?,?,?,?)", (e["id"], f["id"], r["id"], v, ts))
    cur = db().execute(
        "UPDATE recipients SET status='signed', signed_at=?, ip=?, user_agent=?, adopted_name=?, adopted_initials=?, "
        "signature_file=?, initials_file=?, signature_kind=? WHERE id=? AND status IN ('sent','viewed')",
        (ts, client_ip(), request.headers.get("User-Agent", "")[:300], adopted_name, adopted_initials,
         sig_file, ini_file, "draw" if data.get("signatureKind") == "draw" else "type", r["id"]))
    if cur.rowcount != 1:
        db().rollback()
        return jsonify(error="This document was already signed."), 409
    log(e["id"], "signed", f"Signed by {adopted_name} <{r['email']}>", r["id"])
    db().commit()
    remaining = db().execute("SELECT COUNT(*) FROM recipients WHERE envelope_id=? AND kind='signer' AND status!='signed'",
                             (e["id"],)).fetchone()[0]
    if remaining == 0:
        complete_envelope(e["id"])
    else:
        advance_routing(e["id"])
    return jsonify(ok=True, redirect=url_for("sign_done", token=token))


@app.route("/sign/<token>/decline", methods=["POST"])
def sign_decline(token):
    r, e = load_recipient(token)
    if not can_sign_now(e, r):
        return redirect(url_for("sign", token=token))
    reason = request.form.get("reason", "").strip()[:500]
    if not reason:
        flash("Tell the sender why you're declining.", "error")
        return redirect(url_for("sign", token=token))
    db().execute("UPDATE recipients SET status='declined', declined_at=?, decline_reason=?, ip=? WHERE id=?",
                 (now(), reason, client_ip(), r["id"]))
    db().execute("UPDATE envelopes SET status='declined', declined_at=? WHERE id=?", (now(), e["id"]))
    log(e["id"], "declined", f"Declined by {r['name']}: {reason}", r["id"])
    db().commit()
    if mailer.enabled():
        try:
            mailer.notify_sender(f"Declined: {e['title']}", f"{r['name']} declined to sign. Reason: {reason}",
                                 public_base() + url_for("envelope_detail", eid=e["id"]))
        except Exception:  # noqa: BLE001
            pass
    return redirect(url_for("sign", token=token))


@app.route("/sign/<token>/done")
def sign_done(token):
    r, e = load_recipient(token)
    if env_state(e) in ("voided", "declined"):
        return redirect(url_for("sign", token=token))
    if e["status"] != "completed" and r["status"] != "signed" and r["kind"] != "cc":
        return redirect(url_for("sign", token=token))
    waiting = [x for x in recipients_of(e["id"]) if x["kind"] == "signer" and x["status"] != "signed"]
    return render_template("sign_done.html", e=e, r=r, waiting=waiting)


@app.route("/sign/<token>/download")
def sign_download(token):
    r, e = load_recipient(token)
    if e["status"] != "completed" or not e["completed_file"]:
        abort(404)
    return send_file(os.path.join(DONE_DIR, e["completed_file"]), mimetype="application/pdf", as_attachment=True,
                     download_name=f"{secure_filename(e['title']) or 'document'}-completed.pdf")


# ------------------------------------------------------------------ public verification
@app.route("/verify", methods=["GET", "POST"])
def verify():
    result = None
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            result = {"ok": False, "msg": "Choose a PDF to check."}
        else:
            digest = pdfengine.sha256_bytes(f.read())
            e = db().execute("SELECT * FROM envelopes WHERE completed_sha256=?", (digest,)).fetchone()
            if e:
                result = {"ok": True, "e": e, "digest": digest}
            else:
                result = {"ok": False, "digest": digest,
                          "msg": "No match. This file isn't a completed document issued by this system, "
                                 "or it was changed after signing."}
    return render_template("verify.html", result=result)


# ------------------------------------------------------------------ errors + headers
@app.errorhandler(404)
def _404(_e):
    return render_template("error.html", title="Page not found",
                           message="This link doesn't match anything. Check that you copied the whole address."), 404


@app.errorhandler(400)
def _400(e):
    return render_template("error.html", title="Something went wrong",
                           message=getattr(e, "description", "Reload the page and try again.")), 400


@app.errorhandler(413)
def _413(_e):
    if request.is_json or request.path.endswith("/documents"):
        return jsonify(error="Uploads are limited to 40 MB."), 413
    return render_template("error.html", title="File too large", message="Uploads are limited to 40 MB."), 413


@app.after_request
def _headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    if request.path.startswith("/sign/"):
        resp.headers.setdefault("X-Robots-Tag", "noindex")
        if not request.path.endswith(".png"):
            resp.headers.setdefault("Cache-Control", "no-store")
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=os.environ.get("DEBUG") == "1")
