-- ═══════════════════════════════════════════════════════════════
--  Pinterest agent tables
--
--  Run this in the PIN AGENT's own Supabase project — the one named in
--  PIN_SUPABASE_URL — not in Novi's. The free tier gives 1 GB of file
--  storage per project, and article heroes at eight a day beside pin images
--  at four a day fill a shared bucket inside a year.
--
--  Run once in Supabase > SQL Editor. Safe to re-run.
--
--  IF THIS SCRIPT HAS FAILED FOR YOU, IT WAS PART 2, AND PART 1 STILL RAN.
--
--  Two different projects refuse it in two different ways:
--
--      ERROR: 42P01: relation "storage.buckets" does not exist
--          The storage schema is not reachable from this project's SQL
--          editor. Nothing is wrong with the project — storage is simply
--          managed from the dashboard here.
--
--      ERROR: 42501: must be owner of table objects
--          storage.objects belongs to supabase_storage_admin and the editor
--          runs as postgres.
--
--  Either way the answer is the same and takes about twenty seconds:
--  MAKE THE BUCKET IN THE DASHBOARD. See PART 2.
--
--  Part 2 is wrapped so that neither error can abort the script and take
--  Part 1 down with it.
-- ═══════════════════════════════════════════════════════════════


-- ═══════════════════════════════════════════════════════════════
--  PART 1 — the table.  This always works.
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS pin_posts (
    id           BIGSERIAL PRIMARY KEY,
    product_id   TEXT NOT NULL,
    title        TEXT NOT NULL,
    description  TEXT,
    link         TEXT NOT NULL,
    image_url    TEXT,
    -- Fingerprint of the finished pin image. AliExpress relists the same
    -- product under new ids constantly, so identity is checked on the picture
    -- and the destination link as well as the product id.
    image_hash   TEXT,
    angle        TEXT,
    category     TEXT,
    score        REAL DEFAULT 0,
    -- queued | awaiting_review | published | failed | rejected
    status       TEXT NOT NULL DEFAULT 'queued',
    external_id  TEXT,
    -- Filled in later by the analytics pass; feeds the ranking.
    saves        INTEGER DEFAULT 0,
    clicks       INTEGER DEFAULT 0,
    impressions  INTEGER DEFAULT 0,
    measured_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Dedupe lookups run on every cycle, over a bounded time window.
CREATE INDEX IF NOT EXISTS pin_posts_product_idx ON pin_posts (product_id);
CREATE INDEX IF NOT EXISTS pin_posts_created_idx ON pin_posts (created_at DESC);
CREATE INDEX IF NOT EXISTS pin_posts_status_idx  ON pin_posts (status);
CREATE INDEX IF NOT EXISTS pin_posts_hash_idx    ON pin_posts (image_hash);

-- THE SOURCE PHOTOGRAPH, AND WHAT THE VISION CHECK SAW IN IT.
--
-- image_url holds the finished 1000x1500 composite; these hold the picture
-- it was built from and the model's one-line description of it. Without
-- them every verified photograph was forgotten on restart, so each call
-- against a hard allowance of twenty per key per model bought exactly one
-- pin and was then thrown away.
--
-- Additive and safe to re-run, like the rest of this file. Old rows read
-- back as NULL, which every caller treats as "no cached photograph".
ALTER TABLE pin_posts ADD COLUMN IF NOT EXISTS photo_url  TEXT;
ALTER TABLE pin_posts ADD COLUMN IF NOT EXISTS photo_note TEXT;

-- Looked up by URL when checking whether a photograph has been used, and
-- scanned for the cached pool.
CREATE INDEX IF NOT EXISTS pin_posts_photo_idx ON pin_posts (photo_url);

ALTER TABLE pin_posts ENABLE ROW LEVEL SECURITY;

-- The bot authenticates with the anon key, so the policy must admit anon.
-- A policy requiring authenticated/service_role silently rejected every
-- write on the news side and the rows simply never appeared.
DROP POLICY IF EXISTS "novi_all" ON pin_posts;
CREATE POLICY "novi_all" ON pin_posts FOR ALL USING (true) WITH CHECK (true);


-- ═══════════════════════════════════════════════════════════════
--  PART 2 — the image bucket.
--
--  Buffer downloads the pin image itself, so it must be publicly reachable
--  before a pin can publish at all.
--
--  Wrapped in exception handlers so that a project which refuses these
--  prints a NOTICE and carries on, instead of aborting and leaving you
--  wondering whether Part 1 ran. Read the notices at the bottom of the
--  editor output.
-- ═══════════════════════════════════════════════════════════════

DO $$
BEGIN
    INSERT INTO storage.buckets (id, name, public, file_size_limit,
                                 allowed_mime_types)
    VALUES ('pin-images', 'pin-images', true, 10485760,
            ARRAY['image/jpeg', 'image/png', 'image/webp'])
    ON CONFLICT (id) DO UPDATE
      SET public             = true,
          file_size_limit    = 10485760,
          allowed_mime_types = ARRAY['image/jpeg', 'image/png', 'image/webp'];
    RAISE NOTICE 'bucket pin-images: created or already public.';
EXCEPTION
    WHEN insufficient_privilege OR undefined_table OR undefined_object THEN
        RAISE NOTICE
          'COULD NOT CREATE THE BUCKET FROM SQL. Do it in the dashboard: '
          'Storage > New bucket > name it exactly pin-images > turn ON '
          '"Public bucket" > Save.';
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS "pin_images_read"   ON storage.objects;
    DROP POLICY IF EXISTS "pin_images_write"  ON storage.objects;
    DROP POLICY IF EXISTS "pin_images_update" ON storage.objects;

    -- Read is what Buffer needs: it fetches the image over plain HTTPS with
    -- no credentials at all.
    CREATE POLICY "pin_images_read" ON storage.objects
      FOR SELECT USING (bucket_id = 'pin-images');
    -- Write is what the bot needs, and it authenticates with the anon key.
    CREATE POLICY "pin_images_write" ON storage.objects
      FOR INSERT WITH CHECK (bucket_id = 'pin-images');
    -- Update covers a re-upload of the same filename.
    CREATE POLICY "pin_images_update" ON storage.objects
      FOR UPDATE USING (bucket_id = 'pin-images')
               WITH CHECK (bucket_id = 'pin-images');
    RAISE NOTICE 'storage policies for pin-images: created.';
EXCEPTION
    WHEN insufficient_privilege OR undefined_table OR undefined_object THEN
        RAISE NOTICE
          'COULD NOT CREATE STORAGE POLICIES FROM SQL (this project owns '
          'storage.objects as supabase_storage_admin). Add them in the '
          'dashboard instead: Storage > pin-images > Policies > New policy '
          '> "For full customisation". Make two, both with the target '
          'expression  bucket_id = ''pin-images''  -- one allowing SELECT, '
          'one allowing INSERT, both for the anon role. A public bucket '
          'usually grants SELECT already, so INSERT is the one that matters.';
END $$;


-- ═══════════════════════════════════════════════════════════════
--  PART 2b — IF PART 2 PRINTED A NOTICE, DO THIS INSTEAD. 20 seconds.
--
--    1. Dashboard > Storage > New bucket
--    2. Name it exactly:  pin-images
--    3. Turn ON "Public bucket"
--    4. Save
--
--  That is the whole job on most projects: a public bucket is readable by
--  anyone, which is all Buffer needs, and Supabase adds the upload policy
--  for the anon key itself.
--
--  If an upload later fails with "new row violates row-level security":
--    Storage > pin-images > Policies > New policy > For full customisation
--    Allowed operation: INSERT      Target role: anon
--    Policy definition:  bucket_id = 'pin-images'
--
--  Do NOT verify by loading a URL in the browser — an empty bucket returns
--  404 for every path whether or not it exists. Use PART 3.
-- ═══════════════════════════════════════════════════════════════


-- ═══════════════════════════════════════════════════════════════
--  PART 3 — verify.  Run this on its own afterwards.
--
--  The table check works everywhere. The bucket check is wrapped, because
--  on a project where storage is dashboard-only the query itself cannot
--  run -- which is not a failure, it just means look at the dashboard.
-- ═══════════════════════════════════════════════════════════════

SELECT 'table' AS what, table_name AS name, 'ok' AS state
FROM information_schema.tables
WHERE table_schema = 'public' AND table_name = 'pin_posts';

DO $$
DECLARE
    is_public BOOLEAN;
BEGIN
    EXECUTE 'SELECT public FROM storage.buckets WHERE id = ''pin-images'''
      INTO is_public;
    IF is_public IS NULL THEN
        RAISE NOTICE 'bucket pin-images: MISSING. Make it in the dashboard.';
    ELSIF is_public THEN
        RAISE NOTICE 'bucket pin-images: public. Ready.';
    ELSE
        RAISE NOTICE 'bucket pin-images: exists but is NOT PUBLIC. Buffer '
                     'cannot fetch the image. Turn Public on.';
    END IF;
EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'Storage is not queryable from SQL in this project. '
                     'Check it in the dashboard: Storage > pin-images should '
                     'exist and be marked Public.';
END $$;
