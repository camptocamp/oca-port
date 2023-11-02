# Copyright 2022 Camptocamp SA
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl)

import os
import shutil
import tempfile
import uuid
from collections import defaultdict

import click
import git

from .utils import git as g, misc
from .utils.misc import Output, bcolors as bc

AUTHOR_EMAILS_TO_SKIP = [
    "transbot@odoo-community.org",
    "noreply@weblate.org",
    "oca-git-bot@odoo-community.org",
    "oca+oca-travis@odoo-community.org",
    "oca-ci@odoo-community.org",
    "shopinvader-git-bot@shopinvader.com",
]

SUMMARY_TERMS_TO_SKIP = [
    "Translated using Weblate",
    "Added translation using Weblate",
]

PR_BRANCH_NAME = "oca-port-from-{from_branch}-to-{to_branch}-{slug}"

FOLDERS_TO_SKIP = [
    "setup",
    ".github",
]

FILES_TO_KEEP = [
    "requirements.txt",
    "test-requirements.txt",
    "oca_dependencies.txt",
]

# Fake PR for commits w/o any PR (used as fallback)
FAKE_PR = g.PullRequest(*[""] * 6)


def path_to_skip(commit_path):
    """Return True if the commit path should not be ported."""
    # Allows all folders (addons!) excepted those like 'setup/' generated
    # automatically by pre-commit.
    if commit_path.isdir:
        return commit_path in FOLDERS_TO_SKIP
    # Forbid all files excepted those that developers could update
    return commit_path not in FILES_TO_KEEP


class PortAddonPullRequest(Output):
    def __init__(self, app, create_branch=True, push_branch=True):
        """Port pull requests of an addon."""
        self.app = app
        self.create_branch = create_branch
        self.push_branch = push_branch
        self._results = {"process": "port_commits", "results": {}}

    def run(self):
        if not self.app.check_addon_exists_to_branch():
            if self.app.non_interactive:
                if self.app.output:
                    return False, self._render_output(self.app.output, {})
            return False, None
        dest_branch = self.app._eval_dest_branch()
        self._print(
            f"{bc.BOLD}{self.app.addon}{bc.END} already exists "
            f"on {bc.BOLD}{dest_branch.ref()}{bc.END}, "
            "checking PRs to port..."
        )
        branches_diff = BranchesDiff(self.app)
        branches_diff.print_diff(self.app.verbose)
        if self.app.non_interactive:
            if branches_diff.commits_diff:
                # If an output is defined we return the result in the expected format
                if self.app.output:
                    self._results["results"] = branches_diff.serialized_diff
                    return True, self._render_output(self.app.output, self._results)
                if self.app.cli:
                    # Exit with an error code if commits are eligible for (back)porting
                    # User-defined exit codes should be defined between 64 and 113.
                    # Allocate 110 for 'PortAddonPullRequest'.
                    raise SystemExit(110)
                return True, None
            if self.app.output:
                # Nothing to port -> return an empty output
                return False, self._render_output(self.app.output, {})
            return False, None
        # Print empty line
        self._print()
        if not self.app.dry_run:
            self._port_pull_requests(branches_diff)
        return True, None

    def _port_pull_requests(self, branches_diff):
        """Open new Pull Requests (if it doesn't exist) on the GitHub repository."""
        # Create a temporary development branch to stack all ported commits.
        # This branch will be renamed to the expected destination branch at the
        # end of the process before the user decides to push its work.
        dev_branch = self._get_dev_branch()
        processed_prs = []
        last_pr = (
            list(branches_diff.commits_diff.keys())[-1]
            if branches_diff.commits_diff
            else None
        )
        current_commit = self.app.repo.commit(self.app.to_branch.ref())
        for pr, commits in branches_diff.commits_diff.items():
            pr_processed = self._port_pull_request_commits(pr, commits, dev_branch)
            # Check if commits have been ported.
            # If none has been ported, blacklist automatically the current PR.
            no_new_commit = self.app.repo.commit(dev_branch.ref()) == current_commit
            if pr_processed:
                if no_new_commit:
                    self._blacklist_pr(pr)
                    continue
                processed_prs.append(pr)
                is_last = pr == last_pr
                if is_last:
                    self._print("\t🎉 Last PR processed! 🎉")
        if not pr_processed:
            self._print("⚠️  Nothing has been ported")
        dest_branch = self._create_dest_branch(dev_branch, processed_prs)
        is_pushed = self._push_branch_to_remote(dest_branch)
        if not is_pushed:
            return
        pr_data = self._prepare_pull_request_data(processed_prs, dest_branch)
        pr_url = self._search_pull_request(pr_data["base"], pr_data["title"])
        if pr_url:
            self._print(f"\tExisting PR has been refreshed => {pr_url}")
        else:
            self._create_pull_request(dest_branch, pr_data, processed_prs)

    def _get_dev_branch(self):
        """Return the development branch to stack ported commits.

        Create one if needed.
        """
        # Stay on the current branch if coming from MigrateAddon
        if not self.create_branch:
            branch_name = self.app.repo.active_branch.name
            if self.app.verbose:
                self._print(
                    f"Starting from existing branch {bc.BOLD}{branch_name}{bc.END}...\n"
                )
            return g.Branch(self.app.repo, branch_name)
        # Create one on the fly
        slug = "dev-" + str(uuid.uuid4()).split("-")[0]
        branch_name = PR_BRANCH_NAME.format(
            from_branch=self.app.from_branch.name,
            to_branch=self.app.to_branch.name,
            slug=slug,
        )
        base_ref = self.app.to_branch.ref()
        branch = g.Branch(self.app.repo, branch_name, base_ref=base_ref)
        if self.app.verbose:
            self._print(
                f"Creating development branch {bc.BOLD}{branch.name}{bc.END} "
                f"from {base_ref}...\n"
            )
        branch.checkout(create=True)
        return branch

    def _create_dest_branch(self, dev_branch, processed_prs):
        """Create the destination branch.

        If a destination branch name is not provided, one will be created based
        on the processed PRs.
        """
        branch_name = self.app.destination.branch
        if not branch_name:
            pr_numbers = [str(pr.number) for pr in processed_prs]
            slug = "-".join(pr_numbers) or "misc"
            branch_name = PR_BRANCH_NAME.format(
                from_branch=self.app.from_branch.name,
                to_branch=self.app.to_branch.name,
                slug=slug,
            )
        if branch_name in self.app.repo.heads:
            msg = (
                f"Branch {bc.BOLD}{branch_name}{bc.END} already exists. "
                "Validate to override existing branch or change its name.\n"
            )
            new_branch_name = click.prompt(msg, default=branch_name)
            if new_branch_name == branch_name:
                self.app.repo.delete_head(branch_name, "-f")
            branch_name = new_branch_name
        git_branch = self.app.repo.heads[dev_branch.ref()]
        git_branch.rename(branch_name)
        self.app.destination["branch"] = branch_name
        dest_branch = self.app._prepare_branch(self.app.destination)
        return dest_branch

    def _port_pull_request_commits(self, pr, commits, dev_branch):
        """Port commits of a Pull Request in a new branch."""
        if pr.number:
            self._print(
                f"- {bc.BOLD}{bc.OKCYAN}Port PR {pr.ref}{bc.END} "
                f"{bc.OKCYAN}{pr.title}{bc.ENDC}..."
            )
            self._print(f"\t{pr.url}")
        else:
            self._print(f"- {bc.BOLD}{bc.OKCYAN}Port commits w/o PR{bc.END}...")
        # Ask the user if he wants to port the PR (or orphaned commits)
        if not click.confirm("\tPort it?" if pr.number else "\tPort them?"):
            self.app.storage.blacklist_pr(pr.ref, confirm=True)
            if not self.app.storage.dirty:
                # PR hasn't been blacklisted
                return False
        # If the PR has been blacklisted we need to commit this information
        if self.app.storage.dirty:
            self.app.storage.commit()
            return True
        # Cherry-pick commits of the source PR
        for commit in commits:
            self._print(
                f"\t\tApply {bc.OKCYAN}{commit.hexsha[:8]}{bc.ENDC} "
                f"{commit.summary}..."
            )
            # Port only relevant diffs/paths from the commit
            paths_to_port = set(commit.paths_to_port)
            for diff in commit.diffs:
                skip, message = self._skip_diff(commit, diff)
                if skip:
                    if message:
                        self._print(f"\t\t\t{message}")
                    if diff.a_path in paths_to_port:
                        paths_to_port.remove(diff.a_path)
                    if diff.b_path in paths_to_port:
                        paths_to_port.remove(diff.b_path)
                    continue
            if not paths_to_port:
                self._print("\t\t\tℹ️  Nothing to port from this commit, skipping")
                continue
            try:
                patches_dir = tempfile.mkdtemp()
                self.app.repo.git.format_patch(
                    "--keep-subject",
                    "-o",
                    patches_dir,
                    "-1",
                    commit.hexsha,
                    "--",
                    *paths_to_port,
                )
                patches = [
                    os.path.join(patches_dir, f)
                    for f in sorted(os.listdir(patches_dir))
                ]
                self.app.repo.git.am("-3", "--keep", *patches)
                shutil.rmtree(patches_dir)
            except git.exc.GitCommandError as exc:
                self._print(f"{bc.FAIL}ERROR:{bc.ENDC}\n{exc}\n")
                # High chance a conflict occurs, ask the user to resolve it
                if not click.confirm(
                    "⚠️  A conflict occurs, please resolve it and "
                    "confirm to continue the process (y) or skip this commit (N)."
                ):
                    self.app.repo.git.am("--abort")
                    continue
        return True

    def _blacklist_pr(self, pr):
        self._print("\tℹ️  Nothing has been ported, skipping")
        # Do not ask to blacklist if already blacklisted.
        # This might happen when a migration process has been interrupted brutally.
        if not self.app.storage.is_pr_blacklisted(pr.ref):
            self.app.storage.blacklist_pr(
                pr.ref,
                confirm=True,
                reason=f"(auto) Nothing to port from PR #{pr.ref}",
            )
            if self.app.storage.dirty:
                self.app.storage.commit()
            msg = (
                f"\t{bc.DIM}PR #{pr.number} has been"
                if pr.number
                else "Orphaned commits have been"
            ) + f" automatically blacklisted{bc.ENDD}"
            self._print(msg)

    @staticmethod
    def _skip_diff(commit, diff):
        """Check if a commit diff should be skipped or not.

        A skipped diff won't have its file path ported through 'git format-path'.

        Return a tuple `(bool, message)` if the diff is skipped.
        """
        if diff.deleted_file:
            if diff.a_path not in commit.paths_to_port:
                return True, ""
        if diff.b_path not in commit.paths_to_port:
            return True, ""
        if diff.renamed:
            return False, ""
        diff_path = diff.b_path.split("/", maxsplit=1)[0]
        # Do not accept diff on unported addons
        if (
            not misc.get_manifest_path(diff_path)
            and diff_path not in commit.addons_created
        ):
            return (
                True,
                (
                    f"{bc.WARNING}SKIP diff "
                    f"{bc.BOLD}{diff.change_type} {diff.b_path}{bc.END}: "
                    "relates to an unported addon"
                ),
            )
        if diff.change_type in ("M", "D"):
            # Do not accept update and deletion on non-existing files
            if not os.path.exists(diff.b_path):
                return (
                    True,
                    (
                        f"SKIP: '{diff.change_type} {diff.b_path}' diff relates "
                        "to a non-existing file"
                    ),
                )
        return False, ""

    def _push_branch_to_remote(self, branch, is_last=False):
        """Force push the local branch to remote destination fork."""
        if not self.push_branch or self.app.push_only_when_done and not is_last:
            return False
        confirm = (
            f"Push branch '{bc.BOLD}{branch.name}{bc.END}' "
            f"to remote '{bc.BOLD}{self.app.destination.remote}{bc.END}'?"
        )
        if click.confirm(confirm):
            branch.repo.git.push(
                self.app.destination.remote, branch.name, "--force-with-lease"
            )
            branch.remote = self.app.destination.remote
            return True
        return False

    def _prepare_pull_request_data(self, processed_prs, pr_branch):
        if len(processed_prs) > 1:
            title = (
                f"[{self.app.to_branch.name}][FW] {self.app.addon}: multiple ports "
                f"from {self.app.from_branch.name}"
            )
            lines = [f"- #{pr.number}" for pr in processed_prs]
            body = "\n".join(
                [
                    f"Port of the following PRs from {self.app.from_branch.name} "
                    f"to {self.app.to_branch.name}:"
                ]
                + lines
            )
        else:
            pr = processed_prs[0]
            title = f"[{self.app.to_branch.name}][FW] {pr.title}"
            body = (
                f"Port of #{pr.number} from {self.app.from_branch.name} "
                f"to {self.app.to_branch.name}."
            )
        return {
            "draft": True,
            "title": title,
            "head": f"{self.app.destination.org}:{pr_branch.name}",
            "base": self.app.to_branch.name,
            "body": body,
        }

    def _search_pull_request(self, base_branch, title):
        params = {
            "q": (
                f"is:pr "
                f"repo:{self.app.source.org}/{self.app.repo_name} "
                f"base:{base_branch} "
                f"state:open {title} in:title"
            ),
        }
        response = self.app.github.request("search/issues", params=params)
        if response["items"]:
            return response["items"][0]["html_url"]

    def _create_pull_request(self, pr_branch, pr_data, processed_prs):
        if len(processed_prs) > 1:
            self._print(
                "PR(s) ported locally:",
                ", ".join(
                    [f"{bc.OKCYAN}#{pr.number}{bc.ENDC}" for pr in processed_prs]
                ),
            )
        if click.confirm(
            f"Create a draft PR from '{bc.BOLD}{pr_branch.name}{bc.END}' "
            f"to '{bc.BOLD}{self.app.to_branch.name}{bc.END}' "
            f"against {bc.BOLD}{self.app.source.org}/{self.app.repo_name}{bc.END}?"
        ):
            response = self.app.github.request(
                f"repos/{self.app.source.org}/{self.app.repo_name}/pulls",
                method="post",
                json=pr_data,
            )
            pr_url = response["html_url"]
            self._print(
                f"\t{bc.BOLD}{bc.OKCYAN}PR created =>" f"{bc.ENDC} {pr_url}{bc.END}"
            )
            return pr_url


class BranchesDiff(Output):
    """Helper to compare easily commits (and related PRs) between two branches."""

    def __init__(self, app):
        self.app = app
        self.path = self.app.addon
        self.from_branch_path_commits, _ = self._get_branch_commits(
            self.app.from_branch.ref(), self.path
        )
        self.from_branch_all_commits, _ = self._get_branch_commits(
            self.app.from_branch.ref()
        )
        dest_branch = self.app._eval_dest_branch()
        self.dest_branch_path_commits, _ = self._get_branch_commits(
            dest_branch.ref(), self.path
        )
        self.dest_branch_all_commits, _ = self._get_branch_commits(dest_branch.ref())
        self.commits_diff = self.get_commits_diff()
        self.serialized_diff = self._serialize_diff(self.commits_diff)

    def _serialize_diff(self, commits_diff):
        data = {}
        for pr, commits in commits_diff.items():
            data[pr.number] = pr.to_dict()
            data[pr.number]["missing_commits"] = [commit.hexsha for commit in commits]
        return data

    def _get_branch_commits(self, branch, path="."):
        """Get commits from the local repository for the given `branch`.

        An optional `path` parameter can be set to limit commits to a given folder.
        This function also filters out undesirable commits (merge or translation
        commits...).

        Return two data structures:
            - a list of Commit objects `[Commit, ...]`
            - a dict of Commits objects grouped by SHA `{SHA: Commit, ...}`
        """
        commits = self.app.repo.iter_commits(branch, paths=path)
        commits_list = []
        commits_by_sha = {}
        for commit in commits:
            if self.app.cache.is_commit_ported(commit.hexsha):
                continue
            com = g.Commit(commit)
            if self._skip_commit(com):
                continue
            commits_list.append(com)
            commits_by_sha[commit.hexsha] = com
        # Put ancestors at the beginning of the list to loop with
        # the expected order
        commits_list.reverse()
        return commits_list, commits_by_sha

    @staticmethod
    def _skip_commit(commit):
        """Check if a commit should be skipped or not.

        Merge or translations commits are skipped for instance, or commits
        updating only files/folders we do not want to port (pre-commit
        configuration, setuptools files...).
        """
        return (
            # Skip merge commit
            len(commit.parents) > 1
            or commit.author_email in AUTHOR_EMAILS_TO_SKIP
            or any([term in commit.summary for term in SUMMARY_TERMS_TO_SKIP])
            or all(path_to_skip(path) for path in commit.paths)
        )

    def print_diff(self, verbose=False):
        lines_to_print = [""]
        fake_pr = None
        i = 0
        for i, pr in enumerate(self.commits_diff, 1):
            if pr.number:
                lines_to_print.append(
                    f"{i}) {bc.BOLD}{bc.OKBLUE}{pr.ref}{bc.END} "
                    f"{bc.OKBLUE}{pr.title}{bc.ENDC}:"
                )
                lines_to_print.append(f"\tBy {pr.author}, merged at {pr.merged_at}")
            else:
                lines_to_print.append(f"{i}) {bc.BOLD}{bc.OKBLUE}w/o PR{bc.END}:")
                fake_pr = pr
            if verbose:
                pr_paths = ", ".join([f"{bc.DIM}{path}{bc.ENDD}" for path in pr.paths])
                lines_to_print.append(f"\t=> Updates: {pr_paths}")
            if pr.number:
                pr_paths_not_ported = ", ".join(
                    [f"{bc.OKBLUE}{path}{bc.ENDC}" for path in pr.paths_not_ported]
                )
                lines_to_print.append(f"\t=> Not ported: {pr_paths_not_ported}")
            lines_to_print.append(
                f"\t=> {bc.BOLD}{bc.OKBLUE}{len(self.commits_diff[pr])} "
                f"commit(s){bc.END} not (fully) ported"
            )
            if pr.number:
                lines_to_print.append(f"\t=> {pr.url}")
            if verbose or not pr.number:
                for commit in self.commits_diff[pr]:
                    lines_to_print.append(
                        f"\t\t{bc.DIM}{commit.hexsha[:8]} " f"{commit.summary}{bc.ENDD}"
                    )
        if fake_pr:
            # We have commits without PR, adapt the message
            i -= 1
            nb_commits = len(self.commits_diff[fake_pr])
            message = (
                f"{bc.BOLD}{bc.OKBLUE}{i} pull request(s){bc.END} "
                f"and {bc.BOLD}{bc.OKBLUE}{nb_commits} commit(s) w/o "
                f"PR{bc.END} related to '{bc.OKBLUE}{self.path}"
                f"{bc.ENDC}' to port from {self.app.from_branch.ref()} "
                f"to {self.app.dest_branch.ref()}"
            )
        else:
            message = (
                f"{bc.BOLD}{bc.OKBLUE}{i} pull request(s){bc.END} "
                f"related to '{bc.OKBLUE}{self.path}{bc.ENDC}' to port from "
                f"{self.app.from_branch.ref()} to {self.app.dest_branch.ref()}"
            )
        lines_to_print.insert(0, message)
        self._print("\n".join(lines_to_print))

    def get_commits_diff(self):
        """Returns the commits which do not exist in `dest_branch`, grouped by
        their related Pull Request.

        :return: a dict {PullRequest: {Commit: data, ...}, ...}
        """
        commits_by_pr = defaultdict(list)
        for commit in self.from_branch_path_commits:
            if commit in self.dest_branch_all_commits:
                self.app.cache.mark_commit_as_ported(commit.hexsha)
                continue
            # Get related Pull Request if any
            pr = self._get_original_pr(commit)
            if pr:
                for pr_commit_sha in pr.commits:
                    try:
                        raw_commit = self.app.repo.commit(pr_commit_sha)
                    except ValueError:
                        # Ignore commits referenced by a PR but not present
                        # in the stable branches
                        continue
                    pr_commit = g.Commit(raw_commit)
                    if self._skip_commit(pr_commit):
                        continue
                    pr_commit_paths = {
                        path for path in pr_commit.paths if not path_to_skip(path)
                    }
                    pr.paths.update(pr_commit_paths)
                    # Check that this PR commit does not change the current
                    # addon we are interested in, in such case also check
                    # for each updated addons that the commit has already
                    # been ported.
                    # Indeed a commit could have been ported partially
                    # in the past (with git-format-patch), and we now want
                    # to port the remaining chunks.
                    if pr_commit not in self.dest_branch_path_commits:
                        paths = set(pr_commit_paths)
                        # A commit could have been ported several times
                        # if it was impacting several addons and the
                        # migration has been done with git-format-patch
                        # on each addon separately
                        dest_branch_all_commits = self.dest_branch_all_commits[:]
                        skip_pr_commit = False
                        with g.no_strict_commit_equality():
                            while pr_commit in dest_branch_all_commits:
                                index = dest_branch_all_commits.index(pr_commit)
                                ported_commit = dest_branch_all_commits.pop(index)
                                ported_commit_paths = {
                                    path
                                    for path in ported_commit.paths
                                    if not path_to_skip(path)
                                }
                                pr.ported_paths.update(ported_commit_paths)
                                pr_commit.ported_commits.append(ported_commit)
                                paths -= ported_commit_paths
                                if not paths:
                                    # The ported commits have already updated
                                    # the same addons than the original one,
                                    # we can skip it.
                                    skip_pr_commit = True
                        if skip_pr_commit:
                            continue
                    # We want to port commits that were still not ported
                    # for the addon we are interested in.
                    # If the commit has already been included, skip it.
                    if (
                        pr_commit in self.dest_branch_path_commits
                        and pr_commit in self.dest_branch_all_commits
                    ):
                        continue
                    existing_pr_commits = commits_by_pr.get(pr, [])
                    for existing_pr_commit in existing_pr_commits:
                        if (
                            existing_pr_commit == pr_commit
                            and existing_pr_commit.hexsha == pr_commit.hexsha
                        ):
                            # This PR commit has already been appended, skip
                            break
                    else:
                        commits_by_pr[pr].append(pr_commit)
            # No related PR: add the commit to the fake PR
            else:
                commits_by_pr[FAKE_PR].append(commit)
        # Sort PRs on the merge date (better to port them in the right order).
        # Do not return blacklisted PR.
        sorted_commits_by_pr = {}
        for pr in sorted(commits_by_pr, key=lambda pr: pr.merged_at or ""):
            blacklisted = self.app.storage.is_pr_blacklisted(pr.ref)
            if not blacklisted:
                # TODO: Backward compat for old tracking only by number
                blacklisted = self.app.storage.is_pr_blacklisted(pr.number)
            if blacklisted:
                msg = (
                    f"{bc.DIM}PR #{pr.number}" if pr.number else "Orphaned commits"
                ) + f" blacklisted ({blacklisted}){bc.ENDD}"
                self._print(msg)
                continue
            sorted_commits_by_pr[pr] = commits_by_pr[pr]
        return sorted_commits_by_pr

    def _get_original_pr(self, commit: g.Commit):
        """Return the original PR of a given commit."""
        # Try to get the data from the user's cache first
        data = self.app.cache.get_pr_from_commit(commit.hexsha)
        if data:
            return g.PullRequest(**data)
        # Request GitHub to get them
        if not any("github.com" in remote.url for remote in self.app.repo.remotes):
            return
        raw_data = self.app.github.get_original_pr(
            self.app.source.org,
            self.app.repo_name,
            self.app.from_branch.name,
            commit.hexsha,
        )
        if raw_data:
            # Get all commits of the PR as they could update others addons
            # than the one the user is interested in.
            # NOTE: commits fetched from PR are already in the right order
            pr_number = raw_data["number"]
            pr_commits_data = self.app.github.request(
                f"repos/{self.app.source.org}/{self.app.repo_name}"
                f"/pulls/{pr_number}/commits?per_page=100"
            )
            pr_commits = [pr["sha"] for pr in pr_commits_data]
            data = {
                "number": raw_data["number"],
                "url": raw_data["html_url"],
                "author": raw_data["user"].get("login", ""),
                "title": raw_data["title"],
                "body": raw_data["body"],
                "merged_at": raw_data["merged_at"],
                "commits": pr_commits,
            }
            self.app.cache.store_commit_pr(commit.hexsha, data)
            return g.PullRequest(**data)
