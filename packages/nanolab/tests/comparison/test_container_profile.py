import pytest
import yaml

from nanolab.comparison.profiles import declared_options


@pytest.mark.parametrize(
    ("gc", "distribution", "monitoring"),
    [("serial", "community", []), ("G1", "oracle", ["jfr"])],
)
def test_container_options_match_native_distribution(
    tmp_path, gc, distribution, monitoring
):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        yaml.safe_dump(
            {
                "controlPlane": {
                    "modules": [
                        "k8s-deployment-provider",
                        "async-queue",
                        "build-metadata",
                    ],
                    "build": {
                        "mode": "native",
                        "builder": "container",
                        "native": {"gc": gc, "maxHeap": "64m"},
                    },
                }
            }
        )
    )
    assert declared_options(profile)["native"] == {
        "builder": "container",
        "distribution": distribution,
        "optimization": "3",
        "gc": gc,
        "monitoring": monitoring,
        "maxHeap": "64m",
    }
