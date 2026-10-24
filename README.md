# uptime-kuma + cPanel sync

Uptime Kuma 2 (embedded MariaDB) plus a `kuma-sync` container that automatically
monitors every site and the infrastructure of one or more WHM/cPanel servers.
Runs as a Dockge stack.

## What `kuma-sync` does

**Sites**
- pulls domains from all WHM servers (`get_domain_info` + `listaccts`)
- monitors `main`, `addon`, `parked` and `sub` domains; skips suspended accounts and the
  helper subdomains cPanel creates for addon domains
- only monitors domains whose DNS actually points to our server (or Cloudflare)
- one group per server (`cPanel · cp1`); tags: `cpanel-sync` (server/user),
  `addon` / `sub` / `parked`, `Cloudflare`
- keyword check: a site is down even with HTTP 200 if the page contains `Fatal error`
- domain moved to another server → the monitor follows; domain gone → paused with the
  `auto-paused` tag, and resumed automatically when the domain comes back
- **monitors you pause by hand are never resumed** (only those tagged `auto-paused`)

**Infrastructure** (group `Infrastructure`)
- for every WHM server: ping, WHM, cPanel, Webmail, SMTP 465/587, IMAPS, FTP
- nameservers (`NAMESERVERS`), extra monitors (`INFRA_EXTRA`, e.g. Proxmox)
- `Local uplink (Kuma)`: ping 1.1.1.1 / 8.8.8.8 — if these are down, outages are false alarms

**Notifications** — ntfy (phone app)
- `NTFY_TOPIC_SITES` (priority 3/4), `NTFY_TOPIC_INFRA` (priority 5), `NTFY_TOPIC_EXPIRY`
- a "kuma-sync is failing" message after 3 failed sync runs

**Domain and SSL expiry** — own ntfy topic `NTFY_TOPIC_EXPIRY`
- domains: RDAP where available, WHOIS for `.rs` / `.io` and other TLDs without RDAP;
  alerts at 30/14/7/3/1 days
- SSL: the certificate the site actually serves; alerts at 14/7/3/1 days
  (= AutoSSL is failing to renew)
- both dates are shown in the monitor description; alert state lives in `sync-state/`
  (no repeated alerts after a restart)

## Setup

```bash
cd /path/to/stacks
git clone <repo> uptime-kuma && cd uptime-kuma
cp .env.example .env && nano .env      # KUMA_*, WHM_SERVERS, NTFY_TOPIC_*
docker compose up -d --build
```

1. Open `http://<host>:3001`, choose **Embedded MariaDB**, create the admin account (no 2FA)
2. Put `KUMA_USER` / `KUMA_PASS` into `.env`
3. `docker compose up -d --force-recreate kuma-sync`

WHM token: *WHM → Development → Manage API Tokens*. Create it **on each server
separately**, as `root`, restricted to the public IP of the Kuma host.

## Useful commands

```bash
docker compose logs -f kuma-sync                                  # log
docker compose exec kuma-sync python kuma_sync.py --dry-run       # what would change
docker compose exec kuma-sync python kuma_sync.py --test-ntfy     # test message
docker compose exec kuma-sync python kuma_sync.py --expiry x.rs   # domain expiry
docker compose exec kuma-sync python kuma_sync.py --ssl x.rs      # SSL expiry
docker compose up -d --build --force-recreate kuma-sync           # after changing the script
docker compose up -d --force-recreate kuma-sync                   # after changing .env
```

## Notes

- Kuma 2.x registers its `login` handler only after it emits `loginRequired`; the sync
  waits for that event before logging in (otherwise the login is silently dropped and
  the client waits for the timeout).
- The sync only changes monitors tagged `cpanel-sync`. Hand-made monitors are never
  touched or duplicated. On its own monitors it doesn't touch manually added status
  codes (e.g. 503 for a "coming soon" page).
- `.rs` and `.io` have no RDAP entry in the IANA bootstrap → Kuma's built-in domain
  expiry check doesn't work for them, so the sync uses WHOIS.
- Upgrading from the earlier Serbian version: groups, ntfy notifications and the
  `auto-pauza` tag are renamed in place on the first run (nothing is duplicated).
