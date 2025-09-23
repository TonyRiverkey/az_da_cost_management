# Monthly Azure Cost Pull (Per Resource Group)

This repo contains a small, repeatable workflow to pull **last month’s total cost** per **Resource Group** across specific subscriptions.

## Folder layout
```text
(repo root)/
├─ pull_monthly/
│  └─ rg_monthly_costs.py         # main script (uses your account via az login)
│  └─ config_rg.yml               # subscription IDs + RG names to query
├─ outputs/                       # CSVs land here by default
├─ Run-PullMonthly.ps1            # one-click PowerShell runner
├─ preparation/
│  └─ build_subscriptions_yaml.py # Helper script to build the config file
│  └─ subscriptions.csv           # source list for helper script
```

---

## Prerequisites (one-time)
1. **Python 3.9+** and **Azure CLI** installed.
2. Create & activate a local **virtual environment** and install deps:
   ```powershell
   python -m venv .venv
   .\\.venv\\Scripts\\Activate.ps1
   python -m pip install -r requirements.txt
   ```
3. Your **user** must have the **Cost Management Reader** role on each subscription you’ll query



---

## One-click monthly pull (PowerShell)
This runs everything in one go: activate venv, ensure Azure login, and execute the pull.

```powershell
# From repo root
.\\Run-PullMonthly.ps1
```

**Parameters (optional):**
- `-ConfigPath` (default: `./config.yml`) — path to your YAML config
- `-ClientType` (default: `tony-cost-collector`) — client header to help with rate limits
- `-Sleep` (default: `1.0`) — seconds to wait between subscriptions
- `-MaxRetries` (default: `8`) — max retries on 429/503 throttling

The script writes a file named `outputs/costs_<YYYY-MM>.csv` (e.g., `outputs/costs_2025-08.csv`).

---

## Running the Python script directly (optional)
From repo root (after activating venv and logging in with `az login`):

```powershell
python rg_monthly_costs.py --config config_rg.yml
```

### Script arguments
The `rg_monthly_costs.py` accepts:


- **`--config`**: YAML file listing subscriptions and resource groups to query (see schema below).
- **`--out`**: Where to write the CSV. If omitted, the script defaults to `../outputs/costs_<YYYY-MM>.csv` when invoked from inside `pull_monthly/` (or `outputs/` in repo root when invoked with the runner).
- **`--sleep`**: A small pause between subscriptions to reduce throttling.
- **`--maxretries`**: Retries on HTTP 429/503 with adaptive backoff.
- **`--clienttype`**: Value for the `ClientType` header (helps in some tenants with rate limits).

---

## `config_rg.yml` schema
```yaml
subscriptions:
  - id: "00000000-0000-0000-0000-000000000000"
	resource_groups: ["rg-data-prod", "rg-analytics"]
  - id: "11111111-1111-1111-1111-111111111111"
	resource_groups: ["rg-platform", "rg-ingest"]
```

- **id**: Subscription GUID (required)
- **resource_groups**: list of resource group *names* within that subscription

---

## Output CSV schema
File: `outputs/costs_<YYYY-MM>.csv`

Columns:
- `subscription_id` — GUID of the subscription
- `resource_group` — resource group name (lowercased in output)
- `start` — ISO timestamp for the **start of last month** (inclusive, UTC)
- `end` — ISO timestamp for the **first day of this month** (exclusive, UTC)
- `total_cost` — numeric, **PreTaxCost** sum for the period

Example:

| subscription_id                        | resource_group | start                  | end                    | total_cost |
|----------------------------------------|----------------|------------------------|------------------------|------------|
| 00000000-0000-0000-0000-000000000000   | rg-data-prod   | 2025-08-01T00:00:00Z   | 2025-09-01T00:00:00Z   | 1234.56    |



---

## Helper script: `build_subcriptions_yaml.py`
When you change which subscriptions/RGs to include, use the helper to (re)build the `config_rg.yml` used by the monthly pull.



**CSV expectations:**
The script expects at least these headers (lowercase, no spaces):

- `subscription` – the subscription name
- `resource_group` – the RG name

**Usage:**
```powershell
# From the preparation folder ./preparation/
python build_subscriptions_yaml.py -i subscriptions.csv -o "../pull_monthly/config_rg.yml"
```

This is handy when you add/remove resource groups; regenerate the YAML and re-run the monthly pull.

---

## Troubleshooting
- **429 Too Many Requests**: the Python script includes adaptive backoff and pacing. You can tune `--sleep`, `--maxretries`, and `--clienttype`.
- **Auth prompts**: user-based runs may occasionally require re-login due to MFA/CA policies (`az login`). For “always on,” switch to Managed Identity or a Service Principal.