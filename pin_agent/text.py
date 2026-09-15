"""
Plain text for text that gets DRAWN.

A pin title is set in 56px bold across the middle of a 1000x1500 image. One
character the host font lacks is a blank box on a published pin, and
nothing downstream would catch it -- the title passes every length and
content check either way, because the character is there, it just has no
glyph.

Seen live in both paths: the model wrote "coat drop-off" for an advice pin
and a product title with the same NON-BREAKING HYPHEN (U+2011) minutes
later. Arial has that glyph and so does DejaVu, which is what the Linux
host uses, but nothing should depend on which font a host happens to have.

Folded BEFORE lengths are measured, so what is counted is what is drawn.
That matters for the ellipsis, which becomes three characters.
"""

# Characters models reach for that are not on a keyboard.
TYPOGRAPHIC = {
    "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2015": "-", "\u2212": "-",
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u2026": "...", "\u00a0": " ", "\u202f": " ", "\u2009": " ",
    "\u200b": "", "\ufeff": "",
}


def fold_typographic(text: str) -> str:
    """Curly quotes, long dashes and invisible spaces become plain ASCII."""
    text = text or ""
    for fancy, plain in TYPOGRAPHIC.items():
        if fancy in text:
            text = text.replace(fancy, plain)
    return text
