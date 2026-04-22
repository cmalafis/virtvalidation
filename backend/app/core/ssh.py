"""
SSH Collection Engine
Connects to VMs via Ed25519 keys, collects system state for
pre-migration baseline capture and post-migration validation.
"""
import paramiko
from pathlib import Path


class SSHCollector:
    def __init__(self, key_path: str = "/app/keys/id_ed25519"):
        self.key_path = Path(key_path)

    def collect(self, host: str, username: str = "virtvalidate") -> dict:
        """SSH into a VM and collect full system state."""
        raise NotImplementedError("SSH collection engine — coming in v0.2")
