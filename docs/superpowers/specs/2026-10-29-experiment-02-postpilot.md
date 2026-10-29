# Experiment 02 fresh-host bootstrap repairs

The approved pilot exposed three missing preparation steps before valid load:
GraalVM Community/C compiler/zlib for native host builds, Docker Compose for
FunLess, and a complete engine stop before changing its initial systemd slice.
The kernel process guard correctly refused containerd in its cached old cgroup.
Manual corrections and their versions are recorded in the paper pilot evidence.

Automate those exact preparation steps with existing command/logging primitives.
Read GraalVM release/Java version from the captured source's gradle.properties
and invoke its checksum-verifying installer. Set JAVA_HOME/GRAALVM_HOME only for
experiment-02 native recipe commands; keep experiment01 behavior. No latest
lookup, new dependency, tunable option or service-quality gate. Keep all budget
read-back and effective-process checks. FunLess preparation installs Compose only
when that baseline is selected. Preserve existing registry recreation semantics.

Work only in fix/experiment-02-postpilot. Sonata0.6.18 is published; adopt its
pair and remove NanoLab's duplicate NSG method. Never alter the active pilot
worktree/venv, source inputs, raw evidence or protocol. No VM action, NanoLab push
or merge is included in these local repairs. Use CI-pinned read-only nanoFaaS for
contracts. Cold-host automation is based on the observed manual Azure repairs;
do not claim a second fresh-VM smoke without executing one.
