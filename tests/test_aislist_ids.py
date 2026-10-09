import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aislist_ids as ai  # noqa: E402

UC_A = "UC" + "a" * 22
UC_B = "UC" + "b" * 22
UC_C = "UC" + "c" * 22
UC_D = "UC" + "d" * 22
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=ai.QUOTA_TZ)


class FakeYouTube(ai.YouTube):
    """Answers from dicts instead of the API, counting units the same way"""

    def __init__(self, handles, channels, budget=1000, fail=None):
        super().__init__("key", budget)
        self.handles = handles  # handle (any case) -> channel ID
        self.channels = channels  # channel ID -> current handle
        self.fail = fail
        self.calls = []

    def _get(self, params):
        with self._lock:
            if self.used >= self._budget:
                raise ai.QuotaSpent("run budget used")
            self.used += 1
        self.calls.append(params)
        if self.fail:
            raise self.fail
        if "forHandle" in params:
            channel_id = {ai.key(h): c for h, c in self.handles.items()}.get(ai.key(params["forHandle"]))
            return {"items": [{"id": channel_id}]} if channel_id else {}
        ids = params["id"].split(",")
        return {"items": [{"id": i, "snippet": {"customUrl": self.channels[i]}} for i in ids if i in self.channels]}


def source(block, warn="! nothing\n@Warned\n", sha="abc123"):
    return lambda: (sha, {"blocklist": block, "warnlist": warn})


class ParseTest(unittest.TestCase):
    def test_handles_ids_comments(self):
        text = "! header\n\n@One\n@one\n%s\n@Two  \nnot-an-entry\n@\n" % UC_A
        self.assertEqual(ai.parse_list(text), (["@One", "@Two"], [UC_A]))

    def test_unicode_handles(self):
        self.assertEqual(ai.parse_list("@ЗелёныйГектар\n")[0], ["@ЗелёныйГектар"])

    def test_url_encoded_handles_are_decoded_once(self):
        self.assertEqual(ai.parse_list("@%C3%89cho\n@Écho\n@éCHO\n")[0], ["@Écho"])


class RunTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def read(self, path):
        return (self.root / path).read_text(encoding="utf-8")

    def test_first_run_resolves_and_publishes(self):
        youtube = FakeYouTube({"@One": UC_A, "@Warned": UC_B}, {})
        stats = ai.run(self.root, youtube, source("@One\n@Missing\n%s\n" % UC_C), NOW)

        ids = [line for line in self.read("lists/aislist_blocklist_ids.txt").splitlines() if not line.startswith("!")]
        self.assertEqual(ids, sorted([UC_A, UC_C]))
        self.assertIn("CC BY-NC 4.0", self.read("lists/aislist_blocklist_ids.txt"))
        self.assertEqual(self.read("lists/aislist_blocklist.csv"), "handle,channel_id\n@One,%s\n" % UC_A)
        self.assertEqual(stats["lists"]["blocklist"]["missing"], 1)
        self.assertEqual(stats["lists"]["warnlist"]["channel_ids"], 1)
        self.assertEqual(json.loads(self.read("data/state.json"))["quota"]["used"], 3)

    def test_second_run_only_looks_up_new_handles(self):
        ai.run(self.root, FakeYouTube({"@One": UC_A}, {UC_A: "@One"}), source("@One\n"), NOW)

        youtube = FakeYouTube({"@One": UC_A, "@New": UC_D}, {UC_A: "@One"})
        ai.run(self.root, youtube, source("@One\n@New\n"), NOW)

        handles = [call["forHandle"] for call in youtube.calls if "forHandle" in call]
        self.assertEqual(handles, ["@New"])

    def test_budget_leaves_the_rest_pending(self):
        youtube = FakeYouTube({"@A": UC_A, "@B": UC_B, "@C": UC_C}, {}, budget=2)
        stats = ai.run(self.root, youtube, source("@A\n@B\n@C\n", warn="@A\n"), NOW)

        self.assertEqual(youtube.used, 2)
        self.assertEqual(stats["lists"]["blocklist"]["pending"], 1)

        # The next run picks up where this one stopped
        youtube = FakeYouTube({"@A": UC_A, "@B": UC_B, "@C": UC_C}, {})
        stats = ai.run(self.root, youtube, source("@A\n@B\n@C\n", warn="@A\n"), NOW)
        self.assertEqual(stats["lists"]["blocklist"]["pending"], 0)
        self.assertEqual(stats["lists"]["blocklist"]["channel_ids"], 3)

    def test_recheck_marks_deleted_and_renamed_channels(self):
        handles = {"@Gone": UC_A, "@Renamed": UC_B}
        ai.run(self.root, FakeYouTube(handles, {}), source("@Gone\n@Renamed\n"), NOW)

        # Every channel falls in some day's slice; run on each day of a week
        youtube = FakeYouTube(handles, {UC_B: "@NewName"})
        for day in range(1, 8):
            state = json.loads(self.read("data/state.json"))
            state.pop("rechecked", None)
            (self.root / "data/state.json").write_text(json.dumps(state), encoding="utf-8")
            stats = ai.run(self.root, youtube, source("@Gone\n@Renamed\n"), NOW.replace(day=9 + day))

        self.assertEqual(stats["lists"]["blocklist"]["gone"], 1)
        self.assertEqual(stats["lists"]["blocklist"]["channel_ids"], 1)
        self.assertIn("@Renamed\t%s\t@NewName" % UC_B, self.read("reports/handle_changes.tsv"))

    def test_recheck_once_a_day(self):
        ai.run(self.root, FakeYouTube({"@A": UC_A}, {UC_A: "@A"}), source("@A\n"), NOW)
        youtube = FakeYouTube({"@A": UC_A}, {UC_A: "@A"})
        ai.run(self.root, youtube, source("@A\n"), NOW)
        self.assertEqual(youtube.calls, [])

    def test_dropped_handles_leave_the_cache(self):
        ai.run(self.root, FakeYouTube({"@A": UC_A, "@B": UC_B}, {}), source("@A\n@B\n"), NOW)
        ai.run(self.root, FakeYouTube({}, {}), source("@A\n"), NOW)
        self.assertNotIn("@B", self.read("data/channels.tsv"))

    def test_shrunken_source_is_refused(self):
        many = "".join("@h%d\n" % i for i in range(10))
        ai.run(self.root, FakeYouTube({}, {}, budget=0), source(many), NOW)
        before = self.read("lists/aislist_blocklist_ids.txt")

        with self.assertRaises(ai.SourceError):
            ai.run(self.root, FakeYouTube({}, {}), source("@h1\n"), NOW)
        self.assertEqual(self.read("lists/aislist_blocklist_ids.txt"), before)

    def test_bad_key_keeps_progress_and_fails(self):
        youtube = FakeYouTube({}, {}, fail=ai.ApiError("HTTP 400 keyInvalid"))
        with self.assertRaises(ai.ApiError):
            ai.run(self.root, youtube, source("@A\n"), NOW)
        self.assertTrue((self.root / "lists/stats.json").exists())

    def test_quota_exceeded_is_not_an_error(self):
        youtube = FakeYouTube({}, {}, fail=ai.QuotaSpent("quotaExceeded"))
        stats = ai.run(self.root, youtube, source("@A\n"), NOW)
        self.assertEqual(stats["lists"]["blocklist"]["pending"], 1)


if __name__ == "__main__":
    unittest.main()
