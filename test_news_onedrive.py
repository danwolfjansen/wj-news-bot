"""OneDrive failure handling in the news bot. Run: python3 test_news_onedrive.py

Reproduces the 8 Oct 2026 failure: the Microsoft sign-in returned 401, the
drafts never reached OneDrive, yet the approval email looked normal and the
run showed green.
"""
import os, sys, tempfile
from unittest import mock

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp()
os.chdir(TMP)
os.environ.update({
    "HOME": TMP, "ONEDRIVE_FOLDER": "",
    "MS_TENANT_ID": "t", "MS_CLIENT_ID": "c", "MS_CLIENT_SECRET": "s",
    "MS_USER_EMAIL": "u@example.com",
    "SMTP_USER": "bot@example.com", "SMTP_PASSWORD": "x",
    "PA_APPROVE_URL": "https://pa.example/approve", "PA_REJECT_URL": "https://pa.example/reject",
    "ANTHROPIC_API_KEY": "test",
})
import news_bot as nb

failures = []


def expect(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        failures.append(msg)


def reset():
    nb._ONEDRIVE_ERRORS.clear()
    nb._ONEDRIVE_DOWNLOAD_FAILED = False
    nb._RUN_ERRORS.clear()
    local = nb._pending_file_path()
    if os.path.exists(local):
        os.remove(local)


class Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error: Unauthorized for url: "
                "https://login.microsoftonline.com/t/oauth2/v2.0/token")


def token_401(*a, **k):
    return Resp(401)


def token_ok(*a, **k):
    return Resp(200, {"access_token": "tok"})


class FakeSMTP:
    sent = []

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def ehlo(self): pass
    def starttls(self): pass
    def login(self, *a): pass

    def sendmail(self, frm, to, msg):
        # Decode the MIME message so tests can search plain text.
        import email
        from email.header import decode_header, make_header
        m = email.message_from_string(msg)
        parts = [str(make_header(decode_header(m["Subject"])))]
        for part in m.walk():
            if part.get_content_maintype() == "text":
                parts.append(part.get_payload(decode=True).decode("utf-8"))
        FakeSMTP.sent.append("\n".join(parts))


DRAFT = {"token": "abc", "post_id": 2666, "title": "Test headline",
         "excerpt": "Excerpt", "body": "<p>Body</p>", "division": "sap",
         "style_warnings": []}

print("download")
reset()
with mock.patch("requests.post", token_401):
    expect(nb._download_pending_via_graph() == {}, "401 returns an empty dict")
expect(nb._ONEDRIVE_DOWNLOAD_FAILED and nb._ONEDRIVE_ERRORS,
       "401 is recorded as a OneDrive failure")

reset()
with mock.patch("requests.post", token_ok), \
     mock.patch("requests.get", lambda *a, **k: Resp(404)):
    nb._download_pending_via_graph()
expect(not nb._ONEDRIVE_ERRORS and not nb._ONEDRIVE_DOWNLOAD_FAILED,
       "404 (file not created yet) is not treated as a failure")

print("upload")
reset()
nb._ONEDRIVE_DOWNLOAD_FAILED = True
local = nb._pending_file_path()
os.makedirs(os.path.dirname(local), exist_ok=True)
open(local, "w").write("{}")
with mock.patch("requests.post", token_ok), mock.patch("requests.put") as put:
    nb._upload_pending_via_graph(local)
expect(not put.called,
       "after a failed download, the partial local copy is NOT uploaded over the full file")
expect(nb._ONEDRIVE_ERRORS, "the skipped upload is recorded")

reset()
open(local, "w").write("{}")
with mock.patch("requests.post", token_401):
    nb._upload_pending_via_graph(local)
expect(any("upload" in e for e in nb._ONEDRIVE_ERRORS), "a 401 on upload is recorded")

print("approval email")
reset()
FakeSMTP.sent.clear()
with mock.patch("smtplib.SMTP", FakeSMTP):
    nb.send_approval_email([DRAFT])
msg = FakeSMTP.sent[-1] if FakeSMTP.sent else ""
expect("ACTION NEEDED" not in msg, "no warning when OneDrive worked")

reset()
nb._note_onedrive_error("could not upload pending_approvals.json to OneDrive (401)")
FakeSMTP.sent.clear()
with mock.patch("smtplib.SMTP", FakeSMTP):
    nb.send_approval_email([DRAFT])
msg = FakeSMTP.sent[-1] if FakeSMTP.sent else ""
expect("ACTION NEEDED" in msg, "subject and body carry the warning when OneDrive failed")
expect("2666" in msg, "the warning lists the WordPress draft IDs to publish by hand")
expect("MS_CLIENT_SECRET" in msg, "the warning names the likely fix")

print("main, end to end")
reset()
FakeSMTP.sent.clear()
story = {"id": "s1", "title": "SAP story", "link": "https://example.com/1",
         "summary": "x"}
rewritten = {"title": "Test headline", "excerpt": "Ex", "body": "<p>B</p>",
             "tags": [], "style_warnings": []}
seen_saved = []
with mock.patch.object(nb.run_guard, "should_deliver", lambda *a, **k: (True, "test window")), \
     mock.patch.object(nb.run_guard, "mark_delivered") as marked, \
     mock.patch.object(nb, "fetch_stories", lambda div, seen: [story] if div == "sap" else []), \
     mock.patch.object(nb, "is_story_relevant", lambda *a, **k: True), \
     mock.patch.object(nb, "rewrite_story", lambda *a, **k: dict(rewritten)), \
     mock.patch.object(nb, "create_wp_draft", lambda *a, **k: 2666), \
     mock.patch.object(nb, "save_seen_stories", lambda s: seen_saved.append(set(s))), \
     mock.patch.object(nb, "load_seen_stories", lambda: set()), \
     mock.patch.object(nb, "send_failure_alert_email"), \
     mock.patch("requests.post", token_401), \
     mock.patch("smtplib.SMTP", FakeSMTP):
    exited = None
    try:
        nb.main()
    except SystemExit as e:
        exited = e.code
expect(FakeSMTP.sent and "ACTION NEEDED" in FakeSMTP.sent[-1],
       "the approval email still goes out, with the warning")
expect(marked.called, "the delivery is still recorded (no duplicate digest later)")
expect(seen_saved and "s1" in seen_saved[-1], "seen stories are still saved")
expect(exited == 1, "the run exits non-zero, so it shows red in GitHub Actions")

reset()
FakeSMTP.sent.clear()
with mock.patch.object(nb.run_guard, "should_deliver", lambda *a, **k: (True, "test window")), \
     mock.patch.object(nb.run_guard, "mark_delivered"), \
     mock.patch.object(nb, "fetch_stories", lambda div, seen: [story] if div == "sap" else []), \
     mock.patch.object(nb, "is_story_relevant", lambda *a, **k: True), \
     mock.patch.object(nb, "rewrite_story", lambda *a, **k: dict(rewritten)), \
     mock.patch.object(nb, "create_wp_draft", lambda *a, **k: 2666), \
     mock.patch.object(nb, "save_seen_stories", lambda s: None), \
     mock.patch.object(nb, "load_seen_stories", lambda: set()), \
     mock.patch("requests.post", token_ok), \
     mock.patch("requests.get", lambda *a, **k: Resp(200, {})), \
     mock.patch("requests.put", lambda *a, **k: Resp(200)), \
     mock.patch("smtplib.SMTP", FakeSMTP):
    exited = "no exit"
    try:
        nb.main()
    except SystemExit as e:
        exited = e.code
expect(exited == "no exit", "a healthy run finishes normally (green)")
expect(FakeSMTP.sent and "ACTION NEEDED" not in FakeSMTP.sent[-1],
       "a healthy run's email has no warning")

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("ALL TESTS PASSED")
