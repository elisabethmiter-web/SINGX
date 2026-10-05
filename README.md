# FormSign 2

A self-hosted e-signature app that works like DocuSign: upload a PDF, drop signature and form fields where each person needs to act, send it, and recipients sign in order. When everyone has signed, you get one completed PDF with the signatures stamped on the pages and a Certificate of Completion at the end.

## What it does

**Prepare (sender)**
- Upload one or more PDFs into an *envelope*.
- Add recipients: people who need to sign, and people who just get a copy.
- Set signing order. Same number signs at the same time; higher numbers wait. Example: client signs first (1), you countersign (2), finance gets a copy (3).
- Drag fields onto the pages for each recipient, colour-coded per person: **Signature, Initials, Date signed, Full name, Text, Checkbox**. Move, resize, mark required, label, delete. Arrow keys nudge.
- Save as a draft, or save the layout as a **template** for documents you send often.

**Sign (recipient)**
- Opens their unique link (any device, no account), agrees to the e-signature disclosure.
- Gets guided field by field with **Start / Next**, the way DocuSign does it.
- Adopts a signature once (typed in a signature style, or drawn) with initials, then taps each field to apply it.
- Can **decline** with a reason, which stops the envelope and tells you why.

**Track and finish**
- Dashboard: Waiting for others, Completed, Drafts, Needs attention.
- Per-envelope view: each recipient's status (Sent, Viewed, Signed), signing links, full history with timestamps and IP addresses.
- Correct a recipient's name or email (their old link stops working), extend expiry, void with a reason, copy an envelope to resend.
- Completed PDF: fields flattened onto the original pages, an Envelope ID on every page, then a **Certificate of Completion** (signers, signatures, IPs, sent/viewed/consented/signed times, document fingerprints, full history).
- **Verify page** (`/verify`, public): anyone can upload a completed PDF to confirm it's unaltered, by matching its SHA-256 fingerprint.
- With email turned on, recipients are emailed when it's their turn, and everyone gets the completed PDF.

## Run it on your computer

Needs Python 3.10+.

```bash
pip install -r requirements.txt
ADMIN_PASSWORD=choose-a-password BUSINESS_NAME="Your Company" python app.py
```

Open http://localhost:5000. (Windows PowerShell: `$env:ADMIN_PASSWORD="..."` first.) Locally only you can open signing links; put it online for clients.

## Put it online

**Render (about US$7/month):** push this folder to a private GitHub repo, then on render.com choose **New → Blueprint** and pick the repo. `render.yaml` creates the web service and a 1 GB persistent disk. Fill in `ADMIN_PASSWORD`, `BUSINESS_NAME` and `BASE_URL` when asked.

**Anywhere with Docker** (Railway, Fly.io, a VPS): build the `Dockerfile` and mount a persistent volume at `/data`.

Back up `DATA_DIR`: it holds the database, uploaded PDFs, signatures and completed documents.

## Email (optional)

| Variable | Example |
| --- | --- |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.postmarkapp.com` / `587` (`465` for SSL) |
| `SMTP_USER` / `SMTP_PASSWORD` | from your provider |
| `SMTP_FROM` | `Your Company <sign@yourdomain.com>` |
| `ADMIN_EMAIL` | gets completed/declined notices; used as reply-to |

Without email, copy each signer's link from the envelope page when it's their turn. A transactional provider (Postmark, Amazon SES, Mailgun) delivers best.

## Settings

| Variable | Purpose |
| --- | --- |
| `ADMIN_PASSWORD` | Your sign-in password (required) |
| `SECRET_KEY` | Long random string for sessions (required in production) |
| `BUSINESS_NAME` | Shown to recipients, in emails and on PDFs |
| `BASE_URL` | Public address used in emailed links |
| `DATA_DIR` | Storage folder (default `./data`) |
| `LINK_EXPIRY_DAYS` | Signing-link lifetime (default 30) |
| `DATE_FORMAT` | Format for Date signed fields (default `%b %d, %Y`) |

## How it works

- `app.py`: Flask routes. Sender pages need the admin password; recipient pages are reached only by unguessable per-recipient tokens.
- `pdfengine.py`: normalizes uploads (bakes in page rotation, removes interactive form fields), renders page previews with PDFium, stamps fields with ReportLab and pypdf, builds the certificate.
- `static/editor.js`: drag-and-drop field editor. `static/sign.js`: guided signing and signature adoption.
- SQLite tables: `envelopes`, `recipients`, `field_values`, `events`, `templates`.
- Field positions are stored as fractions of the page, so they line up exactly on screen and on the PDF.
- Envelopes snapshot their documents and fields at creation; editing a template never changes envelopes already sent.

## Legal note

This produces simple electronic signatures with consent, intent (adopt-and-sign), signer identification by email link, and an audit trail. That is generally accepted for business contracts, engagement letters and consent forms under laws like the US ESIGN Act, Canada's PIPEDA/UECA-based provincial acts and eIDAS "simple" signatures. It does **not** verify identity (ID checks, SMS codes) or apply a certificate-based digital signature, and some documents (wills, some real-estate and family-law documents) can't be signed electronically in many places. Check what your documents need.

## Ideas for later

- SMS or access-code verification before signing
- Certificate-based PDF seal (PAdES) so Adobe shows a blue "signed" ribbon
- Automatic reminders every N days
- Multiple sender accounts and shared templates
- Signer attachments (upload ID or supporting documents)
- Word file upload (convert with LibreOffice)
