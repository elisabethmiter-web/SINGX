"""Optional email via SMTP. Without SMTP_HOST the app runs in copy-the-link mode."""
import os
import smtplib
import ssl
from email.message import EmailMessage
from html import escape


def enabled():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def sender_email():
    return os.environ.get("ADMIN_EMAIL") or os.environ.get("SMTP_FROM", "")


def _send(msg):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
            if user:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(context=ctx)
            if user:
                s.login(user, password)
            s.send_message(msg)


def _html(paragraphs, button=None, footer=""):
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    if button:
        label, url = button
        body += (f'<p style="margin:24px 0"><a href="{escape(url)}" style="background:#24408e;color:#fff;'
                 f'padding:12px 22px;border-radius:6px;text-decoration:none;font-weight:600">{escape(label)}</a></p>')
    if footer:
        body += f'<p style="color:#5b6475;font-size:12px">{footer}</p>'
    return (f'<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;color:#1d2433;'
            f'line-height:1.5">{body}</div>')


def send(to, subject, text, html, attachments=()):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to
    if os.environ.get("ADMIN_EMAIL"):
        msg["Reply-To"] = os.environ["ADMIN_EMAIL"]
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    for name, path in attachments:
        with open(path, "rb") as fh:
            msg.add_attachment(fh.read(), maintype="application", subtype="pdf", filename=name)
    _send(msg)


def signing_request(to, name, business, title, message, link, reminder=False):
    subj = f"{'Reminder: ' if reminder else ''}Please sign: {title}"
    note = f"\n{message}\n" if message else ""
    text = (f"Hi {name},\n\n{business} sent you “{title}” to review and sign.\n{note}\n"
            f"Review and sign: {link}\n\nDon't forward this email: the link is unique to you.\n")
    paras = [f"Hi {escape(name)},", f"{escape(business)} sent you <strong>{escape(title)}</strong> to review and sign."]
    if message:
        paras.append(f'<em style="color:#5b6475">“{escape(message)}”</em>')
    send(to, subj, text, _html(paras, ("Review document", link),
                               "Don't forward this email: the link is unique to you."))


def completed(to, name, business, title, ref, link, attachment):
    subj = f"Completed: {title}"
    text = (f"Hi {name},\n\nAll parties have signed “{title}”. The completed document is attached.\n"
            f"Envelope ID {ref}\n\nView or download: {link}\n")
    send(to, subj, text, _html([f"Hi {escape(name)},",
                                f"All parties have signed <strong>{escape(title)}</strong>. "
                                f"The completed document is attached.", f"Envelope ID {escape(ref)}"],
                               ("View document", link)), [attachment])


def notify_sender(subject, text, link):
    to = sender_email()
    if not to:
        return
    send(to, subject, f"{text}\n\n{link}\n", _html([escape(text)], ("Open envelope", link)))
