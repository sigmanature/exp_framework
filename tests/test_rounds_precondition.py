"""多轮次 / precondition 编排解析单元测试。"""
from __future__ import annotations

import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
import sys
sys.path.insert(0, str(SRC))

from exp_framework.experiment.config import (resolve_precondition_mode,
                                             resolve_rounds)


class TestResolveRounds(unittest.TestCase):
    def test_default_is_one(self):
        self.assertEqual(resolve_rounds({}), 1)

    def test_config_value(self):
        self.assertEqual(resolve_rounds({"rounds": 3}), 3)

    def test_cli_wins(self):
        self.assertEqual(resolve_rounds({"rounds": 3}, cli_value=5), 5)

    def test_invalid_raises(self):
        for bad in (0, -1, "x", None, True, False, 2.5):
            with self.assertRaises(ValueError, msg=f"rounds={bad!r}"):
                resolve_rounds({"rounds": bad})

    def test_float_integer_accepted(self):
        self.assertEqual(resolve_rounds({"rounds": 3.0}), 3)


class TestResolvePreconditionMode(unittest.TestCase):
    def test_absent_block_disables(self):
        self.assertIsNone(resolve_precondition_mode({}))

    def test_once_and_per_round(self):
        self.assertEqual(resolve_precondition_mode(
            {"precondition": {"mode": "once"}}), "once")
        self.assertEqual(resolve_precondition_mode(
            {"precondition": {"mode": "per_round"}}), "per_round")

    def test_block_without_mode_defaults_once(self):
        self.assertEqual(resolve_precondition_mode({"precondition": {}}), "once")

    def test_mode_none_disables(self):
        self.assertIsNone(resolve_precondition_mode(
            {"precondition": {"mode": "none"}}))

    def test_cli_wins(self):
        self.assertEqual(resolve_precondition_mode(
            {"precondition": {"mode": "per_round"}},
            cli_value="once"), "once")
        self.assertIsNone(resolve_precondition_mode(
            {"precondition": {"mode": "per_round"}}, cli_value="none"))

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            resolve_precondition_mode({"precondition": {"mode": "sometimes"}})


if __name__ == "__main__":
    unittest.main()
