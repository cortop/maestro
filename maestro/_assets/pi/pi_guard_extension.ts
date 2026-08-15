// maestro pi guard extension (T-59/PI-7).
//
// pi ships no permission system of its own -- no sandbox, no PreToolUse-style
// hook, no declarative per-tool ask/allow/deny map (see the pi docs' Security
// page: "Pi does not include a built-in sandbox... Extensions... run with the
// same permissions"). A headless run with stdin closed executes bash with
// zero gating. This extension is maestro's opt-out: a `tool_call` hook (pi's
// exact equivalent of Claude Code's PreToolUse hook and opencode's
// `tool.execute.before`) that blocks before the tool ever runs.
//
// Carries NO independent copy of any predicate. Every decision is made by
// `pi_guard_check.py` (this file's sibling, materialized alongside it by
// `maestro.pi_guard.install`), which itself imports `destructive_command_guard`
// unmodified -- the SAME module Claude Code's own PreToolUse hook
// (`.claude/hooks/block-home-deletion.py`) and opencode's guard plugin
// (`opencode_guard_plugin.mjs` -> `guard_argv.py --check`) both delegate to.
// See `pi_guard_check.py`'s own module docstring for the full source chain
// and why pi widens protection beyond what's safe for Claude/opencode (no
// other containment layer exists here).
//
// Covers two tool_call shapes:
//   - "bash": the command string, checked via `--check-bash`.
//   - "write"/"edit": the target path, checked via `--check-path` -- pi's
//     `write`/`edit` tools mutate the filesystem without going through bash,
//     so a bash-only guard would leave $MAESTRO_HOME (and this guard's own
//     install directory) writable through those tools instead.
//
// Verified against a REAL pi run (see this ticket's spec AC5/AC6): a live
// `rm -rf $MAESTRO_HOME/events` bash tool call is rejected with this
// extension loaded and succeeds without it.

import { execFileSync } from "node:child_process"
import { fileURLToPath } from "node:url"
import { dirname, join } from "node:path"

const __dirname = dirname(fileURLToPath(import.meta.url))
const CHECKER = join(__dirname, "pi_guard_check.py")

function runCheck(mode, value) {
  try {
    execFileSync("python3", [CHECKER, mode, value], { stdio: "pipe" })
    return null
  } catch (err) {
    const stderr = err && err.stderr ? err.stderr.toString() : String(err)
    return stderr.trim() || "BLOCKED by maestro pi guard"
  }
}

export default function (pi) {
  pi.on("tool_call", async (event) => {
    if (event.toolName === "bash") {
      const command = event.input && event.input.command
      if (!command) return
      const reason = runCheck("--check-bash", command)
      if (reason) return { block: true, reason }
      return
    }
    if (event.toolName === "write" || event.toolName === "edit") {
      const target = event.input && (event.input.path || event.input.file_path)
      if (!target) return
      const reason = runCheck("--check-path", target)
      if (reason) return { block: true, reason }
      return
    }
  })
}
