"""Verification for the shared style guard + both bots' retry loops.
Run:  python3 test_style_guard.py
"""
import json
import style_guard as sg

failures = []


def expect(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        failures.append(msg)


print("== 1. Fragments that LEAKED into published posts on 2026-08-17 ==")
leaked = {
    "'increasingly' (was LinkedIn-only)":
        "hiring teams are increasingly filtering for AI proficiency",
    "'highlighted' (old regex missed past tense)":
        "New research highlighted by Diginomica puts a name to this pattern.",
    "'The uncomfortable truth is' (no pattern existed)":
        "The uncomfortable truth is that domain expertise takes years.",
    "'The practical question ... is whether' (old regex too narrow)":
        "The practical question for hiring managers is whether to wait.",
    "'has never been in X' reversal setup (no pattern existed)":
        "The scarcity has never been in people who understand AI concepts.",
}
for name, text in leaked.items():
    expect(len(sg.detect(text)) > 0, f"now caught: {name}")

print("\n== 2. Union coverage: news-side patterns fire in linkedin context and vice versa ==")
expect(len(sg.detect("This underscores the importance of hiring early.", "linkedin")) > 0,
       "puffery caught in linkedin context (was news-only)")
expect(len(sg.detect("Rates rose, signalling that budgets are tight.", "linkedin")) > 0,
       "participle tail caught in linkedin context (was news-only)")
expect(len(sg.detect("Experts argue the market will cool.", "linkedin")) > 0,
       "vague attribution caught in linkedin context (was news-only)")
expect(len(sg.detect("Six months ago, nobody asked. Now every spec leads with it.")) > 0,
       "then/now contrast caught in news context (was LinkedIn-only)")
expect(len(sg.detect("Demand has moved from configuration to strategy.")) > 0,
       "'has moved from X to Y' caught in news context (was LinkedIn-only)")
expect(len(sg.detect("In our SAP practice we keep hearing this.")) > 0,
       "'In our X practice' caught in news context (was LinkedIn-only)")
expect(len(sg.detect("AI fluency gets a candidate into the conversation.")) > 0,
       "'into the conversation' caught in news context (was LinkedIn-only)")
expect(len(sg.detect("Strong quarter for SAP hiring. Thoughts?", "linkedin")) > 0,
       "engagement bait caught (was in neither detector)")
expect(len(sg.detect("Big week for DACH hiring \U0001F680", "linkedin")) > 0,
       "emoji caught (was in neither detector)")
expect(len(sg.detect("Advisory firms are consolidating.", "linkedin")) > 0,
       "'firms' caught in linkedin context (was news-only)")

print("\n== 3. Long-standing tells still fire ==")
still = [
    "That's not a criticism, it's a gap.",
    "Those remain foundational. What has changed is the third requirement.",
    "That skill pairing now appears in senior role specifications.",
    "It sounds like back-office housekeeping. It is not.",
    "It works until it doesn't.",
    "Here's the reality: budgets are tight.",
    "We're seeing this play out in real time.",
    "Three of our recent mandates asked for this.",
    "That combination is rare in the DACH market.",
    "For candidates, act now. For hiring managers, wait.",
    "The talent landscape is shifting under everyone.",
    "Companies should leverage this window.",
]
for t in still:
    expect(len(sg.detect(t)) > 0, f"flagged: {t[:55]!r}")

print("\n== 4. Clean WJ-voice text passes (incl. the prompt's own good examples) ==")
clean = [
    # Example A from the live prompt
    "A Munich manufacturer spent five months trying to fill an S/4HANA finance "
    "role and kept turning down strong candidates. The sticking point was data "
    "residency: they wanted someone who had actually run a Swiss-German split "
    "and could argue the posting logic with their auditors. We found her in a "
    "company that had just been through the same migration.",
    # Example B from the live prompt
    "Salaries for SAP architects who can also stand up an AI integration have "
    "moved up roughly 15% in Frankfurt and Zurich over the past year. The "
    "premium goes to people who can sit between the SAP team and the data "
    "science team and translate in both directions. Companies are paying it "
    "because that gap is where their migrations stall.",
    # Ordinary clean commentary
    "Siemens cut 300 finance roles in Munich last quarter. Two clients read "
    "that as a green light to restructure their own teams. Candidates with "
    "consolidation experience will have the strongest hand over the next "
    "12-18 months.",
    # WJ boilerplate
    "Wolf Jansen is a specialist recruitment company focused on the DACH "
    "region. We have been recruiting in Germany since 2000.",
]
for i, t in enumerate(clean, 1):
    hits = sg.detect(t)
    expect(len(hits) == 0, f"clean sample {i} passes"
           + ("" if not hits else f" -> {sg.format_hits(hits)}"))

print("\n== 5. News bot end-to-end with mocked client ==")
import news_bot

BAD = json.dumps({
    "title": "SAP budgets tighten across the DACH mid-market",
    "excerpt": "The uncomfortable truth is that firms are increasingly cautious.",
    "body": "<p>This isn't about software, it's about talent. New research "
            "highlighted by Diginomica underscores the shift.</p>",
    "tags": ["sap"]})
GOOD = json.dumps({
    "title": "SAP budgets tighten across the DACH mid-market",
    "excerpt": "Four clients paused S/4HANA hiring this month. Contractor "
               "rates dropped 5 percent in the same period.",
    "body": "<p>Four of our clients paused S/4HANA hiring this month. "
            "Contractor rates dropped 5 percent in the same period. "
            "Candidates with brownfield migration experience are still "
            "choosing between offers.</p>",
    "tags": ["sap"]})


class FakeResp:
    def __init__(self, text):
        self.content = [type("B", (), {"text": text})()]


class FakeClient:
    """Generator replies pop in order; judge calls (Opus copy-chief system
    prompt) answer 'clean' by default so gen-call counts stay meaningful."""
    def __init__(self, replies, judge_replies=None):
        self.replies = list(replies)
        self.judge_replies = list(judge_replies or [])
        self.calls = []
        self.judge_calls = 0
        self.messages = self

    def create(self, **kw):
        if "copy chief" in (kw.get("system") or ""):
            self.judge_calls += 1
            reply = (self.judge_replies.pop(0) if self.judge_replies
                     else '{"violations": []}')
            return FakeResp(reply)
        self.calls.append(kw)
        return FakeResp(self.replies.pop(0))


story = {"title": "t", "source": "s", "link": "http://x", "summary": "sum",
         "division": "sap"}

c = FakeClient([BAD, GOOD])
r = news_bot.rewrite_story(story, c)
expect(r is not None, "news: returns a result")
expect(len(c.calls) == 2, f"news: 1 corrective retry made ({len(c.calls)} calls)")
corr = c.calls[1]["messages"][2]["content"]
expect('"' in corr and "uncomfortable truth" in corr.lower(),
       "news: correction QUOTES the offending fragment")
expect(r.get("style_warnings") == [], "news: clean final draft, no warnings")

c2 = FakeClient([BAD, BAD, BAD, BAD])
r2 = news_bot.rewrite_story(story, c2)
expect(len(c2.calls) == 4,
       "news: stubborn draft exhausts retries + one surgical repair attempt")
expect(len(r2.get("style_warnings", [])) > 0,
       "news: unresolved tells attached as style_warnings")

card = news_bot._post_card_html("tok", "T", "E", "<p>B</p>", "sap", None,
                                r2["style_warnings"])
expect("Style guard" in card, "news: warning strip renders on email card")
clean_card = news_bot._post_card_html("tok", "T", "E", "<p>B</p>", "sap")
expect("Style guard" not in clean_card, "news: no strip on clean card")

print("\n== 6. LinkedIn bot end-to-end with mocked client ==")
import linkedin_bot

BAD_LI = json.dumps({
    "post_text": "Here's the thing: firms are increasingly cautious. "
                 "The hiring signal is clear. Thoughts?\n\n#sap #dach",
    "hook": "x", "word_count": 20})
GOOD_LI = json.dumps({
    "post_text": "Four DACH clients paused S/4HANA hiring this month.\n\n"
                 "Contractor rates dropped 5 percent in the same period. "
                 "Candidates with brownfield migration experience are still "
                 "choosing between offers.\n\n#sap #dach",
    "hook": "Four DACH clients paused S/4HANA hiring this month.",
    "word_count": 30})

entry = {"division": "sap", "title": "t", "excerpt": "e", "body": "<p>b</p>"}
lc = FakeClient([BAD_LI, GOOD_LI])
lr = linkedin_bot.rewrite_for_linkedin(entry, None, lc)
expect(lr is not None and lr.get("style_warnings") == [],
       "linkedin: converges to clean draft")
expect(len(lc.calls) == 2, "linkedin: 1 corrective retry made")

lc2 = FakeClient([BAD_LI, BAD_LI, BAD_LI, BAD_LI])
lr2 = linkedin_bot.rewrite_for_linkedin(entry, None, lc2)
expect(len(lc2.calls) == 4,
       "linkedin: two retries + one surgical repair attempt")
expect(len(lr2.get("style_warnings", [])) > 0,
       "linkedin: unresolved tells attached as style_warnings")
expect("firm" not in lr2["post_text"].split("#")[0]
       or "company" in lr2["post_text"],
       "linkedin: firm→company scrub applied to post text")

lcard = linkedin_bot._linkedin_card_html("tok", "text", entry, None, None,
                                         lr2["style_warnings"])
expect("Style guard" in lcard, "linkedin: warning strip renders on email card")

print("\n== 7. Semantic judge layer + repair convergence ==")
# JF quotes a fragment that appears in BAD's excerpt, so a repair that removes
# it is detectably successful; JF2 quotes text not present anywhere (a fresh
# subjective opinion on the re-read).
JF = json.dumps({"violations": [{"quote": "puts a number on something leaders have felt",
                                 "problem": "meta setup frame"}]})
JP = json.dumps({"violations": []})
jc = FakeClient([GOOD, GOOD], judge_replies=[JF, JP])
jr = news_bot.rewrite_story(story, jc)
expect(len(jc.calls) == 2 and jc.judge_calls == 2,
       "regex-clean draft rewritten on judge verdict alone")
expect(jr.get("style_warnings") == [], "judge-clean second draft carries no warnings")

# Reproducibly-dirty: judge returns the same finding on every pass, including
# the final stability pass -> it must surface. (1 initial + 2 rewrites +
# 3 adopted repairs = 6 gen calls; 6 checks + 1 stability = 7 judge calls.)
jc2 = FakeClient([GOOD] * 6, judge_replies=[JF] * 7)
jr2 = news_bot.rewrite_story(story, jc2)
expect(any(w.startswith("judge:") for w in jr2.get("style_warnings", [])),
       "reproducible judge finding still surfaces in style_warnings")

# One-off opinion: same flow but the stability pass disagrees -> suppressed.
jc3 = FakeClient([GOOD] * 6, judge_replies=[JF] * 6 + [JP])
jr3 = news_bot.rewrite_story(story, jc3)
expect(jr3.get("style_warnings") == [],
       "non-reproducible judge finding suppressed by stability filter")

# Repair acceptance is fragment-based: a repair that removes the quoted
# fragment is adopted even when the judge finds NEW findings on the re-read.
GOOD_D = json.loads(GOOD)
BAD_FRAG = dict(GOOD_D)
BAD_FRAG["excerpt"] = "This puts a number on something leaders have felt for years."
JF2 = json.dumps({"violations": [{"quote": "Contractor rates dropped 5 percent",
                                  "problem": "fresh subjective nitpick"}]})
# initial JF -> rewrites return BAD_FRAG again (JF, JF: no improvement) ->
# repair returns GOOD (fragment gone) -> adopted -> post-repair check JF2
# (new, different) -> round 2 repair returns GOOD again (JF2 fragment still
# present -> NOT adopted, break) -> stability pass JP -> warnings empty.
jc4 = FakeClient([json.dumps(BAD_FRAG)] * 2 + [GOOD] * 3,
                 judge_replies=[JF, JF, JF, JF2, JP])
jr4 = news_bot.rewrite_story(story, jc4)
expect(jr4["excerpt"] == GOOD_D["excerpt"],
       "repair adopted on fragment removal despite new judge findings")
expect(jr4.get("style_warnings") == [],
       "new one-off finding then suppressed by stability filter")

expect(sg.judge_draft(FakeClient([], judge_replies=["not json at all"]),
                      {"body": "x"}) == [],
       "unparseable judge output never blocks the pipeline")

print("\n== 8. Convergence helper units ==")
expect(sg.extract_fragment('judge: meta frame: "the tell here"') == "the tell here",
       "extract_fragment pulls the quoted text")
expect(not sg.fragments_gone(['x: "Contractor rates dropped 5 percent"'],
                             "<p>Contractor rates dropped 5 percent in Q1</p>"),
       "fragments_gone false while fragment present")
expect(sg.fragments_gone(['x: "Contractor rates dropped 5 percent"'],
                         "<p>Rates for contractors moved down.</p>"),
       "fragments_gone true after rewrite")
stable = sg.reproducible_judge_tells(
    ['judge: p: "the hiring signal is clear to everyone watching"'],
    [{"fragment": "the hiring signal is clear"}])
expect(len(stable) == 1, "overlapping second-pass finding counts as stable")
expect(sg.reproducible_judge_tells(
    ['judge: p: "the hiring signal is clear to everyone watching"'],
    [{"fragment": "a completely different sentence about margins"}]) == [],
       "non-overlapping second-pass finding is dropped")

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("ALL TESTS PASSED")
