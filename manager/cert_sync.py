"""Import certificates from a directory (e.g. an AllinSSL deploy target) into
the certificate library and auto-bind them to matching unbound sites.

Shared by the CLI (`ssl sync-dir`) and the Web panel button.
"""

import os


def _pick_cert_file(files):
    low = lambda f: f.lower()
    cands = [f for f in files
             if low(f).endswith((".pem", ".crt", ".cer"))
             and not low(f).endswith(".key")
             and "privkey" not in low(f) and "private" not in low(f)]
    for word in ("fullchain", "chain", "cert"):
        for f in cands:
            if word in low(f):
                return f
    return cands[0] if cands else None


def _pick_key_file(files):
    for word in ("privkey", "private", "key"):
        for f in files:
            low = f.lower()
            if word in low and low.endswith((".pem", ".key")) \
               and "fullchain" not in low and "cert" not in low and "chain" not in low:
                return f
    for f in files:
        if f.lower().endswith(".key") and "csr" not in f.lower():
            return f
    return None


def _choose_primary_domain(domains):
    ds = sorted(domains)
    for d in ds:
        if not d.startswith("*."):
            return d
    return ds[0] if ds else ""


def sync_dir(db, ssl_mgr, root, bind=True):
    """Scan `root` recursively, import certs, optionally bind unbound sites.

    Returns a summary dict:
        {imported, bound, skipped, failed, error, certificates:[{domain,id}]}
    """
    result = {"imported": 0, "bound": 0, "skipped": 0, "failed": 0,
              "error": "", "certificates": []}
    if not root or not os.path.isdir(root):
        result["error"] = f"directory not found: {root}"
        return result

    changed = False
    for dirpath, _dirnames, files in os.walk(root):
        cf = _pick_cert_file(files)
        kf = _pick_key_file(files)
        if not cf or not kf:
            continue
        cpath = os.path.join(dirpath, cf)
        kpath = os.path.join(dirpath, kf)
        try:
            pem = open(cpath, encoding="utf-8", errors="replace").read()
            domains = ssl_mgr.cert_domains(pem)
            primary = _choose_primary_domain(domains)
            if not primary:
                result["skipped"] += 1
                continue

            cert = ssl_mgr.import_cert(domain=primary, cert_path=cpath, key_path=kpath)
            if not cert:
                result["failed"] += 1
                continue
            cert.name = primary

            existing = db.get_certificate_by_name(primary)
            if existing and existing.fullchain_pem == cert.fullchain_pem \
               and existing.privkey_pem == cert.privkey_pem:
                cid = existing.id
            else:
                cert = db.save_certificate(cert)
                cid = cert.id
                changed = True
                result["imported"] += 1
                result["certificates"].append({"domain": primary, "id": cid})

            if bind:
                for site in db.list_sites():
                    if site.certificate_id is None and ssl_mgr.domain_matches(site.domain, domains):
                        site.certificate_id = cid
                        db.update_site(site)
                        changed = True
                        result["bound"] += 1
        except Exception as e:
            result["failed"] += 1
            result["error"] = str(e)

    if changed:
        db.bump_all_node_hashes()
        result["changed"] = True
    else:
        result["changed"] = False
    return result
