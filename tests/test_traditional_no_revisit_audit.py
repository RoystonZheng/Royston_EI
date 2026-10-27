import unittest

from scripts.audit_traditional_route_no_revisit import duplicate_positions


class TraditionalNoRevisitAuditTests(unittest.TestCase):
    def test_duplicate_positions_are_one_based_and_include_every_occurrence(self):
        self.assertEqual(
            duplicate_positions(["a", "b", "c", "b", "a", "a"]),
            {"a": [1, 5, 6], "b": [2, 4]},
        )

    def test_unique_or_empty_sequences_have_no_duplicate_positions(self):
        self.assertEqual(duplicate_positions([]), {})
        self.assertEqual(duplicate_positions(["a", "b", "c"]), {})


if __name__ == "__main__":
    unittest.main()
