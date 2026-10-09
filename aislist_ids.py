#!/usr/bin/env python3
"""
AiSList's YouTube channel lists, by channel ID.

AiSList (https://github.com/Override92/AiSList) lists AI-slop channels by @handle. Many YouTube clients, such as the
Android TV apps, only see a video's channel ID, so this looks up each handle's channel ID with the YouTube Data API and
publishes the lists by ID. A cache keeps the work small: after the first pass, a run looks up only the handles AiSList
added and re-checks a seventh of the known channels once a day (deleted channels, handle changes).

Usage: AISLIST_YT_API_KEY=<key> python aislist_ids.py [--root DIR] [--budget UNITS]
Optional: GITHUB_TOKEN (avoids GitHub's rate limit for the source lookup).
Exit codes: 0 done (also when the day's quota ran out), 1 a problem that needs a look (bad key, broken source).
"""

import argparse
import csv
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE_REPO = "Override92/AiSList"
SOURCE_DIR = "AiSList"
# Output name -> AiSList file
LISTS = {"blocklist": "aislist_blocklist.txt", "warnlist": "aislist_warnlist.txt"}
THIS_REPO = "https://github.com/theSiegs/aislist-channel-ids"
API = "https://www.googleapis.com/youtube/v3/channels"
CHANNEL_ID = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
# YouTube gives 10,000 units a day by default; every channels.list call costs 1. Leave room for manual runs.
DAILY_QUOTA = 9000
RUN_BUDGET = 3000
# YouTube's quota day starts at midnight Pacific time
try:
    QUOTA_TZ = ZoneInfo("America/Los_Angeles")
except Exception:
    # No time zone data (Windows without tzdata): standard time is close enough, a quota error only stops a run early
    QUOTA_TZ = timezone(timedelta(hours=-8))
# A list that shrinks below this share of its last size is taken as a broken download, not published
MIN_KEEP = 0.5
RECHECK_SLICES = 7
WORKERS = 8
USER_AGENT = "aislist-channel-ids (+" + THIS_REPO + ")"


class SourceError(Exception):
    """AiSList's lists couldn't be read, or look broken"""


class ApiError(Exception):
    """The YouTube Data API refused for a reason other than quota (e.g. a bad key)"""


class QuotaSpent(Exception):
    """This run's or the day's units are used up; the rest waits for the next run"""


@dataclass
class Channel:
    # As AiSList writes it: "@name"
    handle: str
    channel_id: str = ""
    # pending (not looked up yet), ok, missing (no channel has the handle), gone (the channel was deleted)
    status: str = "pending"
    # The channel's handle on YouTube when it differs from the list's
    current_handle: str = ""

    @property
    def key(self):
        return key(self.handle)


def key(handle):
    """Handles are case-insensitive"""
    return handle.casefold()


def in_slice(text, day):
    """A stable seventh of all channels for each day of the week, so each is re-checked weekly"""
    return zlib.crc32(text.encode("utf-8")) % RECHECK_SLICES == day.toordinal() % RECHECK_SLICES


def parse_list(text):
    """
    AiSList's format: one @handle or UC channel ID per line; "!" starts a comment. Returns (handles, ids). Some handles
    are URL-encoded ("@%C3%89cho" for "@Écho"), sometimes next to the plain spelling: decoded, they count once.
    """
    handles, ids = [], []
    seen = set()

    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("!"):
            continue
        if entry.startswith("@") and len(entry) > 1:
            entry = urllib.parse.unquote(entry)
            if key(entry) not in seen:
                seen.add(key(entry))
                handles.append(entry)
        elif CHANNEL_ID.match(entry):
            ids.append(entry)

    return handles, ids


class YouTube:
    """The two channels.list calls this needs, counting units against a budget"""

    def __init__(self, api_key, budget):
        self._key = api_key
        self._budget = budget
        self._lock = threading.Lock()
        self.used = 0

    def remaining(self):
        with self._lock:
            return self._budget - self.used

    def _get(self, params):
        with self._lock:
            if self.used >= self._budget:
                raise QuotaSpent("run budget used")
            self.used += 1

        # The key goes in a header, never the URL, so it can't turn up in a logged URL or error message
        url = API + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "X-Goog-Api-Key": self._key})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            reason = _error_reason(e)
            if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                raise QuotaSpent(reason) from None
            raise ApiError("YouTube Data API: HTTP %s %s" % (e.code, reason)) from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise ApiError("YouTube Data API unreachable: %s" % e) from None

    def channel_for_handle(self, handle):
        """The channel ID that has this handle now, or None"""
        items = self._get({"part": "id", "forHandle": handle}).get("items") or []
        return items[0]["id"] if items else None

    def handles_for_ids(self, ids):
        """{channel ID: its handle ("@name", or "" if it has none)} for the channels that still exist (max 50)"""
        items = self._get({"part": "snippet", "id": ",".join(ids), "maxResults": 50}).get("items") or []
        return {item["id"]: item.get("snippet", {}).get("customUrl", "") for item in items}


def _error_reason(error):
    try:
        body = json.loads(error.read().decode("utf-8", "replace"))
        return body["error"]["errors"][0]["reason"]
    except Exception:
        return "unknown"


def _http_get(url, token=None):
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
            return response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError) as e:
        raise SourceError("Couldn't read %s: %s" % (url, e)) from None


def fetch_source(token=None):
    """AiSList's newest commit that touched the lists, and the lists' text at that commit"""
    commits = json.loads(_http_get(
        "https://api.github.com/repos/%s/commits?path=%s&per_page=1" % (SOURCE_REPO, SOURCE_DIR), token))
    if not commits:
        raise SourceError("AiSList has no commits for %s" % SOURCE_DIR)

    sha = commits[0]["sha"]
    texts = {name: _http_get("https://raw.githubusercontent.com/%s/%s/%s/%s" % (SOURCE_REPO, sha, SOURCE_DIR, file))
             for name, file in LISTS.items()}
    return sha, texts


# --- Files ---------------------------------------------------------------------------------------------------------

CACHE_FIELDS = ["handle", "channel_id", "status", "current_handle"]


def load_cache(root):
    path = root / "data" / "channels.tsv"
    if not path.exists():
        return {}

    with path.open(encoding="utf-8", newline="") as file:
        rows = csv.DictReader(file, delimiter="\t")
        channels = [Channel(**{field: row.get(field) or "" for field in CACHE_FIELDS}) for row in rows]
    return {channel.key: channel for channel in channels}


def save_cache(root, cache):
    rows = sorted(cache.values(), key=lambda channel: channel.key)
    lines = ["\t".join(CACHE_FIELDS)]
    lines += ["\t".join(getattr(channel, field) for field in CACHE_FIELDS) for channel in rows]
    write(root / "data" / "channels.tsv", "\n".join(lines) + "\n")


def load_state(root):
    path = root / "data" / "state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_state(root, state):
    write(root / "data" / "state.json", json.dumps(state, indent=2, sort_keys=True) + "\n")


def write(path, text):
    """Writes only real changes, so unchanged files stay out of the commit"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    path.write_text(text, encoding="utf-8", newline="\n")


# --- The run -------------------------------------------------------------------------------------------------------

def check_source(lists, state):
    """Refuses a list that came back empty or far smaller than last time"""
    counts = state.get("counts", {})

    for name, (handles, ids) in lists.items():
        size = len(handles) + len(ids)
        if size == 0:
            raise SourceError("AiSList's %s is empty" % name)
        if size < counts.get(name, 0) * MIN_KEEP:
            raise SourceError("AiSList's %s shrank from %d to %d entries" % (name, counts[name], size))


def update_cache(cache, lists):
    """A row for every listed handle (in the list's spelling), none for handles AiSList dropped"""
    listed = {}
    for handles, _ in lists.values():
        for handle in handles:
            listed.setdefault(key(handle), handle)

    for dropped in set(cache) - set(listed):
        del cache[dropped]

    for k, handle in listed.items():
        if k not in cache:
            cache[k] = Channel(handle)
        else:
            cache[k].handle = handle


def recheck(cache, youtube, today):
    """Today's seventh of the known channels: which were deleted, and which now go by another handle"""
    due = [channel for channel in cache.values()
           if channel.status in ("ok", "gone") and in_slice(channel.channel_id, today)]

    for start in range(0, len(due), 50):
        batch = due[start:start + 50]
        found = youtube.handles_for_ids([channel.channel_id for channel in batch])

        for channel in batch:
            if channel.channel_id not in found:
                channel.status = "gone"
                channel.current_handle = ""
                continue

            channel.status = "ok"
            handle = found[channel.channel_id]
            channel.current_handle = handle if handle and key(handle) != channel.key else ""


def resolve(cache, youtube, today):
    """New handles first, then today's weekly retry of handles no channel had"""
    due = [channel for channel in cache.values() if channel.status == "pending"]
    due += [channel for channel in cache.values() if channel.status == "missing" and in_slice(channel.key, today)]
    due = due[:max(youtube.remaining(), 0)]

    def look_up(channel):
        channel_id = youtube.channel_for_handle(channel.handle)
        channel.channel_id = channel_id or ""
        channel.status = "ok" if channel_id else "missing"
        channel.current_handle = ""

    first_error = None
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = [pool.submit(look_up, channel) for channel in due]
        for future in futures:
            try:
                future.result()
            except CancelledError:
                pass
            except (QuotaSpent, ApiError) as e:
                # Keep what's done; stop asking
                first_error = first_error or e
                pool.shutdown(wait=False, cancel_futures=True)

    if first_error:
        raise first_error


def write_lists(root, cache, lists, source_sha):
    stats = {"source_commit": source_sha, "lists": {}}
    changes = []

    for name, (handles, ids) in lists.items():
        rows = [cache[key(handle)] for handle in handles]
        found = sorted({channel.channel_id for channel in rows if channel.status == "ok"} | set(ids))
        counts = {status: sum(1 for channel in rows if channel.status == status)
                  for status in ("ok", "pending", "missing", "gone")}
        stats["lists"][name] = {"entries": len(handles) + len(ids), "channel_ids": len(found), **counts}

        header = [
            "! AiSList %s by YouTube channel ID" % name,
            "! Source: https://github.com/%s/blob/%s/%s/%s" % (SOURCE_REPO, source_sha, SOURCE_DIR, LISTS[name]),
            "! License: CC BY-NC 4.0 (https://creativecommons.org/licenses/by-nc/4.0/), by the AiSList contributors;",
            "!   converted from handles to channel IDs by " + THIS_REPO,
            "! Format: one channel ID per line; lines starting with ! are comments",
            "! Channels: %d of %d entries (%d not looked up yet, %d with no channel, %d deleted)" % (
                len(found), len(handles) + len(ids), counts["pending"], counts["missing"], counts["gone"]),
        ]
        write(root / "lists" / ("aislist_%s_ids.txt" % name), "\n".join(header + found) + "\n")

        pairs = sorted((channel.handle, channel.channel_id) for channel in rows if channel.status == "ok")
        write(root / "lists" / ("aislist_%s.csv" % name),
              "handle,channel_id\n" + "".join("%s,%s\n" % pair for pair in pairs))

        changes += [(channel.handle, channel.channel_id, channel.current_handle, name)
                    for channel in rows if channel.current_handle]

    write(root / "lists" / "stats.json", json.dumps(stats, indent=2, sort_keys=True) + "\n")
    # Channels whose YouTube handle no longer matches the list (AiSList's own bot usually catches up)
    write(root / "reports" / "handle_changes.tsv",
          "list_handle\tchannel_id\tcurrent_handle\tlist\n" + "".join("\t".join(row) + "\n" for row in sorted(changes)))
    return stats


def run(root, youtube, source, now):
    """One update. source() returns (commit sha, {list name: text}). Returns the stats written."""
    state = load_state(root)
    sha, texts = source()
    lists = {name: parse_list(texts[name]) for name in LISTS}
    check_source(lists, state)

    cache = load_cache(root)
    update_cache(cache, lists)

    today = now.astimezone(QUOTA_TZ).date()
    quota = state.get("quota", {})
    used_before = quota.get("used", 0) if quota.get("day") == today.isoformat() else 0

    error = None
    try:
        if state.get("rechecked") != today.isoformat():
            recheck(cache, youtube, today)
            state["rechecked"] = today.isoformat()
        resolve(cache, youtube, today)
    except (QuotaSpent, ApiError) as e:
        error = e
    finally:
        state["quota"] = {"day": today.isoformat(), "used": used_before + youtube.used}
        state["source_commit"] = sha
        state["counts"] = {name: len(handles) + len(ids) for name, (handles, ids) in lists.items()}
        save_cache(root, cache)
        stats = write_lists(root, cache, lists, sha)
        save_state(root, state)

    if isinstance(error, ApiError):
        raise error
    if error:
        print("Stopped early: %s. The rest waits for the next run." % error)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--budget", type=int, default=RUN_BUDGET, help="most API units this run may use")
    args = parser.parse_args(argv)

    api_key = os.environ.get("AISLIST_YT_API_KEY")
    if not api_key:
        print("Set AISLIST_YT_API_KEY to a YouTube Data API v3 key.", file=sys.stderr)
        return 1

    now = datetime.now(QUOTA_TZ)
    state = load_state(args.root)
    quota = state.get("quota", {})
    used_today = quota.get("used", 0) if quota.get("day") == now.date().isoformat() else 0
    budget = max(0, min(args.budget, DAILY_QUOTA - used_today))

    youtube = YouTube(api_key, budget)
    try:
        stats = run(args.root, youtube, lambda: fetch_source(os.environ.get("GITHUB_TOKEN")), now)
    except (SourceError, ApiError) as e:
        print("Error: %s" % e, file=sys.stderr)
        return 1

    for name, counts in stats["lists"].items():
        print("%s: %d channel IDs; %d not looked up yet, %d with no channel, %d deleted"
              % (name, counts["channel_ids"], counts["pending"], counts["missing"], counts["gone"]))
    print("Used %d API units this run (budget %d)." % (youtube.used, budget))
    return 0


if __name__ == "__main__":
    sys.exit(main())
