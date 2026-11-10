from concurrent.futures import ThreadPoolExecutor

import pytest

from nanolab.tasks.one_shot.artifacts import write_immutable_artifact


def test_artifact_is_idempotent_and_refuses_changed_bytes(tmp_path):
    path = tmp_path / "profile.json"
    digest = write_immutable_artifact(path, b'{"v":1}')
    assert write_immutable_artifact(path, b'{"v":1}') == digest
    with pytest.raises(ValueError, match="immutable"):
        write_immutable_artifact(path, b'{"v":2}')
    assert path.read_bytes() == b'{"v":1}'


def test_concurrent_different_publications_do_not_overwrite(tmp_path):
    path = tmp_path / "profile"

    def publish(content):
        try:
            return write_immutable_artifact(path, content)
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(publish, [b"a", b"b"]))
    assert sum(result is not None for result in results) == 1
    assert path.read_bytes() in (b"a", b"b")


@pytest.mark.parametrize("filename", ["main.tf", ".terraform.lock.hcl"])
def test_campaign_fingerprint_binds_terraform_assets(tmp_path, monkeypatch, filename):
    from nanolab.tasks.one_shot import artifacts

    monkeypatch.setattr(artifacts, "files", lambda _: tmp_path)
    asset = tmp_path / "assets/terraform/multipass" / filename
    asset.parent.mkdir(parents=True)
    asset.write_text("original")
    before = artifacts.package_fingerprint()
    asset.write_text("changed")
    assert artifacts.package_fingerprint() != before
