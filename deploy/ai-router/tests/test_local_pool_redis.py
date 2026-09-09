"""Optional real Redis regression, run against disposable network-isolated Redis."""
import asyncio, os
import pytest
from ai_router.local_pool import LocalPool, MEMBERS, PREFIX
from ai_router.store import RedisStateStore
from test_local_pool import fixtures, trace

@pytest.mark.skipif(not os.environ.get("LOCAL_POOL_TEST_REDIS"), reason="requires disposable Redis")
def test_independent_router_clients_share_atomic_allocations():
    async def case():
        template, es, sts = fixtures()
        stores=[RedisStateStore(os.environ["LOCAL_POOL_TEST_REDIS"]) for _ in range(2)]
        pools=[LocalPool(s,template.settings) for s in stores]
        ids=["redis-new-A", "redis-new-B", "redis-new-C"]
        try:
            results=await asyncio.gather(*(pools[i%2].select(es,sts,trace=trace(rid),conversation=None,prompt_tokens=40000,output_tokens=16000) for i,rid in enumerate(ids)))
            assert {r.id for r in results} == set(MEMBERS)
            assert len(await stores[0].list_json(PREFIX+"claim:")) == 3
            assert len(await stores[1].list_json(PREFIX+"claim:")) == 3
        finally:
            for rid in ids: await pools[0].release(rid)
            for store in stores: await store.close()
    asyncio.run(case())


@pytest.mark.skipif(not os.environ.get("LOCAL_POOL_TEST_REDIS"), reason="requires disposable Redis")
def test_verified_history_atomic_writes_preserve_all_candidates_and_bound_memory():
    async def case():
        from ai_router.policy import ConversationRepository
        from ai_router.history import history_identities, history_lookup_identities
        from tests.test_verified_history import histories, save
        template, _, _ = fixtures()
        stores = [RedisStateStore(os.environ["LOCAL_POOL_TEST_REDIS"]) for _ in range(2)]
        repos = [ConversationRepository(s, template.settings) for s in stores]
        a, b = histories()
        try:
            for i in range(20): await save(repos[i%2], "parallel-"+str(i))
            await asyncio.gather(*(repos[i%2].map_history("isolated", history_identities(a), "parallel-"+str(i)) for i in range(20)))
            values = await stores[0].list_json("router:verified-history:isolated:")
            assert len(values) == 1 and len(values[0]["branches"]) == 16 and values[0]["overflow"] is True
            parent, evidence = await repos[1].verified_history_match("isolated", history_lookup_identities([*b, {"role":"user","content":"next"}]))
            assert parent is None and evidence["reason"] == "ambiguous_history"
        finally:
            for store in stores: await store.close()
    asyncio.run(case())
