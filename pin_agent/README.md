# Pinterest Agent

Finds home-and-kitchen products on AliExpress, writes a pin for each, builds a
branded image, checks it against Pinterest's affiliate rules, and publishes it.

Lives in its own folder with its own AI key and its own Buffer account. The
only thing it shares with Novi is the Supabase database. It is **off by
default** and switched on from the Novi dashboard.

---

## Why it publishes through Buffer

Pinterest's own API grants **Trial access** first, where every pin created is a
sandbox entity **visible only to its creator** — nothing reaches Pinterest.
Standard access needs a video demo of the OAuth flow and takes one to four
weeks of review.

Buffer is an official Pinterest Marketing Partner, so posting through it needs
no approval of ours and stays inside Pinterest's terms. Browser automation
would not: it breaches those terms and risks the account.

Applying for Standard access later is still worth doing, for the analytics
(saves, outbound clicks) Buffer does not expose. It is not needed to publish.

---

## The pipeline

```
AliExpress  ->  filter & rank  ->  write copy  ->  build image
                                                       |
Pinterest  <-  publish  <-  human review  <-  compliance gate
```

| Stage | File | What it does |
|---|---|---|
| Source | `sourcing.py` | Signed AliExpress Portals query. Falls back to sample products with no keys. |
| Select | `selector.py` | Hard filters, then ranking. Order count is log-compressed so one viral listing cannot dominate forever. |
| Write | `content.py` | Pin title and description, rotating between six angles. |
| Image | `imaging.py` | 1000x1500 branded pin. AliExpress photos are square; Pinterest ranks 2:3. |
| Check | `compliance.py` | Plain code, never a model. |
| Publish | `publisher.py` | Queues to Pinterest through Buffer. |

### What the compliance gate refuses

Every rule is deterministic. A language model would be right most of the time,
and the failure mode is losing the account.

- Shortened or cloaked links (`bit.ly`, `linktr.ee`, …) — Pinterest treats
  these as spam
- Any host that is not AliExpress, including lookalikes like
  `aliexpress.com.attacker.net`
- A missing `#ad` disclosure
- **Any price in the copy** — AliExpress prices move constantly and a pin
  outlives them by months, so a stated price becomes a false claim
- Duplicates, checked on product id, destination link **and** image
  fingerprint, because AliExpress relists identical products under new ids

### Review

While `PIN_REQUIRE_REVIEW` is on, every pin waits for a decision and you get an
email. This recommends purchases rather than opinions, so catching an invented
feature before it publishes is cheap insurance. Turn it off once the output has
earned trust.

---

## Setup

### 1. AliExpress

`portals.aliexpress.com` -> Tools -> *Dropshipping and Affiliates developer
API*. Needs ID; takes a few days. Gives an App Key, an App Secret, and a
tracking ID.

Until then the agent runs on sample products, so everything else is testable.

### 2. Buffer (a second, separate account)

1. Create a **new** Buffer account — separate from the one Novi uses for
   Facebook, so they do not share the free plan's three channels and ten-post
   queue.
2. Connect the Pinterest business account to it.
3. Create a board for the niche.
4. Get an access token from `publish.buffer.com/developers/api`.

### 3. Database

Run `database/pin_schema.sql` in Supabase -> SQL Editor. Creates the
`pin_posts` table and the public `pin-images` bucket. Buffer downloads the pin
image itself, so it must be hosted publicly.

### 4. Environment

```
ALI_APP_KEY=
ALI_APP_SECRET=
ALI_TRACKING_ID=

PIN_BUFFER_TOKEN=
PIN_BUFFER_ORG_ID=          # optional, discovered automatically
PIN_BOARD_ID=               # optional, uses the default board

PIN_AI_KEYS=                # its own key; falls back to Novi's AI
PIN_AI_PROVIDER=groq
PIN_AI_MODEL=openai/gpt-oss-20b

PIN_NICHE=home_kitchen
PIN_MAX_PER_DAY=8           # clamped to 15, Pinterest's guidance
PIN_MIN_GAP_MINUTES=45
PIN_REQUIRE_REVIEW=true
PIN_MIN_RATING=4.3
PIN_MIN_ORDERS=100
PIN_MIN_PRICE=3
PIN_MAX_PRICE=80
```

### 5. Switch it on

Dashboard -> **PINTEREST**, or tell Novi *"turn on the pinterest agent"*.
*"Make a pin"* builds one immediately.

---

## Control

| Endpoint | Purpose |
|---|---|
| `POST /api/pins/toggle` | On/off. Accepts `{"active": true\|false}`. |
| `GET /api/pins/status` | State, plus anything awaiting review. |
| `POST /api/pins/review` | `{"product_id": "...", "decision": "approve"\|"reject"}` |
| `POST /api/pins/run_now` | Build one pin immediately. |

Rejecting a pin also suppresses that product from future runs.

---

## Tests

```
python -m unittest pin_agent.tests.test_pin_agent
```

Runs fully offline. The compliance tests matter most.
