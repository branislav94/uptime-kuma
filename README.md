# uptime-kuma + cPanel sync

Uptime Kuma 2 (embedded MariaDB) + `kuma-sync` kontejner koji automatski prati sve
sajtove i infrastrukturu sa WHM/cPanel servera. Pokreće se kao Dockge stack
(trenutno na TrueNAS-u: `/mnt/storage/stacks/uptime-kuma`).

## Šta radi `kuma-sync`

**Sajtovi**
- povlači domene sa svih WHM servera (`get_domain_info` + `listaccts`)
- prati `main`, `addon`, `parked`, `sub`; preskače suspendovane naloge i pomoćne
  subdomene koje cPanel pravi uz addon domene
- prati samo domene čiji DNS stvarno pokazuje na naš server (ili Cloudflare)
- grupa po serveru (`cPanel · cp1`), tagovi: `cpanel-sync` (server/korisnik),
  `addon` / `sub` / `parked`, `Cloudflare`
- keyword provera: sajt je dole i kad vrati 200, ali stranica sadrži `Fatal error`
- domen preseljen na drugi server → monitor se sam premesti; domen nestao → pauza
  sa tagom `auto-pauza`; kad se domen vrati, sync ga ponovo pokreće
- **ručno pauzirane monitore sync nikad ne pokreće** (samo one sa tagom `auto-pauza`)

**Infrastruktura** (grupa `Infrastruktura`)
- za svaki WHM server: ping, WHM, cPanel, Webmail, SMTP 465/587, IMAPS, FTP
- nameserveri (`NAMESERVERS`), dodatni monitori (`INFRA_EXTRA`, npr. Proxmox)
- `Lokalna veza (Kuma)`: ping 1.1.1.1 / 8.8.8.8 — ako su dole, padovi su lažni

**Notifikacije** — ntfy (aplikacija na telefonu)
- `NTFY_TOPIC_SITES` (prioritet 3/4), `NTFY_TOPIC_INFRA` (prioritet 5), `NTFY_TOPIC_EXPIRY`
- poruka „kuma-sync ne radi" posle 3 neuspela sync-a

**Istek domena i SSL-a** — poseban ntfy kanal `NTFY_TOPIC_EXPIRY`
- domeni: RDAP gde postoji, WHOIS za `.rs` / `.io` i ostale bez RDAP-a; alarm na 30/14/7/3/1 dan
- SSL: sertifikat koji sajt stvarno servira; alarm na 14/7/3/1 dan (= AutoSSL ne uspeva da obnovi)
- oba datuma u opisu monitora; stanje alarma u `sync-state/` (ne ponavljaju se posle restarta)

## Postavljanje

```bash
cd /mnt/storage/stacks
git clone <repo> uptime-kuma && cd uptime-kuma
cp .env.example .env && nano .env      # KUMA_*, WHM_SERVERS, NTFY_TOPIC_*
docker compose up -d --build
```

1. Otvori `http://<host>:3001`, izaberi **Embedded MariaDB**, napravi admin nalog (bez 2FA)
2. Upiši `KUMA_USER` / `KUMA_PASS` u `.env`
3. `docker compose up -d --force-recreate kuma-sync`

WHM token: *WHM → Development → Manage API Tokens*, pravi se **na svakom serveru
posebno**, pod `root`, ograničen na javnu IP Kuma hosta.

## Korisne komande

```bash
docker compose logs -f kuma-sync                                  # log
docker compose exec kuma-sync python kuma_sync.py --dry-run       # šta bi uradio
docker compose exec kuma-sync python kuma_sync.py --test-ntfy     # probna poruka
docker compose exec kuma-sync python kuma_sync.py --expiry x.rs   # istek domena
docker compose exec kuma-sync python kuma_sync.py --ssl x.rs      # istek SSL-a
docker compose up -d --build --force-recreate kuma-sync           # posle izmene skripte
docker compose up -d --force-recreate kuma-sync                   # posle izmene .env
```

## Napomene

- Kuma 2.x registruje `login` handler tek kad pošalje `loginRequired`; sync zato
  čeka taj event pre logina (inače login tiho propadne i čeka timeout).
- Sync menja samo monitore sa tagom `cpanel-sync`. Ručno napravljene ne dira i ne duplira.
  Na svojim monitorima ne dira ručno dodate status kodove (npr. 503 za „coming soon").
- `.rs` i `.io` nemaju RDAP u IANA bazi → Kumina ugrađena provera isteka za njih ne radi,
  zato sync koristi WHOIS.
