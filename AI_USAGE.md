# AI Usage

- **Tools used:** Claude Code (Anthropic)

- **Where they helped most:** Generating boilerplate-heavy but correctness-critical code — SQLAlchemy models, repository functions, retry/backoff logic, and the policy egress transforms. Also useful for keeping the test suite consistent with implementation changes across all six test files simultaneously.

- **Something they got wrong that you caught, and how you caught it:** The context assembler and reconciliation rules were checking `drop.status == "accepted"` but the custodian connector sets status to `"ingested"`. This caused the live service to return $0 AUM for all households. Caught by running an end-to-end curl against H-1001 after the first live sync and seeing zero totals, then tracing it back through the assembler to the status check.

- **Anything you chose to write entirely by hand, and why:** The SHA-256 and row-count verification of the actual custodian files — done manually in PowerShell to confirm the Oct-8 v1 and v2 account files are byte-for-byte identical despite the assignment description implying they differ. This was a data integrity question that needed direct verification, not code generation.
