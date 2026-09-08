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
