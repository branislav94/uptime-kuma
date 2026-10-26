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
- groups: `Websites / <server> / <cPanel account>` (e.g. `Websites / cp1 / lartgroupe`);
  tags: `cpanel-sync` (server/user), `addon` / `sub` / `parked`, `Cloudflare`
- empty client groups (account removed or moved) are deleted automatically
- keyword check: a site is down even with HTTP 200 if the page contains `Fatal error`
- domain moved to another server → the monitor follows; domain gone → paused with the
  `auto-paused` tag, and resumed automatically when the domain comes back
- **monitors you pause by hand are never resumed** (only those tagged `auto-paused`)

**Infrastructure** — nested groups
```
Infrastructure
├── cPanel / cp1, cp2 …           ping, WHM, cPanel, Webmail, mail/FTP ports (from WHM_SERVERS)
├── Proxmox / Node102, Node103 …  ping, Proxmox UI (8006) + optional ports (PROXMOX_NODES)
├── DNS                           nameservers (NAMESERVERS)
└── Other / your groups           INFRA_EXTRA, e.g. Game / Wings
Local uplink (Kuma)               ping 1.1.1.1 / 8.8.8.8 — if these are down, outages are false alarms
```
- each category gets its own checks; per-host overrides: `INFRA_PORTS_<NAME>`, `PROXMOX_PORTS_<NAME>`
- infra monitors removed from the config are paused (or deleted with `INFRA_ON_REMOVED=delete`)

**Status pages** — maintained by the sync, updated when monitors change
- **public** (`STATUS_PUBLIC_SLUG`): services only — Web hosting (cPanel, Webmail, FTP),
  Email (SMTP/IMAP), DNS + chosen extras (`STATUS_PUBLIC_EXTRA`); a coarse location in the
  description. No client sites, no node names, no IPs, tags are never shown.
- **internal** (`STATUS_INTERNAL_SLUG`): every infrastructure group + all sites per server.
  Kuma 2 status pages have no password, so keep it behind NetBird / Cloudflare Access.
- **per client** (`STATUS_CLIENT_PAGES=user1,user2` or `all`): that account's sites only.

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

## Exposing the public status page (cloudflared)

Publishing Kuma through a tunnel also publishes its login page and API. Make only the
status page paths public and put everything else behind Cloudflare Access:

- public: `/status/*`, `/api/status-page/*`, `/api/badge/*`, `/assets/*`, `/upload/*`,
  `/icon.svg`, `/favicon.ico`, `/manifest.json`
- everything else (the dashboard, `/socket.io/*`, `/metrics`): Cloudflare Access

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
- Upgrading from older versions: groups, ntfy notifications and the `auto-pauza` tag
  are renamed in place, monitors are moved into the new groups and the old empty groups
  (`cPanel · cp1`, …) are deleted on the first run — history is kept, nothing is duplicated.
- Empty groups below `Websites/` and `Infrastructure/` are treated as owned by the sync
  and deleted — don't create your own empty groups there.
