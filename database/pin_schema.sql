-- ═══════════════════════════════════════════════════════════════
--  Pinterest agent tables
--
--  Shares Novi's Supabase project but keeps its own tables, so the two
--  agents never contend over the same rows.
--
--  Run this once in Supabase > SQL Editor. Safe to re-run.
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

ALTER TABLE pin_posts ENABLE ROW LEVEL SECURITY;

-- The bot authenticates with the anon key, so the policy must admit anon.
-- A policy requiring authenticated/service_role silently rejected every
-- write on the news side and the rows simply never appeared.
DROP POLICY IF EXISTS "novi_all" ON pin_posts;
CREATE POLICY "novi_all" ON pin_posts FOR ALL USING (true) WITH CHECK (true);


-- ── Pin images bucket ─────────────────────────────────────────────
-- Buffer downloads the image itself, so every pin image must be publicly
-- reachable before it can be published.
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES ('pin-images', 'pin-images', true, 10485760,
        ARRAY['image/jpeg','image/png','image/webp'])
ON CONFLICT (id) DO UPDATE
  SET public             = true,
      file_size_limit    = 10485760,
      allowed_mime_types = ARRAY['image/jpeg','image/png','image/webp'];

DROP POLICY IF EXISTS "pin_images_read"   ON storage.objects;
DROP POLICY IF EXISTS "pin_images_write"  ON storage.objects;
DROP POLICY IF EXISTS "pin_images_update" ON storage.objects;

CREATE POLICY "pin_images_read" ON storage.objects
  FOR SELECT USING (bucket_id = 'pin-images');
CREATE POLICY "pin_images_write" ON storage.objects
  FOR INSERT WITH CHECK (bucket_id = 'pin-images');
CREATE POLICY "pin_images_update" ON storage.objects
  FOR UPDATE USING (bucket_id = 'pin-images')
           WITH CHECK (bucket_id = 'pin-images');


-- ── Verify ────────────────────────────────────────────────────────
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public' AND table_name = 'pin_posts';

SELECT id, public FROM storage.buckets WHERE id = 'pin-images';
