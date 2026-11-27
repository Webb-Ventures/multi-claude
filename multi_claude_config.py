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


def read_token_file(path, key=None) -> str:
    """Read the gateway token from a file as data; the file is never sourced or executed.

    Without a key, the whole file (minus surrounding whitespace) is the token.
    With a key, the file holds KEY=value lines and the first line for that key is used.
    Errors never include file contents.
    """
    if not isinstance(path, str) or not path:
        raise ValueError("gateway_token_file must be a file path.")
    if key is not None and (not isinstance(key, str) or not key or "=" in key
                            or any(c.isspace() or ord(c) < 32 for c in key)):
        raise ValueError("gateway_token_key must be a single name without spaces or '='.")
    file = Path(path).expanduser()
    if not file.is_absolute():
        raise ValueError("gateway_token_file must be an absolute path or start with ~.")
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ValueError(f"Cannot read gateway_token_file {file}.") from None
    if key is None:
        return text.strip()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value
    raise ValueError(f"gateway_token_key {key} was not found in {file}.")


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
        data.pop("gateway_token_file", None)
        data.pop("gateway_token_key", None)
    elif "MULTI_CLAUDE_GATEWAY_TOKEN_FILE" in os.environ:
        data.pop("gateway_token", None)
        data["gateway_token_file"] = os.environ["MULTI_CLAUDE_GATEWAY_TOKEN_FILE"]
        data.pop("gateway_token_key", None)
    if "MULTI_CLAUDE_GATEWAY_TOKEN_KEY" in os.environ and "gateway_token_file" in data:
        data["gateway_token_key"] = os.environ["MULTI_CLAUDE_GATEWAY_TOKEN_KEY"]
    if "gateway_token_file" in data:
        if "gateway_token" in data:
            raise ValueError("Set either gateway_token or gateway_token_file, not both.")
        data["gateway_token"] = read_token_file(data["gateway_token_file"], data.get("gateway_token_key"))
    elif "gateway_token_key" in data:
        raise ValueError("gateway_token_key needs gateway_token_file.")
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
