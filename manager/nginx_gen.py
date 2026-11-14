import os
import hashlib
import json
from typing import List, Dict, Optional
from jinja2 import Environment, FileSystemLoader

from .config import (
    NGINX_CACHE_PATH, NGINX_CACHE_SIZE, NGINX_CACHE_INACTIVE,
    NGINX_KEYS_ZONE,
    AGENT_CONFIG_DIR, AGENT_SSL_DIR, MANAGER_HOST,
)
from .models import Site, Node


TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _load_template(name: str):
    return _env.get_template(name)


BAOTA_SSL_DIR = "/www/server/nginx/conf/ssl"


def _ssl_root(node: Node) -> str:
    """Return the SSL root directory for a node based on its type."""
    return BAOTA_SSL_DIR if node.node_type == "baota" else AGENT_SSL_DIR


def _traffic_log(node: Node) -> str:
    """Dedicated access log for traffic accounting (has host/bytes/ts fields)."""
    return ("/www/server/nginx/logs/cdn_access.log" if node.node_type == "baota"
            else "/var/log/nginx/cdn_access.log")


def generate_nginx_base() -> str:
    tmpl = _load_template("nginx_base.conf.j2")
    return tmpl.render(
        nginx_cache_path=NGINX_CACHE_PATH,
        nginx_cache_size=NGINX_CACHE_SIZE,
        nginx_cache_inactive=NGINX_CACHE_INACTIVE,
        nginx_keys_zone=NGINX_KEYS_ZONE,
        agent_config_dir=AGENT_CONFIG_DIR,
    )


def generate_site_config(site: Site, node: Node, manager_ip: str = "",
                         ssl_cert_path: str = "", ssl_key_path: str = "") -> str:
    if site.redirect_url:
        return _render_redirect(site, node, ssl_cert_path, ssl_key_path)
    tmpl = _load_template("edge_site.conf.j2")
    return tmpl.render(
        domain=site.domain,
        origin_url=site.origin_url,
        origin_host=site.origin_host or site.domain,
        cache_ttl=site.cache_ttl,
        ssl_enabled=site.ssl_enabled,
        cache_enabled=site.cache_enabled,
        force_https=site.force_https,
        custom_conf=site.custom_conf,
        custom_location_conf=site.custom_location_conf,
        traffic_log=_traffic_log(node),
        ssl_cert=ssl_cert_path,
        ssl_key=ssl_key_path,
        node_name=node.name,
        manager_ip=manager_ip or MANAGER_HOST,
    )


def _render_redirect(site: Site, node: Node, ssl_cert_path: str, ssl_key_path: str) -> str:
    tmpl = _load_template("redirect_site.conf.j2")
    redirect = site.redirect_url.strip()
    if redirect and not redirect.startswith(("http://", "https://")):
        redirect = "$scheme://" + redirect
    return tmpl.render(
        domain=site.domain,
        redirect_url=redirect,
        ssl_enabled=site.ssl_enabled,
        force_https=site.force_https,
        custom_conf=site.custom_conf,
        traffic_log=_traffic_log(node),
        ssl_cert=ssl_cert_path,
        ssl_key=ssl_key_path,
    )


def generate_node_config(node: Node, sites: List[Site],
                         ssl_certs_map: Optional[Dict[int, tuple]] = None,
                         manager_ip: str = "") -> dict:
    base_conf = generate_nginx_base()
    site_configs = []

    if ssl_certs_map is None:
        ssl_certs_map = {}

    ssl_root = _ssl_root(node)

    for site in sites:
        cert_info = ssl_certs_map.get(site.id, ("", ""))
        ssl_cert_path = ""
        ssl_key_path = ""
        if site.ssl_enabled and cert_info[0]:
            ssl_cert_path = f"{ssl_root}/{site.domain}/fullchain.pem"
            ssl_key_path = f"{ssl_root}/{site.domain}/privkey.pem"

        site_conf = generate_site_config(site, node, manager_ip, ssl_cert_path, ssl_key_path)
        site_configs.append({
            "domain": site.domain,
            "filename": f"{site.domain}.conf",
            "content": site_conf,
        })

    config_hash = _hash_configs(base_conf, site_configs)

    return {
        "action": "reload",
        "nginx_base": base_conf,
        "config_hash": config_hash,
        "sites": site_configs,
        "certs": _build_certs_list(sites, ssl_certs_map, ssl_root),
    }


def _hash_configs(base_conf: str, site_configs: list) -> str:
    data = base_conf + json.dumps(site_configs, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


def _build_certs_list(sites: List[Site], ssl_certs_map: Dict[int, tuple],
                      ssl_root: str = AGENT_SSL_DIR) -> list:
    certs = []
    for site in sites:
        if not site.ssl_enabled:
            continue
        cert_info = ssl_certs_map.get(site.id)
        if cert_info and cert_info[0] and cert_info[1]:
            certs.append({
                "domain": site.domain,
                "fullchain_pem": cert_info[0],
                "privkey_pem": cert_info[1],
                "cert_path": f"{ssl_root}/{site.domain}/fullchain.pem",
                "key_path": f"{ssl_root}/{site.domain}/privkey.pem",
            })
    return certs
