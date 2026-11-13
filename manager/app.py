import os
import secrets
from functools import wraps
from flask import Flask, request, jsonify, render_template, Response, session, redirect

from .config import MANAGER_HOST, MANAGER_PORT, PANEL_USER, PANEL_PASS, get_panel_credentials
from .database import Database
from .models import Site, Node, RegistrationToken
from .auth import init_auth, require_api_key
from .nginx_gen import generate_node_config
from .ssl_manager import SSLManager

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
db = Database()
ssl_mgr = SSLManager()

init_auth(db)


def require_panel(f):
    @wraps(f)
    def decorated(*a, **kw):
        if not session.get("logged_in"):
            return redirect("/cdn-gui/login")
        return f(*a, **kw)
    return decorated


def _site_to_dict(site: Site) -> dict:
    return {
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
        "custom_conf": site.custom_conf,
        "custom_location_conf": site.custom_location_conf,
        "created_at": site.created_at,
    }


def _node_to_dict(node: Node) -> dict:
    return {
        "id": node.id,
        "name": node.name,
        "ip": node.ip,
        "node_type": node.node_type,
        "status": node.status,
        "last_seen": node.last_seen,
        "config_hash": node.config_hash,
        "sync_verified": node.sync_verified,
        "baota_url": node.baota_url,
        "has_baota_key": bool(node.baota_key),
        "created_at": node.created_at,
    }


def _cert_to_dict(cert) -> dict:
    sites = db.certificate_sites(cert.id) if cert.id else []
    return {
        "id": cert.id,
        "name": cert.name,
        "domains": cert.domains,
        "issued_at": cert.issued_at,
        "expires_at": cert.expires_at,
        "needs_renewal": ssl_mgr.needs_renewal(cert),
        "bound_sites": [s.domain for s in sites],
    }


# ═══════════════════════════════════════════════════════════════
#  Sites API
# ═══════════════════════════════════════════════════════════════

@app.route("/api/v1/sites", methods=["GET"])
def list_sites():
    sites = db.list_sites()
    return jsonify({"sites": [_site_to_dict(s) for s in sites]})


@app.route("/api/v1/sites", methods=["POST"])
def create_site():
    data = request.get_json() or {}
    domain = data.get("domain", "").strip().lower()
    origin_url = data.get("origin_url", "").strip()
    redirect_url = data.get("redirect_url", "").strip()

    if not domain:
        return jsonify({"error": "domain is required"}), 400
    if not origin_url and not redirect_url:
        return jsonify({"error": "origin_url or redirect_url is required"}), 400

    existing = db.get_site_by_domain(domain)
    if existing:
        return jsonify({"error": f"site {domain} already exists", "id": existing.id}), 409

    site = Site(
        domain=domain,
        origin_url=origin_url,
        redirect_url=redirect_url,
        cache_ttl=data.get("cache_ttl", 3600),
        ssl_enabled=data.get("ssl_enabled", True),
        cache_enabled=data.get("cache_enabled", True),
        force_https=data.get("force_https", False),
        certificate_id=data.get("certificate_id") or None,
        custom_conf=data.get("custom_conf", "") or "",
        custom_location_conf=data.get("custom_location_conf", "") or "",
    )
    site = db.create_site(site)
    db.bump_all_node_hashes()

    return jsonify({"site": _site_to_dict(site)}), 201


@app.route("/api/v1/sites/<int:site_id>", methods=["GET"])
def get_site(site_id):
    site = db.get_site(site_id)
    if not site:
        return jsonify({"error": "site not found"}), 404
    return jsonify({"site": _site_to_dict(site)})


@app.route("/api/v1/sites/<int:site_id>", methods=["PUT"])
def update_site(site_id):
    site = db.get_site(site_id)
    if not site:
        return jsonify({"error": "site not found"}), 404

    data = request.get_json() or {}
    for field in ["domain", "origin_url", "origin_host", "cache_ttl", "ssl_enabled", "active",
                  "cache_enabled", "redirect_url", "force_https", "certificate_id",
                  "custom_conf", "custom_location_conf"]:
        if field in data:
            if field == "certificate_id":
                setattr(site, field, data[field] or None)
            else:
                setattr(site, field, data[field])

    db.update_site(site)
    db.bump_all_node_hashes()
    return jsonify({"site": _site_to_dict(site)})


@app.route("/api/v1/sites/<int:site_id>", methods=["DELETE"])
def delete_site(site_id):
    site = db.get_site(site_id)
    if not site:
        return jsonify({"error": "site not found"}), 404
    db.delete_site(site_id)
    db.bump_all_node_hashes()
    return jsonify({"deleted": True})


# ═══════════════════════════════════════════════════════════════
#  Nodes API
# ═══════════════════════════════════════════════════════════════

@app.route("/api/v1/nodes", methods=["GET"])
def list_nodes():
    nodes = db.list_nodes()
    result = []
    for n in nodes:
        d = _node_to_dict(n)
        active = db.list_active_sites()
        d["site_count"] = len(active)
        d["sites"] = [s.domain for s in active]
        result.append(d)
    return jsonify({"nodes": result})


@app.route("/api/v1/nodes", methods=["POST"])
def create_node():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    node_type = data.get("node_type", "auto")

    if not name:
        return jsonify({"error": "name is required"}), 400

    if node_type not in ("auto", "baota"):
        return jsonify({"error": "node_type must be auto or baota"}), 400

    existing = db.get_node_by_name(name)
    if existing:
        return jsonify({"error": f"node {name} already exists", "id": existing.id}), 409

    node = Node(
        name=name,
        ip=data.get("ip", ""),
        node_type=node_type,
        api_key=secrets.token_urlsafe(32),
        status="pending",
        baota_url=data.get("baota_url", ""),
        baota_key=data.get("baota_key", ""),
    )
    node = db.create_node(node)

    return jsonify({
        "node": _node_to_dict(node),
        "api_key": node.api_key,
    }), 201


@app.route("/api/v1/nodes/<int:node_id>", methods=["PUT"])
def update_node(node_id):
    node = db.get_node(node_id)
    if not node:
        return jsonify({"error": "node not found"}), 404

    data = request.get_json() or {}
    name = (data.get("name") or node.name).strip()
    node_type = data.get("node_type") or node.node_type

    if node_type not in ("auto", "baota"):
        return jsonify({"error": "node_type must be auto or baota"}), 400

    dup = db.get_node_by_name(name)
    if dup and dup.id != node.id:
        return jsonify({"error": f"node {name} already exists", "id": dup.id}), 409

    node.name = name
    node.ip = data.get("ip", node.ip) or ""
    node.node_type = node_type
    node.baota_url = data.get("baota_url", node.baota_url) or ""
    if data.get("baota_key"):
        node.baota_key = data["baota_key"]

    db.update_node(node)
    db.bump_all_node_hashes()
    return jsonify({"node": _node_to_dict(node)})


@app.route("/api/v1/nodes/<int:node_id>", methods=["DELETE"])
def delete_node(node_id):
    node = db.get_node(node_id)
    if not node:
        return jsonify({"error": "node not found"}), 404
    db.delete_node(node_id)
    return jsonify({"deleted": True})


# ═══════════════════════════════════════════════════════════════
#  Node Sync (polled by agents)
# ═══════════════════════════════════════════════════════════════

@app.route("/api/v1/node/sync", methods=["GET"])
@require_api_key
def node_sync(_node=None):
    node = _node

    client_hash = request.args.get("hash", "")
    current_hash = node.config_hash

    if client_hash == current_hash and current_hash:
        db.update_node_status(node.id, "online", current_hash)
        return jsonify({"action": "none", "config_hash": current_hash, "message": "up to date"})

    sites = db.get_node_sites(node.id)
    ssl_certs_map = {}
    for site in sites:
        if site.ssl_enabled:
            cert = db.get_site_certificate(site.id) or db.get_certificate_for_domain(site.domain)
            if cert:
                ssl_certs_map[site.id] = (cert.fullchain_pem, cert.privkey_pem)

    config = generate_node_config(node, sites, ssl_certs_map)

    sync_token = secrets.token_hex(4)
    config["sync_token"] = sync_token

    db.update_node_config_hash(node.id, config["config_hash"])
    db.update_node_sync_token(node.id, sync_token)
    db.update_node_status(node.id, "online", config["config_hash"])

    return jsonify(config)


@app.route("/api/v1/node/status", methods=["POST"])
@require_api_key
def node_status(_node=None):
    node = _node
    data = request.get_json() or {}
    status = data.get("status", "online")

    # Self-correct node type from the node's own detection (fixes legacy nodes
    # whose stored type was wrong -> wrong ssl/cache paths). Force a re-sync.
    detected = data.get("detected_type", "")
    if detected in ("auto", "baota") and detected != node.node_type:
        node.node_type = detected
        db.update_node(node)
        db.update_node_config_hash(node.id, None)
        app.logger.warning("node %s type corrected to '%s', forcing re-sync", node.name, detected)

    db.update_node_status(node.id, status)
    return jsonify({"status": "updated", "node_type": node.node_type})


@app.route("/api/v1/node/verify", methods=["POST"])
@require_api_key
def node_verify(_node=None):
    node = _node
    data = request.get_json() or {}
    token = data.get("sync_token", "")
    ok = db.verify_node_sync(node.id, token)
    return jsonify({"verified": ok})


# ═══════════════════════════════════════════════════════════════
#  Registration Tokens
# ═══════════════════════════════════════════════════════════════

@app.route("/api/v1/tokens", methods=["POST"])
def create_token():
    data = request.get_json() or {}
    node_name = data.get("node_name", "").strip()
    node_type = data.get("node_type", "auto")
    if not node_name:
        return jsonify({"error": "node_name is required"}), 400
    token = db.create_registration_token(node_name, node_type)
    return jsonify({
        "token": token.token,
        "node_name": token.node_name,
        "node_type": token.node_type,
    }), 201


@app.route("/api/v1/tokens", methods=["GET"])
def list_tokens():
    tokens = db.list_registration_tokens()
    return jsonify({"tokens": [
        {"id": t.id, "token": t.token[:8] + "***", "node_name": t.node_name,
         "node_type": t.node_type, "used": t.used, "created_at": t.created_at}
        for t in tokens
    ]})


@app.route("/api/v1/nodes/register", methods=["POST"])
def register_node():
    data = request.get_json() or {}
    token_str = data.get("token", "").strip()
    node_ip = data.get("ip", request.remote_addr or "")
    detected = data.get("detected_type", "")

    token = db.use_registration_token(token_str)
    if not token:
        return jsonify({"error": "invalid or used token"}), 403

    existing = db.get_node_by_name(token.node_name)
    if existing:
        return jsonify({"error": "node already exists, use sync endpoint", "api_key": existing.api_key}), 409

    # Trust the node's on-the-ground detection over the token's declared type.
    node_type = detected if detected in ("auto", "baota") else token.node_type
    if detected in ("auto", "baota") and detected != token.node_type:
        app.logger.warning("node %s detected as '%s' (token said '%s'); using '%s'",
                           token.node_name, detected, token.node_type, node_type)

    node = Node(
        name=token.node_name,
        ip=node_ip,
        node_type=node_type,
        api_key=secrets.token_urlsafe(32),
        status="online",
    )
    node = db.create_node(node)
    db.bump_all_node_hashes()

    return jsonify({
        "node_id": node.id,
        "node_name": node.name,
        "node_type": node.node_type,
        "api_key": node.api_key,
        "sites_assigned": len(db.get_node_sites(node.id)),
        "message": "node registered successfully",
    }), 201


# ═══════════════════════════════════════════════════════════════
#  Certificate Library API
# ═══════════════════════════════════════════════════════════════

@app.route("/api/v1/certificates", methods=["GET"])
def list_certificates():
    certs = db.list_certificates()
    return jsonify({"certificates": [_cert_to_dict(c) for c in certs]})


@app.route("/api/v1/certificates/issue", methods=["POST"])
def issue_certificate():
    data = request.get_json() or {}
    domain = (data.get("domain") or "").strip().lower()
    if not domain:
        return jsonify({"error": "domain is required"}), 400

    cert = ssl_mgr.issue_cert(domain, use_acme=data.get("use_acme", True))
    if not cert:
        return jsonify({"error": "failed to issue certificate"}), 500

    name = (data.get("name") or "").strip() or domain
    cert.name = name
    cert = db.save_certificate(cert)
    db.bump_all_node_hashes()

    return jsonify({"certificate": _cert_to_dict(cert)}), 201


@app.route("/api/v1/certificates/import", methods=["POST"])
def import_certificate():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    domain = (data.get("domain") or name or "").strip().lower()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not domain:
        return jsonify({"error": "domain is required (used for SAN validation)"}), 400

    cert_pem = data.get("cert_pem", "")
    key_pem = data.get("key_pem", "")
    if cert_pem and key_pem:
        cert = ssl_mgr.import_cert_from_text(domain, cert_pem, key_pem)
    else:
        cert = ssl_mgr.import_cert(
            domain=domain,
            cert_dir=data.get("cert_dir", ""),
            cert_path=data.get("cert_path", ""),
            key_path=data.get("key_path", ""),
        )

    if not cert:
        return jsonify({"error": "failed to import certificate"}), 400

    cert.name = name
    cert = db.save_certificate(cert)
    db.bump_all_node_hashes()

    return jsonify({"certificate": _cert_to_dict(cert)}), 201


@app.route("/api/v1/certificates/<int:cert_id>/renew", methods=["POST"])
def renew_certificate(cert_id):
    cert = db.get_certificate(cert_id)
    if not cert:
        return jsonify({"error": "certificate not found"}), 404

    domain = cert.name.lower()
    if "." not in domain:
        # name was a custom label, not a domain — renew against a SAN/CN
        parts = [d.strip() for d in (cert.domains or "").split(",") if d.strip()]
        if parts:
            domain = parts[0]
    new_cert = ssl_mgr.issue_cert(domain, use_acme=True)
    if not new_cert:
        return jsonify({"error": "renewal failed"}), 500

    new_cert.name = cert.name
    new_cert = db.save_certificate(new_cert)
    db.bump_all_node_hashes()

    return jsonify({"certificate": _cert_to_dict(new_cert)})


@app.route("/api/v1/certificates/<int:cert_id>", methods=["DELETE"])
def delete_certificate(cert_id):
    cert = db.get_certificate(cert_id)
    if not cert:
        return jsonify({"error": "certificate not found"}), 404
    db.delete_certificate(cert_id)
    db.bump_all_node_hashes()
    return jsonify({"deleted": True})


@app.route("/api/v1/sites/<int:site_id>/ssl/bind", methods=["POST"])
def bind_site_ssl(site_id):
    site = db.get_site(site_id)
    if not site:
        return jsonify({"error": "site not found"}), 404

    cert_id = (request.get_json() or {}).get("certificate_id")
    cert_id = int(cert_id) if cert_id else None
    if cert_id and not db.get_certificate(cert_id):
        return jsonify({"error": "certificate not found"}), 404

    site.certificate_id = cert_id
    db.update_site(site)
    db.bump_all_node_hashes()
    return jsonify({"certificate_id": cert_id})


@app.route("/api/v1/sites/<int:site_id>/ssl", methods=["GET"])
def get_ssl(site_id):
    cert = db.get_site_certificate(site_id)
    if not cert:
        return jsonify({"error": "no certificate bound to this site", "certificate": None}), 404
    return jsonify({
        "domain": cert.name,
        "issued_at": cert.issued_at,
        "expires_at": cert.expires_at,
        "has_cert": bool(cert.fullchain_pem),
        "needs_renewal": ssl_mgr.needs_renewal(cert),
    })


# ═══════════════════════════════════════════════════════════════
#  Install Script (served to auto nodes)
# ═══════════════════════════════════════════════════════════════

@app.route("/install.sh", methods=["GET"])
def serve_install_script():
    token = request.args.get("token", "")
    manager_url = request.args.get("manager", f"http://{request.host}")

    script_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "agent", "install.sh"
    )
    try:
        with open(script_path, "r") as f:
            content = f.read()
    except FileNotFoundError:
        content = "#!/bin/bash\necho 'install.sh not found on manager'\nexit 1\n"

    content = f'MANAGER_URL="{manager_url}"\nREGISTER_TOKEN="{token}"\n' + content

    return Response(content, mimetype="text/x-shellscript")


@app.route("/api/v1/agent/download", methods=["GET"])
def download_agent():
    agent_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "agent", "cdn_agent.py"
    )
    try:
        with open(agent_path, "r") as f:
            content = f.read()
    except FileNotFoundError:
        return jsonify({"error": "agent not found"}), 404

    return Response(content, mimetype="text/x-python")


# ═══════════════════════════════════════════════════════════════
#  Health
# ═══════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "server": "cdn-manager",
        "sites": len(db.list_sites()),
        "nodes": len(db.list_nodes()),
        "certs": len(db.list_certificates()),
    })


# ═══════════════════════════════════════════════════════════════
#  Web GUI
# ═══════════════════════════════════════════════════════════════

@app.route("/cdn-gui/login", methods=["GET", "POST"])
def panel_login():
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        cu, cp = get_panel_credentials()
        if u == cu and p == cp:
            session["logged_in"] = True
            return redirect("/cdn-gui/")
        return render_template("login.html", error="用户名或密码错误")
    return render_template("login.html")


@app.route("/cdn-gui/logout")
def panel_logout():
    session.clear()
    return redirect("/cdn-gui/login")


@app.route("/cdn-gui/")
@require_panel
def serve_gui():
    return render_template("dashboard.html")


def run_manager(host=None, port=None):
    db.bump_all_node_hashes()
    app.run(
        host=host or MANAGER_HOST,
        port=port or MANAGER_PORT,
        debug=False,
    )


if __name__ == "__main__":
    run_manager()
