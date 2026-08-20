"""
Wolf Jansen Style Guard (shared detector)
=========================================
One unified detector for BOTH bots, replacing the two divergent in-file
pattern lists (news_bot._AI_TELL_PATTERNS and linkedin_bot's
_detect_banned_patterns). Each bot previously covered tells the other
missed; this module is the union of both, plus patterns for tells found
leaking in published posts on 2026-08-17:

  - "highlighted by"           (old regex only matched highlights/highlighting)
  - "increasingly"             (was LinkedIn-only)
  - "The uncomfortable truth is" and cousins
  - "The practical question for X is whether" (old regex only matched
    "the real/underlying question")
  - "has never been about/in X" cross-sentence reversal setups

detect(text, context) returns a list of {"label", "fragment"} dicts so the
corrective re-prompt can QUOTE the offending fragment back to the model
instead of naming an abstract pattern. Empty list means clean.

Aligned to ABOUT WOLF JANSEN/context-os/ANTI AI WRITING STYLE.md.
No third-party dependencies.
"""

import re

# ---------------------------------------------------------------------------
# Pattern table: (label, compiled_regex)
# Hits trigger a corrective rewrite, not a hard reject, so broad coverage is
# acceptable; false positives cost one retry, false negatives cost the brand.
# ---------------------------------------------------------------------------

def _c(pat, flags=re.IGNORECASE):
    return re.compile(pat, flags)


_COMMON_PATTERNS = [
    # --- Negative parallelisms / contrastive reversals ---------------------
    ("neg-parallel 'not just X'",            _c(r"\bnot just\b")),
    ("neg-parallel 'not only X but'",        _c(r"\bnot only\b[^.!?]*\bbut\b")),
    ("neg-parallel 'X, not Y'",              _c(r",\s+not\s+(?:a |an |the |just )?\w+")),
    ("neg-parallel \"isn't X, it's Y\"",     _c(r"\bis(?:n[o']?t| not)\b[^.!?]*,\s*(?:it'?s|its|but|rather)\b")),
    ("neg-parallel \"not X, it's/but Y\"",   _c(r"\bnot\s+(?:a\s+|an\s+|the\s+)?[a-z][^,.!?\n]{2,60},\s*(?:it'?s|it is|but(?:\s+rather)?|rather)\b")),
    ("neg-parallel 'isn't about/just X'",    _c(r"\b(?:isn'?t|aren'?t)\s+(?:about|just|merely|simply)\b")),
    ("neg-parallel 'no longer X but/it's Y'", _c(r"\bno longer\b[^.!?]*\b(?:but|it'?s|its|they'?re|rather)\b")),
    ("'no longer sufficient/enough'",        _c(r"\bno longer\s+(?:sufficient|enough|adequate)\b")),
    ("'not sufficient / alone is not enough'", _c(r"\b(?:not sufficient|alone is not enough)\b")),
    ("'on the surface ... but'",             _c(r"\bon the surface\b")),
    ("'shifted/changed rather than'",        _c(r"\b(?:shifted|changed|moved|grown)\s+rather than\b")),
    ("cross-sentence \"isn't Y. It's Z.\"",  _c(r"\b(?:isn['']t|is not|aren['']t|are not|wasn['']t|was not)\b"
                                                r"[^.?!\n]{1,120}[.?!]\s+"
                                                r"(?:It['']?s|It is|That['']?s|That is|They['']?re|They are|Those are)\b")),
    ("reversal copula 'is X, not Y'",        _c(r"\b(?:is|are|was|were)\s+[a-z][^.?!\n]{2,80},\s+not\s+[a-zA-Z]")),
    ("'Less about X. More about Y.'",        _c(r"\bLess\s+about\b[^.?!\n]{1,90}[.?!]\s+More\s+about\b")),
    ("'This isn't' opener",                  _c(r"\bThis\s+(?:isn['']t|is not)\b")),
    ("'has never been about/in X' setup",    _c(r"\b(?:has|have|had|was|were)\s+never\s+been\s+(?:about|in|the|a|an)\b")),
    ("then/now '[time] ago, X. Now Y.'",     _c(r"\b\w+\s+(?:months?|years?|quarters?|weeks?|decades?)\s+ago\b"
                                                r"[^.?!\n]{1,120}[.?!]\s+(?:Now|Today)\b")),
    ("'has moved from X to Y' contrast",     _c(r"\b(?:has|have)\s+moved\s+from\s+[^.?!\n]{2,80}\s+to\s+\w+")),
    # --- Short 'profound' fragment tells -----------------------------------
    ("fragment \"until it doesn't\"",        _c(r"until it doesn'?t")),
    ("fragment 'is a symptom'",              _c(r"\bis a symptom\b")),
    ("fragment 'litmus test'",               _c(r"\blitmus test\b")),
    ("fragment 'the cracks show'",           _c(r"\bthe cracks show\b")),
    ("fragment 'framing matters'",           _c(r"\bframing matters\b")),
    ("fragment 'was never the hard part'",   _c(r"\bwas never the (?:hard part|main constraint|point)\b")),
    ("cliché 'table stakes'",                _c(r"\btable stakes\b")),
    ("aphorism 'pays the bills'",            _c(r"\bpays?\s+the\s+bills\b")),
    ("fragment 'The new variable is X'",     _c(r"\bthe\s+new\s+variable\s+is\b")),
    ("cliché 'the calculus has changed'",    _c(r"\bthe\s+calculus\s+has\s+(?:changed|shifted)\b")),
    ("cliché 'raises the bar'",              _c(r"\brais(?:es?|ing)\s+the\s+bar\b")),
    ("cliché 'did not sign up for'",         _c(r"\b(?:did|didn'?t)\s+(?:not\s+)?sign\s+up\s+for\b")),
    ("abstract 'the floor has risen'",       _c(r"\b(?:floor|bar|baseline)\s+has\s+(?:risen|moved|shifted)\b")),
    ("contrast 'bears little resemblance'",  _c(r"\bbears?\s+little\s+resemblance\b")),
    ("puffery 'significant shift'",          _c(r"\b(?:represents?|marks?|signals?)\s+a\s+significant\b|\bsignificant\s+shift\b")),
    ("setup 'one pattern stands out'",       _c(r"\b(?:one\s+)?pattern\s+stands\s+out\b|\bmirrors?\s+a\s+(?:pattern|broader)\b")),
    ("closer 'the ones who / separate themselves'", _c(r"\bare\s+the\s+ones\s+who\b|\bwill\s+(?:be\s+the\s+ones|separate\s+themselves)\b")),
    # --- Rhetorical setups / meta-narration --------------------------------
    ("'the [adj] question is/becomes'",      _c(r"\bthe\s+(?:\w+\s+){0,2}question\b[^.!?]{0,90}\b(?:is|becomes|remains)\b")),
    ("'the implication/takeaway is clear/direct'", _c(r"\bthe (?:\w+\s+)?(?:implication|takeaway|lesson|message)s? (?:is|are|here is) (?:clear|direct|obvious|simple|stark|straightforward)\b")),
    ("\"Here's the/what/who\"",              _c(r"here'?s (?:the|what|who|why|where|how)\b")),
    ("setup 'the uncomfortable/hard truth'", _c(r"\bthe\s+(?:uncomfortable|hard|simple|inconvenient|blunt)\s+truth\b|\bthe\s+reality\s+is\b")),
    ("setup \"what's interesting is\"",      _c(r"\bwhat[']?s\s+(?:really\s+|particularly\s+)?"
                                                r"(?:interesting|notable|striking|telling|surprising)\s+is\b"
                                                r"|\bwhat\s+stands?\s+out\s+(?:here\s+)?is\b"
                                                r"|\bthe\s+interesting\s+thing\s+(?:here\s+)?is\b")),
    ("setup 'what makes X worth/notable'",   _c(r"\bwhat\s+makes\s+[A-Za-z][^.?!\n]{2,80}\s+"
                                                r"(?:worth\s+(?:watching|noting|paying|heeding|reading)|"
                                                r"interesting|notable|different|stand\s+out|stands?\s+out)\b")),
    ("meta 'play(ing) out'",                 _c(r"\bplay(?:ing|ed)? out\b")),
    ("meta 'unfold'",                        _c(r"\bunfold(?:s|ing|ed)?\b")),
    ("meta 'in real time'",                  _c(r"\bin real[\s-]?time\b")),
    ("meta 'we are watching'",               _c(r"\bwe(?:'re| are) watching\b")),
    ("'the/clear signal'",                   _c(r"\bthe signal\b|\bclear signal\b")),
    ("'is a [adj] signal'",                  _c(r"\b(?:is|are|that[']?s|this\s+is|it[']?s)\s+(?:a|an)\s+(?:\w+\s+)?signal\b")),
    ("tic 'into the conversation'",          _c(r"\binto\s+the\s+conversation\b")),
    # --- Vague "we're seeing" observation openers --------------------------
    ("filler 'we are fielding X'",           _c(r"\bwe\s+are\s+(?:already\s+)?fielding\b|\bfielding\s+(?:briefs?|mandates?|requests?|searches|enquiries|calls)\b")),
    ("vague 'we keep seeing/hearing'",       _c(r"\bwe\s+keep\s+(?:seeing|hearing|noticing|finding)\b")),
    ("filler 'reflect(s) this shift'",       _c(r"\breflect(?:s|ing)?\s+this\s+shift\b")),
    ("vague 'we are seeing/observing'",      _c(r"\bwe(?:'re| are)\s+(?:already\s+|increasingly\s+|now\s+|also\s+|currently\s+|still\s+)?(?:seeing|observing|noticing|hearing|witnessing|learning|finding)\b")),
    ("vague 'we have seen/noticed'",         _c(r"\bwe(?:'ve| have)\s+(?:been\s+)?(?:seen|noticed|observed|tracked|tracking|been seeing|been tracking|started seeing)\b")),
    ("vague 'we see this/it'",               _c(r"\bwe see (?:this|it)\b")),
    # --- Significance / legacy puffery -------------------------------------
    ("puffery 'stands/serves as'",           _c(r"\b(?:stands|serves)\s+as\b")),
    ("puffery 'is a testament/reminder'",    _c(r"\bis a (?:testament|reminder)\b")),
    ("puffery 'underscores/highlights'",     _c(r"\b(?:underscore|underscores|underscored|underscoring|highlights|highlighted|highlighting)\b")),
    ("puffery 'pivotal/turning point'",      _c(r"\b(?:pivotal|key turning point)\b")),
    ("puffery 'evolving landscape'",         _c(r"\bevolving landscape\b")),
    ("puffery 'marks a shift'",              _c(r"\bmarks? a shift\b")),
    ("puffery 'setting the stage'",          _c(r"\bsetting the stage\b")),
    ("puffery 'reflects broader'",           _c(r"\breflect(?:s|ing)?\s+(?:a\s+)?broader\b")),
    ("puffery 'deeply rooted/focal point'",  _c(r"\bdeeply rooted\b|\bfocal point\b|\bindelible\b")),
    # --- Superficial present-participle tails ------------------------------
    ("participle tail ', -ing'",             _c(r",\s*(?:highlighting|underscoring|emphasi[sz]ing|reflecting|symboli[sz]ing|signal(?:l)?ing|ensuring|fostering|demonstrating|showcasing|cementing|solidifying|reinforcing|contributing to|encompassing)\b")),
    # --- Vague attribution --------------------------------------------------
    ("vague attribution",                    _c(r"\b(?:industry reports?|observers (?:have )?(?:cited|noted)|experts (?:argue|say|agree|believe|warn)|analysts (?:say|suggest|believe|argue)|some critics|widely (?:regarded|seen|considered))\b")),
    ("vague 'than benchmarks/data suggest'", _c(r"\bthan\s+(?:benchmarks?|the data|the numbers?|metrics?|reports?)\s+suggests?\b|\bfaster than expected\b")),
    # --- Outline conclusions / summaries / didactic ------------------------
    ("outline 'despite challenges/success'", _c(r"\bdespite (?:its|their|these) (?:challenges|success)\b")),
    ("outline 'future outlook'",             _c(r"\bfuture outlook\b")),
    ("'in summary/conclusion/overall'",      _c(r"\b(?:in summary|in conclusion|overall,)\b")),
    ("didactic 'it's important to note'",    _c(r"\bit'?s (?:important|crucial|worth)\s+(?:to note|noting|remembering|considering)\b")),
    ("'worth noting/heeding'",               _c(r"\bworth (?:noting|heeding|paying attention)\b")),
    # --- AI vocabulary -------------------------------------------------------
    ("ai-vocab",                             _c(r"\b(?:leverage|leverages|leveraging|delve|delving|tapestry|vibrant|"
                                                r"seamless|seamlessly|garner(?:ed|ing)?|foster(?:s|ing|ed)?|"
                                                r"showcas(?:e|es|ing|ed)|intricat(?:e|ies)|interplay|realm|"
                                                r"navigat(?:e|es|ing|ed)|underpin(?:s|ned|ning)?|myriad|bespoke|"
                                                r"robust|increasingly|ecosystem|groundbreaking|nestled|boasts?)\b")),
    ("ai-vocab 'landscape' (metaphor)",      _c(r"\b(?:hiring|talent|recruitment|competitive|business|market|tech(?:nology)?|regulatory|AI)\s+landscape\b|\blandscape\s+(?:is|has|of)\b")),
    ("corporate filler phrase",              _c(r"\b(?:deep dive|double down|moving the needle|in today'?s fast[\s-]paced|in the broader context)\b")),
    # --- Reworded pivots / abstract-motion ---------------------------------
    ("'the X signal'",                       _c(r"\bthe\s+\w+\s+signal\b")),
    ("'the X is clear' sign-off",            _c(r"\bthe\s+\w+\s+is\s+clear\b")),
    ("abstract-motion 'X is/are tilting/widening'", _c(r"\bthe\s+\w+\s+(?:is|are)\s+(?:tilting|shifting|narrowing|widening|closing|shrinking)\b|\b(?:is|are)\s+shifting\s+accordingly\b")),
    ("metaphor 'the window' (closing)",      _c(r"\bthe window\b[^.!?]*\b(?:narrow|clos|shrink)")),
    ("fragment 'X matters' pronouncement",   _c(r"\b(?:that|this|the timing|the distinction|the difference|the nuance|the gap|the context)\s+matters\b")),
    ("reworded 'neither X ... but'",         _c(r"\bneither\b[^.!?]*\bbut\b")),
    ("reworded 'is/are (not) wrong, but'",   _c(r"\b(?:is|are)\s+(?:not\s+)?wrong,\s*but\b")),
    ("setup 'the subtext/real story is'",    _c(r"\bthe\s+(?:subtext|real story)\s+is\b")),
    ("setup 'framing is about'",             _c(r"\bframing is about\b")),
    # --- Templated WJ evidence / cross-post repetition ----------------------
    ("rule-of-three placement claim",        _c(r"\bthree\b[^.!?]{0,40}\b(?:mandates?|briefs?|roles?|placements?|consultants?|hires?|searches?|candidates?|clients?)\b")),
    ("'(N) of our last/recent three'",       _c(r"\bof our (?:last|recent)\s+three\b")),
    ("'we (have) placed three'",             _c(r"\bwe(?:'ve| have)?\s+placed\s+three\b")),
    ("novelty 'did not exist ... ago'",      _c(r"\b(?:did|does)\s*n[o']?t\s+exist\b[^.!?]{0,80}\bago\b|\bago\b[^.!?]{0,80}\b(?:did|does)\s*n[o']?t\s+exist\b")),
    ("novelty 'barely existed / would not have'", _c(r"\bbarely existed\b|\bwould not have (?:existed|appeared)\b")),
    ("'rare/scarce skill combination'",      _c(r"\bcombination\b[^.!?]{0,30}\b(?:is|are|was|were|remains?)\s+(?:rare|scarce|uncommon|hard(?:er)? to find)\b|\b(?:rare|scarce|uncommon)\s+(?:skill\s+)?combination\b")),
    ("'profiles/candidates are scarce'",     _c(r"\b(?:profile|profiles|candidates?|specialists?)\b[^.!?]{0,20}\b(?:are|is|remain|remains)\s+(?:scarce|rare|uncommon|thin on the ground)\b")),
    ("dual 'for candidates ... for hiring'", _c(r"for candidates\b.{0,600}for hiring managers\b|for hiring managers\b.{0,600}for candidates\b", re.IGNORECASE | re.DOTALL)),
    ("opener 'In our X practice'",           _c(r"\bIn\s+our\s+[A-Z][A-Za-z&\s]{1,30}\s+practice\b", 0)),
    # --- Concessive pivot (concede-then-elevate) ----------------------------
    ("concessive 'remain(s) foundational/useful'", _c(r"\bremains?\s+(?:foundational|essential|valuable|important|critical|central|relevant|useful|helpful|necessary|the baseline)\b")),
    ("concessive 'the differentiator is'",   _c(r"\bthe\s+(?:differentiator|separator|dividing\s+line)\s+is\b")),
    ("concessive 'those/these remain/still'", _c(r"\b(?:those|these)\s+(?:remain\b|still\s+(?:matter|count|apply|hold))")),
    ("concessive 'still matters/counts'",    _c(r"\bstill\s+(?:matters?|counts?|applies|hold(?:s)? true)\b")),
    ("concessive 'what has changed is'",     _c(r"\bwhat(?:'s| has| have)?\s+changed\s+is\b")),
    ("concessive 'become the separator'",    _c(r"\bbecom(?:e|es|ing)\s+the\s+(?:separator|differentiator|dividing line)\b")),
    # --- Pointer-sentence ----------------------------------------------------
    ("pointer-sentence 'That/This X signals'", _c(r"(?:^|[.!?]\s+)(?:that|this|such)\s+(?:\w+\s+){0,4}(?:also\s+)?(?:signals?|means\b|matters\b|appears?\b|creates?|changes?|translates?\s+into|reflects?|reshapes?|underscores?|mirrors?|(?:is|are)\s+significant)\b")),
    # --- Dismiss-then-elevate opener ----------------------------------------
    ("dismiss 'sounds like plumbing/boring'", _c(r"\bsounds?\s+like\b[^.!?]*\b(?:plumbing|housekeeping|boring|mundane|dull|a footnote)\b")),
    ("dismiss 'Another X, another Y'",       _c(r"\banother\b[^,.!?]{0,40},\s*another\b")),
    # --- House style ---------------------------------------------------------
    ("'firm'/'firms'",                       _c(r"\bfirms?\b")),
    ("specific year reference",              _c(r"\b(?:in|by|through|during|until|into)\s+20(?:2[0-9]|3\d)\b|\bQ[1-4]\s+20(?:2[0-9]|3\d)\b|\b20(?:2[0-9]|3\d)[-–]20(?:2[0-9]|3\d)\b")),
    ("em dash leak",                         _c(r"—")),
]

# LinkedIn-only additions (company-page format rules)
_LINKEDIN_PATTERNS = [
    ("engagement bait",                      _c(r"\bagree\?|\bthoughts\?|share\s+your\s+thoughts|\bdm\s+me\b|what\s+do\s+you\s+think\?|drop\s+a\s+comment|let\s+us\s+know\b")),
    ("linkedin cliché",                      _c(r"\b(?:excited\s+to\s+share|thrilled\s+to|humbled|proud\s+to\s+announce|exciting\s+times)\b")),
]

_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _plain(text: str) -> str:
    """Strip HTML tags, normalise whitespace and curly apostrophes."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = text.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text)


def detect(text: str, context: str = "news") -> list:
    """
    Scan plain or HTML text for banned AI-writing tells.
    context: "news" or "linkedin" (adds emoji/engagement/cliché checks).
    Returns a list of {"label": ..., "fragment": ...} dicts. Empty = clean.
    """
    plain = _plain(text)
    found = []
    seen = set()

    patterns = list(_COMMON_PATTERNS)
    if context == "linkedin":
        patterns += _LINKEDIN_PATTERNS

    for label, pat in patterns:
        m = pat.search(plain)
        if m:
            frag = m.group(0).strip()[:90]
            key = (label, frag.lower())
            if key not in seen:
                seen.add(key)
                found.append({"label": label, "fragment": frag})

    # 'brief'/'briefs' acceptable once; flag overuse.
    briefs = re.findall(r"\bbriefs?\b", plain, re.IGNORECASE)
    if len(briefs) > 1:
        found.append({"label": "overused 'brief'/'briefs' (appears more than once)",
                      "fragment": f"'brief' x{len(briefs)}"})

    if context == "linkedin":
        m = _EMOJI_RE.search(plain)
        if m:
            found.append({"label": "emoji (banned in posts)", "fragment": m.group(0)})

    return found


def detect_fields(result: dict, fields=("title", "excerpt", "body"),
                  context: str = "news") -> list:
    """Detect across several dict fields; fragments keep their field name."""
    found = []
    for f in fields:
        v = result.get(f) if isinstance(result, dict) else None
        if isinstance(v, str) and v:
            for hit in detect(v, context=context):
                hit["field"] = f
                found.append(hit)
    return found


def format_hits(hits: list) -> list:
    """Human-readable one-liners for logs, correction prompts and emails."""
    out, seen = [], set()
    for h in hits:
        s = f'{h["label"]}: "{h["fragment"]}"'
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def correction_preamble(hits: list) -> str:
    """Quoted-fragment list to open a corrective re-prompt with."""
    lines = ["Your draft contains banned AI writing tells. The automated "
             "checker found these EXACT fragments (quoted from your draft):\n"]
    for s in format_hits(hits)[:14]:
        lines.append(f"  - {s}")
    lines.append(
        "\nRewrite ONLY the offending sentences so every fragment above is "
        "gone. Do not introduce new banned patterns while fixing these. Keep "
        "all other sentences, the same facts, length and format."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Semantic style judge (Opus)
# ---------------------------------------------------------------------------
# The regex layer above catches exact phrasings; it cannot catch the same
# rhetoric reworded. On 2026-08-18 all four drafts passed every regex while
# being full of paraphrased tells ("fielding searches", "Here's who benefits",
# "the implication is direct", "remain useful, and the differentiator is") and
# batch-level templating (a Munich client anecdote in the same slot in three
# posts, every post closing on the same forward-looking prediction). The judge
# reads each draft the way an editor does — for the MOVE, not the words — and
# sees the sibling drafts so cross-post repetition is visible.

JUDGE_MODEL = "claude-opus-4-5-20251101"

_JUDGE_PROMPT = """You are the copy chief at Wolf Jansen, a specialist \
recruitment company. You review draft posts before publication and reject \
AI-sounding rhetoric. You judge the underlying rhetorical MOVE, not the exact \
words — paraphrases of a banned move are equally banned.

BANNED MOVES (any wording):
1. Contrastive reversal: defining a thing by what it is not, in one sentence \
or across two ("Rather than X, ... now Y", "not X, it's Y", "X. The real \
story is Y", "has never been about X"). The affirmative point must stand alone.
2. Concessive pivot: conceding the old thing then elevating the new ("X \
remains useful, and/but the differentiator is Y", "X still matters. What has \
changed is Y").
3. Pointer sentence: opening a sentence with That/This/Such + abstract noun \
just to announce the previous sentence mattered ("That compression is \
significant enough to reshape...", "This mirrors a pattern...").
4. Portentous fragment or pronouncement: short abstract declarations doing \
significance work ("The new variable is AI.", "The technical floor has \
risen.", "The calculus has changed.", "The stakes are highest downstream.").
5. Meta-narration and setup frames: "Here's who/what/why...", "one pattern \
stands out", "puts a number on something leaders have felt", "the implication \
is direct", "raises the bar", "changes the shape of the talent problem".
6. Vague self-referential observation: "we are fielding searches", "we keep \
seeing", "the skill pairing we keep seeing", "this mirrors a pattern across \
the market" — instead of one concrete named observation.
7. Formulaic closer: ending on a forward-looking prediction ("We expect X to \
become standard within 18 months", "will separate themselves over the coming \
twelve months"), a "the ones who moved early" construction, or a rhetorical \
question aimed at the reader.
8. Implausible or over-engineered anecdote: first-person client stories are \
allowed and welcome, but they must read like something a recruiter would \
actually recount. Suspicious signs: cinematic detail ("rewrote its entire \
consultant specification mid-search"), suspiciously exact figures ("salary \
budget was 40% higher"), an interview "hinging on one question" with a \
perfect three-part answer. Prefer rounded, modest, hedged specifics.
9. Batch repetition (when sibling drafts are provided): reusing a sibling's \
opener shape, closer shape, anecdote template (same city, same "a client \
asked us" slot), or signature phrases/timelines ("over the coming twelve \
months" in several drafts). Each draft must read like a different writer.
10. Any remaining classic tells: em dashes, "not just", puffery \
("significant shift", "underscores"), rule-of-three cadence, false ranges.

Judge STRICTLY but do not invent problems: a plain concrete sentence is fine. \
Report at most the 8 worst violations.

Return ONLY a JSON object, no prose:
{"violations": [{"quote": "exact offending text, max 20 words", "problem": \
"which move and why, max 25 words"}]}
Return {"violations": []} if the draft is genuinely clean."""


def _extract_json(raw: str):
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        import json as _json
        return _json.loads(raw[start:end + 1])
    except Exception:
        return None


def judge_draft(client, fields: dict, siblings: list = None,
                context: str = "news") -> list:
    """Ask Opus to judge a draft against the banned rhetorical moves.
    `fields` maps field names to text (HTML is stripped). `siblings` is a list
    of short plain-text summaries of the other drafts in this batch, used for
    repetition checks. Returns hits in the same shape as detect(); a judge
    failure returns [] so the pipeline never blocks on the judge."""
    parts = []
    for name, value in (fields or {}).items():
        if isinstance(value, str) and value.strip():
            parts.append(f"[{name}]\n{_plain(value).strip()}")
    if not parts:
        return []
    draft_block = "\n\n".join(parts)

    sibling_block = ""
    if siblings:
        joined = "\n\n---\n\n".join(s.strip()[:900] for s in siblings if s and s.strip())
        if joined:
            sibling_block = (
                "\n\nSIBLING DRAFTS IN THIS BATCH (check the draft under "
                "review for repeated openers, closers, anecdote templates, "
                "cities, and signature phrases against these):\n\n" + joined)

    kind = "LinkedIn company-page post" if context == "linkedin" else "news commentary post"
    user_msg = (f"DRAFT {kind.upper()} UNDER REVIEW:\n\n{draft_block}"
                f"{sibling_block}\n\nReturn the JSON verdict now.")

    try:
        resp = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=800,
            system=_JUDGE_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        obj = _extract_json(resp.content[0].text or "")
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Style judge call failed: {e}")
        return []
    if not obj or not isinstance(obj.get("violations"), list):
        return []

    hits = []
    for v in obj["violations"][:8]:
        if isinstance(v, dict) and v.get("quote"):
            hits.append({
                "label": f"judge: {str(v.get('problem', 'banned move'))[:120]}",
                "fragment": str(v["quote"])[:120],
            })
    return hits


def sibling_summary(draft: dict) -> str:
    """Short plain-text fingerprint of a draft for the judge's batch check:
    title, first sentence, last sentence, and any first-person anecdote lines."""
    title = _plain(draft.get("title", ""))
    body = _plain(draft.get("body", ""))
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
    lines = [f"Title: {title}"]
    if sentences:
        lines.append(f"Opens: {sentences[0]}")
        lines.append(f"Closes: {sentences[-1]}")
    anecdotes = [s for s in sentences
                 if re.search(r"\b(?:we placed|a client|we took|asked us|our (?:search|mandate|placement)|a recent mandate)\b",
                              s, re.IGNORECASE)]
    for a in anecdotes[:2]:
        lines.append(f"Anecdote: {a}")
    return "\n".join(lines)


def warning_strip_html(hits_or_strings: list) -> str:
    """Amber warning box for the approval email when tells survive retries.
    Accepts either detect() hit dicts or pre-formatted strings."""
    if not hits_or_strings:
        return ""
    if hits_or_strings and isinstance(hits_or_strings[0], dict):
        lines = format_hits(hits_or_strings)
    else:
        lines = list(hits_or_strings)
    items = "<br>".join(
        "&#9888; " + s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for s in lines[:10]
    )
    return f"""
      <table width="100%" cellpadding="0" cellspacing="0"
             style="margin:0 0 16px;background:#fff8e1;border:1px solid #f0d070;
                    border-radius:6px;">
        <tr><td style="padding:10px 14px;font-size:12px;color:#7a5c00;line-height:1.6;">
          <strong>Style guard: unresolved AI-language flags.</strong>
          Review these before approving.<br>{items}
        </td></tr>
      </table>"""
