# Host probe: which MCP result channel reaches the model?

Cartogate sends every successful tool result twice, as the MCP specification asks: a JSON text
block for text-only clients and `structuredContent` for schema-aware ones. A host that forwards
both to its model makes every tool call cost double. Hosts do not document which they forward,
so this probe measures it.

`probe_server.py` is a stdio MCP server with one tool, `probe_both`, that returns different
content in each channel. Ask the model to repeat the tool result and read which one it saw:

| The model reports | The host passed |
|---|---|
| `"channel": "text-block"`, `"fruit": "kiwi"` | the text block only |
| `"channel": "structured-content"`, `"fruit": "plum"` | `structuredContent` only |
| both | both: results cost double on this host |

## Results

| Host | Version | Date | Channel the model saw |
|---|---|---|---|
| Claude Code | 2.1.325 | 2026-11-15 | `structuredContent` only, serialised without spaces |
| Cursor | | | not measured |
| Devin Desktop | | | not measured |
| Codex CLI | | | not measured |
| Gemini CLI | | | not measured |

Add a row when you measure a host. If one forwards the text block, its size is what
`_text_result` in `src/cartogate/mcp/server.py` emits (already separator-free).

## Running it

The server needs the `mcp` 2.x SDK, which Cartogate's own environment has.

Write a config file `probe.json` that points at this server with an absolute path:

```json
{"mcpServers": {"probe": {"command": "python", "args": ["/abs/path/to/evaluation/host_probe/probe_server.py"]}}}
```

### Claude Code

```bash
ENABLE_TOOL_SEARCH=false claude -p \
  "Call mcp__probe__probe_both once, then reply with only the raw tool result." \
  --model haiku --mcp-config probe.json --strict-mcp-config \
  --allowedTools mcp__probe__probe_both --output-format stream-json --verbose
```

In the stream, the `tool_result` block of the `user` event is exactly what the model received.
One run costs about one cent.

### Other hosts

Register the same server in the host's MCP settings, start a session, and ask:
"Call the probe_both tool once, then reply with only the raw tool result."
