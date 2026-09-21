"""
Pictures made to order, through a signed-in Gemini session.

WHY THIS EXISTS. Every mismatch this board has published came from pairing a
sentence with a photograph somebody else took: a cutlery drawer under a tip
about stacking pans, a bowl of fruit under one about appliances, a front
door under one about shoes. Searching for a picture of a specific idea
fails because the open libraries do not hold one for every idea.

Generating it removes the problem rather than filtering it. The picture is
made FOR the sentence, so the two cannot disagree.

WHAT IT COSTS, honestly. This is not the official API -- it drives the
consumer Gemini web app with the cookies from a signed-in browser, which is
against Google's terms for that product and puts the account at risk. The
official image models are on the same keys this project already holds, but
return no free quota at all: every one of them answers
"GenerateRequestsPerDayPerProjectPerModel-FreeTier, daily quota used up" on
a project that has never called them.

The precedent for doing it this way is this project's own: the Bing image
cookie ran in production for weeks.

HOW IT FAILS. Never loudly, and never into a slot. A dead cookie, a refused
prompt, a rate limit -- all of them return None, and the caller falls back
to the photograph path that has been working. The cookie dies eventually;
when it does this says so ONCE, by email, rather than degrading quietly.
"""
import asyncio
import logging
import os
import time
from collections import deque
from typing import Optional, Tuple

from pin_agent import photo_styles

logger = logging.getLogger("PinAgent.GeminiImages")

# The consumer app rotates __Secure-1PSIDTS every few hours. The library
# refreshes it on a timer, which is the only reason this is viable
# unattended -- a fixed cookie would die before the next morning.
REFRESH_SECONDS = 600.0

# A generated picture is worth one try. Two would double the slowest step in
# the slot for a second roll of the same dice.
TRIES = 1

# Long, because the web app queues. Still bounded: a slot must not hang.
TIMEOUT = 180.0

# After this many consecutive failures the cookie is treated as dead and
# nothing is attempted until the process restarts or it is replaced. Without
# it every slot pays the timeout to learn the same thing.
FAILURES_BEFORE_RESTING = 3
REST_SECONDS = 3600.0


# HOW A DEAD COOKIE ANNOUNCES ITSELF.
#
# Not as an error. The web app answers a signed-out caller in conversational
# English -- "Are you signed in?", "I can search for images, but can't create
# any" -- with HTTP 200 and no image attached. That is indistinguishable from
# a prompt it declined on policy grounds, and the two need opposite responses:
# a declined prompt should be shrugged off and the next one tried, a dead
# cookie should stop the attempts and fetch somebody.
#
# Reading the reply is the only way to tell them apart, and getting this
# wrong cost a day: every slot recorded "refused", the failure counter never
# moved, so the session never rested and the alert never went out. The board
# published photographs for a day while the status page said the picture
# maker was ready.
SIGNED_OUT = (
    "are you signed in",
    "you might be signed out",
    "unauthenticated",
    "cookies are invalid",
    "can't create any",
    "cannot create any",
    "can't seem to create",
)


def looks_signed_out(text: str) -> bool:
    low = (text or "").lower()
    return any(mark in low for mark in SIGNED_OUT)


class GeminiImageMaker:
    """Makes one photograph-like image for a tip, or returns None."""

    def __init__(self, psid: str = "", psidts: str = "", proxy: str = "",
                 nm=None, image_dir: str = ""):
        self.psid = (psid or "").strip()
        self.psidts = (psidts or "").strip()
        self.proxy = (proxy or "").strip() or None
        self.nm = nm
        self.image_dir = image_dir or "."
        self._client = None
        self._starting: Optional[asyncio.Lock] = None
        self._failures = 0
        self._resting_until = 0.0
        self._alerted = False
        # How the last few pictures were framed, so the next one is not the
        # same photograph of a different object. Small on purpose: this is
        # meant to space repeats out, not to forbid them forever.
        self._recent_framings = deque(maxlen=6)
        self.made = 0
        self.refused = 0
        self.last_error = ""
        # The model's own words the last time it answered without a picture.
        self.last_reply = ""
        # Set once the replies show the cookie is no longer signed in.
        self.signed_out = False

        if not self.is_ready:
            logger.info("No Gemini cookie set — pins will use photographs "
                        "only, which is how this worked before.")

    @property
    def is_ready(self) -> bool:
        # __Secure-1PSIDTS is optional on some accounts; the PSID is not.
        return bool(self.psid)

    @property
    def awake(self) -> bool:
        return time.monotonic() >= self._resting_until

    # ── the session ──────────────────────────────────────────────

    async def _connect(self):
        """
        Starts the session once, and only once.

        Lazily, because a dead cookie must not hold up the whole bot at
        boot; and behind a lock, because four advice slots can land close
        enough together to start four sessions otherwise.
        """
        if self._client is not None:
            return self._client
        if self._starting is None:
            self._starting = asyncio.Lock()

        async with self._starting:
            if self._client is not None:
                return self._client
            try:
                from gemini_webapi import GeminiClient
            except ImportError:
                self.last_error = "gemini_webapi is not installed"
                logger.warning(self.last_error)
                return None
            try:
                client = GeminiClient(self.psid, self.psidts, proxy=self.proxy)
                await client.init(timeout=TIMEOUT, auto_close=False,
                                  auto_refresh=True,
                                  refresh_interval=REFRESH_SECONDS,
                                  verbose=False)
                self._client = client
                logger.info("Gemini image session is live.")
                return client
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                logger.warning(f"Gemini image session would not start: "
                               f"{self.last_error}")
                return None

    # ── the prompt ───────────────────────────────────────────────

    def prompt_for(self, scene: str, when=None) -> Tuple[str, str]:
        """
        What to ask for, how it is framed, and what to forbid.

        The look turns over weekly and the framing moves within it -- see
        photo_styles. Two rules never move: NO WORDS IN THE PICTURE,
        because the template prints the title across the lower third itself
        and generated lettering is misspelled besides; and vertical,
        because the pin is 1000x1500 and a square crops to a keyhole.

        Returns the prompt and a fingerprint of the framing, so the caller
        can keep the last few and stop two pins in a row being the same
        photograph of different objects.
        """
        return photo_styles.compose(scene, when=when,
                                    avoid=list(self._recent_framings))

    # ── making one ───────────────────────────────────────────────

    async def make_from(self, prompt: str,
                        scene: str) -> Optional[Tuple[str, str]]:
        """
        Make a picture from a prompt written by hand, not composed here.

        The scheduled pins carry their own prompt out of the prompt book --
        a style block, a scene and a tail that somebody wrote and read. This
        sends exactly that, so what ships is what was reviewed.
        """
        return await self._make(prompt, scene, framing="scheduled")

    async def make(self, scene: str, when=None) -> Optional[Tuple[str, str]]:
        """
        Returns (local file path, what was asked for), or None.

        None is not an error worth stopping for: the caller falls back to a
        real photograph, which is what it did before this existed.
        """
        if not (self.is_ready and scene.strip() and self.awake):
            return None
        prompt, framing = self.prompt_for(scene, when=when)
        return await self._make(prompt, scene, framing)

    async def _make(self, prompt: str, scene: str,
                    framing: str) -> Optional[Tuple[str, str]]:
        """The part both paths share: ask, save, count, rest on failure."""
        if not (self.is_ready and prompt.strip() and self.awake):
            return None

        # _connect swallows its own failures and answers None, but this is
        # wrapped anyway: the promise this class makes is that it can never
        # cost a slot, and that promise should not rest on another method
        # keeping its own.
        try:
            client = await self._connect()
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self._note_failure()
            return None
        if client is None:
            self._note_failure()
            return None

        try:
            output = await asyncio.wait_for(
                client.generate_content(prompt), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            self.last_error = "the web app did not answer in time"
            self._note_failure()
            return None
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self._note_failure()
            return None

        images = getattr(output, "images", None) or []
        if not images:
            # A refusal is not a broken cookie -- some prompts simply come
            # back as words. Counted separately so one does not rest the
            # session.
            #
            # KEEP WHAT IT SAID. "No image came back" is true and useless:
            # a quota notice, a policy refusal and a model that answered the
            # prompt as a question all look identical from here, and they
            # need three different fixes. The reply itself distinguishes
            # them, and throwing it away meant a whole afternoon spent
            # guessing which one it was.
            said = " ".join((getattr(output, "text", "") or "").split())
            self.last_reply = said

            if looks_signed_out(said):
                # The cookie, not the prompt. Counted as a failure so the
                # session rests instead of paying the timeout on every
                # remaining slot, and so somebody is actually told.
                self.signed_out = True
                self.last_error = ("the cookie is not signed in; it said: "
                                   + said[:300])
                logger.warning("Gemini is answering as a signed-out user -- "
                               "the cookie needs replacing.")
                self._note_failure()
                return None

            self.refused += 1
            self.last_error = ("no image came back; it said: "
                               + (said[:400] or "(nothing at all)"))
            logger.info(f"Gemini returned no image for '{scene[:44]}'. "
                        f"It replied: {said[:200]}")
            return None

        name = "gen_%d.png" % int(time.time() * 1000)
        try:
            os.makedirs(self.image_dir, exist_ok=True)
            saved = await images[0].save(path=self.image_dir, filename=name,
                                         verbose=False)
        except Exception as e:
            self.last_error = f"could not save: {type(e).__name__}: {e}"
            self._note_failure()
            return None

        path = saved if isinstance(saved, str) and os.path.exists(saved) \
            else os.path.join(self.image_dir, name)
        if not os.path.exists(path):
            self.last_error = "the file was not written"
            self._note_failure()
            return None

        self._failures = 0
        self.made += 1
        self._recent_framings.append(framing)
        logger.info(f"Generated a picture for '{scene[:44]}' "
                    f"({photo_styles.style_for()['name']}).")
        return path, scene

    # ── failure ──────────────────────────────────────────────────

    def _note_failure(self) -> None:
        self._failures += 1
        logger.info(f"Gemini image attempt failed ({self._failures}): "
                    f"{self.last_error}")
        if self._failures < FAILURES_BEFORE_RESTING:
            return

        # Three in a row is a dead cookie, not bad luck. Resting stops every
        # slot paying the timeout to learn the same thing.
        self._resting_until = time.monotonic() + REST_SECONDS
        self._client = None
        logger.warning(f"Gemini images resting for "
                       f"{int(REST_SECONDS / 60)} minutes after "
                       f"{self._failures} failures: {self.last_error}")
        try:
            asyncio.get_running_loop().create_task(self._tell_someone())
        except RuntimeError:
            # Called from somewhere with no loop (a test, a sync path).
            # Resting still took effect; only the email is skipped.
            pass

    async def _tell_someone(self) -> None:
        """
        The cookie expires. Said once, by email, rather than silently.

        This is the failure the Bing cookie taught: it stopped working, the
        images quietly stopped, and nothing said so for days.
        """
        if self._alerted or not self.nm:
            return
        self._alerted = True
        try:
            await self.nm.send_notification(
                subject="The Gemini image cookie needs replacing",
                message=(
                    f"Pin images have fallen back to stock photographs.\n\n"
                    f"Last error: {self.last_error}\n\n"
                    f"To replace it:\n"
                    f"  1. Open https://gemini.google.com signed in\n"
                    f"  2. F12 > Application > Cookies > "
                    f"https://gemini.google.com\n"
                    f"  3. Copy __Secure-1PSID and __Secure-1PSIDTS\n"
                    f"  4. Put them in Render as GEMINI_WEB_PSID and "
                    f"GEMINI_WEB_PSIDTS, then redeploy\n\n"
                    f"Nothing is broken meanwhile. Pins are still publishing, "
                    f"with real photographs instead of made ones."),
                is_critical=False)
        except Exception as e:
            logger.warning(f"Could not send the cookie alert: "
                           f"{type(e).__name__}")

    # ── finding out why, without spending a pin slot ─────────────

    async def probe(self, prompt: str, model: str = "") -> dict:
        """
        Ask for one picture and report EXACTLY what came back.

        The pipeline is built to swallow a failure here -- a refused prompt
        must never cost a slot, so _make returns None and the caller quietly
        publishes a photograph instead. That is right in production and
        useless when the question is "why is there never an image", because
        every cause produces the same silent None.

        This is the same call with nothing swallowed: the reply text, the
        model that answered, the models the account can reach. It publishes
        nothing and counts towards nothing.
        """
        out = {"ok": False, "images": 0, "text": "", "error": "",
               "model_asked": model or "(library default)",
               "available": [], "saved": ""}
        try:
            client = await self._connect()
        except Exception as e:
            out["error"] = f"connect: {type(e).__name__}: {e}"
            return out
        if client is None:
            out["error"] = f"no session: {self.last_error}"
            return out

        # What this ACCOUNT can reach, which is not what the library knows
        # about -- the tiers depend on the subscription behind the cookie.
        try:
            models = getattr(client, "available_models", None) or []
            out["available"] = [str(getattr(m, "name", m)) for m in models]
        except Exception as e:
            out["available"] = [f"(could not list: {type(e).__name__})"]

        kwargs = {}
        if model:
            try:
                from gemini_webapi.constants import Model
                kwargs["model"] = Model[model]
            except Exception:
                kwargs["model"] = model

        try:
            output = await asyncio.wait_for(
                client.generate_content(prompt, **kwargs), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            out["error"] = "timed out"
            return out
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
            return out

        images = getattr(output, "images", None) or []
        out["images"] = len(images)
        out["text"] = " ".join((getattr(output, "text", "") or "").split())[:900]
        out["kinds"] = [type(i).__name__ for i in images]
        if images:
            name = "probe_%d.png" % int(time.time() * 1000)
            try:
                os.makedirs(self.image_dir, exist_ok=True)
                saved = await images[0].save(path=self.image_dir,
                                             filename=name, verbose=False)
                out["saved"] = str(saved or name)
                out["ok"] = True
            except Exception as e:
                out["error"] = f"could not save: {type(e).__name__}: {e}"
        return out

    @property
    def status(self) -> dict:
        return {
            "ready": self.is_ready,
            "style_this_week": photo_styles.style_for()["name"],
            # A STARTED SESSION IS NOT A SIGNED-IN ONE. init() succeeds on a
            # dead cookie -- it gets the signed-out web app, which talks back
            # happily and generates nothing. Reporting that as "session: true"
            # is how a dead cookie passed for a working one.
            "session": self._client is not None and not self.signed_out,
            "signed_out": self.signed_out,
            "made": self.made,
            "refused": self.refused,
            "failures": self._failures,
            "resting": not self.awake,
            "last_error": self.last_error,
            "last_reply": self.last_reply[:400],
        }
