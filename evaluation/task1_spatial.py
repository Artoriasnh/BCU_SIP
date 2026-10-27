# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Evaluation of Task 1: parcel ingestion, classification and command-zone clustering.

Run with ``python -m evaluation.task1_spatial``; the report is written to
``evaluation/results/task1/REPORT.md``. No network access is needed.
"""

from __future__ import annotations

import argparse
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Patch
from pyproj import Geod, Transformer
from shapely.geometry import Point

from dss.config import (
    DEFAULT_PARCELS_PATH,
    GEOGRAPHIC_CRS,
    M2_PER_HA,
    METRIC_CRS,
    SpatialConfig,
)
from dss.exceptions import EmptyClusterError
from dss.hydraulics import size_hydraulics
from dss.reference_tables import CropCategory
from dss.schema import ParcelColumns, SourceColumns
from dss.spatial import ParcelCluster, ParcelLayer
from dss.taxonomy import HcatClassifier, LabelOverrides, is_agricultural
from evaluation.common import (
    BRIEF_ALENTEJO,
    LISBON,
    SEED,
    Report,
    markdown_table,
    timed,
)

logger = logging.getLogger(__name__)

MAINLAND_BBOX: Final[tuple[float, float, float, float]] = (-9.55, 36.95, -6.15, 42.16)
SINGLE_PUMP_LIMIT_KW: Final[float] = 11.0  # largest tier of the brief's catalogue
CATEGORY_COLOURS: Final[dict[CropCategory, str]] = {
    CropCategory.CEREALS: "#d4a017",
    CropCategory.ORCHARDS: "#2e8b57",
    CropCategory.PASTURES: "#9acd32",
    CropCategory.VEGETABLES: "#e4572e",
    CropCategory.FALLBACK: "#8c8c8c",
}
_TO_DEG = Transformer.from_crs(METRIC_CRS, GEOGRAPHIC_CRS, always_xy=True)
_TO_M = Transformer.from_crs(GEOGRAPHIC_CRS, METRIC_CRS, always_xy=True)


@dataclass(frozen=True, slots=True)
class SiteSearch:
    """Result of the search for a demonstration site."""

    cluster: ParcelCluster
    distance_km: float
    nearest_any_km: float
    candidates_tried: int


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> None:
    """Run the Task 1 evaluation and write the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size", type=int, default=500)
    parser.add_argument("--search-radius-km", type=float, default=30.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rng = np.random.default_rng(SEED)

    report = Report(
        "task1",
        "Task 1 evaluation: spatial ingestion and clustering",
        "This report verifies the parcel ingestion and clustering engine "
        "(`dss.spatial`, `dss.taxonomy`) against the supplied EuroCrops Portugal "
        "layer: classification coverage, geometric accuracy, robustness over "
        "random locations, performance, and sensitivity to the two Task 1 "
        "modelling choices (area accounting and buffer radius).",
    )
    for suffix in (".shp", ".dbf"):
        report.register_data_file(DEFAULT_PARCELS_PATH.with_suffix(suffix))

    raw, read_s = timed(
        lambda: gpd.read_file(
            DEFAULT_PARCELS_PATH, columns=list(SourceColumns.REQUIRED), engine="pyogrio"
        )
    )
    layer, prepare_s = timed(lambda: ParcelLayer(raw))
    parcel_points = parcel_sample_points(layer, args.sample_size, rng)
    random_points = _random_mainland_points(args.sample_size, rng)

    evaluate_classification(raw, layer, report)
    evaluate_area_accuracy(layer, report, rng)
    evaluate_performance(layer, report, read_s, prepare_s, parcel_points[:200])
    results = evaluate_robustness(layer, report, parcel_points, random_points)
    evaluate_area_basis(results, report)
    evaluate_radius_sensitivity(layer, report, parcel_points[:200])
    site = evaluate_demo_location(layer, report, args.search_radius_km)
    evaluate_maps(layer, report, site)

    print(f"Report written to {report.write()}")


# ---------------------------------------------------------------------------
# 1. Classification and label audit
# ---------------------------------------------------------------------------
def evaluate_classification(
    raw: gpd.GeoDataFrame, layer: ParcelLayer, report: Report
) -> None:
    """Coverage of the HCAT mapping, effect of the overrides, full label audit."""
    classifier = HcatClassifier.from_csv()
    overrides = LabelOverrides.from_csv()
    codes = raw[SourceColumns.HCAT_CODE].astype("string").str.strip()
    labels = raw[SourceColumns.ORIGINAL_LABEL]
    by_code = classifier.classify_series(codes)
    final = overrides.apply(codes, labels, by_code)
    agricultural = is_agricultural(raw[SourceColumns.LAND_COVER], final)
    area_ha = pd.to_numeric(raw[SourceColumns.DECLARED_AREA_M2]) / M2_PER_HA

    status = pd.Series(
        [_status_label(c) for c in final], index=raw.index, dtype="string"
    ).where(agricultural | final.isna(), "NON-AGRICULTURAL (land cover)")
    summary = (
        pd.DataFrame({"Status": status, "area_ha": area_ha})
        .groupby("Status")
        .agg(Parcels=("area_ha", "size"), Area=("area_ha", "sum"))
        .reset_index()
        .rename(columns={"Area": "Area (ha)"})
    )
    summary["Share of area"] = (
        summary["Area (ha)"] / summary["Area (ha)"].sum()
    ).map("{:.1%}".format)

    unmatched = sorted(
        code
        for code in codes.dropna().unique()
        if not any(code.startswith(rule.prefix) for rule in classifier.rules)
    )
    changed = np.array([a is not b for a, b in zip(by_code, final, strict=True)])
    override_rows = []
    for override in overrides.overrides:
        same_code = (codes == override.hcat_code).fillna(False)
        same_label = (
            labels.astype("string").str.strip().str.upper()
            == override.original_label.upper()
        ).fillna(False)
        mask = changed & same_code.to_numpy(dtype=bool) & same_label.to_numpy(bool)
        override_rows.append(
            {
                "HCAT code": override.hcat_code,
                "National label": override.original_label,
                "New category": _status_label(override.category),
                "Parcels": int(mask.sum()),
                "Area (ha)": float(area_ha[mask].sum()),
            }
        )

    audit = (
        pd.DataFrame(
            {
                "hcat_code": codes,
                "hcat_name": raw[SourceColumns.HCAT_NAME],
                "original_label": labels,
                "land_cover": raw[SourceColumns.LAND_COVER],
                "code_category": [_status_label(c) for c in by_code],
                "final_status": status,
                "area_ha": area_ha,
            }
        )
        .groupby(
            ["final_status", "hcat_code", "hcat_name", "original_label", "land_cover",
             "code_category"],
            dropna=False,
        )
        .agg(parcels=("area_ha", "size"), area_ha=("area_ha", "sum"))
        .reset_index()
        .sort_values(["final_status", "parcels"], ascending=[True, False])
    )
    audit_link = report.save_csv(audit, "hcat_label_audit")

    fig, ax = plt.subplots(figsize=(7, 3.5))
    shown = summary.sort_values("Area (ha)")
    ax.barh(shown["Status"], shown["Area (ha)"], color="#4c72b0")
    ax.set_xlabel("Declared area (ha)")
    ax.set_title("Parcel area by final classification")
    figure = report.save_figure(fig, "classification_area", "Area by final status.")

    report.check(
        "Every HCAT code matches an explicit mapping rule",
        not unmatched,
        f"{codes.nunique()} distinct codes; unmatched: {unmatched or 'none'}",
    )
    report.check(
        "Every retained parcel has a crop archetype",
        bool(layer.parcels[ParcelColumns.CATEGORY].notna().all()),
        f"{len(layer.parcels):,} agricultural parcels retained",
    )
    report.check(
        "Label overrides changed only the targeted parcels",
        sum(r["Parcels"] for r in override_rows) == int(changed.sum()),
        f"{int(changed.sum())} parcels re-assigned by {len(override_rows)} overrides",
    )
    report.add_section(
        "1. Classification coverage and label audit",
        f"""
**What.** Every parcel is classified from its 10-digit HCAT code by longest-prefix
match (`resources/hcat_mapping.csv`), corrected by a small table of documented
EuroCrops harmonisation errors (`resources/label_overrides.csv`), and kept only
if its national land-cover class is agricultural.

{markdown_table(summary)}

{figure}

**Overrides applied** (each matches both the HCAT code and the national label):

{markdown_table(pd.DataFrame(override_rows))}

**Full audit.** Every combination of HCAT code, national label, land cover and
assigned category, with parcel counts and areas, is listed in {audit_link}. All
combinations were reviewed manually; apart from the overridden labels, the
EuroCrops assignment is consistent with the national label. About 0.5 % of
parcels carry several crops (e.g. `MILHO;BATATA`); EuroCrops harmonises only
the first one, which this engine inherits.
""",
    )


# ---------------------------------------------------------------------------
# 2. Geometric accuracy
# ---------------------------------------------------------------------------
def evaluate_area_accuracy(
    layer: ParcelLayer, report: Report, rng: np.random.Generator
) -> None:
    """Planar EPSG:3763 areas against declared areas and geodesic areas."""
    parcels = layer.parcels
    declared = parcels[ParcelColumns.DECLARED_AREA_HA]
    computed = parcels[ParcelColumns.AREA_FULL_HA]
    valid = declared > 0
    rel = ((computed - declared) / declared)[valid]
    abs_rel = rel.abs()

    sample = parcels.iloc[rng.choice(len(parcels), size=min(1000, len(parcels)),
                                     replace=False)]
    geod = Geod(ellps="GRS80")  # ETRS89 ellipsoid
    geodesic_ha = np.array(
        [abs(geod.geometry_area_perimeter(g)[0]) / M2_PER_HA
         for g in sample.geometry.to_crs(GEOGRAPHIC_CRS)]
    )
    geo_rel = sample[ParcelColumns.AREA_FULL_HA].to_numpy() / geodesic_ha - 1.0
    easting_km = sample.geometry.representative_point().x.to_numpy() / 1000

    radius = layer.config.buffer_radius_m
    buffer_area = Point(0, 0).buffer(radius, quad_segs=layer.config.buffer_quad_segs)
    buffer_error = buffer_area.area / (math.pi * radius**2) - 1.0

    stats = pd.DataFrame(
        {
            "Comparison": ["Planar vs declared (all parcels)",
                           "Planar vs geodesic (1,000 sampled parcels)"],
            "n": [int(valid.sum()), len(sample)],
            "Median |error|": [f"{abs_rel.median():.3%}",
                               f"{np.median(np.abs(geo_rel)):.4%}"],
            "P95 |error|": [f"{abs_rel.quantile(0.95):.3%}",
                            f"{np.quantile(np.abs(geo_rel), 0.95):.4%}"],
            "Share > 5 %": [f"{(abs_rel > 0.05).mean():.2%}",
                            f"{(np.abs(geo_rel) > 0.05).mean():.2%}"],
        }
    )
    worst = (
        parcels.loc[valid]
        .assign(rel_error=rel)
        .reindex(abs_rel.sort_values(ascending=False).index[:10])
        [[ParcelColumns.HCAT_NAME, ParcelColumns.DECLARED_AREA_HA,
          ParcelColumns.AREA_FULL_HA, "rel_error"]]
    )
    worst["rel_error"] = worst["rel_error"].map("{:+.1%}".format)
    n_above_1pct = int((abs_rel > 0.01).sum())
    small = parcels.loc[valid & (rel.abs().reindex(parcels.index) > 0.005)]
    small_median_m2 = float(small[ParcelColumns.DECLARED_AREA_HA].median() * M2_PER_HA)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    pick = rng.choice(int(valid.sum()), size=min(5000, int(valid.sum())), replace=False)
    axes[0].loglog(declared[valid].iloc[pick], computed[valid].iloc[pick], ".",
                   ms=2, alpha=0.4)
    lims = [1e-3, max(declared.max(), computed.max())]
    axes[0].plot(lims, lims, "k--", lw=0.8)
    axes[0].set(xlabel="Declared area (ha)", ylabel="Computed area, EPSG:3763 (ha)",
                title="Planar vs declared area")
    axes[1].plot(easting_km, geo_rel * 100, ".", ms=3)
    axes[1].set(xlabel="Easting from TM06 central meridian (km)",
                ylabel="Planar / geodesic - 1 (%)",
                title="Projection scale error")
    figure = report.save_figure(
        fig, "area_accuracy",
        "Left: computed vs declared area. Right: planar vs geodesic area; the "
        "error grows with distance from the TM06 central meridian, as expected "
        "for a transverse Mercator projection.",
    )

    report.check(
        "Planar areas agree with declared areas",
        abs_rel.median() < 0.01,
        f"median |error| = {abs_rel.median():.3%} (threshold 1 %)",
    )
    report.check(
        "Planar areas agree with geodesic areas",
        np.quantile(np.abs(geo_rel), 0.99) < 0.005,
        f"P99 |error| = {np.quantile(np.abs(geo_rel), 0.99):.4%} (threshold 0.5 %)",
    )
    report.check(
        "Buffer polygon approximates the 150 m disc",
        abs(buffer_error) < 0.001,
        f"area error {buffer_error:+.4%} with {4 * layer.config.buffer_quad_segs} "
        "vertices",
    )
    report.add_section(
        "2. Geometric accuracy",
        f"""
**What.** Areas are computed in ETRS89 / Portugal TM06 (EPSG:3763). They are
checked against (i) the areas declared in the source data and (ii) an
independent geodesic computation on the GRS80 ellipsoid (`pyproj.Geod`), which
does not depend on any map projection.

{markdown_table(stats)}

{figure}

Largest disagreements with the declared area. Only {n_above_1pct} parcel(s) differ
by more than 1 %; the remaining differences above 0.5 % occur on very small
parcels (median declared area {small_median_m2:.0f} m2), consistent with declared
areas being rounded to whole square metres.

{markdown_table(worst.reset_index(drop=True))}

The buffer polygon (128 vertices) under-estimates the exact disc area by
{abs(buffer_error):.3%}.
""",
    )


# ---------------------------------------------------------------------------
# 3. Performance
# ---------------------------------------------------------------------------
def evaluate_performance(
    layer: ParcelLayer,
    report: Report,
    read_s: float,
    prepare_s: float,
    points: Sequence[tuple[float, float]],
) -> None:
    """Load time and per-query latency with the R-tree index."""
    durations = []
    for lat, lon in points:
        _, seconds = timed(lambda lat=lat, lon=lon: _try_cluster(layer, lat, lon))
        durations.append(seconds)
    q = pd.Series(durations) * 1000
    table = pd.DataFrame(
        {
            "Step": ["Read shapefile (4 attributes)", "Prepare layer (reproject, "
                     "classify, index)", f"Cluster query (n = {len(q)})"],
            "Time": [f"{read_s:.1f} s", f"{prepare_s:.1f} s",
                     f"median {q.median():.1f} ms, P95 {q.quantile(0.95):.1f} ms, "
                     f"max {q.max():.1f} ms"],
        }
    )
    report.check(
        "Cluster extraction well under a few seconds",
        q.quantile(0.95) < 1000,
        f"P95 query time {q.quantile(0.95):.1f} ms",
    )
    report.add_section(
        "3. Performance",
        f"""
The layer is loaded once; each query uses the R-tree spatial index
(`GeoDataFrame.sindex.query`) to pre-select candidates before the exact
intersection test.

{markdown_table(table)}
""",
    )


# ---------------------------------------------------------------------------
# 4. Robustness
# ---------------------------------------------------------------------------
def evaluate_robustness(
    layer: ParcelLayer,
    report: Report,
    parcel_points: Sequence[tuple[float, float]],
    random_points: Sequence[tuple[float, float]],
) -> pd.DataFrame:
    """Batch runs over random locations; checks outcomes and invariants."""
    rows = [
        {"sample": "parcel points", **_cluster_summary(layer, lat, lon)}
        for lat, lon in parcel_points
    ] + [
        {"sample": "random mainland points", **_cluster_summary(layer, lat, lon)}
        for lat, lon in random_points
    ]
    results = pd.DataFrame(rows)
    data_link = report.save_csv(results, "robustness_runs")

    outcomes = (
        results.assign(outcome=results["outcome"].str.split(":").str[0])
        .groupby(["sample", "outcome"]).size().unstack(fill_value=0).reset_index()
    )
    ok = results[results["outcome"] == "ok"]
    radius = layer.config.buffer_radius_m
    violations = {
        "clipped area > buffer area": int(
            (ok["area_clipped_ha"] > ok["buffer_ha"] * (1 + 1e-9)).sum()),
        "clipped area > whole-parcel area": int(
            (ok["area_clipped_ha"] > ok["area_full_ha"] + 1e-9).sum()),
        "centroid outside buffer": int(
            (ok["centroid_offset_m"] > radius + 1e-6).sum()),
        "empty cluster returned": int((ok["n_parcels"] < 1).sum()),
    }
    errors = results[results["outcome"].str.startswith("error")]
    parcel_ok = (results["sample"] == "parcel points") & (results["outcome"] == "ok")
    described = ok.groupby("sample")[["n_parcels", "area_clipped_ha"]].describe(
        percentiles=[0.5]
    )
    described.columns = [f"{a} {b}" for a, b in described.columns]
    described = described.reset_index()[
        ["sample", "n_parcels mean", "n_parcels 50%", "n_parcels max",
         "area_clipped_ha mean", "area_clipped_ha 50%", "area_clipped_ha max"]
    ]

    fig, axes = plt.subplots(1, 2, figsize=(11, 3.5))
    sub = ok[ok["sample"] == "parcel points"]
    axes[0].hist(sub["area_clipped_ha"], bins=30, color="#4c72b0")
    axes[0].axvline(math.pi * radius**2 / M2_PER_HA, color="k", ls="--", lw=0.8)
    axes[0].set(xlabel="Agricultural area inside buffer (ha)", ylabel="Clusters",
                title="Clipped cluster area (dashed: disc area)")
    axes[1].hist(sub["n_parcels"], bins=range(1, int(sub["n_parcels"].max()) + 2),
                 color="#55a868")
    axes[1].set(xlabel="Parcels per cluster", ylabel="Clusters",
                title="Cluster size")
    figure = report.save_figure(fig, "robustness", "Distribution over parcel points.")

    report.check(
        "No unexpected exception over random locations",
        errors.empty,
        f"{len(results)} runs; unexpected errors: {len(errors)}",
    )
    report.check(
        "Every point inside a parcel yields a cluster",
        bool(parcel_ok.sum() == len(parcel_points)),
        f"{int(parcel_ok.sum())} / {len(parcel_points)} parcel points succeeded",
    )
    report.check(
        "Geometric invariants hold for every cluster",
        sum(violations.values()) == 0,
        ", ".join(f"{k}: {v}" for k, v in violations.items()),
    )
    report.add_section(
        "4. Robustness over random locations",
        f"""
**What.** Two random samples (seed {SEED}): points placed inside randomly chosen
agricultural parcels (every query must succeed) and points drawn uniformly over
the mainland bounding box (many fall in the sea, in Spain or far from any parcel
of this sparse layer, and must raise `EmptyClusterError`). Every successful
cluster is checked against geometric invariants.

{markdown_table(outcomes)}

{markdown_table(described)}

{figure}

Per-run results: {data_link}.
""",
    )
    return results


# ---------------------------------------------------------------------------
# 5. Area accounting (clipped vs whole parcel)
# ---------------------------------------------------------------------------
def evaluate_area_basis(results: pd.DataFrame, report: Report) -> None:
    """Effect of clipping parcels to the buffer on demand and pump power."""
    ok = results[(results["outcome"] == "ok") & (results["sample"] == "parcel points")]
    ratio = ok["area_full_ha"] / ok["area_clipped_ha"]
    disc_ha = math.pi * 150.0**2 / M2_PER_HA
    max_power = size_hydraulics({CropCategory.VEGETABLES: disc_ha}).required_power_kw
    table = pd.DataFrame(
        {
            "Statistic": ["Whole / clipped area, median", "Whole / clipped area, P90",
                          "Clusters needing > 11 kW (clipped)",
                          "Clusters needing > 11 kW (whole parcel)",
                          "Max required power, clipped (kW)"],
            "Value": [f"{ratio.median():.2f}", f"{ratio.quantile(0.9):.2f}",
                      f"{(ok['power_clipped_kw'] > SINGLE_PUMP_LIMIT_KW).mean():.1%}",
                      f"{(ok['power_full_kw'] > SINGLE_PUMP_LIMIT_KW).mean():.1%}",
                      f"{ok['power_clipped_kw'].max():.2f}"],
        }
    )
    fig, ax = plt.subplots(figsize=(5, 4.5))
    ax.loglog(ok["power_clipped_kw"], ok["power_full_kw"], ".", ms=3, alpha=0.6)
    lims = [ok["power_clipped_kw"].min(), ok["power_full_kw"].max()]
    ax.plot(lims, lims, "k--", lw=0.8)
    ax.axvline(SINGLE_PUMP_LIMIT_KW, color="r", lw=0.8)
    ax.axhline(SINGLE_PUMP_LIMIT_KW, color="r", lw=0.8)
    ax.set(xlabel="Required power, clipped areas (kW)",
           ylabel="Required power, whole-parcel areas (kW)",
           title="Effect of area accounting (red: 11 kW)")
    figure = report.save_figure(fig, "area_basis", "Clipped vs whole-parcel power.")
    report.check(
        "Clipped demand never exceeds the physical maximum of the disc",
        ok["power_clipped_kw"].max() <= max_power * (1 + 1e-6),
        f"max {ok['power_clipped_kw'].max():.2f} kW <= {max_power:.2f} kW "
        "(disc fully planted with vegetables)",
    )
    report.add_section(
        "5. Area accounting: clipped vs whole parcel",
        f"""
Headline results use the area inside the buffer (areal weighting). Counting
whole parcels instead would attribute land far outside a 150 m command zone to
a single tube well:

{markdown_table(table)}

{figure}
""",
    )


# ---------------------------------------------------------------------------
# 6. Buffer radius sensitivity
# ---------------------------------------------------------------------------
def evaluate_radius_sensitivity(
    layer: ParcelLayer, report: Report, points: Sequence[tuple[float, float]]
) -> None:
    """Re-run the same locations with 100, 150 and 200 m radii."""
    rows = []
    for radius in (100.0, 150.0, 200.0):
        variant = layer.with_config(SpatialConfig(buffer_radius_m=radius))
        runs = pd.DataFrame(
            [_cluster_summary(variant, lat, lon) for lat, lon in points]
        )
        ok = runs[runs["outcome"] == "ok"]
        disc_ha = math.pi * radius**2 / M2_PER_HA
        rows.append(
            {
                "Radius (m)": int(radius),
                "Disc (ha)": round(disc_ha, 2),
                "Mean parcels": round(ok["n_parcels"].mean(), 2),
                "Mean clipped area (ha)": round(ok["area_clipped_ha"].mean(), 2),
                "Mean fill of disc": f"{(ok['area_clipped_ha'] / disc_ha).mean():.0%}",
                "Mean power (kW)": round(ok["power_clipped_kw"].mean(), 2),
                "Share > 11 kW": f"{(ok['power_clipped_kw'] > 11).mean():.1%}",
            }
        )
    report.add_section(
        "6. Sensitivity to the buffer radius",
        f"""
The same {len(points)} locations, re-run with `ParcelLayer.with_config` (the
prepared layer and its index are reused):

{markdown_table(pd.DataFrame(rows))}

Clipped area, and therefore demand and pump power, scale roughly with the disc
area (radius squared), moderated by how much of the disc is farmland.
""",
    )


# ---------------------------------------------------------------------------
# 7. Brief locations and demonstration site
# ---------------------------------------------------------------------------
def evaluate_demo_location(
    layer: ParcelLayer, report: Report, search_radius_km: float
) -> SiteSearch:
    """Check the brief's two locations and find a demonstration site."""
    outcomes = []
    for name, (lat, lon) in (("Brief 'valid' point (Alentejo)", BRIEF_ALENTEJO),
                             ("Brief urban point (Lisbon)", LISBON)):
        result = _cluster_summary(layer, lat, lon)
        nearest_km = _nearest_parcel_km(layer, lat, lon)
        outcomes.append(
            {
                "Location": name,
                "lat, lon": f"{lat}, {lon}",
                "Outcome": result["outcome"],
                "Nearest agricultural parcel": f"{nearest_km:.2f} km",
            }
        )
    report.check(
        "Lisbon raises EmptyClusterError",
        outcomes[1]["Outcome"] == "empty",
        f"outcome: {outcomes[1]['Outcome']}",
    )

    site = find_demo_site(layer, *BRIEF_ALENTEJO, radius_km=search_radius_km)
    c = site.cluster
    composition = pd.DataFrame(
        {
            "Category": [str(k.value) for k in c.area_by_category().index],
            "Clipped area (ha)": c.area_by_category().round(3).to_numpy(),
            "Whole-parcel area (ha)": c.area_by_category(clipped=False).round(3)
            .to_numpy(),
        }
    )
    report.save_csv(
        pd.DataFrame([{"lat": round(c.query_lat, 5), "lon": round(c.query_lon, 5),
                       "distance_km": round(site.distance_km, 2),
                       "centroid_lat": round(c.centroid_lat, 5),
                       "centroid_lon": round(c.centroid_lon, 5)}]),
        "demo_site",
    )
    report.check(
        "A demonstration site is found near the brief's point",
        True,
        f"({c.query_lat:.5f}, {c.query_lon:.5f}), {site.distance_km:.1f} km away",
    )
    report.add_section(
        "7. Brief locations and demonstration site",
        f"""
{markdown_table(pd.DataFrame(outcomes))}

The brief's 'valid' Alentejo point has no parcel within 150 m: the supplied layer
is a sparse subset (100,000 polygons, `OSA_ID` up to ~2.08 million), so it is
expected to raise `EmptyClusterError`.

**Demonstration site, selected by rule rather than by hand.** Candidate wells
are placed at a point inside each agricultural parcel within
{search_radius_km:.0f} km of the brief's point, ordered by distance; the first
cluster with at least 3 parcels and 2 crop archetypes is selected (so that the
demonstration exercises mixed water demand and PUE crop compatibility). The
nearest cluster of any composition lies {site.nearest_any_km:.2f} km away;
{site.candidates_tried} candidates were evaluated.

| Item | Value |
|---|---|
| Well location (lat, lon) | {c.query_lat:.5f}, {c.query_lon:.5f} |
| Distance from brief point | {site.distance_km:.2f} km |
| Parcels | {c.n_parcels} |
| Cluster centroid (lat, lon) | {c.centroid_lat:.5f}, {c.centroid_lon:.5f} |

{markdown_table(composition)}
""",
    )
    return site


def find_demo_site(
    layer: ParcelLayer,
    lat: float,
    lon: float,
    radius_km: float,
    min_parcels: int = 3,
    min_categories: int = 2,
) -> SiteSearch:
    """Nearest well location whose cluster meets the composition criteria.

    Raises:
        RuntimeError: If no candidate within ``radius_km`` qualifies.
    """
    qx, qy = _TO_M.transform(lon, lat)
    points = layer.parcels.geometry.representative_point()
    distance = np.hypot(points.x.to_numpy() - qx, points.y.to_numpy() - qy)
    order = np.argsort(distance)
    order = order[distance[order] <= radius_km * 1000]
    nearest_any = distance[order[0]] / 1000 if len(order) else math.inf
    for tried, index in enumerate(order, start=1):
        plon, plat = _TO_DEG.transform(points.x.iloc[index], points.y.iloc[index])
        try:
            cluster = layer.extract_cluster(plat, plon)
        except EmptyClusterError:  # degenerate (zero-area) repaired geometry
            continue
        enough_crops = len(cluster.categories) >= min_categories
        if cluster.n_parcels >= min_parcels and enough_crops:
            return SiteSearch(cluster, distance[index] / 1000, nearest_any, tried)
    raise RuntimeError(f"No qualifying site within {radius_km} km.")


# ---------------------------------------------------------------------------
# 8. Maps
# ---------------------------------------------------------------------------
def evaluate_maps(layer: ParcelLayer, report: Report, site: SiteSearch) -> None:
    """Visual checks: national coverage, demonstration cluster, empty locations."""
    parcels = layer.parcels
    points = parcels.geometry.representative_point()

    fig, ax = plt.subplots(figsize=(5, 8))
    ax.plot(points.x / 1000, points.y / 1000, ".", ms=0.5, color="0.6", alpha=0.5)
    for (lat, lon), marker, label in (
        (BRIEF_ALENTEJO, "rx", "Brief 'valid' point"),
        (LISBON, "kx", "Lisbon"),
        ((site.cluster.query_lat, site.cluster.query_lon), "b*", "Demonstration site"),
    ):
        x, y = _TO_M.transform(lon, lat)
        ax.plot(x / 1000, y / 1000, marker, ms=10, label=label)
    ax.set(aspect="equal", xlabel="Easting (km, EPSG:3763)",
           ylabel="Northing (km, EPSG:3763)", title="Agricultural parcels in the layer")
    ax.legend(loc="lower left")
    overview = report.save_figure(fig, "map_overview", "Sparse national coverage.")

    cluster = site.cluster
    fig, ax = plt.subplots(figsize=(6, 6))
    whole = parcels.loc[cluster.parcels.index]
    whole.boundary.plot(ax=ax, color="0.4", lw=0.8, ls="--")
    for category, group in cluster.parcels.groupby(ParcelColumns.CATEGORY):
        group.plot(ax=ax, color=CATEGORY_COLOURS[category], alpha=0.8)
    gpd.GeoSeries([cluster.buffer]).boundary.plot(ax=ax, color="k", lw=1.2)
    centre = cluster.buffer.centroid
    ax.plot(centre.x, centre.y, "k^", ms=8)
    ax.set_xlim(centre.x - 400, centre.x + 400)
    ax.set_ylim(centre.y - 400, centre.y + 400)
    ax.set(aspect="equal", title="Demonstration cluster (dashed: whole parcels)",
           xlabel="Easting (m, EPSG:3763)", ylabel="Northing (m, EPSG:3763)")
    ax.legend(handles=[Patch(color=CATEGORY_COLOURS[k], label=k.value)
                       for k in sorted(cluster.categories)], loc="upper right")
    detail = report.save_figure(fig, "map_demo_cluster",
                                "Clipped parcels by crop archetype inside the 150 m "
                                "buffer.")

    fig, axes = plt.subplots(1, 2, figsize=(11, 5.5))
    for ax, (name, (lat, lon)) in zip(
        axes, (("Brief 'valid' point", BRIEF_ALENTEJO), ("Lisbon", LISBON)), strict=True
    ):
        x, y = _TO_M.transform(lon, lat)
        nearest_km = _nearest_parcel_km(layer, lat, lon)
        half = max(3000.0, nearest_km * 1000 + 1000)  # always show the nearest parcel
        window = parcels.cx[x - half : x + half, y - half : y + half]
        if not window.empty:
            window.plot(ax=ax, color="#2e8b57", alpha=0.7)
        gpd.GeoSeries([Point(x, y).buffer(150)]).boundary.plot(ax=ax, color="r")
        ax.plot(x, y, "r+", ms=10)
        ax.set_xlim(x - half, x + half)
        ax.set_ylim(y - half, y + half)
        ax.set(aspect="equal", title=f"{name}: nearest parcel {nearest_km:.2f} km",
               xlabel="Easting (m, EPSG:3763)", ylabel="Northing (m, EPSG:3763)")
    context = report.save_figure(fig, "map_empty_locations",
                                 "Surroundings of the brief's points (red: 150 m "
                                 "buffer; green: agricultural parcels).")
    report.add_section("8. Maps", f"{overview}\n\n{detail}\n\n{context}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _status_label(category: CropCategory | None) -> str:
    return "EXCLUDED (HCAT branch)" if category is None else category.value


def parcel_sample_points(
    layer: ParcelLayer, n: int, rng: np.random.Generator
) -> list[tuple[float, float]]:
    """(lat, lon) of a point inside each of ``n`` random agricultural parcels."""
    index = rng.choice(len(layer.parcels), size=min(n, len(layer.parcels)),
                       replace=False)
    points = layer.parcels.geometry.iloc[index].representative_point()
    lon, lat = _TO_DEG.transform(points.x.to_numpy(), points.y.to_numpy())
    return list(zip(lat.tolist(), lon.tolist(), strict=True))


def _random_mainland_points(
    n: int, rng: np.random.Generator
) -> list[tuple[float, float]]:
    lon_min, lat_min, lon_max, lat_max = MAINLAND_BBOX
    lat = rng.uniform(lat_min, lat_max, n)
    lon = rng.uniform(lon_min, lon_max, n)
    return list(zip(lat.tolist(), lon.tolist(), strict=True))


def _try_cluster(layer: ParcelLayer, lat: float, lon: float) -> ParcelCluster | None:
    try:
        return layer.extract_cluster(lat, lon)
    except EmptyClusterError:
        return None


def _cluster_summary(layer: ParcelLayer, lat: float, lon: float) -> dict[str, Any]:
    """Outcome and key figures of one query, never raising."""
    base: dict[str, Any] = {"lat": lat, "lon": lon}
    try:
        cluster = layer.extract_cluster(lat, lon)
    except EmptyClusterError:
        return {**base, "outcome": "empty"}
    except Exception as exc:  # any other failure is recorded, not raised
        return {**base, "outcome": f"error: {type(exc).__name__}: {exc}"}
    qx, qy = _TO_M.transform(lon, lat)
    cx, cy = _TO_M.transform(cluster.centroid_lon, cluster.centroid_lat)
    clipped = size_hydraulics(cluster.area_by_category())
    whole = size_hydraulics(cluster.area_by_category(clipped=False))
    return {
        **base,
        "outcome": "ok",
        "n_parcels": cluster.n_parcels,
        "n_categories": len(cluster.categories),
        "buffer_ha": cluster.buffer.area / M2_PER_HA,
        "area_clipped_ha": cluster.total_area_clipped_ha,
        "area_full_ha": cluster.total_area_full_ha,
        "demand_clipped_m3_day": clipped.daily_demand_m3,
        "power_clipped_kw": clipped.required_power_kw,
        "power_full_kw": whole.required_power_kw,
        "centroid_offset_m": math.hypot(cx - qx, cy - qy),
    }


def _nearest_parcel_km(layer: ParcelLayer, lat: float, lon: float) -> float:
    x, y = _TO_M.transform(lon, lat)
    return float(layer.parcels.distance(Point(x, y)).min()) / 1000


if __name__ == "__main__":
    main()
