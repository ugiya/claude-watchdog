"""Public release guards use synthetic data and never touch user sessions."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.check_public import inspect


class PublicReleaseTests(unittest.TestCase):
    def test_version_command_has_no_runtime_side_effects(self):
        root = Path(__file__).resolve().parent
        result = subprocess.run([sys.executable, str(root / "claude-watchdog"), "--version"],
                                check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "claude-watchdog " + (root / "VERSION").read_text().strip())

    def test_private_content_is_rejected_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = "gh" + "p_" + "a" * 36
            (root / "README.md").write_text(secret + " /" + "Users" + "/person/project/", encoding="utf-8")
            findings = inspect(root)
        self.assertTrue(any("credential" in line or "token" in line for line in findings))
        self.assertTrue(any("home path" in line for line in findings))
        self.assertNotIn(secret, "\n".join(findings))

    def test_session_artifacts_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "private.jsonl").write_text("{}", encoding="utf-8")
            self.assertTrue(inspect(root))

    def test_runtime_and_credential_named_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("watchdog.log", "lineage.json", ".env", ".env.local"):
                (root / name).write_text("synthetic\n", encoding="utf-8")
            findings = inspect(root)
        rendered = "\n".join(findings)
        self.assertIn("watchdog.log: private/runtime artifact path", rendered)
        self.assertIn("lineage.json: private/runtime artifact path", rendered)
        self.assertIn(".env: credential file name", rendered)
        self.assertIn(".env.local: credential file name", rendered)

    def test_aws_credentials_are_rejected_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            access_id = "AK" + "IA" + "A" * 16
            secret_name = "AWS_SECRET" + "_ACCESS_KEY"
            secret_value = "b" * 40
            (root / "settings.txt").write_text(
                f"access={access_id}\n{secret_name}={secret_value}\n",
                encoding="utf-8",
            )
            findings = inspect(root)
        rendered = "\n".join(findings)
        self.assertIn("AWS access key ID", rendered)
        self.assertIn("AWS secret access key assignment", rendered)
        self.assertNotIn(access_id, rendered)
        self.assertNotIn(secret_value, rendered)


if __name__ == "__main__":
    unittest.main()
