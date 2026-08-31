"""
Tests for the integration layer: Buffer/Facebook, the auto-blogging engine,
cross-platform fan-out, state persistence and the database layer.

Standard library only. Nothing here touches a real network service.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.brain import BotBrain
from modules.buffer_broadcaster import BufferBroadcaster
from modules.article_engine import ArticleAgent
from modules.fanout import Fanout
from database.supabase_db import SupabaseDB, REQUIRED_TABLES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_brain(**kw):
    cfg = MagicMock()
    cfg.max_daily_posts = kw.get("max_posts", 6)
    cfg.max_daily_telegram_replies = kw.get("max_replies", 8)
    cfg.max_daily_reddit_posts = kw.get("max_reddit", 2)
    cfg.max_daily_x_posts = kw.get("max_x", 5)
    cfg.sleep_start_hour = kw.get("sleep_start", 23)
    cfg.sleep_end_hour = kw.get("sleep_end", 7)
    return BotBrain(db=None, weekly_goal=100, config=cfg)


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ═══════════════════════════════════════════════════════════════
#  BUFFER / FACEBOOK
# ═══════════════════════════════════════════════════════════════
class TestBufferBroadcaster(unittest.TestCase):

    def _bb(self, **kw):
        return BufferBroadcaster(
            access_token=kw.get("token", "tok"),
            organization_id="org1",
            enabled_services=kw.get("services", ["facebook"]),
            db=kw.get("db"))

    def test_no_token_is_not_ready(self):
        self.assertFalse(self._bb(token="").is_ready)

    def test_target_channels_filters_disconnected_and_other_services(self):
        bb = self._bb()
        bb.channels = [
            {"id": "1", "service": "facebook", "name": "Page", "isDisconnected": False},
            {"id": "2", "service": "facebook", "name": "Dead", "isDisconnected": True},
            {"id": "3", "service": "twitter", "name": "X", "isDisconnected": False},
        ]
        self.assertEqual([c["id"] for c in bb.target_channels], ["1"])

    def test_all_three_services_are_enabled_by_default(self):
        # Facebook, X and Threads all run through Buffer.
        bb = BufferBroadcaster(access_token="tok")
        self.assertEqual(sorted(bb.enabled_services),
                         ["facebook", "threads", "twitter"])

    def test_x_and_twitter_are_the_same_channel(self):
        """Buffer's API says 'twitter'; their UI says 'X'. Accept either."""
        bb = self._bb(services=["twitter"])
        bb.channels = [
            {"id": "1", "service": "twitter", "name": "X acct", "isDisconnected": False},
            {"id": "2", "service": "x", "name": "X acct 2", "isDisconnected": False},
        ]
        self.assertEqual([c["id"] for c in bb.channels_for("x")], ["1", "2"])
        self.assertEqual([c["id"] for c in bb.channels_for("twitter")], ["1", "2"])

    def test_channels_for_separates_the_two_services(self):
        bb = self._bb(services=["facebook", "twitter"])
        bb.channels = [
            {"id": "f", "service": "facebook", "name": "Page", "isDisconnected": False},
            {"id": "t", "service": "twitter", "name": "X", "isDisconnected": False},
        ]
        self.assertEqual([c["id"] for c in bb.channels_for("facebook")], ["f"])
        self.assertEqual([c["id"] for c in bb.channels_for("twitter")], ["t"])

    def test_empty_text_is_never_queued(self):
        bb = self._bb()
        bb._gql = AsyncMock(return_value={})
        ok = asyncio.run(bb.send({"id": "c1", "service": "facebook"}, "   "))
        self.assertFalse(ok)
        bb._gql.assert_not_awaited()

    def test_facebook_post_includes_required_type_metadata(self):
        """Buffer rejects Facebook posts that omit metadata.facebook.type."""
        bb = self._bb()
        captured = {}

        async def fake_gql(query, variables=None, timeout=30):
            captured.update(variables or {})
            return {"createPost": {"__typename": "PostActionSuccess",
                                   "post": {"id": "p1", "status": "queued"}}}

        bb._gql = fake_gql
        ok = asyncio.run(bb.send(
            {"id": "c1", "service": "facebook", "name": "Page"}, "hello"))
        self.assertTrue(ok)
        self.assertEqual(captured["i"]["metadata"]["facebook"]["type"], "post")

    def test_x_post_carries_no_type_metadata(self):
        """Only Facebook/Instagram want a type. Sending one to X is an error."""
        bb = self._bb(services=["twitter"])
        captured = {}

        async def fake_gql(query, variables=None, timeout=30):
            captured.update(variables or {})
            return {"createPost": {"__typename": "PostActionSuccess",
                                   "post": {"id": "p1", "status": "queued"}}}

        bb._gql = fake_gql
        ok = asyncio.run(bb.send(
            {"id": "c1", "service": "twitter", "name": "X"}, "hello"))
        self.assertTrue(ok)
        self.assertNotIn("metadata", captured["i"])

    def test_image_is_attached_as_a_public_url(self):
        """Buffer fetches the picture itself, so a local path is no use."""
        bb = self._bb()
        captured = {}

        async def fake_gql(query, variables=None, timeout=30):
            captured.update(variables or {})
            return {"createPost": {"__typename": "PostActionSuccess",
                                   "post": {"id": "p1", "status": "queued"}}}

        bb._gql = fake_gql
        asyncio.run(bb.send({"id": "c1", "service": "facebook"}, "hi",
                            image_url="https://cdn.example/pic.jpg"))
        self.assertEqual(captured["i"]["assets"][0]["image"]["url"],
                         "https://cdn.example/pic.jpg")

        captured.clear()
        asyncio.run(bb.send({"id": "c1", "service": "facebook"}, "hi",
                            image_url="C:\\local\\pic.jpg"))
        self.assertEqual(captured["i"]["assets"], [])

    def test_error_variant_treated_as_failure(self):
        """Buffer reports failures as HTTP 200 union variants, not error codes."""
        bb = self._bb()

        async def fake_gql(query, variables=None, timeout=30):
            return {"createPost": {"__typename": "InvalidInputError",
                                   "message": "Facebook posts require a type"}}

        bb._gql = fake_gql
        ok = asyncio.run(bb.send(
            {"id": "c1", "service": "facebook", "name": "Page"}, "hi"))
        self.assertFalse(ok)
        self.assertIn("require a type", bb.last_error)

    def test_uses_graphql_endpoint_not_legacy_rest(self):
        # The legacy REST host rejects modern tokens and retires 2027-02-01.
        # Assert on the actual endpoint constant, not on prose in the module.
        from modules.buffer_broadcaster import BUFFER_GRAPHQL_URL
        self.assertEqual(BUFFER_GRAPHQL_URL, "https://api.buffer.com/graphql")


# ═══════════════════════════════════════════════════════════════
#  ARTICLE AGENT / SEO
# ═══════════════════════════════════════════════════════════════
class TestArticleAgent(unittest.TestCase):

    def _agent(self, db=None):
        return ArticleAgent(ai_engine=MagicMock(), db=db, site_name="Novi News")

    def test_slug_strips_stopwords(self):
        slug = self._agent()._slugify("The Rise of Bitcoin: A New Era!")
        self.assertNotIn("the", slug.split("-"))
        self.assertRegex(slug, r"^[a-z0-9-]+$")

    def test_slug_never_empty(self):
        self.assertTrue(self._agent()._slugify("!!! ???"))

    def test_slug_length_bounded(self):
        self.assertLessEqual(len(self._agent()._slugify("word " * 60)), 71)

    def test_parse_clean_json(self):
        self.assertEqual(ArticleAgent._parse_json('{"a": 1}'), {"a": 1})

    def test_parse_fenced_json(self):
        self.assertEqual(ArticleAgent._parse_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_parse_reasoning_preamble(self):
        """Reasoning models emit their working out before the real answer."""
        raw = 'Let me consider {"draft": 1} first.\nFinal answer:\n{"meta_title": "Real"}'
        self.assertEqual(ArticleAgent._parse_json(raw), {"meta_title": "Real"})

    def test_parse_truncated_array(self):
        got = ArticleAgent._parse_json('{"t": "x", "keywords": ["a", "b", "cr')
        self.assertEqual(got["keywords"][:2], ["a", "b"])

    def test_parse_truncated_object(self):
        self.assertEqual(ArticleAgent._parse_json('{"a": "1", "b": "2",'),
                         {"a": "1", "b": "2"})

    def test_parse_garbage_returns_none(self):
        self.assertIsNone(ArticleAgent._parse_json("no json here at all"))

    def test_parse_empty_returns_none(self):
        self.assertIsNone(ArticleAgent._parse_json(""))
        self.assertIsNone(ArticleAgent._parse_json(None))

    def test_clean_html_strips_fences_and_wrappers(self):
        out = ArticleAgent._clean_html("```html\n<html><body><p>Hi</p></body></html>\n```")
        self.assertNotIn("<html>", out)
        self.assertIn("<p>Hi</p>", out)

    def test_short_article_rejected(self):
        agent = self._agent()
        agent._write_body = AsyncMock(return_value="<p>Too short.</p>")
        self.assertIsNone(asyncio.run(agent.generate_and_publish_article({"title": "T"})))

    def test_empty_body_rejected(self):
        agent = self._agent()
        agent._write_body = AsyncMock(return_value="")
        self.assertIsNone(asyncio.run(agent.generate_and_publish_article({"title": "T"})))

    def test_seo_falls_back_when_model_unusable(self):
        agent = self._agent()
        agent.ai.generate = AsyncMock(return_value="I cannot comply.")
        seo = asyncio.run(agent._write_seo("Bitcoin rally", "summary",
                                           "<p>body text here</p>", "crypto"))
        self.assertTrue(seo["meta_title"])
        self.assertTrue(seo["keywords"])

    def test_seo_prompt_has_no_char_count_constraints(self):
        """Char counts make reasoning models count characters out loud."""
        src = read("modules", "article_engine.py")
        self.assertNotIn("<=60 chars", src)
        self.assertNotIn("<=155 chars", src)

    def test_full_article_pipeline_produces_seo_fields(self):
        agent = self._agent()
        agent._hero_image = AsyncMock(return_value="https://cdn.example/hero.jpg")
        # Must clear MIN_ACCEPTABLE_WORDS (250)
        body = "<h2>H</h2><p>" + ("Real analysis sentence here now. " * 60) + "</p>"
        agent._write_body = AsyncMock(return_value=body)
        agent.ai.generate = AsyncMock(return_value=(
            '{"meta_title":"Bitcoin Rally Extends As Institutional Buyers Return",'
            '"meta_description":"Bitcoin extended its rally for a fourth session '
            'as institutional buyers returned to spot markets, lifting volume to '
            'a quarterly high.",'
            '"keywords":["bitcoin rally","institutional buyers","spot bitcoin"],'
            '"summary":"S","slug_hint":"bitcoin rally today"}'))

        # A real headline: the quality gate rejects a two-word one, and this
        # test is about the metadata, not about the gate.
        rec = asyncio.run(agent.generate_and_publish_article(
            {"title": "Bitcoin rally extends into a fourth session",
             "summary": "s", "category": "crypto"}))

        self.assertEqual(rec["meta_title"],
                         "Bitcoin Rally Extends As Institutional Buyers Return")
        self.assertIn("bitcoin rally", rec["seo_keywords"])
        self.assertEqual(rec["slug"], "bitcoin-rally-today")
        self.assertGreater(rec["word_count"], 250)
        self.assertGreaterEqual(rec["reading_minutes"], 1)
        self.assertEqual(rec["category"], "Crypto", "must be filed under a real section")

    def test_placeholder_seo_never_reaches_the_record(self):
        """
        The quality gate must reject metadata too thin to rank — a two-letter
        title and a one-line description are what a weak model actually
        returns, and they shipped to production once.
        """
        agent = self._agent()
        agent._hero_image = AsyncMock(return_value="https://cdn.example/hero.jpg")
        body = "<h2>H</h2><p>" + ("Bitcoin extended its rally again today. " * 60) + "</p>"
        agent._write_body = AsyncMock(return_value=body)
        agent.ai.generate = AsyncMock(return_value=(
            '{"meta_title":"MT","meta_description":"MD",'
            '"keywords":["news","market"],"summary":"S","slug_hint":"bitcoin rally today"}'))

        rec = asyncio.run(agent.generate_and_publish_article(
            {"title": "Bitcoin rally extends into a fourth session",
             "summary": "", "category": "crypto"}))

        self.assertNotEqual(rec["meta_title"], "MT")
        self.assertGreaterEqual(len(rec["meta_description"]), 90)
        self.assertNotIn("news", rec["seo_keywords"])
        self.assertNotIn("market", rec["seo_keywords"])


# ═══════════════════════════════════════════════════════════════
#  FANOUT
# ═══════════════════════════════════════════════════════════════
class TestFanout(unittest.TestCase):

    def test_one_platform_failing_does_not_stop_others(self):
        reddit = MagicMock(); reddit.post = AsyncMock(side_effect=Exception("reddit down"))
        twitter = MagicMock(); twitter.post = AsyncMock(return_value=True)

        fo = Fanout(brain=make_brain(), reddit=reddit, twitter=twitter)
        results = asyncio.run(fo.distribute({"telegram_text": "hi"}))

        self.assertFalse(results["reddit"])
        self.assertTrue(results["twitter"])

    def test_telegram_fanout_never_touches_facebook_or_x(self):
        """
        Facebook and X are announced when the ARTICLE publishes, not when
        Telegram posts. They used to hang off this path, and after the
        website was decoupled the two schedules carried different stories --
        so a Facebook post fired from here had no article to link to.
        """
        syn = MagicMock(); syn.syndicate = AsyncMock(return_value={})
        fo = Fanout(brain=make_brain(), syndicator=syn)
        results = asyncio.run(fo.distribute({"telegram_text": "hi"}))

        self.assertNotIn("facebook", results)
        syn.syndicate.assert_not_awaited()

    def test_reddit_daily_limit_respected(self):
        brain = make_brain(max_reddit=0)
        reddit = MagicMock(); reddit.post = AsyncMock(return_value=True)
        fo = Fanout(brain=brain, reddit=reddit)
        self.assertFalse(asyncio.run(fo.distribute({"telegram_text": "hi"}))["reddit"])
        reddit.post.assert_not_awaited()

    def test_scheduled_article_is_announced_the_moment_it_publishes(self):
        brain = make_brain()
        brain.website_module_active = True
        agent = MagicMock()
        agent.generate_and_publish_article = AsyncMock(
            return_value={"slug": "my-slug", "title": "T"})
        scraper = MagicMock()
        scraper.fetch_latest_news = AsyncMock(
            return_value=[{"title": "T", "link": "https://src/1"}])
        syn = MagicMock(); syn.syndicate = AsyncMock(return_value={"facebook": True})

        fo = Fanout(brain=brain, article_agent=agent, scraper=scraper,
                    syndicator=syn, pick_category=lambda: "crypto")
        article = asyncio.run(fo.publish_scheduled_article())

        self.assertEqual(article["slug"], "my-slug")
        syn.syndicate.assert_awaited_once()
        self.assertEqual(syn.syndicate.await_args.args[0]["slug"], "my-slug")

    def test_a_failing_social_network_cannot_unpublish_an_article(self):
        brain = make_brain()
        brain.website_module_active = True
        agent = MagicMock()
        agent.generate_and_publish_article = AsyncMock(return_value={"slug": "s"})
        scraper = MagicMock()
        scraper.fetch_latest_news = AsyncMock(
            return_value=[{"title": "T", "link": "https://src/1"}])
        syn = MagicMock(); syn.syndicate = AsyncMock(side_effect=Exception("buffer down"))

        fo = Fanout(brain=brain, article_agent=agent, scraper=scraper,
                    syndicator=syn, pick_category=lambda: "crypto")
        self.assertEqual(asyncio.run(fo.publish_scheduled_article())["slug"], "s")

    def test_no_platforms_wired_is_safe(self):
        self.assertFalse(any(asyncio.run(Fanout().distribute({"telegram_text": "hi"})).values()))


# ═══════════════════════════════════════════════════════════════
#  BRAIN STATE PERSISTENCE
# ═══════════════════════════════════════════════════════════════
class TestBrainPersistence(unittest.TestCase):

    def test_snapshot_contains_key_settings(self):
        snap = make_brain().snapshot()
        for k in ("news_module_active", "master_kill", "max_posts_today",
                  "sleep_start", "sleep_end"):
            self.assertIn(k, snap)

    def test_restore_applies_saved_values(self):
        brain = make_brain()
        brain.db = MagicMock()
        brain.db.load_state = AsyncMock(return_value={
            "news_module_active": True, "master_kill": False,
            "max_posts_today": 15, "sleep_start": 1, "sleep_end": 5,
        })
        asyncio.run(brain.restore_state())
        self.assertTrue(brain.news_module_active)
        self.assertEqual(brain.max_posts_today, 15)
        self.assertEqual(brain.SCHEDULE["sleep_start"], 1)

    def test_restore_with_no_saved_state_keeps_defaults(self):
        brain = make_brain(max_posts=6)
        brain.db = MagicMock()
        brain.db.load_state = AsyncMock(return_value=None)
        asyncio.run(brain.restore_state())
        self.assertEqual(brain.max_posts_today, 6)

    def test_restore_survives_db_error(self):
        brain = make_brain()
        brain.db = MagicMock()
        brain.db.load_state = AsyncMock(side_effect=Exception("db down"))
        asyncio.run(brain.restore_state())  # must not raise

    def test_save_state_survives_db_error(self):
        brain = make_brain()
        brain.db = MagicMock()
        brain.db.save_state = AsyncMock(side_effect=Exception("db down"))
        asyncio.run(brain.save_state())  # must not raise


# ═══════════════════════════════════════════════════════════════
#  DATABASE LAYER
# ═══════════════════════════════════════════════════════════════
class TestDatabase(unittest.TestCase):

    def test_required_tables_cover_all_code_usage(self):
        for t in ("posts", "alerts", "metrics", "error_logs",
                  "articles", "scraped_users", "bot_state", "social_posts"):
            self.assertIn(t, REQUIRED_TABLES)

    def test_offline_writes_are_safe(self):
        db = SupabaseDB("", "")
        self.assertIsNone(asyncio.run(db.log_post("telegram", "hi")))
        asyncio.run(db.log_metric("m", 1))
        asyncio.run(db.log_alert("INFO", "mod", "msg"))
        self.assertEqual(asyncio.run(db.get_invited_user_ids()), set())
        self.assertEqual(asyncio.run(db.get_articles()), [])

    def test_schema_defines_every_required_table(self):
        sql = read("database", "schema.sql").lower()
        for t in REQUIRED_TABLES:
            self.assertIn(f"create table if not exists {t}", sql,
                          f"{t} missing from schema.sql")

    def test_schema_grants_anon_access_to_articles(self):
        """The bot authenticates with the anon key; the old policy blocked it."""
        sql = read("database", "schema.sql")
        self.assertIn('CREATE POLICY "novi_all" ON articles', sql)
        # No CREATE POLICY may gate on a role the bot does not have.
        # (A DROP POLICY naming the old one is fine and expected.)
        creates = [ln for ln in sql.splitlines()
                   if ln.strip().upper().startswith("CREATE POLICY")]
        for line in creates:
            self.assertNotIn("auth.role()", line,
                             f"policy still gated on a role: {line.strip()}")

    def test_no_blocking_supabase_calls_in_db_layer(self):
        """Every Supabase round-trip must go through a worker thread."""
        src = read("database", "supabase_db.py")
        self.assertIn("asyncio.to_thread", src)


# ═══════════════════════════════════════════════════════════════
#  WEBSITE MODULE TOGGLE  +  DEDICATED ARTICLE AI
# ═══════════════════════════════════════════════════════════════
from core.ai_engine import AIEngine, DEFAULT_MODEL


class TestWebsiteToggle(unittest.TestCase):

    def test_website_is_off_by_default(self):
        self.assertFalse(make_brain().website_module_active)

    def test_article_agent_does_not_run_while_off(self):
        brain = make_brain()
        agent = MagicMock()
        agent.generate_and_publish_article = AsyncMock(return_value={"slug": "s"})
        fo = Fanout(brain=brain, article_agent=agent)

        results = asyncio.run(fo.distribute({"telegram_text": "x"}, story={"title": "T"}))

        agent.generate_and_publish_article.assert_not_awaited()
        self.assertFalse(results["website"])

    def test_article_agent_runs_when_on(self):
        brain = make_brain()
        brain.website_module_active = True
        agent = MagicMock()
        agent.generate_and_publish_article = AsyncMock(return_value={"slug": "s"})
        scraper = MagicMock()
        scraper.fetch_latest_news = AsyncMock(
            return_value=[{"title": "T", "link": "https://src/1"}])
        fo = Fanout(brain=brain, article_agent=agent, scraper=scraper,
                    pick_category=lambda: "crypto")

        article = asyncio.run(fo.publish_scheduled_article())

        agent.generate_and_publish_article.assert_awaited_once()
        self.assertEqual(article["slug"], "s")

    def test_toggle_is_persisted(self):
        brain = make_brain()
        brain.website_module_active = True
        self.assertTrue(brain.snapshot()["website_module_active"])

    def test_toggle_is_restored(self):
        brain = make_brain()
        brain.db = MagicMock()
        brain.db.load_state = AsyncMock(return_value={"website_module_active": True})
        asyncio.run(brain.restore_state())
        self.assertTrue(brain.website_module_active)

    def test_reddit_still_posts_while_website_off(self):
        """Turning the website off must not stop the Telegram-side fan-out."""
        brain = make_brain()
        agent = MagicMock()
        agent.generate_and_publish_article = AsyncMock(return_value={"slug": "s"})
        reddit = MagicMock(); reddit.post = AsyncMock(return_value=True)

        fo = Fanout(brain=brain, article_agent=agent, reddit=reddit)
        results = asyncio.run(fo.distribute({"telegram_text": "x"}, story={"title": "T"}))

        self.assertFalse(results["website"])
        self.assertTrue(results["reddit"])

    def test_website_off_means_nothing_is_announced(self):
        """No article, nothing to announce. The syndicator is never reached."""
        brain = make_brain()          # website_module_active is False
        agent = MagicMock()
        agent.generate_and_publish_article = AsyncMock(return_value={"slug": "s"})
        syn = MagicMock(); syn.syndicate = AsyncMock(return_value={})

        fo = Fanout(brain=brain, article_agent=agent, scraper=MagicMock(),
                    syndicator=syn)
        self.assertIsNone(asyncio.run(fo.publish_scheduled_article()))
        syn.syndicate.assert_not_awaited()


class TestDedicatedArticleAI(unittest.TestCase):

    def test_groq_base_url_resolved(self):
        eng = AIEngine(api_keys=["k"], provider="groq", label="ArticleAI")
        self.assertEqual(eng.base_url, "https://api.groq.com/openai/v1")

    def test_openrouter_is_default_provider(self):
        self.assertEqual(AIEngine(api_keys=["k"]).base_url,
                         "https://openrouter.ai/api/v1")

    def test_explicit_base_url_wins(self):
        eng = AIEngine(api_keys=["k"], provider="groq",
                       base_url="https://custom.example/v1")
        self.assertEqual(eng.base_url, "https://custom.example/v1")

    def test_dedicated_engine_uses_its_own_model(self):
        eng = AIEngine(api_keys=["k"], provider="groq",
                       default_model="llama-3.3-70b-versatile")
        self.assertEqual(eng.default_model, "llama-3.3-70b-versatile")

    def test_shared_engine_keeps_task_routing(self):
        self.assertEqual(AIEngine(api_keys=["k"]).default_model, DEFAULT_MODEL)

    def test_empty_keys_rejected(self):
        with self.assertRaises(ValueError):
            AIEngine(api_keys=[], label="ArticleAI")


if __name__ == "__main__":
    unittest.main(verbosity=2)
