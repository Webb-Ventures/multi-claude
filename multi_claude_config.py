"""Shared, standard-library-only gateway configuration. Configuration is data, never code."""

from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import socket
from urllib.parse import SplitResult, urlsplit


@dataclass(frozen=True)
class Gateway:
    url: str
    token: str = field(repr=False)

    def __post_init__(self):
        try:
            if not isinstance(self.url, str) or any(
                c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in self.url
            ):
                raise ValueError
            parsed = urlsplit(self.url)
            host = parsed.hostname
            if (parsed.scheme not in ("http", "https") or not host
                    or parsed.username is not None or parsed.password is not None
                    or "?" in self.url or "#" in self.url or "%" in host
                    or parsed.port == 0 or parsed.netloc.endswith(":")):
                raise ValueError
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host == "localhost"
            if parsed.scheme == "http" and not (loopback or host.endswith(".ts.net")):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Invalid gateway URL; use HTTPS, loopback HTTP, or .ts.net HTTP.") from None
        object.__setattr__(self, "url", self.url.rstrip("/") + "/")

    @property
    def parsed(self) -> SplitResult:
        return urlsplit(self.url)

    @property
    def port(self) -> int:
        return self.parsed.port or (443 if self.parsed.scheme == "https" else 80)


def default_config_path() -> Path:
    if "MULTI_CLAUDE_CONFIG" in os.environ:
        return Path(os.environ["MULTI_CLAUDE_CONFIG"]).expanduser()
    local = Path(__file__).resolve().parent / "config.local.json"
    return local if local.is_file() else Path.home() / ".config/multi-claude/config.json"


def load_config(path=None) -> dict:
    """Return effective JSON fields with validated, normalized gateway settings.

    A missing file is allowed only when both gateway settings come from the environment.
    Optional standalone CLI fields are left for the CLI to interpret.
    """
    try:
        data = json.loads(Path(path if path is not None else default_config_path()).expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError, UnicodeError):
        raise ValueError("Cannot read configuration; expected a readable JSON object.") from None
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a JSON object.")
    if "MULTI_CLAUDE_GATEWAY_URL" in os.environ:
        data["gateway_urls"] = [os.environ["MULTI_CLAUDE_GATEWAY_URL"]]
    if "MULTI_CLAUDE_GATEWAY_TOKEN" in os.environ:
        data["gateway_token"] = os.environ["MULTI_CLAUDE_GATEWAY_TOKEN"]
    token = data.get("gateway_token")
    if (not isinstance(token, str) or not token or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token)
            or token.lower().replace("_", "").replace("-", "").startswith(
                ("replace", "placeholder", "changeme", "yourtoken", "yourapikey", "yourgatewaytoken", "<")
            )):
        raise ValueError("Set gateway_token to a private gateway API key, not a placeholder.")
    urls = data.get("gateway_urls")
    if not isinstance(urls, list) or not urls:
        raise ValueError("gateway_urls must be a nonempty list of base URLs.")
    data["gateway_urls"] = [Gateway(url, token).url for url in urls]
    return data


def load_gateways(config_path=None) -> list[Gateway]:
    config = load_config(config_path)
    return [Gateway(url, config["gateway_token"]) for url in config["gateway_urls"]]


def select_gateway(gateways) -> Gateway:
    """Try gateways in order with credential-free, three-second TCP probes only."""
    for gateway in gateways:
        try:
            with socket.create_connection((gateway.parsed.hostname, gateway.port), timeout=3):
                return gateway
        except OSError:
            continue
    raise ValueError("No configured gateway is reachable. No direct fallback is permitted.") from None
