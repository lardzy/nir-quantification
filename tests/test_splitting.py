from __future__ import annotations

import unittest

from nir_quantification.splitting import build_split_definition


def make_record(fabric_id: str, num_components: int) -> dict:
    return {
        "file_path": f"/tmp/{fabric_id}.csv",
        "fabric_id": fabric_id,
        "present_14": [1] * 14,
        "composition_14": [100.0 / 14.0] * 14,
        "num_components": num_components,
    }


class SplittingTests(unittest.TestCase):
    def test_group_split_keeps_fabric_ids_isolated(self) -> None:
        records = []
        for bucket in (1, 2, 3, 4):
            for group_index in range(3):
                fabric_id = f"group_{bucket}_{group_index}"
                records.append(make_record(fabric_id, bucket))
                records.append(make_record(fabric_id, bucket))
        split_definition = build_split_definition(records)
        assignments = split_definition["assignments"]
        self.assertEqual(len(assignments), 12)
        seen = {}
        for record in records:
            assigned = assignments[record["fabric_id"]]
            previous = seen.setdefault(record["fabric_id"], assigned)
            self.assertEqual(previous, assigned)
        for split_name in ("train", "val", "test"):
            self.assertTrue(split_definition["splits"][split_name])

    def test_reports_when_three_way_class_coverage_is_impossible(self) -> None:
        records = [
            {
                **make_record(f"group_{index}", 1),
                "present_14": [1, 0] + [0] * 12,
            }
            for index in range(2)
        ]
        with self.assertRaisesRegex(ValueError, "classes with fewer than 3 independent fabric groups"):
            build_split_definition(records)


if __name__ == "__main__":
    unittest.main()
