# Managed Spot campaign integration

The August GC/JIT/native campaign uses the bundled
`azure-comparison-spot.yaml.example`: two managed roles without explicit hosts,
Spot priority, Deallocate eviction policy, maximum bid -1. The existing workflow
creates and cleans up both roles through Sonata and azure-vm-sdk 0.8.0.

Sonata 0.6.19 was published from `a58d0bdc999f664199c6521b494e6a6df931ae4d`:

- [Merged PR #25](https://github.com/Nanofaas/sonata/pull/25)
- [Successful release workflow](https://github.com/Nanofaas/sonata/actions/runs/41064498314)
- Both PyPI wheel/sdist pairs were present and a clean PyPI-only installation
  passed the managed Spot provider/SDK rendering smoke test.

NanoLab now pins both distributions at 0.6.19; the lock contains PyPI artifacts,
not local checkouts. After installing those published dependencies, 674 targeted
configuration/provisioning/comparison tests passed. All pre-commit checks passed,
including types, import contracts and security checks. An additional pre-release
selection had passed 203 VM/release tests, with 6 skipped and 191 contracts
deselected. Independent review found no correctness blocker.

The integration test uses the real `provision_roles` context, managed provider
and SDK renderer, with simulated external commands. It verifies creation of
both Spot VMs and two teardown operations. No live Azure allocation or benchmark
was run, and this update did not rerun NanoLab's full Linux test suite.
