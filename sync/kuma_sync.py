#!/usr/bin/env python3
"""
kuma_sync.py — syncs WHM/cPanel servers into Uptime Kuma.

Sites
- Pulls domains via the WHM API (get_domain_info + listaccts) from one or more servers
- Groups: Websites / <server> / <cPanel account>; monitors check status AND content (keyword)
- Tags: cpanel-sync (server/user), addon/sub/parked, Cloudflare
- Only monitors domains whose DNS actually points to our server (or Cloudflare)
- Domain moved from cp2 to cp1 -> monitor is moved; domain gone -> paused

Infrastructure
- Nested groups: Infrastructure / cPanel / cp1, Infrastructure / Proxmox / Node102, Infrastructure / DNS, ...
- cPanel servers (automatic from WHM_SERVERS): ping, WHM, cPanel, Webmail, mail/FTP ports
- Proxmox nodes (PROXMOX_NODES): ping, Proxmox UI, optional ports
- Per-host port overrides (INFRA_PORTS_<NAME>, PROXMOX_PORTS_<NAME>)
- Nameservers (DNS monitor), extra monitors from INFRA_EXTRA, own uplink check

Notifications
- ntfy (phone app): separate topics for sites and for infrastructure

Status pages
- public (services only: web hosting, email, DNS + chosen extras; no client sites, no nodes)
- internal infrastructure + internal "Clients Websites" (one section per client)
  — keep both behind NetBird / Cloudflare Access
- optional per-client pages (STATUS_CLIENT_PAGES)

Domain and SSL expiry (separate ntfy topic NTFY_TOPIC_EXPIRY)
- RDAP where available, WHOIS for .rs/.io and other TLDs without RDAP; alerts at 30/14/7/3/1 days
- SSL certificate actually served by the site; alerts at 14/7/3/1 days (= AutoSSL is not renewing)
- Dates are written into the monitor description

Usage:
  python kuma_sync.py                  # single run
  python kuma_sync.py --dry-run        # only show what would change
  python kuma_sync.py --loop           # run every SYNC_INTERVAL seconds
  python kuma_sync.py --expiry DOMAIN  # test: when does a domain expire
  python kuma_sync.py --ssl DOMAIN     # test: when does an SSL certificate expire
  python kuma_sync.py --test-ntfy      # test: send a test message to the phone
"""
import argparse
import fnmatch
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import tldextract
from uptime_kuma_api import UptimeKumaApi, MonitorType, NotificationType

TAG_NAME = "cpanel-sync"
TAG_COLOR = "#2563EB"
CF_TAG_NAME = "Cloudflare"
CF_TAG_COLOR = "#F38020"
# Monitors paused BY THE SYNC get this tag, and only those are resumed by it.
# Monitors you pause by hand (no tag) are never resumed.
PAUSE_TAG_NAME = "auto-paused"
PAUSE_TAG_COLOR = "#6B7280"
INFRA_TAG_NAME = "infra"
INFRA_TAG_COLOR = "#DC2626"
# Domain type tags (main domains are not tagged)
TYPE_TAGS = {
    "addon": "#7C3AED",
    "sub": "#0D9488",
    "parked": "#CA8A04",
}
SITES_GROUP = "Websites"          # Websites / <server> / <cPanel account>
LEGACY_SITE_GROUP_PREFIX = "cPanel · "   # older versions: one flat top-level group per server
INFRA_GROUP = "Infrastructure"
CPANEL_SUBGROUP = "cPanel"
PROXMOX_SUBGROUP = "Proxmox"
DNS_SUBGROUP = "DNS"
OTHER_SUBGROUP = "Other"
LOCAL_GROUP = "Local uplink (Kuma)"
NTFY_SITES_NAME = "ntfy · sites"
NTFY_INFRA_NAME = "ntfy · infrastructure"
EXPIRY_PREFIX = "Domain expires:"
SSL_EXPIRY_PREFIX = "SSL expires:"
DATE_FMT = "%Y-%m-%d"

# Names used by earlier (Serbian) versions — existing objects are renamed, not duplicated
LEGACY_NAMES = {
    INFRA_GROUP: ["Infrastruktura", "Servers"],
    CPANEL_SUBGROUP: ["cPanel servers"],
    LOCAL_GROUP: ["Lokalna veza (Kuma)"],
    NTFY_SITES_NAME: ["ntfy · sajtovi"],
    NTFY_INFRA_NAME: ["ntfy · infrastruktura"],
    PAUSE_TAG_NAME: ["auto-pauza"],
}
# A description starting with one of these was written by the sync (safe to overwrite)
DNS_PROBLEM_TEXT = "DNS: domain does not resolve (NXDOMAIN) — expired domain or missing zone?"
SYNC_DESC_PREFIXES = (EXPIRY_PREFIX, SSL_EXPIRY_PREFIX, "SSL:", "DNS:",
                      "Domen ističe:", "SSL ističe:")

STATE_DIR = os.environ.get("STATE_DIR", "/app/state")

# Domains that are on a server but don't resolve (filled in every run). They are NOT
# paused — the site really is down for visitors, so Kuma keeps alerting.
NXDOMAINS = set()


# ------------------------------------------------------------- helpers ---

def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"[error] missing environment variable {name}")
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
        log(f"[warning] cannot save {name}: {e}")


# ---------------------------------------------------------------- ntfy ---

def ntfy_server():
    return env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")


def expiry_topic():
    """Domain/SSL expiry: its own topic, or the sites topic if not set."""
    return env("NTFY_TOPIC_EXPIRY", "") or env("NTFY_TOPIC_SITES", "")


def ntfy_send(topic, title, message, priority=3, tags=None):
    """Direct phone message (expiry alerts and errors of the sync itself)."""
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
        log(f"[warning] ntfy send failed: {e}")


# ---------------------------------------------------------------- WHM ---

def parse_servers():
    servers = []
    for item in env_list("WHM_SERVERS"):
        parts = item.split("|")
        if len(parts) != 3:
            sys.exit(f"[error] WHM_SERVERS entry must be name|url|token: {item!r}")
        name, url, token = (p.strip() for p in parts)
        servers.append({"name": name, "url": url.rstrip("/"), "token": token,
                        "host": urlparse(url).hostname})
    if not servers:
        sys.exit("[error] WHM_SERVERS is empty")
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


# Cloudflare IPv4 ranges (https://www.cloudflare.com/ips-v4) — a domain behind the CF proxy
# doesn't point to our IP, but is still hosted by us.
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
    """Returns (verdict, on_cloudflare). verdict: 'ok' | 'nxdomain' | 'elsewhere:<ip>'"""
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
    """Returns {domain: (cpanel_user, on_cloudflare, type)} for one WHM server, after filters."""
    types = set(env_list("DOMAIN_TYPES", "main,addon,parked,sub"))
    excludes = env_list("EXCLUDE", "")
    skip_suspended = env_bool("SKIP_SUSPENDED")

    accts = whm_call(server, "listaccts", want="user,suspended,ip").get("acct", [])
    suspended = {a["user"] for a in accts if int(a.get("suspended", 0))} if skip_suspended else set()

    domains = whm_call(server, "get_domain_info").get("domains", [])

    # Server IPs: all account IPs + ipv4 from domain_info + SERVER_IPS from .env
    server_ips = set(env_list("SERVER_IPS", ""))
    server_ips |= {a.get("ip") for a in accts if a.get("ip")}
    for d in domains:
        for key in ("ipv4", "ipv4_ssl"):
            if d.get(key):
                server_ips.add(d[key])

    # diagnostics: what WHM actually returns
    users_with_domains = {d.get("user") for d in domains}
    by_type = Counter(d.get("domain_type") for d in domains)
    log(f"{server['name']}: WHM returned {len(domains)} domains "
        f"({', '.join(f'{k}={v}' for k, v in sorted(by_type.items()))}) "
        f"for {len(users_with_domains)}/{len(accts)} accounts")
    missing_users = {a["user"] for a in accts} - users_with_domains
    if missing_users:
        log(f"{server['name']}: [warning] get_domain_info returned no domains for accounts: "
            f"{', '.join(sorted(missing_users))}")

    # Helper subdomains cPanel creates for every addon domain
    # (e.g. triaris.rs.lartgroupe.com) — same docroot as the addon, not a real site.
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
            # WHM returns docroot -> reliable, no guessing by name
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
            skipped["type"] += 1
            continue
        if user in suspended:
            skipped["suspended"] += 1
            continue
        if is_addon_helper(d):
            skipped["addon-helper-sub"] += 1
            continue
        if any(fnmatch.fnmatch(domain, pat) for pat in excludes):
            skipped["excluded"] += 1
            continue
        verdict, on_cf = dns_verdict(domain, server_ips)
        if verdict == "nxdomain" and env("DNS_NXDOMAIN", "monitor").lower() != "pause":
            # still ours, but broken DNS: keep monitoring so it shows as DOWN with a reason
            NXDOMAINS.add(domain)
            log(f"  DNS problem {domain} ({user}): does not resolve — keeping it monitored")
        elif verdict != "ok":
            skipped["dns"] += 1
            log(f"  skipping {domain} ({user}): DNS {verdict}")
            continue
        result[domain] = (user, on_cf, d.get("domain_type"))
    if skipped:
        log(f"{server['name']}: skipped " + ", ".join(f"{k}={v}" for k, v in skipped.items()))
    return result


# ------------------------------------------------------- domain expiry ---

_tld = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)  # works offline


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
            log(f"[warning] IANA RDAP list unavailable ({e}), using WHOIS only")
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
            log(f"[warning] whois.iana.org for .{tld}: {e}")
        _whois_servers[tld] = srv
    return _whois_servers[tld]


_DATE_FORMATS = (
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%b-%Y", "%Y.%m.%d", "%d/%m/%Y", "%Y/%m/%d",
)


def parse_date(text):
    text = text.strip()
    text = re.sub(r"\.\d+(?=Z|$|[+-])", "", text)                            # ...00.000Z
    text = re.sub(r"(?<=\d\d:\d\d:\d\d)[+-]\d\d:?\d\d$", "", text).strip()   # ...T10:00:00+02:00
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
    """Returns (datetime | None, source)."""
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
    return None, "none"


def check_expiry(reg_domains):
    """reg_domains: {registrable: [client label, ...]}. Returns {registrable: datetime|None}.
    Checks each domain at most once per EXPIRY_CHECK_HOURS and sends ntfy alerts."""
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
            time.sleep(1)  # be polite to WHOIS servers
            new_exp = exp.isoformat() if exp else None
            if new_exp != st.get("expires"):
                st["alerted"] = []  # renewed or seen for the first time
            st.update({"expires": new_exp, "source": src, "checked": time.time()})
            state[dom] = st
            if not exp:
                log(f"  expiry {dom}: unknown ({src})")
        exp = datetime.fromisoformat(st["expires"]) if st.get("expires") else None
        out[dom] = exp
        if not exp:
            continue
        left = (exp - now).days
        crossed = [d for d in days_list if left <= d and d not in st.get("alerted", [])]
        if crossed:
            who = ", ".join(reg_domains[dom])
            title = (f"Domain {dom} has EXPIRED" if left < 0
                     else f"Domain {dom} expires in {left} days")
            log(f"⚠ {title} ({exp:{DATE_FMT}}) — {who}")
            ntfy_send(topic, title, f"Expiry date: {exp:{DATE_FMT}}\nClient: {who}",
                      priority=5 if left <= 7 else 4, tags=["hourglass"])
            st["alerted"] = sorted(set(st.get("alerted", [])) | set(crossed))
            state[dom] = st

    save_state("expiry.json", state)
    known = sum(1 for v in out.values() if v)
    log(f"domain expiry: {known}/{len(out)} known (checked now: {checked})")
    return out


# ------------------------------------------------------------ SSL ---

def get_cert(domain, port=443):
    """Returns (notAfter datetime | None, error | None) for the certificate the site actually serves."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((domain, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                cert = tls.getpeercert()
        exp = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc)
        return exp, None
    except ssl.SSLCertVerificationError as e:
        return None, f"invalid certificate: {e.verify_message}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def check_ssl(domains):
    """domains: {domain: client label}. Returns {domain: (datetime|None, error|None)}.
    Alerts at SSL_EXPIRY_DAYS days left — with cPanel that means AutoSSL is failing to renew."""
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
                st["alerted"] = []  # renewed
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
            title = (f"SSL for {dom} has EXPIRED" if left < 0
                     else f"SSL for {dom} expires in {left} days")
            log(f"⚠ {title} ({exp:{DATE_FMT}}) — {who}")
            ntfy_send(expiry_topic(), title,
                      f"Expiry date: {exp:{DATE_FMT}}\nClient: {who}\n"
                      f"AutoSSL is probably failing to renew — WHM > Manage AutoSSL > Logs",
                      priority=5 if left <= 3 else 4, tags=["lock"])
            st["alerted"] = sorted(set(st.get("alerted", [])) | set(crossed))
            state[dom] = st

    save_state("ssl.json", state)
    ok = sum(1 for e, _ in out.values() if e)
    log(f"SSL: {ok}/{len(out)} certificates read (checked now: {len(todo)})")
    return out


# --------------------------------------------------------------- Kuma ---

def host_of(url):
    try:
        return (urlparse(url).hostname or "").lower().strip(".")
    except Exception:
        return ""


class ReadyKumaApi(UptimeKumaApi):
    """UptimeKumaApi that waits until Kuma is ready to accept a login.

    Kuma 2.x registers its "login" handler only at the end of processing a new
    connection (then it emits "loginRequired"). A login sent earlier is silently
    ignored and the client hangs until timeout. So we wait for "loginRequired"/"autoLogin".
    """

    def connect(self):
        self._kuma_ready = threading.Event()
        self.sio.on("loginRequired", lambda *a: self._kuma_ready.set())
        # autoLogin = auth disabled in Kuma; still means "ready"
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
        log(f"Kuma: connection not ready after {ready_wait}s (attempt {attempt}/5), retrying...")
        api.disconnect()
        time.sleep(3)
    raise RuntimeError("Kuma never sent loginRequired — check the uptime-kuma log")


def get_tag_id(api, dry, name=TAG_NAME, color=TAG_COLOR):
    tags = api.get_tags()
    for t in tags:
        if t["name"] == name:
            return t["id"]
    for t in tags:  # rename a tag left over from an older version
        if t["name"] in LEGACY_NAMES.get(name, []):
            log(f"~ tag {t['name']} -> {name}")
            if not dry:
                api.edit_tag(t["id"], name=name)
            return t["id"]
    if dry:
        return None
    return api.add_tag(name=name, color=color)["id"]


def find_tag(monitor, name):
    names = {name, *LEGACY_NAMES.get(name, [])}
    for t in monitor.get("tags") or []:
        if t.get("name") in names:
            return t
    return None


def managed_tag(monitor):
    return find_tag(monitor, TAG_NAME)


def sync_pause(api, m, pause_tag_id, dry):
    """Pause a monitor and mark that the sync paused it."""
    if dry:
        return
    if not find_tag(m, PAUSE_TAG_NAME):
        api.add_monitor_tag(tag_id=pause_tag_id, monitor_id=m["id"], value="")
    api.pause_monitor(m["id"])


def sync_resume(api, m, label, stats, dry):
    """Resume ONLY if the sync paused the monitor. Returns True if resumed."""
    t = find_tag(m, PAUSE_TAG_NAME)
    if m.get("active", True):
        # active — if a leftover auto-paused tag exists (e.g. you resumed it by hand), drop it
        if t and not dry:
            api.delete_monitor_tag(tag_id=t["tag_id"], monitor_id=m["id"], value=t.get("value", ""))
        return False
    if not t:
        stats["paused by hand"] += 1  # you paused it — leave it alone
        return False
    log(f"▶ {label}  back in scope, resuming")
    stats["resumed"] += 1
    if not dry:
        api.resume_monitor(m["id"])
        api.delete_monitor_tag(tag_id=t["tag_id"], monitor_id=m["id"], value=t.get("value", ""))
    return True


def ensure_group(api, path, monitors, dry):
    """Make sure a (nested) group exists. path: "Name" or ("Parent", "Child", ...).
    Groups are matched by name AND parent, so "cp1" under cPanel servers and under
    another branch are different groups. Returns the id of the last group."""
    if isinstance(path, str):
        path = (path,)
    parent = None
    for name in path:
        gid = None
        for m in monitors:
            if m["type"] == MonitorType.GROUP and m["name"] == name and (m.get("parent") or None) == parent:
                gid = m["id"]
                break
        if gid is None:
            for m in monitors:  # rename a group left over from an older version (same parent)
                if m["type"] == MonitorType.GROUP and (m.get("parent") or None) == parent \
                        and m["name"] in LEGACY_NAMES.get(name, []):
                    log(f"~ group {m['name']} -> {name}")
                    if not dry:
                        api.edit_monitor(m["id"], name=name)
                    m["name"] = name
                    gid = m["id"]
                    break
        if gid is None:
            log(f"+ group  {' / '.join(path[:path.index(name) + 1])}")
            if not dry:
                gid = api.add_monitor(type=MonitorType.GROUP, name=name, parent=parent)["monitorID"]
                monitors.append({"id": gid, "type": MonitorType.GROUP, "name": name, "parent": parent})
        parent = gid
    return parent


def cleanup_empty_groups(api, dry):
    """Delete groups the sync owns that ended up empty (client left a server, old layout).
    Only touches groups below Websites/ and Infrastructure/ and old top-level "cPanel · x" groups."""
    for _ in range(5):  # bottom-up, a few levels
        ms = api.get_monitors()
        by_id = {m["id"]: m for m in ms}
        children = Counter(m.get("parent") for m in ms if m.get("parent"))

        def root_of(m):
            while m.get("parent") and m["parent"] in by_id:
                m = by_id[m["parent"]]
            return m

        empty = []
        for m in ms:
            if m["type"] != MonitorType.GROUP or children.get(m["id"]):
                continue
            top = root_of(m)
            owned_tree = top is not m and top["name"] in (SITES_GROUP, INFRA_GROUP)
            legacy_flat = not m.get("parent") and m["name"].startswith(LEGACY_SITE_GROUP_PREFIX)
            if owned_tree or legacy_flat:
                empty.append(m)
        if not empty:
            return
        for m in empty:
            log(f"- empty group  {m['name']}")
            if not dry:
                api.delete_monitor(m["id"])
        if dry:
            return


# --------------------------------------------------------- status pages ---

STATUS_LABELS = {
    "en": {"web": "Web hosting", "email": "Email", "dns": "DNS", "sites": "Websites",
           "services": "Services", "uplink": "Monitoring uplink"},
    "sr": {"web": "Web hosting", "email": "Email", "dns": "DNS", "sites": "Sajtovi",
           "services": "Usluge", "uplink": "Monitoring veza"},
}
MAIL_PORTS = {25, 110, 143, 465, 587, 993, 995}


def slugify(text):
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")[:60] or "page"


def status_label(key):
    lang = env("STATUS_LANG", "en").lower()
    return STATUS_LABELS.get(lang, STATUS_LABELS["en"])[key]


def ensure_status_page(api, slug, title, sections, dry, description="", domains=None, stats=None):
    """Create/update a status page. sections: [(name, [monitor ids])]. Empty sections are dropped.
    Saves only when something changed. Tags are never shown (they contain client usernames)."""
    sections = [(n, ids) for n, ids in sections if ids]
    want_groups = [(n, list(ids)) for n, ids in sections]
    existing = {p["slug"]: p for p in api.get_status_pages()}
    if slug not in existing:
        log(f"+ status page  /status/{slug}  ({title})")
        if stats is not None:
            stats["status pages"] += 1
        if dry:
            return
        api.add_status_page(slug, title)
    else:
        cur = api.get_status_page(slug)
        cur_groups = [(g.get("name"), [m["id"] for m in g.get("monitorList") or []])
                      for g in cur.get("publicGroupList") or []]
        same = (cur_groups == want_groups and cur.get("title") == title
                and (cur.get("description") or "") == (description or "")
                and sorted(cur.get("domainNameList") or []) == sorted(domains or [])
                and not cur.get("showTags"))
        if same:
            return
        log(f"~ status page  /status/{slug}")
        if stats is not None:
            stats["status pages"] += 1
        if dry:
            return
    kw = dict(
        title=title,
        description=description or None,
        showTags=False,
        showPoweredBy=False,
        showCertificateExpiry=False,
        publicGroupList=[{"name": n, "weight": i + 1, "monitorList": [{"id": x} for x in ids]}
                         for i, (n, ids) in enumerate(want_groups)],
    )
    if domains:
        kw["domainNameList"] = domains
    if env("STATUS_FOOTER", ""):
        kw["footerText"] = env("STATUS_FOOTER")
    api.save_status_page(slug, **kw)


def sync_status_pages(api, infra, dry, stats):
    """Public page (services), internal page (everything) and optional per-client pages."""
    if not env_bool("STATUS_PAGES", "true"):
        return
    ms = api.get_monitors()
    by_id = {m["id"]: m for m in ms}
    infra_ids, sites = {}, []      # key -> id ; [(server, user, name, id)]
    for m in ms:
        t = managed_tag(m)
        if not t or m["type"] == MonitorType.GROUP:
            continue
        v = str(t.get("value", ""))
        if v.startswith("infra:"):
            infra_ids[v[6:]] = m["id"]
        else:
            srv, _, user = v.partition("/")
            sites.append((srv, user, m["name"], m["id"]))
    sites.sort()
    infra_by_key = {i["key"]: i for i in infra}

    # --- public: services only, no client sites, no nodes/IPs ---
    slug = env("STATUS_PUBLIC_SLUG", "")
    if slug:
        web, email, dns = [], [], []
        for key, item in infra_by_key.items():
            mid = infra_ids.get(key)
            g = item["group"]
            if not mid or len(g) < 2:
                continue
            if g[1] == CPANEL_SUBGROUP:
                if item.get("port") in MAIL_PORTS:
                    email.append(mid)
                elif key.endswith((" · cPanel", " · Webmail")) or item.get("port") == 21:
                    web.append(mid)
            elif g[1] == DNS_SUBGROUP:
                dns.append(mid)
        sections = [(status_label("web"), sorted(web)), (status_label("email"), sorted(email)),
                    (status_label("dns"), sorted(dns))]
        # STATUS_PUBLIC_EXTRA="Monitor name|Section;..."  (any monitor in Kuma, e.g. WHMCS, Pterodactyl)
        extra = {}
        names = {m["name"]: m["id"] for m in ms if m["type"] != MonitorType.GROUP}
        for entry in env_list("STATUS_PUBLIC_EXTRA", "", sep=";"):
            name, _, section = (x.strip() for x in entry.partition("|"))
            if name not in names:
                log(f"[warning] STATUS_PUBLIC_EXTRA: no monitor named {name!r}")
                continue
            extra.setdefault(section or status_label("services"), []).append(names[name])
        sections += list(extra.items())
        ensure_status_page(api, slugify(slug), env("STATUS_PUBLIC_TITLE", "Status"), sections, dry,
                           description=env("STATUS_PUBLIC_DESCRIPTION", ""),
                           domains=env_list("STATUS_PUBLIC_DOMAINS", ""), stats=stats)

    # --- internal infrastructure: every infra group (keep it behind NetBird/Access!) ---
    slug = env("STATUS_INTERNAL_SLUG", "")
    if slug:
        sections = {}
        for key, item in sorted(infra_by_key.items()):
            mid = infra_ids.get(key)
            if mid:
                g = item["group"]
                name = " / ".join(g[1:]) if len(g) > 1 else status_label("uplink")
                sections.setdefault(name, []).append(mid)
        ensure_status_page(api, slugify(slug), env("STATUS_INTERNAL_TITLE", "Internal status"),
                           sorted(sections.items()), dry, stats=stats)

    # --- internal client websites: one section per client (cPanel account) ---
    slug = env("STATUS_SITES_SLUG", "")
    if slug:
        sections = {}
        for srv, user, _, mid in sites:
            sections.setdefault(f"{user} ({srv})", []).append(mid)
        ensure_status_page(api, slugify(slug), env("STATUS_SITES_TITLE", "Clients Websites"),
                           sorted(sections.items()), dry, stats=stats)

    # --- per client (cPanel account): STATUS_CLIENT_PAGES=user1,user2 | all ---
    wanted = env_list("STATUS_CLIENT_PAGES", "")
    if wanted:
        users = sorted({u for _, u, _, _ in sites})
        if [w.lower() for w in wanted] != ["all"]:
            users = [u for u in users if u in wanted]
        tpl = env("STATUS_CLIENT_TITLE", "{user}")
        for user in users:
            ids = [mid for _, u, _, mid in sites if u == user and by_id[mid].get("active", True)]
            ensure_status_page(api, slugify(env("STATUS_CLIENT_SLUG_PREFIX", "") + user),
                               tpl.format(user=user), [(status_label("sites"), ids)], dry,
                               description=env("STATUS_PUBLIC_DESCRIPTION", ""), stats=stats)


def ensure_ntfy(api, name, topic, priority, dry):
    """Create/update an ntfy notification in Kuma. Returns its id or None."""
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
    names = [name, *LEGACY_NAMES.get(name, [])]
    for n in api.get_notifications():
        if n.get("name") in names:
            if n.get("name") != name or any(n.get(k) != v for k, v in want.items()):
                log(f"~ notification {n.get('name')} -> {name}")
                if not dry:
                    api.edit_notification(n["id"], name=name, isDefault=False, **want)
            return n["id"]
    log(f"+ notification {name} (topic {topic})")
    if dry:
        return None
    return api.add_notification(name=name, isDefault=False, applyExisting=False, **want)["id"]


def extra_notification_ids(api):
    ids = [int(x) for x in env_list("NOTIFICATION_IDS", "")]
    if ids:
        return ids
    ours = {NTFY_SITES_NAME, NTFY_INFRA_NAME,
            *LEGACY_NAMES[NTFY_SITES_NAME], *LEGACY_NAMES[NTFY_INFRA_NAME]}
    return [n["id"] for n in api.get_notifications()
            if n.get("isDefault") and n.get("name") not in ours]


def notif_set(m):
    v = m.get("notificationIDList") or []
    if isinstance(v, dict):
        return {int(k) for k, on in v.items() if on}
    return {int(x) for x in v}


def monitor_changes(m, want):
    """Differences between an existing monitor and the desired settings."""
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


# ----------------------------------------------------- infrastructure ---

PORT_NAMES = {21: "FTP", 22: "SSH", 25: "SMTP 25", 465: "SMTP 465", 587: "SMTP 587",
              110: "POP3", 995: "POP3S", 143: "IMAP", 993: "IMAPS", 3306: "MySQL",
              8007: "PBS", 2087: "WHM", 2083: "cPanel", 2096: "Webmail"}


def per_host_list(prefix, name, default):
    """PREFIX_<NAME> overrides the generic setting for one server, e.g. INFRA_PORTS_CP2=465,993."""
    key = prefix + "_" + re.sub(r"[^A-Z0-9]", "_", name.upper())
    return env_list(key, env(prefix, default))


def build_infra(servers):
    """List of infrastructure monitors: {key, group (path tuple), type, ...kuma fields}."""
    items = []
    interval = env_int("INFRA_INTERVAL", 60)

    def add(key, group, **kw):
        kw.setdefault("interval", interval)
        items.append({"key": key, "group": tuple(group), **kw})

    def add_port(key, group, host, port):
        add(key, group, type=MonitorType.PORT, hostname=host, port=port)

    # cPanel servers (from WHM_SERVERS): Infrastructure / cPanel / <server>
    if env_bool("INFRA_AUTO", "true"):
        for s in servers:
            h, n = s["host"], s["name"]
            g = (INFRA_GROUP, CPANEL_SUBGROUP, n)
            add(f"{n} · ping", g, type=MonitorType.PING, hostname=h)
            for label, port in (("WHM", 2087), ("cPanel", 2083), ("Webmail", 2096)):
                add(f"{n} · {label}", g, type=MonitorType.HTTP,
                    url=f"https://{h}:{port}", accepted_statuscodes=["200-299", "300-399"])
            for p in per_host_list("INFRA_PORTS", n, env("INFRA_AUTO_PORTS", "465,587,993,21")):
                p = int(p)
                add_port(f"{n} · {PORT_NAMES.get(p, f'port {p}')}", g, h, p)

    # Proxmox nodes: PROXMOX_NODES="Node102|1.2.3.4,Node103|5.6.7.8"
    #   Infrastructure / Proxmox / <node>: ping + Proxmox UI (8006) + PROXMOX_PORTS
    for entry in env_list("PROXMOX_NODES", ""):
        name, _, host = (x.strip() for x in entry.partition("|"))
        if not host:
            log(f"[warning] PROXMOX_NODES entry must be name|host: {entry!r}")
            continue
        g = (INFRA_GROUP, PROXMOX_SUBGROUP, name)
        add(f"{name} · ping", g, type=MonitorType.PING, hostname=host)
        add(f"{name} · Proxmox UI", g, type=MonitorType.HTTP, url=f"https://{host}:8006",
            ignoreTls=True, accepted_statuscodes=["200-299", "300-399"])
        for p in per_host_list("PROXMOX_PORTS", name, ""):
            p = int(p)
            add_port(f"{name} · {PORT_NAMES.get(p, f'port {p}')}", g, host, p)

    # Nameservers: Infrastructure / DNS
    test_domain = env("DNS_TEST_DOMAIN", "")
    for ns in env_list("NAMESERVERS", ""):
        ips = sorted(resolve_a(ns))
        if not ips or not test_domain:
            log(f"[warning] nameserver {ns}: no IP or DNS_TEST_DOMAIN not set")
            continue
        add(f"DNS · {ns}", (INFRA_GROUP, DNS_SUBGROUP), type=MonitorType.DNS, hostname=test_domain,
            dns_resolve_server=ips[0], port=53, dns_resolve_type="A")

    # INFRA_EXTRA="name|type|target[|group/subgroup];..."
    #   type: http | http-insecure | port (host:port) | ping | dns (domain@resolver-ip)
    #   group (optional): path under Infrastructure, e.g. "Game" or "Game/Wings"; default "Other"
    for item in env_list("INFRA_EXTRA", "", sep=";"):
        parts = [p.strip() for p in item.split("|")]
        if len(parts) not in (3, 4):
            log(f"[warning] INFRA_EXTRA entry must be name|type|target[|group]: {item!r}")
            continue
        name, typ, target = parts[:3]
        sub = [x.strip() for x in (parts[3] if len(parts) == 4 else OTHER_SUBGROUP).split("/") if x.strip()]
        g = (INFRA_GROUP, *sub)
        typ = typ.lower()
        if typ in ("http", "http-insecure"):
            add(name, g, type=MonitorType.HTTP, url=target,
                ignoreTls=(typ == "http-insecure"), accepted_statuscodes=["200-299", "300-399"])
        elif typ == "port":
            h, _, p = target.rpartition(":")
            add_port(name, g, h, int(p))
        elif typ == "ping":
            add(name, g, type=MonitorType.PING, hostname=target)
        elif typ == "dns":
            d, _, resolver = target.partition("@")
            add(name, g, type=MonitorType.DNS, hostname=d,
                dns_resolve_server=resolver or "1.1.1.1", port=53, dns_resolve_type="A")
        else:
            log(f"[warning] INFRA_EXTRA unknown type {typ!r} for {name}")

    # own uplink: if these are down, site outages are probably false alarms
    for ip in env_list("INTERNET_CHECK", "1.1.1.1,8.8.8.8"):
        add(f"Internet · {ip}", (LOCAL_GROUP,), type=MonitorType.PING, hostname=ip)
    return items


# ---------------------------------------------------------------- sync ---

def sync(dry=False):
    servers = parse_servers()
    NXDOMAINS.clear()

    # 1) domains from all servers
    desired = {}  # domain -> (server, user, on_cloudflare, type)
    for s in servers:
        try:
            doms = fetch_domains(s)
        except Exception as e:
            # If a server doesn't respond, DON'T touch its monitors (don't pause everything by mistake)
            log(f"[error] {s['name']}: {e} — skipping this server in this run")
            s["failed"] = True
            continue
        n_cf = sum(1 for _, cf, _ in doms.values() if cf)
        by_t = Counter(t for _, _, t in doms.values())
        log(f"{s['name']}: {len(doms)} domains to monitor "
            f"({', '.join(f'{k}={v}' for k, v in sorted(by_t.items()))}; {n_cf} on Cloudflare)")
        for dom, (user, cf, dtype) in doms.items():
            desired.setdefault(dom, (s["name"], user, cf, dtype))
    failed = {s["name"] for s in servers if s.get("failed")}

    # 2) domain expiry (registrable domains only: main/addon, not subdomains)
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
            log(f"[warning] domain expiry check failed: {e}")

    # 2b) SSL certificates (checked by the sync so alerts go to the expiry topic)
    ssl_check = env_bool("SSL_CHECK", "true")
    ssl_info = {}
    if ssl_check:
        try:
            ssl_info = check_ssl({d: f"{u} ({s})" for d, (s, u, _, _) in desired.items()})
        except Exception as e:
            log(f"[warning] SSL check failed: {e}")

    on_removed = env("ON_REMOVED", "pause").lower()
    keyword = env("CONTENT_ERROR_KEYWORD", "Fatal error")
    site_base = {
        "interval": env_int("MONITOR_INTERVAL", 300),
        "retryInterval": env_int("RETRY_INTERVAL", 60),
        "maxretries": env_int("MONITOR_RETRIES", 3),
        # Kuma's own SSL alert uses the same notifications as outages — off when the sync checks SSL
        "expiryNotification": not ssl_check,
    }
    if keyword:
        site_base.update(type=MonitorType.KEYWORD, keyword=keyword, invertKeyword=True)
    else:
        site_base.update(type=MonitorType.HTTP)
    enforce = env_bool("ENFORCE_SETTINGS", "true")

    with connect_kuma() as api:
        log("Kuma: logging in...")
        api.login(env("KUMA_USER", required=True), env("KUMA_PASS", required=True))
        log("Kuma: logged in, reading tags/notifications/monitors...")

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
        log(f"Kuma: {len(monitors)} existing monitors")

        site_groups = {}  # (server, user) -> group id

        def site_group(srv, user):
            if (srv, user) not in site_groups:
                site_groups[(srv, user)] = ensure_group(api, (SITES_GROUP, srv, user), monitors, dry)
            return site_groups[(srv, user)]

        managed = {}        # host -> monitor (sites)
        infra_managed = {}  # key -> monitor (infrastructure)
        unmanaged = set()   # hosts already covered by a hand-made monitor
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

        # 3) sites: add new / update existing
        for dom in sorted(desired):
            srv, user, on_cf, dtype = desired[dom]
            tag_value = f"{srv}/{user}"
            parent = site_group(srv, user)
            want = dict(site_base)
            now = datetime.now(timezone.utc)
            parts = [DNS_PROBLEM_TEXT] if dom in NXDOMAINS else []
            exp = expiry.get(dom)
            if exp:
                parts.append(f"{EXPIRY_PREFIX} {exp:{DATE_FMT}} ({(exp - now).days} d)")
            sexp, serr = ssl_info.get(dom, (None, None))
            if sexp:
                parts.append(f"{SSL_EXPIRY_PREFIX} {sexp:{DATE_FMT}} ({(sexp - now).days} d)")
            elif serr and ssl_check and dom not in NXDOMAINS:
                parts.append(f"SSL: {serr[:80]}")
            if parts:
                want["description"] = " · ".join(parts)

            if dom in unmanaged and dom not in managed:
                continue  # a hand-made monitor already exists, don't duplicate

            m = managed.get(dom)
            if m is None:
                log(f"+ {dom}  ({tag_value}) [{dtype}]{'  [Cloudflare]' if on_cf else ''}")
                stats["added"] += 1
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

            # exists — group, server tag, settings, tags, pause state
            old_tag = managed_tag(m)
            if old_tag.get("value") != tag_value:
                log(f"~ {dom}  {old_tag.get('value')} -> {tag_value}")
                stats["moved"] += 1
                if not dry:
                    api.delete_monitor_tag(tag_id=tag_id, monitor_id=m["id"], value=old_tag.get("value", ""))
                    api.add_monitor_tag(tag_id=tag_id, monitor_id=m["id"], value=tag_value)
            if m.get("parent") != parent:
                stats["regrouped"] += 1
                if not dry:
                    api.edit_monitor(m["id"], parent=parent)

            if enforce:
                # only touch the description if it's empty or was written by the sync
                if "description" in want and (m.get("description") or "").strip() and \
                        not (m.get("description") or "").startswith(SYNC_DESC_PREFIXES):
                    want.pop("description")
                ch = monitor_changes(m, want)
                if ch:
                    only_desc = set(ch) == {"description"}
                    if not only_desc:
                        log(f"⚙ {dom}  settings: {', '.join(sorted(ch))}")
                    stats["description" if only_desc else "updated"] += 1
                    if not dry:
                        api.edit_monitor(m["id"], **ch)

            has_cf = find_tag(m, CF_TAG_NAME)
            if on_cf and not has_cf:
                log(f"☁ {dom}  now behind Cloudflare, adding tag")
                stats["cloudflare"] += 1
                if not dry:
                    api.add_monitor_tag(tag_id=cf_tag_id, monitor_id=m["id"], value="")
            elif not on_cf and has_cf:
                log(f"☁ {dom}  no longer behind Cloudflare, removing tag")
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
                    log(f"# {dom}  removing tag {t}")
                    if not dry:
                        api.delete_monitor_tag(tag_id=tid, monitor_id=m["id"], value=has_t.get("value", ""))
            sync_resume(api, m, dom, stats, dry)

        # 4) sites that are gone
        for dom, m in managed.items():
            if dom in desired:
                continue
            srv, _, user = (managed_tag(m).get("value") or "").partition("/")
            # keep out-of-scope monitors in their client group too (so old groups can be cleaned up)
            if srv and user and on_removed != "delete":
                gid = site_group(srv, user)
                if gid and m.get("parent") != gid:
                    stats["regrouped"] += 1
                    if not dry:
                        api.edit_monitor(m["id"], parent=gid)
            if srv in failed:
                continue  # server didn't respond, conclude nothing
            if on_removed == "delete":
                log(f"- {dom}  deleted")
            elif on_removed == "pause" and m.get("active", True):
                log(f"⏸ {dom}  out of scope (not on the server or DNS doesn't point to us), pausing")
            else:
                continue
            stats["removed"] += 1
            if dry:
                continue
            if on_removed == "delete":
                api.delete_monitor(m["id"])
            else:
                sync_pause(api, m, pause_tag_id, dry)

        # 5) infrastructure
        infra = build_infra(servers)
        infra_groups = {g: ensure_group(api, g, monitors, dry)
                        for g in sorted({i["group"] for i in infra})}
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
        infra_removed = env("INFRA_ON_REMOVED", "pause").lower()
        for key, m in infra_managed.items():
            if key in infra_keys:
                continue
            if infra_removed == "delete":
                log(f"- infra  {key}  (no longer in config, deleting)")
                stats["infra-"] += 1
                if not dry:
                    api.delete_monitor(m["id"])
            elif m.get("active", True):
                log(f"⏸ infra  {key}  (no longer in config)")
                stats["infra-"] += 1
                sync_pause(api, m, pause_tag_id, dry)

        cleanup_empty_groups(api, dry)
        try:
            sync_status_pages(api, infra, dry, stats)
        except Exception as e:
            log(f"[warning] status pages: {type(e).__name__}: {e}")

    summary = ", ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "no changes"
    log(f"done{' (DRY RUN)' if dry else ''}: {summary}")


def main():
    ap = argparse.ArgumentParser(description="cPanel -> Uptime Kuma sync")
    ap.add_argument("--dry-run", action="store_true", help="only show what would change")
    ap.add_argument("--loop", action="store_true", help="repeat every SYNC_INTERVAL seconds")
    ap.add_argument("--expiry", metavar="DOMAIN", help="test: when does a domain expire")
    ap.add_argument("--ssl", metavar="DOMAIN", help="test: when does an SSL certificate expire")
    ap.add_argument("--test-ntfy", action="store_true", help="test: send a test message to the phone")
    args = ap.parse_args()

    if args.ssl:
        exp, err = get_cert(args.ssl.lower())
        print(f"{args.ssl}: SSL expires {exp:{DATE_FMT}}" if exp else f"{args.ssl}: {err}")
        return
    if args.expiry:
        exp, src = lookup_expiry(args.expiry.lower())
        print(f"{args.expiry}: {exp:{DATE_FMT}} (source: {src})" if exp
              else f"{args.expiry}: date not found (source: {src})")
        return
    if args.test_ntfy:
        for var in ("NTFY_TOPIC_SITES", "NTFY_TOPIC_INFRA", "NTFY_TOPIC_EXPIRY"):
            ntfy_send(env(var, ""), "Uptime Kuma test", f"Test message ({var})", 3, ["test_tube"])
            print(f"sent to {var}={env(var, '') or '(empty)'}")
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
            # e.g. Kuma setup not finished yet or wrong user/pass — retry soon
            fails += 1
            log(f"[error] sync failed: {type(e).__name__}: {e} — retrying in 2 min")
            traceback.print_exc()
            if fails == 3:
                ntfy_send(env("NTFY_TOPIC_INFRA", ""), "kuma-sync is failing",
                          f"3 failed attempts in a row.\n{type(e).__name__}: {e}", 4, ["warning"])
            wait = 120
        time.sleep(wait)


if __name__ == "__main__":
    main()
