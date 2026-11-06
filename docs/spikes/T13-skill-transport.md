# T13 spike: how the CEO skill reaches CEO (OQ12)

Date: 2026-11-05. Decides OQ12 for D13. Constraints that apply: D6 (CEO opens no
inbound ports), C10 (only the engineer clears gates, and every route
authenticates them), C4 (nothing but CEO writes the ledger), and the key design
principle (the engineer's assistant is their choice, not one vendor's).

## Options

| | A. CLI over SSH, restricted key | B. MCP server | C. A2A |
|---|---|---|---|
| New inbound port | No: reuses the engineer's existing SSH | Over stdio: no (but it still needs A underneath to reach the box). Over HTTP on the box: yes, against D6 | Yes: an HTTP endpoint on the box |
| How the caller is authenticated | The SSH key. `authorized_keys` pins the key to a forced command that carries the identity, so the client can't claim one | Inherited from A (stdio) or needs its own auth (HTTP) | Needs its own auth |
| Works with any assistant | Yes: every coding assistant can run a shell command | Most assistants speak MCP, but it's another moving part | Built for agent-to-agent, not a human at a desk |
| Moving parts | An entry point and one `authorized_keys` line | A server process, plus A | A server, agent cards, task lifecycle |

## Recommendation

**A, with JSON output.** It keeps D6, binds identity to a key rather than to anything the client sends (C10), and works with any assistant. An MCP server can come later as a thin stdio wrapper on the engineer's machine that calls the same commands over the same SSH, with no change on the box. A2A fits a future where *agents* talk to CEO; that's not the desk interface.

How it works:
- The box's `authorized_keys` for the CEO user pins the engineer's key to
  `ceo-skill-shell <identity>` with `restrict` (no shell, no forwarding, no PTY).
- The entry point reads the requested command from `SSH_ORIGINAL_COMMAND`,
  splits it without a shell, and runs it only if it's on an allowlist.
- Decisions made through it go through the same gate code as chat, with route
  `skill` and the identity from `authorized_keys` (C10, T14).

Not tested here: MCP clients across vendors; A2A.
