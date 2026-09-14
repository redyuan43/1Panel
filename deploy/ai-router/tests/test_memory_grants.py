"""Router-account isolation, independent cloud consent and live revocation."""
import asyncio
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import pytest

from ai_router.client_accounts import (
    ClientAccountManager, HISTORY_GRANT_FIELDS, _validated_account,
    _policy_from_account, _public_account,
)
from ai_router.errors import RouterError
from ai_router.store import InMemoryStateStore


def account(changes=None, existing=None):
    value = dict(id="alice", name="Alice", models=["siyuan/auto"],
                 rpm_limit=10, tpm_limit=10000, max_parallel_requests=1)
    if existing is not None:
        value = {}
    return _validated_account({**value, **(changes or {})},
        allowed_models={"siyuan/auto"}, public_model_id="siyuan/auto", existing=existing)


def enabled():
    return account(dict.fromkeys(HISTORY_GRANT_FIELDS, True))


def test_new_account_defaults_to_own_history_without_cloud_consent():
    value = account()
    assert value["history_recall_enabled"]
    assert not value["history_owner_confirmed"]
    assert not value["history_cloud_allowed"]
    assert not value["history_legacy_cloud_allowed"]
    assert _policy_from_account(value).history_recall_enabled
    assert _public_account(value, [], {})["history_recall_enabled"]


def test_history_only_patch_preserves_retired_model_grants():
    existing = account({"history_recall_enabled": False, "disclosure_mode": "internal"})
    existing["models"] = ["retired-model"]
    updated = account({"history_recall_enabled": True}, existing)
    assert updated["models"] == ["retired-model"]
    assert updated["history_recall_enabled"]
    assert not updated["history_cloud_allowed"]
    # Preserving a stored model is not permission to grant it explicitly.
    with pytest.raises(RouterError) as error:
        account({"models": ["retired-model"]}, existing)
    assert error.value.code == "invalid_client_models"
    with pytest.raises(RouterError):
        account({"models": ["retired-model"]})


def test_history_only_patch_preserves_legacy_empty_model_grants():
    existing = account({"history_recall_enabled": False, "local_only": True,
                        "disclosure_mode": "internal"})
    existing["models"] = []
    updated = account({"history_recall_enabled": True}, existing)
    assert updated["models"] == existing["models"]
    assert updated["disclosure_mode"] == "internal"
    assert updated["local_only"] and not updated["history_cloud_allowed"]
    with pytest.raises(RouterError):
        account({"models": []}, existing)
    with pytest.raises(RouterError):
        account({"models": []})


def test_existing_opt_out_and_legacy_accounts_are_not_silently_activated():
    value = account({"history_recall_enabled": False})
    assert not account({"name": "renamed"}, value)["history_recall_enabled"]
    for field in HISTORY_GRANT_FIELDS:
        value.pop(field, None)
    assert not account({"name": "legacy"}, value)["history_recall_enabled"]
    assert not _policy_from_account(value).history_recall_enabled


@pytest.mark.parametrize("field", HISTORY_GRANT_FIELDS)
@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [], {}])
def test_consent_requires_actual_boolean(field, value):
    with pytest.raises(RouterError):
        account({field: value})


def test_all_router_accounts_can_enable_recall_without_personal_ownership():
    value = account({"history_owner_confirmed": False, "history_recall_enabled": True})
    assert _policy_from_account(value).history_recall_enabled
    value = account({"history_owner_confirmed": False}, enabled())
    assert _policy_from_account(value).history_cloud_allowed


def test_rename_preserves_grants_and_public_policy_exposes_them():
    value = account({"name": "Renamed"}, enabled())
    policy = _policy_from_account(value)
    public = _public_account(value, [], {})
    assert all(getattr(policy, key) and public[key] for key in HISTORY_GRANT_FIELDS)


@pytest.mark.parametrize("change", [
    {"history_recall_enabled": False},
    {"local_only": True},
])
def test_revocation_clears_dependent_cloud_consent(change):
    value = account(change, enabled())
    assert not value["history_cloud_allowed"]
    assert not value["history_legacy_cloud_allowed"]
    if "local_only" not in change:
        assert not value["history_recall_enabled"]
    # Re-enabling a parent grant must not silently restore cloud permission.
    value = account({"history_owner_confirmed": True,
                     "history_recall_enabled": True, "local_only": False}, value)
    assert not value["history_cloud_allowed"]


def test_local_only_rejects_explicit_cloud_grant():
    with pytest.raises(RouterError):
        account({"local_only": True, "history_cloud_allowed": True}, enabled())


def test_legacy_cloud_requires_separate_grant_and_revokes_without_implicit_regrant():
    value = account({"history_owner_confirmed": True, "history_recall_enabled": True, "history_cloud_allowed": True})
    assert not value["history_legacy_cloud_allowed"]
    value = account({"history_legacy_cloud_allowed": True}, value)
    assert _policy_from_account(value).history_legacy_cloud_allowed
    value = account({"history_cloud_allowed": False}, value)
    assert not value["history_legacy_cloud_allowed"]
    with pytest.raises(RouterError):
        account({"history_legacy_cloud_allowed": True}, value)
    value = account({"history_cloud_allowed": True}, value)
    assert not value["history_legacy_cloud_allowed"]


def test_malformed_stored_grants_fail_closed():
    value = enabled()
    value["history_recall_enabled"] = "true"
    assert not _policy_from_account(value).history_recall_enabled
    assert not _public_account(value, [], {})["history_cloud_allowed"]


def test_use_time_checks_current_grants_and_key_status():
    async def scenario():
        store = InMemoryStateStore()
        manager = ClientAccountManager(store, None, Fernet.generate_key().decode())
        # is_key_active itself is covered by existing client-account tests.
        manager.is_key_active = AsyncMock(return_value=True)
        from ai_router.client_accounts import _account_key
        value = enabled()
        await store.set_json(_account_key("alice"), value)
        assert await manager.history_policy("alice", "key-1", cloud=True)
        value = account({"history_cloud_allowed": False}, value)
        await store.set_json(_account_key("alice"), value)
        assert await manager.history_policy("alice", "key-1", cloud=True) is None
        assert await manager.history_policy("alice", "key-1", cloud=False)
        value = account({"history_recall_enabled": False}, value)
        await store.set_json(_account_key("alice"), value)
        assert await manager.history_policy("alice", "key-1", cloud=False) is None
        await store.set_json(_account_key("alice"), enabled())
        manager.is_key_active.return_value = False
        assert await manager.history_policy("alice", "key-1", cloud=False) is None
        manager.is_key_active.return_value = True
        assert await manager.history_policy("unknown", "key-1", cloud=False) is None
        manager.is_key_active.side_effect = RuntimeError("store unavailable")
        with pytest.raises(RuntimeError):
            await manager.history_policy("alice", "key-1", cloud=False)
    asyncio.run(scenario())


def test_distinct_router_accounts_and_their_keys_remain_isolated():
    async def scenario():
        manager = ClientAccountManager(InMemoryStateStore(), None, Fernet.generate_key().decode())
        for name in ("alice", "bob"):
            created = await manager.create_account(dict(id=name, name=name, models=["siyuan/auto"],
                rpm_limit=10, tpm_limit=10000, max_parallel_requests=1), allowed_models={"siyuan/auto"})
            assert created["history_recall_enabled"] and not created["history_owner_confirmed"]
        alice_key, _ = await manager.create_key("alice", "first")
        second_alice_key, _ = await manager.create_key("alice", "second")
        bob_key, _ = await manager.create_key("bob", "other account")
        assert await manager.history_policy("alice", alice_key["key_id"], cloud=False)
        assert await manager.history_policy("alice", second_alice_key["key_id"], cloud=False)
        assert await manager.history_policy("alice", bob_key["key_id"], cloud=False) is None
        assert await manager.history_policy("bob", alice_key["key_id"], cloud=False) is None
        assert await manager.history_policy("alice", alice_key["key_id"], cloud=True) is None
    asyncio.run(scenario())
