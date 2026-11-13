# rolescout MCP server

`rolescout serve` lets an AI agent use rolescout through the Model Context
Protocol instead of the command line: read the shortlist, look up and search
roles, mark roles, manage companies, and check coverage gaps, hiring trends
and health.

The server only reads and writes the local database. It never starts a
scout and never makes a network request, so its answers are as fresh as the
last scheduled run. It is meant for this machine and a private network; do
not expose it to the public internet.

## 1. Install

The server needs the official MCP Python SDK, which is an optional extra.
The scraper and the scheduled job work without it. On the machine that
holds the database (see DEPLOY.md for the base install):

```sh
~/.rolescout/venv/bin/pip install "$HOME/rolescout-scraper[mcp]"
```

Check: `rolescout serve --help` prints its options.

## 2. Start it

There are two ways to connect, for two kinds of client.

### A client on the same machine (stdio)

Nothing to start. The client launches `rolescout serve` itself and talks to
it over standard input and output; configure the client as in section 4.
There is no port and no token.

### A client on another machine (HTTP)

Choose a token, then start the server:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # prints a token; keep it private
export ROLESCOUT_MCP_TOKEN='paste-the-token-here'
rolescout serve --http
```

- Without `--host` it listens on `127.0.0.1:8765`: this machine only.
- It refuses to start without a token of at least 16 characters. Instead of
  the environment variable you can pass `--token-file PATH`.
- Every request must carry `Authorization: Bearer <token>`; anything else
  gets `401`.
- The URL for clients is `http://<address>:8765/mcp`.
- The process runs until you stop it (Ctrl-C). Nothing here keeps it
  running after a restart of the Mac.

### Over Tailscale

Bind the server to the Mac's tailnet address, so it is reachable from your
other devices on the tailnet and from nowhere else:

```sh
tailscale ip -4                                  # e.g. 100.64.0.10
rolescout serve --http --host 100.64.0.10
```

Clients then use `http://100.64.0.10:8765/mcp`. To use the Mac's Tailscale
name instead of the number, tell the server to accept it:

```sh
rolescout serve --http --host 100.64.0.10 --allow-host mac.your-tailnet.ts.net
```

The server answers only requests addressed to a host it expects (the bind
address, localhost, and any `--allow-host`); others get `421`. Traffic
between tailnet devices is encrypted by Tailscale, which is why plain
`http://` is acceptable here. Do not use `--host 0.0.0.0`.

### Read-only mode

```sh
rolescout serve --read-only            # or: rolescout serve --http --read-only
```

Every tool that changes anything is absent, not just refused: the agent can
look but cannot mark roles or edit companies.

## 3. Tools

Read (always available):

| Tool | What it does |
|---|---|
| `get_shortlist` | The ranked shortlist: flat, grouped by company, or only what is new since the previous list |
| `get_role` | One role in full; the description only when asked for |
| `search_roles` | Search all stored roles by title, company, open/closed and first-seen date |
| `explain_rank` | Why a role ranks where it does, or why it is not on the shortlist |
| `list_marks` | Roles marked dismissed, saved or applied |
| `list_target_companies` | Target companies with tier, boards and coverage state |
| `coverage_gaps` | Companies that cannot be fetched, grouped by job system, with build candidates |
| `connector_brief` | A Markdown brief for building an adapter for one job system |
| `hiring_trends` | Open counts and roles opened and closed over a date range, overall and by department |
| `health` | What `rolescout status` shows |
| `list_candidates` | Companies found on portfolio boards, with status and reason, and the funnel |
| `explain_candidate` | Each filter rule's result for one candidate |

Write (absent in read-only mode):

| Tool | What it does |
|---|---|
| `mark_role` | Mark a role dismissed, saved or applied, with an optional note |
| `unmark_role` | Remove a role's mark |
| `add_target_company` | Add a company to the target list |
| `deactivate_company` | Stop tracking a company, keeping its history |
| `reactivate_company` | Resume tracking it |
| `map_board` | Map a company to a Greenhouse, Lever, Ashby or Workday board |
| `record_job_system` | Record by hand which unsupported job system a company uses |
| `activate_candidate` | Start tracking a candidate as a tier 3 company |
| `dismiss_candidate` | Mark a candidate as never wanted |

Roles are identified by `role_id`, the integer every role-returning tool
includes. Companies are identified by name. Lists take `limit` and `offset`
and return `next_offset`.

## 4. Client configuration

Paths below assume the install from DEPLOY.md; replace `/Users/you` with
your home folder and the address and token with your own.

### Claude Code

Same machine, stdio:

```sh
claude mcp add --transport stdio --scope user rolescout -- /Users/you/.rolescout/venv/bin/rolescout serve
```

Another machine, HTTP over Tailscale:

```sh
claude mcp add --transport http --scope user rolescout http://100.64.0.10:8765/mcp --header "Authorization: Bearer paste-the-token-here"
```

Or in a project's `.mcp.json`, which expands environment variables, so the
token itself stays out of the file:

```json
{
  "mcpServers": {
    "rolescout": {
      "type": "http",
      "url": "http://100.64.0.10:8765/mcp",
      "headers": { "Authorization": "Bearer ${ROLESCOUT_MCP_TOKEN}" }
    }
  }
}
```

Check with `claude mcp list`, or `/mcp` inside Claude Code.

### Claude Desktop

Same machine, stdio. Edit
`~/Library/Application Support/Claude/claude_desktop_config.json`
(Settings > Developer > Edit Config), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "rolescout": {
      "command": "/Users/you/.rolescout/venv/bin/rolescout",
      "args": ["serve"]
    }
  }
}
```

Another machine. This config file is documented for commands that Claude
Desktop starts locally; it has no documented entry for an HTTP server with
a custom header. The simplest route that stays inside that format is to let
the command be `ssh`, which runs the stdio server on the Mac and needs no
HTTP server or token (it does need key-based SSH to the Mac, which works
over Tailscale):

```json
{
  "mcpServers": {
    "rolescout": {
      "command": "ssh",
      "args": ["mac", "/Users/you/.rolescout/venv/bin/rolescout", "serve"]
    }
  }
}
```

This `ssh` form has not been tested with Claude Desktop as part of this
project; it relies only on the client starting a command and talking to it
over standard input and output.

### Any other MCP client

- stdio: run the command `/Users/you/.rolescout/venv/bin/rolescout serve`.
- HTTP: Streamable HTTP at `http://<address>:8765/mcp`, with the header
  `Authorization: Bearer <token>`. The server is stateless and answers with
  plain JSON, so there is no session to keep alive.

With the official Python SDK (`pip install mcp`), version 2:

```python
import asyncio, os
import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

async def over_stdio():
    server = StdioServerParameters(command="/Users/you/.rolescout/venv/bin/rolescout", args=["serve"])
    async with Client(server) as client:
        result = await client.call_tool("get_shortlist", {"limit": 10})
        print(result.structured_content)

async def over_http():
    headers = {"Authorization": "Bearer " + os.environ["ROLESCOUT_MCP_TOKEN"]}
    async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30.0)) as http:
        async with Client(streamable_http_client("http://100.64.0.10:8765/mcp", http_client=http)) as client:
            print([tool.name for tool in (await client.list_tools()).tools])

asyncio.run(over_stdio())
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| `the MCP server needs the optional dependency` | Install the extra (section 1). |
| `no database at ...` | No scout has run for that database yet, or `--db` points at the wrong file. |
| `401` | The `Authorization: Bearer` header is missing or the token differs from the server's. |
| `421` | The client used a hostname the server does not expect; add it with `--allow-host`. |
| A write tool is missing | The server was started with `--read-only`. |
| Data looks old | The server never fetches; call `health` to see when the last scout ran. |
