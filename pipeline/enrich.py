"""Pipeline stage: Enrich PRs with GitHub API data (resumable, rate-limit aware)."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from config import DBConfig
from db.connection import DBAdapter
from db.repository import PRRepository

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"
REST_BASE = "https://api.github.com"

REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $prNumber: Int!, $threadCursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $prNumber) {
      reviewThreads(first: 100, after: $threadCursor) {
        nodes {
          id
          isResolved
          resolvedBy { login }
          comments(first: 50) {
            nodes {
              databaseId
              body
              path
              line
              originalLine
              diffHunk
              author { login }
              createdAt
              reactionGroups {
                content
                reactors { totalCount }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

# Steps in order
ENRICHMENT_STEPS = ["bq_events", "commits", "reviews", "threads", "details", "done"]


class RateLimitExhausted(Exception):
    """Raised when GitHub rate limit is hit, with reset timestamp."""
    def __init__(self, reset_at: int):
        self.reset_at = reset_at
        super().__init__(f"Rate limit exhausted, resets at {reset_at}")


class GitHubEnrichClient:
    """Async GitHub API client with rate limiting and retries — adapted from gh_enrich.py."""

    def __init__(self, token: str, concurrency: int = 10):
        self.token = token
        self.semaphore = asyncio.Semaphore(concurrency)
        self.api_calls = 0
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _check_rate_limit(self, response: httpx.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset_time = response.headers.get("X-RateLimit-Reset")
        if remaining is not None and int(remaining) < 10:
            if reset_time:
                raise RateLimitExhausted(int(reset_time))
        self.api_calls += 1
        if self.api_calls % 100 == 0:
            logger.info(f"GitHub API calls: {self.api_calls}, remaining: {remaining}")

    async def rest_get(self, path: str, params: dict | None = None) -> httpx.Response | None:
        async with self.semaphore:
            client = await self._get_client()
            url = f"{REST_BASE}{path}"
            for attempt in range(4):
                try:
                    resp = await client.get(url, params=params)
                    if resp.status_code == 403:
                        reset_time = resp.headers.get("X-RateLimit-Reset")
                        if reset_time:
                            raise RateLimitExhausted(int(reset_time))
                        wait = 60
                        logger.warning(f"403 on {url}, sleeping {wait}s (attempt {attempt + 1})")
                        await asyncio.sleep(wait)
                        continue
                    await self._check_rate_limit(resp)
                    if resp.status_code in (404, 422):
                        logger.warning(f"{resp.status_code} for {url} — skipping")
                        return None
                    if resp.status_code == 301:
                        location = resp.headers.get("Location", "unknown")
                        raise httpx.HTTPStatusError(
                            f"301 Moved Permanently (repo likely renamed) → {location}",
                            request=resp.request, response=resp,
                        )
                    if resp.status_code >= 500:
                        wait = 2 ** attempt
                        logger.warning(f"{resp.status_code} on {url}, retrying in {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return resp
                except RateLimitExhausted:
                    raise
                except httpx.HTTPError as e:
                    if attempt < 3:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        logger.error(f"Failed after 4 attempts: {url}: {e}")
                        return None
        return None

    async def rest_get_paginated(self, path: str, params: dict | None = None) -> list[dict]:
        results: list[dict] = []
        params = dict(params or {})
        params.setdefault("per_page", "100")
        page = 1
        while True:
            params["page"] = str(page)
            resp = await self.rest_get(path, params)
            if resp is None:
                break
            data = resp.json()
            if not isinstance(data, list) or len(data) == 0:
                break
            results.extend(data)
            link = resp.headers.get("Link", "")
            if 'rel="next"' not in link:
                break
            page += 1
        return results

    async def graphql(self, query: str, variables: dict) -> dict | None:
        async with self.semaphore:
            client = await self._get_client()
            for attempt in range(4):
                try:
                    resp = await client.post(
                        GRAPHQL_URL,
                        json={"query": query, "variables": variables},
                    )
                    if resp.status_code == 403:
                        reset_time = resp.headers.get("X-RateLimit-Reset")
                        if reset_time:
                            raise RateLimitExhausted(int(reset_time))
                        await asyncio.sleep(60)
                        continue
                    await self._check_rate_limit(resp)
                    if resp.status_code >= 500:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    data = resp.json()
                    if "errors" in data:
                        logger.warning(f"GraphQL errors: {data['errors']}")
                        if data.get("data") is not None:
                            return data["data"]
                        return None
                    return data.get("data")
                except RateLimitExhausted:
                    raise
                except httpx.HTTPError as e:
                    if attempt < 3:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        logger.error(f"GraphQL failed after 4 attempts: {e}")
                        return None
        return None


# -- Enrichment sub-steps (return JSONB-ready data) ----------------------------

async def _fetch_commits(gh: GitHubEnrichClient, owner: str, repo: str, pr_number: int) -> list[dict]:
    path = f"/repos/{owner}/{repo}/pulls/{pr_number}/commits"
    raw = await gh.rest_get_paginated(path)
    return [
        {
            "sha": c["sha"],
            "message": c.get("commit", {}).get("message", ""),
            "date": c.get("commit", {}).get("author", {}).get("date", ""),
            "author": (c.get("author") or {}).get("login"),
        }
        for c in raw
    ]


async def _fetch_reviews(gh: GitHubEnrichClient, owner: str, repo: str, pr_number: int) -> list[dict]:
    path = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    raw = await gh.rest_get_paginated(path)
    return [
        {
            "id": r["id"],
            "author": (r.get("user") or {}).get("login"),
            "state": r.get("state", ""),
            "body": r.get("body", ""),
            "submitted_at": r.get("submitted_at"),
            "commit_id": r.get("commit_id"),
            "author_association": r.get("author_association"),
        }
        for r in raw
    ]


async def _fetch_review_threads(gh: GitHubEnrichClient, owner: str, repo: str, pr_number: int) -> list[dict]:
    all_threads: list[dict] = []
    cursor = None
    while True:
        variables = {"owner": owner, "repo": repo, "prNumber": pr_number, "threadCursor": cursor}
        data = await gh.graphql(REVIEW_THREADS_QUERY, variables)
        if data is None:
            break
        pr_data = (data.get("repository") or {}).get("pullRequest")
        if pr_data is None:
            break
        threads_data = pr_data.get("reviewThreads", {})
        for node in threads_data.get("nodes", []):
            thread = {
                "id": node["id"],
                "is_resolved": node["isResolved"],
                "resolved_by": (node.get("resolvedBy") or {}).get("login"),
                "comments": [],
            }
            for comment in (node.get("comments") or {}).get("nodes", []):
                reactions = {}
                for rg in comment.get("reactionGroups") or []:
                    reactions[rg["content"]] = rg["reactors"]["totalCount"]
                thread["comments"].append({
                    "id": comment["databaseId"],
                    "body": comment["body"],
                    "path": comment.get("path"),
                    "line": comment.get("line"),
                    "original_line": comment.get("originalLine"),
                    "diff_hunk": comment.get("diffHunk"),
                    "author": (comment.get("author") or {}).get("login"),
                    "created_at": comment.get("createdAt"),
                    "reactions": reactions,
                })
            all_threads.append(thread)
        page_info = threads_data.get("pageInfo", {})
        if page_info.get("hasNextPage"):
            cursor = page_info["endCursor"]
        else:
            break
    return all_threads


async def _fetch_commit_details(
    gh: GitHubEnrichClient, owner: str, repo: str, commits: list[dict]
) -> list[dict]:
    details: list[dict] = []
    for commit in commits:
        sha = commit["sha"]
        resp = await gh.rest_get(f"/repos/{owner}/{repo}/commits/{sha}")
        if resp is None:
            details.append({"sha": sha, "files": []})
            continue
        data = resp.json()
        files = []
        for f in data.get("files", []):
            files.append({
                "filename": f["filename"],
                "status": f.get("status", "unknown"),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "patch": f.get("patch", ""),
            })
        details.append({"sha": sha, "files": files})
    return details


# -- PR summary (lightweight size check) ---------------------------------------

async def _fetch_pr_summary(
    gh: GitHubEnrichClient, owner: str, repo: str, pr_number: int
) -> dict | None:
    """Fetch PR summary (1 API call) to check size before full enrichment."""
    resp = await gh.rest_get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
    if resp is None:
        return None
    data = resp.json()
    return {
        "additions": data.get("additions", 0),
        "deletions": data.get("deletions", 0),
        "commits": data.get("commits", 0),
        "changed_files": data.get("changed_files", 0),
    }


# -- Token pool for multi-token rotation ---------------------------------------

class TokenPool:
    """Manages multiple GitHubEnrichClient instances, rotating on rate-limit."""

    def __init__(self, tokens: list[str], concurrency: int = 10):
        self._entries = [
            {"client": GitHubEnrichClient(t, concurrency), "reset_at": 0}
            for t in tokens
        ]
        self._idx = 0

    def get(self) -> GitHubEnrichClient | None:
        """Round-robin to next non-rate-limited client. None if all exhausted."""
        now = time.time()
        n = len(self._entries)
        for _ in range(n):
            entry = self._entries[self._idx]
            self._idx = (self._idx + 1) % n
            if entry["reset_at"] <= now:
                return entry["client"]
        return None

    def mark_limited(self, client: GitHubEnrichClient, reset_at: int) -> None:
        for e in self._entries:
            if e["client"] is client:
                e["reset_at"] = reset_at
                break

    def earliest_reset(self) -> float:
        return min(e["reset_at"] for e in self._entries)

    async def close(self) -> None:
        for e in self._entries:
            await e["client"].close()


# -- Main enrichment logic -----------------------------------------------------

def _step_index(step: str | None) -> int:
    """Return the index of a step in the enrichment sequence, -1 if not started."""
    if step is None:
        return -1
    try:
        return ENRICHMENT_STEPS.index(step)
    except ValueError:
        return -1


async def enrich_single_pr(
    gh: GitHubEnrichClient,
    repo_obj: PRRepository,
    pr_row: dict,
    cfg: DBConfig | None = None,
) -> None:
    """Enrich a single PR, resuming from wherever we left off."""
    pr_id = pr_row["id"]
    repo_name = pr_row["repo_name"]
    pr_number = pr_row["pr_number"]
    current_step = pr_row.get("enrichment_step")
    step_idx = _step_index(current_step)

    owner, repo = repo_name.split("/", 1)

    # Size check: fetch PR summary and skip if too large
    if cfg is not None and step_idx < 1:
        summary = await _fetch_pr_summary(gh, owner, repo, pr_number)
        if summary is not None:
            total_lines = summary["additions"] + summary["deletions"]
            if summary["commits"] > cfg.max_pr_commits:
                reason = f"Too many commits: {summary['commits']} > {cfg.max_pr_commits}"
                logger.warning(f"Skipping {repo_name}#{pr_number}: {reason}")
                await repo_obj.mark_skipped(pr_id, reason)
                return
            if total_lines > cfg.max_pr_changed_lines:
                reason = f"Too many changed lines: {total_lines} > {cfg.max_pr_changed_lines}"
                logger.warning(f"Skipping {repo_name}#{pr_number}: {reason}")
                await repo_obj.mark_skipped(pr_id, reason)
                return

    # Step: commits (index 1)
    if step_idx < 1:
        commits = await _fetch_commits(gh, owner, repo, pr_number)
        await repo_obj.update_commits(pr_id, commits)
        logger.debug(f"  {repo_name}#{pr_number}: commits done ({len(commits)})")

    # Step: reviews (index 2)
    if step_idx < 2:
        reviews = await _fetch_reviews(gh, owner, repo, pr_number)
        await repo_obj.update_reviews(pr_id, reviews)
        logger.debug(f"  {repo_name}#{pr_number}: reviews done ({len(reviews)})")

    # Step: threads (index 3)
    if step_idx < 3:
        threads = await _fetch_review_threads(gh, owner, repo, pr_number)
        await repo_obj.update_threads(pr_id, threads)
        logger.debug(f"  {repo_name}#{pr_number}: threads done ({len(threads)})")

    # Step: commit details (index 4)
    if step_idx < 4:
        # Need commits data — either from this run or from DB
        commits_json = pr_row.get("commits")
        if commits_json:
            commits_data = json.loads(commits_json) if isinstance(commits_json, str) else commits_json
        else:
            # Re-read from DB since we just wrote it
            refreshed = await repo_obj.get_pr_by_id(pr_id)
            commits_json = refreshed.get("commits") if refreshed else None
            commits_data = json.loads(commits_json) if commits_json else []

        details = await _fetch_commit_details(gh, owner, repo, commits_data)
        await repo_obj.update_commit_details(pr_id, details)
        logger.debug(f"  {repo_name}#{pr_number}: commit details done")

    # Mark enrichment complete
    await repo_obj.mark_enrichment_done(pr_id)
    logger.info(f"Enriched {repo_name}#{pr_number}")


async def enrich_loop(
    cfg: DBConfig,
    db: DBAdapter,
    chatbot_id: int,
    chatbot_username: str | None = None,
    max_prs: int | None = None,
    one_shot: bool = False,
) -> int:
    """Main enrichment loop. Processes pending PRs until exhausted or rate-limited.

    If one_shot=True, processes available PRs once and returns.
    If one_shot=False, runs indefinitely (daemon mode), sleeping when idle or rate-limited.
    If chatbot_username is provided, assembles enriched PRs after each pass.

    Returns total number of PRs enriched.
    """
    from pipeline.assemble import assemble_enriched_prs

    repo = PRRepository(db)
    tokens = cfg.github_tokens if cfg.github_tokens else [cfg.github_token]
    pool = TokenPool(tokens)
    n_tokens = len(tokens)
    logger.info(f"Using {n_tokens} GitHub token(s)")

    enriched_count = 0
    batch_size = 100
    limit = max_prs or 10000

    async def _enrich_one(pr_row: dict) -> bool:
        """Enrich a single PR with locking and token rotation. Returns True if enriched."""
        pr_id = pr_row["id"]
        locked = await repo.lock_pr(pr_id, cfg.worker_id, cfg.lock_timeout_minutes)
        if not locked:
            return False
        try:
            while True:
                gh = pool.get()
                if gh is None:
                    wait = max(0, pool.earliest_reset() - time.time()) + 5
                    logger.warning(f"All {n_tokens} tokens rate-limited, sleeping {wait:.0f}s")
                    await asyncio.sleep(wait)
                    continue
                try:
                    await enrich_single_pr(gh, repo, pr_row, cfg)
                    return True
                except RateLimitExhausted as e:
                    pool.mark_limited(gh, e.reset_at)
                    logger.info(f"Token rate-limited, rotating ({n_tokens} total)")
                    continue
        except Exception as e:
            logger.error(f"Error enriching PR {pr_row['repo_name']}#{pr_row['pr_number']}: {e}")
            await repo.mark_error(pr_id, str(e))
            return False

    try:
        while True:
            prs = await repo.get_pending_prs(chatbot_id, limit=limit)
            if not prs:
                if one_shot:
                    logger.info("No pending PRs found.")
                    break
                logger.info("No pending PRs, sleeping 5 minutes...")
                await asyncio.sleep(300)
                continue

            # Process PRs in concurrent batches
            for i in range(0, len(prs), batch_size):
                batch = prs[i : i + batch_size]
                results = await asyncio.gather(
                    *[_enrich_one(pr_row) for pr_row in batch],
                    return_exceptions=True,
                )

                for result in results:
                    if isinstance(result, Exception):
                        logger.error(f"Unexpected error in batch: {result}")
                    elif result is True:
                        enriched_count += 1

                logger.info(f"Enriched {enriched_count} PRs so far")

                if max_prs and enriched_count >= max_prs:
                    logger.info(f"Reached max_prs limit ({max_prs})")
                    return enriched_count

            # Assemble any newly enriched PRs
            if chatbot_username:
                assembled = await assemble_enriched_prs(db, chatbot_id, chatbot_username)
                if assembled:
                    logger.info(f"Assembled {assembled} PRs")

            if one_shot:
                break

    finally:
        await pool.close()

    return enriched_count
