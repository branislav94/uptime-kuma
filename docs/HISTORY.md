# Project history — uptime-kuma + cPanel sync (ONET)

Log of what was built, why, and which problems were hit along the way.
Read this before changing anything; [GUIDE.md](GUIDE.md) is the how-to.

> No secrets in this file. WHM tokens, the Kuma password and the ntfy topic names live
> only in the deployed `.env` (Dokploy → Environment) and in Passbolt.

---

## Current state (2026-11-19)

| | |
|---|---|
| Runs on | **Dokploy** (GitHub provider, branch `main`, Compose Path `./compose.dokploy.yaml`, trigger *On Push*) |
| Kuma | Uptime Kuma 2.5.x, **Embedded MariaDB**, port `3001` published, named volume `kuma-data` |
| Sync | `kuma-sync` container (built from `sync/`), runs every hour, state in volume `kuma-sync-state` |
| Old instance | Dockge stack on TrueNAS (`/mnt/storage/stacks/uptime-kuma`) — to be **stopped** once Dokploy is confirmed working (otherwise every alert arrives twice) |
| Domain | `uptime.onet.rs` added in Dokploy, **Cloudflare A record not created yet**; Kuma is used via `http://<dokploy-host>:3001` |
| Repo | `github.com/branislav94/uptime-kuma` (private) |

### Monitored infrastructure

| What | Value |
|---|---|
| cPanel servers (`WHM_SERVERS`) | `cp1` → `cp1.onet.rs:2087` (VM on Node103), `cp2` → `cp2.onet.rs:2087` (VM on Node102) |
| Extra "our" IP (`SERVER_IPS`) | `23.88.67.187` — old Node102 IP, NAT-ed to cp2 so clients with their own DNS keep working |
| Proxmox (`PROXMOX_NODES`) | `Node102 \| 23.88.67.187` (unconfirmed whether 8006 answers on this IP), `Node103 \| 176.9.32.151` |
| Nameservers | `ns1/ns3 → 176.9.32.151`, `ns2/ns4 → 23.88.67.187` (cPanel DNSOnly VMs dns1/dns2). Monitor `ns1` + `ns2` only; `DNS_TEST_DOMAIN` must be a client zone (e.g. `lartgroupe.com`), **not** `onet.rs` (that zone is on Cloudflare) |
| Planned extras | WHMCS client area `https://panel.onet.rs` (admin is behind NetBird — don't monitor `/admin`), Pterodactyl `https://gp1.onet.rs` |

### Status pages

| Slug | Title | Content |
|---|---|---|
| `onet` | ONET status | public — Web hosting, Email, DNS (+ WHMCS / Game hosting when added). Description: `Hetzner · Falkenstein, Nemačka (EU)`. Domain `status.onet.rs` (planned via cloudflared) |
| `interno` | ONET interno | internal — all infrastructure groups |
| `klijenti` | Clients Websites | internal — all sites, one section per cPanel account |

Per-client pages were explicitly **not** wanted.

### Notifications (ntfy.sh, phone app)

Three topics (names are secret, in `.env`): **sites**, **infra** (priority 5, loud),
**domains** (domain + SSL expiry). Telegram, Discord and e-mail were rejected
(Telegram annoying, ONET mail not working at the time).

---

## Timeline

### 2026-10-24 — first version (Dockge on TrueNAS)

- Goal: Uptime Kuma + a script that automatically monitors **all cPanel domains**.
- First attempt targeted Dokploy by mistake ("dockge" ≠ Dokploy) → rebuilt as a **Dockge** stack.
  Dockge runs on the TrueNAS VM (on home Proxmox `pve1`), stacks dir `/mnt/storage/stacks`.
- Database: **Embedded MariaDB** chosen over SQLite (hundreds of monitors; no migration
  path later).
- **WHM token 403**: the curl test used the placeholder `TVOJ_TOKEN`; the real token worked.
  Lesson: tokens are per server, created as `root`, IP-restricted.
- **Kuma login hung until timeout** (Kuma 2.5.7 + embedded MariaDB). Cause: Kuma 2.x
  registers its `login` socket handler only at the end of connection setup and then emits
  `loginRequired`; a login sent earlier is silently dropped. Fix: `ReadyKumaApi` waits for
  `loginRequired` / `autoLogin` before logging in. Reproduced by artificially delaying
  Kuma's `sendInfo` by 5 s.
- **DNS check** added: only monitor domains whose A record points to our server (or
  Cloudflare). Triggered by `sk-invictus.com` — cPanel main domain that no longer exists.
- **Cloudflare tag** for domains behind the CF proxy.
- **Addon/sub/parked**: WHM returned only 3 addon domains; the rest were sub/parked.
  All four types are monitored and tagged; cPanel's helper subdomains for addon domains
  (`triaris.rs.lartgroupe.com`) are skipped (same docroot as the addon).
- `unitedtech.group` showed DOWN while the page said "coming soon": the plugin returns
  **HTTP 503**. Accepted 503 manually on that monitor; the sync never touches status codes.
- Improvements round (user picked): ntfy notifications, infrastructure monitors
  (WHM/cPanel/Webmail/mail/FTP + ping), keyword check (`Fatal error`), domain expiry,
  hourly sync, uplink check (1.1.1.1/8.8.8.8). Skipped for now: WHMCS mapping,
  maintenance windows, Grafana/Prometheus.
- **Domain expiry**: `.rs` and `.io` have no RDAP in the IANA bootstrap → sync uses WHOIS
  (`whois.rnids.rs` etc.). Alerts at 30/14/7/3/1 days, state in `sync-state/`.
- Separate ntfy topic for **domain + SSL expiry**; SSL is checked by the sync (Kuma's own
  SSL alert can't be routed to a different notification, so it is turned off).
- **Manually paused monitors stay paused**: the sync tags what it pauses with
  `auto-paused` (originally `auto-pauza`) and only resumes those.
- Repo created and pushed to GitHub; `.env` is git-ignored.
- Everything translated to **English** (code, README, Kuma-visible names). Old Serbian
  names are renamed in place on first run.
- Alternatives discussed: Beszel/Netdata (not needed — Grafana already exists),
  Prometheus + Alertmanager (later), HetrixTools (hosted, not self-hosted), own DNSBL
  check (offered, not built; Spamhaus needs a DQS key or own resolver).

### 2026-10-26 — Node102 became Proxmox, cp2 is a VM

- `WHM_SERVERS`: `node102` replaced with `cp2` (new token on cp2).
- `SERVER_IPS=23.88.67.187` because that IP is NAT-ed to cp2 and WHM doesn't see it as its own.
- Infrastructure split into nested groups:
  `Infrastructure / cPanel / <server>`, `/ Proxmox / <node>`, `/ DNS`, `/ Other|<group>`.
  `PROXMOX_NODES`, per-host port overrides, `INFRA_EXTRA` 4th field = group,
  `INFRA_ON_REMOVED=delete`.
- Sites regrouped: `Websites / <server> / <cPanel account>`; old flat `cPanel · cp1`
  groups are emptied and deleted automatically. User kept the name **Infrastructure**
  (briefly renamed to "Servers", reverted).
- **Status pages**: public (services only, coarse location, no client sites/nodes/IPs),
  internal infrastructure, internal Clients Websites.

### 2026-10-27 — new DNSOnly nameservers, NXDOMAIN handling

- ns1–ns4 moved to the DNSOnly VMs (see table above). Domains whose zone was missing on
  the new nameservers returned NXDOMAIN and were auto-paused — user pointed out that
  this hides a real outage.
- Change: a domain that is on the server but **doesn't resolve** stays monitored, Kuma
  alerts, and the description says `DNS: domain does not resolve (NXDOMAIN)`.
  `DNS_NXDOMAIN=pause` restores the old behaviour. Previously auto-paused ones resume.
- Fix for missing zones: `/scripts/dnscluster syncall` on cp1 and cp2.

### 2026-11-13 — moved to Dokploy

- Deployed the GitHub repo on Dokploy. Added `compose.dokploy.yaml` with **named volumes**
  (Dokploy re-clones the repo on each deploy, so `./data` is not safe).
- First version also removed the host port (domain-only access) — that broke access,
  **port 3001 restored** the same day.
- New Kuma instance (2.5.11) → setup again (Embedded MariaDB + admin, no 2FA),
  `KUMA_USER`/`KUMA_PASS` in Dokploy Environment.
- Sync reached WHM fine (cp2: 41 domains) but failed with `unable to connect` because
  Kuma was still on the database setup screen.
- WHM tokens must allow the **public IP of the Dokploy host** in addition to TrueNAS.

---

## Open items

- [ ] Finish Kuma setup on Dokploy and confirm `done: added=...` in the `kuma-sync` log
- [ ] Stop the Dockge stack on TrueNAS (duplicate alerts)
- [ ] `NAMESERVERS=ns1.onet.rs,ns2.onet.rs`, `DNS_TEST_DOMAIN=<client zone>` in Dokploy env
- [ ] Confirm Node102 Proxmox IP (8006 on `23.88.67.187` or not)
- [ ] Add WHMCS (`panel.onet.rs`) and Pterodactyl (`gp1.onet.rs`) to `INFRA_EXTRA` +
      `STATUS_PUBLIC_EXTRA` once they are live
- [ ] `uptime.onet.rs` A record in Cloudflare (optional) and `status.onet.rs` via cloudflared
      — only public status paths, everything else behind Cloudflare Access
- [ ] Domains reported as DNS problem: `sk-invictus.com`, `old.markoristic.digital`,
      `borisrifelj.com` — fix DNS or remove from cPanel
- [ ] Store WHM tokens / Kuma password / ntfy topics in Passbolt
- [ ] Rotate the cp1 token that was partly visible on a screenshot (24 Oct)
