# Sentinella 🐦‍⬛

**Cross-platform system monitor with TUI, web dashboard, and remote monitoring**

**Sentinella** is a cross-platform, open-source system monitor written in Python that works on Linux, BSD, macOS, and Windows.

---

## ✨ Features

- 🖥 **Interactive TUI Dashboard**: A beautiful terminal dashboard powered by [Textual](https://github.com/Textualize/textual). Inspect CPU, memory, disk, network, active processes, users, sensors, and containers. Supports instant runtime theme toggling (`t` key: dark/light), dynamic process re-sorting (`p` key), force refresh (`r` key), and clear visual stale-state indicators during collection hiccups.
- 🌐 **Modern Web Dashboard**: A real-time browser interface powered by FastAPI and WebSockets via a high-performance, capped `BroadcastHub`. Features 100% offline/air-gapped vendorized assets, persistent light/dark themes, dynamic thresholds and process ranking matching agent config, snapshot payload wire trimming (~70% payload reduction), an accessible (ARIA, keyboard focus-trapped) authentication modal with XOR obfuscation for client-side storage, and reconnection backoff with manual retry.
- 📡 **Remote Monitoring Agent**: Run Sentinella as a remote server agent (`serve` mode) and monitor hosts securely via `RemoteCollector` over HTTP/WebSockets from a centralized Sentinella instance, or fetch console summaries remotely.
- 🚀 **Quick Summary (`fetch`)**: A quick, `neofetch`-style console summary of system hardware, OS, and resource utilization, available locally or queried from a remote agent (`sentinella --remote HOST:PORT fetch`).
- 📊 **Structured Exports (`print`)**: Print snapshots of system metrics to stdout in **Text**, **CSV**, or **JSON** formats with module name validation, ideal for scripting, integrations, or cron jobs.
- 📦 **Container Support**: Native monitoring for Docker and LXC containers (memory, CPU, status, and metadata) with streaming bounded output readers to avoid memory exhaustion.
- 🔒 **Security-First**: Constant-time API key verification, security headers (`Content-Security-Policy`, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy: no-referrer`) applied to all HTTP responses, automatic warnings for insecure open network binds across IPv4/IPv6 without authentication, CSV formula-injection sanitization, and permission warnings (`chmod 600`) for config files holding plaintext credentials.

---

## 🚀 Quick Start

### Prerequisites

- **Python**: `3.11` or higher.
- **Package Manager**: [uv](https://github.com/astral-sh/uv) (recommended) or `pip`.

### Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/salvatorecorvaglia/sentinella.git
   cd sentinella
   ```

2. **Sync the workspace (using `uv`)**:
   ```bash
   uv sync --all-extras --dev
   ```

3. **Install the package (using `pip`)**:
   ```bash
   pip install -e .
   ```

---

## 🖥 Command Line Usage

Once installed, the `sentinella` executable will be available on your system path.

```bash
# Start the interactive TUI Dashboard (default)
sentinella

# Run the TUI connected to a remote Sentinella agent
sentinella --remote 127.0.0.1:9090

# Print a quick neofetch-style system summary
sentinella fetch

# Fetch a system summary from a remote Sentinella agent
sentinella --remote 127.0.0.1:9090 fetch

# Start the Web Dashboard server (default: port 8080)
sentinella web

# Start the Remote Monitoring Agent server (default: port 9090)
sentinella serve

# Print system stats directly to stdout (JSON format)
sentinella print --format json

# Print specific modules' metrics (e.g. cpu, memory)
sentinella print cpu memory

# Print metrics from a remote Sentinella agent
sentinella --remote 127.0.0.1:9090 print
```

### ⌨️ TUI Keyboard Controls

| Key | Action |
| --- | --- |
| `q` / `Ctrl+C` | Quit Sentinella |
| `t` | Toggle theme on the fly (Dark ⇄ Light) |
| `p` | Cycle process sorting order (`cpu` → `memory` → `pid` → `name`) |
| `r` | Force immediate metric refresh |

### Global CLI Options

- `-c PATH`, `--config PATH`: Path to a custom `sentinella.toml` configuration file.
- `-v`, `--verbose`: Enable verbose debug logging.
- `-r HOST:PORT`, `--remote HOST:PORT`: Connect to a remote Sentinella agent instead of inspecting the local system (applicable to TUI, `fetch`, and `print`).

---

## 🛠 Configuration

Sentinella can be configured using a `sentinella.toml` file. The search order for configuration is:
1. `--config` / `-c` CLI flag (explicit path).
2. `./sentinella.toml` (current working directory).
3. `~/.config/sentinella/sentinella.toml` (user profile configuration).
4. Default built-in values.

A default configuration template is available in [sentinella.example.toml](sentinella.example.toml).

### Configuration Options Reference

```toml
[general]
refresh_interval = 2        # Seconds between updates (minimum: 1)
theme = "dark"              # Theme to apply: "dark" or "light"

[modules]
cpu = true                  # Enable/disable specific monitoring modules
memory = true
disk = true
network = true
processes = true
users = true
sensors = true
containers = true

[web]
enabled = false             # Automatically run web server in TUI background
host = "127.0.0.1"          # Host address to bind the web server
port = 8080                 # Port for the web server (1-65535)
api_key = ""                # API key for the dashboard (empty = no authentication)

[remote]
enabled = false             # Automatically run remote agent in TUI background
host = "127.0.0.1"          # Host address to bind the agent server
port = 9090                 # Port for the remote agent (1-65535)
api_key = ""                # API key for agent connection security

[export]
format = "text"             # Default export format: "text", "csv", or "json"

[processes]
max_display = 25            # Max processes to display in TUI and print output
sort_by = "cpu"             # Sort criteria: "cpu", "memory", "pid", "name"
```

---

## 🤝 Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## 📜 Changelog

Detailed release history and version changes can be found in [CHANGELOG.md](CHANGELOG.md).

## 🔐 Security

If you discover a security vulnerability, please see our [Security Policy](SECURITY.md).

## 📝 License

Distributed under the MIT License. See [LICENSE](LICENSE) for more information.

---

**Author**: [Salvatore Corvaglia](https://github.com/salvatorecorvaglia)