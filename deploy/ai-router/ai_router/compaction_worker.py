"""Router-owned background compaction, with per-call admission and cleanup."""
from __future__ import annotations

import asyncio
from uuid import uuid4

from .compaction_checkpoint import CheckpointedCompactor
from .compaction import SummaryNotSentError, SummaryResponseError
from .compaction_jobs import CompactionJobs, CompactionJobConflict
from .compaction_limits import job_limits
from .errors import CompactionUnavailableError
from .memory_service import _thread
from .routing_modes import resolve
from .types import Evaluation, RequestCapabilities
from .summary_profile import summary_profile


async def _summary_input_budget(current, endpoint, output_tokens):
    """Use a budget every currently ready physical summary worker can honor."""
    context_tokens = [int(endpoint.safe_context_tokens)]
    health = getattr(current, "health", None)
    if health is not None and hasattr(health, "status"):
        status = await health.status(endpoint)
        for worker in status.detail.get("workers", []):
            if not worker.get("ready"):
                continue
            try:
                value = int(worker.get("safe_context_tokens", 0))
            except (TypeError, ValueError):
                continue
            if value > 0:
                context_tokens.append(value)
    budget = min(context_tokens) - int(output_tokens)
    if budget < 1024:
        raise CompactionUnavailableError(
            "ready summary workers have insufficient context"
        )
    return budget


def _refresh_background_settings(current):
    """Use the foreground reload path when a Control process changes settings."""
    settings = current.settings
    if hasattr(settings, "defaults_path"):
        def stamp(path):
            try:
                value = path.stat()
                return value.st_ino, value.st_mtime_ns, value.st_size
            except FileNotFoundError:
                return None
        version = stamp(settings.defaults_path), stamp(settings.runtime_path)
        if version != getattr(current, "_background_settings_version", None):
            # Keep policy, budget and the compactor on one validated snapshot.
            # This is synchronous, as in the foreground request reload path.
            current.reload_settings()
            current._background_settings_version = version
    section = current.settings.section("compaction")
    return (section.get("background_enabled", False) and section.get("enabled", True)
            and section.get("mode") != "disabled")


def validate_background_settings(settings, registry):
    compaction = settings.get("compaction", {})
    profile = summary_profile(compaction, registry.by_id(str(compaction.get("model_id", ""))))
    background = compaction.get("background_enabled", False)
    rewrite = compaction.get("history_query_rewrite_enabled", False)
    if not background and not rewrite:
        return
    if background and (not compaction.get("enabled", True) or compaction.get("mode") == "disabled"):
        raise ValueError("background compaction requires enabled compaction")
    endpoint = registry.by_id(str(compaction.get("model_id", "")))
    if not endpoint or not endpoint.enabled or endpoint.safe_context_tokens <= profile.output_tokens + 1024:
        raise ValueError("background compaction requires an enabled summary model with sufficient context")
    if endpoint.cloud:
        cloud = settings.get("cloud", {})
        if (not cloud.get("enabled") or endpoint.public_model not in cloud.get("allowed_models", [])
                or endpoint.metadata.get("provider") not in cloud.get("allowed_providers", [])):
            raise ValueError("background summary model requires explicit cloud model and provider authorization")
        if endpoint.metadata.get("billing_mode") != "subscription" and float(cloud.get("monthly_budget", 0)) <= 0:
            raise ValueError("background cloud summary model requires a positive budget")


class RoutedCompactor(CheckpointedCompactor):
    def __init__(self, *args, runtime, **kwargs):
        kwargs["summary_profile"] = summary_profile(kwargs["job"]["parameters"],
            runtime.registry.by_id(kwargs["model_id"]))
        super().__init__(*args, **kwargs)
        self.runtime = runtime

    def _profile_current(self):
        return self.summary_profile == summary_profile(self.runtime.settings.section("compaction"),
            self.runtime.registry.by_id(self.model_id))

    def _before_summary_send(self):
        # Check after checkpoint journal awaits, immediately before HTTP I/O.
        current = self.runtime
        if not _refresh_background_settings(current) or current.compactor.model_id != self.model_id:
            raise SummaryNotSentError("background compaction settings changed before send")
        if not self._profile_current():
            raise SummaryNotSentError("summary reasoning changed before send; regenerate candidate")

    async def _summarize(self, messages, *, target=None):
        from .api import _acquire_internal_model
        current = self.runtime
        if not _refresh_background_settings(current) or current.compactor.model_id != self.model_id:
            raise CompactionUnavailableError("background compaction settings changed")
        if not self._profile_current():
            raise CompactionUnavailableError("summary reasoning changed; regenerate candidate")
        owner = self.job["owner"]
        key_id = self.job["parameters"].get("key_id", "")
        policy = await current.clients.current_policy(owner)
        if not policy or not policy.allow_compaction or not await current.clients.is_key_active(owner, key_id):
            raise CompactionUnavailableError("background compaction account authorization revoked")
        endpoint = current.registry.by_id(self.model_id)
        if not endpoint or not endpoint.enabled:
            raise CompactionUnavailableError("background compaction model unavailable")
        request = self._summary_request(messages, None)
        tokens = self.token_counter.count_request(request, "chat")
        routing_options = resolve(
            current.settings.section("routing"),
            policy.routing_mode,
            policy.local_only or self.job["parameters"].get("local_only", True),
        )
        decision = await current.policy.choose(requested_model=endpoint.public_model,
            evaluation=Evaluation("general", None, 1.0, "background_compaction"),
            prompt_tokens=tokens, output_reserve_tokens=self.summary_output_tokens,
            modalities={"text"}, has_tools=False, required_capabilities=RequestCapabilities(protocol="chat"),
            conversation=None, client_id=owner, routing_key=self.job["id"],
            routing_options=routing_options)
        if decision.endpoint.id != endpoint.id:
            raise CompactionUnavailableError("background compaction cannot silently change models")
        call_id = self.job["id"] + ":" + uuid4().hex
        lease = await current.scheduler.begin_request(None)
        reservation = None
        started_calls = None
        parallel = False
        try:
            started_calls = (await _thread(self.jobs.read, owner, self.job["id"]))["calls"]
            parallel = await current.limiter.acquire_parallel(owner, call_id, policy.max_parallel_requests)
            if not parallel:
                raise CompactionUnavailableError("background compaction account concurrency limit exceeded")
            target = await _acquire_internal_model(current, lease=lease, request_id=call_id,
                model_id=self.model_id, wait=False, prompt_tokens=tokens,
                output_reserve_tokens=self.summary_output_tokens,
                routing_options=routing_options)
            request = self._summary_request(messages, target)
            tokens = self.token_counter.count_request(request, "chat")
            if (
                target.safe_context_tokens is not None
                and tokens + self.summary_output_tokens
                > target.safe_context_tokens
            ):
                raise CompactionUnavailableError(
                    "summary request exceeds the selected worker context"
                )
            cached = await _thread(self.jobs.cached_step, self.job["id"], self.worker,
                                  self.step_key(request, target))
            if cached is not None:
                return cached
            allowed, _ = await current.limiter.check_rate_limits(owner, prompt_tokens=tokens,
                rpm_limit=policy.rpm_limit, tpm_limit=policy.tpm_limit)
            if not allowed:
                raise CompactionUnavailableError("background compaction account rate limit exceeded")
            reservation = await current.budget.reserve(endpoint, request_id=call_id,
                prompt_tokens=tokens, output_reserve_tokens=self.summary_output_tokens)
            if not _refresh_background_settings(current) or current.compactor.model_id != self.model_id:
                raise CompactionUnavailableError("background compaction settings changed during admission")
            if not self._profile_current():
                raise CompactionUnavailableError("summary reasoning changed during admission; regenerate candidate")
            policy = await current.clients.current_policy(owner)
            if (not policy or not policy.allow_compaction or (endpoint.cloud and resolve(
                    current.settings.section("routing"), policy.routing_mode,
                    policy.local_only or self.job["parameters"].get("local_only", True))["local_only"])
                    or not await current.clients.is_key_active(owner, key_id)):
                raise CompactionUnavailableError("background compaction authorization changed during admission")
            # Admission may yield while endpoint configuration is replaced.
            # Do not send to a disabled or changed target using stale approval.
            latest_endpoint = current.registry.by_id(self.model_id)
            if not latest_endpoint or not latest_endpoint.enabled or latest_endpoint != endpoint:
                raise CompactionUnavailableError("background compaction model changed during admission")
            self._before_summary_send()
            return await super()._summarize(messages, target=target)
        finally:
            try:
                state = await _thread(self.jobs.read, owner, self.job["id"])
                # Unknown outcomes conservatively consume the reservation.
                if state and started_calls is not None and state["calls"] > started_calls:
                    await current.budget.commit(reservation)
                else:
                    await current.budget.release(reservation)
            finally:
                try:
                    await lease.release()
                finally:
                    if parallel:
                        await current.limiter.release_parallel(owner, call_id)


class CompactionWorker:
    def __init__(self, runtime):
        self.runtime = runtime
        self.jobs = None

    def _open(self):
        self.jobs = CompactionJobs(self.runtime.settings.runtime_path.with_name("compaction-jobs.sqlite3"),
                                  self.runtime.state_encryption_key)

    async def tick(self):
        current = self.runtime
        if not _refresh_background_settings(current):
            return False
        if self.jobs is None:
            await _thread(self._open)
        worker = uuid4().hex
        job = await _thread(self.jobs.claim, worker)
        if not job:
            return False
        async def execute():
            model_id = job["parameters"].get("model_id", "")
            if model_id != current.compactor.model_id:
                raise CompactionUnavailableError("configured summary model changed; regenerate candidate")
            endpoint = current.registry.by_id(model_id)
            if not endpoint or not endpoint.enabled or endpoint.safe_context_tokens <= 9216:
                raise CompactionUnavailableError("summary model has insufficient context")
            compactor = RoutedCompactor(current.token_counter, current.compactor.cipher,
                internal_base_url=current.internal_base_url, internal_api_key=current.internal_api_key,
                model_id=model_id, client=current.compactor.client,
                jobs=self.jobs, job=job, worker=worker, runtime=current)
            from .summary_provenance import SummaryScope, recover_legacy
            parameters = job["parameters"]
            summary_scope = SummaryScope(owner=job["owner"], branch=job["branch"],
                api_kind=job["api_kind"], cipher=current.compactor.cipher,
                ancestors=parameters.get("summary_ancestors", ()),
                protected=parameters.get("summary_protected", ()),
                records=parameters.get("summary_records", ()))
            await recover_legacy(current, summary_scope)
            summary_input_tokens = await _summary_input_budget(
                current,
                endpoint,
                compactor.summary_output_tokens,
            )
            for attempt in range(2):
                try:
                    capsule = await compactor.compact(job["body"], api_kind=job["api_kind"],
                        target_context_tokens=int(job["parameters"]["target_context_tokens"]),
                        summary_input_tokens=summary_input_tokens,
                        summary_scope=summary_scope)
                    break
                except SummaryResponseError as exc:
                    if attempt or not exc.retryable:
                        raise
                    current.audit.write("background_compaction_retry", job_id=job["id"], status_code=exc.status_code)
                    await asyncio.sleep(exc.retry_after)
            await _thread(self.jobs.candidate, job["id"], worker, compactor.cipher.decrypt(capsule.encrypted_messages),
                          summary_indices=capsule.summary_indices)
        async def keep_alive():
            while True:
                await asyncio.sleep(5)
                if not _refresh_background_settings(current):
                    raise CompactionUnavailableError("background compaction disabled")
                await _thread(self.jobs.heartbeat, job["id"], worker)
        task = asyncio.create_task(execute())
        heartbeat = asyncio.create_task(keep_alive())
        try:
            done, _ = await asyncio.wait({task, heartbeat}, timeout=max(0.001, job_limits(job)["max_seconds"] - job.get("elapsed_seconds", 0)),
                                         return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
            elif heartbeat in done:
                await heartbeat
            else:
                raise TimeoutError("background compaction time budget exhausted")
        except BaseException as exc:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            try:
                await _thread(self.jobs.fail, job["id"], worker, type(exc).__name__)
            except CompactionJobConflict:
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            current.audit.write("background_compaction_failed", job_id=job["id"], error_type=type(exc).__name__)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        return True

    async def run(self):
        while True:
            try:
                progressed = await self.tick()
            except Exception as exc:
                self.runtime.audit.write("background_compaction_worker_failed", error_type=type(exc).__name__)
                progressed = False
            await asyncio.sleep(0.1 if progressed else 5)
