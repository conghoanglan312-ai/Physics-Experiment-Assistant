"""A short, fast answer from the model, with nothing between it and the API.

The chat path is built for a conversation: it embeds the question, searches the
manual, assembles a long system prompt and holds the session while it streams.
That is right for "explain the static method to me" and wrong for a student
standing at the apparatus pressing **Why?** -- measured on 2026-09-20, the round
trip ran to several seconds and twice returned nothing at all because the
student's own chat was mid-answer and the session was locked.

So this path does none of it: one call, a fixed system line, a hard word budget,
no retrieval, no memory, no shared lock. The physics it explains is the physics
the aid already measured, which the model knows without being handed the manual.

THE SEAL is unchanged -- what goes in comes from cv.api.llm_context through the
callers in tutor.py, never the raw status, never a voltage.
"""

import os
import threading

_client = None
_lock = threading.Lock()

MODEL = "gemini-3.1-flash-lite"
TIMEOUT_S = 12.0

SYSTEM = (
    "You are a physics tutor standing beside a student at a Millikan oil-drop "
    "apparatus. Answer in ENGLISH, in at most two sentences and 45 words, with no "
    "preamble and no pleasantries -- they are mid-measurement and reading this on a "
    "small panel. Explain the physics; never state a charge, an electron count or a "
    "voltage. A droplet balanced motionless is what they are trying to achieve -- "
    "never advise keeping it moving or off balance to suit the software."
)

# The lab's own manual, read ONCE per process and carried on every call.
#
# The chat path embeds each question and searches the manual for it, which is a
# network round trip before the answer can even start -- and it was the step that
# failed when the proxy dropped. Nothing about the manual changes between
# questions, so it is loaded straight out of the vector store (no embedding
# needed: the documents are already there) and prepended to the system line. The
# answers stay grounded in the lab's text, and the per-question cost is zero.
MANUAL_BUDGET = 24000           # how much of it is kept in memory...
MANUAL_SLICE = 4000             # ...and how much of it is sent with a question.
#
# Sending the whole manual made the answers WORSE. Asked why a droplet falling at
# 0.13 grid/s was rejected, the ungrounded model said "too small, air resistance
# dominates" -- right -- and the whole-manual version said "too large, causing it
# to fall slower", three times out of three. The manual's own sentence is "if the
# oil droplets are too small ... the time error will also increase; if the oil
# droplets are too large and fall too fast, the relative time error will
# increase": both halves end in "time error", and with 20k characters of context
# the model matched the phrase rather than the physics.
#
# Retrieval exists to hand over the relevant paragraph. This does the same job
# without a network call: the manual is already in memory, so the slice is chosen
# by looking for the words in the question.
_manual = None


def manual():
    """The manual as one string, or "" if it has not been ingested here."""
    global _manual
    if _manual is not None:
        return _manual
    text = ""
    try:
        import chromadb
        from llm.src.contextRetrieval import collection as _c  # noqa: F401
    except Exception:
        pass
    try:
        import chromadb
        client = chromadb.PersistentClient(path = "llm/chroma_db")
        docs = client.get_collection("physics_manual").get().get("documents") or []
        out = []
        for d in docs:
            d = (d or "").strip()
            # RecursiveCharacterTextSplitter overlaps neighbours by 300 characters;
            # sending the same paragraph twice costs tokens and teaches nothing.
            head = d[:200]
            if out and head and head in out[-1]:
                d = d[len(head):]
            out.append(d)
        text = "\n".join(out)[:MANUAL_BUDGET]
    except Exception as exc:
        print("quick ai: manual not available (%s)" % str(exc)[:90], flush = True)
    _manual = text
    if text:
        print("quick ai: manual loaded, %d characters" % len(text), flush = True)
    return _manual


def available():
    return bool(os.getenv("GEMINI_API_KEY"))


def _get():
    global _client
    with _lock:
        if _client is None:
            from dotenv import load_dotenv
            from google import genai
            load_dotenv()
            _client = genai.Client(api_key = os.getenv("GEMINI_API_KEY"))
        return _client


def ask(prompt, system = SYSTEM):
    """One short answer, or None. Never raises."""
    try:
        from google.genai import types
        book = relevant(prompt)
        if book:
            system = ("%s\n\nFROM THE LAB'S OWN MANUAL, which is authoritative where "
                      "it speaks -- but do not repeat a phrase from it whose physics "
                      "contradicts the measurement you are given:\n%s" % (system, book))
        result = _get().models.generate_content(
            model = MODEL,
            contents = prompt,
            config = types.GenerateContentConfig(
                system_instruction = system,
                temperature = 0.3,
                max_output_tokens = 400,
                http_options = types.HttpOptions(timeout = int(TIMEOUT_S * 1000)),
            ),
        )
        text = (getattr(result, "text", None) or "").strip()
        return text or None
    except Exception as exc:
        print("quick ai: %s" % str(exc)[:160], flush = True)
        return None


def relevant(question, budget = MANUAL_SLICE):
    """The parts of the manual that speak to this question.

    Scores each paragraph by how many of the question's own words it contains,
    which is crude next to an embedding and costs nothing -- and the alternative
    measured worse than no manual at all. Returns "" when nothing matches, and
    an unmatched question is better answered from the model's own physics than
    from a paragraph that happens to share a word.
    """
    book = manual()
    if not book:
        return ""
    stop = set("the a an of to in is are for and or with that this it be on at by from "
               "your you their they which what why how when will not no do does did "
               "student droplet droplets aid measurement session seconds grid".split())
    # The QUESTION, not the payload: both prompts put the situation in their first
    # line and the JSON state after it, and scoring against the JSON matched key
    # names like "fallRunning" against nothing at all -- the slice came back empty
    # every time and the manual was never consulted.
    head = question.split("Session state")[0].split("\n\n")[0]
    words = {w.strip(".,;:()[]\"'").lower() for w in head.split()}
    words = {w for w in words if len(w) > 3 and w not in stop and not w.isdigit()}
    # Fixed windows, because the extracted PDF text is nearly all short lines: a
    # "paragraphs longer than 120 characters" split found none of them, the slice
    # came back empty, and the manual was never actually consulted.
    step = 500
    paras = [" ".join(book[i:i + 900].split()) for i in range(0, len(book), step)]
    scored = []
    for para in paras:
        low = para.lower()
        hits = sum(1 for w in words if w in low)
        if hits:
            scored.append((hits, para))
    scored.sort(key = lambda pair: -pair[0])
    out, total = [], 0
    for hits, para in scored:
        if total + len(para) > budget:
            continue
        out.append(para)
        total += len(para)
        if total >= budget * 0.8:
            break
    return "\n\n".join(out)
