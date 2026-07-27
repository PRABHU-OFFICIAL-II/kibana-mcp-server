# Kibana Skill

Interact with Kibana/Elasticsearch at `https://kibana.ext.prod.elk.cloudtrust.rocks` via MCP tools. Execute all queries autonomously — never ask the user for permission to run a query, search logs, or drill into data. When a question is asked, gather ALL relevant data first, then present a complete answer.

---

## Step 0 — Always check auth first

Call `mcp__kibana__auth_status` at the start of every Kibana conversation.

If the session is expired or missing, attempt login automatically using credentials from the `.env` file at the project root (`C:\Users\ppenthoi\Documents\DEV\mcp-servers\kibana-mcp-server\.env`):
- `KIBANA_USERNAME` → username (default: `ppenthoi@informatica.com`)
- `KIBANA_PASSWORD` → password

Call `mcp__kibana__login` immediately with those values — **do not ask the user for the password if `.env` contains it.** Then tell the user: "Approve the Okta Verify push on your phone."

If `.env` is missing or `KIBANA_PASSWORD` is blank, ask the user for their Okta password once, use it, and remind them to fill in `.env` so this is automatic next time.

If login fails or push is unavailable: ask the user to inject a session cookie instead — open `https://kibana.ext.prod.elk.cloudtrust.rocks` in a browser → DevTools → Application → Cookies → copy the `sid` value → call `mcp__kibana__inject_session` with `cookie_name="sid"`.

---

## Environment — know this, never ask

**Kibana:** `https://kibana.ext.prod.elk.cloudtrust.rocks` — version 8.19.13

**Spaces confirmed:**
| Space ID | Name | Workloads |
|---|---|---|
| `gcs` | GCS | CDI — Cloud Data Integration workloads (default) |
| `cai` | CAI | CAI — Cloud Application Integration workloads |
| `cdi` | CDI | CDI workloads |
| `dq` | DQ | Data Quality |
| `ccgf` | CCGF | CCGF workloads |
| `claire` | ClaireGPT | Claire/GPT |
| `global` | Global | Global/shared |
| `hostedgcs` | HostedGCS | Hosted GCS |
| `platformops` | PlatformOps | Platform operations |

**Default space:** `gcs`

**Space routing rules (applied automatically — never search both in parallel):**
- Service or query contains `cai-`, `cai_`, `process-server`, `active-vcs`, `active-bpel`, or `cai` keyword → `cai` space only
- Everything else → `global` first; if 0 results → fall back to `gcs`
- User explicitly passes `space=` → always honour it

**Primary index patterns (filebeat intcloud logs):**
- `filebeat-*-intcloud-*` — main application logs (CDI + CAI services)
- `k8s_controlplane-*` — Kubernetes pod events (OOMKills, CrashLoopBackOff, evictions)
- `filebeat-*-dqgcloud-*` — DQ/GCloud logs
- APM: `traces-apm*`, `logs-apm*`, `metrics-apm*`

**Log field reference:**
| Field | Meaning |
|---|---|
| `kubernetes.labels.app` | Service name (e.g. `vcs`, `migration`, `cai-run`) |
| `kubernetes.namespace` | K8s namespace (e.g. `iics-prod-nause6`) |
| `kubernetes.pod.name` | Full pod name |
| `dissect.catalina_out.level` | Log level: `INFO`, `DEBUG`, `WARN`, `ERROR` |
| `dissect.catalina_out.message` | Parsed log message body |
| `dissect.catalina_out.reqid` | Request/correlation ID for tracing |
| `dissect.catalina_out.org` | Org/tenant ID |
| `dissect.catalina_out.app` | App path (e.g. `/vcs`, `/migration`) |
| `dissect.catalina_out.uid` | User ID |
| `dissect.catalina_out.un` | Username |
| `CT_PODNAME` | Pod group (e.g. `AWS-PROD-USE1-POD6`) |
| `POD_cluster_name` | Cluster name (e.g. `use6`, `use1`) |

**Known services (non-exhaustive):**
- CDI/GCS: `vcs`, `migration`, `frs`, `runtime`, `session-service`, `datasync`, `di-service`
- CAI: `cai-run`, `process-server`, `active-vcs`, `active-bpel`
- Platform: `iics-cai`, `iics-platform`

---

## Step 1 — Space routing before any log query

**Never hardcode a space. Never search both spaces in parallel. Always route first.**

```
Does the service/query contain cai-, cai_, process-server, active-vcs, active-bpel, or "cai" keyword?
  YES → space = "cai"  (search cai only)
  NO  → search "global" first
          → got results? use them
          → 0 results? fall back to "gcs"
```

This is handled automatically by the tools — you never need to specify a space unless you want to force one.

---

## Step 2 — ALWAYS gather context before diagnosing

For any diagnostic question, collect these in parallel before drawing conclusions:

```
mcp__kibana__investigate_service_errors(service, range_minutes=60)  → recent ERRORs/WARNs
mcp__kibana__compare_log_volume(service, baseline_minutes=60, comparison_minutes=60)  → volume trend
```

If the user provides a request ID: also run `mcp__kibana__trace_request(reqid)` immediately.
If the user provides an org/tenant ID: also run `mcp__kibana__search_by_org(org_id)`.
If pods are crashing or restarting: run `mcp__kibana__investigate_pod_health(service)`.

---

## Timezone — ALWAYS IST (UTC+5:30)

**All timestamps in every query and every response must be in IST (Indian Standard Time = UTC+5:30).**

Before firing any query:
1. If the user gives a time in any other timezone (UTC, PDT, EST, etc.) → convert to IST first, then use that IST value in the query.
2. If the user gives a time already in IST → use as-is.
3. If no time given → default to IST "now" (`now` in Kibana always reflects the server clock; just make sure any displayed times are converted to IST before showing the user).

When displaying results:
- All `@timestamp` values from Kibana come back as UTC. **Convert every timestamp to IST before showing it to the user.** Never show a raw UTC timestamp in the final answer.
- Format: `YYYY-MM-DD HH:MM:SS IST`
- Example: `2026-07-27T08:30:00.000Z` → `2026-07-27 14:00:00 IST`

Conversion formula: **IST = UTC + 5 hours 30 minutes**

Quick reference:
| UTC | IST |
|---|---|
| 00:00 | 05:30 |
| 06:00 | 11:30 |
| 12:00 | 17:30 |
| 18:00 | 23:30 |
| 18:30 | 00:00 next day |

When using `range_minutes` in tool calls, no conversion is needed — relative ranges (`now-60m`, etc.) are timezone-agnostic. Conversion is only needed when displaying absolute timestamps to the user or when the user provides an absolute time as input.

---

## The golden rule — reqid is the source of truth

**For any investigation, always follow this sequence:**

```
1. FIND the specific event → search_logs with the exact identifier the user gave
   (commit hash, object name, error text, endpoint, anything specific)
   Use range_minutes=1440 (24h) as default — events may not be recent.
   Sort asc to see the operation in sequence.

2. EXTRACT the reqid → every meaningful log line has a reqid in the context block
   dissect.catalina_out.reqid  OR  message field (look for reqid=... or [reqid:...])
   If multiple reqids found, pick the one from the error or the operation entry point.

3. TRACE the reqid → mcp__kibana__trace_request(reqid, range_minutes=1440)
   This gives the full cross-service call chain for that specific request.
   The error in this trace IS the root cause — nothing else.

4. REPORT what the trace shows: which service threw the error, the error code/message,
   the timestamp, and the reqid. That's the complete root cause.
```

**If step 1 returns 0 results** — say so and stop. State exactly what was searched and that no matching logs were found. Do NOT substitute errors from other operations, background jobs, or unrelated org activity as a replacement explanation. Errors from the same org that don't share a reqid with the reported operation are irrelevant noise.

**If the trace in step 3 shows no errors** — the failure may be in a downstream service not logging to this index, or outside the log retention window. Say so explicitly.

---

## Autonomous execution rules

- **Never ask** "Can I run this query?" or "Should I check X?" — just do it.
- **Never ask** "Do you want me to look at Y?" — if it's relevant, look.
- When a service, request ID, or org ID is mentioned, immediately run the corresponding investigation tool.
- When you find errors or anomalies, immediately drill in with follow-up queries (e.g. error in VCS → trace the reqid, check which org is affected).
- When space is unspecified, search both and report from whichever has results.
- **If a query returns 0 events:**
  1. Try the other space (`gcs` ↔ `cai`)
  2. Broaden the time range (`--range 120` or `--range 240`)
  3. Try `search_logs` with `index_pattern="filebeat-*-intcloud-*"` and a broader KQL
  4. **If still 0:** say "no logs found" — do NOT substitute errors from other unrelated operations

---

## Tool reference

| Tool | Required params | Purpose |
|------|----------------|---------|
| `mcp__kibana__auth_status` | — | Check session expiry |
| `mcp__kibana__login` | `username`, `password` | Okta SAML login (push MFA) |
| `mcp__kibana__inject_session` | `cookie_name`, `cookie_value`, `expires_at_unix_seconds` | Manual sid cookie injection |
| `mcp__kibana__list_spaces` | — | List all Kibana spaces |
| `mcp__kibana__list_data_views` | — | List all index patterns / data views |
| `mcp__kibana__list_dashboards` | `space_id?` | List dashboards in a space |
| `mcp__kibana__get_dashboard_info` | `dashboard_id` | Panel layout and metadata |
| `mcp__kibana__search_logs` | `index_pattern`, `kql?`, `range_minutes?`, `size?` | Raw KQL search against any index |
| `mcp__kibana__run_esql` | `query` | ES\|QL query (aggregations, stats) |
| `mcp__kibana__get_alert_rules` | — | List all Kibana alerting rules |
| `mcp__kibana__get_active_alerts` | `rule_id` | Active instances for a rule |
| `mcp__kibana__get_apm_services` | `environment?` | APM service list |
| `mcp__kibana__list_ml_jobs` | — | ML anomaly detection jobs |

### Investigation / scenario tools — use these for all diagnostic questions

| Tool | Required params | When to use |
|------|----------------|-------------|
| `mcp__kibana__search_service_logs` | `service` | General log search — starting point for any service question |
| `mcp__kibana__investigate_service_errors` | `service` | ERROR/WARN/exception investigation — first step when debugging an alert |
| `mcp__kibana__trace_request` | `reqid` | Full cross-service request trace — use any time a reqid is known |
| `mcp__kibana__search_by_org` | `org_id` | All logs for a customer/tenant — customer support investigations |
| `mcp__kibana__investigate_pod_health` | — | OOMKills, CrashLoopBackOff, evictions — when pods are crashing |
| `mcp__kibana__compare_log_volume` | `service` | Error rate trend before/after a deployment or incident |

### Utility tools — for deeper drill-down

| Tool | Required params | When to use |
|------|----------------|-------------|
| `mcp__kibana__get_log_context` | `timestamp` | Fetch N lines before/after a specific log timestamp — understand what led to an error |
| `mcp__kibana__count_by_field` | `field` | Aggregate by any field: error count per service, most active orgs, namespace distribution |
| `mcp__kibana__log_histogram` | — | Text bar chart of log volume over time — spot spikes, confirm whether issue is ongoing |
| `mcp__kibana__list_fields` | — | Discover field names in an index — use when a query returns 0 results and you suspect wrong field name |

---

## Scenario keyword → tool mapping

| User says | Tools to run |
|---|---|
| specific event described (commit hash / object name / error text / endpoint) | `search_logs` to find it → extract reqid → `trace_request` — ALWAYS do this first |
| specific reqid already known | `trace_request(reqid)` immediately — no search needed |
| "errors" / "exceptions" / "failures" for a service | `investigate_service_errors` + `compare_log_volume` |
| "what happened to" / "debug" a service | `investigate_service_errors` + `search_service_logs` (ERROR, last 60m) |
| specific org / tenant / customer (no specific operation) | `search_by_org(org_id)` — always |
| "pod crashing" / "OOMKill" / "restart" | `investigate_pod_health` + `investigate_service_errors` |
| "deploy went wrong" / "did the release break anything" | `compare_log_volume` (before vs after window) + `investigate_service_errors` |
| "slow" / "timeout" / "not responding" | `investigate_service_errors` (WARNs) + `search_service_logs` (level=WARN, range=60) |
| "what is org X doing" / "customer issue" | `search_by_org` + `trace_request` for any reqid found |
| "overall health" / "status of service X" | `investigate_service_errors` + `compare_log_volume` + `investigate_pod_health` |
| "how many errors" / "error rate" | `compare_log_volume` |

---

## Common investigation flows

### Flow 1 — Service alert / error spike

```
1. mcp__kibana__investigate_service_errors(service, range_minutes=60)
2. If errors found:
   a. Pick the reqid from a representative error
   b. mcp__kibana__trace_request(reqid) → full call chain
   c. Note the org in the error → mcp__kibana__search_by_org(org_id, service=service) if org is interesting
3. mcp__kibana__compare_log_volume(service) → has error rate changed vs last hour?
4. mcp__kibana__investigate_pod_health(service) → are pods restarting?
```

### Flow 2 — Customer-reported issue (org ID known)

```
1. mcp__kibana__search_by_org(org_id, range_minutes=60)
2. If errors visible in org logs:
   a. mcp__kibana__search_by_org(org_id, service=<affected_service>)
   b. Pick a reqid → mcp__kibana__trace_request(reqid) for full trace
3. mcp__kibana__investigate_service_errors(service, org=org_id)
```

### Flow 3 — Request trace (reqid known)

```
1. mcp__kibana__trace_request(reqid, range_minutes=60)
   → Returns chronological events across ALL services for this request
   → Shows exactly where the request failed and which services were involved
2. For each ERROR in the trace: note the service name and check its recent errors
3. mcp__kibana__investigate_service_errors(failing_service, range_minutes=30)
```

### Flow 4 — Post-deployment health check

```
1. mcp__kibana__compare_log_volume(service, baseline_minutes=60, comparison_minutes=60)
   → Baseline = hour before deploy, comparison = hour after
2. mcp__kibana__investigate_service_errors(service, range_minutes=30)
3. mcp__kibana__investigate_pod_health(service, range_minutes=30)
```

### Flow 5 — Pod instability investigation

```
1. mcp__kibana__investigate_pod_health(service, namespace=<ns>, range_minutes=60)
   → Shows OOMKills, CrashLoopBackOff, evictions from k8s_controlplane-*
2. mcp__kibana__search_service_logs(service, level=ERROR, range_minutes=30)
   → What was logged just before the crash?
3. mcp__kibana__investigate_service_errors(service, range_minutes=60)
```

---

## KQL patterns — use these directly

### Service log search
```kql
kubernetes.labels.app: "vcs"
kubernetes.labels.app: "vcs" AND dissect.catalina_out.level: "ERROR"
kubernetes.labels.app: "migration" AND kubernetes.namespace: "iics-prod-nause6"
kubernetes.labels.app: "vcs" AND dissect.catalina_out.org: "e9siX3d59Q3cth8lcLJLIW"
```

### Request tracing
```kql
dissect.catalina_out.reqid: "9JYerohKO8ufiUSqDLaMoq"
dissect.catalina_out.reqid: "9JYerohKO8ufiUSqDLaMoq" OR message: "9JYerohKO8ufiUSqDLaMoq"
```

### Error patterns
```kql
dissect.catalina_out.level: "ERROR" OR dissect.catalina_out.level: "WARN"
message: "*Exception*" OR message: "*ERROR*"
kubernetes.labels.app: "vcs" AND (dissect.catalina_out.level: "ERROR" OR message: "*Exception*")
```

### Tenant/org search
```kql
dissect.catalina_out.org: "5fctaaJudJmjqCCl7Xmzk2"
dissect.catalina_out.org: "5fctaaJudJmjqCCl7Xmzk2" AND kubernetes.labels.app: "runtime"
```

### Pod / namespace scoping
```kql
kubernetes.namespace: "iics-prod-nause6"
POD_cluster_name: "use6"
CT_PODNAME: "AWS-PROD-USE1-POD6"
kubernetes.pod.name: "vcs-*"
```

### K8s pod events (k8s_controlplane-* index)
```kql
message: "OOMKill*" OR message: "CrashLoopBackOff*" OR message: "*Killing*" OR message: "*Evicted*"
kubernetes.labels.app: "vcs" AND (message: "OOMKill*" OR message: "CrashLoopBackOff*")
```

---

## Time range guidance

| User says | `range_minutes` |
|-----------|:-:|
| "right now" / "current" | 15 |
| "last 30 minutes" | 30 |
| "last hour" / default | 60 |
| "last 2 hours" | 120 |
| "this morning" / "today" | 480 |
| "last 24 hours" | 1440 |
| specific incident window | calculate exact minutes from now to start |

---

## Reporting format

For any diagnostic or investigative question:

1. **Run the matching scenario tool(s) first** and present their output directly — do not re-summarize tool output into your own words.
2. **Add a brief interpretation** only if the tool output contains something non-obvious (e.g. a reqid that should be traced, an org that appears in multiple errors).
3. **State coverage**: which space(s) were searched, what time range, how many total events matched.
4. **Suggest the next step** if the investigation is incomplete (e.g. "Found 3 VCS_004 errors — run `trace_request` with reqid `E-vNCrqlFfP1fFNEgKLPD` to see the full call chain").

For raw `search_logs` or `run_esql` results: present them concisely with timestamps and key fields — no fabricated summary format.

---

## Navigation flow

```
auth_status → (login if expired)
  ↓
STEP 1 — DO I HAVE A SPECIFIC EVENT TO FIND?
  User gave a specific identifier (commit hash, object name, error text, endpoint, etc.)?
    YES → search_logs(kql='<identifier>', range_minutes=1440, sort_order="asc")
          → extract reqid from the found log line
          → trace_request(reqid, range_minutes=1440)
          → report what the trace shows — that is the root cause, nothing else
          → if 0 results: say "no logs found for <identifier>" and stop
    NO  → continue to Step 2

STEP 2 — SPACE ROUTING (automatic — tools handle this):
  Service/query contains cai-/cai_/process-server/active-*/cai keyword? → cai only
  Everything else → global first, fall back to gcs if 0 results
  ↓
STEP 3 — INTENT MAPPING:
  reqid already known → trace_request immediately
  org_id present      → search_by_org, then trace_request for any reqid found
  service + errors    → investigate_service_errors + compare_log_volume
  pod instability     → investigate_pod_health
  post-deploy check   → compare_log_volume + investigate_service_errors
  general logs        → search_service_logs with appropriate level/range
  ↓
STEP 4 — DRILL DOWN:
  Errors found → pick reqid → trace_request for full call chain
  Org visible  → search_by_org for tenant context
  Pod events   → investigate_pod_health for crash/OOM details
  ↓
STEP 5 — FILL GAPS (only if genuinely no data):
  0 results in gcs → try cai (and vice versa)
  0 results in 60m → widen to 120m or 240m
  Still 0          → report exactly what was searched and that no data was found
```
