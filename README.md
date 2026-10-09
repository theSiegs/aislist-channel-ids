# AiSList by channel ID

[AiSList](https://github.com/Override92/AiSList) is a community-maintained list of YouTube channels that mainly
publish AI-generated content ("AI slop"). It lists channels by **@handle**. Many YouTube clients, such as the Android
TV apps (SmartTube and its forks), only see a video's **channel ID** (`UC…`). They can't match the lists without an
extra lookup per channel.

This repo publishes the same lists by channel ID, updated every 6 hours.

## The lists

| File | What |
|---|---|
| [`lists/aislist_blocklist_ids.txt`](lists/aislist_blocklist_ids.txt) | Blocklist (high confidence): one channel ID per line |
| [`lists/aislist_warnlist_ids.txt`](lists/aislist_warnlist_ids.txt) | Warnlist (medium confidence): one channel ID per line |
| `lists/aislist_blocklist.csv`, `lists/aislist_warnlist.csv` | `handle,channel_id` pairs |
| [`lists/stats.json`](lists/stats.json) | Counts, and the AiSList commit the lists come from |
| `reports/handle_changes.tsv` | Listed handles whose channel now uses another handle |

The `.txt` files use AiSList's own format: lines starting with `!` are comments. A client can download them from
`https://raw.githubusercontent.com/theSiegs/aislist-channel-ids/main/lists/aislist_blocklist_ids.txt`.

The **lists come from AiSList**. To add a channel or ask for one to be removed, use
[AiSList's own process](https://github.com/Override92/AiSList#readme), not this repo.

## How it works

`aislist_ids.py` runs on a GitHub Actions schedule:

1. It reads AiSList's newest lists.
   - It refuses a list that comes back empty or less than half its last size, and keeps publishing the last good one.
   - Some AiSList handles are URL-encoded (`@%C3%89cho`); they're decoded and counted once.
2. It looks up each **new** handle with the YouTube Data API (`channels.list?forHandle=`, 1 unit each).
   - The first full pass of about 28,000 handles takes about three days of the free daily quota.
   - After that, it handles AiSList's daily additions in minutes.
3. Once a day it re-checks a seventh of the known channels (50 per unit). Deleted channels drop out of the lists, and
   handle changes go to `reports/handle_changes.tsv`.
4. It commits the results. `data/channels.tsv` is its cache: handle, channel ID, status.

Handles a channel changes later don't matter: AiSList's own bot updates the list, and the new handle resolves to the
same channel ID.

## Running it yourself

You need Python 3.10 or later (no other packages) and a YouTube Data API v3 key.

```sh
AISLIST_YT_API_KEY=<key> python3 aislist_ids.py --budget 100
python3 -m unittest discover -s tests
```

For your own copy on GitHub, add the key as the repository secret `YT_API_KEY`.

## License

- **The lists** are AiSList's, by the AiSList contributors, under
  [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/): credit AiSList, and no commercial use. This repo
  only converts them from handles to channel IDs.
- **The code** is under the MIT license (`LICENSE`).

If AiSList publishes channel IDs itself, this repo will point there and stop updating.
