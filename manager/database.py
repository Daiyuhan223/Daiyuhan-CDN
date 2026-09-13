import os
import sqlite3
import secrets
import hashlib
import json
from datetime import datetime, timedelta
from typing import Optional, List

from .config import DB_PATH
from .models import Site, Node, Certificate, SSLCertificate, RegistrationToken

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL UNIQUE,
    origin_url TEXT NOT NULL,
    origin_host TEXT NOT NULL DEFAULT '',
    cache_ttl INTEGER DEFAULT 3600,
    ssl_enabled INTEGER DEFAULT 1,
    active INTEGER DEFAULT 1,
    cache_enabled INTEGER DEFAULT 1,
    redirect_url TEXT NOT NULL DEFAULT '',
    force_https INTEGER DEFAULT 0,
    certificate_id INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    ip TEXT NOT NULL DEFAULT '',
    node_type TEXT NOT NULL DEFAULT 'auto',
    api_key TEXT NOT NULL UNIQUE,
    status TEXT DEFAULT 'pending',
    config_hash TEXT DEFAULT '',
    sync_token TEXT DEFAULT '',
    sync_verified INTEGER DEFAULT 0,
    baota_url TEXT DEFAULT '',
    baota_key TEXT DEFAULT '',
    last_seen TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    domains TEXT NOT NULL DEFAULT '',
    fullchain_pem TEXT NOT NULL DEFAULT '',
    privkey_pem TEXT NOT NULL DEFAULT '',
    issued_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ssl_certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL,
    domain TEXT NOT NULL,
    fullchain_pem TEXT NOT NULL DEFAULT '',
    privkey_pem TEXT NOT NULL DEFAULT '',
    issued_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT DEFAULT '',
    FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS registration_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL UNIQUE,
    node_name TEXT NOT NULL DEFAULT '',
    node_type TEXT NOT NULL DEFAULT 'auto',
    used INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _row_to_site(row) -> Site:
    return Site(
        id=row["id"], domain=row["domain"], origin_url=row["origin_url"],
        origin_host=row["origin_host"], cache_ttl=row["cache_ttl"],
        ssl_enabled=bool(row["ssl_enabled"]), active=bool(row["active"]),
        cache_enabled=bool(row["cache_enabled"]),
        redirect_url=row["redirect_url"] or "",
        force_https=bool(row["force_https"]) if "force_https" in row.keys() else False,
        certificate_id=row["certificate_id"] if "certificate_id" in row.keys() and row["certificate_id"] else None,
        created_at=row["created_at"]
    )


def _row_to_certificate(row) -> Certificate:
    return Certificate(
        id=row["id"], name=row["name"], domains=row["domains"] or "",
        fullchain_pem=row["fullchain_pem"] or "", privkey_pem=row["privkey_pem"] or "",
        issued_at=row["issued_at"] or "", expires_at=row["expires_at"] or "",
        created_at=row["created_at"] or ""
    )


def _row_to_node(row) -> Node:
    return Node(
        id=row[0], name=row[1], ip=row[2], node_type=row[3],
        api_key=row[4], status=row[5], config_hash=row[6] or "",
        sync_token=row[7] or "", sync_verified=bool(row[8]),
        baota_url=row[9] or "" if len(row) > 11 else "",
        baota_key=row[10] or "" if len(row) > 11 else "",
        last_seen=row[-2] or "", created_at=row[-1] or ""
    )


def _row_to_cert(row) -> SSLCertificate:
    return SSLCertificate(
        id=row[0], site_id=row[1], domain=row[2],
        fullchain_pem=row[3], privkey_pem=row[4],
        issued_at=row[5], expires_at=row[6] or ""
    )


class Database:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.executescript(SCHEMA)
            # Migration: add redirect_url to sites
            try:
                conn.execute("ALTER TABLE sites ADD COLUMN redirect_url TEXT NOT NULL DEFAULT ''")
            except sqlite3.DatabaseError:
                pass
            # Migration: add cache_enabled to sites
            try:
                conn.execute("ALTER TABLE sites ADD COLUMN cache_enabled INTEGER DEFAULT 1")
            except sqlite3.DatabaseError:
                pass
            # Migration: add force_https to sites
            try:
                conn.execute("ALTER TABLE sites ADD COLUMN force_https INTEGER DEFAULT 0")
            except sqlite3.DatabaseError:
                pass
            # Migration: add certificate_id to sites
            try:
                conn.execute("ALTER TABLE sites ADD COLUMN certificate_id INTEGER")
            except sqlite3.DatabaseError:
                pass
            self._migrate_legacy_certs(conn)
            # Migration: add columns + fix old CHECK constraint
            for col, typ in [("baota_url", "TEXT DEFAULT ''"), ("baota_key", "TEXT DEFAULT ''")]:
                try:
                    conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {typ}")
                except sqlite3.DatabaseError:
                    pass
            # Fix old CHECK(manual) constraint for existing databases
            try:
                conn.execute("INSERT INTO nodes (name, node_type, api_key) VALUES ('_mig_test','baota','_mig_key')")
                conn.execute("DELETE FROM nodes WHERE name='_mig_test'")
            except sqlite3.DatabaseError:
                conn.executescript("""
                    PRAGMA foreign_keys=OFF;
                    DROP TABLE IF EXISTS node_sites;
                    CREATE TABLE nodes_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        ip TEXT NOT NULL DEFAULT '',
                        node_type TEXT NOT NULL DEFAULT 'auto',
                        api_key TEXT NOT NULL UNIQUE,
                        status TEXT DEFAULT 'pending',
                        config_hash TEXT DEFAULT '',
                        sync_token TEXT DEFAULT '',
                        sync_verified INTEGER DEFAULT 0,
                        baota_url TEXT DEFAULT '',
                        baota_key TEXT DEFAULT '',
                        last_seen TEXT DEFAULT '',
                        created_at TEXT DEFAULT (datetime('now'))
                    );
                    INSERT INTO nodes_new SELECT * FROM nodes;
                    DROP TABLE nodes;
                    ALTER TABLE nodes_new RENAME TO nodes;
                    PRAGMA foreign_keys=ON;
                """)

    def _migrate_legacy_certs(self, conn):
        """One-time migration: move old per-site ssl_certificates into the new
        certificates library and bind them to their sites."""
        try:
            has_legacy = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='ssl_certificates'"
            ).fetchone()[0]
            count = conn.execute("SELECT COUNT(*) FROM certificates").fetchone()[0]
            if not has_legacy or count > 0:
                return
            rows = conn.execute(
                "SELECT * FROM ssl_certificates WHERE fullchain_pem != '' AND privkey_pem != ''"
            ).fetchall()
            for r in rows:
                try:
                    name = r["domain"] or f"cert-{r['id']}"
                    cur = conn.execute(
                        """INSERT INTO certificates (name, domains, fullchain_pem, privkey_pem, issued_at, expires_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (name, r["domain"] or "", r["fullchain_pem"], r["privkey_pem"],
                         r["issued_at"], r["expires_at"] or "")
                    )
                    conn.execute(
                        "UPDATE sites SET certificate_id=? WHERE id=?", (cur.lastrowid, r["site_id"])
                    )
                except sqlite3.DatabaseError:
                    continue
        except sqlite3.DatabaseError:
            pass

    # ── Sites ────────────────────────────────────────────────────

    def create_site(self, site: Site) -> Site:
        with self._get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO sites (domain, origin_url, origin_host, cache_ttl, ssl_enabled, cache_enabled, redirect_url, force_https, certificate_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (site.domain, site.origin_url, site.origin_host, site.cache_ttl,
                 int(site.ssl_enabled), int(site.cache_enabled), site.redirect_url,
                 int(site.force_https), site.certificate_id)
            )
            site.id = cur.lastrowid
            return site

    def get_site(self, site_id: int) -> Optional[Site]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
            return _row_to_site(row) if row else None

    def get_site_by_domain(self, domain: str) -> Optional[Site]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM sites WHERE domain=?", (domain,)).fetchone()
            return _row_to_site(row) if row else None

    def list_sites(self) -> List[Site]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM sites ORDER BY id DESC").fetchall()
            return [_row_to_site(r) for r in rows]

    def update_site(self, site: Site):
        with self._get_conn() as conn:
            conn.execute(
                """UPDATE sites SET domain=?, origin_url=?, origin_host=?, cache_ttl=?, ssl_enabled=?, active=?, cache_enabled=?, redirect_url=?, force_https=?, certificate_id=?
                   WHERE id=?""",
                (site.domain, site.origin_url, site.origin_host, site.cache_ttl,
                 int(site.ssl_enabled), int(site.active), int(site.cache_enabled),
                 site.redirect_url, int(site.force_https), site.certificate_id, site.id)
            )

    def delete_site(self, site_id: int):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM sites WHERE id=?", (site_id,))

    # ── Nodes ────────────────────────────────────────────────────

    def create_node(self, node: Node) -> Node:
        with self._get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO nodes (name, ip, node_type, api_key, status, baota_url, baota_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (node.name, node.ip, node.node_type, node.api_key, node.status, node.baota_url, node.baota_key)
            )
            node.id = cur.lastrowid
            return node

    def get_node(self, node_id: int) -> Optional[Node]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            return _row_to_node(row) if row else None

    def get_node_by_name(self, name: str) -> Optional[Node]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM nodes WHERE name=?", (name,)).fetchone()
            return _row_to_node(row) if row else None

    def get_node_by_api_key(self, api_key: str) -> Optional[Node]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM nodes WHERE api_key=?", (api_key,)).fetchone()
            return _row_to_node(row) if row else None

    def list_nodes(self) -> List[Node]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM nodes ORDER BY id DESC").fetchall()
            return [_row_to_node(r) for r in rows]

    def update_node_status(self, node_id: int, status: str, config_hash: str = None):
        with self._get_conn() as conn:
            if config_hash is not None:
                conn.execute(
                    "UPDATE nodes SET status=?, config_hash=?, last_seen=? WHERE id=?",
                    (status, config_hash, _now(), node_id)
                )
            else:
                conn.execute(
                    "UPDATE nodes SET status=?, last_seen=? WHERE id=?",
                    (status, _now(), node_id)
                )

    def update_node_sync_token(self, node_id: int, token: str):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE nodes SET sync_token=?, sync_verified=0 WHERE id=?",
                (token, node_id)
            )

    def verify_node_sync(self, node_id: int, token: str) -> bool:
        with self._get_conn() as conn:
            node = conn.execute(
                "SELECT sync_token FROM nodes WHERE id=?", (node_id,)
            ).fetchone()
            if node and node["sync_token"] and node["sync_token"] == token:
                conn.execute(
                    "UPDATE nodes SET sync_verified=1, last_seen=? WHERE id=?",
                    (_now(), node_id)
                )
                return True
        return False

    def update_node_config_hash(self, node_id: int, config_hash: str):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE nodes SET config_hash=? WHERE id=?",
                (config_hash, node_id)
            )

    def delete_node(self, node_id: int):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM nodes WHERE id=?", (node_id,))

    def update_node(self, node: Node):
        with self._get_conn() as conn:
            conn.execute(
                """UPDATE nodes SET name=?, ip=?, node_type=?, baota_url=?, baota_key=? WHERE id=?""",
                (node.name, node.ip, node.node_type, node.baota_url, node.baota_key, node.id)
            )

    # ── Active sites (all nodes serve all active sites) ─────────

    def list_active_sites(self) -> List[Site]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sites WHERE active = 1 ORDER BY domain"
            ).fetchall()
            return [_row_to_site(r) for r in rows]

    def get_node_sites(self, node_id: int) -> List[Site]:
        return self.list_active_sites()

    def bump_all_node_hashes(self):
        with self._get_conn() as conn:
            new_hash = secrets.token_hex(16)
            conn.execute("UPDATE nodes SET config_hash=?", (new_hash,))

    # ── Certificate Library ──────────────────────────────────────

    def save_certificate(self, cert: Certificate) -> Certificate:
        with self._get_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM certificates WHERE name=?", (cert.name,)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE certificates
                       SET domains=?, fullchain_pem=?, privkey_pem=?, issued_at=?, expires_at=?
                       WHERE id=?""",
                    (cert.domains, cert.fullchain_pem, cert.privkey_pem,
                     cert.issued_at or _now(), cert.expires_at, existing["id"])
                )
                cert.id = existing["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO certificates (name, domains, fullchain_pem, privkey_pem, issued_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (cert.name, cert.domains, cert.fullchain_pem, cert.privkey_pem,
                     cert.issued_at or _now(), cert.expires_at)
                )
                cert.id = cur.lastrowid
            return cert

    def list_certificates(self) -> List[Certificate]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM certificates ORDER BY name").fetchall()
            return [_row_to_certificate(r) for r in rows]

    def get_certificate(self, cert_id: int) -> Optional[Certificate]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM certificates WHERE id=?", (cert_id,)).fetchone()
            return _row_to_certificate(row) if row else None

    def get_certificate_by_name(self, name: str) -> Optional[Certificate]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM certificates WHERE name=?", (name,)).fetchone()
            return _row_to_certificate(row) if row else None

    def delete_certificate(self, cert_id: int):
        with self._get_conn() as conn:
            conn.execute("UPDATE sites SET certificate_id=NULL WHERE certificate_id=?", (cert_id,))
            conn.execute("DELETE FROM certificates WHERE id=?", (cert_id,))

    def certificate_sites(self, cert_id: int) -> List[Site]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sites WHERE certificate_id=?", (cert_id,)
            ).fetchall()
            return [_row_to_site(r) for r in rows]

    def get_site_certificate(self, site_id: int) -> Optional[Certificate]:
        with self._get_conn() as conn:
            row = conn.execute(
                """SELECT c.* FROM certificates c
                   JOIN sites s ON s.certificate_id = c.id
                   WHERE s.id=?""",
                (site_id,)
            ).fetchone()
            return _row_to_certificate(row) if row else None

    def get_expiring_certificates(self, days: int = 30) -> List[Certificate]:
        threshold = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM certificates WHERE expires_at <= ? AND expires_at != ''",
                (threshold,)
            ).fetchall()
            return [_row_to_certificate(r) for r in rows]

    def get_certificate_for_domain(self, domain: str) -> Optional[Certificate]:
        """Best-match a certificate from the library for a domain.
        Prefers explicit binding, then wildcard/name coverage."""
        with self._get_conn() as conn:
            site = conn.execute("SELECT certificate_id FROM sites WHERE domain=?", (domain,)).fetchone()
            if site and site["certificate_id"]:
                row = conn.execute(
                    "SELECT * FROM certificates WHERE id=?", (site["certificate_id"],)
                ).fetchone()
                if row:
                    return _row_to_certificate(row)
        rows = conn.execute("SELECT * FROM certificates").fetchall()
        target = domain.lower()
        for r in rows:
            for d in (r["domains"] or "").split(","):
                d = d.strip().lower()
                if not d:
                    continue
                if d == target:
                    return _row_to_certificate(r)
                if d.startswith("*.") and (target.endswith("." + d[2:]) or target == d[2:]):
                    return _row_to_certificate(r)
        return None

    def site_certificate_id(self, site_id: int) -> Optional[int]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT certificate_id FROM sites WHERE id=?", (site_id,)).fetchone()
            return row["certificate_id"] if row and row["certificate_id"] else None

    # ── Registration Tokens ──────────────────────────────────────

    def create_registration_token(self, node_name: str, node_type: str = "auto") -> RegistrationToken:
        token = secrets.token_urlsafe(32)
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO registration_tokens (token, node_name, node_type)
                   VALUES (?, ?, ?)""",
                (token, node_name, node_type)
            )
        return RegistrationToken(token=token, node_name=node_name, node_type=node_type)

    def use_registration_token(self, token: str) -> Optional[RegistrationToken]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM registration_tokens WHERE token=? AND used=0",
                (token,)
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE registration_tokens SET used=1 WHERE id=?",
                (row["id"],)
            )
            return RegistrationToken(
                id=row["id"], token=row["token"], node_name=row["node_name"],
                node_type=row["node_type"], used=True, created_at=row["created_at"]
            )

    def list_registration_tokens(self) -> List[RegistrationToken]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM registration_tokens ORDER BY id DESC LIMIT 20"
            ).fetchall()
            return [RegistrationToken(
                id=r["id"], token=r["token"], node_name=r["node_name"],
                node_type=r["node_type"], used=bool(r["used"]),
                created_at=r["created_at"]
            ) for r in rows]

    def delete_registration_token(self, token_id: int):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM registration_tokens WHERE id=?", (token_id,))

    # ── Utility ──────────────────────────────────────────────────

    def compute_node_config_hash(self, node_id: int) -> str:
        sites = self.list_active_sites()
        data = json.dumps([
            {"domain": s.domain, "origin_url": s.origin_url, "cache_ttl": s.cache_ttl,
             "ssl_enabled": s.ssl_enabled, "cache_enabled": s.cache_enabled,
             "redirect_url": s.redirect_url, "force_https": s.force_https,
             "certificate_id": s.certificate_id}
            for s in sites
        ], sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()
