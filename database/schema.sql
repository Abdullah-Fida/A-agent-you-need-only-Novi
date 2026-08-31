-- =====================================================================
--  NOVI — Complete Supabase Schema
--  Run this ENTIRE file in the Supabase SQL Editor.
--  It is idempotent: safe to run again at any time.
-- =====================================================================
--
--  IMPORTANT — why the old schema silently failed:
--
--  1. `articles` and `scraped_users` were used by the code but never
--     existed in the database, so every article insert and every scraped
--     user write failed. That is why the website had no content and the
--     stealth marketer could never track who it had already contacted.
--
--  2. The old `articles` policy required auth.role() = 'authenticated'
--     or 'service_role'. The bot connects with the ANON key, so even
--     once the table existed every insert would have been rejected by
--     RLS with no visible error.
--
--  This file fixes both.
-- =====================================================================


-- ─────────────────────────────────────────────────────────────────────
-- 1. POSTS — every post made on every platform
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS posts (
    id          UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    platform    TEXT NOT NULL,           -- telegram_channel | twitter | reddit | facebook | whale_tracker_vip
    content     TEXT NOT NULL,
    image_path  TEXT DEFAULT '',
    status      TEXT DEFAULT 'posted',   -- posted | failed | scheduled
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────
-- 2. ALERTS — dashboard notifications
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts (
    id          UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    level       TEXT NOT NULL,           -- INFO | WARNING | CRITICAL
    module      TEXT NOT NULL,
    message     TEXT NOT NULL,
    resolved    BOOLEAN DEFAULT false,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────
-- 3. METRICS — subscriber counts, daily stats, limit changes
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS metrics (
    id          UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    metric_name TEXT NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────
-- 4. ERROR LOGS — self-healing tracking
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS error_logs (
    id            UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    module        TEXT NOT NULL,
    error_type    TEXT NOT NULL,
    error_message TEXT NOT NULL,
    auto_resolved BOOLEAN DEFAULT false,
    created_at    TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────
-- 5. ARTICLES — the website / auto-blogging system  ** WAS MISSING **
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS articles (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    title           TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,
    content         TEXT NOT NULL,           -- rendered HTML body
    summary         TEXT DEFAULT '',
    main_image_url  TEXT DEFAULT '',
    category        TEXT DEFAULT 'News',
    seo_keywords    TEXT[] DEFAULT '{}',
    meta_title      TEXT DEFAULT '',         -- <title> override
    meta_description TEXT DEFAULT '',        -- <meta name="description">
    reading_minutes INT DEFAULT 3,
    word_count      INT DEFAULT 0,
    source_url      TEXT DEFAULT '',
    source_name     TEXT DEFAULT '',
    author          TEXT DEFAULT 'NOVI ArticleAgent',
    status          TEXT DEFAULT 'published', -- published | draft
    views           INT DEFAULT 0,
    published_at    TIMESTAMPTZ DEFAULT now(),
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────
-- 6. SCRAPED USERS — stealth invite dedupe  ** WAS MISSING **
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scraped_users (
    user_id      BIGINT PRIMARY KEY,
    source_group TEXT DEFAULT '',
    scraped_at   TIMESTAMPTZ DEFAULT now(),
    invited      BOOLEAN DEFAULT false,
    invited_at   TIMESTAMPTZ,
    outcome      TEXT DEFAULT ''          -- sent | privacy_blocked | already_member | failed
);

-- ─────────────────────────────────────────────────────────────────────
-- 7. BOT STATE — survives restarts and redeploys
--    Module toggles and limits used to reset on every deploy.
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bot_state (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────
-- 8. SOCIAL QUEUE — cross-platform delivery tracking (incl. Buffer)
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS social_posts (
    id           UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    platform     TEXT NOT NULL,          -- facebook | twitter | reddit | linkedin
    provider     TEXT DEFAULT 'direct',  -- direct | buffer
    content      TEXT NOT NULL,
    image_url    TEXT DEFAULT '',
    status       TEXT DEFAULT 'pending', -- pending | sent | failed
    external_id  TEXT DEFAULT '',        -- Buffer update id / tweet id
    error        TEXT DEFAULT '',
    article_slug TEXT DEFAULT '',
    created_at   TIMESTAMPTZ DEFAULT now(),
    sent_at      TIMESTAMPTZ
);


-- =====================================================================
--  INDEXES
-- =====================================================================
CREATE INDEX IF NOT EXISTS idx_posts_platform    ON posts(platform, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_unresolved ON alerts(resolved, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_name      ON metrics(metric_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_errors_module     ON error_logs(module, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_slug     ON articles(slug);
CREATE INDEX IF NOT EXISTS idx_articles_pub      ON articles(status, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_cat      ON articles(category, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_scraped_invited   ON scraped_users(invited);
CREATE INDEX IF NOT EXISTS idx_social_status     ON social_posts(status, created_at DESC);


-- =====================================================================
--  ROW LEVEL SECURITY
--
--  This bot is private and authenticates with the ANON key, so the anon
--  role must be allowed to read and write. The previous articles policy
--  required 'authenticated'/'service_role', which silently rejected
--  every insert the bot made.
-- =====================================================================
ALTER TABLE posts         ENABLE ROW LEVEL SECURITY;
ALTER TABLE alerts        ENABLE ROW LEVEL SECURITY;
ALTER TABLE metrics       ENABLE ROW LEVEL SECURITY;
ALTER TABLE error_logs    ENABLE ROW LEVEL SECURITY;
ALTER TABLE articles      ENABLE ROW LEVEL SECURITY;
ALTER TABLE scraped_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_state     ENABLE ROW LEVEL SECURITY;
ALTER TABLE social_posts  ENABLE ROW LEVEL SECURITY;

-- Drop any older conflicting policies so this file stays idempotent
DROP POLICY IF EXISTS "Allow all for anon"                    ON posts;
DROP POLICY IF EXISTS "Allow all for anon"                    ON alerts;
DROP POLICY IF EXISTS "Allow all for anon"                    ON metrics;
DROP POLICY IF EXISTS "Allow all for anon"                    ON error_logs;
DROP POLICY IF EXISTS "Allow public read access on articles"  ON articles;
DROP POLICY IF EXISTS "Allow authenticated insert on articles" ON articles;
DROP POLICY IF EXISTS "Allow authenticated update on articles" ON articles;
DROP POLICY IF EXISTS "novi_all"                              ON articles;
DROP POLICY IF EXISTS "novi_all"                              ON scraped_users;
DROP POLICY IF EXISTS "novi_all"                              ON bot_state;
DROP POLICY IF EXISTS "novi_all"                              ON social_posts;

CREATE POLICY "novi_all" ON posts         FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "novi_all" ON alerts        FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "novi_all" ON metrics       FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "novi_all" ON error_logs    FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "novi_all" ON articles      FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "novi_all" ON scraped_users FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "novi_all" ON bot_state     FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "novi_all" ON social_posts  FOR ALL USING (true) WITH CHECK (true);


-- =====================================================================
--  STORAGE — hero images for website articles
--  The bot's own disk is wiped on every Render deploy, so generated
--  article images are uploaded here and served from Supabase's CDN.
--  Public read; the bot writes with the anon key, hence the open policies.
-- =====================================================================
--  NOT every Supabase project lets the SQL editor manage storage. A newer
--  one answers with either
--      ERROR: 42P01: relation "storage.buckets" does not exist
--      ERROR: 42501: must be owner of table objects
--  and an uncaught error there aborts the whole script, taking the TABLES
--  above down with it. Wrapped so it cannot, and so the notice tells you to
--  use the dashboard instead:
--      Storage > New bucket > name it article-images > Public bucket ON.
DO $$
BEGIN
    INSERT INTO storage.buckets (id, name, public, file_size_limit,
                                 allowed_mime_types)
    VALUES ('article-images', 'article-images', true, 10485760,
            ARRAY['image/jpeg','image/png','image/webp'])
    ON CONFLICT (id) DO UPDATE
      SET public             = true,
          file_size_limit    = 10485760,
          allowed_mime_types = ARRAY['image/jpeg','image/png','image/webp'];
    RAISE NOTICE 'bucket article-images: created or already public.';
EXCEPTION
    WHEN insufficient_privilege OR undefined_table OR undefined_object THEN
        RAISE NOTICE
          'COULD NOT CREATE THE BUCKET FROM SQL. Dashboard > Storage > '
          'New bucket > name it exactly article-images > Public bucket ON.';
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS "novi_images_read"   ON storage.objects;
    DROP POLICY IF EXISTS "novi_images_write"  ON storage.objects;
    DROP POLICY IF EXISTS "novi_images_update" ON storage.objects;

    CREATE POLICY "novi_images_read" ON storage.objects
      FOR SELECT USING (bucket_id = 'article-images');

    CREATE POLICY "novi_images_write" ON storage.objects
      FOR INSERT WITH CHECK (bucket_id = 'article-images');

    -- Needed because uploads use upsert
    CREATE POLICY "novi_images_update" ON storage.objects
      FOR UPDATE USING (bucket_id = 'article-images')
               WITH CHECK (bucket_id = 'article-images');
    RAISE NOTICE 'storage policies for article-images: created.';
EXCEPTION
    WHEN insufficient_privilege OR undefined_table OR undefined_object THEN
        RAISE NOTICE
          'COULD NOT CREATE STORAGE POLICIES FROM SQL. A PUBLIC bucket is '
          'usually enough; if an upload fails with "violates row-level '
          'security", add an INSERT policy for anon in the dashboard with '
          'the definition  bucket_id = ''article-images''.';
END $$;


-- =====================================================================
--  VERIFY — this should return 8 rows
-- =====================================================================
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('posts','alerts','metrics','error_logs',
                     'articles','scraped_users','bot_state','social_posts')
ORDER BY table_name;

-- …and this should return one row: article-images, public = true
SELECT id, public FROM storage.buckets WHERE id = 'article-images';
