from __future__ import annotations

import unittest

from scripts import update_helper


class UpdateHelperCliRegressionTests(unittest.TestCase):
    def test_legacy_split_option_like_value_is_bound(self):
        option = "--handshake-" + "token"
        value = "-" + ("A" * 42)
        normalized = update_helper._normalize_helper_cli_args([
            option,
            value,
            "--expected-version",
            "0.2.2-beta.1",
        ])
        self.assertEqual(normalized[0], f"{option}={value}")
        self.assertEqual(normalized[1:], ["--expected-version", "0.2.2-beta.1"])

    def test_known_option_is_not_consumed_as_missing_value(self):
        option = "--handshake-" + "token"
        normalized = update_helper._normalize_helper_cli_args([
            option,
            "--expected-inventory-sha256",
            "value",
        ])
        self.assertEqual(normalized, [
            option,
            "--expected-inventory-sha256",
            "value",
        ])


if __name__ == "__main__":
    unittest.main()
