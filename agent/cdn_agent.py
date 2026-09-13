#!/usr/bin/env python3
"""CDN Edge Node Agent — polls Manager, applies Nginx config with backup/rollback."""

import os, sys, json, time, shutil, hashlib, subprocess
from datetime import datetime

try:
    import requests
except ImportError:
    print("[agent] pip3 install requests")
    sys.exit(1)

CONF_DIR = "/opt/cdn-agent"
CONFIG_FILE = f"{CONF_DIR}/agent.json"
STATE_FILE = f"{CONF_DIR}/state.json"
BACKUP_DIR = f"{CONF_DIR}/backup"

NGINX_CONF = "/etc/nginx/nginx.conf"
SITE_DIR = "/etc/nginx/conf.d/cdn"
SSL_DIR = "/etc/nginx/ssl"
CACHE_DIR = "/var/cache/nginx/cdn_cache"
LOG_DIR = "/var/log/nginx"
POLL = 60

# Auto-detect Baota
IS_BAOTA = os.path.exists("/www/server/panel")
if IS_BAOTA:
    NGINX_CONF = "/www/server/nginx/conf/nginx.conf"
    SITE_DIR = "/www/server/panel/vhost/nginx"
    SSL_DIR = "/www/server/nginx/conf/ssl"
    CACHE_DIR = "/www/server/nginx/cdn_cache"
    LOG_DIR = "/www/server/nginx/logs"

# Disk cleanup thresholds (percent of disk used to trigger)
DISK_CLEAN_THRESHOLD = 90
DISK_CLEAN_TARGET = 80


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default or {}


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def sha256(s):
    return hashlib.sha256(s.encode()).hexdigest()


def backup_current():
    """Backup current working nginx config before applying changes."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bdir = f"{BACKUP_DIR}/{ts}"
    os.makedirs(bdir, exist_ok=True)

    for src in [NGINX_CONF, SITE_DIR, SSL_DIR]:
        dst = os.path.join(bdir, os.path.basename(src))
        if os.path.exists(src):
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    log(f"Backup saved: {bdir}")

    backups = sorted(os.listdir(BACKUP_DIR), reverse=True)
    for old in backups[5:]:
        shutil.rmtree(f"{BACKUP_DIR}/{old}", ignore_errors=True)
    return bdir


def rollback(backup_path):
    """Restore nginx config from backup."""
    log(f"Rolling back to: {backup_path}")
    for item in os.listdir(backup_path):
        src = os.path.join(backup_path, item)
        if item == "nginx.conf":
            shutil.copy2(src, NGINX_CONF)
        elif item == "cdn":
            if os.path.exists(SITE_DIR):
                shutil.rmtree(SITE_DIR, ignore_errors=True)
            shutil.copytree(src, SITE_DIR, dirs_exist_ok=True)
        elif item == "ssl":
            if os.path.exists(SSL_DIR):
                shutil.rmtree(SSL_DIR, ignore_errors=True)
            shutil.copytree(src, SSL_DIR, dirs_exist_ok=True)
    nginx_reload()


def nginx_test():
    r = subprocess.run(["/usr/sbin/nginx", "-t"], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        log(f"nginx -t FAILED:\n{r.stderr[-500:]}")
        return False
    return True


def nginx_reload():
    r = subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        log(f"systemctl reload FAILED: {r.stderr}")
        return False
    return True


def nginx_start():
    r = subprocess.run(["systemctl", "start", "nginx"], capture_output=True, text=True, timeout=10)
    return r.returncode == 0


def nginx_running():
    r = subprocess.run(["systemctl", "is-active", "--quiet", "nginx"], timeout=5)
    return r.returncode == 0


def ensure_nginx():
    """Self-heal: if nginx died and a config exists, bring it back."""
    if nginx_running():
        return True
    if not os.path.exists(NGINX_CONF):
        return False
    log("nginx not running, testing config & starting...")
    if nginx_test():
        if nginx_start():
            log("nginx restarted (self-heal)")
            return True
    else:
        log("nginx config invalid, cannot self-heal")
    return False


def _disk_used_pct():
    try:
        st = os.statvfs(os.path.dirname(CACHE_DIR) or "/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if total <= 0:
            return 0
        return int((total - free) * 100 / total)
    except Exception:
        return 0


def _cleanup_disk():
    """Free disk when usage is high. Cleans logs, journald, apt/tmp, old agent backups."""
    used = _disk_used_pct()
    if used < DISK_CLEAN_THRESHOLD:
        return
    log(f"Disk usage {used}% >= {DISK_CLEAN_THRESHOLD}%, running cleanup...")

    # 1. truncate nginx logs
    if os.path.isdir(LOG_DIR):
        for name in ("access.log", "error.log"):
            p = os.path.join(LOG_DIR, name)
            try:
                if os.path.exists(p) and os.path.getsize(p) > 10 * 1024 * 1024:
                    with open(p, "w"):
                        pass
                    log(f"  Truncated {p}")
            except Exception:
                pass

    # 2. journald vacuum
    try:
        subprocess.run(["journalctl", "--vacuum-size=200M"], capture_output=True,
                       text=True, timeout=30)
    except Exception:
        pass

    # 3. apt cache + temp files
    for cmd in (["apt-get", "clean"], ["apt-get", "autoclean"]):
        try:
            subprocess.run(cmd, capture_output=True, timeout=60)
        except Exception:
            pass
    for d in ("/tmp", "/var/tmp"):
        try:
            subprocess.run(
                ["find", d, "-type", "f", "-mtime", "+1", "-delete"],
                capture_output=True, timeout=30)
        except Exception:
            pass

    # 4. keep only recent agent backups
    try:
        if os.path.isdir(BACKUP_DIR):
            backups = sorted(os.listdir(BACKUP_DIR))
            for old in backups[:-3]:
                shutil.rmtree(f"{BACKUP_DIR}/{old}", ignore_errors=True)
                log(f"  Removed old backup {old}")
    except Exception:
        pass

    log(f"Cleanup done, disk now {_disk_used_pct()}%")


def maintenance():
    """Run periodic housekeeping: self-heal nginx + disk cleanup."""
    ensure_nginx()
    if _disk_used_pct() >= DISK_CLEAN_THRESHOLD:
        _cleanup_disk()


def _ensure_baota_cache_path():
    """Inject proxy_cache_path into Baota's nginx.conf http block."""
    conf = NGINX_CONF
    if not os.path.exists(conf):
        return
    with open(conf) as f:
        content = f.read()
    if "proxy_cache_path" in content:
        return
    # Insert after 'http {' line
    cache_line = "\tproxy_cache_path /www/server/nginx/cdn_cache levels=1:2 keys_zone=cdn_cache:10m max_size=1g inactive=7d;\n"
    content = content.replace("http {", "http {\n" + cache_line, 1)
    with open(conf, "w") as f:
        f.write(content)
    os.makedirs("/www/server/nginx/cdn_cache", exist_ok=True)
    log("proxy_cache_path added to nginx.conf")


def apply_config(data):
    nginx_base = data.get("nginx_base", "")
    sites = data.get("sites", [])
    certs = data.get("certs", [])

    if not nginx_base and not sites:
        log("Empty config received, skipping")
        return False

    backup_path = backup_current()

    changed = False

    if nginx_base:
        if not IS_BAOTA:
            cur = sha256(open(NGINX_CONF).read()) if os.path.exists(NGINX_CONF) else ""
            new = sha256(nginx_base)
            if cur != new:
                log("nginx.conf changed, updating")
                with open(NGINX_CONF, "w") as f:
                    f.write(nginx_base)
                changed = True
        else:
            _ensure_baota_cache_path()

    os.makedirs(SITE_DIR, exist_ok=True)
    existing_files = set()

    for sc in sites:
        base_name = sc.get("filename", f"{sc.get('domain', 'unknown')}.conf")
        name = f"cdn-{base_name}" if IS_BAOTA else base_name
        content = sc.get("content", "")
        if not content:
            continue
        path = os.path.join(SITE_DIR, name)
        existing_files.add(name)

        cur = sha256(open(path).read()) if os.path.exists(path) else ""
        new = sha256(content)
        if cur != new:
            log(f"  Updating: {name}")
            with open(path, "w") as f:
                f.write(content)
            changed = True

    for f in os.listdir(SITE_DIR):
        if f not in existing_files and f.endswith(".conf"):
            if IS_BAOTA and not f.startswith("cdn-"):
                continue
            log(f"  Removing stale: {f}")
            os.remove(os.path.join(SITE_DIR, f))
            changed = True

    existing_dirs = set()
    for cert in certs:
        domain = cert.get("domain", "")
        if not domain:
            continue
        d = os.path.join(SSL_DIR, domain)
        existing_dirs.add(domain)
        os.makedirs(d, exist_ok=True)

        cp = os.path.join(d, "fullchain.pem")
        kp = os.path.join(d, "privkey.pem")
        cur_c = sha256(open(cp).read()) if os.path.exists(cp) else ""
        new_c = sha256(cert.get("fullchain_pem", ""))

        if cur_c != new_c:
            log(f"  Updating SSL: {domain}")
            with open(cp, "w") as f:
                f.write(cert["fullchain_pem"])
            with open(kp, "w") as f:
                f.write(cert["privkey_pem"])
            os.chmod(kp, 0o600)
            os.chmod(cp, 0o644)
            changed = True

    if os.path.isdir(SSL_DIR):
        for d in os.listdir(SSL_DIR):
            if d not in existing_dirs:
                p = os.path.join(SSL_DIR, d)
                if os.path.isdir(p):
                    log(f"  Removing stale SSL: {d}")
                    shutil.rmtree(p, ignore_errors=True)
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

    return True


def sync(config):
    state = load_json(STATE_FILE)
    url = f"{config['manager_url']}/api/v1/node/sync"
    h = {"Authorization": f"Bearer {config['api_key']}"}

    try:
        r = requests.get(url, params={"hash": state.get("hash", "")}, headers=h, timeout=15)
    except requests.RequestException as e:
        log(f"Sync error: {e}")
        return

    if r.status_code != 200:
        log(f"Sync {r.status_code}: {r.text[:150]}")
        return

    data = r.json()

    # Report online status every poll (even no changes)
    try:
        requests.post(f"{config['manager_url']}/api/v1/node/status",
                      json={"status": "online"}, headers=h, timeout=5)
    except Exception:
        pass

    action = data.get("action", "none")
    if action == "none":
        # Heartbeat log once every 20 polls
        if state.get("_hb", 0) % 20 == 0:
            log("Poll OK, no changes")
        state["_hb"] = state.get("_hb", 0) + 1
        save_json(STATE_FILE, state)
        return

    log(f"Config update: {len(data.get('sites',[]))} sites, {len(data.get('certs',[]))} certs")
    ok = apply_config(data)
    if ok:
        state["hash"] = data.get("config_hash", "")
        state["last_sync"] = datetime.now().isoformat()
        save_json(STATE_FILE, state)

        st = data.get("sync_token", "")
        try:
            r = requests.post(f"{config['manager_url']}/api/v1/node/verify",
                              json={"sync_token": st}, headers=h, timeout=5)
            if r.json().get("verified"):
                log("Sync verified by manager")
            else:
                log("Sync verify FAILED")
        except Exception:
            pass


def register(args):
    url = args["manager"].rstrip("/")
    token = args["token"]

    log(f"Registering: {url}")
    try:
        r = requests.post(f"{url}/api/v1/nodes/register", json={"token": token}, timeout=15)
    except requests.RequestException as e:
        log(f"Registration failed: {e}")
        sys.exit(1)

    d = r.json()
    if r.status_code == 409 and d.get("api_key"):
        log(f"Node already exists, using existing api_key")
    elif r.status_code == 201:
        pass
    else:
        log(f"Registration {r.status_code}: {r.text}")
        sys.exit(1)

    config = {
        "manager_url": url,
        "api_key": d["api_key"],
        "node_id": d.get("node_id"),
        "node_name": d.get("node_name", ""),
    }
    save_json(CONFIG_FILE, config)
    log(f"Registered: node_id={config['node_id']}")

    log("Initial sync...")
    try:
        r = requests.get(f"{url}/api/v1/node/sync",
                         headers={"Authorization": f"Bearer {config['api_key']}"},
                         timeout=15)
        if r.status_code == 200:
            data = r.json()
            apply_config(data)
            save_json(STATE_FILE, {"hash": data.get("config_hash", ""),
                      "last_sync": datetime.now().isoformat()})
            log("Initial sync complete")
    except requests.RequestException as e:
        log(f"Initial sync failed: {e}")


LAST_UPDATE_CHECK = 0


def check_self_update(config):
    """Every hour, check if Manager has a newer agent version."""
    global LAST_UPDATE_CHECK
    now = time.time()
    if now - LAST_UPDATE_CHECK < 3600:
        return
    LAST_UPDATE_CHECK = now

    me = os.path.abspath(__file__)
    my_hash = sha256(open(me).read())

    try:
        r = requests.get(f"{config['manager_url']}/api/v1/agent/download", timeout=15)
        if r.status_code == 200 and r.text.strip().startswith("#!/"):
            new_hash = sha256(r.text)
            if new_hash != my_hash:
                log("Agent update found, applying...")
                with open(me + ".new", "w") as f:
                    f.write(r.text)
                os.replace(me + ".new", me)
                os.chmod(me, 0o755)
                log("Agent updated, restarting...")
                os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception:
        pass


def run():
    config = load_json(CONFIG_FILE)
    if not config.get("api_key"):
        log("Not configured. Run: python3 cdn_agent.py --register --manager URL --token TOKEN")
        sys.exit(1)

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
    args = p.parse_args()

    if args.register and args.manager and args.token:
        register({"manager": args.manager, "token": args.token})

    if args.once:
        config = load_json(CONFIG_FILE)
        if config.get("api_key"):
            sync(config)
        sys.exit(0)

    run()
