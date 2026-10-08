# reddit-research-mcp — one-time setup

This gives Claude a read-only window into public Reddit so you can ask it to search any topic and
read what real people are saying. It runs on your Mac; Claude launches it on demand.

> 🔐 **Your Reddit credentials never reach Claude.** They live in a `.env` file in this folder.
> The server reads them from the environment and only ever calls read-only endpoints.

---

## Step 0 — Reddit API access (the part that is out of our hands)

Since November 2025 Reddit no longer hands out API credentials on demand. New Data API access
requires a request through Reddit Developer Support and Reddit's approval under its
[Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy).

Until you have a **client id** and **secret**, every tool returns a clear "credentials are not
configured" message. Everything else below can be done in advance.

If you created a Reddit app **before** November 2025 on any account, its credentials still work.
Check the top of https://www.reddit.com/prefs/apps for a "developed applications" section.

---

## Step 1 — Install (Terminal)

```bash
cd "/Users/adamgrodin/Documents/Claude/Projects/reddit-research-mcp"
/opt/homebrew/opt/python@3.13/bin/python3.13 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python test_server.py      # should print 12 lines of "ok"
```

Full path to the venv python (needed in Step 3):
`/Users/adamgrodin/Documents/Claude/Projects/reddit-research-mcp/.venv/bin/python`

---

## Step 2 — Credentials

```bash
cp .env.example .env
open -e .env
```

Fill in `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` and `REDDIT_USERNAME`, save, close.
`.env` is git-ignored, so it never ends up in a repo.

---

## Step 3 — Tell Claude Desktop about it

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` and add this entry inside
`"mcpServers"`, next to `vayu-meta-ads`:

```json
"reddit-research": {
  "command": "/Users/adamgrodin/Documents/Claude/Projects/reddit-research-mcp/.venv/bin/python",
  "args": ["/Users/adamgrodin/Documents/Claude/Projects/reddit-research-mcp/server.py"]
}
```

Quit and reopen Claude Desktop. The server appears as **reddit-research** with four tools.
Because it's a local server on the Mac mini, it is also available in Cowork sessions linked to this
machine (same as `vayu-meta-ads`), but not from the phone or claude.ai in a browser.

---

## Step 4 — Try it

> "Search Reddit for what people think of THC seltzers in the last month, then open the most
> discussed thread."

Claude will call `search_reddit` and then `get_thread`. To narrow to communities:

> "Search r/smallbusiness and r/Entrepreneur for 'FedEx route' from the past year."

---

## Troubleshooting

| Symptom | Meaning |
|---|---|
| "credentials are not configured" | `.env` is missing or a value is blank. |
| "rejected the credentials (401)" | Client id or secret is wrong. |
| "refused the request (403)" | Reddit hasn't approved the app for Data API access, or the subreddit is private. |
| "Rate limited (429)" | Too many calls in a minute; wait and ask for fewer results. |
| Server doesn't appear in Claude Desktop | Check the JSON (commas!), and that the python path in Step 3 exists. |

Logs go to stderr; Claude Desktop keeps them under `~/Library/Logs/Claude/mcp-server-reddit-research.log`.
