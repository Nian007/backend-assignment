"""
Connector registry.

Adding a third source connector:
  1. Implement ConnectorBase in a new file.
  2. Add an entry to CONNECTOR_FACTORIES below.
  3. No other files need to change.
"""
from app.connectors.base import ConnectorBase, SyncResult
from app.connectors.crm import CRMConnector
from app.connectors.custodian import CustodianConnector
from app.policy.engine import PolicyEngine


def build_crm_connector(policy: PolicyEngine) -> ConnectorBase:
    return CRMConnector(policy=policy)


def build_custodian_connector() -> ConnectorBase:
    return CustodianConnector()


CONNECTOR_FACTORIES = {
    "ledgerline_crm": build_crm_connector,
    "meridian_custody": build_custodian_connector,
}

__all__ = [
    "ConnectorBase",
    "SyncResult",
    "CRMConnector",
    "CustodianConnector",
    "CONNECTOR_FACTORIES",
]
