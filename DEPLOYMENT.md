# HearHelper Deployment Guide

Step-by-step instructions for packaging the Django/Channels backend into a container and serving it through Cloudflare’s edge.

## 1. Build the production image

1. Ensure the main virtualenv mirrors production requirements (`requirements.prod.txt` was generated from `venv`).  
2. Build the image locally:
   ```bash
   docker build -t hearhelper-api:latest .
   ```
   The Dockerfile installs system packages (`ffmpeg`, `libsndfile1`), Python deps, and runs `collectstatic`.

## 2. Provide runtime configuration

The app expects environment variables instead of the repo’s `.env` (which is ignored in the image). At minimum set:

| Variable | Purpose |
| --- | --- |
| `DJANGO_SECRET_KEY` | Production secret key |
| `DJANGO_DEBUG` | `false` in production |
| `ALLOWED_HOSTS` | Comma-separated hostnames (e.g. `api.example.com`) |
| `DB_URL` / `DB_NAME` | MongoDB connection info |
| `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_REGION` | Cloudflare R2 creds |
| `PIPER_MODELS_DIR`, `PIPER_DEFAULT_VOICE`, `GTTS_LANG` | Optional overrides for TTS |

Example run command (replace the placeholder secrets):
```bash
docker run -d --name hearhelper \
  -e DJANGO_SECRET_KEY='prod-secret' \
  -e DJANGO_DEBUG=false \
  -e ALLOWED_HOSTS='api.example.com' \
  -e DB_URL='mongodb+srv://...' \
  -e DB_NAME='hearHelper' \
  -e R2_ENDPOINT_URL='https://...' \
  -e R2_ACCESS_KEY_ID='...' \
  -e R2_SECRET_ACCESS_KEY='...' \
  -e R2_BUCKET='hear-helper' \
  -p 8000:8000 \
  hearhelper-api:latest
```

Run one-off management commands (e.g., `python manage.py migrate`) inside the container as needed.

## 3. Host the container

You can run the container anywhere that exposes `0.0.0.0:8000`: a VM, Fly.io, Render, ECS/Fargate, etc. Ensure outbound internet is available for MongoDB, R2, and gTTS. Monitor memory/CPU if Piper voices are loaded.

## 4. Wire Cloudflare

1. **DNS + SSL**: Add your domain to Cloudflare. Create an `A` or `CNAME` placeholder; the tunnel (next step) will take over.
2. **Cloudflare Tunnel**:  
   ```bash
   cloudflared tunnel login
   cloudflared tunnel create hearhelper-api
   cloudflared tunnel route dns hearhelper-api api.example.com
   ```
   Configure `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: hearhelper-api
   credentials-file: /root/.cloudflared/<tunnel-id>.json
   ingress:
     - hostname: api.example.com
       service: http://127.0.0.1:8000
     - service: http_status:404
   ```
   Run `cloudflared tunnel run hearhelper-api` as a service on the host.
3. **Access/WAF**: Use Cloudflare Zero Trust policies or rulesets to lock down `/ws/hat` and `/api/*`, enable “Always use HTTPS,” and set rate limiting thresholds appropriate for WebSocket traffic.
4. **Workers (optional)**: Create a Worker with `wrangler` if you want custom auth, caching, or request rewriting before requests hit the tunnel. The Worker can proxy to the tunnel hostname.

## 5. Storage (R2)

In Cloudflare’s dashboard create an API token with object read/write permissions for the `hear-helper` bucket. Store the resulting credentials as secrets/environment variables when running the container. `storage/r2_client.py` auto-detects the configuration and can run anywhere the variables are injected.

## 6. Observability & runbook

- Health checks: add an `api/healthz` endpoint (or reuse an existing view) and monitor it via Cloudflare’s health checks pointed at `https://api.example.com/healthz`.  
- Logs: tail `docker logs hearhelper -f` locally or ship to your logging stack.  
- Scaling: to scale horizontally, run multiple containers and have Cloudflare Tunnel load-balance them, or place them behind a private load balancer and point the tunnel there.
- Updates: rebuild the image (`docker build ...`), push to your registry, redeploy containers, and Cloudflare continues routing traffic with no DNS changes required.

Following these steps gets the application containerized, secrets-injected at runtime, and exposed through Cloudflare’s edge (DNS, WAF, Zero Trust, and R2). Adapt the host-specific pieces (systemd service, CI/CD, registry) to your infrastructure of choice.
