"""Первое знакомство: отдельная база и новые процессы, без моделей и авторизаций."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_pipeline as fixture


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.saved = fixture.db.get_setting("onboarding_status")
        self.addCleanup(fixture.db.set_setting, "onboarding_status", self.saved)

    def read_in_process(self, root, agent="codex", action=""):
        code = "import db\n" + action + "\nprint(db.get_setting('onboarding_status'))"
        out = subprocess.run([sys.executable, "-c", code], cwd=root / "server",
                             env={**os.environ, "PIPELINE_AGENT": agent}, capture_output=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        return out.stdout.decode().strip()

    def test_new_install_and_existing_database_offer_without_reset(self):
        self.assertEqual(fixture.MIGRATED_SETTINGS["onboarding_status"], "pending")
        with tempfile.TemporaryDirectory(prefix="pipeline-onboarding-") as tmp:
            root = Path(tmp)
            shutil.copytree(fixture.SOURCE / "server", root / "server")
            self.assertEqual(self.read_in_process(root), "pending")
            for status in ("skipped", "completed"):
                with self.subTest(status=status):
                    self.assertEqual(self.read_in_process(root, action=f"db.set_setting('onboarding_status', '{status}')"), status)
                    # Новый процесс заново применяет текущие defaults/миграции, как при обновлении.
                    self.assertEqual(self.read_in_process(root, "claude"), status)
                    self.assertEqual(self.read_in_process(root, "codex"), status)

    def test_save_and_state_use_installation_not_device_or_provider(self):
        for status in ("skipped", "completed"):
            with self.subTest(status=status), patch.object(fixture.pl, "agent_run") as model:
                result = fixture.main.api_settings(None, {"onboarding_status": status}, {})
                self.assertEqual(result["onboarding_status"], status)
                self.assertEqual(fixture.main.api_state(None, {}, {})["settings"]["onboarding_status"], status)
                self.assertEqual(self.read_in_process(fixture.SANDBOX, "claude"), status)
                model.assert_not_called()

    def test_invalid_values_cannot_reset_or_partially_save_settings(self):
        fixture.db.set_setting("onboarding_status", "completed")
        original = fixture.db.get_setting("extra_prompt")
        for status in ("pending", "ready", "", None, True, [], {}):
            with self.subTest(status=json.dumps(status)), self.assertRaises(fixture.main.HttpError) as err:
                fixture.main.api_settings(None, {"extra_prompt": "must-not-save", "onboarding_status": status}, {})
            self.assertEqual(err.exception.code, 400)
            self.assertEqual(fixture.db.get_setting("onboarding_status"), "completed")
            self.assertEqual(fixture.db.get_setting("extra_prompt"), original)


if __name__ == "__main__":
    unittest.main()
