"""
The house style gate: what a machine writes that a journalist would not.

An audit of the first 84 published articles found AI phrasing in 78 of them.
Not subtle phrasing either -- "underscores" 60 times across 43 articles,
"landscape" 57 times across 41, "moreover" 40 times across 38. Google has
spent two years learning to spot exactly this register, and readers feel it
long before they can name it.

Asking the model nicely does not fix it. Every one of those 84 articles was
written under a prompt that said "be factual and analytical", and the model
reached for "underscores the broader shift" anyway, because that is what the
register it was trained on does. So this is a GATE, not a request: the body
is scanned after it is written, and a draft carrying too many tells is sent
back with the offending words named.

Two lists, because the two failures are different:

  * BANNED phrases are dead giveaways. A newsroom sub-editor would strike
    them on sight. They carry no information -- "underscores" is what you
    write when you have nothing to add about the fact you just stated.
  * WATCHED words are legitimate English that models over-reach for. One is
    fine. Four in an 800-word article is a tell.
"""
import logging
import re
from typing import Dict, List, Tuple

logger = logging.getLogger("OmniBot.HouseStyle")

# Struck on sight. The value is what to do instead -- handed to the model on
# a rewrite, because "don't use this" without an alternative just produces a
# synonym from the same register.
BANNED: Dict[str, str] = {
    "underscore": "say what the fact shows, or delete the sentence",
    "underscores": "say what the fact shows, or delete the sentence",
    "underscoring": "say what the fact shows, or delete the sentence",
    "underscored": "say what the fact shows, or delete the sentence",
    "highlights the": "name the thing directly",
    "highlighting the": "name the thing directly",
    "moreover": "start the sentence with its own subject",
    "furthermore": "start the sentence with its own subject",
    "in conclusion": "just write the last paragraph",
    "it is important to note": "if it matters, state it; if not, cut it",
    "it's important to note": "if it matters, state it; if not, cut it",
    "it is worth noting": "if it matters, state it; if not, cut it",
    "it's worth noting": "if it matters, state it; if not, cut it",
    "testament to": "say what it proves",
    "a stark reminder": "say what it reminds people of",
    "serves as a": "use a plain verb: is, shows, works as",
    "stands as a": "use a plain verb",
    "plays a crucial role": "say what it actually does",
    "plays a vital role": "say what it actually does",
    "plays a key role": "say what it actually does",
    "paving the way": "say what it makes possible",
    "paves the way": "say what it makes possible",
    "shaping the future": "say what changes",
    "the future of": "name the thing that changes",
    "ever-evolving": "cut it",
    "rapidly evolving": "cut it",
    "in today's": "cut it, or give the year",
    "in the world of": "cut it",
    "in the realm of": "cut it",
    "realm": "field, market, area",
    "tapestry": "cut it",
    "myriad": "many, or a number",
    "plethora": "many, or a number",
    "delve into": "look at, examine",
    "delves into": "looks at, examines",
    "dive into": "look at, examine",
    "game-changer": "say what changes",
    "game changer": "say what changes",
    "at the end of the day": "cut it",
    "only time will tell": "cut it",
    "remains to be seen": "say what is undecided and who decides it",
    "sheds light on": "shows, explains",
    "a broader shift": "name the shift",
    "signaling a": "say what it means",
    "signalling a": "say what it means",
    "navigate the": "deal with, work through",
    "navigating the": "dealing with, working through",
    "when it comes to": "for, in, with",
    "the landscape": "the market, the sector, the industry",
    "landscape of": "market, sector, industry",
    "unlock the": "say what becomes possible",
    "harness the": "use",
    "usher in": "start, bring",
    "a testament": "say what it proves",
    "double-edged sword": "say both effects",
    "not only ... but also": "two plain sentences",
}

# Legitimate words that models reach for far too often. Counted, not banned:
# one "crucial" in an article is English, four is a machine.
WATCHED: Dict[str, int] = {
    "crucial": 1,
    "pivotal": 1,
    "robust": 1,
    "leverage": 1,
    "foster": 1,
    "poised": 1,
    "significant": 3,
    "key": 4,
    "critical": 2,
    "comprehensive": 1,
    "seamless": 1,
    "vital": 1,
    "landscape": 0,
    "notably": 1,
    "additionally": 1,
}

# How many banned hits a draft may carry before it is sent back. Not zero:
# one slip in a thousand words is not worth a second model call and the
# retry can easily come back worse.
MAX_BANNED = 1

TAG = re.compile(r"<[^>]+>")

# Models write typographic quotes, so "in today's" in this list never
# matched "in today’s" in the prose and the phrase sailed through every
# check. Caught in a dry run: the gate reported a clean draft that had
# "In today's drop" in the third paragraph.
_SMART = str.maketrans({"’": "'", "‘": "'",
                        "“": '"', "”": '"',
                        "‑": "-", "–": "-", "—": "-"})


def visible_text(html: str) -> str:
    """
    The prose only, with typographic punctuation normalised.

    Detection has to ignore markup, or a source URL containing the word
    "landscape" counts as a style failure and an article is rewritten for
    something no reader can see.
    """
    return TAG.sub(" ", html or "").translate(_SMART)


def _sequence(phrase: str) -> str:
    """
    A phrase as a pattern, tolerant of the whitespace between its words.

    re.escape stopped escaping spaces in Python 3.7, so the old
    `.replace(r"\\ ", r"\\s+")` silently did nothing and a phrase broken
    across two lines by the model was never found.
    """
    return r"\s+".join(re.escape(w) for w in phrase.split())


def _uses(text: str, phrase: str) -> int:
    phrase = phrase.translate(_SMART)
    if "..." in phrase:            # "not only ... but also"
        head, tail = (p.strip() for p in phrase.split("...", 1))
        pattern = _sequence(head) + r"\b.{0,80}?\b" + _sequence(tail)
    else:
        pattern = r"\b" + _sequence(phrase) + r"\b"
    return len(re.findall(pattern, text, re.I))


def find_banned(html: str) -> List[Tuple[str, int]]:
    """Every banned phrase in the draft, worst first."""
    text = visible_text(html)
    hits = [(p, n) for p in BANNED if (n := _uses(text, p))]
    return sorted(hits, key=lambda h: -h[1])


def find_overused(html: str) -> List[Tuple[str, int, int]]:
    """(word, uses, allowance) for every watched word over its allowance."""
    text = visible_text(html)
    over = []
    for word, allowance in WATCHED.items():
        n = _uses(text, word)
        if n > allowance:
            over.append((word, n, allowance))
    return sorted(over, key=lambda o: -(o[1] - o[2]))


def report(html: str) -> Dict:
    """Everything wrong with the register, in one look."""
    banned = find_banned(html)
    overused = find_overused(html)
    return {
        "banned": banned,
        "overused": overused,
        "banned_total": sum(n for _, n in banned),
        "passes": sum(n for _, n in banned) <= MAX_BANNED and not overused,
    }


def instructions(html: str, limit: int = 12) -> str:
    """
    The note handed back to the model on a rewrite.

    Naming the exact phrases matters. "Write less like an AI" produces a
    draft with the same words in a different order; "you used 'underscores'
    four times, here is what to write instead" produces a fix.
    """
    lines: List[str] = []
    for phrase, n in find_banned(html)[:limit]:
        times = "once" if n == 1 else f"{n} times"
        lines.append(f'- "{phrase}" ({times}) -> {BANNED[phrase]}')
    for word, n, allowance in find_overused(html)[:limit]:
        allowed = "not at all" if allowance == 0 else f"at most {allowance}x"
        lines.append(f'- "{word}" used {n}x, allowed {allowed} -> vary it or cut it')
    return "\n".join(lines)


# Sentence-opening connectives a sub-editor deletes without replacing. Safe
# to remove mechanically because the sentence stands without them -- unlike
# "underscores", which is load-bearing and needs the sentence rewritten.
_OPENERS = ("Moreover", "Furthermore", "Additionally", "Notably",
            "Importantly", "Indeed", "Ultimately", "Overall")

_OPENER_RE = re.compile(
    r"(?P<before>(?:<p>|<li>|\.\s+|\?\s+|!\s+))"
    r"(?P<opener>" + "|".join(_OPENERS) + r"),\s+(?P<next>[a-z])")


def strip_openers(html: str) -> str:
    """
    Removes "Moreover, " and friends from the start of a sentence.

    Deliberately narrow: it only fires where the connective opens a sentence
    AND is followed by a comma AND a lowercase word, so the next word can be
    capitalised without guessing. Anything less certain is left for the
    rewrite, because a mangled sentence is worse than a stilted one.
    """
    def fix(m: re.Match) -> str:
        return f"{m.group('before')}{m.group('next').upper()}"

    return _OPENER_RE.sub(fix, html or "")
