#!/usr/bin/env python3
import argparse
import sys
import os
import json
import socket
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manager.database import Database
from manager.models import Site, Node
from manager.ssl_manager import SSLManager
from manager.nginx_gen import generate_node_config
from manager.config import MANAGER_HOST, MANAGER_PORT

db = Database()
ssl_mgr = SSLManager()


def _is_private_ip(ip):
    return ip.startswith(("127.", "10.", "172.16.", "192.168.")) or ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31


def _detect_manager_ip():
    if MANAGER_HOST not in ("0.0.0.0", "127.0.0.1", "::"):
        return MANAGER_HOST

    try:
        import urllib.request
        for url in ["https://ifconfig.me", "https://api.ipify.org", "https://ipinfo.io/ip"]:
            try:
                r = urllib.request.urlopen(url, timeout=5)
                ip = r.read().decode().strip()
                if ip and not _is_private_ip(ip):
                    return ip
            except Exception:
                continue
    except Exception:
        pass

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not _is_private_ip(ip):
            return ip
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            for ip in result.stdout.strip().split():
                if not _is_private_ip(ip):
                    return ip
    except Exception:
        pass

    return ""


def cmd_site_list(args):
    sites = db.list_sites()
    if not sites:
        print("No sites configured.")
        return
    print(f"{'ID':<5} {'Domain':<35} {'Origin':<40} {'TTL':<8} {'SSL':<5} {'Https':<6} {'Active':<6} {'Cache':<6} {'Cert':<5} {'Redirect':<30}")
    print("-" * 160)
    for s in sites:
        print(f"{s.id:<5} {s.domain:<35} {s.origin_url:<40} {s.cache_ttl:<8} "
              f"{'Y' if s.ssl_enabled else 'N':<5} {'Y' if s.force_https else 'N':<6} "
              f"{'Y' if s.active else 'N':<6} {'Y' if s.cache_enabled else 'N':<6} "
              f"{s.certificate_id if s.certificate_id else '-':<5} {s.redirect_url:<30}")


def cmd_site_add(args):
    origin = args.origin or ""
    redirect = args.redirect or ""
    if not origin and not redirect:
        print("ERROR: provide --origin (proxy site) or --redirect <url> (301 redirect site)")
        return

    site = Site(
        domain=args.domain.strip().lower(),
        origin_url=origin,
        redirect_url=redirect,
        cache_ttl=args.ttl,
        ssl_enabled=not args.no_ssl,
        cache_enabled=not args.no_cache,
        force_https=args.force_https,
    )
    if args.cert:
        if not db.get_certificate(int(args.cert)):
            print(f"ERROR: certificate #{args.cert} not found in library")
            return
        site.certificate_id = int(args.cert)
    site = db.create_site(site)
    db.bump_all_node_hashes()
    kind = "redirect" if redirect else "proxy"
    print(f"Site created: id={site.id} domain={site.domain} type={kind}")


def cmd_site_delete(args):
    site = db.get_site_by_domain(args.domain)
    if not site:
        print(f"Site '{args.domain}' not found.")
        return
    db.delete_site(site.id)
    db.bump_all_node_hashes()
    print(f"Site '{args.domain}' deleted.")


def cmd_site_info(args):
    site = db.get_site_by_domain(args.domain)
    if not site:
        print(f"Site '{args.domain}' not found.")
        return
    nodes = db.list_nodes()
    print(json.dumps({
        "id": site.id,
        "domain": site.domain,
        "origin_url": site.origin_url,
        "origin_host": site.origin_host,
        "cache_ttl": site.cache_ttl,
        "ssl_enabled": site.ssl_enabled,
        "active": site.active,
        "cache_enabled": site.cache_enabled,
        "redirect_url": site.redirect_url,
        "force_https": site.force_https,
        "certificate_id": site.certificate_id,
        "created_at": site.created_at,
        "nodes": [n.name for n in nodes],
    }, indent=2, ensure_ascii=False))


def cmd_site_update(args):
    site = db.get_site_by_domain(args.domain)
    if not site:
        print(f"Site '{args.domain}' not found.")
        return

    changed = False
    if args.new_origin:
        site.origin_url = args.new_origin
        site.origin_host = ""  # force recalculate in __post_init__
        changed = True
    if args.ttl is not None:
        site.cache_ttl = args.ttl
        changed = True
    if args.enable:
        site.active = True
        changed = True
    if args.disable:
        site.active = False
        changed = True
    if args.cache is not None:
        site.cache_enabled = args.cache
        changed = True
    if args.redirect is not None:
        site.redirect_url = args.redirect
        if args.redirect:
            site.cache_enabled = False
        changed = True
    if args.force_https is not None:
        site.force_https = args.force_https
        changed = True
    if args.cert is not None:
        if args.cert:
            if not db.get_certificate(args.cert):
                print(f"ERROR: certificate #{args.cert} not found in library")
                return
            site.certificate_id = args.cert
        else:
            site.certificate_id = None
        changed = True

    if not changed:
        print("No changes specified. Use --origin / --ttl / --enable / --disable / --cache / --no-cache / --redirect / --no-redirect / --https / --no-https / --cert <id> / --no-cert")
        return

    db.update_site(site)
    db.bump_all_node_hashes()
    print(f"Site updated: domain={site.domain} "
          f"origin={site.origin_url} ttl={site.cache_ttl}s "
          f"active={'Y' if site.active else 'N'} "
          f"cache={'Y' if site.cache_enabled else 'N'} "
          f"https={'Y' if site.force_https else 'N'} "
          f"cert={site.certificate_id if site.certificate_id else '-'} "
          f"redirect={site.redirect_url or '-'}")


def cmd_node_list(args):
    nodes = db.list_nodes()
    if not nodes:
        print("No nodes configured.")
        return
    print(f"{'ID':<5} {'Name':<20} {'IP':<18} {'Type':<8} {'Status':<10} {'Sites':<6} {'Last Seen':<20}")
    print("-" * 95)
    for n in nodes:
        sites = db.get_node_sites(n.id)
        print(f"{n.id:<5} {n.name:<20} {n.ip:<18} {n.node_type:<8} "
              f"{n.status:<10} {len(sites):<6} {n.last_seen:<20}")


def cmd_node_add(args):
    node = Node(
        name=args.name.strip(),
        ip=args.ip.strip(),
        node_type=args.type,
        api_key=__import__("secrets").token_urlsafe(32),
        status="pending",
        baota_url=getattr(args, "baota_url", "") or "",
        baota_key=getattr(args, "baota_key", "") or "",
    )
    node = db.create_node(node)
    db.bump_all_node_hashes()
    print(f"Node created: id={node.id} name={node.name}")
    print(f"API Key: {node.api_key}")


def cmd_node_delete(args):
    node = db.get_node_by_name(args.name)
    if not node:
        print(f"Node '{args.name}' not found.")
        return
    db.delete_node(node.id)
    print(f"Node '{args.name}' deleted.")


def cmd_node_update(args):
    node = db.get_node_by_name(args.name)
    if not node:
        print(f"Node '{args.name}' not found.")
        return
    if args.new_name:
        dup = db.get_node_by_name(args.new_name)
        if dup and dup.id != node.id:
            print(f"ERROR: node '{args.new_name}' already exists")
            return
        node.name = args.new_name
    if args.ip is not None:
        node.ip = args.ip
    if args.type:
        node.node_type = args.type
    if args.baota_url:
        node.baota_url = args.baota_url
    if args.baota_key:
        node.baota_key = args.baota_key

    db.update_node(node)
    db.bump_all_node_hashes()
    print(f"Node updated: id={node.id} name={node.name} type={node.node_type}")


def cmd_node_token(args):
    token = db.create_registration_token(args.node_name, args.type)
    manager_ip = _detect_manager_ip() or "<manager_ip>"
    manager_url = f"http://{manager_ip}:{MANAGER_PORT}"

    print("Registration token created:")
    print(f"  Token: {token.token}")
    print(f"  Node name: {token.node_name}")
    print(f"  Type: {token.node_type}")
    print(f"  Manager: {manager_url}")
    print()
    print("Run on the target node:")
    print(f'  curl -fsSL "{manager_url}/install.sh?token={token.token}&manager={manager_url}" | bash')


def cmd_ssl_list(args):
    certs = db.list_certificates()
    if not certs:
        print("No certificates in library.")
        return
    print(f"{'ID':<5} {'Name':<35} {'Domains':<45} {'Expires':<20} BoundSites")
    print("-" * 110)
    for c in certs:
        sites = db.certificate_sites(c.id)
        print(f"{c.id:<5} {c.name:<35} {c.domains:<45} {c.expires_at:<20} {len(sites)}")


def cmd_ssl_issue(args):
    domain = args.domain.strip().lower()
    print(f"Issuing SSL certificate for {domain}...")
    cert = ssl_mgr.issue_cert(domain, use_acme=not args.self_signed)

    if not cert:
        print("Failed to generate certificate.")
        return

    cert.name = (args.name or "").strip() or domain
    cert = db.save_certificate(cert)

    site = db.get_site_by_domain(domain)
    if site:
        site.certificate_id = cert.id
        db.update_site(site)
    db.bump_all_node_hashes()

    print(f"Certificate issued: name={cert.name} domains={cert.domains}")
    print(f"  Issued: {cert.issued_at}")
    print(f"  Expires: {cert.expires_at}")
    if site:
        print(f"  Bound to site: {site.domain}")
    else:
        print(f"  Saved to library only (bind later via site update --cert <id>)")


def cmd_ssl_info(args):
    domain = args.domain.strip().lower()
    site = db.get_site_by_domain(domain)
    cert = db.get_certificate_by_name(domain)
    if not cert and site:
        cert = db.get_site_certificate(site.id)
    if not cert:
        print(f"No certificate for '{domain}'.")
        return

    print(json.dumps({
        "id": cert.id,
        "name": cert.name,
        "domains": cert.domains,
        "issued_at": cert.issued_at,
        "expires_at": cert.expires_at,
        "needs_renewal": ssl_mgr.needs_renewal(cert),
    }, indent=2))


def cmd_ssl_import(args):
    domain = args.domain.strip().lower()
    source = args.cert_dir or f"{args.cert} + {args.key}"
    print(f"Importing certificate for {domain} from {source} ...")

    if args.cert_dir and not os.path.isdir(args.cert_dir):
        print(f"ERROR: cert directory not found: {args.cert_dir}")
        return
    if args.cert and not os.path.exists(args.cert):
        print(f"ERROR: cert file not found: {args.cert}")
        return
    if args.key and not os.path.exists(args.key):
        print(f"ERROR: key file not found: {args.key}")
        return

    cert = ssl_mgr.import_cert(
        domain=domain,
        cert_dir=args.cert_dir or "",
        cert_path=args.cert or "",
        key_path=args.key or "",
    )

    if not cert:
        print("Failed to import certificate. Check paths and try again.")
        return

    cert.name = (args.name or "").strip() or domain
    cert = db.save_certificate(cert)

    site = db.get_site_by_domain(domain)
    if site:
        site.certificate_id = cert.id
        db.update_site(site)
    db.bump_all_node_hashes()

    print(f"Certificate imported: name={cert.name} domains={cert.domains}")
    print(f"  Issued: {cert.issued_at}")
    print(f"  Expires: {cert.expires_at}")
    if site:
        print(f"  Bound to site: {site.domain}")
    else:
        print(f"  Saved to library only (bind later via site update --cert <id>)")


def cmd_cache_purge(args):
    site = db.get_site_by_domain(args.domain)
    if not site:
        print(f"Site '{args.domain}' not found.")
        return

    path = args.path or "/"
    nodes = db.list_nodes()

    print(f"CDN 节点无 Nginx purge 模块。请在每个边缘节点执行：")
    print(f"  rm -rf /var/cache/nginx/cdn_cache/* && nginx -s reload")
    print(f"或按站点域名的缓存文件：")
    print(f"  find /var/cache/nginx/cdn_cache -type f | xargs grep -l '{args.domain}' 2>/dev/null | xargs rm -f")
    print(f"  nginx -s reload")


def cmd_tokens_list(args):
    tokens = db.list_registration_tokens()
    if not tokens:
        print("No registration tokens.")
        return
    for t in tokens:
        used = "USED" if t.used else "ACTIVE"
        print(f"  [{used}] {t.token[:12]}... node={t.node_name} type={t.node_type} {t.created_at}")


def cmd_config_preview(args):
    node = db.get_node_by_name(args.node)
    if not node:
        print(f"Node '{args.node}' not found.")
        return
    sites = db.get_node_sites(node.id)
    ssl_certs_map = {}
    for site in sites:
        if site.ssl_enabled:
            cert = db.get_site_certificate(site.id) or db.get_certificate_for_domain(site.domain)
            if cert:
                ssl_certs_map[site.id] = (cert.fullchain_pem, cert.privkey_pem)

    config = generate_node_config(node, sites, ssl_certs_map)

    print(f"# === Nginx base config for node '{node.name}' ===\n")
    print(config["nginx_base"])
    print(f"\n# === Site configs ({len(config['sites'])} sites) ===\n")
    for sc in config["sites"]:
        print(f"# --- {sc['domain']} ---")
        print(sc["content"])
        print()


def main():
    parser = argparse.ArgumentParser(description="CDN Manager CLI")
    sub = parser.add_subparsers(dest="command")

    # site
    p = sub.add_parser("site", help="Site management")
    sp = p.add_subparsers(dest="sub")
    sp.add_parser("list", help="List all sites").set_defaults(func=cmd_site_list)
    pa = sp.add_parser("add", help="Add a site")
    pa.add_argument("--domain", "-d", required=True, help="Site domain")
    pa.add_argument("--origin", "-o", default="", help="Origin URL (proxy site)")
    pa.add_argument("--redirect", default="", help="301 redirect target URL (redirect site)")
    pa.add_argument("--ttl", "-t", type=int, default=3600, help="Cache TTL in seconds")
    pa.add_argument("--no-ssl", action="store_true", help="Disable SSL")
    pa.add_argument("--no-cache", action="store_true", help="Disable CDN caching (pure reverse proxy)")
    pa.add_argument("--force-https", action="store_true", help="HTTP auto-redirect to HTTPS")
    pa.add_argument("--cert", type=int, default=0, help="Certificate library ID to bind")
    pa.set_defaults(func=cmd_site_add)
    pd = sp.add_parser("delete", help="Delete a site")
    pd.add_argument("--domain", "-d", required=True)
    pd.set_defaults(func=cmd_site_delete)
    pi = sp.add_parser("info", help="Show site info")
    pi.add_argument("--domain", "-d", required=True)
    pi.set_defaults(func=cmd_site_info)
    pu = sp.add_parser("update", help="Update a site")
    pu.add_argument("--domain", "-d", required=True, help="Site domain")
    pu.add_argument("--origin", dest="new_origin", help="New origin URL")
    pu.add_argument("--ttl", "-t", type=int, help="New cache TTL in seconds")
    pu.add_argument("--enable", action="store_true", help="Enable this site")
    pu.add_argument("--disable", action="store_true", help="Disable this site")
    cache_grp = pu.add_mutually_exclusive_group()
    cache_grp.add_argument("--cache", dest="cache", action="store_true", help="Enable CDN caching")
    cache_grp.add_argument("--no-cache", dest="cache", action="store_false", help="Disable CDN caching")
    pu.set_defaults(cache=None)
    redir_grp = pu.add_mutually_exclusive_group()
    redir_grp.add_argument("--redirect", dest="redirect", help="Set 301 redirect target URL")
    redir_grp.add_argument("--no-redirect", dest="redirect", action="store_const", const="", help="Remove redirect, back to proxy")
    pu.set_defaults(redirect=None)
    https_grp = pu.add_mutually_exclusive_group()
    https_grp.add_argument("--https", dest="force_https", action="store_true", help="HTTP auto-redirect to HTTPS")
    https_grp.add_argument("--no-https", dest="force_https", action="store_false", help="Disable HTTP->HTTPS redirect")
    pu.set_defaults(force_https=None)
    cert_grp = pu.add_mutually_exclusive_group()
    cert_grp.add_argument("--cert", dest="cert", type=int, help="Certificate library ID to bind")
    cert_grp.add_argument("--no-cert", dest="cert", action="store_const", const=0, help="Unbind certificate")
    pu.set_defaults(cert=None)
    pu.set_defaults(func=cmd_site_update)

    # node
    p = sub.add_parser("node", help="Node management")
    sp = p.add_subparsers(dest="sub")
    sp.add_parser("list", help="List all nodes").set_defaults(func=cmd_node_list)
    pa = sp.add_parser("add", help="Add a node")
    pa.add_argument("--name", "-n", required=True, help="Node name")
    pa.add_argument("--ip", "-i", default="", help="Node IP")
    pa.add_argument("--type", "-t", choices=["auto", "baota"], default="auto")
    pa.add_argument("--baota-url", default="", help="Baota panel URL (for baota type)")
    pa.add_argument("--baota-key", default="", help="Baota API key (for baota type)")
    pa.set_defaults(func=cmd_node_add)
    pu2 = sp.add_parser("update", help="Update a node")
    pu2.add_argument("--name", "-n", required=True, help="Current node name")
    pu2.add_argument("--new-name", dest="new_name", help="New node name")
    pu2.add_argument("--ip", "-i", help="New node IP")
    pu2.add_argument("--type", "-t", choices=["auto", "baota"], help="New node type")
    pu2.add_argument("--baota-url", default="", help="Baota panel URL")
    pu2.add_argument("--baota-key", default="", help="Baota API key")
    pu2.set_defaults(func=cmd_node_update)
    pd = sp.add_parser("delete", help="Delete a node")
    pd.add_argument("--name", "-n", required=True)
    pd.set_defaults(func=cmd_node_delete)
    pt = sp.add_parser("token", help="Create registration token for auto node")
    pt.add_argument("--node-name", "-n", required=True, help="Desired node name")
    pt.add_argument("--type", "-t", choices=["auto", "baota"], default="auto")
    pt.set_defaults(func=cmd_node_token)

    # ssl
    p = sub.add_parser("ssl", help="SSL certificate management")
    sp = p.add_subparsers(dest="sub")
    sp.add_parser("list", help="List certificate library").set_defaults(func=cmd_ssl_list)
    pi = sp.add_parser("issue", help="Issue SSL certificate into library")
    pi.add_argument("--domain", "-d", required=True)
    pi.add_argument("--name", "-n", default="", help="Library name (default = domain)")
    pi.add_argument("--self-signed", action="store_true", help="Generate self-signed cert")
    pi.set_defaults(func=cmd_ssl_issue)
    pn = sp.add_parser("info", help="Show SSL certificate info")
    pn.add_argument("--domain", "-d", required=True)
    pn.set_defaults(func=cmd_ssl_info)
    pim = sp.add_parser("import", help="Import cert from allinssl/acme.sh/external")
    pim.add_argument("--domain", "-d", required=True, help="Domain to validate / bind")
    pim.add_argument("--name", "-n", default="", help="Library name (default = domain)")
    pim.add_argument("--cert-dir", default="", help="Directory containing cert and key files")
    pim.add_argument("--cert", default="", help="Fullchain certificate file path")
    pim.add_argument("--key", default="", help="Private key file path")
    pim.set_defaults(func=cmd_ssl_import)

    # tokens
    p = sub.add_parser("tokens", help="List registration tokens")
    p.set_defaults(func=cmd_tokens_list)

    # config preview
    p = sub.add_parser("config", help="Preview node config")
    p.add_argument("--node", "-n", required=True)
    p.set_defaults(func=cmd_config_preview)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
