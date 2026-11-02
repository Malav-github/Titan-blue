"""Connector manifests: the only way an API enters Titan.

A manifest is data, validated in code, approved by a human, then versioned and never edited. It
names exactly one host, read-only (GET) endpoints and their parameters. The environment variable
holding the API key is derived from the connector id, so a manifest can never point at some other
secret (e.g. ANTHROPIC_API_KEY) and send it to its own host.
"""

import ipaddress
import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
ENDPOINT_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,40}$")
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
MAX_ENDPOINTS = 20


class Param(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str
    in_: Literal["path", "query"] = Field("query", alias="in")
    required: bool = False
    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not PARAM_RE.match(v):
            raise ValueError(f"bad parameter name {v!r}")
        return v


class Endpoint(BaseModel):
    name: str
    path: str
    description: str
    params: list[Param] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not ENDPOINT_RE.match(v):
            raise ValueError(f"bad endpoint name {v!r}")
        return v

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not v.startswith("/") or ".." in v or "//" in v or "?" in v or "#" in v or "\\" in v or ":" in v.split("{")[0]:
            raise ValueError(f"unsafe endpoint path {v!r}")
        return v

    @model_validator(mode="after")
    def _placeholders_match_params(self):
        in_path = set(PLACEHOLDER_RE.findall(self.path))
        declared = {p.name for p in self.params if p.in_ == "path"}
        if in_path != declared:
            raise ValueError(f"path placeholders {sorted(in_path)} do not match path parameters {sorted(declared)}")
        for p in self.params:
            if p.in_ == "path":
                p.required = True
        if len({p.name for p in self.params}) != len(self.params):
            raise ValueError("duplicate parameter names")
        return self


class Auth(BaseModel):
    type: Literal["none", "header", "bearer", "query"] = "none"
    name: str | None = Field(None, description="Header or query-parameter name carrying the key")

    @model_validator(mode="after")
    def _needs_name(self):
        if self.type in ("header", "query") and not (self.name and PARAM_RE.match(self.name)):
            raise ValueError(f"auth type {self.type} needs a valid name")
        return self


class RateLimit(BaseModel):
    requests_per_minute: int = Field(30, ge=1, le=600)


class Budget(BaseModel):
    max_calls_per_run: int = Field(30, ge=1, le=1000)
    max_calls_per_day: int = Field(500, ge=1, le=100_000)
    cost_per_call_usd: float | None = Field(None, ge=0)
    max_cost_per_day_usd: float | None = Field(None, ge=0)


class Terms(BaseModel):
    storage_allowed: bool | None = Field(None, description="May responses be stored? null means unknown")
    url: str | None = None
    notes: str = ""


class Evidence(BaseModel):
    """A requirement fact with the verbatim quote that backs it, taken from a page we fetched."""
    field: str
    value: str
    quote: str
    source_url: str
    source_sha256: str | None = None
    retrieved_at: str


class Requirements(BaseModel):
    signup_url: str | None = None
    needs_account: bool | None = None
    needs_identity: bool | None = None
    needs_payment: bool | None = None
    free_tier: str | None = None
    pricing_notes: str | None = None
    rate_limit_note: str | None = None
    auth_note: str | None = None
    integration: Literal["rest", "mcp"] = "rest"
    unverified: list[str] = Field(default_factory=list, description="Fields no fetched page backed with a quote")
    evidence: list[Evidence] = Field(default_factory=list)


class SmokeTest(BaseModel):
    endpoint: str
    params: dict[str, str] = Field(default_factory=dict)


class Manifest(BaseModel):
    connector_id: str
    name: str
    description: str
    topics: list[str] = Field(min_length=1, description="What data this API provides")
    base_url: str
    tier: Literal["primary", "regulatory", "secondary"] = "secondary"
    auth: Auth = Field(default_factory=Auth)
    endpoints: list[Endpoint] = Field(min_length=1, max_length=MAX_ENDPOINTS)
    rate_limit: RateLimit = Field(default_factory=RateLimit)
    budget: Budget = Field(default_factory=Budget)
    terms: Terms = Field(default_factory=Terms)
    requirements: Requirements = Field(default_factory=Requirements)
    smoke_test: SmokeTest

    @field_validator("connector_id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError("connector_id must be lowercase letters, digits, underscore (2-31 chars)")
        return v

    @field_validator("base_url")
    @classmethod
    def _base(cls, v: str) -> str:
        p = urlparse(v)
        if p.scheme != "https" or not p.hostname or p.username or p.password or p.port or p.query or p.fragment:
            raise ValueError("base_url must be a plain https URL with a host (no credentials, port or query)")
        try:
            ipaddress.ip_address(p.hostname)
        except ValueError:
            return v.rstrip("/")
        raise ValueError("base_url must use a hostname, not an IP address")

    @model_validator(mode="after")
    def _consistent(self):
        names = [e.name for e in self.endpoints]
        if len(set(names)) != len(names):
            raise ValueError("duplicate endpoint names")
        endpoint = next((e for e in self.endpoints if e.name == self.smoke_test.endpoint), None)
        if endpoint is None:
            raise ValueError(f"smoke_test endpoint {self.smoke_test.endpoint!r} is not declared")
        declared = {p.name: p for p in endpoint.params}
        unknown = set(self.smoke_test.params) - set(declared)
        missing = {n for n, p in declared.items() if p.required} - set(self.smoke_test.params)
        if unknown or missing:
            raise ValueError(f"smoke_test params: unknown {sorted(unknown)}, missing {sorted(missing)}")
        return self

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname

    @property
    def key_env(self) -> str | None:
        """Where the key lives. Derived, never chosen by the manifest."""
        return None if self.auth.type == "none" else f"{self.connector_id.upper()}_API_KEY"

    def endpoint(self, name: str) -> Endpoint:
        for e in self.endpoints:
            if e.name == name:
                return e
        raise KeyError(f"{self.connector_id} has no endpoint {name!r}; available: {[e.name for e in self.endpoints]}")
