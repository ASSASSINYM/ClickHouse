#!/usr/bin/env python3
# In our CI this script runs in style-test containers

import argparse
import logging
import os.path as p
import os
import re
from datetime import date, datetime, timedelta
from queue import Empty, Queue
from subprocess import CalledProcessError, DEVNULL
from threading import Thread
from time import sleep
from typing import Dict, List, Optional, TextIO

from fuzzywuzzy.fuzz import ratio  # type: ignore
from github import Github
from github.GithubException import RateLimitExceededException, UnknownObjectException
from github.NamedUser import NamedUser
from github.Issue import Issue
from github.PullRequest import PullRequest
from github.Repository import Repository
from git_helper import is_shallow, git_runner as runner

# This array gives the preferred category order, and is also used to
# normalize category names.
# Categories are used in .github/PULL_REQUEST_TEMPLATE.md, keep comments there
# updated accordingly
categories_preferred_order = (
    "Backward Incompatible Change",
    "New Feature",
    "Performance Improvement",
    "Improvement",
    "Bug Fix",
    "Build/Testing/Packaging Improvement",
    "Other",
)

FROM_REF = ""
TO_REF = ""
SHA_IN_CHANGELOG = []  # type: List[str]
GitHub = Github()
CACHE_PATH = p.join(p.dirname(p.realpath(__file__)), "gh_cache")


class Description:
    def __init__(
        self, number: int, user: NamedUser, html_url: str, entry: str, category: str
    ):
        self.number = number
        self.html_url = html_url
        self.user = user
        self.entry = entry
        self.category = category

    @property
    def formatted_entry(self) -> str:
        # Substitute issue links.
        # 1) issue number w/o markdown link
        entry = re.sub(
            r"([^[])#([0-9]{4,})",
            r"\1[#\2](https://github.com/ClickHouse/ClickHouse/issues/\2)",
            self.entry,
        )
        # 2) issue URL w/o markdown link
        entry = re.sub(
            r"([^(])https://github.com/ClickHouse/ClickHouse/issues/([0-9]{4,})",
            r"\1[#\2](https://github.com/ClickHouse/ClickHouse/issues/\2)",
            entry,
        )
        # It's possible that we face a secondary rate limit.
        # In this case we should sleep until we get it
        while True:
            try:
                user_name = self.user.name if self.user.name else self.user.login
                break
            except UnknownObjectException:
                user_name = self.user.login
                break
            except RateLimitExceededException:
                sleep_on_rate_limit()
        return (
            f"* {entry} [#{self.number}]({self.html_url}) "
            f"([{user_name}]({self.user.html_url}))."
        )

    # Sort PR descriptions by numbers
    def __eq__(self, other) -> bool:
        if not isinstance(self, type(other)):
            return NotImplemented
        return self.number == other.number

    def __lt__(self, other: "Description") -> bool:
        return self.number < other.number


class Worker(Thread):
    def __init__(self, request_queue: Queue, repo: Repository):
        Thread.__init__(self)
        self.queue = request_queue
        self.repo = repo
        self.response = []  # type: List[Description]

    def run(self):
        while not self.queue.empty():
            try:
                issue = self.queue.get()  # type: Issue
            except Empty:
                break  # possible race condition, just continue
            api_pr = get_pull_cached(self.repo, issue.number, issue.updated_at)
            in_changelog = False
            merge_commit = api_pr.merge_commit_sha
            try:
                runner.run(f"git rev-parse '{merge_commit}'")
            except CalledProcessError:
                # It's possible that commit not in the repo, just continue
                logging.info("PR %s does not belong to the repo", api_pr.number)
                continue

            in_changelog = merge_commit in SHA_IN_CHANGELOG
            if in_changelog:
                desc = generate_description(api_pr, self.repo)
                if desc is not None:
                    self.response.append(desc)

            self.queue.task_done()


def sleep_on_rate_limit(time: int = 20):
    logging.warning("Faced rate limit, sleeping %s", time)
    sleep(time)


def get_pull_cached(
    repo: Repository, number: int, updated_at: Optional[datetime] = None
) -> PullRequest:
    pr_cache_file = p.join(CACHE_PATH, f"{number}.pickle")
    if updated_at is None:
        updated_at = datetime.now() - timedelta(hours=-1)

    if p.isfile(pr_cache_file):
        cache_updated = datetime.fromtimestamp(p.getmtime(pr_cache_file))
        if cache_updated > updated_at:
            with open(pr_cache_file, "rb") as prfd:
                return GitHub.load(prfd)  # type: ignore
    while True:
        try:
            pr = repo.get_pull(number)
            break
        except RateLimitExceededException:
            sleep_on_rate_limit()
    with open(pr_cache_file, "wb") as prfd:
        GitHub.dump(pr, prfd)  # type: ignore
    return pr


def get_descriptions(
    repo: Repository, issues: List[Issue], jobs: int
) -> Dict[str, List[Description]]:
    workers = []  # type: List[Worker]
    queue = Queue()  # type: Queue[Issue]
    for issue in issues:
        queue.put(issue)
    for _ in range(jobs):
        worker = Worker(queue, repo)
        worker.start()
        workers.append(worker)

    descriptions = {}  # type: Dict[str, List[Description]]
    for worker in workers:
        worker.join()
        for desc in worker.response:
            if desc.category not in descriptions:
                descriptions[desc.category] = []
            descriptions[desc.category].append(desc)

    for descs in descriptions.values():
        descs.sort()

    return descriptions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Generate a changelog in MD format between given tags. "
        "It fetches all tags and unshallow the git repositore automatically",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="set the script verbosity, could be used multiple",
    )
    parser.add_argument(
        "--output",
        type=argparse.FileType("w"),
        default="-",
        help="output file for changelog",
    )
    parser.add_argument(
        "--repo",
        default="ClickHouse/ClickHouse",
        help="a repository to query for pull-requests from GitHub",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=10,
        help="number of jobs to get pull-requests info from GitHub API",
    )
    parser.add_argument(
        "--gh-user-or-token",
        help="user name or GH token to authenticate",
    )
    parser.add_argument(
        "--gh-password",
        help="a password that should be used when user is given",
    )
    parser.add_argument(
        "--with-testing-tags",
        action="store_true",
        help="by default '*-testing' tags are ignored, this argument enables them too",
    )
    parser.add_argument(
        "--from",
        dest="from_ref",
        help="git ref for a starting point of changelog, by default is calculated "
        "automatically to match a previous tag in history",
    )
    parser.add_argument(
        "to_ref",
        metavar="TO_REF",
        help="git ref for the changelog end",
    )
    args = parser.parse_args()
    return args


# This function mirrors the PR description checks in ClickhousePullRequestTrigger.
# Returns False if the PR should not be mentioned changelog.
def generate_description(item: PullRequest, repo: Repository) -> Optional[Description]:
    backport_number = item.number
    if item.head.ref.startswith("backport/"):
        branch_parts = item.head.ref.split("/")
        if len(branch_parts) == 3:
            try:
                item = get_pull_cached(repo, int(branch_parts[-1]))
            except Exception as e:
                logging.warning("unable to get backpoted PR, exception: %s", e)
        else:
            logging.warning(
                "The branch %s doesn't match backport template, using PR %s as is",
                item.head.ref,
                item.number,
            )
    description = item.body
    # Don't skip empty lines because they delimit parts of description
    lines = [x.strip() for x in (description.split("\n") if description else [])]
    lines = [re.sub(r"\s+", " ", ln) for ln in lines]

    category = ""
    entry = ""

    if lines:
        i = 0
        while i < len(lines):
            if re.match(r"(?i)^[#>*_ ]*change\s*log\s*category", lines[i]):
                i += 1
                if i >= len(lines):
                    break
                # Can have one empty line between header and the category itself.
                # Filter it out.
                if not lines[i]:
                    i += 1
                    if i >= len(lines):
                        break
                category = re.sub(r"^[-*\s]*", "", lines[i])
                i += 1
            elif re.match(
                r"(?i)^[#>*_ ]*(short\s*description|change\s*log\s*entry)", lines[i]
            ):
                i += 1
                # Can have one empty line between header and the entry itself.
                # Filter it out.
                if i < len(lines) and not lines[i]:
                    i += 1
                # All following lines until empty one are the changelog entry.
                entry_lines = []
                while i < len(lines) and lines[i]:
                    entry_lines.append(lines[i])
                    i += 1
                entry = " ".join(entry_lines)
            else:
                i += 1

    if not category:
        # Shouldn't happen, because description check in CI should catch such PRs.
        # Fall through, so that it shows up in output and the user can fix it.
        category = "NO CL CATEGORY"

    # Filter out the PR categories that are not for changelog.
    if re.match(
        r"(?i)((non|in|not|un)[-\s]*significant)|(not[ ]*for[ ]*changelog)",
        category,
    ):
        category = "NOT FOR CHANGELOG / INSIGNIFICANT"
        return Description(item.number, item.user, item.html_url, item.title, category)

    # Filter out documentations changelog
    if re.match(
        r"(?i)doc",
        category,
    ):
        return None

    if backport_number != item.number:
        entry = f"Backported in #{backport_number}: {entry}"

    if not entry:
        # Shouldn't happen, because description check in CI should catch such PRs.
        category = "NO CL ENTRY"
        entry = "NO CL ENTRY:  '" + item.title + "'"

    entry = entry.strip()
    if entry[-1] != ".":
        entry += "."

    for c in categories_preferred_order:
        if ratio(category.lower(), c.lower()) >= 90:
            category = c
            break

    return Description(item.number, item.user, item.html_url, entry, category)


def write_changelog(fd: TextIO, descriptions: Dict[str, List[Description]]):
    year = date.today().year
    fd.write(
        f"---\nsidebar_position: 1\nsidebar_label: {year}\n---\n\n# {year} Changelog\n\n"
        f"### ClickHouse release {TO_REF} FIXME as compared to {FROM_REF}\n\n"
    )

    seen_categories = []  # type: List[str]
    for category in categories_preferred_order:
        if category in descriptions:
            seen_categories.append(category)
            fd.write(f"#### {category}\n")
            for desc in descriptions[category]:
                fd.write(f"{desc.formatted_entry}\n")

            fd.write("\n")

    for category in sorted(descriptions):
        if category not in seen_categories:
            fd.write(f"#### {category}\n\n")
            for desc in descriptions[category]:
                fd.write(f"{desc.formatted_entry}\n")

            fd.write("\n")


def check_refs(from_ref: Optional[str], to_ref: str, with_testing_tags: bool):
    global FROM_REF, TO_REF
    TO_REF = to_ref

    # Check TO_REF
    runner.run(f"git rev-parse {TO_REF}")

    # Check from_ref
    if from_ref is None:
        # Get all tags pointing to TO_REF
        tags = runner.run(f"git tag --points-at '{TO_REF}^{{}}'").split("\n")
        logging.info("All tags pointing to %s:\n%s", TO_REF, tags)
        if not with_testing_tags:
            tags.append("*-testing")
        exclude = " ".join([f"--exclude='{tag}'" for tag in tags])
        cmd = f"git describe --abbrev=0 --tags {exclude} '{TO_REF}'"
        FROM_REF = runner.run(cmd)
    else:
        runner.run(f"git rev-parse {FROM_REF}")
        FROM_REF = from_ref


def set_sha_in_changelog():
    global SHA_IN_CHANGELOG
    SHA_IN_CHANGELOG = runner.run(
        f"git log --format=format:%H {FROM_REF}..{TO_REF}"
    ).split("\n")


def main():
    log_levels = [logging.CRITICAL, logging.WARN, logging.INFO, logging.DEBUG]
    args = parse_args()
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s [%(filename)s:%(lineno)d]:\n%(message)s",
        level=log_levels[min(args.verbose, 3)],
    )
    # Create a cache directory
    if not p.isdir(CACHE_PATH):
        os.mkdir(CACHE_PATH, 0o700)

    # Get the full repo
    if is_shallow():
        logging.info("Unshallow repository")
        runner.run("git fetch --unshallow", stderr=DEVNULL)
    logging.info("Fetching all tags")
    runner.run("git fetch --tags", stderr=DEVNULL)

    check_refs(args.from_ref, args.to_ref, args.with_testing_tags)
    set_sha_in_changelog()

    logging.info("Using %s..%s as changelog interval", FROM_REF, TO_REF)

    # Get starting and ending dates for gathering PRs
    # Add one day after and before to mitigate TZ possible issues
    # `tag^{}` format gives commit ref when we have annotated tags
    # format %cs gives a committer date, works better for cherry-picked commits
    from_date = runner.run(f"git log -1 --format=format:%cs '{FROM_REF}^{{}}'")
    from_date = (date.fromisoformat(from_date) - timedelta(1)).isoformat()
    to_date = runner.run(f"git log -1 --format=format:%cs '{TO_REF}^{{}}'")
    to_date = (date.fromisoformat(to_date) + timedelta(1)).isoformat()

    # Get all PRs for the given time frame
    global GitHub
    GitHub = Github(
        args.gh_user_or_token, args.gh_password, per_page=100, pool_size=args.jobs
    )
    query = f"type:pr repo:{args.repo} is:merged merged:{from_date}..{to_date}"
    repo = GitHub.get_repo(args.repo)
    api_prs = GitHub.search_issues(query=query, sort="created")
    logging.info("Found %s PRs for the query: '%s'", api_prs.totalCount, query)

    issues = []  # type: List[Issue]
    while True:
        try:
            for issue in api_prs:
                issues.append(issue)
            break
        except RateLimitExceededException:
            sleep_on_rate_limit()

    descriptions = get_descriptions(repo, issues, args.jobs)

    write_changelog(args.output, descriptions)


if __name__ == "__main__":
    main()
