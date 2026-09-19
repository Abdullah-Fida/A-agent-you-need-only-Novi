"""
Is this story American news?

The four social accounts carry US stories and nothing else. The site itself
still publishes everything -- Pakistan explainers, world reporting, evergreen
"what is an IPO" pieces -- but those go out to an audience that is not there:
the accounts are being grown for American readers, and a feed that mixes
Karachi tax filing with the Federal Reserve reads to that audience as
somebody else's newspaper.

There is no country on an article. The pipeline files everything under
tech_ai, business_markets, crypto, world_news or pakistan, and "US" is not
one of them -- an American story about the Fed and a British one about a
mayor are both "business_markets". So it has to be read off the words.

PLAIN CODE, NOT A MODEL. The same choice as everywhere else here: a model
asked "is this US news?" drifts between runs and cannot be tested, while a
list of names can be measured against real headlines and corrected when it
is wrong. Every term below earns its place by appearing in stories this
site actually published.
"""
import re
from typing import Dict, Iterable

# Institutions, places and people that only exist in American coverage.
# Deliberately specific: "president" is every country, "White House" is one.
US_TERMS = {
    # the country
    "united states", "u.s.", "u.s.a.", "usa", "america", "american",
    "americans",
    # government and politics
    "white house", "congress", "senate", "senator", "house republicans",
    "house democrats", "capitol hill", "pentagon", "state department",
    "supreme court", "scotus", "republican", "democrat", "democrats",
    "gop", "trump", "biden", "harris", "vance", "washington",
    # agencies and regulators
    "federal reserve", "the fed", "fed chair", "fomc", "treasury department",
    "sec", "cftc", "fdic", "occ", "fbi", "cia", "nsa", "irs", "fda", "ftc",
    "fcc", "doj", "justice department", "nasa", "epa", "cdc",
    "homeland security",
    # In this site's mix -- crypto, markets, business -- an unqualified
    # "Treasury" is the American one. "Treasury Sanctions Crypto Exchange"
    # is US news, and without this the word "Iran" later in the headline
    # threw it out.
    "treasury",
    # NOT "ice": word boundaries do not save it from "ice cream".
    # markets
    "wall street", "nasdaq", "dow jones", "s&p 500", "nyse", "russell 2000",
    # places
    "silicon valley", "new york", "california", "texas", "florida",
    "chicago", "los angeles", "san francisco", "seattle", "boston",
    "atlanta", "houston", "dallas", "miami", "detroit", "philadelphia",
    "arizona", "nevada", "georgia", "michigan", "ohio", "virginia",
    "massachusetts", "colorado", "oregon", "wall st",
}

# Where a story is plainly somebody else's. Only consulted when NO American
# term is present -- a story about a US-India summit is US news even though
# India is in it.
ELSEWHERE = {
    "pakistan", "pakistani", "karachi", "lahore", "islamabad", "rupee",
    "india", "indian", "modi", "new delhi", "mumbai",
    "china", "chinese", "beijing", "shanghai", "xi jinping",
    "russia", "russian", "moscow", "putin", "kremlin",
    "ukraine", "kyiv", "united kingdom", "britain", "british", "london",
    "downing street", "westminster", "european union", "brussels",
    "germany", "german", "berlin", "france", "french", "paris",
    "japan", "japanese", "tokyo", "korea", "korean", "seoul",
    "canada", "canadian", "ottawa", "australia", "australian",
    "brazil", "nigeria", "indonesia", "turkey", "iran", "tehran",
    "israel", "gaza", "saudi", "uae", "dubai", "bangladesh",
}

# Sections that are never American, whatever the words say.
NEVER = {"pakistan"}


def _has(text: str, terms: Iterable[str]) -> str:
    """The first term that appears as a WHOLE word, or ""."""
    for term in terms:
        # \b does not work against "u.s." or "s&p 500", so the boundary is
        # spelled out: the term must not be glued to a letter or digit.
        pattern = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
        if re.search(pattern, text):
            return term
    return ""


# "US" is the country; "us" is the commonest pronoun in English. Case is
# the only thing that separates them, so this is read BEFORE the text is
# folded to lowercase. Without it "Hot US Inflation Data" looked foreign.
_BARE_US = re.compile(r"(?<![A-Za-z0-9])(US|U\.S\.|USA)(?![A-Za-z0-9])")

# Same trick, same reason: capitalised "Fed" is the Federal Reserve, and
# lowercase "fed" is the past tense of "feed". In this site's mix -- crypto,
# markets, business -- "Fed raises rates" is as American as a headline gets,
# and matching it case-blind would catch "fed the dog".
_BARE_FED = re.compile(r"(?<![A-Za-z0-9])Fed(?![A-Za-z0-9])")


def _clean(text: str) -> str:
    return (text or "").replace("\u2019", "'").replace("&#8217;", "'")


def why(article: Dict) -> str:
    """
    Why this story is or is not American, in a few words.

    Returned rather than logged so the decision can be shown in a report and
    argued with, instead of an article quietly not being posted.

    THE HEADLINE DECIDES, the way it does for a reader. "China's Top Spy
    Chief Warns A.I. Is a Threat to Party Rule" mentions the United States
    in its summary and is not American news; it is news about China that
    mentions America. The summary is consulted only when the headline names
    no country at all.
    """
    if not article:
        return "no article"

    category = (article.get("category") or "").strip().lower()
    if category in NEVER:
        return "not US: filed under %s" % category

    title = _clean(article.get("title"))
    rest = _clean(article.get("summary")) + " " + \
        _clean(article.get("meta_description"))

    if _BARE_US.search(title):
        return "US: headline says US"
    if _BARE_FED.search(title):
        return "US: the Fed"
    hit = _has(title.lower(), US_TERMS)
    if hit:
        return "US: %s" % hit

    other = _has(title.lower(), ELSEWHERE)
    if other:
        return "not US: headline is about %s" % other

    # The headline named nowhere. Fall back to the rest of the piece.
    if _BARE_US.search(rest):
        return "US: story says US"
    if _BARE_FED.search(rest):
        return "US: the Fed"
    hit = _has(rest.lower(), US_TERMS)
    if hit:
        return "US: %s" % hit

    other = _has(rest.lower(), ELSEWHERE)
    if other:
        return "not US: about %s" % other
    return "not US: no American subject"


def is_us_news(article: Dict) -> bool:
    """
    Whether this story belongs on the four American social accounts.

    STRICT ON PURPOSE. An evergreen explainer -- "what a recession is", "how
    blockchain improves traceability" -- names no country and is not news
    about America, so it does not go out. The site keeps publishing it; the
    accounts stay on one subject.
    """
    return why(article).startswith("US:")
