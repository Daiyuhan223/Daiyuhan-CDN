from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Site:
    id: Optional[int] = None
    domain: str = ""
    origin_url: str = ""
    origin_host: str = ""
    cache_ttl: int = 3600
    ssl_enabled: bool = True
    active: bool = True
    cache_enabled: bool = True
    redirect_url: str = ""
    force_https: bool = False
    certificate_id: Optional[int] = None
    created_at: str = ""

    def __post_init__(self):
        if not self.origin_host and self.origin_url:
            self.origin_host = "$host"


@dataclass
class Certificate:
    id: Optional[int] = None
    name: str = ""
    domains: str = ""
    fullchain_pem: str = ""
    privkey_pem: str = ""
    issued_at: str = ""
    expires_at: str = ""
    created_at: str = ""


@dataclass
class Node:
    id: Optional[int] = None
    name: str = ""
    ip: str = ""
    node_type: str = "auto"
    api_key: str = ""
    status: str = "pending"
    config_hash: str = ""
    sync_token: str = ""
    sync_verified: bool = False
    baota_url: str = ""
    baota_key: str = ""
    last_seen: str = ""
    created_at: str = ""


@dataclass
class SSLCertificate:
    id: Optional[int] = None
    site_id: int = 0
    domain: str = ""
    fullchain_pem: str = ""
    privkey_pem: str = ""
    issued_at: str = ""
    expires_at: str = ""


@dataclass
class RegistrationToken:
    id: Optional[int] = None
    token: str = ""
    node_name: str = ""
    node_type: str = "auto"
    used: bool = False
    created_at: str = ""


@dataclass
class SyncResponse:
    action: str = "none"
    config_hash: str = ""
    nginx_base: str = ""
    sites: list = field(default_factory=list)
    certs: list = field(default_factory=list)
    message: str = ""
