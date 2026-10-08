# reddit-research-mcp

A small, read-only [Model Context Protocol](https://modelcontextprotocol.io) server that lets
Claude search and read **public** Reddit posts and comments, so one person can ask open-ended
questions like *"what do people say about THC seltzers?"* and get real consumer opinions back.

It runs locally on the owner's own machine, is used by a single person, and never writes to Reddit.

## What it does

| Tool | Purpose |
|---|---|
| `search_reddit(query, subreddits?, sort?, time_filter?, limit?)` | Keyword search across **all of Reddit** (or a chosen list of subreddits). Returns title, subreddit, score, comment count, date, link and a short body snippet. |
| `get_thread(post, max_comments?, comment_sort?, max_depth?)` | One post in full plus its top comments, capped (default 30) so it fits in the model's context. |
| `subreddit_posts(subreddit, listing?, time_filter?, limit?)` | A subreddit's hot / top / new / rising feed. |
| `find_subreddits(topic, limit?)` | Discover communities related to a topic. |

## What it never does

- No posting, commenting, voting, messaging, saving, or any other write action.
- No scraping: every request goes through Reddit's official Data API over OAuth, in app-only
  (read-only) mode, with the User-Agent format Reddit requires.
- No storage: nothing is written to disk or any database. Results exist only in the current
  conversation.
- No redistribution, resale, or model training on Reddit data.
- Small footprint: results are capped per call (max 50 posts; comments default to 30 per thread and
  "load more" stubs are never expanded, so one thread costs one request). PRAW's built-in rate-limit
  handling keeps it well under Reddit's 100 requests/minute allowance.

## Setup

See [SETUP.md](SETUP.md). Short version: create a Reddit app of type **script**, put its client id,
secret and your username in `.env`, install the requirements into a venv, and add the server to
Claude Desktop's config.

## Tests

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python test_server.py
```

The tests mock the Reddit client; they need no credentials and never touch the network.

## Stack

Python 3.10+, [PRAW](https://praw.readthedocs.io/) 8, the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (works with 1.x and 2.x).
