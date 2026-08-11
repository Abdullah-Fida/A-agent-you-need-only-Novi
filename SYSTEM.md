# NOVI — Complete System Reference

Everything this bot does, when it does it, and how to control it.

---

## 1. What this system is

Modules under one brain, all controllable by voice through NOVI (the orb
dashboard) or directly over HTTP.

| # | Module | What it does | Default |
|---|--------|--------------|---------|
| 1 | **News Agent** | Scrapes news → AI writes a post → generates an image → publishes to your Telegram channel | **OFF** |
| 2 | **Signal Copier** | Watches crypto channels → AI strips competitor branding → posts into your Whale Tracker group | **ON** when connected |
| 3 | **Stealth Marketer** | Grows subscribers: replies helpfully in competitor groups, and sends opt-in invitations | **OFF** |
| 4 | **Website / Auto-Blogging** | Writes a full 800–1200 word SEO article per story and publishes it to the website | **OFF** |
| 5 | **Fan-out** | Cross-posts each story to Reddit, X and **Facebook (via Buffer)** | Runs with News Agent |
| 6 | **Growth Engine** | Measures whether any of it is actually gaining subscribers, and recommends changes | Always measuring |

Every module can be switched on/off independently, and the **Master Kill Switch**
stops all of them instantly. **All settings persist in Supabase**, so a restart
or redeploy no longer resets your toggles and limits.

### What one news story produces

```
        ┌──────────────► Telegram channel   (post + image)
        │
News ───┼──────────────► Website article    (800-1200 words, full SEO)
story   │
        ├──────────────► Facebook           (via Buffer, links the article)
        ├──────────────► X / Twitter
        └──────────────► Reddit
```

The website article is written **first**, so its URL can be linked from the
Facebook caption. Each destination is isolated — one platform failing never
stops the others.

---

## 1b. AI models — which model does which job

There are **two independent AI engines**, so the blog writer can run on a
stronger (or paid) model without touching anything else:

| Engine | Serves | Configured by |
|--------|--------|---------------|
| **NewsAI** | News agent, signals, stealth, captions, images | `OPENROUTER_API_KEYS` |
| **ArticleAI** | The Article Agent only | `ARTICLE_API_KEYS` (falls back to NewsAI if unset) |

`ArticleAI` speaks any OpenAI-compatible endpoint — **OpenRouter, Groq, or
OpenAI**:

```ini
ARTICLE_API_KEYS=gsk_xxxxxxxx
ARTICLE_API_PROVIDER=groq            # openrouter | groq | openai
ARTICLE_MODEL=llama-3.3-70b-versatile
# ARTICLE_API_BASE=                  # only for a custom endpoint
```

When `ARTICLE_API_KEYS` is empty the Article Agent simply shares NewsAI, so
nothing breaks if you leave it blank.

Within an engine, calls route by *task*: the shared NewsAI uses OpenRouter's
`openrouter/free` auto-router per task; a dedicated ArticleAI always uses its
one configured model.

| # | Task key | Used by | What it produces | Tokens | Temp |
|---|----------|---------|------------------|--------|------|
| 1 | `synthesizer` | Content Engine | The Telegram post + tweet + Reddit copy, from 2–3 source articles | 1200 | 0.7 |
| 2 | `article` | Article Agent | The full 800–1200 word website article (HTML) | 3000 | 0.7 |
| 3 | `seo` | Article Agent | meta title, description, keywords, slug | 700 | 0.3 |
| 4 | `social_caption` | Buffer Broadcaster | The Facebook caption (longer, hashtagged) | 420 | 0.7 |
| 5 | `signal_cleansing` | Signal Copier | Rewrites a crypto signal, stripping competitor branding | 500 | 0.2 |
| 6 | `stealth` | Stealth Marketer | Human-sounding group replies and invitation messages | 150 | 0.8 |

Images do **not** use a language model — the picture is generated from the
story's own headline by the image providers below, so no token budget applies.

**Failover chain.** Each call retries at least 3 times with exponential backoff.
On a 404 / "model unavailable" it walks a fallback list; on 429/401 it rotates
to the next API key. Only after all of that does it give up and email you.

### Pointing a task at a different model

Set an env var — no code change:

```ini
MODEL_ARTICLE=deepseek/deepseek-r1
MODEL_SEO=openai/gpt-4o-mini
MODEL_SYNTHESIZER=anthropic/claude-3.5-haiku
```

This is the seam for the dedicated blog-writing agent you plan to add: point
`MODEL_ARTICLE` at it and nothing else changes.

> **One real-world gotcha, already handled.** The free auto-router sometimes
> returns a *reasoning* model that narrates its thinking before answering. The
> SEO prompt originally asked for "meta_title (<=60 chars)", and the model spent
> its entire token budget counting letters out loud, so no JSON ever arrived.
> Length limits are now enforced in code, and the JSON parser recovers values
> from reasoning preambles and truncated replies.

---

## 2. Daily timetable (all times Pakistan Standard Time, UTC+5)

### Posting schedule

| Time | What happens |
|------|--------------|
| 00:40 | Post slot |
| 08:00 | Morning brief (digest of top 5 stories) |
| 10:30 | Post slot |
| 13:00 | Post slot |
| 13:40 | Post slot |
| 16:00 | Post slot |
| 19:30 | Post slot |
| 21:00 | Evening wrap |

**How a slot actually fires:** when the clock enters a slot, the bot waits a
random 0–10 minutes ("human jitter") so posts never land on an exact minute.
Each slot stays open for **25 minutes**, which is deliberately longer than the
maximum jitter so a post can never miss its own window.

Each slot fires **at most once per day**, tracked by a unique key. (Previously
13:00 and 13:40 shared an "hour" key, so the 13:40 post never happened.)

### Sleep window

**Default: 23:00 → 07:00 PKT.** While asleep the bot makes no posts, no replies
and no invitations. It stays connected and the API keeps answering.

Change it at any time — no restart needed:

> *"Novi, sleep from 1am to 6am"*
> *"Novi, never sleep"* → 24/7 mode
> *"Novi, are you sleeping?"* → tells you the current state and window

### Background loops

| Loop | Interval | Purpose |
|------|----------|---------|
| Keep-alive ping | **4 min** | Stops Render idling the free instance |
| Heartbeat | **5 min** | Checks every Telegram client and **auto-reconnects** dead ones |
| Stealth invite cycle | 1 hour | Runs one scrape+invite pass, if all safety gates allow |
| Metric ingest | 3 hours | Records subscriber count, updates growth stats |
| Main tick | 60 sec | Checks whether a post slot is due |

---

## 3. Daily limits — every one is enforced

Set in `.env`, changeable live by voice.

| Setting | Default | Controls |
|---------|---------|----------|
| `MAX_DAILY_POSTS` | 6 | News posts to your Telegram channel |
| `MAX_DAILY_REDDIT_POSTS` | 2 | Reddit cross-posts |
| `MAX_DAILY_X_POSTS` | 5 | X/Twitter cross-posts |
| `MAX_DAILY_TELEGRAM_REPLIES` | 8 | Stealth replies in competitor groups |
| `MAX_DAILY_INVITES` | 2 | People invited per day (**hard cap 30**) |
| `MAX_DAILY_SIGNALS` | 0 | Signals copied per day (0 = unlimited) |

> Before this pass, the Reddit/X/replies settings were read from `.env` and then
> **ignored** — hardcoded values were used instead. They now take effect.

**Changing a limit by voice:**

> *"Novi, increase posts to 10"*
> *"Novi, increase invites to 5"*
> *"Novi, set signals to 20"*

The invite limit is capped at **30/day in code**. If you ask for more, NOVI
applies 30 and tells you it was capped — it will not silently pretend otherwise.
This cap exists because Telegram bans burner numbers that invite aggressively.

---

## 4. Controlling everything through NOVI

Say these to the orb:

### Posting
| Say | Effect |
|-----|--------|
| "Create a post" / "draft a post" | Drafts a post for review — does **not** publish |
| "Publish it" | Publishes the draft you just reviewed |
| **"Post now"** | Drafts **and** publishes in one step |
| "Post about crypto" | Same, restricted to a category |

### Subscriber growth
| Say | Effect |
|-----|--------|
| **"Add subscribers now"** | Runs an invite cycle immediately instead of waiting for the hourly loop |
| **"How is growth?"** | Measured growth + a recommendation on what to change |
| "Increase invites to 5" | Raises the daily invite limit |

### Modules
| Say | Effect |
|-----|--------|
| "Turn on the news agent" | Starts scheduled news posting |
| **"Turn on the website"** | Starts auto-blogging (**OFF by default** — while off, no article is written at all and the article API key is never charged) |
| "Start the whale tracker" | Starts signal copying |
| "Turn on stealth replies" | Starts replying in competitor groups |
| "Turn on member adding" | Starts the invitation engine |
| **"Stop everything"** | Master kill switch — all modules off |

### Status
| Say | Effect |
|-----|--------|
| **"Is everything working?"** | Full health: what's on, what's connected, awake or asleep |
| "Is Telegram connected?" | Connection status for the main account |
| "Is stealth connected?" | Connection status for the burner |
| "Send a test email" | Verifies the notification pipeline |

---

## 5. How each module works

### 5.1 News Agent

```
RSS feeds → dedupe → relevance score → group related stories
   → AI writes Telegram + tweet + Reddit versions
   → generate branded image
   → publish → cross-post to Reddit/X (within their own limits)
   → email you a confirmation
```

**Language.** Everything published is **100% English**. No prompt asks for
Urdu, Roman Urdu or transliteration anywhere — the morning brief and the
stealth replies previously did, and the brief went out to the channel in Roman
Urdu as a result. A test now fails the build if any prompt reintroduces it.

**Morning brief safety net.** The free model router sometimes lands on a
reasoning model that narrates ("We need to produce a morning brief with a
numbered list…") instead of answering, and that narration would be published
verbatim. The reply is now anchored on the greeting and must contain real
numbered items; if it does not, the brief is composed directly from the
stories instead.

**Sources:** 21 feeds across tech/AI, business, world, crypto, Pakistan, sports.
**Relevance scoring:** South-Asia keywords +10, crypto +8, general interest +3.
Highest-scoring story cluster wins.

Stories are grouped when their titles overlap >40%, so the AI reads 2–3
perspectives on the same event before writing.

### 5.1a Images — every post carries one

Pictures come from **Bing Image Creator (DALL·E 3) and nowhere else** — no
other image AI is used. When Bing cannot deliver, the fallback is real
photography, not a different generator:

| # | Source | Typical time | Needs | Notes |
|---|--------|--------------|-------|-------|
| 1 | **Bing Image Creator (DALL·E 3)** | ~30–90 s | `BING_COOKIE` | The only generator. Retried twice per post |
| 2 | **The photo published with the story** | ~2 s | story has one | About 3 in 4 scraped stories carry one |
| 3 | **Branded headline card** | instant | nothing | Drawn locally: gradient, category eyebrow, headline, wordmark |

Tier 3 needs no network and no installed fonts, so a post can only ever lose
its image if the disk write itself fails.

**One image per story.** It is generated once, uploaded once to Supabase
Storage, and reused by Telegram (local file), Facebook and the website
article — so the same story looks the same everywhere and costs one Bing
image, not three.

**When the cookie expires.** The `_U` cookie lasts a few weeks and its death
is silent — Bing simply stops redirecting and serves the signed-out page. That
is detected and **emailed to you** with step-by-step replacement instructions.
The alert is sent once, not once per post, and again only after six hours; when
Bing starts working again you get a short "back to normal" note. Posts keep
going out on the fallback tiers in the meantime.

**Guard rails**

- The whole chain is capped at **150 seconds**; a hung provider can never eat
  a posting slot.
- Generation is fully async. It used to run synchronously inside the event
  loop, which froze the scheduler, the dashboard API and Telegram's keepalives
  for up to six minutes per post.
- Output is a 1280×720 **JPEG** (~100 KB, down from ~1.1 MB as PNG) and is
  re-opened and verified before it is attached to anything.
- If a post *does* go out without a picture, it is recorded in `error_logs`
  and **emailed to you as a critical alert** — it is treated as an incident,
  not a log line.
- `posts.metadata` records `has_image`, `image_status` and `image_source`, so
  the database says which tier produced each picture and whether it was really
  delivered. Previously the intended path was stored on both branches, which
  made a text-only post indistinguishable from an illustrated one.

### 5.2 Signal Copier (Whale Tracker VIP)

```
Watch source channels → new message
   → AI cleanses it (strips competitor links/branding, keeps prices exactly)
   → REJECT if it's a locked VIP teaser or an advert
   → wait 1–2 min (human delay)
   → post into your group + email you
```

**Duplicate protection:** each signal is fingerprinted; edits and reposts of the
same signal are ignored.

**Why it wasn't working before:** three separate faults, all now fixed —
1. `signal_copier.py` had **never been committed to git**, so it did not exist in production at all.
2. Source channels were passed to the event filter as raw strings; when Telethon couldn't resolve them the filter silently matched nothing.
3. The target group ID `-5533411583` was missing the `-100` supergroup prefix, so every send failed.

### 5.3 Stealth Marketer

Two independent modes:

**Reply mode** — watches competitor groups for questions containing trigger
keywords, replies ~1% of the time with a genuinely helpful answer that mentions
your channel naturally. Waits 2–5 minutes and simulates typing first.

**Invite mode** — scrapes recently-active members, filters out anyone already
contacted, then sends **one opt-in invitation by DM**.

> **Important change:** this no longer force-adds anyone. You asked for this
> explicitly, and it is also the single fastest way to get a burner number
> banned. It now sends a personal invite link and lets the person choose.

### 5.4 Auto-Blogging (the website)

Every published news story also becomes a full article at `SITE_URL/{slug}`.

```
story → AI writes 800-1200 words of HTML
      → in parallel:  AI writes meta title / description / keywords / slug hint
                      image generator draws the hero, uploaded to Supabase Storage
      → slug made unique (adds -2, -3 … if taken)
      → reading time + word count computed
      → saved to Supabase `articles`
      → Next.js serves it, already in the sitemap and RSS feed
```

**Quality gate:** anything under 250 words is rejected rather than published.

**Hero images.** The article reuses the picture already generated and hosted
for the Telegram post, so one Bing image serves every channel. If there isn't
one it generates its own, and failing that it uses the photo published with the
original story. Images live in the public `article-images` Supabase Storage
bucket created by `database/schema.sql` — until that has been run, uploads fail
and articles fall back to the outlet's photo, with a warning logged.

**SEO shipped per article:** canonical URL, OpenGraph + Twitter cards,
`NewsArticle` JSON-LD, `BreadcrumbList` JSON-LD, keyword meta, and an entry in
both `/sitemap.xml` and `/feed.xml`. Site-wide there is `NewsMediaOrganization`
and `WebSite` structured data.

> The site-wide metadata was still the create-next-app default
> ("Create Next App" / "Generated by create next app") — that was what Google
> and every social share preview displayed. It is now proper site metadata.

### 5.5 Facebook (via Buffer)

Posts go to Facebook through Buffer's **GraphQL** API at
`https://api.buffer.com/graphql`.

Three things worth knowing, all learned by probing the live API:

- The legacy REST API (`api.bufferapp.com`) **rejects modern public tokens** and
  retires 2027-02-01. Only the GraphQL endpoint works.
- `createPost` returns a *union*. Failures come back as **HTTP 200** with an
  error variant, never an error status — so the response must be read through
  `__typename` or failures look like successes.
- Facebook posts **require** `metadata.facebook.type` (`post` | `reel` | `story`).
  Omitting it fails with "Facebook posts require a type".
- The image goes in `assets` as `[{ image: { url, thumbnailUrl } }]`, and
  **Buffer fetches that URL itself** — a local file path is useless here, which
  is why the picture is uploaded to Supabase Storage first.

Posts are added to your Buffer queue (`addToQueue`), so they respect the posting
schedule you already configured in Buffer. The caption is rewritten for
Facebook's format and ends with a link to the full website article.

> Facebook posts previously went out with **no picture**: the image URL was
> worked out and written to the database, but `assets` was left empty, so
> nothing was ever attached.

### 5.6 Growth Engine

Records subscriber counts and every growth action, then tells you what's working:

- `stalled` — no growth in 24h, with specific things to change
- `behind` — growing but under the weekly pace
- `on_track` — working; explicitly recommends changing nothing

---

## 6. Anti-detection strategy

| Protection | Detail |
|------------|--------|
| **Burner account** | Stealth runs on a separate number, never your personal one |
| **Stable device fingerprint** | Derived from account identity, so it does **not** change between restarts (a changing device is an obvious bot signal) |
| **Sleep-hour enforcement** | No invites/replies at 4 AM local time |
| **Minimum invite spacing** | ≥25 min between invitations regardless of the daily limit |
| **Hard daily cap** | 30/day maximum, enforced in code |
| **Randomised delays** | 30–120 s before invites, 2–5 min before replies, 30–90 min between groups |
| **Typing simulation** | Realistic typing duration before sending |
| **Consent-based invites** | No force-adding |
| **Zero-trace logging** | Scraped user IDs never touch console or log files |
| **Auto-kill triggers** | `PeerFloodError`, `FloodWait > 60s`, session revoked, number banned → module locks itself and emails you |
| **Emergency lock** | After auto-kill, reactivation is **blocked** until you manually reset |

---

## 7. Email notifications

You are emailed for: every post published, every signal copied, every invite
sent, every module on/off, every limit change, every connection loss/recovery,
every error, and the weekly growth report.

**Delivery:** Resend API (HTTPS) first, Gmail SMTP as fallback, **3 retries each**.

> Render's free tier **blocks outbound SMTP ports**. If you only configure Gmail,
> you get no email in production. Set `RESEND_API_KEY`.

Undelivered emails are recorded and visible at `/api/health` → `email`, so a
silent outage becomes visible.

---

## 7b. Everything stored in Supabase

Run **`database/schema.sql`** in the Supabase SQL Editor. It is idempotent.

| Table | Holds |
|-------|-------|
| `posts` | Every post on every platform |
| `articles` | The website's content |
| `metrics` | Subscriber counts, limit changes, module toggles |
| `alerts` | Dashboard notifications |
| `error_logs` | Every error, and whether it self-healed |
| `scraped_users` | Stealth invite dedupe (who has been contacted) |
| `bot_state` | Module toggles + limits — **survives restarts** |
| `social_posts` | Cross-platform delivery tracking, incl. Buffer IDs |

> **Two of these did not exist**, so every write to them was silently discarded:
> `articles` (which is why the website had no content) and `scraped_users`
> (which is why the stealth marketer could never remember who it had contacted).
> Separately, the old `articles` RLS policy required `authenticated` /
> `service_role`, but the bot connects with the **anon** key — so even once the
> table existed, every insert would have been rejected with no visible error.
> Both are fixed in the new schema.

The bot verifies the schema at startup and logs exactly which tables are missing
rather than failing silently forever.

---

## 8. Configuration reference

```ini
# Database
SUPABASE_URL / SUPABASE_KEY

# AI — comma-separated, more keys = better failover
OPENROUTER_API_KEYS=key1,key2

# Main Telegram account (news channel)
TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_PHONE
TELEGRAM_SESSION_STRING      # REQUIRED in production
CHANNEL_USERNAME=@Novi_Network

# Burner account (stealth)
STEALTH_PHONE / STEALTH_SESSION_STRING
TARGET_STEALTH_GROUPS=@group1,@group2
STEALTH_INVITE_GROUP=-100xxxxxxxxxx

# Signal copier
SOURCE_SIGNAL_CHANNELS=@channel1
SIGNAL_TARGET_GROUP=-100xxxxxxxxxx

# Limits + schedule
MAX_DAILY_POSTS=6
MAX_DAILY_INVITES=2
SLEEP_START_HOUR=23
SLEEP_END_HOUR=7

# Facebook via Buffer
BUFFER_ACCESS_TOKEN=xxx
BUFFER_ORGANIZATION_ID=xxx   # auto-discovered if omitted
BUFFER_SERVICES=facebook

# Website
SITE_URL=https://your-domain.com
SITE_NAME=Novi News

# Per-task model overrides (optional)
MODEL_ARTICLE=               # e.g. your dedicated blog agent
MODEL_SEO=

# Email
RESEND_API_KEY=re_xxx        # strongly preferred
EMAIL_RECEIVER=you@gmail.com

# Deployment
RENDER_EXTERNAL_URL=https://your-app.onrender.com
ENABLE_TWITTER=false         # needs ~400MB RAM; will OOM a free Render instance
```

The website reads its own `news_ecosystem_web/.env.local`:

```ini
NEXT_PUBLIC_SUPABASE_URL=      # must match the bot's SUPABASE_URL
NEXT_PUBLIC_SUPABASE_ANON_KEY=
NEXT_PUBLIC_SITE_URL=
NEXT_PUBLIC_SITE_NAME=Novi News
```

> These were left as literal placeholders (`https://your-supabase-url.supabase.co`),
> so the website was never connected to the database at all.

### ⚠ Group IDs — the `-100` rule

Telegram supergroups need a `-100` prefix:

```
-5533411583      ✗ will not resolve
-1005533411583   ✓ correct
```

The bot now auto-corrects this at runtime and warns you at startup, but fix it
in `.env` to remove the ambiguity. Find the right ID with:

```bash
python tools/list_groups.py
```

---

## 9. HTTP API

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/health` | GET | **Everything at once** — modules, connections, awake/asleep, email, growth |
| `/api/report` | GET | Quick stats |
| `/api/schedule` | GET | Current sleep window + post slots |
| `/api/schedule/sleep_window` | POST | Change sleep hours |
| `/api/modify_limits` | POST | Change any limit (`post`/`stealth`/`stealth_invite`/`signal`) |
| `/api/post_now` | POST | Create **and** publish everywhere (Telegram + website + FB + X + Reddit) |
| `/api/create_post_stream` | GET | Draft with live progress (SSE) |
| `/api/publish_post` | POST | Publish the last draft |
| `/api/growth/status` | GET | Measured growth + recommendation |
| `/api/growth/invite_now` | POST | Run an invite cycle now |
| `/api/news/toggle` | POST | News agent on/off |
| `/api/signal_copier/toggle` | POST | Signal copier on/off |
| `/api/stealth/toggle_reply` | POST | Reply mode on/off |
| `/api/stealth/toggle_invite` | POST | Invite mode on/off |
| `/api/master_kill` | POST | Stop everything |
| `/api/stealth/emergency_reset` | POST | Clear the emergency lock |
| `/api/logs` | GET | Last 200 log lines |
| `/ping` | GET | Keep-alive |

---

## 10. Running and testing

```bash
# Run the bot
python main.py

# Run every test (64 unit/integration + 52 QA invariants)
python run_tests.py

# Find a group's real ID
python tools/list_groups.py

# Generate a session string for production
python tools/telegram_login.py
```

**Dashboard:**
```bash
cd novi-dashboard
npm install
npm run dev          # local, talks to localhost:8000
npm run build        # production; set VITE_API_BASE to your backend URL
```

---

## 11. Deployment checklist

1. **Run `database/schema.sql` in the Supabase SQL Editor** — until you do,
   articles and scraped users are silently discarded
2. `python run_tests.py` — must pass (156 checks)
3. **Commit `modules/signal_copier.py`** — it was never in git, which is why the
   Whale Tracker never ran in production
4. Set `RENDER_EXTERNAL_URL` — without it the keep-alive pings localhost and
   Render still idles you
5. Set `RESEND_API_KEY` — SMTP is blocked on Render free
6. Set `TELEGRAM_SESSION_STRING` and `STEALTH_SESSION_STRING` — file sessions do
   not survive a deploy
7. Fix the `-100` prefix on both group IDs
8. Set `SITE_URL` (bot) and the four `NEXT_PUBLIC_*` vars (website)
9. Leave `ENABLE_TWITTER` unset unless you are on ≥1GB RAM
10. Rebuild the dashboard so it stops pointing at `localhost:8000`
11. After deploy, open `/api/health` and confirm every module reads as expected

---

## 12. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Bot "randomly stops working" | Render idled the free instance | Set `RENDER_EXTERNAL_URL` |
| No signals reaching your group | Signal copier not deployed / bad group ID | Commit the file; fix `-100` prefix |
| No emails | SMTP blocked on Render | Set `RESEND_API_KEY` |
| Dashboard says "backend not reachable" | Old build hardcoded to localhost | Rebuild the dashboard |
| Posts stop after a while | Daily limit reached | "Novi, increase posts to N" |
| Invites do nothing | Emergency lock engaged | `/api/stealth/emergency_reset` |
| Nothing happens at night | Sleep window | "Novi, never sleep" |
| `AI keys exhausted` | Single key rate-limited | Add more keys to `OPENROUTER_API_KEYS` |
| Website shows "No stories published yet" | `articles` table missing, or website `.env.local` not set | Run `database/schema.sql`; set the `NEXT_PUBLIC_*` vars |
| Articles never appear | RLS blocked the anon key | Re-run `database/schema.sql` (fixes the policy) |
| Facebook posts don't send | Buffer channel disconnected | Check `/api/health` → `buffer`; reconnect at buffer.com |
| Buffer returns 401 | Using the legacy REST host | Only `api.buffer.com/graphql` accepts modern tokens |
| Settings reset after redeploy | `bot_state` table missing | Run `database/schema.sql` |
| SEO metadata is generic | Reasoning model burned its tokens | Already handled; pin `MODEL_SEO` to a non-reasoning model to be safe |

---

## 13. Project layout

```
omni_channel_bot/
├── main.py                  Orchestrator: startup, loops, scheduling
├── run_tests.py             Runs every test
├── SYSTEM.md                This document
├── core/
│   ├── brain.py             Schedule, sleep, limits, error handling
│   ├── ai_engine.py         Multi-key AI with failover + model fallback
│   ├── api_server.py        HTTP API for the dashboard
│   └── config.py            Env loading + startup validation
├── database/
│   ├── schema.sql           ALL 8 tables — run this in Supabase
│   └── supabase_db.py       Non-blocking DB layer + schema verification
├── modules/
│   ├── news_scraper.py      RSS ingestion, dedupe, scoring
│   ├── content_engine.py    Full content pipeline
│   ├── article_engine.py    Auto-blogging: long-form articles + SEO
│   ├── image_generator.py   Branded images
│   ├── telegram_broadcaster.py
│   ├── signal_copier.py     Whale Tracker VIP
│   ├── stealth_marketer.py  Growth + anti-detection
│   ├── growth_engine.py     Measurement + recommendations
│   ├── fanout.py            Cross-posting to every secondary platform
│   ├── buffer_broadcaster.py  Facebook via Buffer GraphQL
│   ├── notification_manager.py
│   ├── reddit_broadcaster.py
│   └── twitter_broadcaster.py
├── utils/telegram_utils.py  Chat-ID resolution (the -100 fix)
├── tools/                   list_groups.py, telegram_login.py
├── tests/                   test_suite.py, test_integrations.py, qa_check.py
└── novi-dashboard/          React + Three.js voice UI

news_ecosystem_web/          The public website (Next.js 16)
├── app/
│   ├── layout.tsx           Site metadata + org/site JSON-LD
│   ├── page.tsx             Homepage (lead grid)
│   ├── [slug]/page.tsx      Article page + NewsArticle JSON-LD
│   ├── category/[category]/ Category pages
│   ├── sitemap.ts           Every article, freshness-weighted
│   ├── robots.ts
│   └── feed.xml/route.ts    RSS 2.0 feed
└── lib/supabase.ts          Typed article queries
```
