# becu-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that provides read-only access to your [BECU](https://www.becu.org) accounts via browser automation. Use it with Claude Code or any MCP-compatible client to query account balances and transaction history conversationally.

> **Fork of [jrolstad/becu-mcp](https://github.com/jrolstad/becu-mcp)** with the following improvements:
> - `get_transactions` now supports exact date ranges (`start_date`/`end_date`) in addition to `days`
> - Transaction export uses a **direct HTTP POST** to BECU's CSV download endpoint instead of driving the AJAX UI — significantly faster and more reliable
> - Session validation checks the Activity page directly, catching `SystemUnavailable` redirects that the original missed
> - `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` env var lets you point to a custom Chromium binary (e.g. snap-installed Chromium on Ubuntu 24.04+)

## What it does

Exposes three tools:

| Tool | Description |
|------|-------------|
| `get_accounts` | Returns all accounts with current balance, available balance, and YTD interest |
| `get_balance` | Returns balance details for a single account by index |
| `get_transactions` | Returns transaction history for an account; supports `days`, `start_date`, and `end_date` |

Authentication is handled automatically using Playwright to drive a Chromium browser. Sessions are persisted to `session.json` so subsequent calls run headlessly. If the session expires or MFA is required, a visible browser window opens for you to complete login.

## Installation

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended — avoids conflicts with Ubuntu's managed Python)

Install `uv` if you don't have it:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Steps

1. Clone the repository:
   ```bash
   git clone https://github.com/ajbogh/becu-mcp.git
   cd becu-mcp
   ```

2. Create a virtual environment and install dependencies:
   ```bash
   uv venv
   uv pip install -r requirements.txt
   ```

3. Install Playwright's Chromium browser:
   ```bash
   .venv/bin/playwright install chromium
   ```
   > **Ubuntu 24.04+:** Playwright's bundled Chromium may not be supported. Use snap Chromium instead — see [Ubuntu / snap Chromium](#ubuntu--snap-chromium) below.

4. Set your BECU credentials using one of the two options below.

### Configure with Claude Code

Add the server to your Claude Code MCP settings (`~/.claude.json` under `mcpServers`). Credentials can be provided in the MCP config directly (recommended) or via a `.env` file.

**Option A — credentials in MCP config (recommended)**

Pass credentials as environment variables in the MCP server entry. This keeps everything in one place and avoids needing a `.env` file on disk.

```json
{
  "mcpServers": {
    "becu": {
      "type": "stdio",
      "command": "/path/to/becu-mcp/.venv/bin/python",
      "args": ["/path/to/becu-mcp/server.py"],
      "env": {
        "BECU_USERNAME": "your_username_here",
        "BECU_PASSWORD": "your_password_here"
      }
    }
  }
}
```

**Option B — `.env` file**

Create a `.env` file in the repo directory:
```bash
cp .env.example .env
```
Then edit `.env`:
```
BECU_USERNAME=your_username_here
BECU_PASSWORD=your_password_here
```

The server loads this automatically via `python-dotenv` on startup. The `.env` file is gitignored.

## Usage

Once connected, you can ask Claude things like:

- "List my BECU accounts with their balances"
- "What are the last 20 transactions from My Checking?"
- "What's the balance on my savings account?"
- "How much YTD interest has the Annual Payments savings account earned?"
- "Get my checking transactions from April 1 through April 30"
- "Show me all transactions between 01/01/2026 and 03/31/2026"

### Ubuntu / snap Chromium

If you're on Ubuntu 24.04+ where Playwright's bundled Chromium isn't supported, use the system snap Chromium instead:

```bash
sudo snap install chromium
```

Then set the env var in your MCP config or `.env`:

```
PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/snap/bin/chromium
```

## Authentication and sessions

- On first run, a Chromium browser window opens and logs in with your credentials.
- If MFA is required, you have 60 seconds to complete it in the browser window.
- After a successful login, cookies are saved to `session.json` for future headless runs.
- If a session expires, the browser window opens again automatically.
- `session.json` is gitignored — do not commit it.

## Development

### Project structure

```
becu_client.py   # Playwright scraping and HTML parsing logic
server.py        # MCP server (FastMCP) — exposes tools to MCP clients
requirements.txt # Python dependencies
.env.example     # Credential template
session.json     # Persisted browser session cookies (gitignored)
```

### How scraping works

`becu_client.py` uses Playwright to load pages from `onlinebanking.becu.org`, then parses the HTML with BeautifulSoup. The BECU pages use a [Tablesaw](https://github.com/filamentgroup/tablesaw) responsive table library that embeds column labels inside each `<td>` as `<b class="tablesaw-cell-label">` elements. The parser extracts these labels to identify each cell's field regardless of column order, making it resilient to layout changes.

Key functions:

- `_with_authenticated_page(callback)` — runs a callback with a headless authenticated page, re-auths if session expired
- `_export_transactions_csv()` — POSTs directly to BECU's CSV download endpoint using ASP.NET form tokens extracted from the page
- `_parse_becu_csv_bytes()` — parses raw CSV bytes into transaction dicts, handles Debit/Credit split columns
- `_cell_label_and_value()` — extracts the field label and value from a tablesaw `<td>`
- `_parse_currency()` — converts `"$1,234.56"` to `1234.56`
- `get_accounts()` — scrapes the Summary page, deduplicates by account number
- `get_transactions()` — exports transactions via CSV for an exact date range

### Running locally

```bash
.venv/bin/python server.py
```

Or via the MCP CLI:

```bash
.venv/bin/mcp dev server.py
```

### Adding new tools

1. Add a new async function to `becu_client.py` that fetches and parses the relevant BECU page.
2. Register it as a tool in `server.py` using the `@mcp.tool()` decorator.
3. Reconnect your MCP client to pick up the new tool.

### Dependencies

| Package | Purpose |
|---------|---------|
| `mcp` | MCP server framework (FastMCP) |
| `playwright` | Headless browser automation for scraping |
| `beautifulsoup4` | HTML parsing |
| `python-dotenv` | Loading credentials from `.env` |
