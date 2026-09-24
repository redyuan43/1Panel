"""Keep the V100 image admission boundary aligned with its qualified runtime."""

import pytest

from ai_router.errors import NoEligibleModelError
from test_routing_modes import request, run, setup


def test_v100_32_images_admitted_and_33_rejected_before_dispatch(tmp_path):
    policy, registry, options = setup(tmp_path)
    endpoint = registry.by_id("ai-qwen38-27b")
    assert endpoint.supports_image_count(32)
    assert not endpoint.supports_image_count(33)
    assert all(profile.max_images == 32 for profile in endpoint.deployment_profiles)

    choice = run(request(
        policy, options, requested_model=endpoint.public_model,
        modalities={"text", "image"}, image_count=32,
    ))
    assert choice.endpoint.id == endpoint.id

    with pytest.raises(NoEligibleModelError):
        run(request(
            policy, options, requested_model=endpoint.public_model,
            modalities={"text", "image"}, image_count=33,
        ))
