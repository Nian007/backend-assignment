# Pravar AI · Senior Backend Engineer Assignment

## Household Context Service

Thank you for spending time with us. This exercise is a compressed version of the work you would do in your first quarter at Pravar, and we have tried to make it feel like that work rather than like a puzzle.

Please read the whole brief before you write any code. The order in which you tackle it matters to us, and the first thing we will read is your design document, not your source tree.

**Time box:** two working days, roughly 12 to 16 hours of focused effort. If you find yourself well past that, stop, and tell us in `DESIGN.md` what you would have done next. A well-reasoned plan for unfinished work earns more credit than an unfinished implementation of everything.

---



## 1. The situation

Pravar runs teams of AI agents for wealth management firms. An agent that prepares an advisor for a client review needs a single, trustworthy picture of the household: who the people are, which accounts they own, what those accounts hold, and how much of that picture can be believed today.

None of that lives in one place. At our design partner, a mid-sized RIA, relationship data sits in a CRM and holdings data arrives as nightly file drops from the custodian. The two systems were never designed to agree. Account numbers are formatted differently, people are entered twice, files are redelivered with corrections, and the custodian knows about accounts the CRM has not caught up with yet.

Your job is to build the service that sits between those systems of record and our agents. It ingests both sources, reconciles them into a canonical model, applies the firm's data policy, and serves agents a household context they can act on, along with an honest account of what is stale, missing or broken.

We care less about how many features you finish than about whether the thing you build is the beginning of a platform. The design partner will have a third source by next month and a tenth by next year. Build so that adding one is a small, boring change.

## 2. What is in this repository


| Path                              | What it is                                                                                                                                                                 |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mock_crm/`                       | A runnable mock of "Ledgerline CRM", a third-party REST API. It authenticates, paginates, rate limits, fails intermittently and soft deletes, the way real vendor APIs do. |
| `custodian_drops/`                | Three nightly file drops from "Meridian Custody". Each folder holds `accounts.csv`, `positions.csv`, `balances.csv` and a `manifest.json`.                                 |
| `contracts/crm-api.md`            | The CRM vendor's API documentation.                                                                                                                                        |
| `contracts/custodian-feed.md`     | The custodian's file specification.                                                                                                                                        |
| `contracts/canonical-ontology.md` | The minimum canonical entities Pravar's agents expect. You will extend it.                                                                                                 |
| `contracts/agent-context-api.md`  | The contract for the endpoint our agents will call. Treat this as fixed.                                                                                                   |
| `contracts/data_policy.yaml`      | The firm's data policy. Your service must enforce it.                                                                                                                      |
| `DESIGN.md`                       | A template for your design document.                                                                                                                                       |
| `AI_USAGE.md`                     | A short template describing how you used AI tools.                                                                                                                         |


Start the mock CRM with:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn mock_crm.server:app --port 8081
# Interactive docs at http://localhost:8081/docs
```

The CRM token is `pravar-dev-token`. Set `CRM_FAULT_RATE=0` and a high `CRM_RATE_LIMIT` when you want deterministic tests, but your connector must survive the defaults.

## 3. What to build



### Required

**1. A design document, written first.** Fill in `DESIGN.md` before you build, then revise it as you learn. Commit the first version separately so we can see how your thinking moved. We expect two to four pages. It should cover the architecture, the canonical model, the connector contract, sync semantics and failure handling, how the data policy is enforced, and what you deliberately left out.

**2. A connector framework and two connectors.** One connector pulls from the CRM API. The other ingests custodian file drops. Both should implement a common contract that you define, so that a third source requires a new connector and some configuration, and nothing else. We will ask you to add a third source during the follow-up conversation.

**3. A canonical store with correct sync semantics.** Persist into a canonical model of your design (PostgreSQL or SQLite are both fine). Syncs must be idempotent: running the same sync twice, or processing the same file twice, must leave the store in the same state. Incremental CRM syncs should resume from where the last successful run ended. File drops can arrive late, out of order or as corrected redeliveries, and the store must end up reflecting the truth.

**4. Reconciliation and data quality.** Reconcile custodian positions against custodian balances, and CRM relationships against custodian accounts. Surface what you find as structured findings with a severity, a reason and enough lineage to trace each one back to the source record. Some of the data in this repository is wrong on purpose. Finding it is part of the exercise, and so is deciding what to do about it.

**5. The agent context API.** Implement `GET /v1/households/{household_id}/context` exactly as specified in `contracts/agent-context-api.md`, with authentication, the two principals described in the policy, and the field-level rules in `data_policy.yaml` applied before anything leaves the service.

**6. Observability you would want at 3 a.m.** Every sync run should be recorded with its outcome, counts and errors. Logs should be structured and carry a correlation ID across a run and across a request.

**7. Tests for the parts that are hard to get right.** We are not looking for coverage numbers. We want to see that you know where the bugs live.

### Optional (choose at most one, and only if the required work is solid)

- A notification hook that fires when a reconciliation break crosses a threshold, behind an interface that could later send to Slack, email or a queue.
- A metrics endpoint (Prometheus format is fine) for sync latency, record counts and finding counts.
- Crash safety: demonstrate that a sync killed halfway through recovers cleanly on the next run.
- A `docker compose up` that brings up the mock CRM, your service and its database.



### Good to have

A UI is not required, but good to have, primarily to demostrate the execution flow. 

## 4. Constraints

Use Python 3.11 or later. Choose any framework and libraries you like, and be ready to explain why. Your service should run locally with a single documented command plus the mock CRM.

You are welcome to use AI coding tools; we use them every day. You own every line regardless of who typed it, and in the follow-up conversation we will ask you to explain and modify specific parts of your code. Fill in `AI_USAGE.md` briefly and honestly.

Treat the data in this repository as if it were real client data. The tax IDs are fictitious, but your handling of them is not.

## 5. What to submit

Send us a link to a private Git repository (or a zip with the `.git` folder intact) containing:

- your code, with a `README` section explaining how to run the service, the syncs and the tests;
- `DESIGN.md`, with the first draft committed early;
- `AI_USAGE.md`;
- a `FINDINGS.md` listing the data problems your service detected in the sample data, what it did with each, and anything you noticed that your service does not yet catch.

Please keep your commit history. 

## 6. What happens next

We will schedule an onsite with you. You will walk us through your design, we will discuss the trade-offs you made, and we will add a third data source together, live, in your codebase.

## 7. How we will evaluate

In rough order of weight:

1. **Planning and design.** Is the problem decomposed well? Are the trade-offs named and defended? Does the design doc match what was built?
2. **Correctness and data integrity.** Does the agent receive numbers it can trust? Are idempotency, ordering and reconciliation handled correctly?
3. **Platform thinking.** Is the connector contract a real abstraction? Would the tenth source be cheaper than the second?
4. **Security and governance.** Is the data policy enforced structurally rather than by remembering to call a function? What sensitive data is stored, logged or returned, and why?
5. **Code quality and tests.** Is the code readable, and do the tests target the risky paths?
6. **Communication.** Are the documents clear and honest about limits?

If anything in this brief is ambiguous, make a reasonable assumption, write it down in `DESIGN.md` and keep going. Resolving ambiguity sensibly is part of the job. If something is genuinely broken, email us.

Good luck. We are looking forward to reading your work.