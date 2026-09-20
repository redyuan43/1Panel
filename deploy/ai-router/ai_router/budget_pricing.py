"""Versioned USD budget rates, fixed at admission for each request."""
from datetime import datetime, timezone
from urllib.parse import urlparse


# Verified 2026-09-19: https://api-docs.deepseek.com/quick_start/pricing/
# Flash rates took effect 2026-09-10 04:00 UTC. Earlier records must not
# be repriced with this version. Unknown endpoints retain configured rates.
FLASH_SINCE = datetime(2026, 9, 10, 4, tzinfo=timezone.utc).timestamp()
FLASH_ALIASES = {'deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'}


def budget_rates(endpoint, timestamp):
    metadata = endpoint.metadata
    model = endpoint.provider_model
    if (metadata.get('provider') == 'deepseek'
            and urlparse(endpoint.api_base).hostname == 'api.deepseek.com'
            and model in FLASH_ALIASES and timestamp >= FLASH_SINCE):
        utc = datetime.fromtimestamp(timestamp, timezone.utc)
        peak = utc.weekday() < 5 and (1 <= utc.hour < 4 or 6 <= utc.hour < 10)
        return (.3, 1.2, .006) if peak else (.15, .6, .003)
    return (metadata.get('input_cost_per_million_usd'),
            metadata.get('output_cost_per_million_usd'),
            metadata.get('cached_input_cost_per_million_usd'))
