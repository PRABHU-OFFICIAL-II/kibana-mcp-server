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

**Space routing heuristic:**
- Service name starts with `cai-`, `cai_`, `process-server`, `active-vcs`, `active-bpel` → use `cai` space
- All other services (vcs, migration, frs, runtime, session-service, etc.) → use `gcs` space
- When unsure → search both spaces in parallel, deduplicate by `_id`

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

**Never hardcode a space. Always route first.**

```
Does the service name start with cai-, cai_, process-server, active-vcs, active-bpel?
  YES → space = "cai"
  NO  → space = "gcs"
  UNSURE (no service name, org-only query, or cross-service) → search BOTH spaces in parallel
```

When searching both spaces, results are automatically deduplicated by document `_id` — the same ES document will not appear twice.

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
  4. Only then report "no data found" with what was tried

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

---

## Scenario keyword → tool mapping

| User says | Tools to run |
|---|---|
| "errors" / "exceptions" / "failures" for a service | `investigate_service_errors` + `compare_log_volume` |
| "what happened to" / "debug" a service | `investigate_service_errors` + `search_service_logs` (ERROR, last 60m) |
| specific request ID / reqid | `trace_request(reqid)` — always |
| specific org / tenant / customer | `search_by_org(org_id)` — always |
| "pod crashing" / "OOMKill" / "restart" | `investigate_pod_health` + `investigate_service_errors` |
| "deploy went wrong" / "did the release break anything" | `compare_log_volume` (before vs after window) + `investigate_service_errors` |
| "slow" / "timeout" / "not responding" | `investigate_service_errors` (WARNs) + `search_service_logs` (level=WARN, range=60) |
| "what is org X doing" / "customer issue" | `search_by_org` + optionally `trace_request` for any reqid found |
| "overall health" / "status of service X" | `investigate_service_errors` + `compare_log_volume` + `investigate_pod_health` |
| "how many errors" / "error rate" | `compare_log_volume` |
| cross-service investigation | `trace_request` with reqid, then `search_by_org` if org is visible |

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
STEP 1 — SPACE ROUTING:
  Service starts with cai-/cai_/process-server/active-*? → space = "cai"
  All other services                                      → space = "gcs"
  No service / cross-service / org-only                   → search both
  ↓
STEP 2 — INTENT MAPPING:
  reqid present?      → trace_request immediately
  org_id present?     → search_by_org immediately
  service + errors?   → investigate_service_errors + compare_log_volume
  pod instability?    → investigate_pod_health
  post-deploy check?  → compare_log_volume + investigate_service_errors
  general logs?       → search_service_logs with appropriate level/range
  ↓
STEP 3 — DRILL DOWN:
  Errors found → pick reqid from representative error → trace_request
  Org visible  → search_by_org for full tenant context
  Pod events   → investigate_pod_health for crash/OOM details
  Volume spike → compare_log_volume with wider window
  ↓
STEP 4 — FILL GAPS:
  0 results in gcs → try cai (and vice versa)
  0 results in 60m → widen to 120m or 240m
  Field missing    → try alternate field name (e.g. "message" vs "dissect.catalina_out.message")
  Report what was tried if still no data
```
