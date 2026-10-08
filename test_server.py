"""Offline tests for reddit-research-mcp. No network, no credentials needed.

Run:  python -m pytest -q test_server.py     (or: python test_server.py)

They patch `praw.Reddit` with a fake so every tool's parsing, capping and
error-mapping logic is exercised without ever talking to Reddit.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

# Credentials are required for _client(); fake ones are fine because praw is mocked.
os.environ.setdefault("REDDIT_CLIENT_ID", "fake-id")
os.environ.setdefault("REDDIT_CLIENT_SECRET", "fake-secret")
os.environ.setdefault("REDDIT_USERNAME", "u/tester")

import server  # noqa: E402


# ----------------------------------------------------------------------------- fakes
def _ns(**kw):
    return types.SimpleNamespace(**kw)


def _post(i, sub="smallbusiness", body="x" * 2000):
    return _ns(id=f"p{i}", title=f"Post {i}", subreddit=_ns(display_name=sub), author=_ns(name=f"user{i}"),
               score=100 - i, upvote_ratio=0.9, num_comments=5, created_utc=1_700_000_000 + i,
               link_flair_text=None, is_self=True, over_18=False, permalink=f"/r/{sub}/comments/p{i}/post_{i}/",
               url=f"https://www.reddit.com/r/{sub}/comments/p{i}/post_{i}/", selftext=body)


def _comment(i, score, replies=(), body="c" * 3000):
    return _ns(id=f"c{i}", author=_ns(name=f"c{i}"), score=score, created_utc=1_700_000_100 + i,
               is_submitter=(i == 0), body=body, replies=list(replies))


class FakeForest(list):
    def replace_more(self, limit=None):
        self.replace_more_called_with = limit


class FakeSubmission:
    def __init__(self, pid, comments):
        self.id, self._comments = pid, comments
        self.comment_sort = None
        self.comment_limit = None
        p = _post(99, body="full body " * 2000)
        for k, v in vars(p).items():
            if k != "id":
                setattr(self, k, v)
        self.num_comments = 1234

    @property
    def comments(self):
        return self._comments


class FakeSubreddit:
    def __init__(self, name):
        self.display_name = name
        self.calls = []

    def search(self, query, **kw):
        self.calls.append(("search", query, kw))
        n = kw.get("limit") or 25
        return iter(_post(i, sub=self.display_name) for i in range(n))

    def top(self, *, time_filter, limit):
        self.calls.append(("top", time_filter, limit))
        return iter(_post(i) for i in range(limit))

    def hot(self, limit):
        return iter(_post(i) for i in range(limit))

    def new(self, limit):
        return iter(_post(i) for i in range(limit))

    def rising(self, limit):
        return iter(_post(i) for i in range(limit))


class FakeReddit:
    def __init__(self, **kw):
        self.kw = kw
        self.read_only = False
        self.subs = {}
        self.subreddits = _ns(search=lambda q, limit: iter(
            _ns(display_name=f"sub{i}", title=f"Sub {i}", subscribers=1000 * (i + 1), over18=False,
                public_description="d" * 500) for i in range(limit)))

    def subreddit(self, name):
        return self.subs.setdefault(name, FakeSubreddit(name))

    def submission(self, id=None, url=None):
        deep = _comment(3, 1, replies=[_comment(4, 1, replies=[_comment(5, 1)])])
        forest = FakeForest([_comment(0, 5), _comment(1, 50, replies=[_comment(2, 7)]), deep])
        self.last_submission = FakeSubmission(id, forest)
        return self.last_submission


def _fresh():
    """Reset the lazy client and install the fake."""
    server._reddit = None
    fake = FakeReddit
    patcher = mock.patch("praw.Reddit", fake)
    return patcher


# ----------------------------------------------------------------------------- tests
def test_parse_post_id():
    assert server._parse_post_id("1abc2de") == "1abc2de"
    assert server._parse_post_id("t3_1ABC2DE") == "1abc2de"
    assert server._parse_post_id("https://www.reddit.com/r/x/comments/1abc2de/some_title/?utm=1") == "1abc2de"
    assert server._parse_post_id("https://redd.it/1abc2de") == "1abc2de"
    try:
        server._parse_post_id("not a post")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_subreddits_param():
    assert server._subreddits_param(None) == "all"
    assert server._subreddits_param([]) == "all"
    assert server._subreddits_param(["r/smallbusiness", "/r/Entrepreneur/", " sandiego "]) == "smallbusiness+Entrepreneur+sandiego"


def test_user_agent_format():
    assert server.USER_AGENT.startswith("macos:")
    assert server.USER_AGENT.endswith("(by /u/tester)")  # 'u/' prefix stripped, username kept


def test_search_defaults_to_all_and_caps_body():
    with _fresh():
        out = server.search_reddit("thc seltzer")
    assert "error" not in out, out
    assert out["scope"] == "all of Reddit"
    assert out["count"] == server.DEFAULT_SEARCH_LIMIT
    p = out["posts"][0]
    assert p["permalink"].startswith("https://www.reddit.com/r/")
    assert len(p["body"]) <= server.POST_BODY_SNIPPET
    sub = server._reddit.subs["all"]
    assert sub.calls[0][2]["time_filter"] == "year" and sub.calls[0][2]["sort"] == "relevance"


def test_search_scoped_and_limit_clamped():
    with _fresh():
        out = server.search_reddit("sourdough", subreddits=["Sourdough", "r/Breadit"], limit=500, include_body=False)
    assert out["scope"] == "r/Sourdough+Breadit"
    assert out["count"] == server.MAX_LIMIT          # 500 clamped to the hard cap
    assert out["posts"][0]["body"] == ""


def test_search_empty_query():
    with _fresh():
        assert "error" in server.search_reddit("   ")


def test_get_thread_sets_sort_before_fetch_and_caps_comments():
    with _fresh():
        out = server.get_thread("https://www.reddit.com/r/x/comments/1abc2de/t/", max_comments=4, max_depth=1)
    assert "error" not in out, out
    assert out["post"]["id"] == "1abc2de"
    assert len(out["post"]["body"]) <= server.POST_BODY_FULL
    assert out["comments_total"] == 1234
    assert out["comments_included"] == 4
    # top-level sorted by score desc: c1 (50) first, then c0 (5), then c3 (1)
    assert [c["id"] for c in out["comments"]] == ["c1", "c0", "c3"]
    assert out["comments"][0]["replies"][0]["id"] == "c2"
    # budget of 4: c1, c2 (reply), c0, c3 -> c3's reply c4 is cut off by the budget
    assert "replies" not in out["comments"][2]
    assert all(len(c["body"]) <= server.COMMENT_BODY_MAX for c in out["comments"])
    sub = server._reddit.last_submission
    assert sub.comment_sort == "top" and sub.comment_limit == 4      # set before any fetch
    assert sub.comments.replace_more_called_with == 0                 # no extra API calls for "load more"


def test_get_thread_bad_ref():
    with _fresh():
        out = server.get_thread("not a post!")
    assert "error" in out and "post id" in out["error"]


def test_subreddit_posts_top_passes_time_filter_by_keyword():
    with _fresh():
        out = server.subreddit_posts("r/smallbusiness", listing="top", time_filter="month", limit=3)
    assert out["subreddit"] == "r/smallbusiness" and out["count"] == 3
    assert server._reddit.subs["smallbusiness"].calls[0] == ("top", "month", 3)


def test_find_subreddits_sorted_by_size():
    with _fresh():
        out = server.find_subreddits("hemp beverages", limit=3)
    assert [s["subscribers"] for s in out["subreddits"]] == [3000, 2000, 1000]
    assert all(len(s["description"]) <= 300 for s in out["subreddits"])


def test_missing_credentials_message():
    server._reddit = None
    with mock.patch.object(server, "CLIENT_ID", ""), mock.patch.object(server, "CLIENT_SECRET", ""):
        out = server.search_reddit("anything")
    assert "error" in out and "REDDIT_CLIENT_ID" in out["error"]


def test_error_mapping_never_leaks_secret():
    class Boom(Exception):
        pass
    msg = server._friendly_error(Boom("received 403 HTTP response using client fake-secret"))
    assert "fake-secret" not in msg and "403" in msg


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL", name, "->", repr(e))
    sys.exit(1 if failed else 0)
