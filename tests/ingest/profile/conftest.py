import csv
import io

import pytest


def write_csvs(dirpath, tables: dict[str, tuple[list[str], list[list]]]):
    for name, (header, rows) in tables.items():
        with open(dirpath / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
    return dirpath


@pytest.fixture
def csv_dir(tmp_path):
    def make(tables):
        d = tmp_path / "ds"
        d.mkdir(exist_ok=True)
        return write_csvs(d, tables)
    return make
