// opencode plugin: block destructive shell commands against MAESTRO_HOME (T-34/RF-5).
//
// A `tool.execute.before` pre-tool hook -- opencode's plugin equivalent of Claude
// Code's PreToolUse hook -- that shells out to guard_argv.py's `--check` mode (this
// file's sibling) for the actual decision, so this plugin carries NO independent
// copy of the destructive-command predicate: it is exactly as correct as
// destructive_command_guard.check_command, never a second implementation that can
// drift from it. Throwing from `tool.execute.before` aborts the tool call before it
// runs -- verified in a REAL run (opencode 1.18.15, a local ollama model, a real
// `rm -rf $MAESTRO_HOME/events` bash tool call actually attempted and blocked; see
// this ticket's spike Note for the transcript) that this genuinely stops execution,
// not just logs a warning.
//
// Wiring this into a real opencode-backed reconciler is OUT of this ticket's scope
// (maestro has no non-Claude spawn backend yet -- see gates.backend_interlock_reason,
// the predicate that refuses to spawn into one until it exists). This file is the
// proven-viable reference implementation: to use it, add
// `"plugin": ["<path to this file>"]` to the target project's opencode.jsonc.
//
// stdlib-only equivalent for JS: only Node's own `node:child_process`/`node:url`/
// `node:path`, no npm dependency -- this file is loadable as a plain local path
// (opencode resolves a relative/absolute `plugin` entry without needing the npm
// registry), which is what let this be spiked and verified with no network access.

import { execFileSync } from "node:child_process"
import { fileURLToPath } from "node:url"
import { dirname, join } from "node:path"

const __dirname = dirname(fileURLToPath(import.meta.url))
const GUARD_ARGV = join(__dirname, "guard_argv.py")

export const GuardPlugin = async () => {
  return {
    "tool.execute.before": async (input, output) => {
      if (input.tool !== "bash") return
      const command = output && output.args && output.args.command
      if (!command) return
      try {
        execFileSync("python3", [GUARD_ARGV, "--check", command], { stdio: "pipe" })
      } catch (err) {
        const stderr = err && err.stderr ? err.stderr.toString() : String(err)
        throw new Error(stderr.trim() || "BLOCKED by destructive-command guard")
      }
    },
  }
}
