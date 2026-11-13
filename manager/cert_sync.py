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


def _domains_set(domains_str):
    return frozenset(d.strip().lower() for d in (domains_str or "").split(",") if d.strip())


def _match_existing(db, cert):
    """Find an existing library entry for this cert: first by identical content,
    then by identical domain set (so a re-issued cert updates in place instead
    of creating a duplicate under a different name)."""
    certs = db.list_certificates()
    for c in certs:
        if c.fullchain_pem == cert.fullchain_pem and c.privkey_pem == cert.privkey_pem:
            return c
    ds = _domains_set(cert.domains)
    if ds:
        for c in certs:
            if _domains_set(c.domains) == ds:
                return c
    return None


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

            # Match an existing entry by content / domain-set and update it in
            # place (prevents duplicates like "*.example.com" vs "example.com").
            match = _match_existing(db, cert)
            if match:
                same = (match.fullchain_pem == cert.fullchain_pem
                        and match.privkey_pem == cert.privkey_pem)
                if same:
                    cid = match.id
                else:
                    cert.name = match.name
                    cert.id = match.id
                    cert = db.save_certificate(cert)
                    cid = cert.id
                    changed = True
                    result["imported"] += 1
                    result["certificates"].append({"domain": match.name, "id": cid})
            else:
                cert.name = primary
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


def dedup_certificates(db):
    """Merge certificate-library entries that describe the same cert set.

    Groups by the exact domain set; per group keeps the entry bound to the most
    sites (ties -> smallest id), copies the newest content into it, rebinds all
    sites to the keeper, then deletes the duplicates.
    """
    certs = db.list_certificates()
    groups = {}
    for c in certs:
        key = _domains_set(c.domains)
        if key:
            groups.setdefault(key, []).append(c)

    merged_groups = 0
    deleted = 0
    for _key, group in groups.items():
        if len(group) < 2:
            continue

        def bound_count(c):
            return len(db.certificate_sites(c.id)) if c.id else 0

        keeper = max(group, key=lambda c: (bound_count(c), -(c.id or 0)))
        newest = max(group, key=lambda c: c.issued_at or "")

        # ensure keeper holds the newest content
        if group and (keeper.fullchain_pem != newest.fullchain_pem
                      or keeper.privkey_pem != newest.privkey_pem):
            keeper.fullchain_pem = newest.fullchain_pem
            keeper.privkey_pem = newest.privkey_pem
            keeper.domains = newest.domains
            keeper.issued_at = newest.issued_at or keeper.issued_at
            keeper.expires_at = newest.expires_at or keeper.expires_at
            db.save_certificate(keeper)

        for other in group:
            if other.id == keeper.id:
                continue
            for site in db.certificate_sites(other.id):
                site.certificate_id = keeper.id
                db.update_site(site)
            db.delete_certificate(other.id)
            deleted += 1
        merged_groups += 1

    if deleted:
        db.bump_all_node_hashes()
    return {"groups_merged": merged_groups, "deleted": deleted}
