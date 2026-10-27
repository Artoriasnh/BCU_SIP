# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Runner: the DSS pipeline on valid and invalid coordinates.

Usage::

    python run_demo.py

The parcel layer is loaded and indexed once, then each location is run in
turn. Valid locations print the outputs of Tasks 1-4; invalid ones print the
exception raised, showing how each failure is reported.
"""

from __future__ import annotations

from typing import Final

from dss.energy import EnergyBasis
from dss.exceptions import DataValidationError, EmptyClusterError, SolarDataError
from dss_pipeline import DssPipeline, Scenario

# Demonstration site: the nearest location to the brief's example point with at
# least three parcels of two archetypes within 150 m, found by the rule in
# evaluation/task1_spatial.py (ASSUMPTIONS.md, A4).
DEMO_SITE: Final[tuple[float, float]] = (38.07759, -7.96330)

CASES: Final[tuple[tuple[str, float, float, Scenario], ...]] = (
    ("Demonstration site, brief's parameters (headline)", *DEMO_SITE, Scenario()),
    ("Demonstration site, demand-based consumption (sensitivity, A13)",
     *DEMO_SITE, Scenario(energy_basis=EnergyBasis.DEMAND)),
    ("Brief's example point, Alentejo (no parcel in the supplied sample, A4)",
     38.015, -7.865, Scenario()),
    ("Lisbon city centre (urban)", 38.722, -9.139, Scenario()),
    ("Atlantic Ocean", 38.5, -10.0, Scenario()),
    ("Invalid latitude", 95.0, -8.0, Scenario()),
)  # fmt: skip


def main() -> None:
    """Run every case and print its outcome."""
    print("Loading and indexing the parcel layer ...")
    try:
        pipeline = DssPipeline.from_file()
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from None
    for title, lat, lon, scenario in CASES:
        print(f"\n=== {title}: ({lat}, {lon}) ===")
        try:
            print(pipeline.run(lat, lon, scenario).summary())
        except (EmptyClusterError, DataValidationError, SolarDataError) as exc:
            print(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
