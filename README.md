# ipapp — originating-IP service with a full CI/CD pipeline

A tiny [FastAPI](https://fastapi.tiangolo.com/) web app that returns the client's
originating IP address, wrapped in a production-shaped CI/CD pipeline:
**lint → test → build → scan → sign → push → deploy → smoke test**, plus supply-chain
signing/attestation, broad security scanning, and an AI step that diagnoses failed runs.

Built for the BigID DevOps home assignment.

### What's in the box
- **App** — `GET /` returns the originating IP; `GET /health` and `GET /ready` back the
  Kubernetes probes.
- **Dockerfile** — multi-stage, **Chainguard distroless** runtime, non-root, ~zero-CVE.
- **Helm chart** (`charts/ipapp/`) — configurable probes, resources, security context,
  optional ingress, and a no-hardcoded-secrets injection mechanism.
- **CI** (GitHub Actions) — a 7-job pipeline + a CodeQL workflow (details below).
- **Supply-chain** — image **cosign-signed**, **SLSA provenance**, **SBOM attestation**.
- **Security scanning** — CodeQL, gitleaks, Bandit, Trivy (image + config), hadolint.
- **AI failure diagnosis** — on a failed run, Claude posts a formatted root-cause comment.

---

## Repository layout

```
.
├── app/
│   ├── __init__.py
│   └── main.py                 # FastAPI: /, /health, /ready
├── tests/
│   └── test_app.py             # pytest unit tests
├── requirements.txt            # runtime deps (fastapi, uvicorn)
├── requirements-dev.txt        # + pytest, httpx, ruff
├── pyproject.toml              # ruff + pytest config
├── Dockerfile                  # multi-stage, Chainguard distroless runtime
├── .dockerignore / .gitignore / .trivyignore
├── charts/ipapp/               # Helm chart (Deployment, Service, optional Ingress)
├── scripts/ai_diagnose.py      # AI failure-diagnosis helper (stdlib only)
└── .github/workflows/
    ├── ci.yaml                 # the main pipeline
    └── codeql.yml              # CodeQL SAST (separate workflow)
```

---

## Prerequisites

You only need **Python + git** to run and test the app locally — everything else is done
for you in CI. Install the rest only for the optional local steps.

| Tool | Needed for | Required? |
|------|-----------|-----------|
| **Python 3.11+** | run the app, lint, unit tests | required (local) |
| **git** | push to GitHub / run the pipeline | required |
| **Docker** | build/run the container image | optional (local) |
| **kind + kubectl + Helm** | deploy to a local Kubernetes cluster | optional (local) |
| **Trivy** | run the image/config scan locally | optional (local) |
| **cosign** | verify the image signature/attestations | optional |
| **GitHub account** | run CI, publish the image to GHCR | required for CI |

---

## Quickstart — run the app locally

```bash
# from the repo root
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

uvicorn app.main:app --reload --port 8000
```

In another terminal:

```bash
curl -s localhost:8000/                                 # -> {"ip":"127.0.0.1","source":"socket"}
curl -s localhost:8000/ -H 'X-Forwarded-For: 1.2.3.4'   # -> {"ip":"1.2.3.4","source":"x-forwarded-for"}
curl -s localhost:8000/health                           # -> {"status":"ok"}
curl -s localhost:8000/ready                            # -> {"status":"ready"}
```

### Lint & test

```bash
ruff check .
pytest -q
```

## Run in Docker

```bash
docker build -t ipapp:local .
docker run --rm -p 8000:8000 ipapp:local
curl -s localhost:8000/health
docker images ipapp:local      # compact distroless runtime image
```

The [`Dockerfile`](Dockerfile) is **multi-stage**: dependencies are installed into a
virtualenv on Chainguard's `python:latest-dev` image, then the venv is copied into a
**Chainguard distroless** `python:latest` runtime (no shell, no package manager, runs as
non-root uid 65532). Both stages share the same Chainguard Python build, so the venv stays
ABI-compatible. Chainguard is continuously rebuilt to carry ~zero known CVEs, which keeps
the strict HIGH/CRITICAL scan gate green. (The runtime has no shell/curl — the CI smoke
test curls the app from a *separate* pod, never by `exec`-ing into it.)

## Run on local Kubernetes (kind)

```bash
kind create cluster
docker build -t ipapp:local .
kind load docker-image ipapp:local                      # load into the cluster's nodes

helm upgrade --install ipapp charts/ipapp -n staging --create-namespace \
  --set image.repository=ipapp --set image.tag=local --set image.pullPolicy=Never --wait

kubectl -n staging port-forward svc/ipapp 8080:80
curl -s localhost:8080/health
```

`image.pullPolicy=Never` + `kind load` make the cluster use the **locally built** image
instead of trying to pull it from a registry.

---

## The app

| Method | Path      | Response                            | Purpose         |
|--------|-----------|-------------------------------------|-----------------|
| GET    | `/`       | `{"ip": "<addr>", "source": "..."}` | originating IP  |
| GET    | `/health` | `{"status": "ok"}`                  | liveness probe  |
| GET    | `/ready`  | `{"status": "ready"}`               | readiness probe |

### IP resolution (and the spoofing caveat)

Behind a Service / Ingress / load-balancer, the TCP peer the app sees is an internal hop,
not the real client. The real client is carried in the `X-Forwarded-For` header as a chain
`client, proxy1, proxy2`. The app takes the **left-most** entry as the originating client
and falls back to the raw socket peer (`source: "socket"`) when no header is present.

> ⚠️ `X-Forwarded-For` is client-supplied and therefore **spoofable**. Only trust it when
> every hop in front of the app is a proxy you control that *appends* (rather than reflects)
> the header. A stricter model would parse the chain right-to-left against a configured count
> of trusted proxies — intentionally out of scope here to keep the app boring and the DevOps
> pipeline the focus.

## Helm chart (`charts/ipapp/`)

Passes `helm lint`. Everything is configurable in [`values.yaml`](charts/ipapp/values.yaml):

- **Probes** — `liveness`, `readiness`, and optional `startup`, each individually toggleable
  with full `httpGet` path and timing knobs.
- **Resources** — `requests`/`limits` with sensible defaults (50m/64Mi → 200m/128Mi).
- **Hardening** — non-root pod/container security context, read-only root filesystem,
  dropped capabilities, `RuntimeDefault` seccomp.
- **Service** (`ClusterIP` by default) and an optional **Ingress** (`ingress.enabled=false`).

### Secrets

The chart hardcodes **no secrets**. This app needs none at runtime, but the injection
mechanism is wired as an extension point — `extraEnv` (supports `valueFrom.secretKeyRef`)
and `envFrom` (supports `secretRef`) in `values.yaml`. Recommended ladder, least → most
robust:

1. **Local/dev** — plain values via `env:` in `values.yaml` (non-sensitive only).
2. **Basic k8s** — `kubectl create secret generic ipapp-secrets --from-literal=...` then
   reference it via `envFrom: [{ secretRef: { name: ipapp-secrets } }]`.
3. **GitOps** — [sealed-secrets](https://github.com/bitnami-labs/sealed-secrets): commit an
   encrypted `SealedSecret`; the controller decrypts it in-cluster.
4. **Enterprise** — [External Secrets Operator](https://external-secrets.io/): an
   `ExternalSecret` syncs values from AWS Secrets Manager / Vault / GCP SM into a native
   `Secret` that `envFrom` consumes.

---

## Set up CI on your own GitHub repo — step by step

The pipeline is **repo-agnostic**: it derives the image path from your repository owner and
the signing identity from your repository name, so **you don't need to edit any workflow
file**. Just publish the code to a repo you own.

1. **Create a new GitHub repository.** A **public** repo is recommended — then CodeQL, code
   scanning, GHCR, and attestations are all free. (For a private repo, see the CodeQL note
   in *Troubleshooting* below.)

2. **Publish the code.** From the unzipped folder:
   ```bash
   # If the folder is NOT already a git repo (no .git/):
   git init -b main
   git add .
   git commit -m "Initial import"
   git remote add origin https://github.com/<you>/<repo>.git

   # If it IS already a git repo pointing at the original remote:
   git remote set-url origin https://github.com/<you>/<repo>.git

   git push -u origin main
   ```

3. **(Optional) Add the AI-diagnosis secret.** Repo → *Settings → Secrets and variables →
   Actions → New repository secret* → name `ANTHROPIC_API_KEY`, value = your Anthropic API
   key. Without it the pipeline still works; the AI step just skips with a notice.

4. **Watch it run.** The push triggers both workflows. Results show up here:
   | Result | Where |
   |--------|-------|
   | Job pass/fail (the notification) | **Actions** tab / the commit's status check |
   | Published container image | **Packages** (right sidebar of the repo) |
   | Scan findings (CodeQL, Trivy, gitleaks, Bandit, hadolint) | **Security → Code scanning** |
   | SBOM | run artifact `sbom-cyclonedx` |
   | AI diagnosis (on failure) | sticky **PR comment** + the run's **Summary** |

5. **Published image path:** `ghcr.io/<your-owner>/ipapp:<commit-sha>` (plus `:latest` on
   the default branch), pushed with the built-in `GITHUB_TOKEN` — no external registry
   credentials needed.

---

## Pipeline explained — every job

[`.github/workflows/ci.yaml`](.github/workflows/ci.yaml) runs on every push and PR. Jobs run
in parallel where possible and gate each other via `needs:`.

| Job | What it does | Gate |
|-----|--------------|------|
| `lint-test` | `ruff check` + `pytest` | **blocks** on lint/test failure |
| `build-push` | build image, tag with commit SHA, push to GHCR, **cosign-sign**, verify, attach **SLSA provenance** | — |
| `scan` | **Trivy** image scan + **CycloneDX SBOM** (also **cosign-attested**) + SARIF | **blocks** on HIGH/CRITICAL |
| `static-analysis` | **gitleaks** + **Bandit** + **hadolint** + **Trivy config** | secrets/SAST block; lint/misconfig annotate |
| `helm-lint` | `helm lint charts/ipapp` | **blocks** on chart errors |
| `deploy-smoke` | ephemeral **kind** → `kind load` → `helm upgrade --install` into `staging` → smoke-test `/health`, `/ready`, `/` | **blocks** on non-200 |
| `ai-diagnose` | **on failure only**: collect failed-job logs → Claude → formatted PR comment + job summary | advisory |

A separate [`codeql.yml`](.github/workflows/codeql.yml) runs **CodeQL** SAST on push/PR and
weekly.

**Per job, in detail:**

- **`lint-test`** — sets up Python 3.11, installs dev deps, runs `ruff check .` then
  `pytest -q`. This is the first gate; everything else `needs` it.
- **`build-push`** — logs in to GHCR with `GITHUB_TOKEN`, `docker build`s the image, tags it
  with the commit SHA (and `:latest` on the default branch), pushes it, resolves the pushed
  **digest**, then **cosign-signs** the digest (keyless, via GitHub OIDC), **verifies** that
  signature in-line, and records **SLSA build provenance**. It also exports the image as a
  tarball artifact for the downstream jobs.
- **`scan`** — loads the image tarball, generates a **CycloneDX SBOM** (uploaded as an
  artifact and **cosign-attested** to the image), uploads a SARIF report to the Security tab,
  then runs the **gate**: `trivy image` fails on any HIGH/CRITICAL (see `.trivyignore` for the
  one documented, dated exception).
- **`static-analysis`** — independent security sweep. **Blocking:** gitleaks (committed
  secrets, full history) and Bandit (Python SAST). **Annotate-only** (SARIF to the Security
  tab, non-blocking): hadolint (Dockerfile) and Trivy config (Dockerfile + Helm misconfig) —
  because they flag the Chainguard `:latest` base pin, which is unavoidable on the free tier.
- **`helm-lint`** — `helm lint charts/ipapp`.
- **`deploy-smoke`** — spins an ephemeral **kind** cluster, `kind load`s the built image,
  `helm upgrade --install`s into the `staging` namespace, waits for rollout, then curls
  `/health`, `/ready`, and `/` from inside the cluster and fails on a non-200. Tears down with
  the runner.
- **`ai-diagnose`** — runs only when something failed. Collects the failed job's logs via the
  GitHub API, sends the tail to Claude, and posts a formatted diagnosis (see below).
- **`codeql` (separate workflow)** — GitHub's first-party SAST for Python; results land under
  Security → Code scanning.

The overall green check / red X **is** the notification (GitHub commit/PR status).

---

## Security features

### Supply-chain trust (keyless — no keys to manage)

Every pushed image is **signed and attested** with [Sigstore cosign](https://docs.sigstore.dev/),
keyless, using the workflow's GitHub OIDC identity.

- **Image signature** — `cosign sign`, then `cosign verify` in the same run.
- **SLSA build provenance** — `actions/attest-build-provenance` records how/where the image was
  built; viewable on the GHCR package.
- **SBOM attestation** — the CycloneDX SBOM is attached to the image digest via
  `cosign attest --type cyclonedx`.

Verify it yourself (replace `<OWNER>/<REPO>` with your repo and `<DIGEST>` with the pushed
`sha256:…`, visible in the `build-push` logs or on the Packages page):

```bash
cosign verify ghcr.io/<OWNER>/ipapp@<DIGEST> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp "^https://github.com/<OWNER>/<REPO>/.github/workflows/ci.yaml@.*"

cosign verify-attestation --type cyclonedx ghcr.io/<OWNER>/ipapp@<DIGEST> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp "^https://github.com/<OWNER>/<REPO>/.github/workflows/ci.yaml@.*"
```

### Security scanning

Results land in **Security → Code scanning** as SARIF (each tool under its own category):

| Tool | Scope | Policy |
|------|-------|--------|
| **CodeQL** | Python SAST (push/PR + weekly) | annotate |
| **gitleaks** | committed secrets (full git history) | **blocking** |
| **Bandit** | Python security SAST | **blocking** |
| **Trivy image** | image CVEs | **blocking** (HIGH/CRITICAL) |
| **hadolint** | Dockerfile best practices | annotate |
| **Trivy config** | Dockerfile + Helm misconfig | annotate |

Dockerfile-lint and misconfig scans are **annotate-only** because they flag the Chainguard
`:latest` base pin (`DL3007` / `DS001`), which is unavoidable on the free tier — gating on it
would redden the pipeline for a documented, accepted trade-off. Secret and SAST findings block.

### Vulnerability-scan policy & base image

The gate fails on **any HIGH/CRITICAL** in the whole image. Chainguard is continuously rebuilt
to ~zero CVEs, so the strict gate stays green without `--ignore-unfixed`. The one documented
exception lives in [`.trivyignore`](.trivyignore) (a base-image `setuptools` CVE unused at
runtime). A Debian-based distroless image was tried first but its pinned tag lags Debian
security releases, reddening a strict gate — Chainguard avoids that at the cost of a floating
`:latest` tag (free tier) rather than a pinnable digest.

### AI failure diagnosis

[`scripts/ai_diagnose.py`](scripts/ai_diagnose.py) (stdlib-only) sends the tail of the failed-job
logs to Anthropic **Claude (`claude-haiku-4-5`)**, which returns a **structured JSON** diagnosis
(failing step, root cause, suggested fix, confidence, category). The script renders it into a
consistent markdown comment — a confidence badge, a context table (run, commit, branch, failed
jobs), numbered fix steps, and a collapsible log excerpt — posted as a sticky PR comment and
appended to the job summary. If the reply isn't valid JSON it falls back to prose; if
`ANTHROPIC_API_KEY` is unset it degrades gracefully with a notice.

> 🔗 **Reference run (original author's repo)** demonstrating the AI step on a real failure:
> - Failed pipeline run: https://github.com/golickarik-spec/BigID/actions/runs/42702848464
> - AI diagnosis as a PR comment: https://github.com/golickarik-spec/BigID/pull/1
>
> To reproduce on your own repo: break a test (e.g. change an assertion in
> `tests/test_app.py`) on a branch, open a PR, and the `ai-diagnose` job posts its diagnosis.

---

## Troubleshooting & portability gotchas

Things that commonly bite when setting this up on a **new machine** and a **different repo**:

- **Workflow token is read-only.** If GHCR push, SARIF upload, or attestation fail with a
  403/permission error, your org/repo defaults `GITHUB_TOKEN` to read-only. Fix: repo →
  *Settings → Actions → General → Workflow permissions* → **Read and write permissions**.
  (The workflow already requests the scopes it needs; an org policy can still cap them.)

- **Private repo + CodeQL / code scanning.** CodeQL `analyze` and SARIF uploads require
  **GitHub Advanced Security** on **private** repos (they're **free on public** repos). On a
  private repo without Advanced Security, `codeql.yml` goes red (the `ci.yaml` SARIF uploads are
  `continue-on-error`, so the main pipeline stays green). Fix: **make the repo public**, or
  delete `.github/workflows/codeql.yml` and the `Upload … SARIF` steps.

- **GHCR package is private on first push.** The first run creates a *private* package under
  your namespace. CI doesn't care (it side-loads the image into kind), but to pull it elsewhere
  make the package public (Packages → the package → *Package settings → Change visibility*) or
  `docker login ghcr.io` first.

- **`cosign verify` fails on identity.** The `--certificate-identity-regexp` must match **your**
  repo path, not the author's — use `https://github.com/<OWNER>/<REPO>/.github/workflows/ci.yaml@.*`.

- **Local `docker build` can't pull Chainguard.** The build pulls
  `cgr.dev/chainguard/python:latest` and `:latest-dev` anonymously — it needs internet and may
  be blocked behind a corporate proxy or registry mirror. Allowlist `cgr.dev` or mirror the image.

- **Python version.** The app targets **Python 3.11+**. Older interpreters may fail to install
  the pinned deps.

- **Prefer `git clone` over the zip for CI files.** A zip made on Windows can carry CRLF line
  endings. GitHub Actions tolerates them, but cloning (or `git archive`) keeps workflow/script
  files as LF, which is cleaner.

- **Local kind deploy pulls instead of using your image.** Make sure you `kind load
  docker-image …` and pass `--set image.pullPolicy=Never` so the cluster uses the local build.

- **Trivy DB download hiccups.** Trivy pulls its vuln DB from `ghcr.io`; on a rare rate-limit,
  just re-run the job.
