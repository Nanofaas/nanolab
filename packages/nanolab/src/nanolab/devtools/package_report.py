"""Package-level import metrics, measured with grimp.

`calculate_metrics` turns the module import graph into per-package counts of
internal, outgoing and incoming imports and the instability that follows from
them. Run as a script it prints that table for this repository.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import grimp

ROOT_PACKAGE = "nanolab"
TOP_LEVEL_PACKAGES = (
    "nanolab.app",
    "nanolab.cli",
    "nanolab.config",
    "nanolab.core",
    "nanolab.devtools",
    "nanolab.functions",
    "nanolab.plans",
    "nanolab.tui",
    "nanolab.workspace",
)


@dataclass(frozen=True)
class PackageMetrics:
    """The import counts for one top-level package.

    `instability` is Martin's metric, outgoing divided by all cross-package
    imports in and out: 0.0 means the package is only imported, 1.0 means it
    only imports.
    """

    package: str
    internal_imports: int
    outgoing_imports: int
    incoming_imports: int
    instability: float


def _top_level_package(module: str, packages: Sequence[str]) -> str | None:
    for package in packages:
        if module == package or module.startswith(f"{package}."):
            return package
    return None


def calculate_metrics(
    *,
    packages: Sequence[str],
    edges: Iterable[tuple[str, str]],
) -> list[PackageMetrics]:
    """Fold import edges into one `PackageMetrics` per package.

    Each edge is attributed to the top-level packages its two ends belong to:
    the same package counts as internal, anything else as an outgoing import
    for the importer and an incoming one for the imported. Edges with an end
    outside `packages` are ignored.
    """
    internal_counts = dict.fromkeys(packages, 0)
    outgoing_counts = dict.fromkeys(packages, 0)
    incoming_counts = dict.fromkeys(packages, 0)

    for importer, imported in edges:
        importer_package = _top_level_package(importer, packages)
        imported_package = _top_level_package(imported, packages)
        if importer_package is None or imported_package is None:
            continue
        if importer_package == imported_package:
            internal_counts[importer_package] += 1
            continue
        outgoing_counts[importer_package] += 1
        incoming_counts[imported_package] += 1

    metrics: list[PackageMetrics] = []
    for package in packages:
        outgoing = outgoing_counts[package]
        incoming = incoming_counts[package]
        denominator = incoming + outgoing
        instability = round(outgoing / denominator, 2) if denominator else 0.0
        metrics.append(
            PackageMetrics(
                package=package,
                internal_imports=internal_counts[package],
                outgoing_imports=outgoing,
                incoming_imports=incoming,
                instability=instability,
            )
        )
    return metrics


def format_metrics_table(metrics: Sequence[PackageMetrics]) -> str:
    """Render one fixed-width row per package, under a header and a rule.

    Returns the whole table as one string, with no trailing newline.
    """
    header = (
        f"{'package':38} {'internal':>8} {'outgoing':>8} "
        f"{'incoming':>8} {'instability':>11}"
    )
    rows = [header, "-" * len(header)]
    rows.extend(
        f"{metric.package:38} "
        f"{metric.internal_imports:8d} "
        f"{metric.outgoing_imports:8d} "
        f"{metric.incoming_imports:8d} "
        f"{metric.instability:11.2f}"
        for metric in metrics
    )
    return "\n".join(rows)


def _iter_grimp_edges(root_package: str) -> list[tuple[str, str]]:
    graph = grimp.build_graph(root_package, include_external_packages=False)
    modules = sorted(
        module
        for module in graph.modules
        if module == root_package or module.startswith(f"{root_package}.")
    )
    edges: list[tuple[str, str]] = []
    for importer in modules:
        edges.extend(
            (importer, imported)
            for imported in sorted(graph.find_modules_directly_imported_by(importer))
            if imported == root_package or imported.startswith(f"{root_package}.")
        )
    return edges


def build_current_metrics() -> list[PackageMetrics]:
    """Measure this checkout and return one record per top-level package."""
    return calculate_metrics(
        packages=TOP_LEVEL_PACKAGES,
        edges=_iter_grimp_edges(ROOT_PACKAGE),
    )


def main() -> None:
    """Parse arguments and print this repository's import table."""
    parser = argparse.ArgumentParser(
        description="Report internal and cross-package imports for nanolab."
    )
    parser.parse_args()
    print(format_metrics_table(build_current_metrics()))


if __name__ == "__main__":
    main()
