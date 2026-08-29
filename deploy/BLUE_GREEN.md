# Blue-Green Docker Deployment

`blue-green-deploy.sh` deploys an already-built `linux/amd64` image beside the
running application, waits for the candidate to pass its Docker health check
and `/health` probe, then switches Caddy without restarting it.

The script intentionally does not run `docker build`, `docker compose down`, or
`docker pull`. Build the image in CI, verify it, and load it on the server
before invoking the script. PostgreSQL and Redis remain the existing shared
services; only the application container is replaced.

## One-time Caddy preparation

The production Caddy site must have two application upstreams configured with
health-aware primary/secondary routing. The current production Caddyfile is a
custom file outside this repository, so prepare that file once before the
first rollout. Keep the existing host block and replace its Sub2API proxy
block with the following shape:

```caddyfile
reverse_proxy 127.0.0.1:8080 {
    lb_policy first
    health_uri /health
    health_interval 2s
    health_fails 2
    health_passes 2
    health_timeout 5s
    lb_try_duration 5s
    stream_close_delay 5m
}
```

Validate and reload Caddy; do not restart the Caddy container. The deployment
script replaces only the direct `reverse_proxy 127.0.0.1:<port>` line inside
the site identified by `SUB2API_CADDY_SITE_HOST`.

## First rollout from the current single container

The first run treats the existing `sub2api` container on port `8080` as blue
and starts green on `18081`:

```bash
export SUB2API_CADDY_SITE_HOST='sub2api.example.com:8443'
export SUB2API_CADDYFILE=/opt/sub2api-https/Caddyfile
export SUB2API_CADDY_CONTAINER=sub2api-https

./blue-green-deploy.sh sub2api:dev-<commit>
```

The target image must already be loaded locally and must report
`linux/amd64`. The script records the active slot in
`/opt/sub2api/deploy/.blue-green-active`, keeps the Caddy backup next to the
Caddyfile, and leaves the old container stopped rather than deleting it.

The next rollout alternates ports: blue uses `8080`, green uses `18081`.
Only one deployment can run at a time because the script takes a host lock.
After the first rollout, do not run `docker compose up -d sub2api` for the
application service: Compose may resurrect the stopped legacy container on
port `8080`. Use this script for application image updates; Compose can still
manage PostgreSQL and Redis.

## Safety boundaries

- The candidate gets a copied `/app/data` directory so two application
  versions do not concurrently write the same files. Freeze file-backed config
  edits during the short copy and deployment window.
- Both versions use the same PostgreSQL and Redis services. Database migrations
  must be backward-compatible while both versions overlap; use expand/contract
  migrations and keep a database backup before a schema-changing release.
- Caddy is switched only after candidate health and a monitor window pass. If
  either step fails, Caddy is restored and the old container remains available.
- Existing long-lived requests are given a drain window. The application’s
  shutdown timeout should be at least as long as the container stop grace
  period when streaming requests must survive a release.
