#!/usr/bin/env python3
"""
kuma_sync.py — sinhronizuje WHM/cPanel servere u Uptime Kuma.

Sajtovi
- Povlači domene preko WHM API-ja (get_domain_info + listaccts) sa jednog ili više servera
- Za svaki server grupa "cPanel · cp1"; monitori proveravaju status I sadržaj (keyword)
- Tagovi: cpanel-sync (server/user), addon/sub/parked, Cloudflare
- Prati samo domene koji DNS-om stvarno pokazuju na naš server (ili Cloudflare)
- Domen preseljen sa cp2 na cp1 -> monitor se premesti; domen nestao -> pauza

Infrastruktura
- Za svaki WHM server automatski: WHM, cPanel, Webmail, mail/FTP portovi, ping
- Nameserveri (DNS monitor), dodatni monitori iz INFRA_EXTRA, provera sopstvene linije

Notifikacije
- ntfy (aplikacija na telefonu): poseban topic za sajtove i za infrastrukturu

Istek domena i SSL-a (poseban ntfy topic NTFY_TOPIC_EXPIRY)
- RDAP gde postoji, WHOIS za .rs/.io i ostale bez RDAP-a; alarm na 30/14/7/3/1 dan
- SSL sertifikat koji sajt stvarno servira; alarm na 14/7/3/1 dan (= AutoSSL ne obnavlja)
- Datume upisuje u opis monitora

Pokretanje:
  python kuma_sync.py                  # jedan prolaz
  python kuma_sync.py --dry-run        # samo prikaže šta bi uradio
  python kuma_sync.py --loop           # radi na svakih SYNC_INTERVAL sekundi
  python kuma_sync.py --expiry DOMEN   # test: kada ističe domen
  python kuma_sync.py --test-ntfy      # test: pošalji probnu poruku na telefon
  python kuma_sync.py --ssl DOMEN      # test: kada ističe SSL sertifikat
"""
import argparse
import fnmatch
import ipaddress
import json
import os
import re
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
import sys
import threading
import time
import traceback
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import tldextract
from uptime_kuma_api import UptimeKumaApi, MonitorType, NotificationType

TAG_NAME = "cpanel-sync"
TAG_COLOR = "#2563EB"
CF_TAG_NAME = "Cloudflare"
CF_TAG_COLOR = "#F38020"
# Monitore koje je SYNC pauzirao obeležava ovim tagom i samo njih ponovo pokreće.
# Ručno pauzirane monitore (bez ovog taga) nikad ne pokreće.
PAUSE_TAG_NAME = "auto-pauza"
PAUSE_TAG_COLOR = "#6B7280"
INFRA_TAG_NAME = "infra"
INFRA_TAG_COLOR = "#DC2626"
# Tagovi za tip domena (main se ne obeležava)
TYPE_TAGS = {
    "addon": "#7C3AED",
    "sub": "#0D9488",
    "parked": "#CA8A04",
}
GROUP_PREFIX = "cPanel · "
INFRA_GROUP = "Infrastruktura"
LOCAL_GROUP = "Lokalna veza (Kuma)"
NTFY_SITES_NAME = "ntfy · sajtovi"
NTFY_INFRA_NAME = "ntfy · infrastruktura"
EXPIRY_PREFIX = "Domen ističe:"
SSL_PREFIX = "SSL"
# opis monitora koji počinje ovim je pisao sync (sme da ga menja)
SYNC_DESC_PREFIXES = (EXPIRY_PREFIX, "SSL ističe:", "SSL:")

STATE_DIR = os.environ.get("STATE_DIR", "/app/state")


# ------------------------------------------------------------- helpers ---

def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"[greška] nedostaje env promenljiva {name}")
    return val


def env_bool(name, default="true"):
    return str(env(name, default)).strip().lower() in ("1", "true", "yes", "da")


def env_int(name, default):
    return int(str(env(name, str(default))).strip() or default)


def env_list(name, default="", sep=","):
    return [x.strip() for x in str(env(name, default)).split(sep) if x.strip()]


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def load_state(name):
    try:
        with open(os.path.join(STATE_DIR, name)) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(name, data):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = os.path.join(STATE_DIR, name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, os.path.join(STATE_DIR, name))
    except Exception as e:
        log(f"[upozorenje] ne mogu da sačuvam {name}: {e}")


# ---------------------------------------------------------------- ntfy ---

def ntfy_server():
    return env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")


def expiry_topic():
    """Istek domena i SSL-a: poseban topic, ili topic sajtova ako nije podešen."""
    return env("NTFY_TOPIC_EXPIRY", "") or env("NTFY_TOPIC_SITES", "")


def ntfy_send(topic, title, message, priority=3, tags=None):
    """Direktna poruka na telefon (za istek domena i greške samog sync-a)."""
    if not topic:
        return
    headers = {}
    if env("NTFY_TOKEN", ""):
        headers["Authorization"] = "Bearer " + env("NTFY_TOKEN")
    try:
        requests.post(ntfy_server(), json={
            "topic": topic, "title": title, "message": message,
            "priority": priority, "tags": tags or [],
        }, headers=headers, timeout=15).raise_for_status()
    except Exception as e:
        log(f"[upozorenje] ntfy slanje nije uspelo: {e}")


# ---------------------------------------------------------------- WHM ---

def parse_servers():
    servers = []
    for item in env_list("WHM_SERVERS"):
        parts = item.split("|")
        if len(parts) != 3:
            sys.exit(f"[greška] WHM_SERVERS stavka mora biti ime|url|token: {item!r}")
        name, url, token = (p.strip() for p in parts)
        servers.append({"name": name, "url": url.rstrip("/"), "token": token,
                        "host": urlparse(url).hostname})
    if not servers:
        sys.exit("[greška] WHM_SERVERS je prazan")
    return servers


def whm_call(server, func, **params):
    params["api.version"] = 1
    r = requests.get(
        f"{server['url']}/json-api/{func}",
        params=params,
        headers={"Authorization": f"whm root:{server['token']}"},
        timeout=60,
        verify=env_bool("WHM_VERIFY_SSL"),
    )
    r.raise_for_status()
    data = r.json()
    meta = data.get("metadata", {})
    if meta.get("result") != 1:
        raise RuntimeError(f"{server['name']} {func}: {meta.get('reason')}")
    return data.get("data", {})


# Cloudflare IPv4 opsezi (https://www.cloudflare.com/ips-v4) — domen iza CF proxy-ja
# ne pokazuje na naš IP, ali je i dalje hostovan kod nas.
CLOUDFLARE_NETS = [ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
)]


def resolve_a(domain):
    try:
        return {ai[4][0] for ai in socket.getaddrinfo(domain, None, socket.AF_INET)}
    except (socket.gaierror, UnicodeError):
        return set()


def is_cloudflare(ip):
    try:
        return any(ipaddress.ip_address(ip) in n for n in CLOUDFLARE_NETS)
    except ValueError:
        return False


def dns_verdict(domain, server_ips):
    """Vraća (verdict, na_cloudflare). verdict: 'ok' | 'nxdomain' | 'elsewhere:<ip>'"""
    mode = env("DNS_CHECK", "ours").lower()
    ips = resolve_a(domain)
    on_cf = bool(ips) and all(is_cloudflare(ip) for ip in ips)
    if mode == "off":
        return "ok", on_cf
    if not ips:
        return "nxdomain", False
    if mode == "resolves":
        return "ok", on_cf
    if any(ip in server_ips for ip in ips):
        return "ok", on_cf
    if on_cf and env_bool("DNS_ALLOW_CLOUDFLARE", "true"):
        return "ok", True
    return "elsewhere:" + ",".join(sorted(ips)), on_cf


def fetch_domains(server):
    """Vraća {domen: (cpanel_user, na_cloudflare, tip)} za jedan WHM server, posle filtera."""
    types = set(env_list("DOMAIN_TYPES", "main,addon,parked,sub"))
    excludes = env_list("EXCLUDE", "")
    skip_suspended = env_bool("SKIP_SUSPENDED")

    accts = whm_call(server, "listaccts", want="user,suspended,ip").get("acct", [])
    suspended = {a["user"] for a in accts if int(a.get("suspended", 0))} if skip_suspended else set()

    domains = whm_call(server, "get_domain_info").get("domains", [])

    # IP adrese servera: sve IP iz naloga + ipv4 iz domain_info + SERVER_IPS iz .env
    server_ips = set(env_list("SERVER_IPS", ""))
    server_ips |= {a.get("ip") for a in accts if a.get("ip")}
    for d in domains:
        for key in ("ipv4", "ipv4_ssl"):
            if d.get(key):
                server_ips.add(d[key])

    # dijagnostika: šta WHM uopšte vraća
    users_with_domains = {d.get("user") for d in domains}
    by_type = Counter(d.get("domain_type") for d in domains)
    log(f"{server['name']}: WHM vraća {len(domains)} domena "
        f"({', '.join(f'{k}={v}' for k, v in sorted(by_type.items()))}) "
        f"za {len(users_with_domains)}/{len(accts)} naloga")
    missing_users = {a["user"] for a in accts} - users_with_domains
    if missing_users:
        log(f"{server['name']}: [upozorenje] get_domain_info ne vraća domene za naloge: "
            f"{', '.join(sorted(missing_users))}")

    # Pomoćni subdomeni koje cPanel sam pravi uz svaki addon domen
    # (npr. triaris.rs.lartgroupe.com) — isti docroot kao addon, nisu pravi sajt.
    addon_docroots = {(d.get("user"), d.get("docroot")) for d in domains
                      if d.get("domain_type") == "addon" and d.get("docroot")}
    addon_names = {}
    for d in domains:
        if d.get("domain_type") == "addon":
            addon_names.setdefault(d.get("user"), []).append(d.get("domain", "").lower())

    def is_addon_helper(d):
        if d.get("domain_type") != "sub":
            return False
        name = d.get("domain", "").lower()
        if d.get("docroot") and addon_docroots:
            # WHM vraća docroot -> to je pouzdan kriterijum, ne nagađamo po imenu
            return (d.get("user"), d.get("docroot")) in addon_docroots
        for a in addon_names.get(d.get("user"), []):
            if name.startswith(a + ".") or name.split(".")[0] == a.split(".")[0]:
                return True
        return False

    result, skipped = {}, Counter()
    for d in domains:
        domain = d.get("domain", "").lower().strip(".")
        user = d.get("user", "")
        if not domain or d.get("domain_type") not in types:
            skipped["tip"] += 1
            continue
        if user in suspended:
            skipped["suspendovan"] += 1
            continue
        if is_addon_helper(d):
            skipped["addon-pomoćni-sub"] += 1
            continue
        if any(fnmatch.fnmatch(domain, pat) for pat in excludes):
            skipped["exclude"] += 1
            continue
        verdict, on_cf = dns_verdict(domain, server_ips)
        if verdict != "ok":
            skipped["dns"] += 1
            log(f"  preskačem {domain} ({user}): DNS {verdict}")
            continue
        result[domain] = (user, on_cf, d.get("domain_type"))
    if skipped:
        log(f"{server['name']}: preskočeno " + ", ".join(f"{k}={v}" for k, v in skipped.items()))
    return result


# --------------------------------------------------------- istek domena ---

_tld = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)  # radi offline


def registrable(domain):
    r = _tld(domain)
    reg = getattr(r, "top_domain_under_public_suffix", None) or r.registered_domain
    return reg or None


_rdap_map = None
_whois_servers = {}


def rdap_base(tld):
    global _rdap_map
    if _rdap_map is None:
        _rdap_map = {}
        try:
            data = requests.get("https://data.iana.org/rdap/dns.json", timeout=30).json()
            for tlds, urls in data.get("services", []):
                for t in tlds:
                    _rdap_map[t.lower()] = urls[0].rstrip("/") + "/"
        except Exception as e:
            log(f"[upozorenje] IANA RDAP lista nedostupna ({e}), koristim samo WHOIS")
    return _rdap_map.get(tld.lower())


def whois_query(server, query):
    with socket.create_connection((server, 43), timeout=20) as s:
        s.sendall((query + "\r\n").encode())
        chunks = []
        while True:
            b = s.recv(4096)
            if not b:
                break
            chunks.append(b)
    return b"".join(chunks).decode("utf-8", "replace")


def whois_server_for(tld):
    if tld not in _whois_servers:
        srv = None
        try:
            m = re.search(r"(?im)^whois:\s*(\S+)", whois_query("whois.iana.org", tld))
            srv = m.group(1) if m else None
        except Exception as e:
            log(f"[upozorenje] whois.iana.org za .{tld}: {e}")
        _whois_servers[tld] = srv
    return _whois_servers[tld]


_DATE_FORMATS = (
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%b-%Y", "%Y.%m.%d", "%d/%m/%Y", "%Y/%m/%d",
)


def parse_date(text):
    text = text.strip()
    text = re.sub(r"\.\d+(?=Z|$|[+-])", "", text)          # 2027-01-01T00:00:00.000Z
    text = re.sub(r"(?<=\d\d:\d\d:\d\d)[+-]\d\d:?\d\d$", "", text).strip()  # ...T10:00:00+02:00
    text = re.sub(r"\s+(UTC|GMT|CET|CEST)$", "", text)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        return datetime.strptime(m.group(0), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    m = re.search(r"\d{2}\.\d{2}\.\d{4}", text)
    if m:
        return datetime.strptime(m.group(0), "%d.%m.%Y").replace(tzinfo=timezone.utc)
    return None


_EXPIRY_RE = re.compile(
    r"(?im)^\s*(?:registry\s+)?(?:expiration|expiry|expires|expire|paid-till|valid until|"
    r"renewal date|registrar registration expiration)[\w\s]*?:\s*(.+?)\s*$"
)


def parse_whois_expiry(text):
    for m in _EXPIRY_RE.finditer(text):
        d = parse_date(m.group(1))
        if d:
            return d
    return None


def lookup_expiry(domain):
    """Vraća (datetime | None, izvor)."""
    tld = domain.rsplit(".", 1)[-1]
    base = rdap_base(tld)
    if base:
        try:
            r = requests.get(base + "domain/" + domain, timeout=30,
                             headers={"Accept": "application/rdap+json"})
            if r.ok:
                for ev in r.json().get("events", []):
                    if ev.get("eventAction") == "expiration":
                        d = parse_date(ev.get("eventDate", ""))
                        if d:
                            return d, "rdap"
        except Exception as e:
            log(f"  RDAP {domain}: {e}")
    srv = whois_server_for(tld)
    if srv:
        try:
            return parse_whois_expiry(whois_query(srv, domain)), "whois:" + srv
        except Exception as e:
            log(f"  WHOIS {domain} ({srv}): {e}")
    return None, "nema"


def check_expiry(reg_domains):
    """reg_domains: {registrable: [opis klijenta, ...]}. Vraća {registrable: datetime|None}.
    Proverava svaki domen najviše jednom u EXPIRY_CHECK_HOURS, šalje ntfy alarme."""
    state = load_state("expiry.json")
    every = env_int("EXPIRY_CHECK_HOURS", 24) * 3600
    days_list = sorted({int(x) for x in env_list("DOMAIN_EXPIRY_DAYS", "30,14,7,3,1")}, reverse=True)
    topic = expiry_topic()
    now = datetime.now(timezone.utc)
    out = {}
    checked = 0

    for dom in sorted(reg_domains):
        st = state.get(dom, {})
        if time.time() - st.get("checked", 0) > every:
            exp, src = lookup_expiry(dom)
            checked += 1
            time.sleep(1)  # pristojno prema WHOIS serverima
            new_exp = exp.isoformat() if exp else None
            if new_exp != st.get("expires"):
                st["alerted"] = []  # obnovljen ili prvi put viđen
            st.update({"expires": new_exp, "source": src, "checked": time.time()})
            state[dom] = st
            if not exp:
                log(f"  istek {dom}: nepoznat ({src})")
        exp = datetime.fromisoformat(st["expires"]) if st.get("expires") else None
        out[dom] = exp
        if not exp:
            continue
        left = (exp - now).days
        crossed = [d for d in days_list if left <= d and d not in st.get("alerted", [])]
        if crossed:
            who = ", ".join(reg_domains[dom])
            title = (f"Domen {dom} je ISTEKAO" if left < 0
                     else f"Domen {dom} ističe za {left} dana")
            log(f"⚠ {title} ({exp:%d.%m.%Y}) — {who}")
            ntfy_send(topic, title, f"Datum isteka: {exp:%d.%m.%Y}\nKlijent: {who}",
                      priority=5 if left <= 7 else 4, tags=["hourglass"])
            st["alerted"] = sorted(set(st.get("alerted", [])) | set(crossed))
            state[dom] = st

    save_state("expiry.json", state)
    known = sum(1 for v in out.values() if v)
    log(f"istek domena: {known}/{len(out)} poznato (sada proveravano {checked})")
    return out


# ------------------------------------------------------------ SSL ---

def get_cert(domain, port=443):
    """Vraća (notAfter datetime | None, greška | None) za sertifikat koji sajt stvarno servira."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((domain, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                cert = tls.getpeercert()
        exp = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc)
        return exp, None
    except ssl.SSLCertVerificationError as e:
        return None, f"nevažeći sertifikat: {e.verify_message}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def check_ssl(domains):
    """domains: {domen: opis klijenta}. Vraća {domen: (datetime|None, greška|None)}.
    Alarm kad ostane SSL_EXPIRY_DAYS dana — kod cPanel-a to znači da AutoSSL ne uspeva da obnovi."""
    state = load_state("ssl.json")
    every = env_int("EXPIRY_CHECK_HOURS", 24) * 3600
    days_list = sorted({int(x) for x in env_list("SSL_EXPIRY_DAYS", "14,7,3,1")}, reverse=True)
    now = datetime.now(timezone.utc)

    todo = [d for d in domains if time.time() - state.get(d, {}).get("checked", 0) > every]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for dom, (exp, err) in zip(todo, pool.map(get_cert, todo)):
            st = state.get(dom, {})
            new_exp = exp.isoformat() if exp else None
            if new_exp != st.get("expires"):
                st["alerted"] = []  # obnovljen
            st.update({"expires": new_exp, "error": err, "checked": time.time()})
            state[dom] = st
            if err:
                log(f"  SSL {dom}: {err}")

    out = {}
    for dom, who in domains.items():
        st = state.get(dom, {})
        exp = datetime.fromisoformat(st["expires"]) if st.get("expires") else None
        out[dom] = (exp, st.get("error"))
        if not exp:
            continue
        left = (exp - now).days
        crossed = [d for d in days_list if left <= d and d not in st.get("alerted", [])]
        if crossed:
            title = (f"SSL za {dom} je ISTEKAO" if left < 0
                     else f"SSL za {dom} ističe za {left} dana")
            log(f"⚠ {title} ({exp:%d.%m.%Y}) — {who}")
            ntfy_send(expiry_topic(), title,
                      f"Datum isteka: {exp:%d.%m.%Y}\nKlijent: {who}\n"
                      f"AutoSSL verovatno ne uspeva da obnovi — WHM > Manage AutoSSL > Logs",
                      priority=5 if left <= 3 else 4, tags=["lock"])
            st["alerted"] = sorted(set(st.get("alerted", [])) | set(crossed))
            state[dom] = st

    save_state("ssl.json", state)
    ok = sum(1 for e, _ in out.values() if e)
    log(f"SSL: {ok}/{len(out)} sertifikata očitano (sada proveravano {len(todo)})")
    return out


# --------------------------------------------------------------- Kuma ---

def host_of(url):
    try:
        return (urlparse(url).hostname or "").lower().strip(".")
    except Exception:
        return ""


class ReadyKumaApi(UptimeKumaApi):
    """UptimeKumaApi koji čeka da Kuma bude spremna za login.

    Kuma 2.x registruje "login" handler tek na kraju obrade nove konekcije
    (posle toga šalje "loginRequired"). Ako login stigne ranije, Kuma ga tiho
    ignoriše i klijent visi do timeout-a. Zato čekamo "loginRequired"/"autoLogin".
    """

    def connect(self):
        self._kuma_ready = threading.Event()
        self.sio.on("loginRequired", lambda *a: self._kuma_ready.set())
        # autoLogin = auth isključen u Kumi; i dalje znači "spremno"
        self.sio.on("autoLogin", lambda *a: (self._kuma_ready.set(), self._event_auto_login()))
        super().connect()


def connect_kuma():
    url = env("KUMA_URL", "http://localhost:3001")
    timeout = float(env("KUMA_TIMEOUT", "120"))
    ready_wait = 60
    for attempt in range(1, 6):
        api = ReadyKumaApi(url, timeout=timeout)
        if api._kuma_ready.wait(ready_wait):
            return api
        log(f"Kuma: konekcija nije spremna posle {ready_wait}s (pokušaj {attempt}/5), ponovo...")
        api.disconnect()
        time.sleep(3)
    raise RuntimeError("Kuma ne šalje loginRequired — proveri uptime-kuma log")


def get_tag_id(api, dry, name=TAG_NAME, color=TAG_COLOR):
    for t in api.get_tags():
        if t["name"] == name:
            return t["id"]
    if dry:
        return None
    return api.add_tag(name=name, color=color)["id"]


def find_tag(monitor, name):
    for t in monitor.get("tags") or []:
        if t.get("name") == name:
            return t
    return None


def managed_tag(monitor):
    return find_tag(monitor, TAG_NAME)


def sync_pause(api, m, pause_tag_id, dry):
    """Pauziraj monitor i obeleži da ga je pauzirao sync."""
    if dry:
        return
    if not find_tag(m, PAUSE_TAG_NAME):
        api.add_monitor_tag(tag_id=pause_tag_id, monitor_id=m["id"], value="")
    api.pause_monitor(m["id"])


def sync_resume(api, m, label, stats, dry):
    """Nastavi praćenje SAMO ako je monitor pauzirao sync. Vraća True ako je nastavljen."""
    t = find_tag(m, PAUSE_TAG_NAME)
    if m.get("active", True):
        # aktivan — ako ima zaostali auto-pauza tag (npr. ti si ga ručno pokrenuo), skloni tag
        if t and not dry:
            api.delete_monitor_tag(tag_id=t["tag_id"], monitor_id=m["id"], value=t.get("value", ""))
        return False
    if not t:
        stats["ručno pauzirano"] += 1  # ti si ga pauzirao — ne diramo
        return False
    log(f"▶ {label}  ponovo za praćenje, nastavljam")
    stats["nastavljeno"] += 1
    if not dry:
        api.resume_monitor(m["id"])
        api.delete_monitor_tag(tag_id=t["tag_id"], monitor_id=m["id"], value=t.get("value", ""))
    return True


def ensure_group(api, group_name, monitors, dry):
    for m in monitors:
        if m["type"] == MonitorType.GROUP and m["name"] == group_name:
            return m["id"]
    log(f"+ grupa  {group_name}")
    if dry:
        return None
    gid = api.add_monitor(type=MonitorType.GROUP, name=group_name)["monitorID"]
    monitors.append({"id": gid, "type": MonitorType.GROUP, "name": group_name})
    return gid


def ensure_ntfy(api, name, topic, priority, dry):
    """Napravi/ažuriraj ntfy notifikaciju u Kumi. Vraća id ili None."""
    if not topic:
        return None
    want = {
        "type": NotificationType.NTFY,
        "ntfyserverurl": ntfy_server(),
        "ntfytopic": topic,
        "ntfyPriority": priority,
        "ntfyAuthenticationMethod": "accessToken" if env("NTFY_TOKEN", "") else "none",
    }
    if env("NTFY_TOKEN", ""):
        want["ntfyaccesstoken"] = env("NTFY_TOKEN")
    for n in api.get_notifications():
        if n.get("name") == name:
            if any(n.get(k) != v for k, v in want.items()):
                log(f"~ notifikacija {name}")
                if not dry:
                    api.edit_notification(n["id"], name=name, isDefault=False, **want)
            return n["id"]
    log(f"+ notifikacija {name} (topic {topic})")
    if dry:
        return None
    return api.add_notification(name=name, isDefault=False, applyExisting=False, **want)["id"]


def extra_notification_ids(api):
    ids = [int(x) for x in env_list("NOTIFICATION_IDS", "")]
    if ids:
        return ids
    return [n["id"] for n in api.get_notifications()
            if n.get("isDefault") and n.get("name") not in (NTFY_SITES_NAME, NTFY_INFRA_NAME)]


def notif_set(m):
    v = m.get("notificationIDList") or []
    if isinstance(v, dict):
        return {int(k) for k, on in v.items() if on}
    return {int(x) for x in v}


def monitor_changes(m, want):
    """Razlike između postojećeg monitora i željenih podešavanja."""
    changes = {}
    for k, v in want.items():
        cur = m.get(k)
        if k == "notificationIDList":
            if notif_set(m) != set(v):
                changes[k] = v
        elif k == "type":
            if str(cur) != str(v):
                changes[k] = v
        elif (cur or None) != (v or None):
            changes[k] = v
    return changes


# --------------------------------------------------------- infra lista ---

def build_infra(servers):
    """Lista infrastrukturnih monitora: {key, group, type, ...kuma polja}."""
    items = []
    interval = env_int("INFRA_INTERVAL", 60)

    def add(key, group, **kw):
        kw.setdefault("interval", interval)
        items.append({"key": key, "group": group, **kw})

    if env_bool("INFRA_AUTO", "true"):
        ports = [int(p) for p in env_list("INFRA_AUTO_PORTS", "465,587,993,21")]
        port_names = {21: "FTP", 25: "SMTP 25", 465: "SMTP 465", 587: "SMTP 587",
                      110: "POP3", 995: "POP3S", 143: "IMAP", 993: "IMAPS", 3306: "MySQL"}
        for s in servers:
            h, n = s["host"], s["name"]
            add(f"{n} · ping", INFRA_GROUP, type=MonitorType.PING, hostname=h)
            for label, port in (("WHM", 2087), ("cPanel", 2083), ("Webmail", 2096)):
                add(f"{n} · {label}", INFRA_GROUP, type=MonitorType.HTTP,
                    url=f"https://{h}:{port}", accepted_statuscodes=["200-299", "300-399"])
            for p in ports:
                add(f"{n} · {port_names.get(p, f'port {p}')}", INFRA_GROUP,
                    type=MonitorType.PORT, hostname=h, port=p)

    test_domain = env("DNS_TEST_DOMAIN", "")
    for ns in env_list("NAMESERVERS", ""):
        ips = sorted(resolve_a(ns))
        if not ips or not test_domain:
            log(f"[upozorenje] nameserver {ns}: nema IP ili DNS_TEST_DOMAIN nije podešen")
            continue
        add(f"DNS · {ns}", INFRA_GROUP, type=MonitorType.DNS, hostname=test_domain,
            dns_resolve_server=ips[0], port=53, dns_resolve_type="A")

    # INFRA_EXTRA="ime|tip|cilj;ime|tip|cilj"
    #   tip: http | http-insecure | port (host:port) | ping | dns (domen@resolver-ip)
    for item in env_list("INFRA_EXTRA", "", sep=";"):
        parts = [p.strip() for p in item.split("|")]
        if len(parts) != 3:
            log(f"[upozorenje] INFRA_EXTRA stavka mora biti ime|tip|cilj: {item!r}")
            continue
        name, typ, target = parts
        typ = typ.lower()
        if typ in ("http", "http-insecure"):
            add(name, INFRA_GROUP, type=MonitorType.HTTP, url=target,
                ignoreTls=(typ == "http-insecure"), accepted_statuscodes=["200-299", "300-399"])
        elif typ == "port":
            h, _, p = target.rpartition(":")
            add(name, INFRA_GROUP, type=MonitorType.PORT, hostname=h, port=int(p))
        elif typ == "ping":
            add(name, INFRA_GROUP, type=MonitorType.PING, hostname=target)
        elif typ == "dns":
            d, _, resolver = target.partition("@")
            add(name, INFRA_GROUP, type=MonitorType.DNS, hostname=d,
                dns_resolve_server=resolver or "1.1.1.1", port=53, dns_resolve_type="A")
        else:
            log(f"[upozorenje] INFRA_EXTRA nepoznat tip {typ!r} za {name}")

    # sopstvena linija: ako su ovi dole, padovi sajtova su verovatno lažni
    for ip in env_list("INTERNET_CHECK", "1.1.1.1,8.8.8.8"):
        add(f"Internet · {ip}", LOCAL_GROUP, type=MonitorType.PING, hostname=ip)
    return items


# ---------------------------------------------------------------- sync ---

def sync(dry=False):
    servers = parse_servers()

    # 1) domeni sa svih servera
    desired = {}  # domen -> (server, user, na_cloudflare, tip)
    for s in servers:
        try:
            doms = fetch_domains(s)
        except Exception as e:
            # Ako server ne odgovori, NE diramo njegove monitore (da ne pauziramo sve greškom)
            log(f"[greška] {s['name']}: {e} — preskačem ovaj server u ovom prolazu")
            s["failed"] = True
            continue
        n_cf = sum(1 for _, cf, _ in doms.values() if cf)
        by_t = Counter(t for _, _, t in doms.values())
        log(f"{s['name']}: {len(doms)} domena za praćenje "
            f"({', '.join(f'{k}={v}' for k, v in sorted(by_t.items()))}; {n_cf} na Cloudflare-u)")
        for dom, (user, cf, dtype) in doms.items():
            desired.setdefault(dom, (s["name"], user, cf, dtype))
    failed = {s["name"] for s in servers if s.get("failed")}

    # 2) istek domena (samo registrabilni domeni: main/addon, ne subdomeni)
    expiry = {}
    if env_bool("DOMAIN_EXPIRY", "true"):
        reg = {}
        for dom, (srv, user, _, _) in desired.items():
            r = registrable(dom)
            if r == dom:
                reg.setdefault(r, []).append(f"{user} ({srv})")
        try:
            expiry = check_expiry(reg)
        except Exception as e:
            log(f"[upozorenje] provera isteka domena nije uspela: {e}")

    # 2b) SSL sertifikati (sync sam proverava, da bi alarmi išli na topic za istek)
    ssl_check = env_bool("SSL_CHECK", "true")
    ssl_info = {}
    if ssl_check:
        try:
            ssl_info = check_ssl({d: f"{u} ({s})" for d, (s, u, _, _) in desired.items()})
        except Exception as e:
            log(f"[upozorenje] SSL provera nije uspela: {e}")

    on_removed = env("ON_REMOVED", "pause").lower()
    keyword = env("CONTENT_ERROR_KEYWORD", "Fatal error")
    site_base = {
        "interval": env_int("MONITOR_INTERVAL", 300),
        "retryInterval": env_int("RETRY_INTERVAL", 60),
        "maxretries": env_int("MONITOR_RETRIES", 3),
        # Kumin SSL alarm ide na iste notifikacije kao padovi — kad sync proverava SSL, gasimo ga
        "expiryNotification": not ssl_check,
    }
    if keyword:
        site_base.update(type=MonitorType.KEYWORD, keyword=keyword, invertKeyword=True)
    else:
        site_base.update(type=MonitorType.HTTP)
    enforce = env_bool("ENFORCE_SETTINGS", "true")

    with connect_kuma() as api:
        log("Kuma: login...")
        api.login(env("KUMA_USER", required=True), env("KUMA_PASS", required=True))
        log("Kuma: ulogovan, čitam tagove/notifikacije/monitore...")

        tag_id = get_tag_id(api, dry)
        cf_tag_id = get_tag_id(api, dry, CF_TAG_NAME, CF_TAG_COLOR)
        infra_tag_id = get_tag_id(api, dry, INFRA_TAG_NAME, INFRA_TAG_COLOR)
        pause_tag_id = get_tag_id(api, dry, PAUSE_TAG_NAME, PAUSE_TAG_COLOR)
        type_tag_ids = {t: get_tag_id(api, dry, t, c) for t, c in TYPE_TAGS.items()}

        extra = extra_notification_ids(api)
        n_sites = ensure_ntfy(api, NTFY_SITES_NAME, env("NTFY_TOPIC_SITES", ""),
                              env_int("NTFY_PRIORITY_SITES", 3), dry)
        n_infra = ensure_ntfy(api, NTFY_INFRA_NAME, env("NTFY_TOPIC_INFRA", ""),
                              env_int("NTFY_PRIORITY_INFRA", 5), dry)
        site_notif = sorted({*extra, *([n_sites] if n_sites else [])})
        infra_notif = sorted({*extra, *([n_infra] if n_infra else [])})
        site_base["notificationIDList"] = site_notif

        monitors = api.get_monitors()
        log(f"Kuma: {len(monitors)} postojećih monitora")

        groups = {s["name"]: ensure_group(api, GROUP_PREFIX + s["name"], monitors, dry)
                  for s in servers}

        managed = {}        # host -> monitor (sajtovi)
        infra_managed = {}  # key -> monitor (infrastruktura)
        unmanaged = set()   # host-ovi koje već prati ručno napravljen monitor
        for m in monitors:
            if m["type"] == MonitorType.GROUP:
                continue
            t = managed_tag(m)
            if t and str(t.get("value", "")).startswith("infra:"):
                infra_managed[t["value"][6:]] = m
                continue
            h = host_of(m.get("url") or "") or (m.get("hostname") or "").lower()
            if not h:
                continue
            if t:
                managed[h] = m
            else:
                unmanaged.add(h)

        stats = Counter()

        # 3) sajtovi: dodaj nove / ažuriraj postojeće
        for dom in sorted(desired):
            srv, user, on_cf, dtype = desired[dom]
            tag_value = f"{srv}/{user}"
            parent = groups.get(srv)
            want = dict(site_base)
            now = datetime.now(timezone.utc)
            parts = []
            exp = expiry.get(dom)
            if exp:
                parts.append(f"{EXPIRY_PREFIX} {exp:%d.%m.%Y} ({(exp - now).days} d)")
            sexp, serr = ssl_info.get(dom, (None, None))
            if sexp:
                parts.append(f"SSL ističe: {sexp:%d.%m.%Y} ({(sexp - now).days} d)")
            elif serr and ssl_check:
                parts.append(f"SSL: {serr[:80]}")
            if parts:
                want["description"] = " · ".join(parts)

            if dom in unmanaged and dom not in managed:
                continue  # već postoji ručni monitor, ne dupliramo

            m = managed.get(dom)
            if m is None:
                log(f"+ {dom}  ({tag_value}) [{dtype}]{'  [Cloudflare]' if on_cf else ''}")
                stats["dodato"] += 1
                if dry:
                    continue
                res = api.add_monitor(
                    name=dom,
                    url=f"https://{dom}",
                    parent=parent,
                    maxredirects=10,
                    accepted_statuscodes=["200-299", "300-399"],
                    **want,
                )
                mid = res["monitorID"]
                api.add_monitor_tag(tag_id=tag_id, monitor_id=mid, value=tag_value)
                if on_cf:
                    api.add_monitor_tag(tag_id=cf_tag_id, monitor_id=mid, value="")
                if dtype in type_tag_ids:
                    api.add_monitor_tag(tag_id=type_tag_ids[dtype], monitor_id=mid, value="")
                time.sleep(0.2)
                continue

            # postoji — grupa, server tag, podešavanja, tagovi, pauza
            old_tag = managed_tag(m)
            if m.get("parent") != parent or old_tag.get("value") != tag_value:
                log(f"~ {dom}  {old_tag.get('value')} -> {tag_value}")
                stats["premešteno"] += 1
                if not dry:
                    if m.get("parent") != parent:
                        api.edit_monitor(m["id"], parent=parent)
                    api.delete_monitor_tag(tag_id=tag_id, monitor_id=m["id"], value=old_tag.get("value", ""))
                    api.add_monitor_tag(tag_id=tag_id, monitor_id=m["id"], value=tag_value)

            if enforce:
                # opis menjamo samo ako je prazan ili ga je pisao sync
                if "description" in want and (m.get("description") or "").strip() and \
                        not (m.get("description") or "").startswith(SYNC_DESC_PREFIXES):
                    want.pop("description")
                ch = monitor_changes(m, want)
                if ch:
                    only_desc = set(ch) == {"description"}
                    if not only_desc:
                        log(f"⚙ {dom}  podešavanja: {', '.join(sorted(ch))}")
                    stats["opis" if only_desc else "podešeno"] += 1
                    if not dry:
                        api.edit_monitor(m["id"], **ch)

            has_cf = find_tag(m, CF_TAG_NAME)
            if on_cf and not has_cf:
                log(f"☁ {dom}  sada iza Cloudflare-a, dodajem tag")
                stats["cloudflare"] += 1
                if not dry:
                    api.add_monitor_tag(tag_id=cf_tag_id, monitor_id=m["id"], value="")
            elif not on_cf and has_cf:
                log(f"☁ {dom}  više nije iza Cloudflare-a, skidam tag")
                stats["cloudflare"] += 1
                if not dry:
                    api.delete_monitor_tag(tag_id=cf_tag_id, monitor_id=m["id"], value=has_cf.get("value", ""))
            for t, tid in type_tag_ids.items():
                has_t = find_tag(m, t)
                if t == dtype and not has_t:
                    log(f"# {dom}  tag {t}")
                    if not dry:
                        api.add_monitor_tag(tag_id=tid, monitor_id=m["id"], value="")
                elif t != dtype and has_t:
                    log(f"# {dom}  skidam tag {t}")
                    if not dry:
                        api.delete_monitor_tag(tag_id=tid, monitor_id=m["id"], value=has_t.get("value", ""))
            sync_resume(api, m, dom, stats, dry)

        # 4) sajtovi kojih više nema
        for dom, m in managed.items():
            if dom in desired:
                continue
            srv = (managed_tag(m).get("value") or "").split("/")[0]
            if srv in failed:
                continue  # server nije odgovorio, ne zaključujemo ništa
            if on_removed == "delete":
                log(f"- {dom}  obrisan")
            elif on_removed == "pause" and m.get("active", True):
                log(f"⏸ {dom}  nije više za praćenje (nema ga na serveru ili DNS ne pokazuje kod nas), pauziram")
            else:
                continue
            stats["uklonjeno"] += 1
            if dry:
                continue
            if on_removed == "delete":
                api.delete_monitor(m["id"])
            else:
                sync_pause(api, m, pause_tag_id, dry)

        # 5) infrastruktura
        infra = build_infra(servers)
        infra_groups = {g: ensure_group(api, g, monitors, dry) for g in {i["group"] for i in infra}}
        infra_keys = set()
        for item in infra:
            key = item["key"]
            infra_keys.add(key)
            want = {k: v for k, v in item.items() if k not in ("key", "group")}
            want.update(retryInterval=env_int("INFRA_RETRY_INTERVAL", 30),
                        maxretries=env_int("INFRA_RETRIES", 2),
                        notificationIDList=infra_notif)
            parent = infra_groups.get(item["group"])
            m = infra_managed.get(key)
            if m is None:
                log(f"+ infra  {key}")
                stats["infra+"] += 1
                if dry:
                    continue
                res = api.add_monitor(name=key, parent=parent, **want)
                api.add_monitor_tag(tag_id=tag_id, monitor_id=res["monitorID"], value="infra:" + key)
                api.add_monitor_tag(tag_id=infra_tag_id, monitor_id=res["monitorID"], value="")
                time.sleep(0.2)
                continue
            want["parent"] = parent
            ch = monitor_changes(m, want) if enforce else {}
            if ch:
                log(f"⚙ infra  {key}: {', '.join(sorted(ch))}")
                stats["infra~"] += 1
                if not dry:
                    api.edit_monitor(m["id"], **ch)
            sync_resume(api, m, "infra  " + key, stats, dry)
        for key, m in infra_managed.items():
            if key not in infra_keys and m.get("active", True):
                log(f"⏸ infra  {key}  (nije više u konfiguraciji)")
                stats["infra-"] += 1
                sync_pause(api, m, pause_tag_id, dry)

    summary = ", ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "bez promena"
    log(f"gotovo{' (DRY RUN)' if dry else ''}: {summary}")


def main():
    ap = argparse.ArgumentParser(description="cPanel -> Uptime Kuma sync")
    ap.add_argument("--dry-run", action="store_true", help="samo prikaži promene")
    ap.add_argument("--loop", action="store_true", help="ponavljaj na SYNC_INTERVAL sekundi")
    ap.add_argument("--expiry", metavar="DOMEN", help="test: proveri kada ističe domen")
    ap.add_argument("--test-ntfy", action="store_true", help="test: probna poruka na telefon")
    ap.add_argument("--ssl", metavar="DOMEN", help="test: kada ističe SSL sertifikat")
    args = ap.parse_args()

    if args.ssl:
        exp, err = get_cert(args.ssl.lower())
        print(f"{args.ssl}: SSL ističe {exp:%d.%m.%Y}" if exp else f"{args.ssl}: {err}")
        return

    if args.expiry:
        exp, src = lookup_expiry(args.expiry.lower())
        print(f"{args.expiry}: {exp:%d.%m.%Y} (izvor: {src})" if exp
              else f"{args.expiry}: datum nije pronađen (izvor: {src})")
        return
    if args.test_ntfy:
        for var in ("NTFY_TOPIC_SITES", "NTFY_TOPIC_INFRA", "NTFY_TOPIC_EXPIRY"):
            ntfy_send(env(var, ""), "Uptime Kuma test", f"Probna poruka ({var})", 3, ["test_tube"])
            print(f"poslato na {var}={env(var, '') or '(prazno)'}")
        return

    if not args.loop:
        sync(dry=args.dry_run)
        return

    every = env_int("SYNC_INTERVAL", 3600)
    fails = 0
    while True:
        try:
            sync(dry=args.dry_run)
            fails = 0
            wait = every
        except Exception as e:
            # npr. Kuma setup još nije završen ili pogrešan user/pass — probaj ponovo uskoro
            fails += 1
            log(f"[greška] sync nije uspeo: {type(e).__name__}: {e} — novi pokušaj za 2 min")
            traceback.print_exc()
            if fails == 3:
                ntfy_send(env("NTFY_TOPIC_INFRA", ""), "kuma-sync ne radi",
                          f"3 neuspela pokušaja zaredom.\n{type(e).__name__}: {e}", 4, ["warning"])
            wait = 120
        time.sleep(wait)


if __name__ == "__main__":
    main()
