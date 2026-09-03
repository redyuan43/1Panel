from __future__ import annotations


class RouterError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        code: str = "router_error",
        headers: dict[str, str] | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.headers = headers or {}
        self.details = details or {}


class AuthenticationError(RouterError):
    def __init__(self, message: str = "invalid API key") -> None:
        super().__init__(message, status_code=401, code="invalid_api_key")


class ConversationBusyError(RouterError):
    def __init__(self) -> None:
        super().__init__(
            "another request is already running for this conversation",
            status_code=409,
            code="conversation_busy",
        )


class ConversationStateConflictError(RouterError):
    def __init__(self) -> None:
        super().__init__(
            (
                "the supplied conversation history does not match the stored "
                "conversation history boundary"
            ),
            status_code=409,
            code="conversation_state_conflict",
        )


class NoEligibleModelError(RouterError):
    def __init__(self, message: str = "no eligible model is available") -> None:
        super().__init__(message, status_code=503, code="no_eligible_model")


class NoCompatibleModelError(RouterError):
    def __init__(
        self,
        message: str = "no model can satisfy the complete request constraints",
    ) -> None:
        super().__init__(
            message,
            status_code=422,
            code="no_compatible_model",
        )


class HistoryMigrationRequiredError(RouterError):
    def __init__(
        self,
        message: str = (
            "conversation history requires explicit compaction before it can "
            "move to another provider"
        ),
    ) -> None:
        super().__init__(
            message,
            status_code=409,
            code="history_migration_required",
        )


class QueueTimeoutError(RouterError):
    def __init__(self) -> None:
        super().__init__(
            "the selected model queue did not become available in time",
            status_code=429,
            code="model_queue_timeout",
        )


class CapacityBusyError(RouterError):
    def __init__(
        self,
        message: str = "the selected model capacity is busy",
        *,
        code: str = "model_capacity_busy",
    ) -> None:
        super().__init__(
            message,
            status_code=429,
            code=code,
            headers={"Retry-After": "1"},
        )


class AllLocalCapacityBusyError(CapacityBusyError):
    def __init__(self) -> None:
        super().__init__(
            "all eligible local model capacity is busy",
            code="all_local_capacity_busy",
        )


class TokenizationUnavailableError(RouterError):
    def __init__(self, message: str = "the configured tokenizer is unavailable") -> None:
        super().__init__(message, status_code=503, code="tokenizer_unavailable")


class CompactionUnavailableError(RouterError):
    def __init__(self, message: str = "context compaction is required but unavailable") -> None:
        super().__init__(message, status_code=503, code="compaction_unavailable")


class InvalidToolHistoryError(RouterError):
    def __init__(
        self,
        message: str,
        *,
        item_index: int,
        candidate_count: int,
        reason: str,
    ) -> None:
        super().__init__(
            message,
            status_code=400,
            code="invalid_tool_history",
            details={
                "item_index": item_index,
                "candidate_count": candidate_count,
                "reason": reason,
            },
        )


class TrainingArchiveUnavailableError(RouterError):
    def __init__(self, message: str = "encrypted training archive is unavailable") -> None:
        super().__init__(
            message,
            status_code=503,
            code="training_archive_unavailable",
        )
