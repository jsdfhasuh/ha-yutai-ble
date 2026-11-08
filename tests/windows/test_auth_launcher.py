"""Run the Windows launchers with isolated, offline dependency fixtures."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]

SETUP_FIXTURE = r"""
import os
from pathlib import Path
import subprocess
import sys
import venv

root = Path(__file__).resolve().parent
attempt_file = root / "setup_attempts.txt"
seed = sys.argv[1:] == ["seed"]
attempt = 0 if seed else (int(attempt_file.read_text()) if attempt_file.exists() else 0) + 1
if not seed:
    attempt_file.write_text(str(attempt))

interpreter = root / ".venv" / "Scripts" / "python.exe"
if not interpreter.exists():
    venv.EnvBuilder(with_pip=False, system_site_packages=False).create(root / ".venv")
if not seed and attempt == 1 and os.environ.get("YUTAI_TEST_FAIL_FIRST") == "1":
    print("Simulated dependency installation failure.")
    sys.exit(17)

site_packages = subprocess.check_output(
    [str(interpreter), "-I", "-X", "utf8", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
    text=True,
    encoding="utf-8",
).strip()
package = Path(site_packages) / "aiohttp"
package.mkdir(parents=True, exist_ok=True)
(package / "__init__.py").write_text("# Offline dependency fixture.\n")
(package / "web.py").write_text("# Offline dependency fixture.\n")
"""

APPLICATION_FIXTURE = r"""
import os
from pathlib import Path
import sys

import aiohttp
from aiohttp import web

with Path("application_runs.txt").open("a", encoding="utf-8") as output:
    output.write("started\n")
sys.exit(int(os.environ["YUTAI_TEST_APP_EXIT"]))
"""


@unittest.skipUnless(os.name == "nt", "requires a real Windows cmd.exe")
class AuthLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="yutai auth launcher ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        for name in ("run.cmd", "start_qr.cmd"):
            shutil.copyfile(ROOT / "tools" / "auth" / name, self.directory / name)
        (self.directory / "setup_fixture.py").write_text(textwrap.dedent(SETUP_FIXTURE), encoding="utf-8")
        (self.directory / "simple_qr.py").write_text(textwrap.dedent(APPLICATION_FIXTURE), encoding="utf-8")
        (self.directory / "setup.cmd").write_text(
            '@echo off\n"%YUTAI_TEST_PYTHON%" "%~dp0setup_fixture.py"\nexit /b %errorlevel%\n',
            encoding="utf-8",
            newline="\r\n",
        )
        self.environment = os.environ.copy()
        # The real venv must not inherit third-party dependencies from the runner.
        self.environment.pop("PYTHONPATH", None)
        self.environment.pop("PYTHONHOME", None)
        self.environment["YUTAI_TEST_PYTHON"] = sys.executable
        self.environment["PYTHONUTF8"] = "1"

    def run_launcher(self, *, app_exit=23, fail_first=False):
        environment = {
            **self.environment,
            "YUTAI_TEST_APP_EXIT": str(app_exit),
            "YUTAI_TEST_FAIL_FIRST": "1" if fail_first else "0",
        }
        return subprocess.run(
            [environment.get("COMSPEC", "cmd.exe"), "/d", "/c", "call", str(self.directory / "start_qr.cmd")],
            cwd=self.directory.parent,
            env=environment,
            # Supply input to the existing pause commands without an interactive console.
            input="\n" * 4,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )

    def seed_dependency(self):
        subprocess.run(
            [sys.executable, str(self.directory / "setup_fixture.py"), "seed"],
            env=self.environment,
            check=True,
            capture_output=True,
            timeout=45,
        )

    def test_complete_environment_skips_setup_and_preserves_exit_code(self):
        self.seed_dependency()
        for app_exit in (0, 23):
            with self.subTest(app_exit=app_exit):
                result = self.run_launcher(app_exit=app_exit)
                self.assertEqual(result.returncode, app_exit, result.stdout + result.stderr)
                self.assertFalse((self.directory / "setup_attempts.txt").exists())
        self.assertEqual((self.directory / "application_runs.txt").read_text().splitlines(), ["started", "started"])

    def test_failed_install_is_retried_on_next_launch(self):
        first = self.run_launcher(fail_first=True)
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertTrue((self.directory / ".venv" / "Scripts" / "python.exe").is_file())
        self.assertEqual((self.directory / "setup_attempts.txt").read_text(), "1")
        self.assertFalse((self.directory / "application_runs.txt").exists())

        second = self.run_launcher(app_exit=23, fail_first=True)
        self.assertEqual(second.returncode, 23, second.stdout + second.stderr)
        self.assertEqual((self.directory / "setup_attempts.txt").read_text(), "2")
        self.assertEqual((self.directory / "application_runs.txt").read_text().splitlines(), ["started"])

        third = self.run_launcher(app_exit=37, fail_first=True)
        self.assertEqual(third.returncode, 37, third.stdout + third.stderr)
        self.assertEqual((self.directory / "setup_attempts.txt").read_text(), "2")
        self.assertEqual((self.directory / "application_runs.txt").read_text().splitlines(), ["started", "started"])


if __name__ == "__main__":
    unittest.main()
