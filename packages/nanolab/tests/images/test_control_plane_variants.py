from __future__ import annotations

import pytest

from nanolab.images.control_plane_variants import (
    VARIANTS,
    resolve_variants,
)

REGISTRY = "localhost:5000"


def test_every_variant_produces_a_distinct_image_tag() -> None:
    """Two variants sharing a tag would silently benchmark one build twice."""
    tags = [variant.image(REGISTRY) for variant in VARIANTS]
    assert len(set(tags)) == len(tags)


def test_resolve_variants_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown control-plane variants: nope"):
        resolve_variants(("jvm", "nope"))
