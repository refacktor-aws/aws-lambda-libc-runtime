#!/usr/bin/env python3
"""
Deploy and run the AL2023 chunked transfer-encoding probe test.

Deploys a CloudFormation stack that:
  1. Builds a raw-socket bootstrap for provided.al2023
  2. Invokes it with payloads of 1KB, 100KB, 1MB, 5MB
  3. Reports whether the Runtime API used Transfer-Encoding: chunked
     or Content-Length for each payload size

Usage:
    python test_chunked_encoding.py [--profile PROFILE] [--region REGION] [--stack-name NAME]
"""

import argparse
import json
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

TEMPLATE_PATH = Path(__file__).parent / "template.yaml"
DEFAULT_STACK_NAME = "chunked-encoding-test"
DEFAULT_PROFILE = "integration-test"
DEFAULT_REGION = "us-east-1"


def get_session(profile, region):
    return boto3.Session(profile_name=profile, region_name=region)


def deploy_stack(session, stack_name):
    cf = session.client("cloudformation")
    template_body = TEMPLATE_PATH.read_text(encoding="utf-8")

    print(f"Deploying stack: {stack_name}")

    try:
        cf.describe_stacks(StackName=stack_name)
        stack_exists = True
    except ClientError:
        stack_exists = False

    params = dict(
        StackName=stack_name,
        TemplateBody=template_body,
        Capabilities=["CAPABILITY_IAM"],
    )

    if stack_exists:
        try:
            cf.update_stack(**params)
            print("Updating existing stack...")
            waiter = cf.get_waiter("stack_update_complete")
        except ClientError as e:
            if "No updates are to be performed" in str(e):
                print("Stack is already up to date.")
                return True
            raise
    else:
        cf.create_stack(**params)
        print("Creating new stack...")
        waiter = cf.get_waiter("stack_create_complete")

    print("Waiting for stack to complete (this may take a few minutes)...")
    start = time.time()
    try:
        waiter.wait(
            StackName=stack_name,
            WaiterConfig={"Delay": 10, "MaxAttempts": 60},
        )
    except Exception:
        elapsed = time.time() - start
        print(f"\nStack operation failed after {elapsed:.0f}s.")
        print_stack_events(cf, stack_name)
        return False

    elapsed = time.time() - start
    print(f"Stack completed in {elapsed:.0f}s.\n")
    return True


def print_stack_events(cf, stack_name):
    """Print recent failed stack events for debugging."""
    try:
        resp = cf.describe_stack_events(StackName=stack_name)
        events = resp["StackEvents"][:20]
        print("\nRecent stack events:")
        for ev in events:
            status = ev.get("ResourceStatus", "")
            if "FAILED" in status or "ROLLBACK" in status:
                reason = ev.get("ResourceStatusReason", "")
                resource = ev.get("LogicalResourceId", "")
                print(f"  {resource}: {status} - {reason}")
    except Exception:
        pass


def get_outputs(session, stack_name):
    cf = session.client("cloudformation")
    resp = cf.describe_stacks(StackName=stack_name)
    stacks = resp["Stacks"]
    if not stacks:
        return {}
    outputs = stacks[0].get("Outputs", [])
    return {o["OutputKey"]: o["OutputValue"] for o in outputs}


def display_results(outputs):
    print("=" * 70)
    print("  AL2023 Runtime API Transfer-Encoding Test Results")
    print("=" * 70)

    summary_raw = outputs.get("TestSummary", "{}")
    try:
        summary = json.loads(summary_raw)
    except json.JSONDecodeError:
        print(f"Could not parse summary: {summary_raw}")
        return

    for size_label, info in sorted(summary.items()):
        te = info.get("transfer_encoding", "?")
        cl = info.get("content_length", "?")
        bl = info.get("body_length", "?")
        encoding = "CHUNKED" if te != "NOT_PRESENT" else "CONTENT-LENGTH"
        print(f"\n  Payload: {size_label:>6s}")
        print(f"    Encoding used:     {encoding}")
        print(f"    Transfer-Encoding: {te}")
        print(f"    Content-Length:     {cl}")
        print(f"    Body received:     {bl} bytes")

    # Print full headers for each payload size
    full_raw = outputs.get("TestFullResults", "{}")
    try:
        full = json.loads(full_raw)
    except json.JSONDecodeError:
        return

    print(f"\n{'-' * 70}")
    print("  Full headers per payload size:")
    print(f"{'-' * 70}")
    for size_label, info in sorted(full.items()):
        if "error" in info:
            print(f"\n  [{size_label}] ERROR: {info['error']}")
            continue
        status = info.get("raw_status_line", "?")
        print(f"\n  [{size_label}] {status}")
        # Handle both dict (all_headers) and string (raw_headers) formats
        headers = info.get("all_headers")
        if isinstance(headers, dict):
            for k, v in sorted(headers.items()):
                print(f"    {k}: {v}")
        else:
            raw = info.get("raw_headers", "")
            for h in raw.split("|"):
                h = h.strip()
                if h:
                    print(f"    {h}")

    print(f"\n{'=' * 70}")

    # Conclusion
    any_chunked = any(
        info.get("transfer_encoding", "NOT_PRESENT") != "NOT_PRESENT"
        for info in summary.values()
    )
    if any_chunked:
        print("  CONCLUSION: AL2023 DOES use chunked transfer-encoding!")
        print("  The C runtime must handle chunked decoding.")
    else:
        print("  CONCLUSION: AL2023 does NOT use chunked transfer-encoding.")
        print("  All responses used Content-Length.")
    print("=" * 70)


def delete_stack(session, stack_name):
    cf = session.client("cloudformation")
    print(f"\nDeleting stack: {stack_name}")
    cf.delete_stack(StackName=stack_name)

    waiter = cf.get_waiter("stack_delete_complete")
    print("Waiting for stack deletion...")
    try:
        waiter.wait(
            StackName=stack_name,
            WaiterConfig={"Delay": 10, "MaxAttempts": 60},
        )
        print("Stack deleted successfully.")
    except Exception as e:
        print(f"Stack deletion may have failed: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Test AL2023 Lambda Runtime API chunked transfer-encoding"
    )
    parser.add_argument(
        "--profile", default=DEFAULT_PROFILE,
        help=f"AWS profile to use (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--region", default=DEFAULT_REGION,
        help=f"AWS region (default: {DEFAULT_REGION})",
    )
    parser.add_argument(
        "--stack-name", default=DEFAULT_STACK_NAME,
        help=f"CloudFormation stack name (default: {DEFAULT_STACK_NAME})",
    )
    parser.add_argument(
        "--no-delete", action="store_true",
        help="Skip the delete prompt; leave the stack running",
    )
    args = parser.parse_args()

    session = get_session(args.profile, args.region)

    # Deploy
    if not deploy_stack(session, args.stack_name):
        print("\nStack deployment failed. Check the AWS Console for details.")
        sys.exit(1)

    # Read and display results
    outputs = get_outputs(session, args.stack_name)
    if not outputs:
        print("No outputs found. Stack may have failed.")
        sys.exit(1)

    display_results(outputs)

    # Cleanup
    if args.no_delete:
        print(f"\nStack '{args.stack_name}' left running. Delete manually when done.")
        return

    try:
        answer = input("\nDelete the test stack? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
        print()

    if answer in ("", "y", "yes"):
        delete_stack(session, args.stack_name)
    else:
        print(f"Stack '{args.stack_name}' left running. Delete manually when done.")


if __name__ == "__main__":
    main()
