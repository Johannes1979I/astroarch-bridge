import os
import shutil
import socket
import subprocess
from pathlib import Path

HOME_DIR = os.path.expanduser("~")
TOKEN_FILE = Path(HOME_DIR) / ".config" / "astroarch-bridge" / "token"


def command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def get_tailscale_ip() -> str:
    if not command_exists("tailscale"):
        return ""

    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            check=True,
        )

        lines = result.stdout.strip().splitlines()
        return lines[0] if lines else ""

    except Exception:
        return ""


def get_lan_ip() -> str:
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)

        # Fallback if localhost
        if ip.startswith("127."):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
            finally:
                s.close()

        return ip

    except Exception:
        return "127.0.0.1"


def show_start_summary(logger):
    ts_ip = get_tailscale_ip()
    lan_ip = get_lan_ip()
    primary_ip = ts_ip if ts_ip else lan_ip

    if TOKEN_FILE.exists():
        print("\n==> astroarch-bridge installed and running\n")
        if ts_ip:
            print(
f"""

    URL (Tailscale):
        http://{ts_ip}:8765
    URL (LAN):
        http://{lan_ip}:8765

"""
            )
        else:
            print(
f"""
    URL: http://{primary_ip}:8765
    ATTENTION: Tailscale non found. To access remotely
    install Tailscale: sudo pacman -S tailscale && sudo systemctl enable --now tailscaled

"""
            )
            token = TOKEN_FILE.read_text().strip()
            print(
f"""
    Token: {token}

    QR for pairing:
        curl http://{primary_ip}:8765/api/system/qr?fmt=png -o pairing-qr.png

"""
            )
