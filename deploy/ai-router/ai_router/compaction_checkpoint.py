"""Per-job compactor: persist dispatch intent before sending, save validated output."""
from __future__ import annotations

from .compaction import ContextCompactor, SummaryNotSentError, SummaryResponseError
from .compaction_jobs import digest
from .memory_service import _thread
from .compaction_limits import job_limits


class CheckpointedCompactor(ContextCompactor):
    def __init__(self, *args, jobs, job, worker, **kwargs):
        kwargs["work_limits"] = job_limits(job)
        super().__init__(*args, **kwargs)
        self.jobs = jobs
        self.job = job
        self.worker = worker

    def step_key(self, request, target):
        return digest({"request": request,
                       "base_url": target.base_url if target else self.internal_base_url})

    async def _summarize(self, messages, *, target=None):
        if not messages:
            return await super()._summarize(messages, target=target)
        request = self._summary_request(messages, target)
        # Bind cache reuse to actual model, endpoint and full summary prompt;
        # never persist an API key or include it in an operation identifier.
        step_key = self.step_key(request, target)
        cached = await _thread(self.jobs.dispatch, self.job["id"], self.worker,
            step_key, self.token_counter.count_request(request, "chat"), self.summary_output_tokens)
        if cached is not None:
            return cached
        operation_id = await _thread(self.jobs.operation, self.job["id"], self.worker, step_key)
        try:
            result = await super()._summarize(messages, target=target, operation_id=operation_id)
        except SummaryNotSentError:
            await _thread(self.jobs.not_sent_step, self.job["id"], self.worker, step_key)
            raise
        except SummaryResponseError as exc:
            await _thread(self.jobs.failed_step, self.job["id"], self.worker, step_key,
                          exc.status_code, exc.retryable, exc.reason_code)
            raise
        output_tokens = self._summary_output_usage(result)
        await _thread(self.jobs.complete_step, self.job["id"], self.worker,
                      step_key, result, output_tokens)
        return result
