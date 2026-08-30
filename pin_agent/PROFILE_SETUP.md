# Pinterest profile setup — Tidy Nook

Everything below is copy-paste ready. Assets are in `assets/brand/`.

---

## The category

**Small-space kitchen and home organization.**

Chosen over the alternatives for four reasons:

| | Why it wins |
|---|---|
| **Pinterest fit** | Home organization is one of Pinterest's largest evergreen search clusters. The audience is ~70% women planning purchases — the exact behaviour affiliate links need. |
| **Commission** | AliExpress pays roughly 7-9% on Home & Kitchen. Consumer electronics and phone accessories pay 3-5%. |
| **Ban risk** | Near zero. Storage jars and drawer dividers are generic goods. Jewelry, sneakers and branded electronics on AliExpress are frequently counterfeit, and counterfeit links are how affiliate accounts get permanently banned. |
| **Shelf life** | Evergreen. A pin from two years ago still drives clicks. Seasonal niches (Christmas, Halloween) die for ten months of the year. |

**Rejected:** jewelry and fashion (counterfeit risk), electronics (low commission,
male-skewed, weak on Pinterest), craft supplies (order values too small to earn).

### One honest caveat

AliExpress's affiliate cookie is short, and Pinterest traffic is slow — people
save a pin now and buy weeks later, often after the cookie has expired. Expect
this to build slowly. It does not change the category choice; it does mean
judging results over months, not days.

### Config change already made

`PIN_MIN_PRICE` raised from `3` to `12`. At 8% commission a $4 product earns
about 30 cents, and the pin costs exactly as much to produce as one for a $40
product. Cheap listings also have the worst photos and the longest shipping.

---

## Account

1. Create the account, then **Settings > Account management > Convert to a
   business account**. Free, and required for analytics and ads later.
2. Country: whichever you actually operate from. Language: English.

## Profile picture

Use **`assets/brand/avatar_terracotta.jpg`** (1000x1000).

A cream alcove with two stocked shelves, on terracotta. Terracotta ground
rather than cream because Pinterest's interface is white — a pale avatar
disappears in the feed. `avatar_cream.jpg` is the inverse if you prefer it.

The mark matches the pin template exactly, so the profile and the pins read as
one brand.

## Name

```
Tidy Nook | Small Kitchen & Home Organization
```

Pinterest indexes the name field in search, which is why the keywords are
there rather than just the brand.

## Username

```
tidynookhome
```

Fallbacks if taken: `tidynookideas`, `thetidynook`, `tidynook.co`

## Bio

```
Clever storage for small kitchens and tiny apartments. I find the organizers, racks and containers that actually fit - and actually work. New finds daily. Some links are affiliate links (#ad).
```

500 character limit; about 160 show before "more". The disclosure sits in the
bio as well as every pin, which is what the FTC asks for.

---

## Boards

Create all five. Cover images are in `assets/brand/`.

### 1. Small Kitchen Organization
> Storage ideas for kitchens with no counter space. Drawer dividers, cabinet risers, over-the-sink racks and corner organizers that make a small kitchen work harder. Affiliate links marked #ad.

### 2. Kitchen Gadgets Worth Buying
> Kitchen tools that earn their drawer space - herb scissors, jar openers, measuring sets and prep gadgets. Skip the clutter, keep the useful ones. Affiliate links marked #ad.

### 3. Pantry and Fridge Storage
> Airtight jars, stackable bins, egg holders and fridge organizers that stop food going to waste and make shelves easy to read at a glance. Affiliate links marked #ad.

### 4. Tiny Apartment Solutions
> Space-saving ideas for renters and small flats - foldable racks, wall hooks, over-door storage and furniture that does two jobs. Affiliate links marked #ad.

### 5. Under Sink and Cabinet Storage
> The most wasted space in the house, sorted. Pull-out drawers, sliding trays, tension rods and stacking shelves for under-sink cabinets. Affiliate links marked #ad.

To set a cover: board > pencil icon > Cover > upload the matching
`board_N_*.jpg`.

**Leave every board public.** A secret board publishes nothing.

---

## After the boards exist

1. In Buffer, **disconnect and reconnect** the Pinterest channel. Buffer caches
   the board list at connection time and will not see new boards otherwise.
2. Tell me, and I will re-run the live pin.

---

## Warm-up

A brand-new account that immediately posts eight affiliate links a day is the
classic spam signature. For the first ten days:

```
PIN_MAX_PER_DAY=4
```

Then raise it to 8. Pinterest's own guidance tops out around 15, and the config
clamps there.

---

## Known gap

`PIN_BOARD_ID` sends every pin to a single board. With five boards, pins should
be routed by product category — otherwise four boards sit empty and one looks
like a dumping ground. This is the next code task in the Pinterest module,
after the AliExpress keys are in.
