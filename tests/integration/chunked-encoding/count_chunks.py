#!/usr/bin/env python3
"""
Deploy a Lambda probe that captures raw chunked transfer-encoding framing
from the Runtime API, counts chunks, and reports per-chunk sizes.

Uses curl --raw to preserve chunked encoding in the output body, then
parses chunk boundaries with dd + bash arithmetic.

Usage:
    python count_chunks.py [--profile PROFILE] [--region REGION]
"""

import argparse
import io
import json
import os
import stat
import sys
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

FUNCTION_NAME = "chunk-count-probe"
ROLE_NAME = "chunk-count-probe-role"
PAYLOAD_SIZES = [1024, 102400, 1048576, 5242880, 6291456]

TRUST_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
})

# Bootstrap script for provided.al2023
# Uses curl --raw to get un-decoded chunked body, then parses chunk framing.
BOOTSTRAP = r'''#!/bin/bash
set -euo pipefail

API="$AWS_LAMBDA_RUNTIME_API"

while true; do
    HDRS=$(mktemp)
    RAW_BODY=$(mktemp)

    # --raw: preserve chunked framing in the output body
    curl -sS --raw -D "$HDRS" -o "$RAW_BODY" \
        "http://${API}/2018-06-01/runtime/invocation/next"

    REQUEST_ID=$(grep -oi 'lambda-runtime-aws-request-id: [^ ]*' "$HDRS" \
                 | head -1 | cut -d' ' -f2 | tr -d '\r\n')

    TE=$(grep -oi '^transfer-encoding: [^ ]*' "$HDRS" \
         | head -1 | cut -d' ' -f2 | tr -d '\r\n' || true)
    CL=$(grep -oi '^content-length: [^ ]*' "$HDRS" \
         | head -1 | cut -d' ' -f2 | tr -d '\r\n' || true)

    RAW_SIZE=$(stat -c %s "$RAW_BODY")

    CHUNK_COUNT=0
    DECODED_SIZE=0
    ALL_SIZES=""

    if [ "${TE,,}" = "chunked" ]; then
        OFFSET=0
        while true; do
            SIZE_HEX=$(dd if="$RAW_BODY" iflag=skip_bytes skip=$OFFSET \
                        bs=20 count=1 2>/dev/null \
                        | head -1 | tr -d '\r\n ')

            [ -z "$SIZE_HEX" ] && break

            CHUNK_SIZE=$((16#$SIZE_HEX))
            [ $CHUNK_SIZE -eq 0 ] && break

            CHUNK_COUNT=$((CHUNK_COUNT + 1))
            DECODED_SIZE=$((DECODED_SIZE + CHUNK_SIZE))
            ALL_SIZES="${ALL_SIZES}${CHUNK_SIZE},"

            HEX_LEN=${#SIZE_HEX}
            OFFSET=$((OFFSET + HEX_LEN + 2 + CHUNK_SIZE + 2))
        done
    else
        DECODED_SIZE=$RAW_SIZE
    fi

    # Trim trailing comma
    ALL_SIZES="${ALL_SIZES%,}"

    printf -v RESP \
      '{"transfer_encoding":"%s","content_length":"%s","raw_body_size":%d,"decoded_size":%d,"chunk_count":%d,"chunk_sizes":[%s]}' \
      "${TE:-NOT_PRESENT}" "${CL:-NOT_PRESENT}" \
      "$RAW_SIZE" "$DECODED_SIZE" "$CHUNK_COUNT" "$ALL_SIZES"

    curl -sS -X POST \
        -H "Content-Type: application/json" \
        -d "$RESP" \
        "http://${API}/2018-06-01/runtime/invocation/${REQUEST_ID}/response"

    rm -f "$HDRS" "$RAW_BODY"
done
'''


def get_session(profile, region):
    return boto3.Session(profile_name=profile, region_name=region)


def build_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo("bootstrap")
        info.external_attr = (
            stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP |
            stat.S_IROTH | stat.S_IXOTH
        ) << 16
        zf.writestr(info, BOOTSTRAP)
    return buf.getvalue()


def ensure_role(iam):
    try:
        resp = iam.get_role(RoleName=ROLE_NAME)
        return resp["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise

    print(f"Creating IAM role: {ROLE_NAME}")
    resp = iam.create_role(
        RoleName=ROLE_NAME,
        AssumeRolePolicyDocument=TRUST_POLICY,
    )
    arn = resp["Role"]["Arn"]
    iam.attach_role_policy(
        RoleName=ROLE_NAME,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    # IAM role propagation
    print("Waiting for IAM role propagation...")
    time.sleep(10)
    return arn


def ensure_function(lam, role_arn, zip_bytes):
    try:
        lam.get_function(FunctionName=FUNCTION_NAME)
        print(f"Updating function: {FUNCTION_NAME}")
        lam.update_function_code(
            FunctionName=FUNCTION_NAME,
            ZipFile=zip_bytes,
        )
        waiter = lam.get_waiter("function_updated_v2")
        waiter.wait(FunctionName=FUNCTION_NAME)
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    print(f"Creating function: {FUNCTION_NAME}")
    for attempt in range(5):
        try:
            lam.create_function(
                FunctionName=FUNCTION_NAME,
                Runtime="provided.al2023",
                Handler="bootstrap",
                Role=role_arn,
                Code={"ZipFile": zip_bytes},
                Timeout=300,
                MemorySize=512,
            )
            waiter = lam.get_waiter("function_active_v2")
            waiter.wait(FunctionName=FUNCTION_NAME)
            return
        except ClientError as e:
            if "role" in str(e).lower() and attempt < 4:
                print(f"  Role not ready, retrying in 5s... ({attempt + 1}/5)")
                time.sleep(5)
            else:
                raise


def invoke_probe(lam, payload_size):
    padding = "X" * max(0, payload_size - 20)
    payload = json.dumps({"d": padding})
    resp = lam.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=payload.encode(),
    )
    return json.loads(resp["Payload"].read())


def format_size(n):
    if n >= 1048576:
        return f"{n // 1048576} MB"
    if n >= 1024:
        return f"{n // 1024} KB"
    return f"{n} B"


def display_result(payload_size, result):
    label = format_size(payload_size)
    te = result.get("transfer_encoding", "?")
    cl = result.get("content_length", "?")
    raw = result.get("raw_body_size", "?")
    decoded = result.get("decoded_size", "?")
    n_chunks = result.get("chunk_count", 0)
    sizes = result.get("chunk_sizes", [])

    print(f"\n  Payload: {label} ({payload_size:,} bytes)")
    print(f"    Transfer-Encoding:  {te}")
    print(f"    Content-Length:     {cl}")
    print(f"    Raw body size:     {raw:,}" if isinstance(raw, int) else f"    Raw body size:     {raw}")
    print(f"    Decoded body size: {decoded:,}" if isinstance(decoded, int) else f"    Decoded body size: {decoded}")
    print(f"    Chunk count:       {n_chunks}")

    if sizes:
        unique = sorted(set(sizes))
        if len(unique) <= 3:
            for s in unique:
                count = sizes.count(s)
                print(f"      {count:>4d} x {s:,} bytes")
        else:
            print(f"      first: {sizes[0]:,}")
            print(f"      last:  {sizes[-1]:,}")
            print(f"      min:   {min(sizes):,}")
            print(f"      max:   {max(sizes):,}")


def cleanup(iam, lam):
    print("\nCleaning up...")
    try:
        lam.delete_function(FunctionName=FUNCTION_NAME)
        print(f"  Deleted function: {FUNCTION_NAME}")
    except ClientError:
        pass
    try:
        iam.detach_role_policy(
            RoleName=ROLE_NAME,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )
        iam.delete_role(RoleName=ROLE_NAME)
        print(f"  Deleted role: {ROLE_NAME}")
    except ClientError:
        pass


def main():
    parser = argparse.ArgumentParser(description="Count chunks in Lambda Runtime API responses")
    parser.add_argument("--profile", default="integration-test")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--no-delete", action="store_true")
    args = parser.parse_args()

    session = get_session(args.profile, args.region)
    iam = session.client("iam")
    lam = session.client("lambda")

    zip_bytes = build_zip()
    role_arn = ensure_role(iam)
    ensure_function(lam, role_arn, zip_bytes)

    print("=" * 60)
    print("  AL2023 Runtime API — Chunk Count Probe")
    print("=" * 60)

    results = {}
    for size in PAYLOAD_SIZES:
        label = format_size(size)
        print(f"\n  Invoking with {label} payload...", end="", flush=True)
        try:
            result = invoke_probe(lam, size)
            print(" done")
            display_result(size, result)
            results[size] = result
        except Exception as e:
            print(f" ERROR: {e}")
            results[size] = {"error": str(e)}

    print(f"\n{'=' * 60}")

    if not args.no_delete:
        try:
            answer = input("\nDelete probe resources? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
            print()
        if answer in ("", "y", "yes"):
            cleanup(iam, lam)
        else:
            print(f"Resources left running. Delete manually: {FUNCTION_NAME}, {ROLE_NAME}")
    else:
        print(f"Resources left running (--no-delete).")

    return results


if __name__ == "__main__":
    main()
