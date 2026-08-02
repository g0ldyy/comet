import unittest

from comet.core.sql_batch import chunk_parameters


class SqlBatchTests(unittest.TestCase):
    def test_shared_values_are_bound_once(self):
        self.assertEqual(
            chunk_parameters(
                [
                    {"scope": "movie", "candidate_id": "a"},
                    {"scope": "movie", "candidate_id": "b"},
                ],
                frozenset({"scope"}),
            ),
            {
                "scope": "movie",
                "candidate_id_0": "a",
                "candidate_id_1": "b",
            },
        )

    def test_empty_mismatched_or_noncanonical_rows_fail_closed(self):
        cases = (
            ([], frozenset()),
            (
                [{"candidate_id": "a"}, {"candidate_id": "b", "scope": "movie"}],
                frozenset(),
            ),
            ([{"candidate-id": "a"}], frozenset()),
            ([{"candidate_id": "a"}], frozenset({"scope"})),
            (
                [
                    {"scope": "movie", "candidate_id": "a"},
                    {"scope": "episode", "candidate_id": "b"},
                ],
                frozenset({"scope"}),
            ),
        )
        for chunk, shared in cases:
            with (
                self.subTest(chunk=chunk, shared=shared),
                self.assertRaises(ValueError),
            ):
                chunk_parameters(chunk, shared)


if __name__ == "__main__":
    unittest.main()
