"""Headline linter + headline desk tests. Run: python3 test_headlines.py"""
import json
import style_guard as sg

failures = []


def expect(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        failures.append(msg)


# Every headline below is REAL: published on wolfjansen.com in the fortnight to
# 2026-09-28, or sent in the Aug 17-25 digests. They are the problem.
REAL_AI_HEADLINES = [
    "The audit trail for AI agents is becoming a job requirement",
    "SAP Innovation Awards open for 2027, and the winning entries tend to share one trait",
    "SAP's accruals agent changes who finance teams need on S/4HANA projects",
    "SAP's direct spend push is creating a new kind of procurement hire",
    "The gap between SAP automation plans and the people who can run them",
    "Retail and supply chain AI projects are stalling at the same point",
    "The RFP process is where SAP projects quietly fail",
    "Why Pentair's CFO hire signals a shift in what industrial companies want from finance leadership",
    "The agentic AI role nobody is hiring for yet",
    "SAP's ethics policy update tells us something about where AI hiring is headed",
    "When the service desk disappears, where do SAP operations careers go?",
    "The AI pilot that worked is about to become your hiring problem",
    "SAP's AI agent push just rewrote the job spec for transformation managers",
    "The CFO who signs off on AI agents may not understand what they do",
    "Board AI oversight is creating a new filter for senior finance hires",
    "AI token budgets are becoming a finance skill, and most SAP teams are not ready",
    "SAP's CX dominance on G2 is reshaping what companies ask for in customer experience hires",
    "Why LATAM cloud migrations are landing on DACH recruiter desks",
    "SAP's HR cloud dominance is starting to show up in hiring briefs",
    "CFOs are trusting AI for decisions they used to trust consultants for",
    "SAP's new AppHaus signals something about how enterprise software gets built now",
    "The CFOs who got burned on AI are now the hardest to hire for",
    "Reckitt's AI team sits inside shared services, and that changes who gets hired",
    "When the people building AI start asking everyone to slow down, hiring managers should pay attention",
    "SAP just made machine learning accessible to teams that never had a data scientist",
    "Daikin's ERP overhaul puts a question to every manufacturer running disconnected systems",
    "OpenAI's data agent will reshape what companies expect from their analysts",
    "The AI governance role nobody planned for is now appearing in data team specs",
    "SAP's HR automation push will surface a familiar hiring problem",
    "Four critical SAP vulnerabilities, and the skill most companies are missing",
    "The CFO who can close a deal in six weeks is worth more than the one who needs six months",
    "AI agents are an infrastructure bet, and CFOs are learning that late",
    "When AI agents need access controls, who writes the policy?",
    "Agent-to-agent protocols are about to reshape who gets hired for AI integration roles",
    "Oracle's infrastructure spending spree is creating a very specific hiring problem",
    "SAP certifications still open doors, but the questions hiring managers ask have changed",
    "When boards start using AI to ask questions, finance teams have to answer differently",
    "SAP's unified service model is quietly rewriting the support engineer job spec",
    "The cost of embedding AI in SAP just dropped, and it changes who gets hired",
    "SAP's AI pivot is reshaping what 'SAP expertise' actually means",
    # Aug 17-25 digests
    "SAP's industry-specific AI push is about to change what clients ask for in their next hire",
    "The finance teams hiring AI specialists are also hiring someone to watch them",
    "The AI budget line everyone miscalculates",
    "Candidates are running ChatGPT during live interviews, and hiring managers are split on what to do about it",
    "Why consumer goods companies keep poaching finance leaders from tobacco",
    "When a 190,000-person company consolidates HR onto SAP, the ripple reaches every consulting team in DACH",
    "Knowledge graph specialists are about to become much harder to find",
    "The aviation industry's data problem is a hiring problem in disguise",
    "Oracle is steering enterprise AI toward orchestration economics",
    "Rate hike risk is filtering into CFO hiring conversations",
    "The Tricolor collapse and why boards are asking sharper questions about CFO candidates",
    "SAP's agentic AI push changes which consultants get shortlisted",
    "Carve-out assessments just dropped from six weeks to seven days. Here's who benefits.",
    "When the dashboard builder becomes the AI validator",
    "The middle management squeeze is about to shape who gets promoted",
    "The quiet erosion of domain expertise in hiring",
    "The infrastructure gap holding back enterprise AI just got smaller",
]

# Plain trade-press headlines, built only from facts in those same stories.
# These must ALL pass, or the linter is too strict to be usable.
PLAIN_HEADLINES = [
    "SAP and PwC cut carve-out assessments to seven days",
    "NTT Data moves 190,000 staff onto SAP SuccessFactors",
    "Oakley Capital takes majority stake in Graphwise",
    "Coty hires its new CFO from British American Tobacco",
    "Managers' teams have grown by nearly half, Gallup finds",
    "SEC charges former Tricolor executives over $1.9bn collapse",
    "Fed minutes show officials open to a rate rise",
    "Airlines lost 33 million bags last year",
    "Hiring managers split over candidates using ChatGPT in interviews",
    "Inference costs push AI budgets over forecast",
    "Reckitt moves its AI team into shared services",
    "Four critical vulnerabilities found in SAP NetWeaver",
    "SAP opens entries for its 2027 Innovation Awards",
    "SAP adds an accruals agent to S/4HANA finance",
    "OpenAI launches a data agent for analysts",
    "SAP publishes an updated AI ethics policy",
    "G2 ranks SAP first for customer experience software",
    "Board AI oversight adds a question to CFO interviews",
    "Pentair appoints a new CFO from outside the industry",
    "Daikin replaces regional ERP systems with S/4HANA",
]

print("== 1. Linter catches the real AI headlines ==")
missed = []
for h in REAL_AI_HEADLINES:
    tells = sg.headline_tells(h)
    if not tells:
        missed.append(h)
caught = len(REAL_AI_HEADLINES) - len(missed)
print(f"  caught {caught}/{len(REAL_AI_HEADLINES)}")
for m in missed:
    print(f"    MISSED: {m}")
expect(not missed, "every real AI headline from the feed is caught")

print("\n== 2. Linter passes plain trade-press headlines ==")
for h in PLAIN_HEADLINES:
    tells = sg.headline_tells(h)
    expect(not tells, f"{h!r}" + ("" if not tells else f" -> {tells}"))

print("\n== 3. Similarity guard ==")
expect(sg.headline_similarity("SAP and PwC cut carve-out assessments to seven days",
                              "SAP and PwC cut carve-out assessments to seven days") == 1.0,
       "identical headline scores 1.0")
expect(sg.headline_similarity("NTT Data moves 190,000 staff onto SAP SuccessFactors",
                              "NTT DATA Selects SAP SuccessFactors to Power Global HR") < 0.5,
       "a genuine rewrite scores well below the copy threshold")

if __name__ == "__main__" and "--linter-only" in __import__("sys").argv:
    raise SystemExit(1 if failures else 0)

print("\n== 4. Headline desk (mocked model) ==")
import news_bot


class FakeResp:
    def __init__(self, text):
        self.content = [type("B", (), {"text": text})()]


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return FakeResp(self.replies.pop(0))


story = {"title": "NTT DATA Selects SAP SuccessFactors to Power Global HR",
         "source": "SAP News Centre", "link": "http://x",
         "summary": "NTT DATA will move its 190,000 employees onto SAP SuccessFactors.",
         "division": "sap"}
draft = {"title": "working title", "excerpt": "e",
         "body": "<p>NTT DATA is moving 190,000 people onto SuccessFactors.</p>"}

# (a) first plain candidate wins, AI ones skipped
c = FakeClient([json.dumps({"headlines": [
    "NTT Data's HR push is reshaping who gets hired",
    "NTT Data moves 190,000 staff onto SAP SuccessFactors",
    "NTT Data picks SuccessFactors for global HR"]})])
title, warn = news_bot.write_headline(c, draft, story, [])
expect(title == "NTT Data moves 190,000 staff onto SAP SuccessFactors",
       f"first clean candidate chosen ({title!r})")
expect(warn == [], "no warnings when a clean candidate exists")
expect(len(c.calls) == 1, "one model call when the first batch has a clean candidate")

# (b) all dirty first time -> retry lists the rejects -> clean second batch
c = FakeClient([
    json.dumps({"headlines": ["NTT Data's HR push changes who gets hired",
                              "Why NTT Data's move matters"]}),
    json.dumps({"headlines": ["NTT Data moves its global HR onto SuccessFactors"]})])
title, warn = news_bot.write_headline(c, draft, story, [])
expect(title == "NTT Data moves its global HR onto SuccessFactors" and warn == [],
       f"retry produced a clean headline ({title!r})")
retry_prompt = c.calls[1]["messages"][0]["content"]
expect("REJECTED" in retry_prompt and "changes who gets hired" in retry_prompt,
       "retry prompt quotes the rejected headlines and why")

# (c) both batches dirty -> fall back to the source's human-written headline,
#     provided it passes the linter
c = FakeClient([json.dumps({"headlines": ["Why this matters"]}),
                json.dumps({"headlines": ["The HR shift nobody saw coming"]})])
title, warn = news_bot.write_headline(c, draft, story, [])
expect(title == story["title"] and warn == [],
       f"falls back to the clean source headline ({title!r})")

# (d) near-copy of the source is not accepted as our own headline
c = FakeClient([json.dumps({"headlines": [
    "NTT DATA selects SAP SuccessFactors to power global HR",
    "NTT Data moves 190,000 staff onto SAP SuccessFactors"]})])
title, _ = news_bot.write_headline(c, draft, story, [])
expect(title == "NTT Data moves 190,000 staff onto SAP SuccessFactors",
       f"near-copy of the source skipped ({title!r})")

# (e) sibling titles reach the prompt so the batch varies
c = FakeClient([json.dumps({"headlines": ["NTT Data moves 190,000 staff onto SAP SuccessFactors"]})])
news_bot.write_headline(c, draft, story, ["Coty hires its new CFO from British American Tobacco"])
expect("Coty hires its new CFO" in c.calls[0]["messages"][0]["content"],
       "sibling headlines are passed to the desk")

# (f) the model fails entirely -> a usable headline still comes back
class Dead:
    class messages:
        @staticmethod
        def create(**kw):
            raise RuntimeError("api down")
title, warn = news_bot.write_headline(Dead(), draft, story, [])
expect(bool(title), f"API failure still yields a headline ({title!r})")

# (g) worst case: everything dirty, including the source -> least-bad
#     candidate, with its problems surfaced rather than hidden
dirty_story = dict(story, title="SAP Unveils Game-Changing AI to Transform HR Forever")
c = FakeClient([json.dumps({"headlines": ["Why this matters for everyone"]}),
                json.dumps({"headlines": ["NTT Data's HR push is quietly reshaping hiring"]})])
title, warn = news_bot.write_headline(c, draft, dirty_story, [])
expect(bool(warn), f"unresolvable headline carries warnings ({warn[:2]})")

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("ALL TESTS PASSED")
