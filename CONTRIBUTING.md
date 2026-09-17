# Contributing to Sentinella 🐦‍⬛

Thank you for your interest in contributing to **Sentinella**! We welcome contributions, bug reports, feature requests, and security improvements from the community.

---

## 🛠 Setting Up Your Development Environment

Sentinella uses [uv](https://github.com/astral-sh/uv) to manage python dependencies, virtual environments, and workspace configurations.

### Prerequisites

- **Python**: `3.11`, `3.12`, or `3.13`.
- **uv**: Install via curl or your package manager (see [uv installation](https://github.com/astral-sh/uv#installation)).
- **Node.js** & **npm** *(optional)*: `Node.js 20+` (v24 recommended). Required only if developing or running tests for the Web Dashboard (`sentinella/web/static/` and `tests/web/`).

### Setup Steps

1. **Fork and clone** the repository:
   ```bash
   git clone https://github.com/your-username/sentinella.git
   cd sentinella
   ```

2. **Sync the workspace dependencies and virtual environment**:
   ```bash
   uv sync --all-extras --dev
   ```
   This command automatically creates a virtual environment `.venv` and installs all dependencies, including development tools (`pytest`, `ruff`, etc.).

3. **Install web dashboard test dependencies** *(optional, for web dashboard development)*:
   ```bash
   npm install
   ```

---

## 🎨 Coding Style & Guidelines

To maintain code quality and consistency across the repository, we use **Ruff** for formatting and linting.

### Formatting & Linting Rules

- Line length limit: **100 characters**.
- Target Python version: **3.11**.
- We select rules: `E` (errors), `F` (linting/imports), `W` (warnings), `I` (isort import ordering), `UP` (pyupgrade), and `B` (flake8-bugbear).

### Quality Checks

Before committing your changes, always run lint, format, and type checks locally:

```bash
# Run the linter
uv run ruff check

# Run the format check
uv run ruff format --check

# Run static type checking
uv run mypy
```

To automatically fix import order and lint errors, and auto-format your code:

```bash
# Auto-fix linting issues
uv run ruff check --fix

# Auto-format the code
uv run ruff format
```

---

## 🧪 Testing

All new features and bug fixes should include corresponding tests.

### Python Test Suite (pytest)

Run the backend test suite with coverage using `uv`:

```bash
uv run pytest --cov
```

Our Python test suite includes:
- **Unit tests**: Configuration parsing, metrics exporters (Text, CSV, JSON), data models, and utility functions.
- **Integration tests**: Remote client/server communication using FastAPI's test client and `RemoteCollector`.
- **Broadcast & Concurrency tests**: WebSocket fan-out and lifecycle management in `BroadcastHub`, per-client timeouts, and client capacity limits.
- **UI/TUI tests**: Textual widgets, light/dark theme resolution, grid layout geometry, and application lifecycle.
- **Background runner tests**: Daemonized web and remote server background process orchestration.
- **Feature & Regression tests**: Edge cases, error handling, sensor detection, and plugin robustness (CPU, memory, containers, network).

### Web Dashboard Test Suite (Vitest)

The Web Dashboard client-side logic is tested using [Vitest](https://vitest.dev/) and `jsdom`:

```bash
# Run the web dashboard test suite
npm test

# Run tests in watch mode during development
npm run test:watch
```

The Web Dashboard test suite covers:
- **Library utilities** (`lib.js`): Formatting (bytes, rates, percentages, temperatures), table sorting, rate calculation, container status detection, and API key obfuscation.
- **DOM rendering & updates** (`render.test.js`): System cards, CPU bars, memory stats, process tables, truncation notes, and network metrics.
- **Lifecycle & Authentication** (`dashboard.test.js`): WebSocket connection states, authentication handshakes, reconnection backoff, manual retry triggers, and theme persistence.

---

## 🚀 Pull Request Process

When you are ready to submit your changes, please follow these steps:

1. **Create a branch** for your work:
   ```bash
   git checkout -b feature/your-feature-name
   # or
   git checkout -b fix/bug-description
   ```
2. **Make your changes** and ensure they adhere to coding style guidelines.
3. **Write/update tests** for your changes.
4. **Verify everything passes** locally:
   ```bash
   uv run ruff check
   uv run ruff format --check
   uv run mypy
   uv run pytest --cov

   # If you modified web dashboard assets or tests:
   npm test
   ```
5. **Commit your changes** with a clear and descriptive commit message.
6. **Push your branch** to your fork and **open a Pull Request** against the `main` branch of the original repository.
7. Fill out the Pull Request template provided in the repository.

---

Happy coding! 🐦‍⬛