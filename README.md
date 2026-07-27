# cu-copy-analyzer

A command-line harness that reproduces the **Azure AI Content Understanding project-scoped analyzer copy bug**: copying a portal-created (project-scoped) analyzer from a source Foundry resource to a target succeeds, but `GET /analyzers/{id}` on the target returns 404 for that analyzer on the preview api-version (and, in some deployments, on the stable api-version too), even though the list endpoint surfaces it.

The harness talks to the Content Understanding REST API directly (no SDK) so you get authentic HTTP status codes, response headers, and timings — which is what an Azure support engineer needs to trace an issue through service logs.

## What this tests

Given two Foundry resources — a **source** (where a portal-created analyzer already lives) and a **target** (typically QA) — `run-all` does the following:

1. GETs the existing `SOURCE_ANALYZER_ID` on the **source** and confirms it's `ready`. Captures its `projectId` tag. (No PUT/create — the analyzer must already exist.)
2. Grants copy authorization on the source.
3. Copies source → target and polls the copy operation to completion.
4. On the target, runs the diagnostic matrix on the copied analyzer:

   | Auth | api-version | List shows it? | GET by-id | analyze-by-id |
   |---|---|---|---|---|
   | entra | primary (`API_VERSION`) | expect `ready` | expect 200 | expect 202 |
   | entra | preview (`PREVIEW_API_VERSION`) | expect `ready` | expect 200 | — |
   | key _(if `TARGET_KEY` set)_ | primary | expect `ready` | expect 200 | — |

5. Writes `report.md` with the diagnostic matrix, `x-ms-request-id` values, region headers, projectId tags, and a verdict of either **Copy Pipeline Succeeded** or **Copy Pipeline Failed** (fails if get-by-id 404s on either api-version).

## Prerequisites

1. **Two Azure AI Foundry (Cognitive Services) resources** you own — the **source** must already have a portal-created (project-scoped) analyzer on it. The **target** is where the analyzer is copied to (typically your QA resource).
2. **Python 3.10+**.
3. **Azure CLI** (`az`) signed in to the tenant that owns both resources.
4. The identity you're signed in as has the **`Cognitive Services User`** role on **both** resources. Cognitive Services data-plane calls (which is what this harness makes) require this role explicitly — Owner/Contributor alone is not sufficient.
   ```powershell
   $me = az ad signed-in-user show --query id -o tsv

   az role assignment create --assignee $me --role "Cognitive Services User" `
     --scope "<SOURCE_RESOURCE_ID>"
   az role assignment create --assignee $me --role "Cognitive Services User" `
     --scope "<TARGET_RESOURCE_ID>"
   ```
   If both resources are in the same resource group you can assign the role once at the RG scope.

## Setup

```powershell
git clone <this-repo-url>
cd cu-copy-analyzer

python -m venv .venv
.\.venv\Scripts\Activate.ps1    # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

Copy-Item .env.example .env     # macOS/Linux: cp .env.example .env
# then edit .env — fill in both resources and SOURCE_ANALYZER_ID
```

### `.env` fields

| Variable | Required | Description |
|---|---|---|
| `API_VERSION` | no | Primary api-version. Defaults to `2025-11-01`. |
| `PREVIEW_API_VERSION` | no | Second api-version probed during verify. Defaults to `2026-06-01-preview`. |
| `SOURCE_ENDPOINT` | yes | Full source endpoint, e.g. `https://myresource.services.ai.azure.com` |
| `SOURCE_RESOURCE_ID` | yes | Full ARM resource ID of the source (`/subscriptions/.../accounts/<name>`) |
| `SOURCE_REGION` | yes | Azure region of the source, e.g. `eastus2` |
| `SOURCE_AUTH_MODE` | no | `entra` (default) or `key` |
| `SOURCE_KEY` | if `SOURCE_AUTH_MODE=key` | Ocp-Apim key for the source |
| `SOURCE_ANALYZER_ID` | **yes** | Id of an existing portal-created analyzer on the source (e.g. `COQAnalyzerV1`). The harness will not create this — it copies it as-is. |
| `SOURCE_PROJECT_ID` | no | Informational only — surfaced in `report.md`. The actual project scoping comes from the analyzer's tags on the source. |
| `TARGET_ENDPOINT` / `TARGET_RESOURCE_ID` / `TARGET_REGION` / `TARGET_AUTH_MODE` / `TARGET_KEY` | same as source | For the target (QA) resource |

**Tip:** even if you're using Entra auth as primary, setting `TARGET_KEY` unlocks the extra entra-vs-key comparison for get-by-id on the copied analyzer.

## Run it

### End-to-end (recommended)

```powershell
python repro.py run-all
```

At the end you'll see either

```
=== VERDICT: Copy Pipeline Succeeded ===
```

or

```
=== VERDICT: Copy Pipeline Failed ===
```

plus a path to `runs/<RUN_ID>/report.md`.

### Step-by-step

Every subcommand accepts `--run-id <id>` so you can replay individual steps against the same run:

```powershell
$RID = "20260724T190000Z"

python repro.py --run-id $RID verify-source
python repro.py --run-id $RID grant
python repro.py --run-id $RID copy
python repro.py --run-id $RID verify
```

State is persisted between subcommands in `runs/<RUN_ID>/state.json`.

### Cleanup

```powershell
python repro.py --run-id $RID cleanup
```

Deletes the **copied** analyzer on the target. The **source** analyzer is never touched (it's portal-owned by you and shouldn't be deleted by this tool). 404s are tolerated.

## What to send back to Azure support

Two files per run:

- `runs/<RUN_ID>/report.md` — human-readable summary with the diagnostic matrix, `x-ms-request-id` values, region headers, projectId tags, and verdict.
- `runs/<RUN_ID>/http_log.ndjson` — one JSON line per HTTP call, with request bodies, response bodies, all captured headers, api-version, and UTC timestamps.

Together they give the service team enough to pull backend logs for both the copy operation and any failing get-by-id call — with the specific api-version that produced each result.

### Privacy note

- `http_log.ndjson` contains **your resource IDs, endpoints, region headers, and Azure request IDs**. It does **not** contain your bearer token or API key. Even so, review the file before sharing.
- `.env` and everything under `runs/` are gitignored so nothing sensitive lands in the repo.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Missing or placeholder env var: SOURCE_ANALYZER_ID` | Set `SOURCE_ANALYZER_ID` in `.env` to the exact id of a portal-created analyzer on the source resource. |
| `Source analyzer <id> not found on source resource` | Typo in `SOURCE_ANALYZER_ID`, wrong `SOURCE_ENDPOINT`, or the identity you're signed in as doesn't have `Cognitive Services User` on the source. |
| `DefaultAzureCredential` fails or you get `AADSTS…` | `az login --tenant <tenant-id>` and set `$env:AZURE_TENANT_ID` to pin the tenant. |
| `403` on any call | `Cognitive Services User` role missing on that resource (or not yet propagated — wait ~60s). |
| Copy LRO stays `Running` past 10 minutes | The poller times out; bump `timeout_s` in `cu_client.py::poll_operation` or inspect the last polled body in `http_log.ndjson`. |
| Copy succeeds but verdict is "Failed" | That's the reproduction — check the diagnostic matrix in `report.md` to see which api-version's get-by-id returned 404. Share the report + NDJSON log with Azure support. |

## SPN role-scope probe

Separate from the copy-bug matrix, the harness ships a `spn-probe` subcommand
that answers a specific customer question:

> What exact role and scope does a service principal need to resolve a
> project-scoped analyzer by id and run `:analyze`? Is a project-scoped
> `Azure AI User` assignment sufficient, or is a specific Content
> Understanding / project role required?

`spn-probe` runs three calls against the **target** resource under
`API_VERSION` only, authenticated as a client-credential SPN (not your
signed-in user):

1. `GET /analyzers` — does the analyzer id even show up in the list?
2. `GET /analyzers/{id}` — the direct resolve.
3. `POST /analyzers/{id}:analyze` — the real workload call.

Each probe tolerates `403` and `404` so failure modes are **captured**, not
thrown. That's what makes this useful: a `403` vs a `404` is the whole answer
to the customer question.

- `403` — token was rejected at RBAC. Role/scope insufficient for that
  data-plane call.
- `404` — token was accepted (authentication passed) but the analyzer id is
  not resolvable for this principal. This is the fingerprint of a
  project-scoped analyzer the SPN can't see under its current role/scope.
- `200` on get-by-id + `202` on analyze — sufficient.

### Setup

1. Create an SPN in the tenant that owns your Foundry resources:
   ```powershell
   az ad sp create-for-rbac --name "cu-copy-analyzer-probe" --skip-assignment
   ```
   Copy the `appId`, `password`, and `tenant` into `.env`:
   ```
   SPN_TENANT_ID=<tenant>
   SPN_CLIENT_ID=<appId>
   SPN_CLIENT_SECRET=<password>
   ```

2. Have a target analyzer id ready. Either reuse a `--run-id` from a run that
   already did `copy` (the harness reads `target_analyzer_id` from
   `state.json`), pass `--analyzer-id <id>` explicitly, or set
   `TARGET_ANALYZER_ID` in `.env`.

### Recipe — three role scenarios

Before each run, update `SPN_ROLES_NOTE` in `.env` so the resulting
`spn_probe.md` labels which scenario it corresponds to. Wait ~60s after
each role change for Azure RBAC to propagate.

Let `$SPN_OID = az ad sp show --id <SPN_CLIENT_ID> --query id -o tsv` and
`$TARGET_RID = <TARGET_RESOURCE_ID>`. The Foundry project sub-scope is
`$TARGET_RID/projects/<project-name>`.

**Scenario 1 — `Azure AI User` at project sub-scope only (the customer's hypothesis):**
```powershell
$env:SPN_ROLES_NOTE = "Azure AI User @ project sub-scope only"
az role assignment create --assignee $SPN_OID --role "Azure AI User" `
  --scope "$TARGET_RID/projects/<project-name>"
python repro.py --run-id $RID spn-probe
```

**Scenario 2 — `Cognitive Services User` at account scope only (baseline):**
```powershell
# remove scenario 1's assignment first
az role assignment delete --assignee $SPN_OID --role "Azure AI User" `
  --scope "$TARGET_RID/projects/<project-name>"

$env:SPN_ROLES_NOTE = "Cognitive Services User @ account scope only"
az role assignment create --assignee $SPN_OID --role "Cognitive Services User" `
  --scope "$TARGET_RID"
python repro.py --run-id $RID spn-probe
```

**Scenario 3 — both simultaneously:**
```powershell
$env:SPN_ROLES_NOTE = "Azure AI User @ project + Cognitive Services User @ account"
az role assignment create --assignee $SPN_OID --role "Azure AI User" `
  --scope "$TARGET_RID/projects/<project-name>"
python repro.py --run-id $RID spn-probe
```

Each run writes `runs/<RUN_ID>/spn_probe.md` with the probe matrix,
`x-ms-request-id` values, region headers, projectId tags, and a verdict.
The full HTTP traces (labels prefixed `spn:`) land in the same
`http_log.ndjson` as everything else. Send **all three** `spn_probe.md`
files plus the NDJSON log to Azure support — together they empirically
answer the role/scope question.

### Cleanup

```powershell
az role assignment delete --assignee $SPN_OID --role "Azure AI User" `
  --scope "$TARGET_RID/projects/<project-name>"
az role assignment delete --assignee $SPN_OID --role "Cognitive Services User" `
  --scope "$TARGET_RID"
```

## Files

| File | Purpose |
|---|---|
| `repro.py` | CLI entry point — subcommands, `run-all`, report generation |
| `cu_client.py` | Auth + raw-REST wrapper, header/timing capture, NDJSON logging, LRO poller |
| `requirements.txt` | `requests`, `azure-identity`, `python-dotenv` |
| `.env.example` | Configuration template |
| `runs/` | Per-run outputs (gitignored) |
