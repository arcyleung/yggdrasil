# Tailscale Funnel — Yggdrasil Control Plane

Expose the FastAPI control plane (UI + token API + future MCP HTTP) on HTTPS via Tailscale Funnel.
**Do not** put Qdrant or embed workers on Funnel — only the app on port **8080**.

## Prerequisites

- Tailscale installed and logged in on the host
- Funnel enabled for your tailnet (admin console → DNS → HTTPS / Funnel)
- App dependencies: `pip install -e ".[web]"`

## Run the control plane locally

```bash
# From repo / worktree root
export YGG_UI_SECRET="$(openssl rand -hex 32)"   # required in prod; default is dev-secret
export YGG_USER_MAPPING_PATH=user_mapping.yaml   # or KEY_NAME_MAP
export YGG_UI_BIND=127.0.0.1:8080

# After Funnel is up, set the public HTTPS hostname (see below)
# export YGG_PUBLIC_BASE_URL=https://<machine>.<tailnet>.ts.net

uvicorn yggdrasil.web.app:app --host 127.0.0.1 --port 8080
# or: python -m yggdrasil.web
# or: yggdrasil-web
```

Verify liveness:

```bash
curl -s http://127.0.0.1:8080/healthz
# {"status":"ok"}
```

## Enable Funnel on 8080

```bash
# Serve local port 8080 over HTTPS on your MagicDNS name
tailscale funnel 8080
```

Tailscale prints the public URL, e.g. `https://myhost.tail-xxxxx.ts.net`.

Set that as the base URL used inside downloaded skill / mcp.json templates:

```bash
export YGG_PUBLIC_BASE_URL=https://myhost.tail-xxxxx.ts.net
# restart uvicorn so templates embed the correct host
```

Optional: persist Funnel with `tailscale serve` / funnel config (see `tailscale funnel --help` for your client version).

## What is exposed

| Path | Purpose |
|------|---------|
| `/` | Landing (Lab vs Demo) |
| `/lab/login`, `/lab/home` | Lab key exchange + downloads |
| `/lab/skill.md`, `/lab/mcp.json` | Personalized skill + MCP client snippet |
| `/demo`, `/demo/skill.md` | Demo tenant entry |
| `/api/v1/tokens/exchange` | JSON key → bearer token |
| `/healthz` | Liveness |
| `/mcp` | Reserved for Streamable HTTP MCP (may 501 until multi-tenant gateway ships) |

Qdrant (`6333`), Mongo, and embed servers stay on **localhost / private network only**.

## DDNS + reverse proxy (alternative)

If not using Funnel:

1. Point A/AAAA DNS at the host (or a VPS proxy).
2. Terminate TLS with Caddy or nginx and reverse-proxy to `127.0.0.1:8080`.
3. Set `YGG_PUBLIC_BASE_URL=https://ygg.example.com`.

Example Caddy snippet:

```caddy
ygg.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

Firewall Qdrant and other data-plane ports from the public internet.

## Security notes

- Always use HTTPS (Funnel or Caddy) before users paste lab API keys (prove-once exchange).
- Set a strong `YGG_UI_SECRET` in production (session cookies + signed tokens).
- Never commit `user_mapping.yaml` or full API keys.
- Prefer Funnel/DDNS **only** for the control plane process, not Qdrant.
- Rotate demo tokens if the well-known demo path is abused.

## Smoke checklist

1. `curl https://$HOST/healthz` → 200
2. Open `/` in a browser → Lab / Demo cards
3. Lab login with a mapped key → home shows owner, skill.md contains `YGG_PUBLIC_BASE_URL` and bearer token (not `sk-`)
4. Demo → isolated skill with `tenant_id=demo` copy
