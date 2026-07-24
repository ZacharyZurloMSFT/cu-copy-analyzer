# cu-copy-analyzer

A small command-line harness that exercises the **Azure AI Content Understanding** analyzer-copy flow between two Foundry resources and produces a shareable diagnostic report.

Use it to answer: **when a custom analyzer is copied from resource A to resource B, does it end up usable on B?**

The harness talks to the Content Understanding REST API directly (no SDK) so you get authentic HTTP status codes, response headers, and timings — which is what an Azure support engineer needs to trace an issue through service logs.

## What this tests

Given two Foundry resources (a **source** and a **target**), `run-all` does the following on your behalf:

1. Creates a small custom analyzer on the **source** resource and waits for it to become `ready`.
2. Creates the same analyzer natively on the **target** resource (a control) and waits for it to become `ready`.
3. Grants copy authorization on the source.
4. Copies source → target and polls the copy operation to completion.
5. On the target, runs a diagnostic matrix for both the **copied** analyzer and the **native** control:

   | Analyzer | List shows it? | GET by-id | analyze-by-id |
   |---|---|---|---|
   | copied  | expect `ready` | expect 200 | expect 202 |
   | native  | expect `ready` | expect 200 | expect 202 |

6. Writes a `report.md` with the diagnostic matrix, request IDs, timestamps, region headers, and a verdict of either **Copy Pipeline Succeeded** or **Copy Pipeline Failed**.

If you also provide the target's API key in `.env`, the harness re-runs the copied-analyzer get-by-id **twice** — once under Entra token auth, once under API-key auth — to isolate whether any failure is auth-path specific.

## Prerequisites

1. **Two Azure AI Foundry (Cognitive Services) resources** you own — one to act as the source, one as the target. Both must have Content Understanding enabled and the model defaults deployed (see step 4 below).
2. **Python 3.10+**.
3. **Azure CLI** (`az`) signed in to the tenant that owns both resources.
4. Both resources have CU model defaults deployed. If you're not sure, deploy them once per resource:
   ```powershell
   $token = az account get-access-token --resource https://cognitiveservices.azure.com --query accessToken -o tsv
   curl.exe -X PATCH `
     "https://<resource-name>.services.ai.azure.com/contentunderstanding/defaults?api-version=2025-11-01" `
     -H "Authorization: Bearer $token" `
     -H "Content-Type: application/json" -d '{}'
   ```
   Repeat for both resources. See the [CU REST reference](https://learn.microsoft.com/en-us/rest/api/contentunderstanding/) for details.
5. The identity you're signed in as has the **`Cognitive Services User`** role on **both** resources. Cognitive Services data-plane calls (which is what this harness makes) require this role explicitly — Owner/Contributor alone is not sufficient.
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
# then edit .env and fill in your two resources' endpoints, resource IDs, and regions
```

### `.env` fields

| Variable | Required | Description |
|---|---|---|
| `SOURCE_ENDPOINT` | yes | Full source endpoint, e.g. `https://myresource.services.ai.azure.com` |
| `SOURCE_RESOURCE_ID` | yes | Full ARM resource ID of the source (`/subscriptions/.../accounts/<name>`) |
| `SOURCE_REGION` | yes | Azure region of the source resource, e.g. `eastus2` |
| `SOURCE_AUTH_MODE` | no | `entra` (default) or `key` |
| `SOURCE_KEY` | if `SOURCE_AUTH_MODE=key` | Ocp-Apim key for the source |
| `TARGET_ENDPOINT` / `TARGET_RESOURCE_ID` / `TARGET_REGION` / `TARGET_AUTH_MODE` / `TARGET_KEY` | same as above | For the target resource |
| `API_VERSION` | no | Defaults to `2025-11-01` |
| `COMPLETION_MODEL` | no | Completion model name for the `generate` field. Defaults to `gpt-4o-mini`. Must be deployed on **both** resources. |
| `EMBEDDING_MODEL` | no | Embedding model name. Defaults to `text-embedding-3-large`. Must be deployed on **both** resources. |
| `ANALYZER_BASE_NAME` | no | Prefix for the analyzer IDs the harness creates. Defaults to `cu_copy_repro`. |

**Tip:** even if you're using Entra auth as primary, setting `TARGET_KEY` unlocks the extra Entra-vs-key comparison in the diagnostic matrix.

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

python repro.py --run-id $RID create-source
python repro.py --run-id $RID create-native
python repro.py --run-id $RID grant
python repro.py --run-id $RID copy
python repro.py --run-id $RID verify
```

State is persisted between subcommands in `runs/<RUN_ID>/state.json`.

### Cleanup

```powershell
python repro.py --run-id $RID cleanup
```

Deletes the three analyzers the harness created on the two resources. 404s are tolerated.

## What to send back to Azure support

Two files per run:

- `runs/<RUN_ID>/report.md` — human-readable summary with the diagnostic matrix, `x-ms-request-id` values, region headers, and verdict.
- `runs/<RUN_ID>/http_log.ndjson` — one JSON line per HTTP call, with request bodies, response bodies, all captured headers, and UTC timestamps.

Together they give the service team enough to pull backend logs for both the copy operation and any failing get-by-id / analyze-by-id call.

### Privacy note

- `http_log.ndjson` contains **your resource IDs, endpoints, region headers, and Azure request IDs**. It does **not** contain your bearer token or API key. Even so, review the file before sharing.
- `.env` and everything under `runs/` are gitignored so nothing sensitive lands in the repo.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `DefaultAzureCredential` fails or you get `AADSTS…` | `az login --tenant <tenant-id>` and set `$env:AZURE_TENANT_ID` to pin the tenant. |
| `403` on any call | `Cognitive Services User` role missing on that resource (or not yet propagated — wait ~60s). |
| PUT analyzer returns `400 InvalidFieldSchema` / `defaults` | Run the CU `PATCH /defaults` step in prerequisites for that resource. |
| PUT analyzer returns `400 InvalidBaseAnalyzerId` | The `baseAnalyzerId` isn't available at your API version on this resource. Check available prebuilts with `GET /contentunderstanding/analyzers?api-version=2025-11-01`. |
| Analyzer creation ends `status=failed` | Usually a missing completion/embedding model on the resource. Check `_analyzer_body()` in `repro.py`, or override with `COMPLETION_MODEL` / `EMBEDDING_MODEL` in `.env`. |
| Copy LRO stays `Running` past 10 minutes | The poller times out; bump `timeout_s` in `cu_client.py::poll_operation` or inspect the last polled body in `http_log.ndjson`. |

## Files

| File | Purpose |
|---|---|
| `repro.py` | CLI entry point — subcommands, `run-all`, report generation |
| `cu_client.py` | Auth + raw-REST wrapper, header/timing capture, NDJSON logging, LRO poller |
| `requirements.txt` | `requests`, `azure-identity`, `python-dotenv` |
| `.env.example` | Configuration template |
| `runs/` | Per-run outputs (gitignored) |
