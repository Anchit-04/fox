import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loom"))

import git_context
import permissions as p
from approval import ApprovalGate, CLIApprover


def decide(command, mode=p.WORKSPACE_WRITE):
    return p.classify("run_bash_command", {"command": command}, mode)


class GitReadsRunWithoutAsking(unittest.TestCase):
    def test_reads(self):
        for command in ("git status", "git log --oneline -5", "git --no-pager diff", "git branch", "git branch -a",
                        "git branch --show-current", "git branch --contains abc", "git remote -v",
                        "git remote get-url origin", "git stash list", "git tag", "git config --get user.name",
                        "git rev-parse HEAD", "git ls-files"):
            self.assertEqual(decide(command).action, p.ALLOW, command)

    def test_changes_ask(self):
        for command in ("git branch feature", "git branch -m old new", "git remote add up x", "git stash",
                        "git tag v1", "git config user.name x", "git commit -m x", "git push origin main",
                        "git diff --output=patch.txt", "git restore --staged a.py"):
            self.assertEqual(decide(command).action, p.ASK, command)


class GhReadsRunWithoutAsking(unittest.TestCase):
    def test_reads(self):
        for command in ("gh pr view 12", "gh pr list", "gh pr diff 12", "gh pr checks", "gh run view 5 --log-failed",
                        "gh issue view 3", "gh repo view", "gh auth status", "gh api repos/a/b/pulls",
                        "gh api -X GET repos/a/b", "gh search code foo", "gh --version"):
            self.assertEqual(decide(command).action, p.ALLOW, command)

    def test_changes_ask(self):
        for command in ("gh pr create --title x --body y", "gh pr merge 3", "gh pr comment 3 -b hi",
                        "gh issue create -t x", "gh api repos/a/b/issues -f title=x", "gh api -XPOST repos/a/b",
                        "gh api --method=PATCH repos/a/b", "gh workflow run ci.yml", "gh repo clone a/b"):
            self.assertEqual(decide(command).action, p.ASK, command)


class DestructiveCommandsAskEveryTime(unittest.TestCase):
    DANGEROUS = ("git push --force", "git push -f origin x", "git push -fu origin x",
                 "git push --force-with-lease", "git push origin +main", "git push origin :old",
                 "git push --delete origin old", "git reset --hard HEAD~1", "git clean -fd", "git branch -D x",
                 "git checkout -- .", "git checkout .", "git restore a.py", "git stash drop",
                 "git commit -m x --no-verify", "git commit -n -m x", "git -C sub reset --hard",
                 "gh repo delete a/b --yes", "gh release delete v1", "gh api -X DELETE repos/a/b",
                 "git add . && git push --force", "git status & git reset --hard")

    def test_full_access_still_asks(self):
        for command in self.DANGEROUS:
            decision = decide(command, p.FULL_ACCESS)
            self.assertEqual(decision.action, p.ASK, command)
            self.assertTrue(decision.every_time, command)

    def test_read_only_refuses(self):
        for command in self.DANGEROUS:
            self.assertEqual(decide(command, p.READ_ONLY).action, p.DENY, command)

    def test_ordinary_work_is_not_flagged(self):
        for command in ("git push origin feature", "git push -u origin feature", "git commit -m 'fix'",
                        "git branch -d merged", "git restore --staged a.py", "git checkout main",
                        "gh pr merge 3 --squash", "git stash"):
            self.assertIsNone(p.destructive(command), command)
            self.assertFalse(decide(command, p.FULL_ACCESS).every_time, command)

    def test_the_reason_names_what_is_lost(self):
        self.assertIn("throws away uncommitted work", decide("git reset --hard").reason)


class AlwaysDoesNotCoverDestructive(unittest.TestCase):
    def ask(self, gate, every_time, answer_always):
        events = []

        def emit(kind, payload):
            events.append(payload)
            gate.respond(payload["request_id"], True, always=answer_always, tool_name=payload["tool_name"])

        gate._emit = emit
        granted = gate.approve("run_bash_command", {"command": "x"}, "why", every_time=every_time)
        return granted, events

    def test_gate_always_is_ignored_for_every_time_requests(self):
        gate = ApprovalGate()
        granted, events = self.ask(gate, every_time=True, answer_always=True)
        self.assertTrue(granted)
        self.assertEqual(len(events), 1)
        _, events = self.ask(gate, every_time=True, answer_always=False)
        self.assertEqual(len(events), 1)

    def test_gate_earlier_always_does_not_skip_every_time(self):
        gate = ApprovalGate()
        self.ask(gate, every_time=False, answer_always=True)
        _, events = self.ask(gate, every_time=False, answer_always=False)
        self.assertEqual(events, [])
        _, events = self.ask(gate, every_time=True, answer_always=False)
        self.assertEqual(len(events), 1)

    def test_cli_always_is_ignored_for_every_time_requests(self):
        cli = CLIApprover()
        with mock.patch("builtins.input", return_value="a"), mock.patch("builtins.print"):
            self.assertTrue(cli.approve("run_bash_command", {"command": "x"}, "why", every_time=True))
        self.assertNotIn("run_bash_command", cli._always)


class QuietEnvironment(unittest.TestCase):
    def test_prompts_and_pagers_are_off(self):
        env = git_context.quiet_env()
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_EDITOR"], ":")
        self.assertEqual(env["GH_PROMPT_DISABLED"], "1")
        self.assertEqual(env["GH_PAGER"], "")

    def test_a_commit_without_a_message_fails_instead_of_waiting(self):
        with tempfile.TemporaryDirectory() as d:
            def git(*args):
                return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=d,
                                      env=git_context.quiet_env(), capture_output=True, text=True, timeout=20)
            git("init", "-q")
            Path(d, "a.txt").write_text("x")
            git("add", "a.txt")
            done = git("commit")
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("empty commit message", done.stderr)

    def test_sandboxd_gets_the_quiet_environment(self):
        from sandbox import sandbox_client
        with mock.patch.object(sandbox_client.subprocess, "Popen") as popen, \
                mock.patch.object(sandbox_client._SANDBOXD_PATH.__class__, "exists", return_value=True):
            sandbox_client._start()
        self.assertEqual(popen.call_args.kwargs["env"]["GIT_EDITOR"], ":")


class WorkspaceSnapshot(unittest.TestCase):
    def setUp(self):
        git_context.forget()
        self.addCleanup(git_context.forget)

    def test_not_a_repository(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIn("not a git repository", git_context.snapshot(d))

    def test_a_repository_with_a_remote(self):
        with tempfile.TemporaryDirectory() as d:
            run = lambda *a: subprocess.run(["git", *a], cwd=d, capture_output=True, timeout=20)
            run("init", "-q")
            run("remote", "add", "origin", "https://github.com/x/y.git")
            with mock.patch.object(git_context, "_gh_account", return_value="`gh` is signed in to github.com as x."):
                text = git_context.snapshot(d)
        self.assertIn("Remotes: origin https://github.com/x/y.git", text)
        self.assertIn("signed in to github.com as x", text)
        self.assertIn("run `git status`", text)

    def test_gh_missing(self):
        with mock.patch.object(git_context.shutil, "which", return_value=None):
            self.assertIn("not installed", git_context._gh_account(Path(".")))

    def test_gh_signed_out(self):
        done = subprocess.CompletedProcess([], 1, "", "You are not logged into any GitHub hosts.")
        with mock.patch.object(git_context.shutil, "which", return_value="gh"), \
                mock.patch.object(git_context.subprocess, "run", return_value=done):
            self.assertIn("gh auth login", git_context._gh_account(Path(".")))

    def test_the_note_is_built_once_per_workspace(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(git_context, "snapshot", return_value="- snap") as snap:
            first = git_context.prompt_note(d)
            second = git_context.prompt_note(d)
        self.assertEqual(first, second)
        self.assertEqual(snap.call_count, 1)
        self.assertIn("## Git and GitHub", first)

    def test_both_prompts_carry_the_note(self):
        import agent
        from orchestration import orchestrator
        with mock.patch.object(git_context, "prompt_note", return_value="\nGIT-NOTE\n"):
            self.assertIn("GIT-NOTE", agent.build_system_prompt("m"))
            run = SimpleNamespace(model_key="m", mode=p.WORKSPACE_WRITE)
            self.assertIn("GIT-NOTE", orchestrator._system_prompt(run))


if __name__ == "__main__":
    unittest.main()
