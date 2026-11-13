import os
import subprocess
import tempfile
from datetime import datetime
from typing import Optional

from .config import ACME_SH_PATH, ACME_DNS_PLUGIN, ACME_DNS_ENV
from .models import Certificate


class SSLManager:
    def __init__(self, acme_sh_path: str = None):
        self.acme_sh = acme_sh_path or ACME_SH_PATH

    def _make(self, name: str, domains: str, cert_pem: str, key_pem: str,
              expires_at) -> Certificate:
        return Certificate(
            name=name,
            domains=domains,
            fullchain_pem=cert_pem,
            privkey_pem=key_pem,
            issued_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            expires_at=expires_at.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def generate_self_signed(self, domain: str) -> Optional[Certificate]:
        tmpdir = tempfile.mkdtemp(prefix="cdnssl_")
        key_path = os.path.join(tmpdir, "privkey.pem")
        cert_path = os.path.join(tmpdir, "fullchain.pem")

        subject = f"/CN={domain}"
        cmd = [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", key_path, "-out", cert_path,
            "-days", "365", "-nodes",
            "-subj", subject,
            "-addext", f"subjectAltName=DNS:{domain}",
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

        with open(cert_path, "r") as f:
            cert_pem = f.read()
        with open(key_path, "r") as f:
            key_pem = f.read()

        os.remove(key_path)
        os.remove(cert_path)
        os.rmdir(tmpdir)

        expires = datetime.utcnow().replace(year=datetime.utcnow().year + 1)
        return self._make(domain, domain, cert_pem, key_pem, expires)

    def issue_acme(self, domain: str, dns_plugin: str = None) -> Optional[Certificate]:
        if not os.path.exists(self.acme_sh):
            return self.generate_self_signed(domain)

        plugin = dns_plugin or ACME_DNS_PLUGIN
        env = os.environ.copy()
        env.update(ACME_DNS_ENV)

        cmd = [
            self.acme_sh, "--issue",
            "--dns", plugin,
            "-d", domain,
            "--keylength", "ec-256",
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return self.generate_self_signed(domain)

        if result.returncode != 0:
            return self.generate_self_signed(domain)

        cert_dir = os.path.join(os.path.dirname(self.acme_sh), domain)
        if not os.path.isdir(cert_dir):
            return self.generate_self_signed(domain)

        cert_path = os.path.join(cert_dir, "fullchain.cer")
        key_path = os.path.join(cert_dir, f"{domain}.key")

        if not os.path.exists(cert_path) or not os.path.exists(key_path):
            return self.generate_self_signed(domain)

        with open(cert_path, "r") as f:
            cert_pem = f.read()
        with open(key_path, "r") as f:
            key_pem = f.read()

        expires = self._get_expiry(cert_pem) or datetime.utcnow().replace(
            year=datetime.utcnow().year + 1)

        return self._make(domain, domain, cert_pem, key_pem, expires)

    def issue_cert(self, domain: str, use_acme: bool = True) -> Optional[Certificate]:
        if use_acme and os.path.exists(self.acme_sh):
            return self.issue_acme(domain)
        return self.generate_self_signed(domain)

    def _get_expiry(self, cert_pem: str) -> Optional[datetime]:
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
                f.write(cert_pem)
                f.flush()
                result = subprocess.run(
                    ["openssl", "x509", "-enddate", "-noout", "-in", f.name],
                    capture_output=True, text=True, timeout=10
                )
            os.unlink(f.name)
            if result.returncode == 0 and "notAfter=" in result.stdout:
                date_str = result.stdout.split("notAfter=")[1].strip()
                from email.utils import parsedate_to_datetime
                return parsedate_to_datetime(date_str)
        except Exception:
            pass
        return None

    def check_expiry(self, cert: Certificate) -> bool:
        if not cert.expires_at:
            return False
        try:
            expires = datetime.strptime(cert.expires_at, "%Y-%m-%d %H:%M:%S")
            return (expires - datetime.utcnow()).days > 30
        except ValueError:
            return False

    def needs_renewal(self, cert: Certificate) -> bool:
        return not self.check_expiry(cert)

    def renew_cert(self, domain: str) -> Optional[Certificate]:
        return self.issue_cert(domain, use_acme=True)

    def import_cert_from_text(self, domain: str, cert_pem: str, key_pem: str) -> Optional[Certificate]:
        return self._build_cert(domain, cert_pem, key_pem)

    def import_cert(self, domain: str, cert_dir: str = "", cert_path: str = "",
                    key_path: str = "") -> Optional[Certificate]:
        if cert_path and key_path:
            cpath, kpath = cert_path, key_path
        else:
            cpath, kpath = self._find_cert_files(cert_dir, domain)

        if not cpath or not kpath:
            if cert_dir and os.path.isdir(cert_dir):
                files = os.listdir(cert_dir)
                print(f"ERROR: No matching cert/key pair found in {cert_dir}")
                print(f"  Files in directory: {files}")
                print(f"  Expected pairs: fullchain.pem/privkey.pem | cert.pem/key.pem | cert.pem/privkey.pem")
                print(f"  Or use --cert and --key to specify exact file paths")
            elif cert_dir:
                print(f"ERROR: Directory not found: {cert_dir}")
            else:
                print(f"ERROR: No cert/key paths provided")
            return None
        if not os.path.exists(cpath):
            print(f"ERROR: cert file missing: {cpath}")
            return None
        if not os.path.exists(kpath):
            print(f"ERROR: key file missing: {kpath}")
            return None

        with open(cpath, "r") as f:
            cert_pem = f.read()
        with open(kpath, "r") as f:
            key_pem = f.read()

        return self._build_cert(domain, cert_pem, key_pem)

    def _build_cert(self, domain: str, cert_pem: str, key_pem: str) -> Optional[Certificate]:
        cert_domains = self._get_cert_domains(cert_pem)
        if cert_domains and not self._domain_matches(domain, cert_domains):
            print(f"ERROR: Certificate domain mismatch!")
            print(f"  Target domain: {domain}")
            print(f"  Cert covers:   {', '.join(sorted(cert_domains))}")
            return None

        issuer = self._get_cert_issuer(cert_pem)
        subject = self._get_cert_subject(cert_pem)
        if issuer and subject and issuer == subject:
            print(f"WARNING: This appears to be a self-signed certificate (Issuer = Subject)")
            print(f"  Subject: {subject}")

        expires = self._get_expiry(cert_pem) or datetime.utcnow().replace(
            year=datetime.utcnow().year + 1)

        domains = ", ".join(sorted(cert_domains)) if cert_domains else domain
        return self._make(domain, domains, cert_pem, key_pem, expires)

    def _find_cert_files(self, cert_dir: str, domain: str):
        cpath, kpath = "", ""
        if not os.path.isdir(cert_dir):
            return cpath, kpath

        for fname, fkey in [
            ("fullchain.pem", "privkey.pem"),
            ("fullchain.cer", f"{domain}.key"),
            ("fullchain.pem", f"{domain}.key"),
            ("cert.pem", "privkey.pem"),
            ("cert.pem", "key.pem"),
            (f"{domain}.crt", f"{domain}.key"),
            (f"{domain}_fullchain.pem", f"{domain}_privkey.pem"),
        ]:
            c = os.path.join(cert_dir, fname)
            k = os.path.join(cert_dir, fkey)
            if os.path.exists(c) and os.path.exists(k):
                cpath, kpath = c, k
                break

        if not cpath:
            for root, _, files in os.walk(cert_dir):
                for f in files:
                    if f.endswith((".pem", ".cer", ".crt")) and ("fullchain" in f or "cert" in f):
                        cpath = os.path.join(root, f)
                    if f.endswith((".pem", ".key")) and ("privkey" in f or "key" in f):
                        kpath = os.path.join(root, f)
                if cpath and kpath:
                    break

        return cpath, kpath

    def cert_domains(self, cert_pem: str):
        return self._get_cert_domains(cert_pem)

    def domain_matches(self, domain: str, domains) -> bool:
        return self._domain_matches(domain, domains)

    def _get_cert_domains(self, cert_pem: str):
        domains = set()
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
                f.write(cert_pem)
                f.flush()
                result = subprocess.run(
                    ["openssl", "x509", "-text", "-noout", "-in", f.name],
                    capture_output=True, text=True, timeout=10
                )
            os.unlink(f.name)
            if result.returncode == 0:
                text = result.stdout
                for line in text.split("\n"):
                    line = line.strip()
                    if "DNS:" in line:
                        for part in line.split(","):
                            part = part.strip()
                            if part.startswith("DNS:"):
                                domains.add(part[4:].lower())
                import re
                cn = re.search(r"Subject:.*CN\s*=\s*([^\s,]+)", text)
                if cn:
                    domains.add(cn.group(1).lower())
        except Exception:
            pass
        return domains

    def _domain_matches(self, target: str, cert_domains: set) -> bool:
        if not cert_domains:
            return True
        target = target.lower()
        targets = [target]
        if target.startswith("*."):
            targets.append(target[2:])
        for d in cert_domains:
            d = d.lower()
            for t in targets:
                if d == t:
                    return True
                if d.startswith("*."):
                    suffix = d[2:]
                    if t.endswith("." + suffix) or t == suffix:
                        return True
        return False

    def _get_cert_issuer(self, cert_pem: str) -> str:
        return self._get_cert_field(cert_pem, "issuer")

    def _get_cert_subject(self, cert_pem: str) -> str:
        return self._get_cert_field(cert_pem, "subject")

    def _get_cert_field(self, cert_pem: str, field: str) -> str:
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
                f.write(cert_pem)
                f.flush()
                result = subprocess.run(
                    ["openssl", "x509", "-noout", f"-{field}", "-in", f.name],
                    capture_output=True, text=True, timeout=10
                )
            os.unlink(f.name)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""
