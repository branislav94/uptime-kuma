# Operations guide — uptime-kuma + cPanel sync

Everything needed to run, change and troubleshoot the monitoring stack.
For what was decided and why, see [HISTORY.md](HISTORY.md).

## 1. How it works

```
 WHM API (cp1, cp2, …) ──┐
 DNS / WHOIS / RDAP ─────┤
 TLS (port 443) ─────────┼──►  kuma-sync (hourly)  ──socket.io──►  Uptime Kuma 2  ──►  ntfy ──► phone
 .env (Dokploy env) ─────┘          │                                  │
                                    └─ ntfy (expiry alerts directly)   └─ status pages
```

- **Uptime Kuma** does the actual checks (HTTP/keyword, ping, port, DNS) and sends
  outage alerts through the ntfy notifications the sync creates.
- **kuma-sync** (`sync/kuma_sync.py`) never checks uptime itself. Every run it:
  1. reads accounts + domains from every WHM server,
  2. filters them (type, suspended, helper subdomains, `EXCLUDE`, DNS check),
  3. checks domain expiry (RDAP/WHOIS) and SSL expiry, sends expiry alerts to ntfy,
  4. creates / updates / moves / pauses Kuma monitors, groups and tags,
  5. builds infrastructure monitors from `.env`,
  6. deletes empty sync-owned groups, updates status pages.
- It only touches monitors tagged **`cpanel-sync`**. Anything you create by hand is safe.

### What lives where in Kuma

```
Websites
└── cp1 / cp2
    └── <cPanel account>        sites, tags: cpanel-sync=<server>/<user>, addon|sub|parked, Cloudflare
Infrastructure
├── cPanel / cp1, cp2           ping, WHM, cPanel, Webmail, SMTP 465/587, IMAPS, FTP
├── Proxmox / Node102, Node103  ping, Proxmox UI (8006) [+ PROXMOX_PORTS]
├── DNS                         nameservers
└── Client area, Game, Other    INFRA_EXTRA
Local uplink (Kuma)             ping 1.1.1.1, 8.8.8.8
```

## 2. Deployment

### Dokploy (current)

1. **Create → Compose**, provider **GitHub**, repo `uptime-kuma`, branch `main`,
   **Compose Path `./compose.dokploy.yaml`**, trigger *On Push* (a `git push` redeploys).
2. **Environment**: full `.env` (see section 4). Do **not** set `KUMA_URL`.
3. Deploy → open `http://<dokploy-host>:3001` → **Embedded MariaDB** → admin account
   **without 2FA** → put it into `KUMA_USER` / `KUMA_PASS` → **Redeploy**.
4. On **every** WHM server allow the Dokploy host's public IP on the API token
   (`curl -s ifconfig.me` on that host).
5. Optional domain: Dokploy *Domains* → service `uptime-kuma`, port `3001`, HTTPS;
   needs an A record in Cloudflare (grey cloud until the certificate is issued).

Data: named volumes `kuma-data` (Kuma + MariaDB) and `kuma-sync-state` (expiry/SSL alert
state). They survive redeploys; they are deleted only if you remove them explicitly.

### Dockge (old, TrueNAS)

Stack folder `/mnt/storage/stacks/uptime-kuma`: `compose.yaml`, `.env`, `sync/`, `data/`,
`sync-state/`. TrueNAS shell is **zsh** — run multi-line snippets inside `bash`.

## 3. Everyday tasks

| Task | How |
|---|---|
| See what the sync does | Dokploy → Logs → `kuma-sync` (Dockge: `docker compose logs -f kuma-sync`, quit with **Ctrl+C**, not Ctrl+Z) |
| Dry run | `docker compose exec kuma-sync python kuma_sync.py --dry-run` |
| Test phone alerts | `… python kuma_sync.py --test-ntfy` (one message per topic) |
| Domain / SSL expiry of one domain | `… --expiry example.rs`, `… --ssl example.rs` |
| Change `.env` | Dokploy: edit Environment → **Redeploy**. Dockge: edit → `docker compose up -d --force-recreate kuma-sync` (*Restart* does **not** reload `.env`) |
| Change the script | push to GitHub (Dokploy rebuilds). Dockge: copy `sync/kuma_sync.py` → `up -d --build --force-recreate kuma-sync` |
| Add a cPanel server | create API token on it (root, IP-restricted) → append `,cpN\|https://cpN.onet.rs:2087\|TOKEN` to `WHM_SERVERS` |
| Remove a cPanel server | remove it from `WHM_SERVERS`; its sites get `auto-paused` unless they moved to another listed server |
| Migrate an account cp1 → cp2 | nothing — after DNS points to cp2 the monitor moves to `Websites / cp2 / <user>` with history |
| Stop monitoring one site | pause it in Kuma by hand — the sync never resumes manually paused monitors |
| Skip domains permanently | add a pattern to `EXCLUDE` (fnmatch, e.g. `test.*,*.cpanel.site`) |
| Accept a special status code (e.g. 503 "coming soon") | edit that monitor in Kuma → Accepted Status Codes; the sync doesn't overwrite it |
| Add a Proxmox node | `PROXMOX_NODES=Node102\|IP,Node103\|IP,Node104\|IP` |
| Add any other monitor | `INFRA_EXTRA=Name\|http\|https://url\|Group;Name2\|port\|host:port\|Group/Sub` |
| Show it on the public page | `STATUS_PUBLIC_EXTRA=Exact monitor name\|Section;…` |
| New ntfy topic name | `echo "onet-x-$(head -c6 /dev/urandom \| od -An -tx1 \| tr -d ' \n')"` → `.env` → subscribe in the app |
| Rotate a WHM token | create new token on that server → replace in `WHM_SERVERS` → redeploy → delete old token |

## 4. Configuration reference (`.env`)

Full list with comments: [`.env.example`](../.env.example). The ones that matter most:

| Variable | Meaning |
|---|---|
| `KUMA_USER`, `KUMA_PASS` | Kuma admin login (no 2FA; the Kuma API key does not work) |
| `WHM_SERVERS` | `name\|https://host:2087\|token`, comma separated |
| `SERVER_IPS` | extra IPs counted as ours (NAT-ed IPs, e.g. `23.88.67.187`) |
| `DNS_CHECK` | `ours` (default) / `resolves` / `off` |
| `DNS_NXDOMAIN` | `monitor` (default: keep monitoring, alert) / `pause` |
| `DOMAIN_TYPES` | `main,addon,parked,sub` |
| `SKIP_SUSPENDED`, `EXCLUDE`, `ON_REMOVED` | filters; `ON_REMOVED=pause\|delete\|keep` |
| `MONITOR_INTERVAL`, `MONITOR_RETRIES`, `RETRY_INTERVAL` | site checks (300 s, 3 retries × 60 s) |
| `CONTENT_ERROR_KEYWORD` | page containing it = DOWN (`Fatal error`; empty disables) |
| `NTFY_TOPIC_SITES`, `NTFY_TOPIC_INFRA`, `NTFY_TOPIC_EXPIRY` | phone topics (secret names) |
| `DOMAIN_EXPIRY_DAYS`, `SSL_EXPIRY_DAYS` | alert thresholds |
| `INFRA_AUTO_PORTS`, `INFRA_PORTS_<NAME>` | ports per cPanel server |
| `PROXMOX_NODES`, `PROXMOX_PORTS[_<NAME>]` | Proxmox nodes |
| `NAMESERVERS`, `DNS_TEST_DOMAIN` | DNS monitors; test domain must be a zone served by those NS |
| `INFRA_EXTRA`, `INFRA_ON_REMOVED` | other monitors; delete or pause when removed |
| `STATUS_PUBLIC_*`, `STATUS_INTERNAL_*`, `STATUS_SITES_*` | status pages (empty slug = no page) |
| `SYNC_INTERVAL` | seconds between runs (3600) |

## 5. Troubleshooting

| Log / symptom | Cause | Fix |
|---|---|---|
| `403 Client Error: Forbidden Access denied` (WHM) | wrong token, token from another server, not root, or IP not allowed | test: `curl -s -H "Authorization: whm root:$T" https://cpN.onet.rs:2087/json-api/version?api.version=1` from the Kuma host |
| `Failed to resolve 'cpN.onet.rs'` | server doesn't exist / no DNS | remove from `WHM_SERVERS` |
| `CERTIFICATE_VERIFY_FAILED` | no valid cert on 2087 | AutoSSL for hostname, or `WHM_VERIFY_SSL=false` temporarily |
| `authIncorrectCreds` | `KUMA_USER/PASS` wrong | fix env, redeploy |
| `unable to connect` / `Unexpected response from server` | Kuma still on the DB setup screen, or not running | finish setup at `:3001` |
| hangs at `Kuma: logging in...` | (old versions) login race | fixed by `ReadyKumaApi`; update the script |
| `skipping x: DNS elsewhere:<ip>` | domain points elsewhere | if the IP is ours → add to `SERVER_IPS` |
| `DNS problem x: does not resolve` | expired domain or zone missing on our NS | `whois x`; `/scripts/dnscluster syncall` on cp1/cp2 |
| `skipped addon-helper-sub=N` | cPanel helper subdomains | expected |
| site DOWN but page looks fine | non-2xx/3xx status (503 maintenance plugin) or page contains `Fatal error` | `curl -sI https://site`; fix plugin mode or accept code on that monitor |
| every alert arrives twice | two Kuma instances with the same ntfy topics | stop the old stack |
| `docker compose …: no configuration file provided` | not in the stack folder | `cd` into it first |
| `zsh: parse error near ')'` | pasted `#` comments in zsh | run `bash` first |

## 6. Security notes

- `.env` holds root-level WHM tokens — never commit it; keep copies in Passbolt.
- Restrict every WHM token to the Kuma host's public IP.
- Kuma 2 status pages have no password: only the public page may be exposed. Via
  cloudflared publish only `/status/*`, `/api/status-page/*`, `/api/badge/*`, `/assets/*`,
  `/upload/*`, `/icon.svg`, `/favicon.ico`, `/manifest.json`; everything else behind
  Cloudflare Access.
- The public page never lists client sites, node names or IPs.
- ntfy.sh topic names are the only protection for alerts — long and random, regenerate if leaked.
