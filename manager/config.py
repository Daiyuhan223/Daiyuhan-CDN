import os

MANAGER_HOST = os.environ.get("CDN_MANAGER_HOST", "0.0.0.0")
MANAGER_PORT = int(os.environ.get("CDN_MANAGER_PORT", "8080"))

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")


def get_panel_conf_path():
    return os.path.join(_DATA_DIR, "panel.conf")


def get_panel_credentials():
    user = os.environ.get("CDN_PANEL_USER", "Admin")
    pwd = os.environ.get("CDN_PANEL_PASS", "cdn123456")
    f = get_panel_conf_path()
    if os.path.exists(f):
        try:
            with open(f) as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("user="):
                        user = line[5:].strip() or user
                    elif line.startswith("pass="):
                        pwd = line[5:].strip() or pwd
        except Exception:
            pass
    return user, pwd


def set_panel_credentials(user, pwd):
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(get_panel_conf_path(), "w") as f:
        f.write(f"user={user}\npass={pwd}\n")


PANEL_USER, PANEL_PASS = get_panel_credentials()

DB_PATH = os.environ.get("CDN_DB_PATH", os.path.join(_DATA_DIR, "cdn.db"))

ACME_SH_PATH = os.environ.get("ACME_SH_PATH", "/root/.acme.sh/acme.sh")
ACME_DNS_ENV = {
    "Ali_Key": os.environ.get("Ali_Key", ""),
    "Ali_Secret": os.environ.get("Ali_Secret", ""),
    "CF_Token": os.environ.get("CF_Token", ""),
    "CF_Email": os.environ.get("CF_Email", ""),
}
ACME_DNS_PLUGIN = os.environ.get("ACME_DNS_PLUGIN", "dns_ali")

CACHE_TTL_DEFAULT = 3600
CACHE_TTL_BY_EXT = {
    ".html": 300, ".htm": 300, ".css": 3600, ".js": 3600,
    ".png": 86400, ".jpg": 86400, ".jpeg": 86400, ".gif": 86400,
    ".svg": 86400, ".ico": 604800, ".webp": 86400,
    ".woff": 604800, ".woff2": 604800, ".ttf": 604800, ".eot": 604800,
    ".mp4": 86400, ".webm": 86400,
}

NGINX_CACHE_PATH = "/var/cache/nginx/cdn_cache"
NGINX_CACHE_SIZE = "1g"
NGINX_CACHE_INACTIVE = "7d"
NGINX_KEYS_ZONE = "10m"

AGENT_POLL_INTERVAL = 30
AGENT_CONFIG_DIR = "/etc/nginx/conf.d/cdn"
AGENT_SSL_DIR = "/etc/nginx/ssl"
AGENT_INSTALL_DIR = "/opt/cdn-agent"
AGENT_CONFIG_FILE = "/opt/cdn-agent/agent.json"
AGENT_STATE_FILE = "/opt/cdn-agent/state.json"
AGENT_BACKUP_DIR = "/opt/cdn-agent/backup"

MANAGER_URL = f"http://{MANAGER_HOST}:{MANAGER_PORT}"
