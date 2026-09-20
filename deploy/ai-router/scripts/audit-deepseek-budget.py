"""Read-only reconciliation preview; never overwrite the shared monthly ledger.

Run with PYTHONPATH=. python scripts/audit-deepseek-budget.py --db PATH --since ISO
Only successful measured DeepSeek Flash attempts are priced. The output is
not a replacement for the all-provider ledger or a provider invoice.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from ai_router.budget import BudgetReservation, usage_cost
from ai_router.budget_pricing import FLASH_SINCE, budget_rates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--since', required=True, help='ISO timestamp including UTC offset')
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since)
    if since.tzinfo is None or since.timestamp() < FLASH_SINCE:
        parser.error('since must include timezone and be after the verified price effective date')
    endpoint = SimpleNamespace(metadata={'provider': 'deepseek'},
        provider_model='deepseek-v4-flash', api_base='https://api.deepseek.com')
    total = 0.0
    measured = unknown = 0
    with sqlite3.connect(Path(args.db).resolve().as_uri() + '?mode=ro', uri=True) as db:
        for rid, raw in db.execute(
                "SELECT request_id,payload_json FROM route_traces WHERE started_at>=? "
                "AND selected_model='deepseek/deepseek-v4-flash'", (since.timestamp(),)):
            trace = json.loads(raw)
            for attempt in trace.get('attempts', []):
                steps = [s for s in attempt.get('steps', []) if s.get('node_id') == 'upstream_request']
                if not steps:
                    continue  # Rejected before dispatch: no provider bill to reconcile.
                evidence = {}
                for step in steps:
                    evidence.update(step.get('evidence', {}))
                model = evidence.get('selected_model')
                if model and model != 'deepseek/deepseek-v4-flash':
                    continue
                if not model and attempt is not trace['attempts'][-1]:
                    unknown += 1
                    continue
                backend = evidence.get('backend_usage') or {}
                if (backend.get('state') != 'complete'
                        or evidence.get('output_tokens_measured') is not True):
                    unknown += 1
                    continue
                timestamp = steps[0]['timestamp']
                rates = budget_rates(endpoint, timestamp)
                reservation = BudgetReservation('', rid, 0, *rates)
                usage = {'prompt_tokens': backend.get('input_tokens'),
                         'prompt_cache_hit_tokens': backend.get('cached_tokens'),
                         'completion_tokens': evidence.get('output_tokens')}
                amount, source = usage_cost(reservation, usage)
                if source != 'measured':
                    unknown += 1
                    continue
                measured += 1
                total += amount
                print(json.dumps({'request_id': rid, 'attempt': attempt.get('number'),
                                  'usage': usage, 'amount_usd': amount, 'measurement': source}))
    print(json.dumps({'summary': True, 'measured_attempts': measured,
                      'unknown_attempts': unknown, 'measured_usd': total,
                      'safe_to_replace_global_ledger': False}))


if __name__ == '__main__':
    main()
