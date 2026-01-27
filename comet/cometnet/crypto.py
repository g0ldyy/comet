"""
CometNet Cryptographic Identity Module

Manages node identity using ECDSA (SECP256k1) keys for:
- Unique node identification
- Message signing
- Signature verification
"""

import hashlib
import os
from pathlib import Path
from typing import Optional

import aiofiles
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import (
    EllipticCurvePrivateKey, EllipticCurvePublicKey)

from comet.cometnet.utils import run_in_executor
from comet.core.logger import logger
from comet.core.models import settings


class NodeIdentity:
    """
    Manages the cryptographic identity of a CometNet node.

    Each node has:
    - A private key (stored locally, never shared, optionally encrypted with a password)
    - A public key (shared with peers)
    - A node ID (SHA256 hash of the public key, used as identifier)
    """

    KEYS_DIR = Path("data/cometnet")
    PRIVATE_KEY_FILE = "node_private_key.pem"

    def __init__(self, keys_dir: Optional[Path] = None):
        self._keys_dir = keys_dir or self.KEYS_DIR
        self._private_key: Optional[EllipticCurvePrivateKey] = None
        self._public_key: Optional[EllipticCurvePublicKey] = None
        self._node_id: Optional[str] = None

    @property
    def node_id(self) -> str:
        """Returns the node ID (hex string of public key hash)."""
        if self._node_id is None:
            raise RuntimeError(
                "Node identity not initialized. Call load_or_generate() first."
            )
        return self._node_id

    @property
    def public_key_bytes(self) -> bytes:
        """Returns the public key as DER-encoded bytes."""
        if self._public_key is None:
            raise RuntimeError(
                "Node identity not initialized. Call load_or_generate() first."
            )
        return self._public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    @property
    def public_key_hex(self) -> str:
        """Returns the public key as a hex string."""
        return self.public_key_bytes.hex()

    async def load_or_generate(self) -> None:
        """
        Load existing keys from disk or generate new ones.
        This must be called before using any other methods.
        """
        key_path = self._keys_dir / self.PRIVATE_KEY_FILE

        if key_path.exists():
            await self._load_keys(key_path)
            logger.log(
                "COMETNET", f"Loaded existing node identity: {self._node_id[:8]}"
            )
        else:
            await self._generate_keys(key_path)
            logger.log("COMETNET", f"Generated new node identity: {self._node_id[:8]}")

    async def _generate_keys(self, key_path: Path) -> None:
        """Generate a new ECDSA key pair and save to disk."""
        # Generate new key pair
        self._private_key = ec.generate_private_key(ec.SECP256K1())
        self._public_key = self._private_key.public_key()

        # Derive node ID from public key
        self._node_id = self._derive_node_id(self._public_key)

        # Ensure directory exists
        key_path.parent.mkdir(parents=True, exist_ok=True)

        # Determine encryption algorithm based on settings
        key_password = settings.COMETNET_KEY_PASSWORD
        if key_password:
            encryption = serialization.BestAvailableEncryption(
                key_password.encode("utf-8")
            )
            logger.log("COMETNET", "Private key will be encrypted with password")
        else:
            encryption = serialization.NoEncryption()

        # Save private key to disk (PEM format)
        async with aiofiles.open(key_path, "wb") as f:
            await f.write(
                self._private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=encryption,
                )
            )

        # Set restrictive permissions (owner read/write only)
        os.chmod(key_path, 0o600)

    async def _load_keys(self, key_path: Path) -> None:
        """Load existing keys from disk."""
        key_password = settings.COMETNET_KEY_PASSWORD
        password_bytes = key_password.encode("utf-8") if key_password else None

        async with aiofiles.open(key_path, "rb") as f:
            key_data = await f.read()

        try:
            # Try loading with password first (if provided)
            self._private_key = serialization.load_pem_private_key(
                key_data, password=password_bytes
            )
        except (TypeError, ValueError) as e:
            # If password was provided but key is unencrypted, try without password
            if password_bytes:
                try:
                    self._private_key = serialization.load_pem_private_key(
                        key_data, password=None
                    )
                    logger.warning(
                        "COMETNET_KEY_PASSWORD is set but key is unencrypted. "
                        "Consider regenerating the key for better security."
                    )
                except Exception:
                    raise ValueError(
                        f"Failed to load private key: {e}. "
                        "Check if COMETNET_KEY_PASSWORD is correct."
                    )
            else:
                raise ValueError(
                    f"Failed to load private key: {e}. "
                    "If the key is encrypted, set COMETNET_KEY_PASSWORD."
                )

        if not isinstance(self._private_key, EllipticCurvePrivateKey):
            raise ValueError("Invalid key type: expected ECDSA private key")

        self._public_key = self._private_key.public_key()
        self._node_id = self._derive_node_id(self._public_key)

    def _derive_node_id(self, public_key: EllipticCurvePublicKey) -> str:
        """Derive the node ID from the public key (SHA256 hash)."""
        public_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return hashlib.sha256(public_bytes).hexdigest()

    def sign(self, data: bytes) -> bytes:
        """
        Sign data with the node's private key.

        Args:
            data: The data to sign (bytes)

        Returns:
            The signature (bytes)
        """
        if self._private_key is None:
            raise RuntimeError(
                "Node identity not initialized. Call load_or_generate() first."
            )

        return self._private_key.sign(data, ec.ECDSA(hashes.SHA256()))

    def sign_hex(self, data: bytes) -> str:
        """Sign data and return the signature as a hex string."""
        return self.sign(data).hex()

    @staticmethod
    def verify(data: bytes, signature: bytes, public_key_bytes: bytes) -> bool:
        """
        Verify a signature against data using a public key.

        Args:
            data: The original data that was signed
            signature: The signature to verify
            public_key_bytes: The signer's public key (DER-encoded)

        Returns:
            True if the signature is valid, False otherwise
        """
        try:
            public_key = serialization.load_der_public_key(public_key_bytes)
            if not isinstance(public_key, EllipticCurvePublicKey):
                return False

            public_key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False

    @staticmethod
    def verify_hex(data: bytes, signature_hex: str, public_key_hex: str) -> bool:
        """Verify a signature using hex-encoded signature and public key."""
        try:
            signature = bytes.fromhex(signature_hex)
            public_key_bytes = bytes.fromhex(public_key_hex)
            return NodeIdentity.verify(data, signature, public_key_bytes)
        except ValueError:
            return False

    @staticmethod
    def node_id_from_public_key(public_key_hex: str) -> str:
        """Derive a node ID from a hex-encoded public key."""
        try:
            public_key_bytes = bytes.fromhex(public_key_hex)
            return hashlib.sha256(public_key_bytes).hexdigest()
        except ValueError:
            return ""

    async def sign_async(self, data: bytes) -> bytes:
        """Sign data asynchronously."""
        return await run_in_executor(self.sign, data)

    async def sign_hex_async(self, data: bytes) -> str:
        """Sign data and return hex asynchronously."""
        return await run_in_executor(self.sign_hex, data)

    @staticmethod
    async def verify_async(
        data: bytes, signature: bytes, public_key_bytes: bytes
    ) -> bool:
        """Verify signature asynchronously."""
        return await run_in_executor(
            NodeIdentity.verify, data, signature, public_key_bytes
        )

    @staticmethod
    async def verify_hex_async(
        data: bytes, signature_hex: str, public_key_hex: str
    ) -> bool:
        """Verify hex signature asynchronously."""
        return await run_in_executor(
            NodeIdentity.verify_hex, data, signature_hex, public_key_hex
        )

    @staticmethod
    def load_public_key(public_key_hex: str) -> Optional[EllipticCurvePublicKey]:
        """Load a public key object from a hex string."""
        try:
            public_key_bytes = bytes.fromhex(public_key_hex)
            return serialization.load_der_public_key(public_key_bytes)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def verify_with_key(
        data: bytes, signature: bytes, public_key: EllipticCurvePublicKey
    ) -> bool:
        """
        Verify a signature using a pre-loaded public key object.
        Avoids re-parsing the public key bytes every time.
        """
        try:
            if not isinstance(public_key, EllipticCurvePublicKey):
                return False

            public_key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False

    @staticmethod
    async def verify_with_key_async(
        data: bytes, signature: bytes, public_key: EllipticCurvePublicKey
    ) -> bool:
        """Verify signature using key object asynchronously."""
        return await run_in_executor(
            NodeIdentity.verify_with_key, data, signature, public_key
        )
