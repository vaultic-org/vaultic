"""
Restore Command - Retrieve and decrypt files from vaults.
"""

import typer
import json
from pathlib import Path
from getpass import getpass
from rich import print
from typing import Optional

from core.config import Config
from core.encryption.service import EncryptionService
from core.storage.factory import get_provider
from core.vault.manager import get_vault_path

app = typer.Typer()


@app.callback(invoke_without_command=True)
def restore(
    vault_id: str = typer.Argument(..., help="ID of the vault containing the file"),
    filepath: str = typer.Argument(
        ..., help="Path of the file to restore (as shown in 'list files')"
    ),
    output_dir: str = typer.Option(
        "./restored", help="Directory where to save the restored file"
    ),
    output_name: Optional[str] = typer.Option(
        None, help="Alternative filename for the restored file"
    ),
    provider: str = typer.Option(
        None, help="Override storage provider defined in .env"
    ),
    passphrase: Optional[str] = typer.Option(
        None, help="Vault passphrase (will prompt if not provided)"
    ),
):
    """
    Restore a single file from a vault.
    """
    # Prepare paths
    vault_path = get_vault_path(vault_id)
    if not vault_path.exists():
        print(f"[red]❌ Vault not found: {vault_id}[/red]")
        raise typer.Exit(code=1)

    meta_path = vault_path / "keys" / "vault-meta.json"
    if not meta_path.exists():
        print(f"[red]❌ Vault metadata not found for: {vault_id}[/red]")
        raise typer.Exit(code=1)

    # Get encryption service
    if not passphrase:
        passphrase = getpass("🔑 Enter vault passphrase: ")

    enc_service = EncryptionService(passphrase, meta_path)

    try:
        enc_service.verify_passphrase()
    except ValueError as e:
        print(f"[red]❌ Invalid passphrase: {str(e)}[/red]")
        raise typer.Exit(code=1)

    # Check for encrypted index
    encrypted_dir = vault_path / "encrypted"
    encrypted_index_path = encrypted_dir / "content" / "index.json.enc"
    encrypted_hmac_path = encrypted_dir / "hmac" / "index.json.enc.hmac"
    legacy_index_path = encrypted_dir / "index.json"

    # Try to load encrypted index first, if possible
    index = None
    using_encrypted_index = False

    if encrypted_index_path.exists() and encrypted_hmac_path.exists():
        try:
            # Try to use VaultIndexManager
            from core.vault.index_manager import VaultIndexManager

            index_manager = VaultIndexManager(enc_service, vault_path)
            index = index_manager.load()
            using_encrypted_index = True
            print("[blue]🔐 Using encrypted index.[/blue]")
        except Exception as e:
            print(f"[yellow]⚠️ Could not load encrypted index: {str(e)}[/yellow]")
            index = None

    # Fall back to legacy index if needed
    if index is None and legacy_index_path.exists():
        try:
            with open(legacy_index_path, "r", encoding="utf-8") as f:
                index = json.load(f)
            print("[yellow]⚠️ Using legacy unencrypted index.[/yellow]")
        except Exception as e:
            print(f"[red]❌ Error reading index file: {str(e)}[/red]")
            raise typer.Exit(code=1)

    if index is None:
        print(f"[red]❌ No index found for vault: {vault_id}[/red]")
        raise typer.Exit(code=1)

    # Check if file exists in index
    if filepath not in index:
        print(f"[red]❌ File not found in vault: {filepath}[/red]")
        available = "\n  • ".join(list(index.keys())[:5])
        print(f"[yellow]Available files include:[/yellow]\n  • {available}")
        print(f"[blue]Use 'vaultic list files {vault_id}' to see all files.[/blue]")
        raise typer.Exit(code=1)

    file_info = index[filepath]
    file_hash = file_info.get("hash", "")

    # If file_hash is empty or not found, error
    if not file_hash:
        print(f"[red]❌ File hash missing in index for: {filepath}[/red]")
        raise typer.Exit(code=1)

    # Set up paths
    provider_name = provider or Config.PROVIDER
    provider = get_provider(provider_name)

    encrypted_path = encrypted_dir / "content" / file_hash
    hmac_path = encrypted_dir / "hmac" / (file_hash + ".hmac")

    # If files aren't local, download them
    temp_dir = Path(".vaultic/temp")
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_encrypted = temp_dir / file_hash
    temp_hmac = temp_dir / (file_hash + ".hmac")

    if not encrypted_path.exists():
        print(f"[blue]☁️ Downloading from {provider_name}:[/blue] {filepath}")
        try:
            provider.download_file(filepath + ".enc", temp_encrypted)
            encrypted_path = temp_encrypted
        except Exception as e:
            print(f"[red]❌ Failed to download file: {str(e)}[/red]")
            raise typer.Exit(code=1)

    if not hmac_path.exists():
        try:
            provider.download_file(filepath + ".enc.hmac", temp_hmac)
            hmac_path = temp_hmac
        except Exception as e:
            print(f"[red]❌ Failed to download HMAC: {str(e)}[/red]")
            raise typer.Exit(code=1)

    # Prepare output path
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    if output_name:
        final_path = output_path / output_name
    else:
        # Use the original filename
        filename = Path(filepath).name
        final_path = output_path / filename

    # Decrypt the file
    print(f"[yellow]🔓 Decrypting:[/yellow] {filepath}")
    try:
        enc_service.decrypt_file(str(encrypted_path), str(final_path))
        print(f"[green]✅ File restored to:[/green] {final_path}")

        # Clean up temp files
        if temp_encrypted.exists():
            temp_encrypted.unlink()
        if temp_hmac.exists():
            temp_hmac.unlink()

        # Clean up index manager cache to save memory
        if using_encrypted_index:
            index_manager.clear_cache()

        return final_path

    except Exception as e:
        print(f"[red]❌ Decryption failed: {str(e)}[/red]")
        raise typer.Exit(code=1)
