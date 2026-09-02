// spec-089 §5: local CLI commands. These are handled entirely inside the bundled CLI's own
// process (goal memory, context clear, compaction, model/effort switch, MCP inspection) — they
// never touch the SDK conversation. The CLI reads them from stdin between tool calls, so they
// only take effect at a turn boundary; a Stop-hook loop (/goal) never yields one on its own.
// ChatTab's sendMessage uses isLocalCliCommand to route these through the urgent-send path
// (interrupt + head-of-queue) instead of ordinary steer/queue when a turn is active.
export const LOCAL_CLI_COMMANDS = ['/goal', '/clear', '/compact', '/model', '/effort', '/mcp'];

/** True if `text` starts with one of LOCAL_CLI_COMMANDS as its own token (case-insensitive,
 * exact match on the first whitespace-delimited word — `/goals` is NOT `/goal`). */
export function isLocalCliCommand(text: string): boolean {
  const first = text.trim().split(/\s+/, 1)[0]?.toLowerCase() ?? '';
  return LOCAL_CLI_COMMANDS.includes(first);
}
