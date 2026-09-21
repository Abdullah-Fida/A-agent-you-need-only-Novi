"""
The library, and the one thing it exists to guarantee:

    a scheduled pin publishes with NO Gemini session at all.

That is the whole reason for making the pictures in a batch. If these pass
with image_maker=None, the cookie is off the critical path.
"""
import asyncio
import unittest
from unittest.mock import MagicMock

from pin_agent import prompt_book
from pin_agent.picture_library import PictureLibrary, key_for


class FakeStore:
    """Just enough Supabase storage to exercise the library."""

    def __init__(self, names=(), fail=False):
        self.objects = {("%s" % n) for n in names}
        self.fail = fail
        self.uploaded = []
        self.lists = 0

    def list(self, prefix, opts=None):
        self.lists += 1
        if self.fail:
            raise RuntimeError("storage is down")
        rows = [{"name": o.split("/", 1)[1]} for o in sorted(self.objects)]
        offset = (opts or {}).get("offset", 0)
        limit = (opts or {}).get("limit", 100)
        return rows[offset:offset + limit]

    def upload(self, path, file, file_options=None):
        if self.fail:
            raise RuntimeError("storage is down")
        self.uploaded.append(path)
        self.objects.add(path)

    def get_public_url(self, path):
        return "https://example.test/storage/%s" % path


class FakeClient:
    def __init__(self, store):
        self.storage = MagicMock()
        self.storage.from_ = lambda bucket: store


class TestKeys(unittest.TestCase):
    def test_the_key_is_the_slot_not_the_title(self):
        # A reworded title must not orphan its picture.
        self.assertEqual(key_for(1, 1), "library/d01s1.png")
        self.assertEqual(key_for(28, 6), "library/d28s6.png")

    def test_keys_sort_in_schedule_order(self):
        keys = [key_for(d, s) for d in (1, 2, 10, 28) for s in (1, 6)]
        self.assertEqual(keys, sorted(keys))


class TestListing(unittest.TestCase):
    def test_it_reads_what_the_bucket_actually_holds(self):
        store = FakeStore(["library/d01s1.png", "library/d01s2.png"])
        lib = PictureLibrary(FakeClient(store))
        self.assertTrue(lib.has(1, 1))
        self.assertTrue(lib.has(1, 2))
        self.assertFalse(lib.has(1, 3))

    def test_it_pages_past_the_first_hundred(self):
        # 168 pictures is more than one default page; losing the tail would
        # silently make the last days fall back to photographs.
        names = [key_for(d, s) for d in range(1, 29) for s in range(1, 7)]
        store = FakeStore(names)
        lib = PictureLibrary(FakeClient(store))
        self.assertEqual(len(lib.keys), 168)
        self.assertTrue(lib.has(28, 6))

    def test_a_storage_failure_is_an_empty_library_not_a_crash(self):
        lib = PictureLibrary(FakeClient(FakeStore(fail=True)))
        self.assertEqual(lib.keys, set())
        self.assertIn("storage is down", lib.last_error)

    def test_no_client_is_safe(self):
        lib = PictureLibrary(None)
        self.assertFalse(lib.is_ready)
        self.assertEqual(lib.url_for(1, 1), "")
        self.assertFalse(lib.has(1, 1))

    def test_relisting_is_rate_limited(self):
        store = FakeStore([])
        lib = PictureLibrary(FakeClient(store))
        lib.refresh()
        before = store.lists
        lib.refresh_if_stale(600)
        self.assertEqual(store.lists, before)      # too soon
        lib.refresh_if_stale(0)
        self.assertEqual(store.lists, before + 1)  # stale enough


class TestPut(unittest.TestCase):
    def test_upload_returns_a_url_and_updates_the_index(self):
        import os
        import tempfile
        store = FakeStore([])
        lib = PictureLibrary(FakeClient(store))
        lib.refresh()
        fh = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        fh.write(b"not really a png, but not empty")
        fh.close()
        try:
            url = lib.put(3, 4, fh.name)
            self.assertTrue(url.endswith("library/d03s4.png"))
            self.assertTrue(lib.has(3, 4))
            self.assertEqual(store.uploaded, ["library/d03s4.png"])
        finally:
            os.unlink(fh.name)

    def test_a_missing_file_is_refused(self):
        lib = PictureLibrary(FakeClient(FakeStore([])))
        self.assertEqual(lib.put(1, 1, "nowhere.png"), "")


class TestMissing(unittest.TestCase):
    def test_missing_drives_a_resumable_batch(self):
        book = prompt_book.PromptBook()
        if not book.is_ready:
            self.skipTest("schedule not available")
        store = FakeStore([key_for(1, 1), key_for(1, 2)])
        lib = PictureLibrary(FakeClient(store))
        missing = lib.missing(book)
        self.assertEqual(len(missing), 166)
        self.assertNotIn((1, 1), [(e["day"], e["slot"]) for e in missing])
        self.assertIn((1, 3), [(e["day"], e["slot"]) for e in missing])


class TestScheduleWithoutACookie(unittest.IsolatedAsyncioTestCase):
    """The guarantee. No image maker anywhere, and a pin still comes out."""

    def _agent(self, library):
        import pin_agent.pin_bot as pin_bot
        agent = pin_bot.PinAgent.__new__(pin_bot.PinAgent)
        agent.book = prompt_book.PromptBook()
        agent.library = library
        agent.image_maker = None          # NO SESSION AT ALL
        agent.recent_tips = []
        agent.writer = None
        return agent

    async def test_a_stored_picture_publishes_with_no_session(self):
        book = prompt_book.PromptBook()
        if not book.is_ready:
            self.skipTest("schedule not available")
        entry = book.for_day()[0]
        store = FakeStore([key_for(entry["day"], entry["slot"])])
        agent = self._agent(PictureLibrary(FakeClient(store)))

        tip = await agent._from_the_schedule([])
        self.assertIsNotNone(tip, "a stored picture must publish without Gemini")
        self.assertEqual(tip["title"], entry["title"])
        self.assertEqual(tip["board"], entry["board"])
        self.assertTrue(tip["image"].endswith("library/d%02ds%d.png"
                                              % (entry["day"], entry["slot"])))
        self.assertTrue(tip["generated"])
        self.assertTrue(tip["scheduled"])
        # The description falls back to the scene when there is no writer,
        # rather than the pin being dropped.
        self.assertTrue(tip["body"])

    async def test_no_picture_and_no_session_falls_back_quietly(self):
        book = prompt_book.PromptBook()
        if not book.is_ready:
            self.skipTest("schedule not available")
        agent = self._agent(PictureLibrary(FakeClient(FakeStore([]))))
        self.assertIsNone(await agent._from_the_schedule([]))

    async def test_an_already_published_title_is_skipped(self):
        book = prompt_book.PromptBook()
        if not book.is_ready:
            self.skipTest("schedule not available")
        today = book.for_day()
        names = [key_for(e["day"], e["slot"]) for e in today]
        agent = self._agent(PictureLibrary(FakeClient(FakeStore(names))))
        agent.recent_tips = [today[0]["title"]]

        tip = await agent._from_the_schedule([])
        self.assertEqual(tip["title"], today[1]["title"])


if __name__ == "__main__":
    unittest.main()
