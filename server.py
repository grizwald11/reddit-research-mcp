#!/usr/bin/env python3
"""
Reddit Research MCP server
==========================
A small, self-hosted Model Context Protocol server that lets Claude search and
read PUBLIC Reddit content for consumer research. It is strictly read-only.

What it does
------------
  search_reddit      keyword search across all of Reddit (or chosen subreddits)
  get_thread         one post + its top comments, capped so it fits in context
  subreddit_posts    hot / top / new / rising feed of one subreddit
  find_subreddits    discover communities related to a topic

What it never does
------------------
  No posting, commenting, voting, messaging, saving, or any other write action.
  The OAuth client is created in app-only (read_only) mode and the server only
  calls read endpoints. Nothing is stored on disk; results exist only in the
  current conversation.

SECURITY
--------
Credentials are read ONLY from environment variables (or a .env file sitting
next to this script). They are never returned by any tool, never logged, and
never echoed in error messages.

Required env:
  REDDIT_CLIENT_ID       from reddit.com/prefs/apps (the short string under the app name)
  REDDIT_CLIENT_SECRET   from the same page ("secret")
  REDDIT_USERNAME        your Reddit username, required in the User-Agent by Reddit's API rules
Optional env:
  REDDIT_APP_NAME        default reddit-research-mcp (used in the User-Agent)
  REDDIT_MAX_LIMIT       hard cap on results per call, default 50

Run (stdio):  python server.py
Deps:         pip install -r requirements.txt
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from dotenv import load_dotenv

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ModuleNotFoundError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server

# --------------------------------------------------------------------------- #
# Configuration (from environment only)                                       #
# --------------------------------------------------------------------------- #
load_dotenv(Path(__file__).with_name(".env"))

CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
USERNAME = re.sub(r"^/?u/", "", os.environ.get("REDDIT_USERNAME", "").strip())
APP_NAME = os.environ.get("REDDIT_APP_NAME", "reddit-research-mcp").strip()
MAX_LIMIT = int(os.environ.get("REDDIT_MAX_LIMIT", "50"))
VERSION = "1.0.0"

# Reddit's required User-Agent format: <platform>:<app ID>:<version> (by /u/<username>)
USER_AGENT = f"macos:{APP_NAME}:v{VERSION} (by /u/{USERNAME or 'unknown'})"

# stdio transport: NEVER print to stdout. Log to stderr only.
logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("reddit-research-mcp")

# Output caps so a single call can't flood the model's context.
POST_BODY_SNIPPET = 600      # chars of post body shown in search results
POST_BODY_FULL = 8000        # chars of post body shown in get_thread
COMMENT_BODY_MAX = 1500      # chars per comment
DEFAULT_SEARCH_LIMIT = 15
DEFAULT_COMMENT_LIMIT = 30

SortSearch = Literal["relevance", "hot", "top", "new", "comments"]
TimeFilter = Literal["all", "year", "month", "week", "day", "hour"]
CommentSort = Literal["top", "best", "new", "controversial", "old", "qa"]
Listing = Literal["hot", "top", "new", "rising"]

mcp = _Server("reddit-research")

# --------------------------------------------------------------------------- #
# Reddit client (lazy, read-only)                                             #
# --------------------------------------------------------------------------- #
_reddit = None


class ConfigError(RuntimeError):
    pass


def _client():
    """Create the PRAW client on first use so a missing .env doesn't crash startup."""
    global _reddit
    if _reddit is not None:
        return _reddit
    missing = [k for k, v in (("REDDIT_CLIENT_ID", CLIENT_ID), ("REDDIT_CLIENT_SECRET", CLIENT_SECRET),
                               ("REDDIT_USERNAME", USERNAME)) if not v]
    if missing:
        raise ConfigError(
            "Reddit credentials are not configured. Set " + ", ".join(missing) +
            " in the .env file next to server.py (see .env.example), then restart Claude Desktop."
        )
    import praw  # imported lazily so `import server` works without credentials in tests

    _reddit = praw.Reddit(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        user_agent=USER_AGENT,
        check_for_updates=False,
        ratelimit_seconds=30,   # let PRAW sleep (up to 30s) rather than fail when Reddit asks us to wait
    )
    _reddit.read_only = True    # app-only auth is read-only already; make it explicit
    return _reddit


def _friendly_error(exc: Exception) -> str:
    """Map PRAW/prawcore exceptions to plain-English messages without leaking secrets."""
    name = type(exc).__name__
    text = str(exc)
    # Never echo credentials even if a library includes them in a message.
    for secret in (CLIENT_ID, CLIENT_SECRET):
        if secret:
            text = text.replace(secret, "***")
    if isinstance(exc, ConfigError):
        return text
    if "401" in text or name in ("OAuthException", "InvalidToken"):
        return "Reddit rejected the credentials (401). Check REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET in .env."
    if "403" in text or name == "Forbidden":
        return ("Reddit refused the request (403). This usually means the app has not been approved for "
                "Data API access yet, or the subreddit is private/quarantined.")
    if "404" in text or name == "NotFound":
        return "Not found (404): the post, comment, or subreddit does not exist or was removed."
    if "429" in text or name == "TooManyRequests":
        return "Rate limited by Reddit (429). Wait a minute and try again with fewer results."
    if name in ("RequestException", "ConnectionError", "ServerError"):
        return f"Could not reach Reddit ({name}). Check the network and try again."
    return f"Reddit error ({name}): {text}"


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
_POST_ID_RE = re.compile(r"/comments/([a-z0-9]{3,12})", re.I)
_SHORT_URL_RE = re.compile(r"redd\.it/([a-z0-9]{3,12})", re.I)
_BARE_ID_RE = re.compile(r"^(?:t3_)?([a-z0-9]{3,12})$", re.I)


def _parse_post_id(ref: str) -> str:
    """Accept a bare id ('1abc2de'), a fullname ('t3_1abc2de'), a redd.it short link, or any post URL."""
    ref = ref.strip()
    for rx in (_POST_ID_RE, _SHORT_URL_RE, _BARE_ID_RE):
        m = rx.search(ref)
        if m:
            return m.group(1).lower()
    raise ValueError(f"Could not find a Reddit post id in: {ref!r}")


def _clamp(n: Optional[int], default: int, hard_max: int = MAX_LIMIT) -> int:
    if n is None:
        return default
    return max(1, min(int(n), hard_max))


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _trim(text: Optional[str], n: int) -> str:
    if n <= 0:
        return ""
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _author(obj) -> Optional[str]:
    a = getattr(obj, "author", None)
    return getattr(a, "name", None) if a else None


def _subreddits_param(subreddits: Optional[list[str]]) -> str:
    """None/empty -> 'all'; ['a','b'] -> 'a+b'. Strips r/ prefixes."""
    if not subreddits:
        return "all"
    names = [re.sub(r"^/?r/", "", s.strip()).strip("/") for s in subreddits if s and s.strip()]
    return "+".join(names) if names else "all"


def _post_summary(p, body_chars: int) -> dict[str, Any]:
    return {
        "id": p.id,
        "title": p.title,
        "subreddit": p.subreddit.display_name,
        "author": _author(p),
        "score": p.score,
        "upvote_ratio": getattr(p, "upvote_ratio", None),
        "num_comments": p.num_comments,
        "created": _ts(p.created_utc),
        "flair": getattr(p, "link_flair_text", None),
        "is_self_post": bool(getattr(p, "is_self", False)),
        "nsfw": bool(getattr(p, "over_18", False)),
        "permalink": f"https://www.reddit.com{p.permalink}",
        "link_url": None if getattr(p, "is_self", False) else getattr(p, "url", None),
        "body": _trim(getattr(p, "selftext", ""), body_chars),
    }


def _comment_dict(c, depth: int, max_depth: int, budget: list[int]) -> Optional[dict[str, Any]]:
    """Serialize a comment and (up to max_depth) its replies, decrementing a shared count budget."""
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    out = {
        "id": c.id,
        "author": _author(c),
        "score": c.score,
        "created": _ts(c.created_utc),
        "is_op": bool(getattr(c, "is_submitter", False)),
        "body": _trim(getattr(c, "body", ""), COMMENT_BODY_MAX),
    }
    if depth < max_depth:
        replies = []
        children = list(getattr(c, "replies", []) or [])
        children.sort(key=lambda r: getattr(r, "score", 0), reverse=True)
        for r in children:
            if budget[0] <= 0:
                break
            d = _comment_dict(r, depth + 1, max_depth, budget)
            if d:
                replies.append(d)
        if replies:
            out["replies"] = replies
    return out


# --------------------------------------------------------------------------- #
# Tools                                                                       #
# --------------------------------------------------------------------------- #
@mcp.tool()
def search_reddit(
    query: str,
    subreddits: Optional[list[str]] = None,
    sort: SortSearch = "relevance",
    time_filter: TimeFilter = "year",
    limit: int = DEFAULT_SEARCH_LIMIT,
    include_body: bool = True,
) -> dict[str, Any]:
    """Search public Reddit posts by keyword. Searches ALL of Reddit unless `subreddits` is given.

    Args:
        query: Free-text search. Reddit search supports quotes for exact phrases, e.g. "thc seltzer".
        subreddits: Optional list to restrict the search, e.g. ["smallbusiness", "Entrepreneur"].
            Omit (or pass []) to search everywhere.
        sort: relevance | hot | top | new | comments (comments = most-discussed first).
        time_filter: all | year | month | week | day | hour. Default "year".
        limit: Number of posts to return (1-50). Default 15.
        include_body: Include the first ~600 chars of each post's text.

    Returns:
        {"query", "scope", "count", "posts": [...]} where each post has id, title, subreddit,
        score, num_comments, created, permalink, and a body snippet. Use get_thread(id) to read
        the full post and its comments.
    """
    try:
        if not query or not query.strip():
            return {"error": "query is required"}
        scope = _subreddits_param(subreddits)
        n = _clamp(limit, DEFAULT_SEARCH_LIMIT)
        r = _client()
        results = r.subreddit(scope).search(query.strip(), sort=sort, syntax="lucene",
                                            time_filter=time_filter, limit=n)
        posts = [_post_summary(p, POST_BODY_SNIPPET if include_body else 0) for p in results]
        return {"query": query, "scope": "all of Reddit" if scope == "all" else f"r/{scope}",
                "sort": sort, "time_filter": time_filter, "count": len(posts), "posts": posts}
    except Exception as exc:  # noqa: BLE001 - always return a readable message to the model
        log.warning("search_reddit failed: %s", type(exc).__name__)
        return {"error": _friendly_error(exc)}


@mcp.tool()
def get_thread(
    post: str,
    max_comments: int = DEFAULT_COMMENT_LIMIT,
    comment_sort: CommentSort = "top",
    max_depth: int = 2,
) -> dict[str, Any]:
    """Read one post in full plus its top comments (capped so it fits in context).

    Args:
        post: A post id from search_reddit (e.g. "1abc2de"), a fullname ("t3_1abc2de"), or a reddit URL.
        max_comments: Total comments to include, counting replies (1-50). Default 30.
        comment_sort: top | best | new | controversial | old | qa. Default "top".
        max_depth: How many reply levels to include under each top-level comment (0-5). Default 2.

    Returns:
        {"post": {...full post...}, "comments": [...nested...], "comments_included", "comments_total"}
    """
    try:
        pid = _parse_post_id(post)
        n = _clamp(max_comments, DEFAULT_COMMENT_LIMIT)
        depth = max(0, min(int(max_depth), 5))
        r = _client()
        s = r.submission(id=pid)
        # PRAW 8: these must be set BEFORE the submission is fetched.
        s.comment_sort = comment_sort
        s.comment_limit = n
        # limit=0: drop "load more" stubs without making extra API requests.
        s.comments.replace_more(limit=0)
        top_level = list(s.comments)
        if comment_sort in ("top", "best"):
            top_level.sort(key=lambda c: getattr(c, "score", 0), reverse=True)
        budget = [n]
        comments = []
        for c in top_level:
            if budget[0] <= 0:
                break
            d = _comment_dict(c, 0, depth, budget)
            if d:
                comments.append(d)
        return {
            "post": _post_summary(s, POST_BODY_FULL),
            "comment_sort": comment_sort,
            "comments_included": n - budget[0],
            "comments_total": s.num_comments,
            "comments": comments,
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("get_thread failed: %s", type(exc).__name__)
        return {"error": _friendly_error(exc)}


@mcp.tool()
def subreddit_posts(
    subreddit: str,
    listing: Listing = "hot",
    time_filter: TimeFilter = "week",
    limit: int = DEFAULT_SEARCH_LIMIT,
    include_body: bool = True,
) -> dict[str, Any]:
    """Browse one subreddit's feed (what people are talking about right now).

    Args:
        subreddit: Name without the r/ prefix, e.g. "smallbusiness".
        listing: hot | top | new | rising. Default "hot".
        time_filter: Only used for "top": all | year | month | week | day | hour. Default "week".
        limit: Posts to return (1-50). Default 15.
        include_body: Include the first ~600 chars of each post's text.
    """
    try:
        name = _subreddits_param([subreddit])
        if name == "all" and not subreddit.strip():
            return {"error": "subreddit is required"}
        n = _clamp(limit, DEFAULT_SEARCH_LIMIT)
        sr = _client().subreddit(name)
        if listing == "top":
            it = sr.top(time_filter=time_filter, limit=n)
        elif listing == "new":
            it = sr.new(limit=n)
        elif listing == "rising":
            it = sr.rising(limit=n)
        else:
            it = sr.hot(limit=n)
        posts = [_post_summary(p, POST_BODY_SNIPPET if include_body else 0) for p in it]
        return {"subreddit": f"r/{name}", "listing": listing,
                "time_filter": time_filter if listing == "top" else None,
                "count": len(posts), "posts": posts}
    except Exception as exc:  # noqa: BLE001
        log.warning("subreddit_posts failed: %s", type(exc).__name__)
        return {"error": _friendly_error(exc)}


@mcp.tool()
def find_subreddits(topic: str, limit: int = 10) -> dict[str, Any]:
    """Find communities related to a topic (useful before narrowing a search).

    Args:
        topic: e.g. "hemp beverages", "sourdough", "small business san diego".
        limit: Communities to return (1-50). Default 10.
    """
    try:
        if not topic or not topic.strip():
            return {"error": "topic is required"}
        n = _clamp(limit, 10)
        subs = []
        for sr in _client().subreddits.search(topic.strip(), limit=n):
            subs.append({
                "name": f"r/{sr.display_name}",
                "title": getattr(sr, "title", None),
                "subscribers": getattr(sr, "subscribers", None),
                "nsfw": bool(getattr(sr, "over18", False)),
                "description": _trim(getattr(sr, "public_description", ""), 300),
            })
        subs.sort(key=lambda d: d["subscribers"] or 0, reverse=True)
        return {"topic": topic, "count": len(subs), "subreddits": subs}
    except Exception as exc:  # noqa: BLE001
        log.warning("find_subreddits failed: %s", type(exc).__name__)
        return {"error": _friendly_error(exc)}


if __name__ == "__main__":
    mcp.run(transport="stdio")  # Claude Desktop launches this process and talks over stdin/stdout
