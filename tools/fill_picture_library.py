"""
Make the 168 scheduled pictures and put them in Supabase.

    python tools/fill_picture_library.py                 # make what is missing
    python tools/fill_picture_library.py --limit 20      # stop after 20
    python tools/fill_picture_library.py --day 3         # just day 3
    python tools/fill_picture_library.py --redo --day 3  # replace day 3
    python tools/fill_picture_library.py --status        # count, make nothing

RESUMABLE, WHICH IS THE WHOLE DESIGN. The cookie behind this is a consumer
Gemini session and it will die part-way through -- not as an error, but by
quietly answering in English with no picture attached. So nothing here is
held until the end: every picture is uploaded the moment it is made, and
what to make next is derived from the bucket on every run. A session that
manages nine pictures leaves 159 for the next one, and running this again
after a new cookie simply carries on.

It stops on its own when the replies show the session has gone, rather
than working through 150 more slots to be told the same thing each time.
"""
import argparse
import asyncio
import logging
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

from dotenv import load_dotenv

for name in ("render.env", ".env"):
    load_dotenv(os.path.join(BASE, name))

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)-7s %(message)s")
for noisy in ("httpx", "httpcore", "hpack", "curl_cffi", "PIL", "gemini_webapi"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

from supabase import create_client                       # noqa: E402

from pin_agent import gemini_images                      # noqa: E402
from pin_agent.config import load_pin_config             # noqa: E402
from pin_agent.gemini_images import GeminiImageMaker     # noqa: E402
from pin_agent.picture_library import PictureLibrary     # noqa: E402
from pin_agent.prompt_book import PromptBook             # noqa: E402

# A short breather between pictures. The web app queues, and hammering it
# is both rude and the fastest way to have the session closed.
PAUSE = 4.0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many pictures")
    ap.add_argument("--day", type=int, default=0, help="only this day, 1-28")
    ap.add_argument("--redo", action="store_true",
                    help="remake pictures that already exist")
    ap.add_argument("--status", action="store_true",
                    help="report and make nothing")
    args = ap.parse_args()

    book = PromptBook()
    if not book.is_ready:
        print("The schedule did not load: %s" % (book.error or "not found"))
        return 2

    cfg = load_pin_config()
    client = create_client(cfg.supabase_url, cfg.supabase_key)
    library = PictureLibrary(client)
    library.refresh()

    status = library.status(book)
    print("=" * 72)
    print("PICTURE LIBRARY")
    print("=" * 72)
    print("  bucket      : %s/%s" % (status["bucket"], "library"))
    print("  stored      : %d of %d" % (status["pictures"], status["wanted"]))
    print("  missing     : %d" % status["missing"])
    print("  today ready : %d of 6" % status["today_ready"])
    if library.last_error:
        print("  last error  : %s" % library.last_error)
    if args.status:
        return 0

    # What to make.
    if args.redo:
        todo = [e for d in sorted(book.days) for e in book.days[d]]
    else:
        todo = library.missing(book)
    if args.day:
        todo = [e for e in todo if e["day"] == args.day]
    if args.limit:
        todo = todo[:args.limit]

    if not todo:
        print("\nNothing to make.")
        return 0

    maker = GeminiImageMaker(psid=os.getenv("GEMINI_WEB_PSID", ""),
                             psidts=os.getenv("GEMINI_WEB_PSIDTS", ""),
                             image_dir=os.path.join(BASE, "assets", "library"))
    if not maker.is_ready:
        print("\nGEMINI_WEB_PSID is not set, so nothing can be made.")
        return 2

    print("\nMaking %d picture(s). Ctrl-C is safe -- each one is uploaded "
          "as it is made." % len(todo))
    print("-" * 72)

    made = failed = 0
    started = time.time()
    for n, entry in enumerate(todo, 1):
        day, slot = entry["day"], entry["slot"]
        label = "d%02ds%d %-30s" % (day, slot, entry["board"][:30])

        out = await maker.make_from(book.prompt_for(entry), entry["scene"])
        if not out:
            failed += 1
            why = maker.last_error or "no reason given"
            print("  %s  FAILED  %s" % (label, why[:90]))
            # THE SESSION IS GONE. Carrying on would spend a timeout per
            # slot to be told this another hundred and fifty times.
            if getattr(maker, "signed_out", False) or not maker.awake:
                print("\n" + "!" * 72)
                print("The Gemini session is no longer signed in. Stopping.")
                print("Replace GEMINI_WEB_PSID / GEMINI_WEB_PSIDTS and run")
                print("this again -- everything made so far is saved.")
                print("!" * 72)
                break
            continue

        path = out[0]
        url = await asyncio.to_thread(library.put, day, slot, path)
        if not url:
            failed += 1
            print("  %s  MADE BUT NOT STORED  %s"
                  % (label, library.last_error[:70]))
            continue

        made += 1
        size = os.path.getsize(path) // 1024
        print("  %s  ok  %4d KB  (%d/%d)" % (label, size, n, len(todo)))
        try:
            os.remove(path)          # the bucket has it now
        except OSError:
            pass

        if n < len(todo):
            await asyncio.sleep(PAUSE)

    mins = (time.time() - started) / 60.0
    library.refresh()
    after = library.status(book)
    print("-" * 72)
    print("  made        : %d" % made)
    print("  failed      : %d" % failed)
    print("  elapsed     : %.1f min" % mins)
    print("  stored now  : %d of %d  (missing %d)"
          % (after["pictures"], after["wanted"], after["missing"]))
    print("  today ready : %d of 6" % after["today_ready"])
    return 0 if made or not todo else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
