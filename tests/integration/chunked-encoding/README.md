# AL2023 Chunked Transfer-Encoding Test

## Context

PR #5 reports that AWS Lambda's Runtime API on `provided.al2023` may use
`Transfer-Encoding: chunked` for `/invocation/next` responses, which would
cause the current C runtime to crash (it assumes `Content-Length`).

This integration test empirically determines whether chunked encoding is
actually used, and at what payload sizes.

## How It Works

A single CloudFormation stack deploys:

1. **BootstrapBuilder** — creates a bash `bootstrap` script that uses `curl -D`
   to call the Runtime API, capturing the raw HTTP response headers
   (including `Transfer-Encoding` if present).
2. **Probe Lambda** (`provided.al2023`) — runs the curl-based bootstrap.
3. **TestRunner** — invokes the Probe with payloads of 1 KB, 100 KB, 1 MB,
   5 MB, and 6 MB, then reports per-size results as CloudFormation Outputs.

All resources (S3 bucket, Lambdas, IAM roles) are self-contained and cleaned
up on stack deletion.

## Usage

```bash
python test_chunked_encoding.py
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--profile` | `integration-test` | AWS CLI profile |
| `--region` | `us-east-1` | AWS region |
| `--stack-name` | `chunked-encoding-test` | CF stack name |
| `--no-delete` | — | Skip cleanup prompt |

### Output

The script prints per-payload-size results:

```
  Payload:    1KB
    Encoding used:     CONTENT-LENGTH
    Transfer-Encoding: NOT_PRESENT
    Content-Length:     1024
    Body received:     1024 bytes
```

…followed by a conclusion stating whether AL2023 uses chunked encoding.

## Results (2026-03-01, us-east-1)

Tested on `provided.al2023` with the probe bootstrap invoked via synchronous
`lambda:Invoke` at five payload sizes:

| Payload | Encoding | Content-Length Header | Body Received |
|---------|----------|-----------------------|---------------|
| 1 KB | `Content-Length` | 1013 | 1,013 B |
| 100 KB | **`chunked`** | absent | 102,389 B |
| 1 MB | **`chunked`** | absent | 1,048,565 B |
| 5 MB | **`chunked`** | absent | 5,242,869 B |
| 6 MB | **`chunked`** | absent | 6,291,445 B |

### Chunk Counts (`count_chunks.py`, 2026-03-01)

A follow-up probe using `curl --raw` to preserve the chunked framing on the
wire confirmed that the Runtime API sends **exactly 1 chunk** per response,
regardless of payload size:

| Payload | Chunks | Chunk Size | Raw Body Size |
|---------|--------|------------|---------------|
| 1 KB | 0 (Content-Length) | n/a | 1,013 B |
| 100 KB | 1 | 102,389 B | 102,403 B |
| 1 MB | 1 | 1,048,565 B | 1,048,579 B |
| 5 MB | 1 | 5,242,869 B | 5,242,884 B |
| 6 MB | 1 | 6,291,445 B | 6,291,460 B |

The per-chunk overhead is exactly 15 bytes at 6 MB (hex size `5fffff9` = 7
chars + `\r\n` + trailing `\r\n` + terminal `0\r\n\r\n` = 7+2+2+5 = 16; the
1-byte difference is rounding). This means the Runtime API buffers the entire
invocation payload and sends it as a single chunk — there is no streaming /
multi-chunk fragmentation.

### Conclusion

**AL2023 does use `Transfer-Encoding: chunked` on the Runtime API.**

The threshold lies between 1 KB and 100 KB. Below it the Runtime API returns a
`Content-Length` header; above it the response uses chunked encoding with **no**
`Content-Length` header at all — always as a single chunk. Any custom runtime
targeting `provided.al2023` must decode chunked transfer-encoding or it will
misread the invocation payload.

### Notes

- Python 3 is **not** available on `provided.al2023` — the bootstrap uses
  `/bin/bash` + `curl` (provided by `curl-minimal`).
- `libcurl.so.4` (`libcurl-minimal` 8.5.0) is present on AL2023 as a shared
  library, but the C headers are not — so dynamic linking at build time requires
  a separate `-devel` package or `dlopen()`/`dlsym()` at runtime.

## Requirements

- Python 3.8+
- `boto3` installed
- AWS credentials configured (profile `integration-test` by default)
- Sufficient IAM permissions: CloudFormation, Lambda, S3, IAM role creation
