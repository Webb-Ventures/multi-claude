# multi-claude

Small tools for running Claude Code through your own inference gateway and opening
multiple Claude Desktop profiles on one Mac.

- **`start-cc.sh`** launches the installed Claude Code CLI through a gateway.
- **`claude_code_proxy.py`** configures Desktop's **local Code sessions only** to use
  the gateway, while Desktop keeps its normal account interface and Chat/Cowork
  routing.
- **`claude_profiles.py`** builds named, colour-coded `.app` launchers with separate
  Desktop account data and, by default, separate Claude Code configuration.

The profile apps launch the **same, untouched `/Applications/Claude.app`**. They do
not contain patched copies of Claude. Update the original installation normally;
all profiles use that updated installation when reopened.

> **Thanks to [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI).** Its
> gateway and provider integrations make this workflow possible. Please visit
> the upstream project for proxy installation, supported providers,
> authentication, configuration, and troubleshooting.

This is an unofficial toolkit, not an Anthropic or CLIProxyAPI product. It does not
include Claude Desktop, Claude Code, a proxy server, provider credentials, or
subscription entitlements.

## Important limits up front

- Separate Desktop profiles mean separate browser data/cookie stores, **not
  separate macOS users or independently isolated Keychains**. Native services,
  hardware credentials, and browser-based SSO can remain shared.
- Names and coloured icons identify the **launcher apps** in Finder and pinned
  shortcuts. The running vendor application retains its normal **Claude Dock/menu
  identity**. This toolkit does not modify the vendor bundle to change that.
- The Desktop corporate-launcher hook deliberately withholds Desktop inference
  credentials from the Code engine. The engine uses gateway authentication.
  **Remote Control and other first-party-authenticated engine services are not
  preserved by this route; the native launcher disables fast mode.**
- The normal Desktop model picker is unchanged. A gateway must accept/map the
  Claude model IDs and helper-model requests Desktop sends. Arbitrary models
  advertised by a gateway are not automatically added to Desktop's picker.
- Relocating Desktop's user-data directory disables its local-pairing feature in
  the examined build. SSH and cloud Code sessions are not configured here.
- Complete separate-account sign-in, native OAuth/deep-link callback routing,
  every provider/model feature, and updater-triggered profile relaunch have not
  been verified. Test your own accounts before relying on this setup.

## Requirements

For the CLI launcher:

- Python **3.10+** and a POSIX shell, on macOS or Linux.
- Claude Code installed and available as `claude` on `PATH`.
- A gateway implementing Anthropic's Messages API, including streaming and tool
  use. The Desktop preflight also requires `GET /v1/models`.

For Desktop proxy/profile tools:

- macOS and the normal Claude Desktop installation at `/Applications/Claude.app`.
- A Desktop build supporting `claudeCodeProcessWrapperEnabled` in first-party
  mode. The hook is present but **public-undocumented** in the examined builds;
  future updates can change it.
- Administrator access for the **optional Desktop proxy installer**. The profile
  builder runs as your normal user and needs write access to `/Applications`;
  use `--apps-dir "$HOME/Applications"` if that directory is not writable. Do not
  run the whole builder with `sudo`.
- Xcode or Xcode Command Line Tools for profile icon/launcher compilation:

  ```sh
  xcode-select --install
  xcrun --find swiftc
  ```

Python uses only the standard library. The launcher builder uses the system Swift,
AppKit, `iconutil`, and `codesign` tools. Generated wrappers are locally ad-hoc
signed; **the original Claude app is never re-signed**.

## 1. Set up your proxy and private configuration

Install and configure [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
(or another compatible gateway) first. Obtain its **client inference API key**.
A management/admin key is separate and is not required by these tools.

From your checkout:

```sh
mkdir -p "$HOME/.config/multi-claude"
chmod 700 "$HOME/.config/multi-claude"
cp config.example.json "$HOME/.config/multi-claude/config.json"
chmod 600 "$HOME/.config/multi-claude/config.json"
```

Edit that file with your gateway URL and actual client API key:

```json
{
  "gateway_urls": [
    "http://localhost:8317/"
  ],
  "gateway_token": "REPLACE_WITH_YOUR_GATEWAY_TOKEN"
}
```

The placeholder is intentionally rejected until replaced. Never commit your real
key. Prefer this outside-the-repository file for apps launched from Finder, which
normally do not inherit terminal exports.

Optional fallback gateways are listed after the primary:

```json
{
  "gateway_urls": [
    "https://primary.example.com/",
    "https://backup.example.com/"
  ],
  "gateway_token": "REPLACE_WITH_YOUR_GATEWAY_TOKEN"
}
```

Use gateways accepting the same API key. Selection tries credential-free TCP
connections in order, then uses the first reachable endpoint. This is **startup
reachability failover**, not mid-generation failover or a guarantee of switching
on HTTP authentication/model errors. If none is reachable, the wrapper fails;
it does not fall back to Anthropic inference.

Configuration precedence:

1. An explicit `--config /absolute/path/config.json` argument where supported.
2. `MULTI_CLAUDE_CONFIG`.
3. An ignored `config.local.json` next to the scripts, if present.
4. `~/.config/multi-claude/config.json`.

`MULTI_CLAUDE_GATEWAY_URL` and `MULTI_CLAUDE_GATEWAY_TOKEN` override their JSON
values. Environment-only configuration works for terminal use, but an installed
Desktop launcher should use a persistent private JSON file. Do not put credentials
in command-line arguments.

Remote gateways require HTTPS. Plain HTTP is accepted only for loopback addresses
or `.ts.net` hosts intended for an encrypted Tailscale network. Connect your VPN or
tailnet yourself; these tools do not log in to it or change its configuration.
Configuration is parsed as JSON, never sourced as shell code.

## 2. Use the Claude Code CLI

```sh
./start-cc.sh
./start-cc.sh /path/to/project --model claude-opus-5-5
./start-cc.sh --dir "/path/with spaces" --resume
./start-cc.sh --config /absolute/path/config.json mcp list
```

The wrapper sets the gateway address/authentication in the child environment, not
in command-line arguments or committed Claude settings. Claude flags and
subcommands are otherwise forwarded. Use a model your gateway actually serves.

Your existing `CLAUDE_CONFIG_DIR` is retained. To choose a particular directory for
this standalone CLI wrapper, optionally add:

```json
"cli_config_dir": "/absolute/path/to/your/claude-config"
```

This option does not override the Desktop profile launchers' individual Code
config directories.

**Remote Control:** this public launcher intentionally refuses `--rc`,
`--remote-control`, and the Remote Control subcommand. It does not ship the private
forward-proxy/certificate lane used by some custom setups, and never silently
changes to direct subscription inference to make RC work.

## 3. Route normal Desktop's local Code sessions through the proxy

Run these commands **as your normal user**, from a checkout that will stay at its
current location:

```sh
python3 -I claude_code_proxy.py check
```

The check validates the app/configuration and authenticates a Models API request.
It does not generate inference or prove a live Desktop session is routed.

Finish active work and quit **all Claude Desktop instances**, then:

```sh
python3 -I claude_code_proxy.py install
```

The installer requests `sudo` only for the managed-policy step. Do not run the
whole installer as root. It:

1. Merges `processWrapper` into
   `/Library/Application Support/ClaudeCode/managed-settings.json`.
2. Creates/selects a minimal local configuration-library entry containing only
   `claudeCodeProcessWrapperEnabled: true`, under
   `~/Library/Application Support/Claude-3p/configLibrary/`.
3. Records enough state to remove its own changes safely.

Despite the `Claude-3p` directory name, the minimal entry has **no inference
provider** and keeps Desktop in normal first-party mode. Original configuration
entries remain intact. Existing managed wrappers, incompatible authentication
policy, device-managed deployments, and active local profiles with additional
settings are not silently overwritten.

The managed launcher declaration is **machine-wide**. This installer is intended
for a user-controlled Mac, not an unreviewed multi-user or corporate fleet rollout.
Consult your administrator if device policy already controls your installation.

Open Claude normally, sign in, and start a **new Code → Local** session. Desktop
launches its own selected Code executable through the wrapper; the wrapper never
substitutes an unrelated `claude` version from `PATH`. Chat and Cowork are not
routed by this hook. Desktop account switching does not switch the gateway key;
inference continues to use your configured gateway.

Check installed state:

```sh
python3 -I claude_code_proxy.py status
```

`status` verifies installed settings, not a particular completed request. To
verify actual routing, correlate a new local Code prompt with gateway request
logs, or inspect that engine's gateway environment and active network connection.
Do not publish keys, prompt bodies, or private request logs when sharing diagnostics.

To undo, quit all Claude instances and run:

```sh
python3 -I claude_code_proxy.py uninstall
```

Removal preserves unrelated policy edits and restores the prior profile selection
unless you changed it yourself. It refuses to delete edited/replaced profile
settings. Interrupted removals can be retried. If installation was interrupted,
run `uninstall` to recover its recorded changes before trying `install` again,
even if the configuration-library metadata was not created yet.

**Moving an existing installation:** uninstall using the script in its old
location before installing from a new checkout. Do not delete or move an installed
checkout, interpreter, or private config file while its launcher still refers to
those paths. Merely copying this repository does not replace an existing launcher.

## 4. Create multiple named Desktop profile apps

Create two independently configured launchers:

```sh
python3 -I claude_profiles.py create "Claude Work" --color blue
python3 -I claude_profiles.py create "Claude Personal" --color purple
```

They appear under:

```text
/Applications/
  Claude Work.app
  Claude Personal.app
```

Open them in Finder, pin their launcher shortcuts to the Dock, or use:

```sh
python3 -I claude_profiles.py launch "Claude Work"
python3 -I claude_profiles.py launch "Claude Personal"
python3 -I claude_profiles.py list
python3 -I claude_profiles.py status
```

Each starts with fresh account data; **sign in separately**. No existing cookies,
account tokens, or conversations are copied. Existing standard Claude accounts and
app data remain untouched. The profile names are labels you choose, not verified
account identities.

Available colours: `blue`, `purple`, `teal`, `green`, `red`, `orange`, `pink`,
`slate`, `black`, or a quoted hex value such as `--color '#1F8A70'`. Without
`--icon-svg`, the locally installed Claude icon is framed/badged with that colour.
No vendor icon assets are distributed in this repo.

### Recolour an SVG instead of adding a frame

Supply your own static, path-only SVG to change its background colour directly,
without an added frame, padding, or initials badge:

```sh
python3 -I claude_profiles.py create "Claude Purple" --color purple \
  --icon-svg /absolute/path/icon.svg
python3 -I claude_profiles.py create "Claude Black" --color black \
  --icon-svg /absolute/path/icon.svg
```

The first path's fill identifies the background. All paths with that same fill
are recoloured; other fills and path geometry stay unchanged. The icon is rendered
on a 1024-pixel square canvas, preserving the SVG viewBox aspect ratio. Input must
be UTF-8, at most 1 MiB, and contain only flat paths with explicit hex fills. Scripts,
styles, transforms, external resources and DTD/entity definitions are rejected.

Update an **existing** launcher's icon and optionally relocate it, without
recreating its profile or changing its account data, Code config, or proxy opt-in:

```sh
python3 -I claude_profiles.py set-icon "Claude Work" --color purple \
  --icon-svg /absolute/path/icon.svg --apps-dir /Applications
python3 -I claude_profiles.py set-icon "Claude Work" --color black
```

The second command reuses the sanitised SVG embedded in that launcher. Omitting
`--apps-dir` keeps its current location. An unrelated existing app at the target
path is never replaced. Re-add a pinned Dock shortcut if it still points to the old
location; macOS can temporarily cache the previous icon.

Use artwork you have permission to use. User-supplied SVGs are stored only in your
local generated app; they are not included in this repository. Confirm redistribution
rights before sharing generated apps or adding third-party artwork to a fork.

Default storage:

| Data | Location |
| --- | --- |
| Registry/build cache | `~/Library/Application Support/Claude-Profiles/` |
| Desktop account data | `<root>/<profile UUID>/` |
| Companion configuration | `<root>/<profile UUID>-3p/` |
| Separate Code config | `<root>/<profile UUID>-code/` |

Production Claude strips `CLAUDE_USER_DATA_DIR`. These launchers use the supported
Chromium **`--user-data-dir=<absolute path>`** argument instead, and set a distinct
`CLAUDE_CONFIG_DIR` for Code. Do not manually launch the bare source binary without
the profile argument and expect it to select the same account.

If the Desktop proxy is already installed, new profiles automatically receive a
fresh, minimal companion configuration-library opt-in for that same machine-level
launcher. They do not share browser auth stores or copy proxy credentials. Use
`--code-proxy on` to require an installed proxy, or `--code-proxy off` to omit this
local opt-in. Existing device policy always remains authoritative.

### Optional shared Code configuration

To keep Desktop accounts separate but share the usual `~/.claude` directory:

```sh
python3 -I claude_profiles.py create "Claude Shared Settings" \
  --color teal --shared-code-config
```

Sharing that directory shares **Code state, plugins, settings, and potentially
credentials**, not just harmless preferences. Separate Code configs are the safer
default. This choice does not copy or share Desktop's cookie jars.

### Updates

Keep the original `/Applications/Claude.app` installed and update it normally.
Every launcher points to that path rather than a frozen duplicate. Reopening a
profile therefore uses the current installed Claude version without rebuilding
its launcher or moving its account data.

This is not a separate vendor updater for each wrapper, and does not guarantee
that every in-app updater restart preserves the profile argument. Keep your work
saved; reopen the named launcher if an updater restarts an unexpected profile.
A custom source location can be selected at creation with `--source /path/Claude.app`.

### Remove a launcher without deleting account data

Quit the selected profile first, then:

```sh
python3 -I claude_profiles.py remove "Claude Work"
```

This removes its generated launcher and registry entry. **Desktop account data
and Code config are retained**, with paths printed for your records. It does not
terminate a running instance, remove live locks, delete accounts, or purge history.

## Troubleshooting

- **The key works for models but logs return 401:** inference and management keys
  are different. Configure logging/admin access in CLIProxyAPI itself. This
  toolkit only needs the client inference key.
- **Desktop still uses a different launcher:** check `status`, then check the
  machine-managed settings. Uninstall an older installation with its original
  script instead of overwriting managed policy manually.
- **A named launcher shows Claude's usual Dock icon/name:** expected. Finder and
  pinned launcher shortcuts have the custom appearance; the signed source app
  retains the vendor identity.
- **A login link opens another instance:** multiple instances still share the
  vendor's registered URL scheme. Complete sign-in/callback behavior is an
  acceptance test, not an established guarantee. Do not send authentication URLs
  or cookies to other profiles to work around it.
- **A profile has the wrong Code config:** Desktop can have its own stored config
  override. Check the profile's settings and `status`; shared-config mode is
  intentional only when explicitly selected.
- **No gateway is reachable:** connect the required network/VPN, check the URLs,
  and start the gateway. There is no automatic direct-inference fallback.

## Development and verification

```sh
python3 -I -B -m unittest discover -s tests -v
sh -n start-cc.sh
```

Tests use temporary directories, fake credentials and mocked connectivity/exec.
They cover configuration validation, folder/argument handling, exact Desktop engine
forwarding, interrupted policy installation/removal, profile isolation and safe
rollback. They do not log in to real accounts or make paid model requests.

Local native checks exercised launcher/icon compilation, generation of two
profile apps, simultaneous distinct-profile processes, the fresh welcome screen
and its Get started action, and verification that the original Claude signature
remained valid. Completed account login, full Code startup inside every new
account, and an actual source-app update were not automated.

Examined Desktop builds: **3.1623.1** for the initial Code-only hook investigation
and **3.6556.2** for packaged profile behavior. Treat private/undocumented app
integration as version-sensitive and re-test after updates.

## License and acknowledgements

Toolkit source is [MIT licensed](LICENSE), copyright Webb Ventures. Claude and its
local icon belong to their respective owner; neither the application nor its
vendor assets are redistributed here.

Special thanks again to **[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)**
and its contributors for the proxy infrastructure and integrations that enable
these workflows. CLIProxyAPI is a separate project with its own license.
