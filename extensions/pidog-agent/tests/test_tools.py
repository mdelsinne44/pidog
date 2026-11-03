#!/usr/bin/env python3
"""Vérifie les garde-fous shell + l'exécution de code + l'écriture fichier."""

import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(BASE))
for path in (REPO, BASE):
    if path not in sys.path:
        sys.path.insert(0, path)

import tools


class ShellToolsTest(unittest.TestCase):
    def test_shell_pipe(self):
        result = tools.execute_tool("shell", {"cmd": "printf 'a\\nb\\n' | wc -l"})
        self.assertTrue(result["ok"], result)
        self.assertIn("2", result["stdout"])

    def test_shell_and_chain(self):
        result = tools.execute_tool("shell", {"cmd": "cd ~/pidog && pwd"})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["stdout"].endswith("pidog"))

    def test_sudo_refused(self):
        result = tools.execute_tool("shell", {"cmd": "sudo reboot"})
        self.assertFalse(result["ok"])

    def test_reboot_refused(self):
        result = tools.execute_tool("shell", {"cmd": "reboot"})
        self.assertFalse(result["ok"])

    def test_rm_rf_root_refused(self):
        result = tools.execute_tool("shell", {"cmd": "rm -rf /"})
        self.assertFalse(result["ok"])

    def test_run_python(self):
        result = tools.execute_tool(
            "run_python", {"code": "print(sum(range(5)))"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout"], "10")

    def test_run_python_can_import_repo(self):
        result = tools.execute_tool(
            "run_python", {"code": "import robot_actions; print('ok')"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stdout"], "ok")

    def test_run_python_error_is_reported(self):
        result = tools.execute_tool("run_python", {"code": "1/0"})
        self.assertFalse(result["ok"])
        self.assertIn("ZeroDivisionError", result["stderr"])

    def test_write_file_inside_workspace(self):
        target = os.path.join(BASE, "_tmp_write_test.txt")
        try:
            result = tools.execute_tool(
                "write_file", {"path": target, "content": "hello"})
            self.assertTrue(result["ok"], result)
            with open(target, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "hello")
        finally:
            try:
                os.unlink(target)
            except OSError:
                pass

    def test_write_file_outside_workspace_refused(self):
        result = tools.execute_tool(
            "write_file", {"path": "/etc/pidog-test", "content": "x"})
        self.assertFalse(result["ok"])

    def test_write_file_protects_env_and_data(self):
        for path in (os.path.join(BASE, ".env"),
                     os.path.join(REPO, "data", "memory.db"),
                     os.path.join(REPO, ".git", "config")):
            result = tools.execute_tool(
                "write_file", {"path": path, "content": "x"})
            self.assertFalse(result["ok"], path)


class SchemasTest(unittest.TestCase):
    def test_new_tools_exposed(self):
        names = tools.tool_names()
        for expected in ("shell", "run_python", "write_file",
                         "web_search", "fetch_url"):
            self.assertIn(expected, names)

    def test_every_schema_has_handler(self):
        schema_names = {item["function"]["name"] for item in tools.TOOLS}
        self.assertEqual(schema_names, set(tools.tool_names()))


class ToolContentTest(unittest.TestCase):
    def test_truncation_keeps_valid_json_and_drops_list_items(self):
        import json
        cli_dir = os.path.join(BASE, "cli")
        if cli_dir not in sys.path:
            sys.path.insert(0, cli_dir)
        import agent_cli
        result = {"ok": True,
                  "results": [{"title": "t" * 120, "url": "https://x/" + "u" * 40}
                              for _ in range(10)]}
        blob = agent_cli._tool_content(result, 400)
        self.assertLessEqual(len(blob), 400)
        parsed = json.loads(blob)
        self.assertIn("ok", parsed)


if __name__ == "__main__":
    unittest.main()
