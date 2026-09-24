import hashlib
import json
from pathlib import Path

import pytest

from test_workbuddy_media_client import configured, modules, servers, ARTIFACT


def test_delivery_recovers_same_job_and_reuses_verified_file(configured, tmp_path):
    media, client, router = configured.media, configured.client, configured.peer
    args = media.arguments(["image", "--operation-id", "delivery-test", "--prompt", "SIYUAN", "--output", str(tmp_path / "wallpaper.png")])
    result = media.run(args, client)
    identifier = result["id"]
    router.jobs[identifier].update(status="completed", output={"id": "artifact_delivery", "output_id": "version_delivery",
        "sha256": hashlib.sha256(ARTIFACT).hexdigest(), "bytes": len(ARTIFACT), "content_type": "image/png"})
    # The legacy mock returns a generic receipt error; the actual image must
    # still be delivered, and no second inference may be created.
    original = router.reply
    def reply(request):
        if request.path.endswith("/delivery"):
            return 503, {"error": {"code": "media_unavailable"}}, {}
        return original(request)
    router.reply = reply
    first = media.run(media.arguments(["deliver", "--operation-id", "delivery-test", "--seconds", "0"]), client)
    second = media.run(media.arguments(["deliver", "--operation-id", "delivery-test", "--seconds", "0"]), client)
    assert first["delivery_status"] == second["delivery_status"] == "downloaded"
    assert first["delivery_reported"] is False
    assert second["reused"] is True
    assert Path(first["local_path"]).read_bytes() == ARTIFACT
    assert len(router.effects) == 1


def test_rejected_image_never_downloads_or_changes_prompt(configured, tmp_path):
    media, client, router = configured.media, configured.client, configured.peer
    args = media.arguments(["image", "--operation-id", "rejected-test", "--prompt", "Original Iron Man scene", "--output", str(tmp_path / "wallpaper.png")])
    job = media.run(args, client)
    router.jobs[job["id"]].update(status="failed", error={"code": "image_moderation_blocked", "message": "Rejected"})
    value = media.run(media.arguments(["deliver", "--operation-id", "rejected-test", "--seconds", "0"]), client)
    assert value["delivery_status"] == "not_generated"
    assert len(router.effects) == 1
    assert not (tmp_path / "wallpaper.png").exists()
