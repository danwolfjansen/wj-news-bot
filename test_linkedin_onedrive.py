"""OneDrive failure handling in the LinkedIn bot. Run: python3 test_linkedin_onedrive.py

Reproduces the 8 Oct 2026 failure: the Microsoft sign-in returned 401, the bot
read that as "no stories", marked the week as delivered and lost the post.
"""
import os, sys, tempfile
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ["GITHUB_ACTIONS"] = "true"
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
import linkedin_bot as lb

failures = []


def expect(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        failures.append(msg)


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


class Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


def sign_in_refused():
    raise requests.HTTPError("401 Client Error: Unauthorized for url: "
                             "https://login.microsoftonline.com/x/oauth2/v2.0/token")


print("load_pending")
with mock.patch.object(lb, "_get_graph_token", sign_in_refused):
    expect(raises(lb.load_pending, lb.OneDriveUnavailable),
           "401 at sign-in raises instead of returning an empty store")
with mock.patch.object(lb, "_get_graph_token", lambda: "t"), \
     mock.patch.object(lb.requests, "get", lambda *a, **k: Resp(403, text="denied")):
    expect(raises(lb.load_pending, lb.OneDriveUnavailable), "403 on download raises")
with mock.patch.object(lb, "_get_graph_token", lambda: "t"), \
     mock.patch.object(lb.requests, "get", lambda *a, **k: Resp(404)):
    expect(raises(lb.load_pending, lb.OneDriveUnavailable),
           "404 raises (pending_approvals.json always exists)")
with mock.patch.object(lb, "_get_graph_token", lambda: "t"), \
     mock.patch.object(lb.requests, "get", lambda *a, **k: Resp(200, {"a": {"used": True}})):
    expect(lb.load_pending() == {"a": {"used": True}}, "200 returns the store")

print("load_linkedin_pending")
with mock.patch.object(lb, "_get_graph_token", sign_in_refused):
    expect(raises(lb.load_linkedin_pending, lb.OneDriveUnavailable),
           "401 raises (else shared stories could be re-picked and the file overwritten)")
with mock.patch.object(lb, "_get_graph_token", lambda: "t"), \
     mock.patch.object(lb.requests, "get", lambda *a, **k: Resp(404)):
    expect(lb.load_linkedin_pending() == {}, "404 is a genuinely empty store")

print("main")
ok_to_deliver = lambda *a, **k: (True, "Thursday 08:25 UK, first run in the delivery window")
with mock.patch.object(lb.run_guard, "should_deliver", ok_to_deliver), \
     mock.patch.object(lb.run_guard, "mark_delivered") as marked, \
     mock.patch.object(lb, "send_no_linkedin_email") as no_post_email, \
     mock.patch.object(lb, "_get_graph_token", sign_in_refused):
    exited = None
    try:
        lb.main()
    except SystemExit as e:
        exited = e.code
    expect(exited == 1, "a OneDrive failure exits non-zero, so the run shows red")
    expect(not marked.called, "the week is NOT marked as delivered, so the next run retries")
    expect(not no_post_email.called, "no misleading 'no LinkedIn post this week' email")

with mock.patch.object(lb.run_guard, "should_deliver", ok_to_deliver), \
     mock.patch.object(lb.run_guard, "mark_delivered") as marked, \
     mock.patch.object(lb, "send_no_linkedin_email") as no_post_email, \
     mock.patch.object(lb, "candidate_pool", lambda: []):
    lb.main()
    expect(marked.called and no_post_email.called,
           "a genuinely empty week still sends the email and marks the week done")

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("ALL TESTS PASSED")
