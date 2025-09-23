#!/usr/bin/env python3
import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

import yaml
from azure.identity import DefaultAzureCredential
from azure.mgmt.costmanagement import CostManagementClient
from azure.identity import DefaultAzureCredential, AzureCliCredential, ChainedTokenCredential, InteractiveBrowserCredential

import time, random
from azure.core.exceptions import HttpResponseError

RETRY_AFTER_KEYS = [
    "Retry-After",
    "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-client-retry-after",
    "x-ms-ratelimit-microsoft.consumption-retry-after",
]

# ---------- helpers ----------
def _parse_retry_after(headers: dict) -> float | None:
    for k in RETRY_AFTER_KEYS:
        v = headers.get(k)
        if not v:
            continue
        # header can be "seconds" or an HTTP date; treat as seconds if int
        try:
            return float(v)
        except Exception:
            # if HTTP-date, fall back to a conservative default below
            return None
    return None

def usage_with_retry(cm_client, scope: str, parameters: dict,
                     client_type: str = "tony-cost-collector",
                     max_retries: int = 8, base_sleep: float = 2.0):
    """
    Call CostManagement query.usage() with adaptive backoff honoring Retry-After headers.
    """
    headers = {"x-ms-command-name": "CostAnalysis", "ClientType": client_type}
    attempt = 0
    while True:
        try:
            # many Azure SDK ops accept per-call headers via kwargs
            return cm_client.query.usage(scope=scope, parameters=parameters, headers=headers)
        except HttpResponseError as e:
            if e.status_code in (429, 503):
                headers_map = dict(getattr(e.response, "headers", {}))
                retry_after = _parse_retry_after(headers_map)
                if retry_after is None:
                    # exponential backoff with jitter, capped
                    retry_after = min(base_sleep * (2 ** attempt), 60.0)
                # small jitter to de-synchronize
                jitter = random.uniform(0, 0.2 * retry_after)
                sleep_s = retry_after + jitter
                time.sleep(sleep_s)
                attempt += 1
                if attempt >= max_retries:
                    raise
            else:
                raise

def build_credential():
    # Prefer CLI (works great after `az login`), then fall back to interactive browser
    return ChainedTokenCredential(
        AzureCliCredential(),
        InteractiveBrowserCredential()  # opens a browser on first run
    )

def previous_month_range(now_utc: dt.datetime) -> tuple[str, str]:
    first_of_this_month = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end = first_of_this_month
    # go to first day of previous month
    prev_month_last_day = last_month_end - dt.timedelta(days=1)
    first_of_prev_month = prev_month_last_day.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # ISO 8601 Z format
    return first_of_prev_month.isoformat() + "Z", last_month_end.isoformat() + "Z"

def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)

# ---------- core ----------
def query_rg_costs_for_subscription(cm_client: CostManagementClient, subscription_id: str, start_iso: str, end_iso: str, max_retries: int, client_type: str) -> dict:
    """
    Returns a dict: { rg_name_lower: total_cost_float }
    """
    scope = f"/subscriptions/{subscription_id}"
    # Group by ResourceGroupName; aggregate PreTaxCost (common measure for total cost)
    parameters = {
        "type": "Usage",
        "timeframe": "Custom",
        "timePeriod": {"from": start_iso, "to": end_iso},  # end is exclusive
        "dataset": {
            "granularity": "None",
            "aggregation": {
                "totalCost": {"name": "PreTaxCost", "function": "Sum"}
            },
            "grouping": [
                {"type": "Dimension", "name": "ResourceGroupName"}
            ]
        }
    }

    resp = usage_with_retry(cm_client, scope, parameters, client_type, max_retries)
    rows = getattr(resp, "rows", []) or getattr(resp, "properties", {}).get("rows", [])
    cols = getattr(resp, "columns", []) or getattr(resp, "properties", {}).get("columns", [])

    # find indices
    try:
        rg_idx = next(i for i, c in enumerate(cols) if (getattr(c, "name", None) or c.get("name")) in ("ResourceGroupName", "ResourceGroup"))
    except StopIteration:
        raise RuntimeError("Query did not return ResourceGroupName column; check permissions/scope.")
    try:
        cost_idx = next(i for i, c in enumerate(cols) if (getattr(c, "name", None) or c.get("name")) in ("totalCost", "PreTaxCost"))
    except StopIteration:
        # Some SDKs set the column name to the aggregation key ("totalCost")
        try:
            cost_idx = next(i for i, c in enumerate(cols) if (getattr(c, "name", None) or c.get("name")) == "totalCost")
        except StopIteration:
            raise RuntimeError("Query did not return cost column; check aggregation name.")

    out = {}
    for r in rows:
        rg = r[rg_idx] or ""
        cost = float(r[cost_idx] or 0.0)
        out[rg.lower()] = out.get(rg.lower(), 0.0) + cost
    return out

def main():
    ap = argparse.ArgumentParser(description="Pull last month's Azure cost per Resource Group.")
    ap.add_argument("--config", required=True, help="Path to config.yml")
    ap.add_argument("--out", default=None, help="Output CSV path (default: ../outputs/costs_<YYYY-MM>.csv)")
    ap.add_argument("--sleep", type=float, default=1.0, help="Seconds to sleep between subscriptions")
    ap.add_argument("--maxretries", type=int, default=8, help="Max retries on 429/503")
    ap.add_argument("--clienttype", default="tony-cost-collector", help="ClientType header value")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    targets = cfg.get("subscriptions", [])
    if not targets:
        print("No subscriptions in config.", file=sys.stderr)
        sys.exit(2)

    now_utc = dt.datetime.utcnow()
    start_iso, end_iso = previous_month_range(now_utc)
    month_label = start_iso[:7]  # YYYY-MM
    out_path = Path(args.out) if args.out else Path("../outputs") / f"costs_{month_label}.csv"

    credential = build_credential()
    cm_client = CostManagementClient(credential=credential)

    records = []
    for sub in targets:
        sub_id = sub["id"]
        wanted_rgs = [rg.lower() for rg in sub.get("resource_groups", [])]
        rg_costs = query_rg_costs_for_subscription(cm_client, sub_id, start_iso, end_iso, args.maxretries, args.clienttype)

        # Emit requested RGs only; include zeros if not present
        for rg in wanted_rgs:
            cost = rg_costs.get(rg, 0.0)
            records.append({
                "subscription_id": sub_id,
                "resource_group": rg,
                "start": start_iso,
                "end": end_iso,
                "total_cost": round(cost, 2)
            })
        time.sleep(args.sleep) # Pacing to not overload Azure

    # Write CSV
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["subscription_id", "resource_group", "start", "end", "total_cost"])
        writer.writeheader()
        writer.writerows(records)

    print(f"Wrote {len(records)} rows to {out_path}")

if __name__ == "__main__":
    main()