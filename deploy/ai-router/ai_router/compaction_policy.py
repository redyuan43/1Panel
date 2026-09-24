"""One master gate for every Router-owned compaction entry point."""
from .errors import CompactionUnavailableError


def compaction_enabled(settings):
    section = settings.section("compaction")
    return section.get("enabled", False) is True and section.get("mode") != "disabled"


def require_compaction_enabled(settings, *, refresh=False):
    # Re-read the persisted switch immediately before a new model call.
    if refresh and callable(getattr(settings, "reload", None)):
        settings.reload()
    if not compaction_enabled(settings):
        raise CompactionUnavailableError("Router compaction is disabled")
