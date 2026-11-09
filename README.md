# ipapp — originating-IP service

A tiny [FastAPI](https://fastapi.tiangolo.com/) web app that returns the client's
originating IP address, packaged with a full CI/CD pipeline: **lint → test → build
→ scan → push → helm lint → deploy → smoke test**, plus an AI step that diagnoses
failed runs.

Built for the BigID DevOps home assignment.

## Endpoints

| Method | Path      | Response                               | Purpose          |
|--------|-----------|----------------------------------------|------------------|
| GET    | `/`       | `{"ip": "<addr>", "source": "..."}`    | originating IP   |
| GET    | `/health` | `{"status": "ok"}`                     | liveness probe   |
| GET    | `/ready`  | `{"status": "ready"}`                  | readiness probe  |

### IP resolution (and the spoofing caveat)

Behind a Service / Ingress / load-balancer, the TCP peer the app sees is an
internal hop, not the real client. The real client is carried in the
`X-Forwarded-For` header as a chain `client, proxy1, proxy2`. The app takes the
**left-most** entry as the originating client and falls back to the raw socket
peer (`source: "socket"`) when no header is present.

> ⚠️ `X-Forwarded-For` is client-supplied and therefore **spoofable**. Only trust
> it when every hop in front of the app is a proxy you control that *appends*
> (rather than reflects) the header. For a stricter model you'd parse the chain
> right-to-left against a configured count of trusted proxies — intentionally out
> of scope here to keep the app boring and the DevOps pipeline the focus.

## Run locally

### Bare Python

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000

curl -s localhost:8000/                                   # -> socket peer
curl -s localhost:8000/ -H 'X-Forwarded-For: 1.2.3.4'     # -> {"ip":"1.2.3.4","source":"x-forwarded-for"}
curl -s localhost:8000/health
curl -s localhost:8000/ready
```

### Lint & test

```bash
ruff check .
pytest -q
```

### Docker

```bash
docker build -t ipapp:local .
docker run --rm -p 8000:8000 ipapp:local
curl -s localhost:8000/health
docker images ipapp:local      # compact distroless runtime image
```

The [`Dockerfile`](Dockerfile) is **multi-stage**: dependencies are installed
into a virtualenv on Chainguard's `python:latest-dev` image, then the venv is
copied into a **Chainguard distroless** `python:latest` runtime (no shell, no
package manager, runs as non-root uid 65532). Both stages share the same
Chainguard Python build, so the venv stays ABI-compatible. Chainguard is
continuously rebuilt to carry ~zero known CVEs, which keeps the strict
HIGH/CRITICAL scan gate green.

### Kind (local Kubernetes)

```bash
kind create cluster
docker build -t ipapp:local .
kind load docker-image ipapp:local
helm upgrade --install ipapp charts/ipapp -n staging --create-namespace \
  --set image.repository=ipapp --set image.tag=local --set image.pullPolicy=Never --wait

kubectl -n staging port-forward svc/ipapp 8080:80
curl -s localhost:8080/health
```

## Helm chart (`charts/ipapp/`)

Passes `helm lint`. Everything the assignment asks for is configurable in
[`values.yaml`](charts/ipapp/values.yaml):

- **Probes** — `liveness`, `readiness`, and optional `startup`, each individually
  toggleable with full `httpGet` path and timing knobs.
- **Resources** — `requests`/`limits` with sensible defaults (50m/64Mi →
  200m/128Mi).
- **Hardening** — non-root pod/container security context, read-only root
  filesystem, dropped capabilities, `RuntimeDefault` seccomp.
- **Service** (`ClusterIP` by default) and an optional **Ingress**
  (`ingress.enabled=false` by default).

### Secrets

The chart hardcodes **no secrets**. This app needs none at runtime, but the
injection mechanism is wired as an extension point — `extraEnv` (supports
`valueFrom.secretKeyRef`) and `envFrom` (supports `secretRef`) in `values.yaml`.
Recommended ladder, least → most robust:

1. **Local/dev** — plain values via `env:` in `values.yaml` (non-sensitive only).
2. **Basic k8s** — `kubectl create secret generic ipapp-secrets --from-literal=...`
   then reference it:
   ```yaml
   envFrom:
     - secretRef:
         name: ipapp-secrets
   ```
3. **GitOps** — [sealed-secrets](https://github.com/bitnami-labs/sealed-secrets):
   commit an encrypted `SealedSecret`; the controller decrypts it in-cluster.
4. **Enterprise** — [External Secrets Operator](https://external-secrets.io/):
   an `ExternalSecret` syncs values from AWS Secrets Manager / Vault / GCP SM into
   a native `Secret` that `envFrom` consumes.

## CI (GitHub Actions)

[`.github/workflows/ci.yaml`](.github/workflows/ci.yaml) runs on every push and PR
as a 6-job graph:

| Job | What it does | Gate |
|-----|--------------|------|
| `lint-test` | `ruff check` + `pytest` | fails on lint/test errors |
| `build-push` | build image, tag with **commit SHA** (+`latest` on default branch), push to **GHCR**, export a tarball artifact | — |
| `scan` | **Trivy** image scan + **CycloneDX SBOM** artifact + **SARIF** to the Security tab | **hard-fail** on fixable HIGH/CRITICAL |
| `helm-lint` | `helm lint charts/ipapp` | fails on chart errors |
| `deploy-smoke` | spin ephemeral **kind**, `kind load` the image, `helm upgrade --install` into `staging`, then smoke-test `/health`, `/ready`, `/` | fails on non-200 |
| `ai-diagnose` | on **failure only**: collect failed-job logs → Claude → PR comment + job summary | advisory |

The overall green check / red X **is** the notification (GitHub commit/PR status).

### Image registry

Images are published to **GitHub Container Registry**:

```
ghcr.io/golickarik-spec/ipapp:<commit-sha>
ghcr.io/golickarik-spec/ipapp:latest        # default branch only
```

Pushed with the workflow's built-in `GITHUB_TOKEN` (`packages: write`) — no
external registry credentials. Visible on the repo's **Packages** tab.

### Vulnerability scan & SBOM policy

- **Gate:** Trivy fails the pipeline on **any HIGH/CRITICAL** vulnerability in the
  whole image (application dependencies + base). The Chainguard base is
  continuously rebuilt to ~zero CVEs, so this strict gate stays green without an
  `--ignore-unfixed` relaxation.
- **Escape hatch:** [`.trivyignore`](.trivyignore) — empty by default; every entry
  is a knowingly accepted, dated risk.
- **SBOM:** a CycloneDX SBOM is produced every run and uploaded as the
  `sbom-cyclonedx` artifact.
- **SARIF:** results are uploaded to the GitHub **Security → Code scanning** tab.

> **Note on base-image choice:** a Debian-based distroless image was evaluated
> first, but its pinned tag lags Debian security point-releases, so a strict
> HIGH/CRITICAL gate goes red as Trivy's DB learns of new base-OS CVEs. Chainguard's
> continuously-patched image keeps the strict gate green; the trade-off is that the
> free tier serves the floating `:latest` tag rather than a pinnable digest.

### AI failure diagnosis

[`scripts/ai_diagnose.py`](scripts/ai_diagnose.py) (stdlib-only) sends the tail of
the failed-job logs to Anthropic **Claude (`claude-haiku-4-5`)** and asks for the
failing step, likely root cause, and a suggested fix. The result is posted as a
sticky PR comment and appended to the run's job summary. If `ANTHROPIC_API_KEY` is
not set, the step degrades gracefully with a notice instead of failing.

## Setup checklist (one-time)

1. Create the GitHub repo under `golickarik-spec`, push `main`, and ensure Actions
   is enabled.
2. Add repository secret **`ANTHROPIC_API_KEY`** (the only external secret needed).
3. After the first successful run, the image appears under the repo's **Packages**.

### Demonstrating the AI step

Push a commit that deliberately breaks a test (e.g. change an assertion in
`tests/test_app.py`) on a branch and open a PR. The pipeline fails, and
`ai-diagnose` posts its diagnosis as a PR comment.

> 🔗 _Failed-run link demonstrating the AI step:_ **<add link after first failed run>**

## Repository layout

```
app/                     FastAPI application
tests/                   pytest unit tests
Dockerfile               multi-stage, distroless runtime
charts/ipapp/            Helm chart (deployment, service, optional ingress)
scripts/ai_diagnose.py   AI failure-diagnosis helper
.github/workflows/ci.yaml  the CI/CD pipeline
```
