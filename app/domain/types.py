from enum import Enum


class Principal(str, Enum):
    AGENT = "agent"
    OPS = "ops"


class SyncSource(str, Enum):
    CRM = "ledgerline_crm"
    CUSTODIAN = "meridian_custody"


class FindingSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DataQualityStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    UNRELIABLE = "unreliable"


class AccountStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class DropStatus(str, Enum):
    PENDING = "pending"
    INGESTED = "ingested"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class SyncRunStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
