# Novi — project handoff

**Last updated:** 12 August 2026
**Repo:** https://github.com/Abdullah-Fida/A-agent-you-need-only-Novi (branch `main`)
**Read this first when resuming.** `SYSTEM.md` explains how the system works;
this file records where it *is* and what is still broken.

---

## 1. What this is

An autonomous news operation run by one Python bot ("Novi"):

| Piece | Where it runs | URL |
|---|---|---|
| Bot + API backend | Render (San Francisco) | `https://a-agent-you-need-only-novi.onrender.com` |
| Novi dashboard (control panel) | Vercel, root `novi-dashboard` | `https://a-agent-you-need-only-novi.vercel.app` |
| News website (Next.js) | Vercel, root `website` | `https://a-agent-you-need-only-novi-jh45.vercel.app` |
| Database + image storage | Supabase | project `romytbehhwgpbzdtvesf` |
| Telegram channel | — | `@Novi_Network` |
| Facebook | via Buffer | page "Zindagi Ke Rang" |

One monorepo holds all three. Render builds from `/`, the two Vercel projects
build from their own subdirectories.

---

## 2. Live status (last checked 12 Aug 2026)

```
news agent        ON    telegram connected
signal copier     ON    connected, target group resolved
stealth marketer  OFF   connected (uptime 1186 min) — just needs the toggle
website module    ON    4 articles written
buffer/facebook   ON    1 channel
database          connected
```

---

## 3. What was fixed in the last session

Newest first. Every one has regression tests.

- **`80dc331`** Article page: related-stories rail fills the empty side
  margins; light/dark theme switch added to the masthead.
- **`ec2f7ae`** `.lead-img` was the last image rule missing `height: auto`
  (the big homepage photo). Global `img { height: auto }` added so it can't
  recur. Category labels and card hovers now show the accent.
- **`eef9974`** Squashed images fixed on `.hero`/`.card-img`. Scraper now
  finds the publisher's photo in 5 feed patterns (was 2) — coverage went to
  139/180 stories — plus `og:image` recovery from the article page. Accent
  moved from pine green to editorial blue.
- **`7359994`** Removed the hardcoded `novinews.pk` fallback from 6 files
  (it was poisoning canonical URLs and the sitemap). Now resolves via
  `NEXT_PUBLIC_SITE_URL` → `VERCEL_PROJECT_PRODUCTION_URL` → localhost.
- **`addff63`** Asking Novi for "the report of the news agent" **switched the
  news agent off**. Toggles now take an explicit `{"active": true|false}` and
  are idempotent. Added an SEO quality gate (Groq was returning 30-char
  titles and 44-char descriptions). Articles are now filed under a real
  section instead of everything being "News".
- **`170fd55`** Stealth session died after every single connect: it was
  created as a Windows PC but used by Render as an "OnePlus 12". Device
  identity is now shared from `utils/telegram_utils.py`.
- **`d8db19b`** Bing is the only image generator; falls back to the news
  photo, then a branded card. Email alert when the Bing cookie expires.
  Facebook was posting with no image (`assets` was always empty). All
  prompts forced to English (morning brief was going out in Roman Urdu).
- **`8bdbf09`** Image generation made async (it was blocking the event loop
  for up to 6 minutes per post). Post records now say truthfully whether a
  photo was attached.

**Tests: 240 passing** (135 core + 53 integration + 52 QA) — `python run_tests.py`

---

## 4. Still open

### Blocking nothing, but wanted
1. **Domain.** User is evaluating the article agent for 3–4 days first.
   My recommendation was **`novibrief.com`** (available, matches the morning
   brief feature, 10 chars). Alternatives checked and available:
   `novidispatch.com`, `novireport.com`, `novibeat.com`, `novinews.net`.
   `novinews.com`, `novidaily.com`, `novi.news` are taken/expensive.
   When bought: change `NEXT_PUBLIC_SITE_URL` (Vercel) + `SITE_URL` (Render),
   and I offered a rename pass for `SITE_NAME`, footer, About copy, logo.
2. **Theme default.** Switch now exists. Decide whether light should be the
   default for everyone rather than following the reader's system setting.

### Actually broken
3. **Reddit** — posting fails `401`. User cannot create a Reddit app; tried
   two accounts, same error. **Almost certainly the VPN** — Reddit blocks
   datacentre IPs for app creation. Fix: turn VPN OFF, verify account email,
   use `old.reddit.com/prefs/apps`, type **script**, redirect
   `http://localhost:8080`. Then set `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`.
4. **Twitter/X** — module exists and uses **Playwright, not the API**, so no
   paid API is needed. Blocked because Chromium needs ~400MB and Render's
   plan has 512MB; enabling it OOM-kills the whole bot. Options:
   (a) add X API v2 free-tier posting (~500 posts/month, near-zero memory) —
   my recommendation, roughly an hour of work;
   (b) upgrade Render to Standard (2GB, $25/mo) and set `ENABLE_TWITTER=true`.
   Note Render Starter ($7) is still 512MB and will not help.
5. **Stealth marketer is off** — connected and ready, just toggle it on.

### Security housekeeping (not yet done)
6. **Rotate every credential pasted into chat**: Supabase anon key,
   OpenRouter, Groq, Buffer token, Bing cookie, Resend.
7. **Delete `VITE_GROQ_API_KEY`** from the *dashboard* Vercel project —
   anything `VITE_`-prefixed is compiled into public JavaScript. It is
   server-side on Render as `GROQ_API_KEY` now.
8. **Google Search Console** — add the domain, submit `/sitemap.xml`. Worth
   doing only once a real domain is attached.

### Known quality gaps
9. Article quality depends on the source story. A weak wire headline
   ("Here's what happened in crypto today") produces a vague article. Worth
   adding a headline-quality filter that skips roundup/digest stories.
10. Bloomberg and NYT block bot requests, so their stories never get a source
    photo — they fall back to Bing, then the branded card.

---

## 5. Landmines — read before touching these

- **Telegram API ID is `2040`** — the leaked Telegram Desktop credential,
  shared by all three clients. `my.telegram.org` refuses to issue new ones
  for this account (tried many times), so there is no alternative. It makes
  Telegram stricter, but the *deterministic* session killer (device
  mismatch) is fixed.
- **Never run the bot locally while Render is running.** Telegram revokes an
  auth key used from two IPs at once. This is how the previous stealth
  session died.
- **Do not terminate the "OnePlus 12" session** in Telegram → Devices. That
  is the bot. The "Infinix SMRAT 8 PRO / Paris" entry is the user's phone.
- **Vercel build cache silently ships stale `NEXT_PUBLIC_*` values.** They
  are baked in at build time. Always redeploy with *"Use existing Build
  Cache" unticked* after changing them. This has caused two false alarms.
- **`VITE_*` and `NEXT_PUBLIC_*` are public.** Never put a secret in either.
- **`supabase-py` is synchronous** — every call must go through
  `asyncio.to_thread` (already handled in `database/supabase_db.py`).
- **Telegram supergroup IDs need the `-100` prefix.** `utils/telegram_utils.py`
  auto-corrects.
- **`next/image` emits width+height attributes.** Any CSS that scales width
  must also set `height: auto`, or the picture stretches. Caused two rounds
  of "images look stretched".
- **Bing `_U` cookie expires every few weeks.** The bot emails when it dies,
  once per 6 hours, with replacement steps. Posts keep going out on the
  fallback tiers meanwhile.
- **VPN is required from Pakistan to reach Telegram** for the login tool.
  Use a **US** exit — Render is in San Francisco, and a big geographic jump
  between session creation and use is itself a revocation trigger.
- **`website/AGENTS.md` requires reading `node_modules/next/dist/docs/`**
  before writing Next.js code — this is Next 16 and APIs differ.

---

## 6. Configuration map

Real values live in `.env` (local) and `render.env` (mirror of Render).
Both are gitignored. **Never commit them.**

**Render** — the bot. Everything server-side: Telegram, Supabase, OpenRouter,
`ARTICLE_API_KEYS` (Groq, `llama-3.3-70b-versatile`), `GROQ_API_KEY`,
`BING_COOKIE`, Buffer, Resend, `SITE_URL`, limits, sleep window.

**Vercel — website project** (root `website`), 4 vars, all `NEXT_PUBLIC_`:
`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SITE_URL`, `SITE_NAME`.

**Vercel — dashboard project** (root `novi-dashboard`): **no vars needed**;
it auto-detects the Render backend. Only pending action is deleting
`VITE_GROQ_API_KEY`.

**Supabase:** 8 tables (`posts`, `alerts`, `metrics`, `error_logs`,
`articles`, `scraped_users`, `bot_state`, `social_posts`) + a public
`article-images` storage bucket. All created by `database/schema.sql`,
which has been run and verified.

---

## 7. How to resume

```bash
cd "c:\Users\AKR-LAPTOP\Desktop\Telegram bot\omni_channel_bot"
git pull
python run_tests.py                    # expect 240 passing
curl https://a-agent-you-need-only-novi.onrender.com/api/health
```

Useful checks:
- Live site: `https://a-agent-you-need-only-novi-jh45.vercel.app`
- Publish one article through the real pipeline without sending to Telegram:
  the scratchpad script `publish_prod.py` pattern — build ContentEngine +
  Fanout with `brain.website_module_active = True`.
- Bot logs and module toggles: the Novi dashboard, or `/api/health`.

**Suggested order next session:** (1) turn on stealth marketer, (2) Reddit
app creation with the VPN off, (3) decide light-vs-dark default, (4) X API
free tier, (5) domain once the user is happy with article quality.

---

## 8. Working notes

- The user prefers being shown evidence over assurances — verify against the
  live system and say plainly what passed and what did not.
- Several "it didn't change" reports were browser cache or the Vercel build
  cache, not the code. Check the deployed bundle before re-fixing anything.
- Everything published must be **100% English**. A test fails the build if
  any prompt reintroduces Urdu/Roman Urdu.
- **One image per article** — page speed is a ranking factor, each Bing image
  costs 30–90s, and one image is reused across Telegram, Facebook and the
  website so the story looks consistent everywhere.
