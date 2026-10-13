"""Launcher selection tests using isolated fake interpreters; no GUI or USB."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


LAUNCHER = Path(__file__).resolve().parents[1] / "run.sh"
ISOLATED_VARIABLES = (
    "PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH",
    "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_FONTDIR",
)


@unittest.skipUnless(os.name == "posix", "launcher requires Bash and POSIX tools")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omnipicker launcher ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tool_dir = self.root / "tool with spaces"
        self.tool_dir.mkdir()
        self.launcher = self.tool_dir / "run.sh"
        self.app = self.tool_dir / "omnipicker_serial.py"
        self.app.write_text("raise AssertionError('fake interpreter must intercept app')\n")
        (self.tool_dir / "requirements.txt").write_text("PyQt5\npyserial\n")
        self.work_dir = self.root / "unrelated working directory"
        self.work_dir.mkdir()
        self.bin_dir = self.root / "current environment bin"
        self.bin_dir.mkdir()
        self.utilities = self.root / "shell utilities"
        self.utilities.mkdir()
        # A private PATH permits testing missing python3 without invoking any
        # real interpreter chosen by the launcher.
        for name in ("bash", "cat", "dirname", "env", "readlink"):
            executable = shutil.which(name, path=os.defpath)
            if executable is None:
                self.skipTest(f"required shell utility unavailable: {name}")
            (self.utilities / name).symlink_to(executable)
        self.log = self.root / "calls.jsonl"
        self.helper = self.root / "record fake interpreter.py"
        self.helper.write_text(textwrap.dedent("""\
            import json
            from pathlib import Path
            import sys

            log_path, mode, app_exit, interpreter, cwd = sys.argv[1:6]
            keys = ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH",
                    "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_FONTDIR")
            captured = sys.argv[6:6 + 2 * len(keys)]
            environment = {
                name: (captured[2 * index + 1] if captured[2 * index] else None)
                for index, name in enumerate(keys)
            }
            args = sys.argv[6 + 2 * len(keys):]
            checking = bool(args and args[0] == "-c")
            record = {"interpreter": interpreter, "cwd": cwd, "argv": args,
                      "phase": "check" if checking else "app",
                      "environment": environment}
            with Path(log_path).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\\n")
            if checking:
                if mode == "bad" or (mode == "clean" and
                                     any(value is not None for value in environment.values())):
                    print("fixture dependency failure: missing pyserial", file=sys.stderr)
                    sys.exit(1)
                sys.exit(0)
            sys.exit(int(app_exit))
            """))
        self.fallbacks = [self.root / "fallback one python", self.root / "fallback two python"]
        self.write_launcher()
        self.environment = os.environ.copy()
        for name in (*ISOLATED_VARIABLES, "OMNIPICKER_PYTHON"):
            self.environment.pop(name, None)
        self.environment["PATH"] = os.pathsep.join((str(self.bin_dir), str(self.utilities)))

    def write_launcher(self, fallbacks=None):
        paths = self.fallbacks if fallbacks is None else fallbacks
        original = "for interpreter in /usr/bin/python3 /usr/local/bin/python3; do"
        source = LAUNCHER.read_text()
        self.assertEqual(source.count(original), 1,
                         "adjust fixture fallback replacement if the launcher changes")
        # Only the temporary copy is patched; the production launcher is read-only.
        replacement = "for interpreter in " + " ".join(shlex.quote(str(path)) for path in paths) + "; do"
        self.launcher.write_text(source.replace(original, replacement))
        self.launcher.chmod(0o755)

    def fake_python(self, path, mode="good", app_exit=0):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        command = ["/usr/bin/env", "-u", "PYTHONPATH", "-u", "PYTHONHOME",
                   sys.executable, str(self.helper), str(self.log), mode, str(app_exit)]
        # Bash captures the incoming variables before clearing Python's variables
        # for the stdlib-only recorder, so even invalid PYTHONHOME is safe to test.
        script = "#!/bin/bash\nexec " + " ".join(shlex.quote(arg) for arg in command)
        script += ' "${BASH_SOURCE[0]}" "$PWD"'
        for name in ISOLATED_VARIABLES:
            script += ' "${' + name + '+x}" "${' + name + '-}"'
        script += ' "$@"\n'
        path.write_text(script)
        path.chmod(0o755)
        return path

    def current_python(self, mode="good", app_exit=0):
        return self.fake_python(self.bin_dir / "python3", mode, app_exit)

    def run_launcher(self, *args, environment=None):
        env = self.environment.copy()
        if environment:
            env.update(environment)
        return subprocess.run([str(self.launcher), *args], cwd=self.work_dir,
                              env=env, text=True, capture_output=True, timeout=10)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_launched(self, result, interpreter, args=(), app_exit=0):
        self.assertEqual(result.returncode, app_exit, result.stderr)
        calls = self.calls()
        apps = [call for call in calls if call["phase"] == "app"]
        self.assertEqual(len(apps), 1, calls)
        self.assertEqual(apps[0]["interpreter"], str(interpreter))
        self.assertEqual(apps[0]["argv"], [str(self.app), *args])
        self.assertTrue(all(call["cwd"] == str(self.tool_dir) for call in calls))
        return apps[0]

    def assert_failed_without_app(self, result, diagnostic):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(diagnostic, result.stderr)
        self.assertFalse(any(call["phase"] == "app" for call in self.calls()))

    def test_explicit_python_preserves_arguments_cwd_and_exit_status(self):
        explicit = self.fake_python(self.root / "chosen interpreter with spaces", app_exit=23)
        self.current_python()
        self.fake_python(self.tool_dir / ".venv/bin/python")
        self.fake_python(self.fallbacks[0])
        args = ("--port", "a port with spaces", "中文参数", "", "$(never-run)")
        result = self.run_launcher(*args, environment={"OMNIPICKER_PYTHON": str(explicit)})
        self.assert_launched(result, explicit, args, app_exit=23)
        self.assertEqual([call["interpreter"] for call in self.calls()], [str(explicit)] * 2)

    def test_explicit_command_name_is_resolved_from_path(self):
        explicit = self.fake_python(self.bin_dir / "my-python")
        self.current_python()
        result = self.run_launcher(environment={"OMNIPICKER_PYTHON": "my-python"})
        self.assert_launched(result, explicit)
        self.assertEqual(len(self.calls()), 2)

    def test_bad_explicit_python_refuses_healthy_venv_current_and_fallback(self):
        explicit = self.fake_python(self.root / "requested bad python", "bad")
        self.fake_python(self.tool_dir / ".venv/bin/python")
        self.current_python()
        self.fake_python(self.fallbacks[0])
        result = self.run_launcher(environment={"OMNIPICKER_PYTHON": str(explicit)})
        self.assert_failed_without_app(result, "指定的 Python 依赖检查失败")
        self.assertIn("fixture dependency failure", result.stderr)
        self.assertIn(str(explicit), result.stderr)
        self.assertIn("pip install -r", result.stderr)
        self.assertEqual([call["interpreter"] for call in self.calls()], [str(explicit)])

    def test_missing_or_nonexecutable_explicit_python_never_falls_back(self):
        self.current_python()
        self.fake_python(self.fallbacks[0])
        nonexecutable = self.root / "not executable"
        nonexecutable.write_text("not a Python executable\n")
        for path in (self.root / "missing interpreter", nonexecutable):
            with self.subTest(path=path):
                result = self.run_launcher(environment={"OMNIPICKER_PYTHON": str(path)})
                self.assert_failed_without_app(result, "指定的 Python 不存在或不可执行")
                self.assertEqual(self.calls(), [])

    def test_local_venv_preferred_to_healthy_current_and_fallback(self):
        venv = self.fake_python(self.tool_dir / ".venv/bin/python")
        self.current_python()
        self.fake_python(self.fallbacks[0])
        result = self.run_launcher("--example")
        self.assert_launched(result, venv, ("--example",))
        self.assertEqual([call["interpreter"] for call in self.calls()], [str(venv)] * 2)

    def test_bad_local_venv_refuses_healthy_current_and_fallback(self):
        venv = self.fake_python(self.tool_dir / ".venv/bin/python", "bad")
        self.current_python()
        self.fake_python(self.fallbacks[0])
        result = self.run_launcher()
        self.assert_failed_without_app(result, "工具虚拟环境依赖检查失败")
        self.assertIn("pip install -r", result.stderr)
        self.assertEqual([call["interpreter"] for call in self.calls()], [str(venv)])

    def test_incomplete_local_venv_refuses_fallback_and_explains_rebuild(self):
        (self.tool_dir / ".venv").mkdir()
        self.current_python()
        self.fake_python(self.fallbacks[0])
        result = self.run_launcher()
        self.assert_failed_without_app(result, "工具虚拟环境不完整")
        self.assertIn("python3 -m venv", result.stderr)
        self.assertIn("pip install -r", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_healthy_current_python_is_checked_and_launched_with_same_environment(self):
        current = self.current_python()
        self.fake_python(self.fallbacks[0])
        contaminated = {name: f"original {name} value" for name in ISOLATED_VARIABLES}
        result = self.run_launcher(environment=contaminated)
        app = self.assert_launched(result, current)
        self.assertEqual([call["interpreter"] for call in self.calls()], [str(current)] * 2)
        self.assertEqual(self.calls()[0]["environment"], contaminated)
        self.assertEqual(app["environment"], contaminated)
        self.assertEqual(result.stderr, "")

    def test_bad_current_retries_fallbacks_and_cleans_check_and_app_environment(self):
        current = self.current_python("bad")
        self.fake_python(self.fallbacks[0], "bad")
        fallback = self.fake_python(self.fallbacks[1], "clean")
        contaminated = {name: f"poison {name} value" for name in ISOLATED_VARIABLES}
        result = self.run_launcher("--port", "fake port", environment=contaminated)
        self.assert_launched(result, fallback, ("--port", "fake port"))
        calls = self.calls()
        self.assertEqual([call["interpreter"] for call in calls],
                         [str(current), str(self.fallbacks[0]), str(fallback), str(fallback)])
        self.assertEqual(calls[0]["environment"], contaminated)
        for call in calls[1:]:
            self.assertEqual(call["environment"], dict.fromkeys(ISOLATED_VARIABLES))
        self.assertIn("当前 Python 依赖不可用", result.stderr)
        self.assertIn(str(fallback), result.stderr)

    def test_same_current_binary_can_be_retried_with_clean_environment(self):
        current = self.current_python("clean")
        self.write_launcher([current, self.fallbacks[1]])
        result = self.run_launcher(environment={"PYTHONHOME": "invalid Python home"})
        self.assert_launched(result, current)
        calls = self.calls()
        self.assertEqual([call["phase"] for call in calls], ["check", "check", "app"])
        self.assertEqual(calls[0]["environment"]["PYTHONHOME"], "invalid Python home")
        self.assertIsNone(calls[1]["environment"]["PYTHONHOME"])
        self.assertIsNone(calls[2]["environment"]["PYTHONHOME"])

    def test_missing_path_python_can_use_clean_fallback(self):
        fallback = self.fake_python(self.fallbacks[0], "clean")
        result = self.run_launcher(environment={"PYTHONPATH": "invalid modules"})
        self.assert_launched(result, fallback)
        self.assertEqual(len(self.calls()), 2)
        self.assertTrue(all(call["environment"]["PYTHONPATH"] is None for call in self.calls()))

    def test_all_bad_environments_report_installation_instructions(self):
        current = self.current_python("bad")
        for path in self.fallbacks:
            self.fake_python(path, "bad")
        result = self.run_launcher()
        self.assert_failed_without_app(result, "没有找到同时具备 PyQt5 和完整 pyserial")
        self.assertEqual([call["interpreter"] for call in self.calls()],
                         [str(current), *map(str, self.fallbacks)])
        self.assertIn("串口依赖包名为 pyserial", result.stderr)
        self.assertIn("python3 -m venv", result.stderr)
        self.assertIn("-m pip install -r", result.stderr)
        # Guidance must be shell-copyable despite spaces in tool/interpreter paths.
        commands = [line.strip() for line in result.stderr.splitlines() if line.startswith("  ")]
        self.assertIn(["python3", "-m", "venv", str(self.tool_dir / ".venv")],
                      [shlex.split(command) for command in commands])
        self.assertIn([str(self.tool_dir / ".venv/bin/python"), "-m", "pip", "install", "-r",
                       str(self.tool_dir / "requirements.txt")],
                      [shlex.split(command) for command in commands])

    def test_equivalent_fallback_paths_are_only_checked_once(self):
        self.current_python("bad")
        self.fake_python(self.fallbacks[0], "bad")
        self.fallbacks[1].symlink_to(self.fallbacks[0])
        result = self.run_launcher()
        self.assert_failed_without_app(result, "没有找到同时具备 PyQt5 和完整 pyserial")
        self.assertEqual([call["interpreter"] for call in self.calls()],
                         [str(self.bin_dir / "python3"), str(self.fallbacks[0])])


if __name__ == "__main__":
    unittest.main()
