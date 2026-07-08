"""cogvault — fleet-grade local memory over plain Markdown. MIT."""
from .core import Vault, Config, apply_tenant_config

__version__ = "0.8.1"
__all__ = ["Vault", "Config", "apply_tenant_config"]
