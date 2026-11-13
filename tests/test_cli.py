import contextlib
import io
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from netdoctor.cli import main
from netdoctor.models import RunResult


class CliTests(unittest.TestCase):
    def test_plan_describes_all_metrics_without_measuring(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(["plan"])
        text = output.getvalue()
        self.assertEqual(exit_code, 0)
        for label in ("DNS", "TCP", "TTFB", "다운로드", "RTT"):
            self.assertIn(label, text)
        self.assertIn("네트워크 요청을 보내지 않습니다", text)

    def test_demo_json_is_valid(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(["demo", "--json"])
        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["mode"], "demo")

    def test_measure_requires_an_explicit_config(self) -> None:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            exit_code = main(["measure", "--quiet"])
        self.assertEqual(exit_code, 2)
        self.assertIn("authorized to test", errors.getvalue())

    def test_measure_json_uses_measurement_result(self) -> None:
        measured = replace(RunResult.demo("Example", "example.com", 5), mode="measured")
        output = io.StringIO()
        with patch("netdoctor.cli.run_diagnostics", return_value=measured):
            with contextlib.redirect_stdout(output):
                exit_code = main(
                    ["--config", "config/default.json", "measure", "--json", "--quiet"]
                )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "measured")


if __name__ == "__main__":
    unittest.main()
