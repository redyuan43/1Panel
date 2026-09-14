"""GLM usage accounting, independent of routing and budget reservations.

Amounts are integer nano-CNY. Only finalized upstream usage can price an
attempt; missing measurements remain unknown, including failed attempts.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
import json
import logging
from pathlib import Path
import sqlite3
import time

from .usage_evidence import token_count

BJT = timezone(timedelta(hours=8))
MODELS = {"glm-5.3", "glm-5.3-flash"}
TERMINAL = {"succeeded", "failed", "interrupted"}


def model_id(value):
    value = str(value or "").removeprefix("zhipu/")
    return value if value in MODELS else None


def money(value):
    return format(Decimal(value) / Decimal(1_000_000_000), ".9f") if value is not None else None


@lru_cache(maxsize=1)
def price_catalog():
    value = json.loads((Path(__file__).resolve().parent.parent / "config/glm-prices.json").read_text())
    for rate in value["versions"]:
        rate["starts_at"] = datetime.fromisoformat(rate["since"]).timestamp()
        rate["ends_at"] = datetime.fromisoformat(rate["until"]).timestamp() if rate["until"] else None
    return value


def price_at(model, timestamp):
    return next((r for r in price_catalog()["versions"] if r["model"] == model
                 and r["starts_at"] <= timestamp and (r["ends_at"] is None or timestamp < r["ends_at"])), None)


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS cost_attempts (
            request_id TEXT NOT NULL, attempt INTEGER NOT NULL,
            started_at REAL NOT NULL, updated_at REAL NOT NULL,
            model TEXT NOT NULL, client_id TEXT NOT NULL, conversation_id TEXT,
            status TEXT NOT NULL, measurement TEXT NOT NULL, total_nano INTEGER,
            payload_json TEXT NOT NULL, PRIMARY KEY(request_id,attempt));
        CREATE INDEX IF NOT EXISTS cost_attempts_time ON cost_attempts(started_at,request_id,attempt);
        CREATE INDEX IF NOT EXISTS cost_attempts_client ON cost_attempts(client_id,started_at);
        CREATE INDEX IF NOT EXISTS cost_attempts_conversation ON cost_attempts(conversation_id,started_at);
        CREATE TABLE IF NOT EXISTS cost_trace_state (request_id TEXT PRIMARY KEY, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS cost_bill_items (
            bill_id TEXT PRIMARY KEY, day TEXT NOT NULL, model TEXT NOT NULL,
            category TEXT NOT NULL, adjustment INTEGER NOT NULL,
            tokens INTEGER NOT NULL, rate_nano INTEGER NOT NULL, amount_nano INTEGER NOT NULL,
            requests INTEGER, imported_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS cost_bill_day ON cost_bill_items(day,model);
        CREATE TABLE IF NOT EXISTS cost_analyses (
            request_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
    """)


def attempt_records(trace):
    attempts = trace.get("attempts", [])
    for index, attempt in enumerate(attempts):
        steps = [s for s in attempt.get("steps", []) if s.get("node_id") == "upstream_request"]
        if not steps:
            continue  # Rejections before dispatch have no upstream cost.
        evidence = {}
        for step in steps:
            evidence.update(step.get("evidence", {}))
        model = model_id(evidence.get("selected_model"))
        if not model and index == len(attempts) - 1:
            model = model_id(trace.get("selected_model"))
        if not model:
            continue
        start = steps[0].get("timestamp") or attempt.get("started_at") or trace["started_at"]
        status = trace.get("status", "running") if index == len(attempts) - 1 else "failed"
        if steps[-1].get("status") == "passed":
            status = "succeeded"
        backend = evidence.get("backend_usage") or {}
        inputs, cached = token_count(backend.get("input_tokens")), token_count(backend.get("cached_tokens"))
        output = token_count(evidence.get("output_tokens"))
        complete = (backend.get("state") == "complete" and inputs is not None
                    and cached is not None and cached <= inputs and output is not None
                    and status in TERMINAL
                    and evidence.get("output_tokens_measured", bool(output)) is True)
        if evidence.get("output_tokens_measured", bool(output)) is not True:
            output = None
        rate = price_at(model, start)
        measurement = "measured" if complete and rate else "pending" if status not in TERMINAL else "unknown"
        amounts = {}
        if measurement == "measured":
            amounts = {"input_nano": (inputs-cached)*rate["input"], "cached_nano": cached*rate["cached"],
                       "output_nano": output*rate["output"], "saving_nano": cached*(rate["input"]-rate["cached"])}
            amounts["total_nano"] = sum(amounts[k] for k in ("input_nano", "cached_nano", "output_nano"))
        if amounts.get("total_nano", 0) > 2**63-1:
            amounts, measurement = {}, "unknown"
        effective = next((s.get("sha256") for s in trace.get("observation", {}).get("content", {}).get("stages", [])
                          if s.get("stage") == "effective"), None)
        yield {
            "request_id": trace["request_id"], "attempt": attempt.get("number", index+1),
            "started_at": start, "request_started_at": trace["started_at"],
            "updated_at": trace.get("updated_at", start), "model": model,
            "client_id": trace.get("client_id", ""), "conversation_id": trace.get("conversation_id"),
            "status": status, "measurement": measurement, "currency": "CNY", "billing_mode": "usage",
            "usage_source": "upstream_usage" if complete else "unavailable",
            "input_tokens": inputs, "cached_tokens": cached, "output_tokens": output,
            "uncached_tokens": inputs-cached if complete else None,
            "price_version": rate["id"] if rate else None, "rates": rate,
            "unknown_reason": None if measurement == "measured" else "amount_out_of_range" if complete and rate else "price_unavailable" if complete else "usage_incomplete",
            "lineage_relation": trace.get("lineage_relation"), "effective_hash": effective,
            "attempts": len(attempts), **amounts,
        }


def consume_trace(db, trace):
    for record in attempt_records(trace):
        old = db.execute("SELECT payload_json FROM cost_attempts WHERE request_id=? AND attempt=?",
                         (record["request_id"], record["attempt"])).fetchone()
        if old:
            previous = json.loads(old[0])
            if previous["updated_at"] > record["updated_at"] or (previous["measurement"] == "measured" and record["measurement"] != "measured"):
                continue
            if previous["status"] in TERMINAL and record["status"] not in TERMINAL:
                continue
        db.execute("""INSERT INTO cost_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(request_id,attempt) DO UPDATE SET updated_at=excluded.updated_at,
            started_at=excluded.started_at,model=excluded.model,client_id=excluded.client_id,conversation_id=excluded.conversation_id,
            status=excluded.status,measurement=excluded.measurement,total_nano=excluded.total_nano,
            payload_json=excluded.payload_json""", (
            record["request_id"], record["attempt"], record["started_at"], record["updated_at"], record["model"],
            record["client_id"], record["conversation_id"], record["status"], record["measurement"],
            record.get("total_nano"), json.dumps(record, ensure_ascii=False, separators=(",", ":"))))
    db.execute("INSERT INTO cost_trace_state VALUES (?,?) ON CONFLICT(request_id) DO UPDATE SET updated_at=max(updated_at,excluded.updated_at)",
               (trace["request_id"], trace.get("updated_at", trace["started_at"])))


def public_record(record):
    result = {k:v for k,v in record.items() if k not in {"effective_hash"} and not k.endswith("_nano")}
    for k in ("input", "cached", "output", "saving", "total"):
        result[k + "_cny"] = money(record.get(k + "_nano"))
    return result


def aggregate(records):
    measured = [r for r in records if r["measurement"] == "measured"]
    result = {"requests": len({r["request_id"] for r in records}), "attempts": len(records),
              "measured_attempts": len(measured), "pending_attempts": sum(r["measurement"] == "pending" for r in records),
              "unknown_attempts": sum(r["measurement"] == "unknown" for r in records),
              "coverage": len(measured)/len(records) if records else None, "currency": "CNY"}
    for key in ("input", "cached", "output", "saving", "total"):
        result[key+"_cny"] = money(sum(r.get(key+"_nano", 0) for r in measured))
    for key in ("input_tokens", "cached_tokens", "uncached_tokens", "output_tokens"):
        result[key] = sum(r[key] for r in measured)
    result["cache_ratio"] = result["cached_tokens"]/result["input_tokens"] if result["input_tokens"] else None
    result["complete"] = len(records) == len(measured)
    return result


class CostLedger:
    def __init__(self, path):
        self.path = str(path)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def initialize(self):
        with closing(self.connect()) as db, db:
            initialize(db)

    def backfill(self, limit=200):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT t.payload_json FROM route_traces t LEFT JOIN cost_trace_state s
                ON s.request_id=t.request_id WHERE s.request_id IS NULL OR s.updated_at<t.updated_at
                ORDER BY t.started_at,t.request_id LIMIT ?""", (limit,)).fetchall()
            for row in rows:
                consume_trace(db, json.loads(row[0]))
        return len(rows)

    def records(self, *, since, until, model=None, client_id=None, conversation_id=None, request_id=None, measurement=None):
        where = ["started_at>=?", "started_at<?"]
        args = [since, until]
        for key, value in (("model", model), ("client_id", client_id), ("conversation_id", conversation_id),
                           ("request_id", request_id), ("measurement", measurement)):
            if value:
                where.append(key+"=?")
                args.append(value)
        with closing(self.connect()) as db:
            rows = db.execute("SELECT payload_json FROM cost_attempts WHERE "+" AND ".join(where)+
                              " ORDER BY started_at DESC,request_id,attempt", args).fetchall()
        return [json.loads(r[0]) for r in rows]

    def summary(self, **filters):
        records = self.records(**filters)
        groups = defaultdict(list)
        for r in records:
            groups[(r["client_id"], r["conversation_id"] or r["request_id"])].append(r)
        top = [{"client_id": k[0], "conversation_id": v[0]["conversation_id"], "request_id": v[0]["request_id"], **aggregate(v)} for k,v in groups.items()]
        top.sort(key=lambda r: Decimal(r["total_cny"]), reverse=True)
        now = datetime.now(BJT)
        base = {k:v for k,v in filters.items() if k not in {"since", "until"}}
        today = self.records(since=now.replace(hour=0,minute=0,second=0,microsecond=0).timestamp(), until=now.timestamp(), **base)
        month = self.records(since=now.replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp(), until=now.timestamp(), **base)
        with closing(self.connect()) as db:
            left = db.execute("SELECT count(*) FROM route_traces t LEFT JOIN cost_trace_state s ON s.request_id=t.request_id WHERE s.request_id IS NULL OR s.updated_at<t.updated_at").fetchone()[0]
        return {**aggregate(records), "today": aggregate(today), "month": aggregate(month),
                "top_conversations": top[:20], "backfill_remaining": left, "prices": price_catalog(),
                "budget_enforcement": False, "basis": "upstream_usage_estimate"}

    def requests(self, *, limit=50, offset=0, sort="cost", **filters):
        records = self.records(**filters)
        if sort == "cost":
            records.sort(key=lambda r: (r.get("total_nano", -1), r["started_at"], r["request_id"], r["attempt"]), reverse=True)
        items = []
        with closing(self.connect()) as db:
            for r in records[offset:offset+limit]:
                item = public_record(r)
                flags = []
                if r["attempts"] > 1:
                    flags.append({"state": "review", "code": "multiple_attempts", "label": "多次上游尝试，需检查"})
                if r.get("effective_hash"):
                    duplicates = db.execute("""SELECT request_id FROM cost_attempts WHERE client_id=? AND model=?
                        AND conversation_id IS ? AND request_id<>? AND abs(started_at-?)<=60
                        AND json_extract(payload_json,'$.effective_hash')=? LIMIT 5""",
                        (r["client_id"],r["model"],r["conversation_id"],r["request_id"],r["started_at"],r["effective_hash"])).fetchall()
                    if duplicates:
                        flags.append({"state": "review", "code": "duplicate_payload", "label": "一分钟内相同有效请求，需检查", "request_ids": [x[0] for x in duplicates]})
                analysis = db.execute("SELECT payload_json FROM cost_analyses WHERE request_id=?", (r["request_id"],)).fetchone()
                analysis = json.loads(analysis[0]) if analysis else {}
                if analysis.get("attempt") == r["attempt"] and analysis.get("version") == 1:
                    flags.extend(analysis.get("findings", []))
                else:
                    prefix = db.execute("SELECT payload_json FROM prefix_breaks WHERE request_id=? AND attempt=?", (r["request_id"],r["attempt"])).fetchone()
                    change = json.loads(prefix[0]) if prefix else {}
                    fields = [f for f in change.get("forwarded_fields", []) if f != "messages.length"]
                    if fields:
                        flags.append({"state":"review", "code":"prefix_change", "label":"检测到前缀变化，需下钻核对原因",
                                      "previous_request_id":change.get("previous_request_id"), "fields":fields})
                item["findings"] = flags
                items.append(item)
        return {"items": items, "total": len(records), "next_offset": offset+limit if offset+limit<len(records) else None}

    def import_bill(self, rows):
        inserted = 0
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for row in rows:
                fields = (row["bill_id"],row["day"],row["model"],row["category"],row["adjustment"],row["tokens"],row["rate_nano"],row["amount_nano"],row["requests"])
                old = db.execute("SELECT bill_id,day,model,category,adjustment,tokens,rate_nano,amount_nano,requests FROM cost_bill_items WHERE bill_id=?", (row["bill_id"],)).fetchone()
                if old:
                    if tuple(old) != fields:
                        raise ValueError("同一账单标识的内容不同，请先核对账单版本")
                    continue
                db.execute("INSERT INTO cost_bill_items VALUES (?,?,?,?,?,?,?,?,?,?)", (*fields,time.time()))
                inserted += 1
        return {"inserted": inserted, "duplicates": len(rows)-inserted, "rows": len(rows)}

    def reconciliation(self, *, since, until, model=None):
        groups = defaultdict(list)
        for r in self.records(since=since, until=until, model=model):
            groups[(datetime.fromtimestamp(r["started_at"], BJT).date().isoformat(), r["model"])].append(r)
        start = datetime.fromtimestamp(since, BJT).date().isoformat()
        end = datetime.fromtimestamp(until-0.001, BJT).date().isoformat()
        with closing(self.connect()) as db:
            bills = db.execute("SELECT * FROM cost_bill_items WHERE day>=? AND day<=?"+(" AND model=?" if model else ""),
                               (start,end,*([model] if model else []))).fetchall()
        by_bill = defaultdict(list)
        for row in bills:
            by_bill[(row["day"],row["model"])].append(row)
        result = []
        for key in sorted(set(groups)|set(by_bill)):
            values, official = groups[key], by_bill[key]
            summary = aggregate(values)
            gross = sum(r["amount_nano"] for r in official if not r["adjustment"])
            adjustment = sum(r["amount_nano"] for r in official if r["adjustment"])
            measured_total = sum(r.get("total_nano",0) for r in values if r["measurement"] == "measured")
            token_sums = {category:sum(r["tokens"] for r in official if r["category"]==category and not r["adjustment"])
                          for category in ("input","cached","output")}
            result.append({"day":key[0],"model":key[1],"router":summary,"official_tokens":token_sums if official else None,
                           "official_gross_cny":money(gross) if official else None,
                           "adjustment_cny":money(adjustment) if official else None,
                           "official_net_cny":money(gross+adjustment) if official else None,
                           "difference_cny":money(gross+adjustment-measured_total) if official else None,
                           "comparison_complete":bool(official and values and summary["complete"]),
                           "scope":"day_model", "router_day_basis":"upstream_dispatch"})
        return {"items":result, "currency":"CNY", "timezone":"Asia/Shanghai"}


async def backfill_costs(path):
    ledger = CostLedger(path)
    while True:
        try:
            count = await asyncio.to_thread(ledger.backfill)
        except Exception:
            logging.getLogger(__name__).exception("Historical cost backfill paused; unprocessed traces remain visible")
            count = 0
        await asyncio.sleep(0.05 if count else 5)
