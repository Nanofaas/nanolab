# Recipe-backed load test design

**Date:** 2026-09-29

## Intent and scope

The NanoLab roadmap calls for reusable recipe profiles in build workflows while preserving load-test measurements. This first slice migrates the ordinary container load test `autoscaling-cycle-container.yaml`. Runtime-comparison matrices follow as a separate slice because their control-plane variants must be built once on the measured VM and every cell must consume fixed images. No comparison cell changes in this slice.

A successful run still registers `word-stats-java`, drives the same k6 cycle, collects the same Prometheus and resource measurements, and tears down the same owned resources. Its images come from one validated recipe distribution, retained with the run evidence. The existing non-recipe load tests remain valid.

## Chosen approach

Use the existing `recipeProfile` field and recipe distribution contract, rather than introducing another build mode. Add a repository profile `packages/nanolab/recipes/loadtest-container-jvm.yaml` with `container-deployment-provider`, `autoscaler`, `async-queue`, and `build-metadata`; a JVM control plane; and the Java `word-stats` function. Keep the effective control-plane JVM tuning of the current scenario. The profile uses the local run registry and a per-run tag.

Extend the load-test plan to prepare a staged source snapshot and run `publishRecipe` once before the platform starts. Parse and validate `distribution.json` against the staged profile and expected component/module set. Use its image references and identities for the control plane and function; do not schedule the old bootJar, Docker build, or push operations for these components. Publication must finish before Compose deployment and registration. Preserve the existing k6, snapshot, replica, and metrics tasks and their configuration.

The container recipe path uses the current local environment and run-scoped registry. The new profile is selected only by the migrated scenario, so other load tests continue using their present build graph. Reject recipe use with an unsupported backend or provider before provisioning. Keep plan and dry-run side-effect free.

## Evidence and failure behavior

Keep the staged recipe, Gradle log, `distribution.json`, and the current load-test summary and metrics under the run directory. Verify the report's recipe hash, source provenance, tag, selected modules, component modes, image references, local IDs, published digests, and exact function set before deployment. A failed publication retains diagnostics, skips deployment and load generation, and leaves cleanup to the existing resource ownership graph. Do not infer image identity from a mutable tag alone.

## Compatibility and boundaries

The scenario's load shape, function resources, autoscaling settings, endpoints, and metric catalogue do not change. The recipe describes artifacts and modules; the scenario remains the owner of workload and measurement settings. Runtime-comparison `prepare` and matrix cells stay unchanged in this slice. Their later migration will use separate reusable profiles for each measured variant, build on the measured VM, publish shared function images once, and pin each cell to prepared image digests or verified immutable references.

## Verification

- Unit and plan tests show one publication, no duplicate component builds, correct dependency order, report validation, and unchanged k6/metrics graph.
- Negative tests reject mismatched profile/report, missing function, unsupported environment, and publication failure before deployment.
- `validateRecipe` succeeds for the new profile against the selected NanoFaaS checkout; CI validates it with the other repository profiles.
- The migrated scenario passes one local container end-to-end run. Compare its step graph and emitted measurement files with the existing load-test contract; no numerical equality of separate load runs is expected.
- Ruff, type checking, relevant NanoLab tests, and `git diff --check` pass. Update `docs/recipes-roadmap.md` only after the end-to-end evidence exists.
