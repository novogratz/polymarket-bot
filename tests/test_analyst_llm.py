import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "analyst_llm.py"
SPEC = importlib.util.spec_from_file_location("analyst_llm_under_test", MODULE_PATH)
analyst_llm = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(analyst_llm)


class TestAnalystLlm(unittest.TestCase):
    def test_codex_is_first_provider_and_read_only(self) -> None:
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, stdout="hold steady\n", stderr="")

        with mock.patch.dict("os.environ", {"ANALYST_LLM_PROVIDERS": "codex,ollama"}, clear=False):
            with mock.patch.object(analyst_llm.shutil, "which", return_value="/usr/bin/tool"):
                with mock.patch.object(analyst_llm.subprocess, "run", side_effect=fake_run):
                    text = analyst_llm.call_analyst_llm("prompt", cwd=Path("/tmp/repo"))

        self.assertEqual(text, "hold steady")
        self.assertEqual(len(calls), 1)
        cmd, kwargs = calls[0]
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("--ephemeral", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertIn("read-only", cmd)
        self.assertIn("--ask-for-approval", cmd)
        self.assertIn("never", cmd)
        self.assertEqual(kwargs["input"], "prompt")

    def test_falls_back_to_ollama_when_codex_fails(self) -> None:
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["codex", "exec"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no auth")
            return subprocess.CompletedProcess(cmd, 0, stdout="ollama verdict", stderr="")

        with mock.patch.dict("os.environ", {"ANALYST_LLM_PROVIDERS": "codex,ollama"}, clear=False):
            with mock.patch.object(analyst_llm.shutil, "which", return_value="/usr/bin/tool"):
                with mock.patch.object(analyst_llm.subprocess, "run", side_effect=fake_run):
                    text = analyst_llm.call_analyst_llm("prompt", cwd=Path("/tmp/repo"))

        self.assertEqual(text, "ollama verdict")
        self.assertEqual(calls[0][:2], ["codex", "exec"])
        self.assertEqual(calls[1][:2], ["ollama", "run"])

    def test_reports_unavailable_when_all_providers_fail(self) -> None:
        with mock.patch.dict("os.environ", {"ANALYST_LLM_PROVIDERS": "codex,ollama"}, clear=False):
            with mock.patch.object(analyst_llm.shutil, "which", return_value=None):
                text = analyst_llm.call_analyst_llm("prompt", cwd=Path("/tmp/repo"))

        self.assertIn("analyst llm unavailable", text)
        self.assertIn("codex", text)
        self.assertIn("ollama", text)


if __name__ == "__main__":
    unittest.main()
