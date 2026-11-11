# SadSha Build Pro on Contabo VPS 8 — deployment guide

**Status (2026-11-11, hosting prep):** this is the deployment guide for the SadSha Build Pro
hosted service — the GC portal as a multi-tenant SaaS. It is written against the repo as it stands
on branch `remix/massing-saas-hosting`. The Docker full stack has **not** been run anywhere yet:
Docker is not installed on the authoring VM, so what is validated here is the static side — every
compose file parses, the env template covers every variable the compose files and this guide name,
and every command below matches the repo's actual file layout (paths, service names, ports). The
first live `up` on the VPS is still owed, and §12 lists exactly what it has to prove.

## 0. The decisions this guide is built on

| Decision | Value | Source |
| --- | --- | --- |
| Product | SadSha Build Pro — GC portal first; BIM authoring gated to a higher tier | Remixer design §4 / clean report §8 |
| Price | **$199/mo flat** (Pro); 30-day free trial on one live project, paid from month 2 | Qasim, fixed |
| Server | Contabo **Cloud VPS 8 — €14/mo (~$16–17)** | Clean report §8 |
| Server budget | **$25/mo** | Clean report §8 |
| Margin | **~$183 per customer per month** ($199 − ~$16 server) **before support** | Arithmetic, not a promise — support time is the real cost |
| Billing | Lemon Squeezy (temporary gateway; Paddle is stuck on proof-of-address) | Remixer design §2 |
| Shape | One VPS, Docker Compose, Caddy for TLS — no Kubernetes, no managed database | This guide |

At one paying customer the server is already covered about twelve times over; at ten customers
the same €14 box is still the server, and the cost that grows is support, not hosting. That is the
whole economic argument for this shape — do not "fix" it by buying a bigger box early.

**Billing blocker — read before promising any date:** the Lemon Squeezy "Toolzo" store is still
**"In Review" (Test mode ON)**. Live billing cannot activate until it is approved, and no Lemon
Squeezy products or variants may be created until then. The environment variables for billing are
already plumbed through the compose stack and the env template (§4), but the code that reads them
does not exist yet — tenancy, billing and BIM gating are separate Remixer work. A deployment made
from this guide today is a working GC portal with auth and per-project roles; it is not yet a
store that can take a subscription.

## 1. The server

Contabo Cloud VPS 8, as listed at the time of writing (confirm in the Contabo panel at purchase —
headline prices are for the first 24 months and region, VAT and term change the final figure):

| Spec | Value |
| --- | --- |
| vCPU | 8 (shared) |
| RAM | 24 GB |
| Disk | 300 GB SSD (150 GB NVMe option on the same plan) |
| Port / traffic | 600 Mbit/s, unlimited traffic (fair-use) |
| Price | €14/mo (~$16–17) |
| Snapshots | 3 included on this plan — a second layer, not the backup (§9) |
| OS | **Ubuntu 24.04 LTS** (pick this at order time; every command below assumes it) |

Why this size and not the cheapest box: the stack runs the API with 4 uvicorn workers plus a
separate job worker that does full-model IFC parses, Postgres, MinIO, Redis and nginx, and the
converter holds a whole model in RAM. The clean report's own warning is that a customer-facing BIM
tier "needs a real server, not the $6.50 box" — 24 GB is what makes the default worker counts and
the generous memory headroom in §10 affordable. The resource-limit blocks in the compose files stay
**off** by default on purpose (a too-low cap OOM-kills a legitimate large-IFC conversion mid-run);
§10 says when to turn them on.

## 2. First boot — OS, access, firewall, DNS

```bash
# 1. Log in as root over SSH (Contabo mails the root password / key at provisioning),
#    update, and make a deploy user — do not run the stack as root.
apt-get update && apt-get upgrade -y
adduser deploy && usermod -aG sudo deploy

# 2. Firewall: SSH + HTTP + HTTPS only. Nothing else is ever opened on this box.
#    (Contabo also has a cloud firewall in its panel — if you enable it, allow the same
#    three there; a cloud rule that blocks 443 will break certificate issuance in §7.)
ufw allow OpenSSH
ufw allow 80,443/tcp
ufw enable
ufw status numbered   # expect: 22, 80, 443 — and nothing else

# 3. DNS: an A record for your public hostname pointing at the VPS IPv4 address.
#    Do this BEFORE the first boot of the stack — Caddy cannot issue a certificate
#    for a name that does not resolve to this box (§7).
#    Example:  app.example.com  A  203.0.113.10
```

Pick the hostname now and keep it: it becomes `DOMAIN` in §4, it is baked into the TLS
certificate, and changing it later means re-issuing. `app.example.com` is used below as the
placeholder — replace it everywhere with the real name.

## 3. Install Docker + Compose

Docker's own apt repository (do not use the distro `docker.io` package or a snap — both have
shipped stale Compose versions on Ubuntu):

```bash
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

docker --version          # expect: Docker version 2x.x.x
docker compose version    # expect: Docker Compose version v2.x.x
usermod -aG docker deploy # log out and back in for this to apply
```

## 4. Get the code and configure it

```bash
# As the deploy user, in /srv (or $HOME — pick one and keep it; the backup cron in §9
# assumes the repo path you choose here, so write it down).
sudo mkdir -p /srv && sudo chown deploy:deploy /srv && cd /srv
git clone https://github.com/fnjlabs-arch/massing.git sadsha-build-pro
cd sadsha-build-pro
git checkout remix/massing-saas-hosting
git log --oneline -1   # record the commit you deployed; put it in the runbook

cp .env.example .env
```

Now edit `.env`. Every variable the stack needs is already in the template with a safe
placeholder — generate real secrets, do not reuse the placeholders:

```bash
openssl rand -hex 32   # run once per secret: AEC_AUTH_SECRET, AEC_API_KEY
openssl rand -hex 24   # run once per password: POSTGRES_PASSWORD, S3_SECRET_KEY
```

### 4.1 Required variables

| Variable | What it is | Notes |
| --- | --- | --- |
| `DOMAIN` | Public hostname, e.g. `app.example.com` | **Required** — the prod overlay fails closed if unset. DNS A record must already point here (§2). |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Postgres credentials + database | `POSTGRES_PASSWORD` is required in prod (fail-closed). Do not keep `bim`. |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` / `S3_BUCKET` | MinIO root credentials + bucket | `S3_SECRET_KEY` is required in prod (fail-closed). Do not keep `minioadmin`. |
| `S3_ENDPOINT` | Object-store endpoint | Default `http://minio:9000` (the compose MinIO). Override only for external S3. |
| `AEC_AUTH_SECRET` | Signs login bearer tokens | **Required in prod** — unset means forgeable tokens. 32+ hex chars. |
| `AEC_API_KEY` | Bearer token treated as admin | Service-to-service / automation. Set a strong value in prod. |
| `AEC_RBAC` | `1` = enforce project roles | Forced to `1` by the prod overlay; the API also refuses to boot on Postgres without it. |
| `AEC_ADMIN_EMAILS` | Comma-separated platform admins | Without it, no account can reach platform-admin operations (user management, audit feed). |
| `AEC_CORS_ORIGINS` | Allowed browser origins | Same-origin behind Caddy, so the default is fine unless a cross-origin client exists. |
| `WEB_PORT` | Host port for the web app | Default `8080`; bound to localhost only by the prod overlay (§8). |
| `MINIO_IMAGE` | MinIO image override | Only if the pinned image no longer pulls — see the warning in §6. |

### 4.2 The database URL — derived, not set

`DATABASE_URL` is the variable the API itself reads (`postgresql+psycopg://…`), but in a compose
deploy **you do not set it**: `docker-compose.yml` derives it from `POSTGRES_USER` /
`POSTGRES_PASSWORD` / `POSTGRES_DB`, and the prod overlay re-derives it with the fail-closed
password check. It is documented in `.env.example` for one case only — running the API outside
compose, where it is the only way to point at Postgres. If you set it in `.env` for a compose
deploy, the compose value wins and your value is silently ignored.

### 4.3 Storage settings

- **MinIO (the default):** `S3_ENDPOINT=http://minio:9000`, `S3_BUCKET=aec-bim`, credentials
  above. Objects (source IFC, `.frag` tiles, `props.json`, attachments) live in the `minio-data`
  volume. This is the only storage that is backed up with the database (§9).
- **Local files (no MinIO):** if `S3_ENDPOINT` is unset, the API falls back to the local
  backend in `STORAGE_DIR` (`/app/storage` in the container). That directory is now a named
  volume (`storage-data`, mounted on both the API and the worker) so uploads survive a
  container recreate — but it is **not** in the backup script's volume list unless MinIO is
  also running, so prefer MinIO for anything customer-facing.
- **Uploaded source IFCs** live on the separate `ifc-data` volume (`/app/ifc`), which is backed
  up alongside the database and MinIO.

### 4.4 SaaS variables — present, not yet live

| Variable | Default | Meaning |
| --- | --- | --- |
| `SAAS_MODE` | `0` | `1` = SaaS mode (org scoping + BIM gating). Default off; dev behaviour unchanged. |
| `BILLING_PROVIDER` | `lemonsqueezy` | Provider name. Paddle is the later swap, not built now. |
| `LEMONSQUEEZY_API_KEY` | _(empty)_ | Lemon Squeezy API key — Test mode key until the store is approved. |
| `LEMONSQUEEZY_STORE_ID` | _(empty)_ | Lemon Squeezy store id (Toolzo store, once approved). |
| `LEMONSQUEEZY_VARIANT_PRO` | _(empty)_ | Variant id for the $199/mo Pro plan — from the Lemon dashboard **after** approval. |
| `LEMONSQUEEZY_VARIANT_ENTERPRISE` | _(empty)_ | Variant id for the enterprise/BIM tier — same source. |
| `LEMONSQUEEZY_WEBHOOK_SECRET` | _(empty)_ | HMAC secret for Lemon Squeezy webhook signature verification. |
| `ENTERPRISE_PRICE_MONTHLY` | `399` | **Decided by Mili on Qasim's "As you wish" delegation** (open to his override). |

These are passed through the compose stack to the API and worker today, but **nothing reads
them yet** — the tenancy, billing and gating code is separate Remixer work. Empty defaults mean
"SaaS features off" and change nothing in the current build. The preflight script
(`validate_prod_config.py`) has no SaaS checks yet either; when the billing code lands, it
should gain one (warn when `SAAS_MODE=1` with Lemon Squeezy keys missing).

## 5. The stack as it will run

`docker-compose.yml` (base) + `docker-compose.prod.yml` (overlay: Caddy, enforced auth,
restart policies, localhost-bound ports). Bring-up is always with **both** files and the
`full` profile — the base file alone is the dev/self-host shape without TLS.

| Service | Image | Ports (prod) | Volumes | Healthcheck |
| --- | --- | --- | --- | --- |
| `postgres` | `postgres:17` | 127.0.0.1:5432 | `postgres-data` | `pg_isready` (uses `POSTGRES_USER`/`POSTGRES_DB` from env) |
| `minio` | `minio/minio:RELEASE.2025-04-22T22-12-26Z` (see §6) | 127.0.0.1:9000, 127.0.0.1:9001 | `minio-data` | `mc ready local` |
| `redis` | `redis:7-alpine` | — (internal only) | — (ephemeral by design) | `redis-cli ping` |
| `api` | built from `services/api/Dockerfile` | 127.0.0.1:8000 | `ifc-data`, `storage-data` | `GET /health`, 4 uvicorn workers |
| `worker` | same API image, `python -m aec_api.worker` | — (serves nothing) | `ifc-data`, `storage-data` | disabled (binds no port; the image's own healthcheck would never pass) |
| `web` | built from `apps/web/Dockerfile` (nginx) | 127.0.0.1:`WEB_PORT` (8080) | — | `wget` on `/` (nginx:alpine has no curl) |
| `caddy` | `caddy:2` | **80, 443 (the only public ports)** | `caddy-data` (certs), `caddy-config` | — (gates on web healthy) |
| `converter` | built from `services/converter/Dockerfile` | — (job-style, `--profile tools`) | `./data:/data` bind mount | — |

Notes on the table, each of which was a real gap before this guide:

- **Postgres is 17, not 16.** The Remixer design doc says "Postgres 16", but the repo moved
  16 → 17 (and the migrations drift-guard runs `postgres:17`). Do not downgrade the pin to
  match the design doc.
- **The API refuses to boot open on Postgres.** With a server database and no
  `AEC_ALLOW_OPEN=1`, it hard-fails without `AEC_RBAC=1` and a real `AEC_AUTH_SECRET`. That
  is the backstop behind the preflight in §6 — if you bypass the preflight, the container
  still will not serve an open deployment by accident.
- **The worker is not optional.** The base file sets `AEC_JOB_WORKER=off` on the API, so
  with no worker running every job enqueue succeeds, every caller is told the work is under
  way, and none of it happens. Nothing raises. If conversions back up, scale it
  (`--scale worker=3`) — job claim is a compare-and-swap and `pid_lock` takes a Postgres
  advisory lock, so two workers cannot both win one job.
- **The converter is job-style, not a daemon:** `docker compose run --rm converter
  /data/model.ifc /data/model.frag`. The `./data` directory does not exist in the repo and
  compose auto-creates a missing bind-mount directory as root, while the converter runs as
  non-root `appuser` (uid 10001) — so pre-create it before first use:
  `mkdir -p data && sudo chown 10001:10001 data`. The converter dependency is pinned to
  `@thatopen/fragments@3.4.7` in both the API image and the converter image (CI enforces the
  pair); a mismatch means the server emits `.frag` the browser cannot parse.
- **Never run the demo seed against production** (`--profile seed`). The seeder refuses when
  projects already exist; do not `--force` it.

## 6. Bring it up

```bash
cd /srv/sadsha-build-pro

# 0. Prove the MinIO image still pulls BEFORE anything else. Upstream archived the
#    minio/minio repository in April 2026 and the image was reported removed from Docker
#    Hub in September–October 2026, so a fresh pull of the pinned tag may fail with
#    "repository does not exist / pull access denied" even though the tag is pinned.
#    If the pull below fails, do NOT edit the compose file blind — set the override in
#    .env to a pullable image (community fork, same S3 API and on-disk format):
#      MINIO_IMAGE=pgsty/minio:RELEASE.2026-04-17T00-00-00Z
#    or a Quay image (needs a Quay login since 24 Sep 2026), then re-run the pull.
#    No CI job in this repo pulls this image, so nothing here proves it still pulls —
#    this command, on this VPS, is the proof.
docker pull minio/minio:RELEASE.2025-04-22T22-12-26Z

# 1. Render the merged config and read it before trusting it. This is where a bad
#    interpolation, a missing required variable, or a wrong port shows up — cheaply.
DOMAIN=app.example.com docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile full config > /tmp/compose-resolved.yml
grep -E "DOMAIN|DATABASE_URL|SAAS_MODE|LEMONSQUEEZY" /tmp/compose-resolved.yml

# 2. The go/no-go gate. Exit 0 required — it asserts RBAC on, a real auth secret,
#    cookie-secure + HSTS + CSP, Redis wired for multi-worker rate limiting, and
#    non-default Postgres/MinIO credentials. (The script is copied into the API image
#    at build time; before that fix the documented command could not run at all.)
DOMAIN=app.example.com docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  run --rm api python /app/scripts/validate_prod_config.py

# 3. Build + start, detached.
DOMAIN=app.example.com docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile full up -d --build

# 4. Watch it become healthy (postgres + minio first, then api, then web, then caddy).
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile full ps
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile full logs -f api

# 5. Verify from inside the box, then from the outside.
curl -s http://localhost:8000/health            # expect {"status":"ok"}
curl -s https://app.example.com/api/health     # expect {"status":"ok"} via Caddy + nginx
curl -s https://app.example.com/ | head -c 200    # expect the SPA HTML

# 6. Create the first admin (bootstraps as admin), then sign in at https://app.example.com.
curl -s -X POST https://app.example.com/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<a-strong-password>"}'
```

If step 2 fails, fix the environment and re-run it — do not proceed to step 3 with a failing
gate. If step 5's public check fails but the localhost check passes, the problem is Caddy or
DNS (§7, §13), not the app.

## 7. Caddy — reverse proxy and automatic HTTPS

`deploy/Caddyfile` is the whole proxy config:

```caddyfile
{$DOMAIN} {
	encode zstd gzip
	reverse_proxy web:80
}
```

- Caddy fetches and renews a Let's Encrypt certificate for `$DOMAIN` automatically on first
  request; there is no manual cert step and no renewal cron. Certificates and the ACME account
  live in the `caddy-data` volume, which is part of the backup (§9) — without it, a restore
  forces fresh issuance against Let's Encrypt's rate limit (50 certificates per domain per week).
- Caddy is the **only** public entrypoint. It forwards to the `web` container (nginx), which
  serves the SPA and proxies `/api/` to the API on the internal Docker network — so cookie auth,
  the SSE feed and downloads all work same-origin, and CORS never applies.
- If the certificate never arrives: DNS is the usual cause (the A record does not point at
  this box, or it was created after Caddy first tried — Caddy retries, but check
  `docker compose logs caddy` for the ACME error). The other usual cause is port 80 blocked
  (ACME HTTP-01 needs it) — check UFW (§2) and the Contabo cloud firewall.

## 8. Firewall and exposure — what is public, what is not

UFW on the host allows **22, 80, 443 only** (§2). The prod overlay adds a second layer: every
internal port is bound to localhost, so even with the host firewall off, nothing but Caddy
listens on the public interface.

| Port | Service | Reachable from |
| --- | --- | --- |
| 80 / 443 | Caddy | Internet (the only public ports) |
| 8000 | API | `127.0.0.1` only — `curl localhost:8000/health` still works |
| 8080 (`WEB_PORT`) | web/nginx | `127.0.0.1` only — Caddy reaches it over the Docker network |
| 5432 | Postgres | `127.0.0.1` only — backups use `docker compose exec`, not this port |
| 9000 / 9001 | MinIO API / console | `127.0.0.1` only — reach the console via `ssh -L 9001:localhost:9001 deploy@<host>` |

Verify after first boot:

```bash
ss -tlnp | grep -E ':80|:443|:8000|:8080|:5432|:9000|:9001'
# 80 and 443 listen on 0.0.0.0; everything else must show 127.0.0.1.
```

Two exposure notes that survive the firewall:

- **`GET /metrics` is unauthenticated** (Prometheus text). It is only reachable on localhost
  by the binding above — do not undo that by re-publishing port 8000 publicly, and point any
  Prometheus scrape at `localhost:8000/metrics` from the box itself.
- **`/metrics` counters are per-process** (4 uvicorn workers), and presence/collaboration state
  is per-worker — cosmetic at this scale, but scrape all workers or run single-worker if exact
  totals ever matter.

## 9. Backups

`scripts/backup.sh` / `scripts/restore.sh` (run from the repo root, with the stack up) are the
backup runbook; the full version is in [deploy.md](../deploy.md#audit--backups) and
[operations.md](../operations.md). What a backup contains:

| Piece | How | Where it lands |
| --- | --- | --- |
| Postgres | `pg_dump --clean` (logical dump, consistent snapshot) | `db.sql.gz` in the tarball |
| MinIO objects | tar of the `minio-data` volume via a throwaway container | `minio-data.tgz` |
| Uploaded source IFCs | tar of the `ifc-data` volume | `ifc-data.tgz` |
| Caddy TLS data | tar of the `caddy-data` volume (certs + ACME account) | `caddy-data.tgz` |
| Redis | **not backed up** — counters are ephemeral by design | — |

```bash
# Back up now (→ ./backups/aec-backup-<timestamp>.tgz)
./scripts/backup.sh

# Nightly at 02:00, prune archives older than 14 days (BACKUP_KEEP, default 14)
0 2 * * *  cd /srv/sadsha-build-pro && ./scripts/backup.sh >> /var/log/aec-backup.log 2>&1
0 3 * * *  find /srv/sadsha-build-pro/backups -name 'aec-backup-*.tgz' -mtime +14 -delete

# Restore — DESTRUCTIVE, overwrites DB + objects + IFCs + TLS data, stops the app first.
# The script stops api, web AND the worker (a restore that leaves the worker running
# lets it write jobs into the database being overwritten) and starts all three again.
./scripts/restore.sh ./backups/aec-backup-<timestamp>.tgz
```

Three rules:

1. **Do one restore drill into a scratch stack before go-live**, and hit `/health` plus a known
   project before trusting any backup. A backup that has never been restored is a hope, not a
   backup. Verify with the same two curls as §6 step 5.
2. **Keep backups off-box** (`aws s3 cp`, `rclone`, or plain `scp` to another machine). A
   backup on the same disk dies with the disk.
3. **Contabo snapshots are the second layer, not the backup.** A snapshot restores the whole
   disk to a point in time; it does not give you a single database or a single project, and it
   lives in the same account as the VM. Daily `backup.sh` + off-box copy is the recovery plan;
   snapshots are for "the whole box is broken" days.

## 10. Updates and restarts — the runbook

```bash
cd /srv/sadsha-build-pro

# --- Routine update (new commit on the deploy branch) ---
git pull --ff-only
DOMAIN=app.example.com docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile full up -d --build
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile full ps
curl -s http://localhost:8000/health

# --- Restart one service (config change, wedged worker) ---
DOMAIN=app.example.com docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile full restart worker
# --- Restart everything (keeps volumes; data is untouched) ---
DOMAIN=app.example.com docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile full restart
# --- Full stop / start ---
DOMAIN=app.example.com docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile full down        # stops + removes containers; volumes survive
DOMAIN=app.example.com docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile full up -d --build
```

Rules and notes:

- **Deploy from a recorded commit.** `git log --oneline -1` before and after; if the new build
  misbehaves, `git checkout <previous-commit>` + `up -d --build` is the rollback (plus
  `restore.sh` if data itself is the problem — never roll back data by hand).
- **Database migrations are not auto-run at startup.** The runtime builds the schema via
  `create_all` plus additive `_ensure_columns()` / `_ensure_indexes()`, so a normal update
  needs no migration step. If a release ever ships an Alembic migration, it is run from the
  running container (`alembic stamp/upgrade head` — the migrations and config are copied
  into the API image for exactly this); the release notes will say so.
- **Dependency updates are owed before any customer touches the stack** (clean report §8):
  the upstream dependency scan was failing on 2026-11-09 (node + python advisories with
  published fixes). Update, rebuild, re-run the preflight (§6) and the smoke test below —
  do not hand a customer a stack with known-fixable advisories in it.
- **Restart policies are `unless-stopped`** on every prod service (set by the overlay), so a
  host reboot brings the stack back by itself. Verify once: `sudo reboot`, then check
  `docker compose ps` after the box comes back.
- **Smoke test after every update:** `API=http://localhost:8000 bash scripts/smoke-stack.sh`
  (health → create project → upload properties index → range request → QTO export). It
  needs a sample IFC path as its argument when the default sample is absent.
- **Logs:** `docker compose logs -f api` (structured JSON access lines on the `aec.access`
  logger), `docker compose logs -f worker`, `docker compose logs -f caddy`. Watch the
  `aec.publish` / `aec.autosync` loggers for conversion/sync failures.
- **Resource limits:** the commented-out `deploy.resources.limits` blocks stay off unless
  you have measured the host under load. If you turn them on, size the **worker** first
  (that is where the large IFC parses live now) and keep the API generous
  (`UVICORN_WORKERS=4` + an in-process reconvert). A cap that is too low OOM-kills a
  legitimate conversion mid-run — that failure looks like a bug, not a limit.

## 11. Sizing and cost — the margin math

| | |
| --- | --- |
| Server (Contabo VPS 8) | ~$16/mo (against the $25/mo budget) |
| Price per customer | $199/mo flat (Pro); trial free for 30 days |
| **Margin per paying customer** | **~$183/mo before support** |
| Customers to cover the server | 1 (covers it ~12× over) |

The box is not the constraint at this price: 8 vCPU / 24 GB runs the whole stack with room for
several concurrent projects and a `--scale worker=3` when conversions back up. The constraint is
support — onboarding a GC, answering RFI/pay-app questions, and the first live project. Budget
for that, not for a bigger VM. Revisit the server size only when one of these is true: sustained
RAM pressure (watch `docker stats`), conversion queue depth that `--scale worker` does not clear,
or Postgres/MinIO disk use approaching the 300 GB disk (IFC models accumulate — check
`docker system df -v` monthly).

## 12. What is NOT verified — the honest list

This guide is validated as far as local checks allow (see "Status" at the top). Everything in
this section needs a box with Docker, and none of it may be assumed from reading the files:

1. **The Docker full stack has never been run.** `docker compose --profile full up --build`
   must be run once on the VPS (or any Docker box) before this guide is trusted — the clean
   report verified the SQLite dev path only, and this guide's author had no Docker at all.
   First-run proof: every service reaches `healthy` in `docker compose ps`, `GET /health`
   answers, and the smoke test in §10 passes.
2. **The MinIO image may not pull** (§6 step 0). This is the single most likely first-boot
   failure. The `MINIO_IMAGE` override and the fallback images are documented, not proven.
3. **The `mc ready local` healthcheck** is the upstream image's bundled client; it is what
   the compose file has always used, but no CI job runs it. If MinIO sits `unhealthy` while
   the console answers, the healthcheck probe — not MinIO — is the suspect
   (`/minio/health/ready` is the alternate probe).
4. **The converter round-trip in containers.** Reconvert worked in dev only after pinning
   `@thatopen/fragments@3.4.7`; the in-container publish → reconvert path is unproven until
   the first live model publish on the VPS.
5. **E-sign.** The module exists; the signing flow itself was never exercised (clean report
   §2). Do not sell it as a feature until it is verified end-to-end on the deployed stack.
6. **Billing, tenancy and BIM gating are not built.** The env vars are plumbed (§4.4), the
   code is not. The store-approval blocker (§0) is still open; the enterprise price is decided
   by Mili on Qasim's "As you wish" delegation ($399/mo, §4.4), open to his override.
7. **The full API suite was never run to completion** (30/715 suites at the clean-report
   cutoff; ~2 min per big test on the authoring VM). CI must run it on a bigger box; this
   guide's YAML/env checks are not a substitute.
8. **Dependency advisories** (§10) are still open upstream.

## 13. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `docker compose up` fails pulling `minio/minio` | Image removed from Docker Hub (§6 step 0) | `docker pull` the fallback, set `MINIO_IMAGE` in `.env`, retry |
| `postgres` never becomes `healthy` | `POSTGRES_USER` renamed in `.env` (fixed in this branch — healthcheck now uses the env vars) or bad password | Check `docker compose logs postgres`; confirm the three `POSTGRES_*` vars match the volume's first-boot values (changing them after first boot does **not** change the existing database — the volume keeps the original credentials) |
| `api` exits on boot, "refuses to start" | `AEC_RBAC` not `1`, or `AEC_AUTH_SECRET` unset/short, on Postgres | Run the preflight (§6 step 2) — it names the failing setting |
| `web` healthy but `/api/*` returns 502 | API not ready yet on first boot | Wait — web now gates on API healthy; if it persists, `docker compose logs api` |
| `caddy` healthy but site shows Caddy's default / cert error | DNS not pointing at the box, or port 80 blocked for ACME | Check the A record (§2) and `docker compose logs caddy`; confirm UFW + cloud firewall allow 80/443 |
| Jobs enqueue but nothing converts | Worker not running (`AEC_JOB_WORKER=off` on the API by design) | `docker compose ps` — worker must be up; check the oldest `queued` job row, not the container |
| `converter` run fails writing `.frag` | `./data` auto-created as root (§5) | `mkdir -p data && sudo chown 10001:10001 data`, retry |
| Disk filling up | IFC models + MinIO objects + old images | `docker system df -v`; `docker image prune`; check volume sizes before adding disk |
| `validate_prod_config.py` "can't open file" | Image built before the Dockerfile fix (§6 step 2 note) | Rebuild the API image (`up -d --build`); the script is copied into the image now |
| Restore leaves jobs stuck | Worker was not restarted after restore | `restore.sh` now stops/starts the worker; if you restored with the old script, `docker compose start worker` |

---

*Companion docs: [deploy.md](../deploy.md) (the full stack, hardening and the cloud-VM
walkthrough this guide specialises for Contabo), [PRODUCTION_CHECKLIST.md](../PRODUCTION_CHECKLIST.md)
(the copy-paste go-live gate), [operations.md](../operations.md) (backups, upgrades,
monitoring). Source of truth for the product decisions: the Remixer design doc and the clean
report in the factory workbench (not committed to this repo).*
