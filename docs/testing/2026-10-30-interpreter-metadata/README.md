# Interpreter metadata verification

Plugin fix: e119e4ea7594d5a868b2a8de943e526cf9eb9f76 on base 24cc21b,
version 0.1.3-SNAPSHOT. Portable plugin patch included.

- Linux plugin build, validatePlugins and pmdMain pass; 232 tests, no failures.
- NanoLab targeted suite: 389 passed. Types: 0 errors/0 warnings. Ruff and
  five import contracts pass.
- Real JVM image rebuilt through NanoFaaS's composite-plugin override.
  Distribution and HTTP metadata agree on interpreted; runtime flags verify
  UseCompiler=false, Serial GC and MaxRAMPercentage=70.
- Smoke uses the same local archived NanoFaaS source snapshot documented in
  the previous GC build evidence (synthetic revision 876ed84), not a publication
  from a real pinned campaign checkout. No performance claim is made.

The first plugin full-suite attempt on macOS failed on Linux-only native build
fixtures and missing sha256sum; the full Linux run supersedes that attempt.
Red tests reproduced the missing interpreted classification in both projects.

Published plugin 0.1.2 and NanoFaaS's existing pin are unchanged. The fix requires
a plugin release and pin update before standard remote campaign builds.
The stricter reader deliberately rejects old mislabeled interpreter reports;
archived reports have not been rewritten. No cloud resources or registry pushes.

## Publication completed

Plugin 0.1.3 published by tag v0.1.3 at b24c10abe813de76952f5f2502c0185459965a52.
Release CI and a standalone Portal-only consumer passed. See publication.json
and portal-consumer.log. The previous development-status notes above are
historical; only the NanoFaaS pin update remains outstanding.
