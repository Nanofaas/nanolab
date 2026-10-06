"""The control-plane builds a comparison run puts side by side.

A variant is only a way of *building* the same source: same modules, same
configuration, same functions. Everything that differs is passed to the build,
so a difference in the results has one candidate explanation rather than five.

The build happens on the VM under test, not here. Native images are compiled for
the machine that runs them and cannot be cross-built from an arm64 laptop for an
amd64 node; more quietly, `-O3` inlines against the target's instruction set, so
even a same-architecture build made elsewhere is not the artefact being measured.

Nothing in this module builds a *function* image. The functions are held fixed
across variants on purpose: the question is what the control plane costs, and a
function rebuilt per variant would put a second moving part in every comparison.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class ControlPlaneVariant:
    """One way of building the control plane, and what to call the result."""

    key: str
    label: str
    # Why this build is in the matrix at all. Carried into the report so a
    # reader does not have to reconstruct the intent from the flags.
    rationale: str
    build_env: Mapping[str, str]

    def image(self, registry: str) -> str:
        """Return the control-plane image reference this variant is tagged as.

        The variant key is the tag, so the images a run compares sit side by
        side in the registry under one name.
        """
        return f"{registry}/nanofaas/control-plane:{self.key}"


def _env(**values: str) -> Mapping[str, str]:
    return MappingProxyType(dict(values))


# Mirrors the ARG default in platform/control-plane/Dockerfile: a variant that
# passes no JVM_TUNING gets this at build time, so its effective tiering is
# derived from here rather than assumed to be "full" just because the variant
# is silent about it.
DEFAULT_JVM_TUNING = "-XX:+UseSerialGC -XX:TieredStopAtLevel=1"


def jvm_optimization(variant: ControlPlaneVariant) -> str:
    """Return the JIT tier this variant's tuning selects, 'c1' or 'c2'.

    Derived from the effective JVM_TUNING — the variant's own, or the
    Dockerfile default when it sets none — so callers never have to reason
    about which flag a silent variant inherits.
    """
    tuning = variant.build_env.get("JVM_TUNING", DEFAULT_JVM_TUNING)
    return "c1" if "TieredStopAtLevel=1" in tuning else "c2"


# Serial is not written out for the Community builds: it is the only collector
# GraalVM CE offers besides epsilon, and naming it would suggest a choice was
# made. G1 is Oracle-only, which is why that variant also switches distribution.
VARIANTS: tuple[ControlPlaneVariant, ...] = (
    ControlPlaneVariant(
        key="jvm",
        label="JVM (Java 25, JIT)",
        rationale=(
            "The baseline everything else is compared against, and the only build "
            "that JIT-compiles from a profile gathered during the run itself."
        ),
        build_env=_env(),
    ),
    # The three below turn the JVM baseline into a 2x2: collector on one axis, JIT
    # tiering on the other. The baseline sets both to what a single core wants,
    # and until the CPU sweep of 2026-08-23 nobody had noticed that the build
    # called "JVM" was neither G1 nor fully JIT-compiled - while the build it was
    # compared against, native-o3-g1, did get G1.
    #
    # Dropping TieredStopAtLevel restores the default of 4 on its own, so the
    # flag is written out where C1-only is meant and omitted where it is not.
    ControlPlaneVariant(
        key="jvm-g1",
        label="JVM (G1, C1 only)",
        rationale=(
            "Isolates the collector: G1 against the baseline's serial, JIT held at C1."
        ),
        build_env=_env(JVM_TUNING="-XX:+UseG1GC -XX:TieredStopAtLevel=1"),
    ),
    # Misurato in A1c su questo codice: a un core il solo livello di tiering vale
    # 225 contro 300 dispatch al secondo, shed dal 20,8% allo 0,7% e p95 da 151 a
    # 4,3 ms. A due core il throughput pareggia e il prezzo si sposta sulla CPU:
    # 0,65 contro 0,37 core per lo stesso lavoro.
    ControlPlaneVariant(
        key="jvm-c2",
        label="JVM (serial GC, full tiering)",
        rationale=(
            "Isolates the JIT: C2 restored, collector held at the baseline's serial."
        ),
        build_env=_env(JVM_TUNING="-XX:+UseSerialGC"),
    ),
    ControlPlaneVariant(
        key="jvm-g1-c2",
        label="JVM (G1, full tiering)",
        rationale=(
            "The JVM as it would be deployed on more than one core, and the only "
            "JVM build that meets native-o3-g1 on equal terms."
        ),
        build_env=_env(JVM_TUNING="-XX:+UseG1GC"),
    ),
    # Event-loop count on a second axis, at the collector/JIT settings a single
    # CPU wants. reactor-netty's default is max(availableProcessors, 4) - four
    # loops sharing a one-core quota, each getting a wedge of CPU time too thin
    # to avoid CFS throttling. -Dreactor.netty.ioWorkerCount is a plain system
    # property, so it rides in JVM_TUNING exactly like the GC/tiering flags -
    # nothing about the build mechanism changes, only which flags it carries.
    # Pairs with jvm and jvm-c2 respectively: same collector and tiering, one
    # event loop instead of four.
    ControlPlaneVariant(
        key="jvm-loop1",
        label="JVM (serial GC, C1 only, 1 event loop)",
        rationale=(
            "jvm with the event-loop floor removed: isolates whether a single "
            "loop avoids the CFS throttling four loops hit on a one-core quota."
        ),
        build_env=_env(
            JVM_TUNING=(
                "-XX:+UseSerialGC -XX:TieredStopAtLevel=1 "
                "-Dreactor.netty.ioWorkerCount=1"
            )
        ),
    ),
    ControlPlaneVariant(
        key="jvm-c2-loop1",
        label="JVM (serial GC, full tiering, 1 event loop)",
        rationale=(
            "jvm-c2 with the event-loop floor removed: the JIT-and-loop-count "
            "2x2 cell that decides both axes together, since both attack the "
            "same one-core CPU starvation and comparisons must stay inside one run."
        ),
        build_env=_env(JVM_TUNING="-XX:+UseSerialGC -Dreactor.netty.ioWorkerCount=1"),
    ),
    ControlPlaneVariant(
        key="native-os",
        label="Native, -Os, serial GC",
        rationale=(
            "Optimised for image size. Halves the binary against -O3 and was the "
            "default until measurement showed the size is paid on the registry "
            "rather than on the node."
        ),
        build_env=_env(NATIVE_OPTIMIZATION="s"),
    ),
    ControlPlaneVariant(
        key="native-o3",
        label="Native, -O3, serial GC",
        rationale=(
            "The current default. Faster than -Os where memory is not the "
            "constraint, and identical to it where it is — the serial collector "
            "becomes the bottleneck before the code quality does."
        ),
        build_env=_env(NATIVE_OPTIMIZATION="3"),
    ),
    ControlPlaneVariant(
        key="native-o3-g1",
        label="Native, -O3, G1 (Oracle GraalVM)",
        rationale=(
            "The variant that tests whether the collector was the limit: under "
            "sustained load the serial collector spent 32% of wall-clock time "
            "collecting, with pauses reaching 1.9s."
        ),
        build_env=_env(NATIVE_OPTIMIZATION="3", NATIVE_GC="G1"),
    ),
)

VARIANTS_BY_KEY: Mapping[str, ControlPlaneVariant] = MappingProxyType(
    {variant.key: variant for variant in VARIANTS}
)


def resolve_variants(keys: tuple[str, ...]) -> tuple[ControlPlaneVariant, ...]:
    """Look up each variant key, keeping the order the caller asked for.

    Raises ValueError listing the unknown keys and the available ones when a
    key is not in the matrix.
    """
    unknown = [key for key in keys if key not in VARIANTS_BY_KEY]
    if unknown:
        raise ValueError(
            "unknown control-plane variants: "
            + ", ".join(unknown)
            + ". Available: "
            + ", ".join(VARIANTS_BY_KEY)
        )
    return tuple(VARIANTS_BY_KEY[key] for key in keys)
