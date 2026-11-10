# Final fresh-context review

Reviewed the complete changes from Sonata `3a08cc6`, Nanolab `742c0d39`, and Workbench `150037d`, including working-tree modifications and untracked implementation/tests/assets. Read the approved design, implementation plans and ruling ledgers. Review was read-only on all repositories; focused reproductions used temporary directories. This is the single review, not a follow-up review.

## Strengths

The separation between provider-neutral Sonata stack ownership and Nanolab borrowed VM bindings is appropriate. Private diagnostics avoid leaking Terraform output through public errors, execution uses argv and finite timeouts, ownership is written before apply, and active stacks refuse infrastructure-changing plans. Nanolab records consumer entry before handing out a VM, validates complete node membership, and orders collection before stack release. The original remediation fixes cover both producers and consumers of host-qualified archives, preserve composite ownership before terminal journal writes, and add meaningful missing-container and pod-network tests.

## Critical

None found.

## Important

### R1 — Effective Terraform inputs are not fully frozen

**Locations:** Sonata `packages/sonata-tasks/src/sonata_tasks/terraform.py:154-170`, `:194-214`; Nanolab `packages/nanolab/src/nanolab/one_shot/terraform.py:164-179`.

`_environment()` permits inherited `TF_VAR_*` and CLI configuration, whereas fingerprints include only declared files and `spec.env`. The explicitly configured `TF_CLI_CONFIG_FILE` is fingerprinted as a pathname, without binding its contents; default user CLI configuration is also uncontrolled. Thus variable values or provider installation/development configuration can change under the same ownership identity. This is a real breach of the promised immutable configuration contract, not merely a missing test.

**Reproduced:** with real local OpenTofu, setting `TF_VAR_label` after constructing the resource left its fingerprint unchanged and applied the new value. Separately, changing the contents of an explicit CLI config file passed `spec.verify()` and left its fingerprint unchanged.

**Smallest fix:** reject undeclared inherited Terraform variable inputs, or capture and verify their effective values privately. Bind the actual CLI configuration bytes to the prepared input set, and prevent implicit user configuration from escaping that binding. Continue hashing secret values rather than putting them in journal payloads. Add regressions for post-construction environment and CLI-file changes.

### R2 — Failed compensation recording can reopen an uncertain stack

**Location:** Sonata `packages/sonata-tasks/src/sonata_tasks/_terraform_stack.py:219-227`, together with `:161-162`.

After a failed apply, compensation calls `destroy()`, which durably writes `phase=destroyed`. Only afterwards does the failure handler attempt to replace that with `uncertain`. If this latter write fails, the durable record authorizes another acquisition despite possible resources that the provider never recorded. A crash between these writes has the same effect.

**Reproduced:** failed apply, successful fake destroy, injected `OSError` for uncertain-record writes produced durable `phase=destroyed`. The next acquire ran `init`, `plan`, `apply`, `output` instead of refusing recovery.

**Smallest fix:** compensation following a failed apply must never publish `destroyed`; give that internal destroy path an uncertain terminal phase. If its final write fails, the previous `destroying`/`applying` phase must continue blocking automatic operations. Test successful compensation followed by a failed terminal record write.

### R3 — A failing retention guard does not persist retention

**Location:** Sonata `packages/sonata-tasks/src/sonata_tasks/_terraform_stack.py:245-259`.

A retention callback exception leaves the owner record `active`. The same applies when a positive retention decision cannot be written. Consequently, reconstruction can erase the only evidence that destruction was unsafe. This contradicts the documented durable retention behavior and is particularly relevant to callbacks reading archive receipts from a failing filesystem.

**Reproduced:** a guard raising `OSError` left `phase=active`; reconstructing the identical stack without the callback then executed destroy. The existing successful-retention reconstruction test does not cover this path.

**Smallest fix:** persist a conservative release-check phase before evaluating the guard, and leave it recovery-required on exceptions or interrupted recording. Ensure the ownership record cannot silently discard a required guard during reconstruction. Add callback-exception and retention-write-failure cases.

### R4 — Normal Docker bootstrap makes the fleet fail resume validation

**Location:** Nanolab `packages/nanolab/src/nanolab/assets/terraform/multipass/main.tf:45-46`; Sonata active-plan guard at `_terraform_stack.py:190-195`.

The module exports every guest IPv4 address, although Nanolab consumes only the first. Installing Docker adds a bridge address and therefore changes the Terraform output. The strict active-stack guard then treats an ordinary, successfully bootstrapped VM as recovery-only even though no infrastructure change is proposed.

**Evidence inspected:** `/private/tmp/nl-terraform-evidence/server-smoke/infrastructure/spark/.sonata-terraform/plan-be174718770f4773b78dbe67dc4bf914.log` shows only `172.17.0.1` being appended to `edge-0.ipv4`; OpenTofu explicitly reports that applying would change no real infrastructure.

**Smallest fix:** export only the stable guest address actually consumed by the binding, preserving the output schema if convenient. Test a second acquisition after Docker bootstrap/bridge creation. Keep the refusal of plans that replace or otherwise modify evidence-bearing infrastructure.

### R5 — Public-key lookup breaks valid dotted private-key filenames

**Location:** Nanolab `packages/nanolab/src/nanolab/one_shot/terraform.py:138-140`.

`with_suffix('.pub')` replaces a private key's existing filename suffix. For `/keys/my.key`, it reads `/keys/my.pub` instead of the SSH-generated `/keys/my.key.pub`. This either prevents provisioning or installs a different public key, after which the existing private-key transport cannot access the VMs.

**Smallest fix:** append `.pub` to the complete private-key filename. Add a fixture with a dotted filename and verify both selected key content and unchanged private-key transport.

### R6 — Frozen campaigns do not bind the new Terraform assets

**Locations:** Nanolab `packages/nanolab/src/nanolab/tasks/one_shot/artifacts.py:58-66`, new `assets/terraform/multipass/main.tf` and `.terraform.lock.hcl`.

The package fingerprint deliberately binds implementation/assets and is checked before acquiring campaign infrastructure (`one_shot/campaign.py:632`). Its extension filter omits both `.tf` and `.hcl`. Editing the new module or provider lock after freezing a campaign therefore does not trigger the frozen-implementation check. A first stack acquisition accepts the changed module; per-stack hashes only protect subsequent uses of that already-prepared directory.

**Reproduced:** changing both Terraform asset contents in an isolated package-root fixture left `package_fingerprint()` unchanged.

**Smallest fix:** include the new Terraform module and lock assets in the package fingerprint and add a regression that each changes the digest. This is required integration with the existing freeze boundary, not a new feature.

## Minor

Workbench `roles/prometheus/README.md:19` and `roles/supervisor/README.md:57` still say pod form uses host networking immediately before the new paragraphs correctly describe pod networking. Remove/update the obsolete sentences and make the bind defaults table explicit about form.

## Review focus conclusions

Unsafe state/CLI argument overrides already covered by `_environment()` are rejected before commands; R1 covers the remaining identity gap. Declared-file mutation is checked before execution. Timeout/exception during apply conservatively records uncertainty without immediate destruction; R2 identifies the separate ordinary-failure compensation durability gap. Nanolab's missing collection completion marker conservatively retains an activated stack across process restart. Exact node membership/name and distinct primary-address checks are present; R4 is the real-fleet restart issue. The original journal-ownership, archive retention, CPU placement, coverage, host alias, comparison, pod Service, pod loadgen and malformed-kind changes did not yield additional material defects in this pass.

For a fresh custom backend configuration, backend metadata is inspected only after `init`; existing initialized custom metadata is rejected before commands. I did not establish a concrete external state mutation from that fresh-init path, so I am not labeling it a demonstrated destructive bug. The existing unit test exercises pre-existing metadata only; a real negative custom-backend case would make this boundary clearer.

## Declined to judge

- Final Linux/Kubernetes/installed-package gate results still running: execution evidence is pending, not a code defect or presumed success.
- Ten-VM scientific acceptance: compatible freeze/reference and capacity are unavailable; small-fleet success cannot establish it.
- Publication ordering and public dependency pin updates: intentionally withheld until candidate artifacts are ready.
- External side effects of initializing every supported remote/cloud backend: not exercised; no remote service was contacted during this review.
- Provider-internal correctness beyond the inspected real-fleet observations: the provider repository is outside these three reviewed changes.

## Verdict

**Ready to merge: No, until R1–R6 are fixed and the pending required gates finish.** The architecture and prior remediation are sound, but immutable-input and durable-recovery contracts still have reproducible holes, and ordinary VM bootstrap currently blocks resume.

Skipped a second review and full-suite reruns; this pass used source inspection, isolated fault injection and one provider-free real OpenTofu lifecycle. Remaining risk includes unverified final integration gates and the explicitly unavailable ten-VM scientific acceptance.
