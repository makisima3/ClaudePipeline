"""Диагностика Git: заглушки команд, никаких сетевых запросов и реального push."""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("git_readiness", SOURCE / "server/git_readiness.py")
gr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gr)


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.config = {"user.name": "Имя", "user.email": "test@example.invalid",
                       "branch.main.remote": "origin", "branch.main.merge": "refs/heads/main"}
        self.url = "git@github.com:owner/project.git"
        self.branch = "main"
        self.remotes = "origin"
        self.gh_code = 0
        self.git_exists = True
        self.helper = "!gh auth git-credential"
        self.repository = True
        self.calls = []
        self.patcher = patch.object(gr.subprocess, "run", side_effect=self.command)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def command(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0] == "gh":
            self.assertEqual(args[1:4], ("auth", "status", "--active"))
            self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
            return subprocess.CompletedProcess(args, self.gh_code)
        self.assertEqual(args[0], "git")
        if not self.git_exists:
            raise FileNotFoundError()
        cmd = args[1:]
        code, out = 0, ""
        if cmd == ("--version",):
            out = "git version test"
        elif cmd == ("rev-parse", "--is-inside-work-tree"):
            code, out = (0, "true") if self.repository else (128, "")
        elif cmd == ("symbolic-ref", "--quiet", "--short", "HEAD"):
            code, out = (0, self.branch) if self.branch else (1, "")
        elif cmd == ("remote",):
            out = self.remotes
        elif cmd[:4] == ("remote", "get-url", "--push", "--all"):
            out = self.url
        elif cmd[0] == "config" and ("--get" in cmd or "--get-all" in cmd):
            code, out = (0, self.config[cmd[-1]]) if cmd[-1] in self.config else (1, "")
        elif cmd[:3] == ("config", "--get-urlmatch", "credential.helper"):
            code, out = (0, self.helper) if self.helper else (1, "")
        else:
            self.fail(f"Непредусмотренная команда проверки: {args}")
        return subprocess.CompletedProcess(args, code, stdout=out)

    def result(self, **kwargs):
        return gr.check("project", **kwargs)

    def check_item(self, result, key):
        return next(c for c in result["checks"] if c["key"] == key)

    def test_off_does_not_run_commands(self):
        self.assertEqual(self.result(mode="off")["status"], "local_ready")
        self.assertFalse(self.calls)

    def test_commit_needs_no_remote_or_gh(self):
        self.remotes = ""
        self.branch = ""
        r = self.result(mode="commit", auth="gh")
        self.assertEqual(r["status"], "local_ready")
        self.assertFalse(any(a[0] == "gh" or a[1] == "remote" for a, _ in self.calls))

    def test_existing_ssh_and_https_need_no_gh(self):
        for url in (self.url, "https://github.com/owner/project.git", "https://gitlab.com/owner/project.git"):
            with self.subTest(url=url):
                self.url = url
                self.calls.clear()
                r = self.result()
                self.assertEqual(r["status"], "local_ready")
                self.assertEqual(r["write_access"], "not_checked")
                self.assertEqual((r["remote"], r["target_branch"]), ("origin", "main"))
                self.assertFalse(any(a[0] == "gh" for a, _ in self.calls))

    def test_missing_git_repository_and_identity(self):
        self.git_exists = False
        self.assertEqual(self.result()["status"], "needs_setup")
        self.git_exists, self.repository = True, False
        self.assertEqual(self.check_item(self.result(), "repository")["status"], "error")
        self.repository = True
        for key in ("user.name", "user.email"):
            self.config.pop(key)
        r = self.result()
        self.assertIn("git config user.name", self.check_item(r, "author_name")["message"])
        self.assertEqual(self.check_item(r, "author_email")["status"], "error")

    def test_detached_missing_remote_and_missing_upstream(self):
        self.branch = ""
        self.assertEqual(self.check_item(self.result(), "branch")["status"], "error")
        self.branch, self.remotes = "main", ""
        self.assertEqual(self.check_item(self.result(), "remote")["status"], "error")
        self.remotes = "origin"
        self.config.pop("branch.main.merge")
        self.assertEqual(self.check_item(self.result(), "target")["status"], "error")
        self.config["push.autoSetupRemote"] = "true"
        self.assertEqual(self.result()["status"], "local_ready")

    def test_remote_priority_and_pushurl(self):
        self.remotes = "origin\nfork\nbackup"
        self.config["remote.pushDefault"] = "fork"
        self.assertEqual(self.result()["remote"], "fork")
        self.config["branch.main.pushRemote"] = "backup"
        r = self.result()
        self.assertEqual(r["remote"], "backup")
        self.assertIn(("git", "remote", "get-url", "--push", "--all", "backup"), [a for a, _ in self.calls])

    def test_single_remote_without_tracking_and_auto_setup(self):
        self.remotes = "fork"
        self.config.pop("branch.main.remote")
        self.config.pop("branch.main.merge")
        r = self.result()
        self.assertEqual(r["remote"], "fork")
        self.assertEqual(self.check_item(r, "target")["status"], "error")
        self.config["push.autoSetupRemote"] = "true"
        for mode in ("simple", "current", "upstream"):
            self.config["push.default"] = mode
            self.assertEqual(self.result()["status"], "local_ready")

    def test_current_upstream_mismatch_and_special_push_rules(self):
        self.config["branch.main.merge"] = "refs/heads/release"
        self.assertEqual(self.check_item(self.result(), "target")["status"], "error")
        self.config["push.default"] = "upstream"
        self.assertEqual(self.result()["target_branch"], "release")
        self.config["push.default"] = "current"
        self.assertEqual(self.result()["target_branch"], "main")
        for key, value in (("remote.origin.push", "+refs/heads/*:refs/heads/*"), ("remote.origin.mirror", "true")):
            self.config[key] = value
            self.assertEqual(self.check_item(self.result(), "target")["status"], "warning")
            self.config.pop(key)
        for mode in ("matching", "nothing"):
            self.config["push.default"] = mode
            self.assertEqual(self.result()["status"], "review")

    def test_gh_selected_only_for_https_and_no_secret_output(self):
        self.url = "https://user:SECRET_TOKEN@github.com/owner/project.git?secret=ANOTHER_SECRET"
        r = self.result(auth="gh")
        self.assertNotIn("SECRET", json.dumps(r))
        self.assertEqual(r["status"], "local_ready")
        self.assertIn(("gh", "auth", "status", "--active", "--hostname", "github.com"), [a for a, _ in self.calls])
        self.helper = "manager"
        self.assertEqual(self.check_item(self.result(auth="gh"), "gh_git")["status"], "warning")
        self.helper = "!gh auth git-credential"
        self.gh_code = 1
        self.assertEqual(self.result(auth="gh")["status"], "review")
        self.url = "git@github.com:owner/project.git"
        self.calls.clear()
        self.result(auth="gh")
        self.assertFalse(any(a[0] == "gh" for a, _ in self.calls))

    def test_missing_gh_timeout_and_config_error_hide_raw_output(self):
        self.url = "https://github.com/owner/project.git"
        original = self.command

        def missing_gh(args, **kwargs):
            if args[0] == "gh":
                raise FileNotFoundError()
            return original(args, **kwargs)

        with patch.object(gr.subprocess, "run", side_effect=missing_gh):
            self.assertEqual(self.result(auth="gh")["status"], "needs_setup")
        with patch.object(gr.subprocess, "run", side_effect=subprocess.TimeoutExpired("SECRET", 8, output="SECRET")):
            r = self.result()
            self.assertEqual(r["status"], "needs_setup")
            self.assertNotIn("SECRET", json.dumps(r))
        with patch.object(gr.subprocess, "run", return_value=subprocess.CompletedProcess([], 128, stdout="SECRET", stderr="SECRET")):
            self.assertNotIn("SECRET", json.dumps(self.result()))

    def test_multiple_pushurls_and_url_remote_need_review(self):
        self.url += "\nhttps://example.com/owner/project.git"
        self.assertEqual(self.result(auth="gh")["status"], "review")
        self.config["branch.main.pushRemote"] = "https://user:SECRET@example.com/p.git"
        r = self.result()
        self.assertEqual(r["status"], "needs_setup")
        self.assertNotIn("SECRET", json.dumps(r))


class LocalGitTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "Нужен Git для проверки локального репозитория")
    def test_unborn_branch_existing_config_and_read_only(self):
        with tempfile.TemporaryDirectory(prefix="git-readiness-") as folder:
            root = Path(folder)
            env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
            with patch.dict(os.environ, env):
                def git(*args):
                    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)
                git("init", "-b", "main")
                git("config", "user.name", "Тест")
                git("config", "user.email", "test@example.invalid")
                git("remote", "add", "origin", "git@github.com:owner/project.git")
                before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                r = gr.check(root)
                self.assertEqual(r["branch"], "main")
                self.assertEqual(r["status"], "needs_setup")
                after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                self.assertEqual(before, after)
                git("config", "push.default", "current")
                self.assertEqual(gr.check(root)["status"], "local_ready")


if __name__ == "__main__":
    unittest.main()
