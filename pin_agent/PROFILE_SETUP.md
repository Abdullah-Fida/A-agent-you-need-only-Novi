# Pinterest profile setup — Tidy Nook

Copy-paste ready. Assets are in `assets/brand/`.
Account being set up: **abdullahkhan646khan@gmail.com** (the one Buffer is
already connected to — do not create a second account).

---

## 1. The category decision

**Home & kitchen organization — storage, not decor.**

You asked whether to widen it to home decor. Don't. Decor and organization look
similar but behave completely differently:

| | Organization (storage) | Home decor |
|---|---|---|
| What the searcher wants | A fix for an annoying problem | Inspiration, a mood |
| What they do next | Buy a $15 solution today | Save it for "someday" |
| Competing against | Other product pins | Professional interior photography |
| AliExpress fit | Excellent — generic goods | Poor — taste-driven, ships badly, high returns |

Somebody searching *"under sink organizer"* has a cupboard that annoys them
right now. Somebody searching *"kitchen decor ideas"* is daydreaming. Only the
first one converts, and AliExpress's short cookie window makes slow-converting
traffic close to worthless.

So the niche is wider than kitchen alone — it also covers bathroom, closet and
small-apartment storage, which is where the AliExpress inventory depth is — but
it stops short of decor.

### What this changed in the code

`fetch_products()` was being called with no keywords, so it pulled only by
category id and would have returned the same best-sellers every single run
until the deduplication gate starved. There is now a rotating list of 16 search
terms in `sourcing.py`, one per run. Every term is something a person types
when they have a problem, not when they are browsing.

`PIN_MIN_PRICE` also went from `3` to `12`: at 8% commission a $4 product earns
about 30 cents and costs exactly as much to build a pin for as a $40 one.

---

## 2. Create a separate business account

The "Upgrade" button on Pinterest's Personal-vs-Business screen is **free** —
that word is Pinterest's marketing, not a paid plan. Converting would have
worked. A separate account is the better choice anyway:

- The affiliate brand is not attached to your real name and personal saves.
- If the brand account is ever restricted for affiliate activity, your personal
  account is untouched.
- The profile is Tidy Nook from day one, with no history to clean up.

### Steps

1. From the account dropdown (top right), click **Add Pinterest account**.
2. Choose **Business**.
3. Email: it must differ from the personal account's. Use a Gmail dot variant —
   `abdullah.khan646khan@gmail.com` delivers to the same inbox and Pinterest
   treats it as a distinct address. Use a password you have written down.
4. Business name: `Tidy Nook`
5. Website: **leave blank.** You don't own a Tidy Nook domain, and pointing this
   at novinews.pk would be a mismatch. A claimed domain only adds analytics; it
   is not needed to publish.
6. Country: whichever you actually operate from. Language: English.
7. "What does your brand do?" → **Home decor** or nearest. This only seeds your
   recommendations; it is not the niche decision.
8. Skip the ads offer and the "build your first campaign" prompts.

Both accounts now live under the same dropdown and you can switch between them
without logging out.

**Do not delete the personal account.** Nothing needs it removed, and Buffer is
still holding a connection to it until step 5.

---

## 3. Fill in the profile

**Settings** (gear icon, bottom of the left rail) → **Edit profile**.

| Field | Value |
|---|---|
| **Photo** | Upload `assets/brand/avatar_terracotta.jpg` |
| **Business name** | `Tidy Nook \| Home & Kitchen Organization` |
| **Username** | `tidynookhome` |
| **About / Bio** | see below |
| **Website** | leave blank |

Bio — paste exactly:

```
Clever storage for small kitchens, bathrooms and tiny apartments. I find the organizers, racks and containers that actually fit - and actually work. New finds daily. Some links are affiliate links (#ad).
```

Notes:
- The keywords are in the **name** field because Pinterest indexes that field
  in search. The bio is not indexed the same way, so it is written for humans.
- If `tidynookhome` is taken, try `tidynookideas`, then `thetidynook`.
- The `#ad` in the bio is deliberate. Every pin carries it too, but the FTC
  expects a profile-level disclosure as well.
- The photo is a cream alcove with two stocked shelves on terracotta, drawn in
  the same palette as the pin template so the profile and the pins match.
  Terracotta ground because Pinterest's interface is white and a pale avatar
  vanishes in the feed. `avatar_cream.jpg` is the inverse if you prefer it.

---

## 4. Create the six boards

Click **Create a board** (the red button already on your screen), once per row.
Name it, then open the board → **pencil icon** → paste the description → set
the cover image.

**Leave every board public.** A secret board publishes nothing.

### 1. Small Kitchen Organization
> Storage ideas for kitchens with no counter space. Drawer dividers, cabinet risers, over-the-sink racks and corner organizers that make a small kitchen work harder. Affiliate links marked #ad.

Cover: `board_1_small-kitchen-organization.jpg`

### 2. Kitchen Gadgets Worth Buying
> Kitchen tools that earn their drawer space - herb scissors, jar openers, measuring sets and prep gadgets. Skip the clutter, keep the useful ones. Affiliate links marked #ad.

Cover: `board_2_kitchen-gadgets-worth-buying.jpg`

### 3. Pantry and Fridge Storage
> Airtight jars, stackable bins, egg holders and fridge organizers that stop food going to waste and make shelves easy to read at a glance. Affiliate links marked #ad.

Cover: `board_3_pantry-and-fridge-storage.jpg`

### 4. Under Sink and Cabinet Storage
> The most wasted space in the house, sorted. Pull-out drawers, sliding trays, tension rods and stacking shelves for under-sink cabinets. Affiliate links marked #ad.

Cover: `board_4_under-sink-and-cabinet-storage.jpg`

### 5. Bathroom Storage Ideas
> Shower caddies, over-toilet shelving, drawer trays and counter organizers for bathrooms with nowhere to put anything. Affiliate links marked #ad.

Cover: `board_5_bathroom-storage-ideas.jpg`

### 6. Tiny Apartment Solutions
> Space-saving ideas for renters and small flats - foldable racks, wall hooks, over-door storage and furniture that does two jobs. Affiliate links marked #ad.

Cover: `board_6_tiny-apartment-solutions.jpg`

---

## 5. Point Buffer at the new account — last, not first

Do this **after** everything above.

1. buffer.com → Channels → Pinterest (**Abdullah Khan**) → **Disconnect**.
   This is the personal profile. Leaving it connected is how affiliate pins end
   up published under your own name.
2. **Connect** Pinterest again and pick **Tidy Nook**.
3. Send me the channel name you see. I will read its id and set:

   ```
   PIN_CHANNEL_ID=<the Tidy Nook channel id>
   ```

Buffer caches the board list at connection time. This is the exact reason the
live pin test failed — Buffer saw zero boards and Pinterest rejected the post.

Then tell me, and I will re-run the live pin.

## 6. Warm-up

A brand-new account posting eight affiliate links a day is the textbook spam
signature. For the first ten days:

```
PIN_MAX_PER_DAY=4
```

Then raise it to 8. Pinterest's guidance tops out near 15 and the config clamps
there.

---

## Known gap

`PIN_BOARD_ID` sends every pin to one board. With six boards, five would sit
empty and one would become a dumping ground. Routing pins to the right board by
product type is the next code task, after the AliExpress keys land.
