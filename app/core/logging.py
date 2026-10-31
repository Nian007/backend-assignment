import logging
import structlog
from structlog.types import EventDict

# Fields that must never appear in logs
_DENY_FIELDS = frozenset({
    "tax_id", "date_of_birth", "dob", "email", "phone", "notes",
    "ssn", "primary_tax_id",
})

# Fields that may appear only as last-4 digits
_LAST4_FIELDS = frozenset({"account_number"})


def _scrub_pii(_logger, _method: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict.keys()):
        if key in _DENY_FIELDS:
            event_dict[key] = "[REDACTED]"
        elif key in _LAST4_FIELDS:
            v = str(event_dict[key])
            digits = "".join(c for c in v if c.isdigit())
            event_dict[key] = f"****{digits[-4:]}" if len(digits) >= 4 else "****"
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _scrub_pii,
            structlog.dev.ConsoleRenderer() if level == "DEBUG" else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
