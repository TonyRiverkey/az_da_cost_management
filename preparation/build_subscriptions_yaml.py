import argparse
import csv
import re
import sys
from collections import defaultdict
from typing import Dict, Iterable, Tuple, Optional
from pathlib import Path

import yaml

from azure.identity import (
    ChainedTokenCredential,
    AzureCliCredential,
    VisualStudioCodeCredential,
    InteractiveBrowserCredential,
)
from azure.mgmt.resource import SubscriptionClient

GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$"
)

HEADERS_CANDIDATES = [
    ("subscription", "resource_group")
]


def get_credential(tenant_id: Optional[str]) -> ChainedTokenCredential:
    """
    Prefer your signed-in user:
      1) Azure CLI (az login)
      2) VS Code Azure Account extension
      3) Interactive browser fallback
    """
    creds = []
    try:
        creds.append(AzureCliCredential(tenant_id=tenant_id) if tenant_id else AzureCliCredential())
    except Exception:
        pass  # not installed or not logged in; continue

    try:
        creds.append(VisualStudioCodeCredential(tenant_id=tenant_id) if tenant_id else VisualStudioCodeCredential())
    except Exception:
        pass  # extension not available; continue

    creds.append(InteractiveBrowserCredential(tenant_id=tenant_id))
    return ChainedTokenCredential(*creds)


def detect_headers(fieldnames: Iterable[str]) -> Tuple[str, str]:
    """Return the actual header names that match our candidates (case-insensitive)."""
    fields = [f for f in (fieldnames or []) if f]
    lower_map = {f.strip().lower(): f for f in fields}
    for sub_col, rg_col in HEADERS_CANDIDATES:
        if sub_col in lower_map and rg_col in lower_map:
            return lower_map[sub_col], lower_map[rg_col]
    raise ValueError(
        f"Could not detect CSV headers. Found columns: {fieldnames}\n"
        f"Expected something like: subscription,resource_group"
    )


def load_rows(path: str) -> Iterable[Tuple[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        sub_col, rg_col = detect_headers(reader.fieldnames or [])
        for row in reader:
            sub_raw = (row.get(sub_col) or "").strip()
            rg = (row.get(rg_col) or "").strip()
            if not sub_raw or not rg:
                # Skip blank or partial rows
                continue
            yield sub_raw, rg


def build_subscription_index(credential, tenant_id: Optional[str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Returns:
      - name_to_id: case-insensitive display name -> subscription_id
      - id_to_name: subscription_id -> display name
    """
    client = SubscriptionClient(credential)
    name_to_id: Dict[str, str] = {}
    id_to_name: Dict[str, str] = {}

    subs = list(client.subscriptions.list())
    if not subs:
        print("No subscriptions visible for the signed-in account.", file=sys.stderr)

    # If duplicate display names exist, we’ll keep the first and warn on collisions.
    seen_names = set()
    for s in subs:
        display = (s.display_name or "").strip()
        sid = (s.subscription_id or "").strip()
        if not sid:
            continue
        id_to_name[sid] = display
        key = display.lower()
        if key in seen_names:
            # Warn once per duplicate name; still keep the first seen.
            print(f"Warning: duplicate subscription display name detected: '{display}'. "
                  f"Matching by name may be ambiguous.", file=sys.stderr)
        else:
            seen_names.add(key)
            if key not in name_to_id:
                name_to_id[key] = sid

    return name_to_id, id_to_name


def resolve_subscription_id(
    sub_input: str,
    name_to_id: Dict[str, str],
    id_to_name: Dict[str, str]
) -> Optional[str]:
    # If input looks like a GUID, pass through (whether or not we can see it)
    if GUID_RE.match(sub_input):
        return sub_input

    # Otherwise treat as a display name (case-insensitive)
    key = sub_input.lower()
    sid = name_to_id.get(key)
    if not sid:
        print(
            f"Warning: could not find subscription by name '{sub_input}'. "
            f"Make sure your signed-in account has access.",
            file=sys.stderr,
        )
        return None
    return sid


def resolve_output_path(args: argparse.Namespace) -> Path:
    """
    Resolve where to write the YAML.

    Precedence:
      1) --output as a full path
      2) --output-dir + --output-name (or default basename)
    """
    # If the user passed -o/--output, treat it as the full path.
    if args.output and (args.output_dir or args.output_name):
        print("Note: --output provided; ignoring --output-dir/--output-name.", file=sys.stderr)

    if args.output and not (args.output_dir or args.output_name):
        out_path = Path(args.output).expanduser().resolve()
    else:
        # Determine basename
        default_basename = "subscriptions.yml"
        basename = (
            args.output_name
            or (Path(args.output).name if args.output else default_basename)
        )
        # Determine directory
        if args.output_dir:
            out_dir = Path(args.output_dir).expanduser()
        else:
            out_dir = Path.cwd()
        out_path = (out_dir / basename).resolve()

    # Ensure directory exists
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Build subscriptions.yml from a CSV of subscriptions/resource groups.")
    parser.add_argument("--input", "-i", required=True, help="Path to subscriptions.csv")
    parser.add_argument("--output", "-o", default=None,
                        help="Path to write YAML. If omitted, use --output-dir/--output-name or defaults.")
    parser.add_argument("--output-dir", help="Directory to write YAML (created if missing). Defaults to CWD.")
    parser.add_argument("--output-name", help="Basename for the YAML file (e.g., output.yml). Defaults to subscriptions.yml.")
    parser.add_argument("--tenant-id", help="Optional Tenant ID if you need to force a specific tenant for auth")
    args = parser.parse_args()

    try:
        rows = list(load_rows(args.input))
    except Exception as e:
        print(f"Error reading CSV: {e}", file=sys.stderr)
        sys.exit(2)

    if not rows:
        print("No valid rows found in the CSV. Nothing to do.", file=sys.stderr)
        sys.exit(1)

    credential = get_credential(args.tenant_id)
    name_to_id, id_to_name = build_subscription_index(credential, args.tenant_id)

    # aggregate
    agg_rgs: Dict[str, set] = defaultdict(set)
    agg_name: Dict[str, str] = {}

    for sub_raw, rg in rows:
        sid = resolve_subscription_id(sub_raw, name_to_id, id_to_name)
        if not sid:
            continue
        agg_rgs[sid].add(rg)

        # prefer Azure’s display name; otherwise use CSV’s display name when not a GUID
        chosen_name = id_to_name.get(sid)
        if not chosen_name and not GUID_RE.match(sub_raw):
            chosen_name = sub_raw.strip()
        if chosen_name:
            agg_name.setdefault(sid, chosen_name)

    if not agg_rgs:
        print("No subscriptions resolved to IDs. Exiting.", file=sys.stderr)
        sys.exit(1)

    # Shape the YAML
    out_doc = {
        "subscriptions": [
            {"id": sid, "name": agg_name.get(sid, id_to_name.get(sid, "")),
            "resource_groups": sorted(list(rgs))}
            for sid, rgs in sorted(agg_rgs.items(), key=lambda kv: kv[0].lower())
        ]
    }

    out_path = resolve_output_path(args)

    with open(out_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(out_doc, f, sort_keys=False)

    print(f"Wrote {out_path}")
    # Also echo to stdout for convenience
    print(yaml.safe_dump(out_doc, sort_keys=False))


if __name__ == "__main__":
    main()