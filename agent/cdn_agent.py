#!/usr/bin/env python3
"""CDN Edge Node Agent — polls Manager, applies Nginx config with backup/rollback.

Design goals:
  * stdlib only (urllib) — avoids pip / PEP668 / distutils problems on nodes.
  * correct environment detection: Baota vs system Nginx, so we never write
    config to the wrong tree, always test/reload the *right* nginx binary,
    and never fight Baota's own service management.
"""

import os
import sys
import re
import json
import time
import shutil
import hashlib
import subprocess
import ssl
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta

CONF_DIR = "/opt/cdn-agent"
CONFIG_FILE = f"{CONF_DIR}/agent.json"
STATE_FILE = f"{CONF_DIR}/state.json"
BACKUP_DIR = f"{CONF_DIR}/backup"
POLL = 60

# Disk cleanup: trigger above threshold, best-effort target below it.
DISK_CLEAN_THRESHOLD = 90
DISK_CLEAN_TARGET = 80

# Baota layout
BAOTA_PANEL = "/www/server/panel"
BAOTA_ROOT = "/www/server/nginx"
BAOTA_NGINX_BIN = f"{BAOTA_ROOT}/sbin/nginx"
BAOTA_NGINX_CONF = f"{BAOTA_ROOT}/conf/nginx.conf"
BAOTA_SITE_DIR = f"{BAOTA_PANEL}/vhost/nginx"
BAOTA_SSL_DIR = f"{BAOTA_ROOT}/conf/ssl"
BAOTA_CACHE_DIR = f"{BAOTA_ROOT}/cdn_cache"
BAOTA_LOG_DIR = f"{BAOTA_ROOT}/logs"
BAOTA_CACHE_INCLUDE = f"{BAOTA_SITE_DIR}/00-cdn-cache.conf"

# Resolved environment (populated by resolve_env at startup)
ENV = {}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── fs / json helpers ─────────────────────────────────────────────

def _exists(p):
    return bool(p) and os.path.exists(p)


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def sha256(s):
    return hashlib.sha256(s.encode()).hexdigest()


# ── environment detection ─────────────────────────────────────────

def find_system_nginx_bin():
    for p in ("/usr/sbin/nginx", "/usr/local/nginx/sbin/nginx", "/usr/bin/nginx"):
        if _exists(p):
            return p
    return shutil.which("nginx") or "/usr/sbin/nginx"


def detect_is_baota():
    """Real detection: Baota nginx binary / config present, not just the panel dir."""
    return _exists(BAOTA_NGINX_BIN) or _exists(BAOTA_NGINX_CONF)


def resolve_env(config):
    """Decide which Nginx tree we operate on. Trust the type registered on the
    Manager when it explicitly says baota; otherwise fall back to detection."""
    global ENV
    forced = (config or {}).get("node_type", "")
    is_baota = detect_is_baota()
    if forced == "baota":
        is_baota = True
    elif forced == "auto" and not is_baota:
        is_baota = False

    if is_baota:
        ENV = {
            "is_baota": True,
            "detected_type": "baota",
            "nginx_bin": BAOTA_NGINX_BIN,
            "nginx_conf": BAOTA_NGINX_CONF,
            "site_dir": BAOTA_SITE_DIR,
            "ssl_dir": BAOTA_SSL_DIR,
            "cache_dir": BAOTA_CACHE_DIR,
            "log_dir": BAOTA_LOG_DIR,
            "traffic_log": f"{BAOTA_ROOT}/logs/cdn_access.log",
            "cache_include": BAOTA_CACHE_INCLUDE,
        }
    else:
        ENV = {
            "is_baota": False,
            "detected_type": "auto",
            "nginx_bin": find_system_nginx_bin(),
            "nginx_conf": "/etc/nginx/nginx.conf",
            "site_dir": "/etc/nginx/conf.d/cdn",
            "ssl_dir": "/etc/nginx/ssl",
            "cache_dir": "/var/cache/nginx/cdn_cache",
            "log_dir": "/var/log/nginx",
            "traffic_log": "/var/log/nginx/cdn_access.log",
            "cache_include": "",
        }
    log(f"Environment: {'Baota' if is_baota else 'system'} nginx "
        f"({ENV['nginx_bin']}), site_dir={ENV['site_dir']}")
    return ENV


# ── HTTP (stdlib only) ────────────────────────────────────────────

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return r.status, r.read().decode("utf-8", "replace")


def http_post_json(url, obj, headers=None, timeout=20):
    data = json.dumps(obj).encode()
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# ── Nginx control ─────────────────────────────────────────────────

def _baota_init():
    """Baota manages Nginx through its own init script; use the same script so
    we cooperate with the panel monitor instead of racing it."""
    for p in ("/etc/init.d/nginx",):
        if _exists(p):
            return p
    return None


def nginx_test():
    binp = ENV.get("nginx_bin", "")
    if not _exists(binp):
        log(f"nginx binary not found: {binp}")
        return False
    cmd = [binp, "-t"]
    if ENV.get("nginx_conf"):
        cmd += ["-c", ENV["nginx_conf"]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception as e:
        log(f"nginx -t error: {e}")
        return False
    out = (r.stderr or "") + (r.stdout or "")
    if r.returncode != 0:
        log("nginx -t FAILED:\n" + out[-600:])
        return False
    # Conflicting server_name (e.g. Baota manual site vs our cdn-*.conf) is a
    # warning, not an error, but silently drops one config — surface it.
    if "conflicting server name" in out or "conflicting server_name" in out:
        for line in out.splitlines():
            if "conflicting" in line:
                log(f"WARNING: {line.strip()}")
    return True


def nginx_reload():
    if ENV.get("is_baota"):
        init = _baota_init()
        if init:
            r = subprocess.run([init, "reload"], capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                return True
            log(f"init reload failed, fallback to binary: {r.stderr}")
        r = subprocess.run([ENV["nginx_bin"], "-c", ENV["nginx_conf"], "-s", "reload"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            log(f"nginx reload FAILED: {r.stderr}")
            return False
        return True
    r = subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        log(f"systemctl reload FAILED: {r.stderr}")
        return False
    return True


def nginx_start():
    if ENV.get("is_baota"):
        init = _baota_init()
        if init:
            r = subprocess.run([init, "start"], capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                return True
            log(f"init start failed, fallback to binary: {r.stderr}")
        r = subprocess.run([ENV["nginx_bin"], "-c", ENV["nginx_conf"]],
                           capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    r = subprocess.run(["systemctl", "start", "nginx"], capture_output=True, text=True, timeout=20)
    return r.returncode == 0


def nginx_running():
    if ENV.get("is_baota"):
        pidfile = os.path.join(BAOTA_ROOT, "logs", "nginx.pid")
        if _exists(pidfile):
            try:
                pid = int(open(pidfile).read().strip())
                os.kill(pid, 0)
                return True
            except Exception:
                pass
        try:
            r = subprocess.run(["pgrep", "-f", ENV["nginx_bin"]], capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", "nginx"], timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def ensure_nginx():
    """Self-heal: if nginx died and a config exists, bring it back."""
    if nginx_running():
        return True
    if not _exists(ENV.get("nginx_conf")):
        return False
    log("nginx not running, testing config & starting...")
    if nginx_test() and nginx_start():
        log("nginx restarted (self-heal)")
        return True
    log("nginx self-heal failed (config invalid or start error)")
    return False


def _strip_legacy_cache(text):
    """Remove any old cdn_cache zone definition we may have injected into
    Baota's nginx.conf, including half-line fragments and multi-line
    proxy_cache_path statements. Returns (new_text, removed)."""
    lines = text.splitlines()
    out = []
    removed = False
    i = 0
    while i < len(lines):
        ln = lines[i]
        is_zone = "keys_zone=cdn_cache" in ln
        is_path = ("proxy_cache_path" in ln and "cdn_cache" in ln) or \
                  ("proxy_temp_path" in ln and "cdn_cache" in ln)
        if is_zone or is_path:
            removed = True
            # If the statement is multi-line, swallow lines until it terminates.
            if ";" not in ln:
                i += 1
                while i < len(lines):
                    if ";" in lines[i]:
                        break
                    i += 1
            i += 1
            continue
        out.append(ln)
        i += 1
    new_text = "\n".join(out)
    if text.endswith("\n"):
        new_text += "\n"
    return new_text, removed


def ensure_cache_path():
    """Define the cdn_cache zone.
    System node: handled by the generated base config.
    Baota node: write a dedicated include file (http context via vhost glob) so
    we never edit Baota's own nginx.conf. Also cleans up the legacy injection
    older agent versions made into Baota's nginx.conf."""
    if not ENV.get("is_baota"):
        return False
    changed = False
    cache_dir = ENV["cache_dir"]
    include = ENV["cache_include"]
    try:
        os.makedirs(cache_dir, exist_ok=True)
        os.makedirs(os.path.join(cache_dir, "tmp"), exist_ok=True)
    except Exception:
        pass

    # Legacy cleanup: remove any old cdn_cache definition we injected into nginx.conf
    nginx_conf = ENV.get("nginx_conf")
    if _exists(nginx_conf):
        try:
            txt = open(nginx_conf).read()
            if "cdn_cache" in txt and ("keys_zone=cdn_cache" in txt or "proxy_cache_path" in txt):
                new_txt, removed = _strip_legacy_cache(txt)
                if removed and new_txt != txt:
                    with open(nginx_conf, "w") as f:
                        f.write(new_txt)
                    log("removed legacy cdn_cache definition from Baota nginx.conf")
                    changed = True
        except Exception:
            pass

    content = (
        "# CDN cache zone + traffic log format — managed by cdn-agent, do not edit\n"
        f"proxy_cache_path {cache_dir} levels=1:2 keys_zone=cdn_cache:10m "
        "max_size=1g inactive=7d use_temp_path=off;\n"
        "log_format cdn_traffic '$remote_addr - $remote_user [$time_local] "
        "\"$request\" $status $body_bytes_sent \"$http_referer\" \"$http_user_agent\" "
        "cache=$upstream_cache_status rt=$request_time uct=$upstream_connect_time "
        "uht=$upstream_header_time urt=$upstream_response_time "
        "host=$host bytes=$body_bytes_sent ts=$time_iso8601';\n"
    )
    # Rewrite whenever content differs — this also strips a stale
    # `proxy_temp_path` line (which duplicates Baota's own proxy.conf and makes
    # `nginx -t` fail) from an older include file we previously wrote.
    cur = ""
    if _exists(include):
        try:
            cur = open(include).read()
        except Exception:
            cur = ""
    if cur != content:
        try:
            os.makedirs(os.path.dirname(include), exist_ok=True)
            with open(include, "w") as f:
                f.write(content)
            log(f"cache zone written: {include}")
            changed = True
        except Exception as e:
            log(f"failed to write cache zone: {e}")
    return changed


# ── Backup / rollback ─────────────────────────────────────────────

def _managed_site_files(site_dir=None):
    """Our site conf files only (Baota: cdn-*.conf; system: dedicated dir)."""
    site_dir = site_dir or ENV["site_dir"]
    out = []
    if os.path.isdir(site_dir):
        for f in os.listdir(site_dir):
            p = os.path.join(site_dir, f)
            if not os.path.isfile(p) or not f.endswith(".conf"):
                continue
            if ENV.get("is_baota") and not f.startswith("cdn-"):
                continue
            out.append(p)
    return out


def _managed_cert_dirs(ssl_dir=None):
    """Our cert dirs only (system: whole dedicated dir; Baota: marked dirs)."""
    ssl_dir = ssl_dir or ENV["ssl_dir"]
    out = []
    if os.path.isdir(ssl_dir):
        for d in os.listdir(ssl_dir):
            p = os.path.join(ssl_dir, d)
            if not os.path.isdir(p):
                continue
            if ENV.get("is_baota") and not _exists(os.path.join(p, ".cdn-agent")):
                continue
            out.append(p)
    return out


def _backup_targets():
    targets = {"sites": ENV["site_dir"], "ssl": ENV["ssl_dir"]}
    if not ENV.get("is_baota"):
        targets["nginx_conf"] = ENV["nginx_conf"]
    return targets


def backup_current():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bdir = os.path.join(BACKUP_DIR, ts)
    os.makedirs(bdir, exist_ok=True)

    manifest = {}
    for key, src in _backup_targets().items():
        manifest[key] = src
        dst = os.path.join(bdir, key)
        try:
            if key == "nginx_conf":
                if _exists(src):
                    shutil.copy2(src, dst)
            elif key == "sites":
                os.makedirs(dst, exist_ok=True)
                for p in _managed_site_files(src):
                    shutil.copy2(p, os.path.join(dst, os.path.basename(p)))
            elif key == "ssl":
                os.makedirs(dst, exist_ok=True)
                for p in _managed_cert_dirs(src):
                    shutil.copytree(p, os.path.join(dst, os.path.basename(p)), dirs_exist_ok=True)
        except Exception as e:
            log(f"backup {key} failed: {e}")
    save_json(os.path.join(bdir, "manifest.json"), manifest)
    log(f"Backup saved: {bdir}")

    backups = sorted(os.listdir(BACKUP_DIR), reverse=True)
    for old in backups[5:]:
        shutil.rmtree(os.path.join(BACKUP_DIR, old), ignore_errors=True)
    return bdir


def rollback(backup_path):
    log(f"Rolling back to: {backup_path}")
    manifest = load_json(os.path.join(backup_path, "manifest.json"), {})
    for key, src in manifest.items():
        bsrc = os.path.join(backup_path, key)
        try:
            if key == "nginx_conf":
                if _exists(bsrc):
                    shutil.copy2(bsrc, src)
            elif key == "sites":
                for p in _managed_site_files(src):
                    os.remove(p)
                if _exists(bsrc):
                    os.makedirs(src, exist_ok=True)
                    for f in os.listdir(bsrc):
                        shutil.copy2(os.path.join(bsrc, f), os.path.join(src, f))
            elif key == "ssl":
                for p in _managed_cert_dirs(src):
                    shutil.rmtree(p, ignore_errors=True)
                if _exists(bsrc):
                    os.makedirs(src, exist_ok=True)
                    for d in os.listdir(bsrc):
                        shutil.copytree(os.path.join(bsrc, d), os.path.join(src, d), dirs_exist_ok=True)
        except Exception as e:
            log(f"rollback {key} failed: {e}")
    nginx_reload()


# ── Certificates: full re-upload ──────────────────────────────────

def _sync_certs(certs):
    """Certificates are all-or-nothing, so on any change we delete every
    agent-managed cert dir and re-upload them fresh (no incremental merge).

    Only directories we created (marked with `.cdn-agent`) are ever removed,
    so Baota's own certificates are never touched."""
    ssl_dir = ENV["ssl_dir"]
    os.makedirs(ssl_dir, exist_ok=True)

    desired = {}
    for cert in certs:
        domain = cert.get("domain", "")
        if domain:
            desired[domain] = (cert.get("fullchain_pem", ""), cert.get("privkey_pem", ""))

    current = {}
    for p in _managed_cert_dirs(ssl_dir):
        d = os.path.basename(p)
        try:
            fc = open(os.path.join(p, "fullchain.pem")).read()
            kp = open(os.path.join(p, "privkey.pem")).read()
            current[d] = (fc, kp)
        except Exception:
            current[d] = None  # broken/incomplete -> force rewrite

    if current == desired:
        return False

    # delete-all (only our managed dirs), then re-upload desired fresh
    for d in list(current.keys()):
        shutil.rmtree(os.path.join(ssl_dir, d), ignore_errors=True)
    for domain, (fc, kp) in desired.items():
        d = os.path.join(ssl_dir, domain)
        os.makedirs(d, exist_ok=True)
        try:
            open(os.path.join(d, ".cdn-agent"), "w").close()
        except Exception:
            pass
        with open(os.path.join(d, "fullchain.pem"), "w") as f:
            f.write(fc)
        with open(os.path.join(d, "privkey.pem"), "w") as f:
            f.write(kp)
        try:
            os.chmod(os.path.join(d, "privkey.pem"), 0o600)
            os.chmod(os.path.join(d, "fullchain.pem"), 0o644)
        except Exception:
            pass

    log(f"  Certificates: full re-upload ({len(desired)} written, {len(current)} old removed)")
    return True


# ── Apply config ──────────────────────────────────────────────────

def apply_config(data):
    nginx_base = data.get("nginx_base", "")
    sites = data.get("sites", [])
    certs = data.get("certs", [])

    if not nginx_base and not sites:
        log("Empty config received, skipping")
        return False

    try:
        os.makedirs(ENV["cache_dir"], exist_ok=True)
        if not ENV.get("is_baota"):
            os.makedirs("/var/cache/nginx/tmp", exist_ok=True)
        tl = ENV.get("traffic_log")
        if tl:
            os.makedirs(os.path.dirname(tl), exist_ok=True)
    except Exception:
        pass

    backup_path = backup_current()
    changed = ensure_cache_path() if ENV.get("is_baota") else False

    if nginx_base and not ENV.get("is_baota"):
        cur = sha256(open(ENV["nginx_conf"]).read()) if _exists(ENV["nginx_conf"]) else ""
        if cur != sha256(nginx_base):
            log("nginx.conf changed, updating")
            with open(ENV["nginx_conf"], "w") as f:
                f.write(nginx_base)
            changed = True

    site_dir = ENV["site_dir"]
    os.makedirs(site_dir, exist_ok=True)
    existing_files = set()

    for sc in sites:
        base_name = sc.get("filename", f"{sc.get('domain', 'unknown')}.conf")
        name = f"cdn-{base_name}" if ENV.get("is_baota") else base_name
        content = sc.get("content", "")
        if not content:
            continue
        path = os.path.join(site_dir, name)
        existing_files.add(name)

        cur = sha256(open(path).read()) if _exists(path) else ""
        if cur != sha256(content):
            log(f"  Updating: {name}")
            with open(path, "w") as f:
                f.write(content)
            changed = True

    if os.path.isdir(site_dir):
        for f in os.listdir(site_dir):
            if f in existing_files or not f.endswith(".conf"):
                continue
            # On Baota, only ever touch our own cdn-*.conf files.
            if ENV.get("is_baota") and not f.startswith("cdn-"):
                continue
            log(f"  Removing stale: {f}")
            os.remove(os.path.join(site_dir, f))
            changed = True

    if _sync_certs(certs):
        changed = True

    if not changed:
        return True

    log("Testing new config...")
    if not nginx_test():
        log("Config test FAILED, rolling back")
        rollback(backup_path)
        return False

    if nginx_running():
        log("Reloading nginx")
        nginx_reload()
    else:
        log("Starting nginx")
        nginx_start()

    # Verify the real nginx is actually up after applying.
    time.sleep(1)
    if not nginx_running():
        log("nginx is NOT running after apply — configuration not active")
        return False
    return True


# ── Disk housekeeping ─────────────────────────────────────────────

def _disk_used_pct():
    try:
        base = ENV.get("cache_dir") or "/"
        st = os.statvfs(os.path.dirname(base) or "/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if total <= 0:
            return 0
        return int((total - free) * 100 / total)
    except Exception:
        return 0


def _cleanup_disk():
    used = _disk_used_pct()
    if used < DISK_CLEAN_THRESHOLD:
        return
    log(f"Disk usage {used}% >= {DISK_CLEAN_THRESHOLD}%, running cleanup...")

    log_dir = ENV.get("log_dir")
    if log_dir and os.path.isdir(log_dir):
        for name in ("access.log", "error.log"):
            p = os.path.join(log_dir, name)
            try:
                if _exists(p) and os.path.getsize(p) > 10 * 1024 * 1024:
                    with open(p, "w"):
                        pass
                    log(f"  Truncated {p}")
            except Exception:
                pass

    try:
        subprocess.run(["journalctl", "--vacuum-size=200M"], capture_output=True, timeout=30)
    except Exception:
        pass

    for cmd in (["apt-get", "clean"], ["apt-get", "autoclean"]):
        try:
            subprocess.run(cmd, capture_output=True, timeout=60)
        except Exception:
            pass
    for d in ("/tmp", "/var/tmp"):
        try:
            subprocess.run(["find", d, "-type", "f", "-mtime", "+1", "-delete"],
                           capture_output=True, timeout=30)
        except Exception:
            pass

    try:
        if os.path.isdir(BACKUP_DIR):
            backups = sorted(os.listdir(BACKUP_DIR))
            for old in backups[:-3]:
                shutil.rmtree(os.path.join(BACKUP_DIR, old), ignore_errors=True)
                log(f"  Removed old backup {old}")
    except Exception:
        pass

    log(f"Cleanup done, disk now {_disk_used_pct()}%")


def maintenance():
    ensure_nginx()
    if _disk_used_pct() >= DISK_CLEAN_THRESHOLD:
        _cleanup_disk()


# ── Traffic accounting (from access.log) ─────────────────────────

TRAFFIC_MAX_READ = 16 * 1024 * 1024
TRAFFIC_KEEP_DAYS = 70
TRAFFIC_AGG_INTERVAL = 180

_HOST_RE = re.compile(r"\bhost=(\S+)")
_BYTES_RE = re.compile(r"\bbytes=(\d+)")
_TS_RE = re.compile(r"\bts=(\d{4}-\d{2}-\d{2})")


def _aggregate_traffic(state):
    """Incrementally parse access.log and accumulate per day/per host bytes."""
    logp = ENV.get("traffic_log") or ""
    if not logp or not _exists(logp):
        return False

    tr = state.get("traffic")
    if not isinstance(tr, dict):
        tr = {"inode": 0, "offset": 0, "days": {}}
        state["traffic"] = tr
    tr.setdefault("days", {})

    try:
        st = os.stat(logp)
    except Exception:
        return False

    if tr.get("inode") != st.st_ino or st.st_size < tr.get("offset", 0):
        tr["inode"] = st.st_ino
        tr["offset"] = 0

    offset = tr.get("offset", 0)
    if st.st_size <= offset:
        return False

    try:
        with open(logp, "rb") as f:
            f.seek(offset)
            chunk = f.read(TRAFFIC_MAX_READ)
    except Exception:
        return False

    nl = chunk.rfind(b"\n")
    if nl < 0:
        return False
    usable = chunk[:nl + 1]
    tr["offset"] = offset + len(usable)

    days = tr["days"]
    for raw in usable.split(b"\n"):
        if not raw:
            continue
        line = raw.decode("utf-8", "replace")
        mh = _HOST_RE.search(line)
        mb = _BYTES_RE.search(line)
        mt = _TS_RE.search(line)
        if not (mh and mb and mt):
            continue
        try:
            nb = int(mb.group(1))
        except Exception:
            continue
        host = mh.group(1).lower()
        day = mt.group(1)
        d = days.setdefault(day, {})
        d[host] = d.get(host, 0) + nb

    cutoff = (datetime.now() - timedelta(days=TRAFFIC_KEEP_DAYS)).strftime("%Y-%m-%d")
    for d in [k for k in list(days.keys()) if k < cutoff]:
        days.pop(d, None)
    return True


def _recent_traffic(state, n=3):
    tr = state.get("traffic") or {}
    days = tr.get("days") or {}
    if not days:
        return None
    out = {}
    today = datetime.now().date()
    for i in range(n):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        if d in days:
            out[d] = days[d]
    return out or None


# ── Sync with Manager ─────────────────────────────────────────────

def _auth_headers(config):
    return {"Authorization": f"Bearer {config['api_key']}"}


def _report_status(config, status, traffic=None):
    try:
        payload = {"status": status, "detected_type": ENV.get("detected_type", "")}
        if traffic:
            payload["traffic"] = traffic
        http_post_json(f"{config['manager_url']}/api/v1/node/status",
                       payload, _auth_headers(config), timeout=8)
    except Exception:
        pass


def sync(config):
    state = load_json(STATE_FILE)
    url = f"{config['manager_url']}/api/v1/node/sync"
    h = _auth_headers(config)

    try:
        status, body = http_get(url + "?hash=" + urllib.parse.quote(state.get("hash", "")), h)
    except Exception as e:
        log(f"Sync error: {e}")
        return

    if status != 200:
        log(f"Sync {status}: {body[:150]}")
        return

    try:
        data = json.loads(body)
    except Exception:
        log("Sync: invalid JSON from manager")
        return

    # Traffic accounting (throttled) + health-aware heartbeat
    now_ts = time.time()
    tr = state.get("traffic")
    if not isinstance(tr, dict) or now_ts - tr.get("agg_at", 0) > TRAFFIC_AGG_INTERVAL:
        _aggregate_traffic(state)
        state.setdefault("traffic", {})["agg_at"] = now_ts
        save_json(STATE_FILE, state)
    _report_status(config, "online" if nginx_running() else "nginx_down",
                   _recent_traffic(state))

    if data.get("action") == "none":
        if state.get("_hb", 0) % 20 == 0:
            log("Poll OK, no changes")
        state["_hb"] = state.get("_hb", 0) + 1
        save_json(STATE_FILE, state)
        return

    log(f"Config update: {len(data.get('sites', []))} sites, {len(data.get('certs', []))} certs")
    ok = apply_config(data)
    if not ok:
        log("Config apply FAILED (nginx not active or test failed)")
        # After a rollback nginx may still be running the last good config.
        _report_status(config, "online" if nginx_running() else "nginx_down",
                       _recent_traffic(state))
        return

    state["hash"] = data.get("config_hash", "")
    state["last_sync"] = datetime.now().isoformat()
    save_json(STATE_FILE, state)

    token = data.get("sync_token", "")
    if not token:
        return
    if not nginx_running():
        log("nginx not running after apply, skip verify")
        _report_status(config, "nginx_down")
        return
    try:
        st, b = http_post_json(f"{config['manager_url']}/api/v1/node/verify",
                               {"sync_token": token}, h, timeout=8)
        try:
            verified = json.loads(b).get("verified")
        except Exception:
            verified = False
        log("Sync verified by manager" if verified else "Sync verify FAILED")
    except Exception as e:
        log(f"Sync verify error: {e}")


def register(args):
    url = args["manager"].rstrip("/")
    token = args["token"]

    log(f"Registering: {url}")
    detected = "baota" if detect_is_baota() else "auto"
    try:
        st, body = http_post_json(f"{url}/api/v1/nodes/register",
                                  {"token": token, "detected_type": detected})
    except Exception as e:
        log(f"Registration failed: {e}")
        sys.exit(1)

    try:
        d = json.loads(body)
    except Exception:
        d = {}

    if st == 409 and d.get("api_key"):
        log("Node already exists, using existing api_key")
    elif st == 201:
        pass
    else:
        log(f"Registration {st}: {body[:200]}")
        sys.exit(1)

    config = {
        "manager_url": url,
        "api_key": d["api_key"],
        "node_id": d.get("node_id"),
        "node_name": d.get("node_name", ""),
        "node_type": d.get("node_type", ""),
    }
    save_json(CONFIG_FILE, config)
    log(f"Registered: node_id={config['node_id']} type={config['node_type'] or '(detect)'}")

    resolve_env(config)
    log("Initial sync...")
    try:
        st, body = http_get(f"{url}/api/v1/node/sync", _auth_headers(config))
        if st == 200:
            data = json.loads(body)
            if apply_config(data):
                save_json(STATE_FILE, {"hash": data.get("config_hash", ""),
                                       "last_sync": datetime.now().isoformat()})
                log("Initial sync complete")
            else:
                log("Initial sync applied with errors")
        else:
            log(f"Initial sync {st}: {body[:150]}")
    except Exception as e:
        log(f"Initial sync failed: {e}")


# ── Self update ───────────────────────────────────────────────────

LAST_UPDATE_CHECK = 0


def check_self_update(config):
    global LAST_UPDATE_CHECK
    now = time.time()
    if now - LAST_UPDATE_CHECK < 3600:
        return
    LAST_UPDATE_CHECK = now

    me = os.path.abspath(__file__)
    try:
        my_hash = sha256(open(me).read())
    except Exception:
        return

    try:
        st, body = http_get(f"{config['manager_url']}/api/v1/agent/download")
    except Exception:
        return
    if st != 200:
        return

    # Validate before replacing: must look like our agent and be valid Python.
    head = body.lstrip()[:200]
    if not head.startswith("#!") or "def run(" not in body or "__main__" not in body:
        log("Agent update rejected: content does not look like the agent (manager error page?)")
        return
    try:
        compile(body, "cdn_agent.new", "exec")
    except SyntaxError as e:
        log(f"Agent update rejected: syntax error in downloaded agent ({e})")
        return

    if sha256(body) == my_hash:
        return

    log("Agent update found, applying...")
    with open(me + ".new", "w") as f:
        f.write(body)
    os.replace(me + ".new", me)
    os.chmod(me, 0o755)
    log("Agent updated, restarting...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ── Entrypoint ────────────────────────────────────────────────────

def diag():
    config = load_json(CONFIG_FILE)
    resolve_env(config)
    print(json.dumps({
        "node_type_registered": config.get("node_type", ""),
        "is_baota": ENV["is_baota"],
        "nginx_bin": ENV["nginx_bin"],
        "nginx_conf": ENV["nginx_conf"],
        "site_dir": ENV["site_dir"],
        "ssl_dir": ENV["ssl_dir"],
        "cache_dir": ENV["cache_dir"],
        "nginx_running": nginx_running(),
    }, indent=2, ensure_ascii=False))


def run():
    config = load_json(CONFIG_FILE)
    if not config.get("api_key"):
        log("Not configured. Run: python3 cdn_agent.py --register --manager URL --token TOKEN")
        sys.exit(1)

    resolve_env(config)
    log(f"Agent started, polling {config['manager_url']} every {POLL}s")
    check_self_update(config)
    sync(config)
    maintenance()
    while True:
        try:
            check_self_update(config)
            sync(config)
            maintenance()
        except Exception as e:
            log(f"Sync exception: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--register", action="store_true")
    p.add_argument("--manager", default="")
    p.add_argument("--token", default="")
    p.add_argument("--once", action="store_true")
    p.add_argument("--diag", action="store_true", help="Print detected environment and exit")
    args = p.parse_args()

    if args.diag:
        diag()
        sys.exit(0)

    if args.register and args.manager and args.token:
        register({"manager": args.manager, "token": args.token})

    if args.once:
        config = load_json(CONFIG_FILE)
        if config.get("api_key"):
            resolve_env(config)
            sync(config)
        sys.exit(0)

    run()
