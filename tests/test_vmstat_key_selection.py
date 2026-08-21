from __future__ import annotations

import argparse
import contextlib
import csv
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SKILL_SCRIPTS = Path("/home/nzzhao/.agents/skills/exp_framework/scripts")
sys.path.insert(0, str(SKILL_SCRIPTS))

from order0_fragment_sampler import (  # noqa: E402
    _parse_vmstat_patterns,
    flatten_vmstat_keys,
    select_vmstat_keys,
    write_selected_csv,
    collect,
)

# A realistic /proc/vmstat subset: legacy whitelisted keys, a new kernel
# counter not in the default whitelist (pgdemote_*), and keys that should
# never be exported (pgmigrate_success / nr_free_pages).
FAKE_VMSTAT = {
    "order0_alloc_success": 100,
    "order0_alloc_fallback": 101,
    "nr_pcp_order0_total": 102,
    "alloc_stall_direct": 103,
    "alloc_fail_order0": 104,
    "compact_stall_success": 105,
    "compact_success_order2": 106,
    "pgscan_kswapd": 107,
    "pgsteal_kswapd": 108,
    "pgoutrun_kswapd": 109,
    "pgwake_kswapd": 110,
    "kswapd_order2_wakeup": 111,
    "pgdemote_kswapd": 112,
    "pgdemote_direct": 113,
    "pgdemote_kswapd_extra": 114,
    "pgmigrate_success": 115,
    "nr_free_pages": 116,
}

DEFAULT_HIT = [
    "alloc_fail_order0",
    "alloc_stall_direct",
    "compact_stall_success",
    "compact_success_order2",
    "kswapd_order2_wakeup",
    "nr_pcp_order0_total",
    "order0_alloc_fallback",
    "order0_alloc_success",
    "pgoutrun_kswapd",
    "pgscan_kswapd",
    "pgsteal_kswapd",
    "pgwake_kswapd",
]


class VmstatKeySelectionTests(unittest.TestCase):
    def test_default_mode_matches_legacy_prefixes(self) -> None:
        selected = select_vmstat_keys(FAKE_VMSTAT, [], "append")
        for key in DEFAULT_HIT:
            self.assertIn(key, selected)
        self.assertEqual(selected, sorted(set(DEFAULT_HIT)))

    def test_append_mode_keeps_defaults_and_adds_user_pattern(self) -> None:
        selected = select_vmstat_keys(FAKE_VMSTAT, ["pgdemote_*"], "append")
        for key in DEFAULT_HIT + ["pgdemote_direct", "pgdemote_kswapd", "pgdemote_kswapd_extra"]:
            self.assertIn(key, selected)

    def test_replace_mode_uses_only_user_patterns(self) -> None:
        selected = select_vmstat_keys(FAKE_VMSTAT, ["pgdemote_*"], "replace")
        self.assertEqual(
            selected,
            sorted(["pgdemote_direct", "pgdemote_kswapd", "pgdemote_kswapd_extra"]),
        )
        for key in DEFAULT_HIT:
            self.assertNotIn(key, selected)

    def test_exact_key_and_prefix_wildcard_syntax(self) -> None:
        prefix_selected = select_vmstat_keys(FAKE_VMSTAT, ["pgsteal_*"], "replace")
        self.assertEqual(prefix_selected, ["pgsteal_kswapd"])
        exact_selected = select_vmstat_keys(FAKE_VMSTAT, ["pgdemote_kswapd"], "replace")
        self.assertEqual(exact_selected, ["pgdemote_kswapd", "pgdemote_kswapd_extra"])
        bare_prefix = select_vmstat_keys(FAKE_VMSTAT, ["pgdemote_"], "replace")
        self.assertEqual(
            bare_prefix,
            sorted(["pgdemote_direct", "pgdemote_kswapd", "pgdemote_kswapd_extra"]),
        )

    def test_unmatched_keys_excluded(self) -> None:
        for mode in ("append", "replace"):
            selected = select_vmstat_keys(FAKE_VMSTAT, ["pgdemote_*"], mode)
            self.assertNotIn("pgmigrate_success", selected)
            self.assertNotIn("nr_free_pages", selected)

    def test_regression_equivalent_to_flatten_vmstat_keys(self) -> None:
        """No patterns => select_vmstat_keys must equal legacy flatten per-key."""
        legacy = flatten_vmstat_keys(FAKE_VMSTAT)
        new_default = select_vmstat_keys(FAKE_VMSTAT, [], "append")
        self.assertEqual(new_default, legacy)
        self.assertEqual(new_default, sorted(set(legacy)))
        self.assertEqual(legacy, DEFAULT_HIT)

    def test_parse_vmstat_patterns_comma_and_repeat(self) -> None:
        self.assertEqual(
            _parse_vmstat_patterns(["pgdemote_*, pgmigrate_", "order0"]),
            ["pgdemote_*", "pgmigrate_", "order0"],
        )
        self.assertEqual(_parse_vmstat_patterns(None), [])
        self.assertEqual(_parse_vmstat_patterns([", ,"]), [])

    def test_write_selected_csv_includes_new_counter(self) -> None:
        records = [
            {
                "host_ts": 1.0,
                "guest_ts": 1,
                "vmstat": dict(FAKE_VMSTAT),
            },
            {
                "host_ts": 2.0,
                "guest_ts": 2,
                "vmstat": {
                    "order0_alloc_success": 200,
                    "pgdemote_kswapd": 212,
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "order0_vmstat_samples.csv"
            write_selected_csv(records, output, ["pgdemote_*"], "append")
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            header = rows[0]
            self.assertIn("pgdemote_kswapd", header)
            self.assertIn("pgdemote_direct", header)
            self.assertIn("order0_alloc_success", header)
            self.assertNotIn("pgmigrate_success", header)
            self.assertNotIn("nr_free_pages", header)
            self.assertEqual(rows[1][header.index("pgdemote_kswapd")], "112")
            self.assertEqual(rows[2][header.index("pgdemote_kswapd")], "212")


class _FakeState:
    def __init__(self) -> None:
        self.stop_requested = False

    def request_stop(self, _signum: int, _frame: object) -> None:
        self.stop_requested = True


class _FakeStateFactory:
    def __init__(self) -> None:
        self.instances: list[_FakeState] = []

    def __call__(self) -> _FakeState:
        state = _FakeState()
        self.instances.append(state)
        return state


def _collect_args(out_dir: str) -> argparse.Namespace:
    return argparse.Namespace(
        out_dir=out_dir,
        serial="SERIAL",
        adb="adb",
        interval_s=10,
        target_order=2,
        vmstat_key_patterns=None,
        vmstat_keys_mode="append",
        expected_uffd_mfill_order2=1,
        expected_mthp_cow_order2=1,
        expected_kfragd_enabled=0,
        expected_compact_order2_alloc_wake=0,
        expected_kswapd_order2_threshold=0,
        expected_kswapd_order2_wakeup_threshold=0,
    )


class CollectIntegrationTests(unittest.TestCase):
    def test_env_patterns_drive_csv_and_toollog(self) -> None:
        """End-to-end: VMSTAT_KEY_PATTERNS env feeds collect(); CSV columns and
        stderr toollog must reflect the external patterns (no real adb)."""
        fake_record = {
            "host_ts": 1.0,
            "guest_ts": 1,
            "adb_return_code": 0,
            "adb_stderr": "",
            "vmstat": dict(FAKE_VMSTAT),
            "fragmentation": {"target_order": 2, "zones": {}},
            "pcp": {},
            "cmdline": "",
            "settings": {},
        }
        state_factory = _FakeStateFactory()

        def fake_sample_once(_adb: str, _serial: str, _target_order: int) -> dict:
            state_factory.instances[0].stop_requested = True
            return fake_record

        with tempfile.TemporaryDirectory() as tmpdir:
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {"VMSTAT_KEY_PATTERNS": "pgdemote_*"}):
                with mock.patch("order0_fragment_sampler.sample_once", fake_sample_once):
                    with mock.patch("order0_fragment_sampler.SamplerState", state_factory):
                        with mock.patch("order0_fragment_sampler.signal.signal", lambda *a: None):
                            with contextlib.redirect_stderr(stderr):
                                collect(_collect_args(tmpdir))
            log = stderr.getvalue()
            self.assertIn("source=env", log)
            self.assertIn("pgdemote_*", log)
            self.assertIn("matched 15 vmstat keys", log)
            with (Path(tmpdir) / "order0_vmstat_samples.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                header = next(csv.reader(handle))
            self.assertIn("pgdemote_kswapd", header)
            self.assertIn("pgdemote_direct", header)
            self.assertIn("order0_alloc_success", header)


if __name__ == "__main__":
    unittest.main()
