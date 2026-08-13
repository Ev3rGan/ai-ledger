# Hong Kong Runtime Benchmark Protocol

This protocol compares the three candidates retained by ADR 0005 without deploying the MVP:

- `tencent-lighthouse-hk`
- `aws-lightsail-hk`
- `alibaba-swas-hk`

Use one Linux node in Hong Kong for each candidate with at least 2 vCPUs and 4 GiB of memory.
The comparison is valid only when all three nodes use the same Git commit, workload image
SHA-256, protocol SHA-256, observer label, and observation window.

## What the fixed workload measures

- Network: three HTTPS health requests from one fixed mainland observer to each node.
- SSE: three complete three-event streams from that same observer.
- Source egress: GitHub Releases and arXiv API reachability, requested inside the node container.
- Model API: unauthenticated DeepSeek and Kimi endpoint reachability from the node. These probes
  never send provider credentials and never make billed inference requests.
- OAuth: GitHub authorization-endpoint reachability from the node; no OAuth login is completed.
- Resource: one fixed 100,000-round SHA-256 loop, a touched 128 MiB allocation, and a synced
  16 MiB write/read inside the representative container.
- Cost: a dated USD-normalized price observation with an official source. It expires after the
  configured evidence window and is not a permanent fact.

The container exposes only allowlisted egress probes. It does not accept caller-supplied URLs.
Protect the temporary public workload with a strong token and a firewall rule restricted to the
fixed observer, then remove each benchmark node after its evidence is captured.

## Build one identical image

Build once, record the image ID, and transfer that exact image to every candidate node. Do not
rebuild independently on each node.

```bash
docker build --pull \
  --file docker/runtime-benchmark.Dockerfile \
  --tag ai-ledger-runtime-benchmark:2026-08-13-v1 \
  .

docker image inspect \
  --format '{{.Id}}' \
  ai-ledger-runtime-benchmark:2026-08-13-v1
```

Export and import the image when the nodes do not share an image registry:

```bash
docker save ai-ledger-runtime-benchmark:2026-08-13-v1 | gzip > runtime-benchmark.tar.gz
gunzip --stdout runtime-benchmark.tar.gz | docker load
```

## Run on each candidate node

Use a distinct random token per benchmark window. The token is not written to result artifacts.
Terminate TLS in front of port 8080 and expose only the fixed observer to the workload URL.

```bash
export RUNTIME_BENCHMARK_TOKEN='<temporary-random-token>'

docker run --detach --rm \
  --name ai-ledger-runtime-benchmark \
  --env RUNTIME_BENCHMARK_TOKEN \
  --publish 127.0.0.1:8080:8080 \
  ai-ledger-runtime-benchmark:2026-08-13-v1
```

The reverse proxy must preserve streaming responses and disable buffering for `/events`.

## Capture from the fixed mainland observer

Run the same command once per candidate during the same observation window. Obtain the current
price from the provider console or official price page; normalize it to USD and retain the exact
official evidence URL and date.

```powershell
$env:RUNTIME_BENCHMARK_TOKEN = '<temporary-random-token>'
$imageSha = '<64 hexadecimal characters from docker image inspect>'

uv run ai-intel-agent benchmark-runtime probe `
  --candidate tencent-lighthouse-hk `
  --target-url https://tencent-benchmark.example `
  --observer mainland-fixed-isp-city-01 `
  --monthly-cost-usd 13.20 `
  --price-observed-at 2026-08-13 `
  --price-source https://cloud.tencent.com/document/product/1207/73452/ `
  --workload-image-sha256 $imageSha `
  --output reports/tencent-lighthouse-hk.json
```

Repeat with the candidate identifiers, URLs, and current price evidence for AWS and Alibaba.
JSON result artifacts are deployment evidence and should be retained with the generated report.

## Compare

```powershell
uv run ai-intel-agent benchmark-runtime compare `
  --input reports/tencent-lighthouse-hk.json `
  --input reports/aws-lightsail-hk.json `
  --input reports/alibaba-swas-hk.json `
  --output reports/hong-kong-runtime-benchmark.md
```

The comparator fails closed unless it receives exactly one result for each configured candidate,
all generated with the same protocol, workload image, and observer. A candidate is eligible only
when every Network, SSE, Source egress, Model API, OAuth, Resource, and Cost measurement passes.
Eligible candidates are ordered by median Network latency, median SSE first-event latency, CPU
workload latency, monthly cost, and stable identifier. No opaque aggregate score is used.

## Interpretation limits

- Results describe one dated node configuration, observer, ISP path, and workload image.
- Repeat the benchmark after material provider, route, instance, image, or price changes.
- The OAuth probe tests endpoint reachability, not callback correctness or administrator access.
- The model probes test egress only, not model quality, latency, price, or availability under load.
- This protocol does not run acquisition, Research, production OAuth, or database migrations.
