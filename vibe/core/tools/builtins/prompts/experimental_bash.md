Use the `bash` tool for shell commands that may run for a while, need ongoing
input, or should remain inspectable after the first tool call.

**Key characteristics:**
- Stateful sessions: each command gets a `session_id`, a PTY, and a durable log file.
- Background handling: use `bash(background=true)` for dev servers, watchers, and long builds that should keep running.
- Hard foreground timeout is the default: set a task-appropriate `timeout_seconds` (for example `300` for five minutes). Vibe kills the whole process group when it expires and reports a timeout error to the agent.
- After a timeout, treat the command as failed or hung: inspect its partial output, fix or split the operation, and retry with a bounded timeout. Do not repeatedly rerun the same unchanged command.
- Soft foreground timeout is opt-in: `bash(background=false, hard_timeout=false)` returns a live session when `timeout_seconds` expires. Use it only when the process is intentionally meant to remain inspectable.
- Long polling: `bash_output(cursor=N, wait_seconds=N, max_bytes=N)` waits internally, aggregates output, and returns on process exit, output cap, kill/reset, or wait-window expiration. `cursor` is a byte offset into the log; pass the `next_cursor` from the previous call to resume without re-reading.
- Interactive input: use `bash_stdin(session_id=..., text="...\n")` to press Enter or drive prompts, REPLs, and installers. Use `bash_stdin(control=["ctrl_c"])` for control bytes.
- Session management: `bash_sessions(action="list"|"inspect"|"kill"|"reset")` lists bash sessions, inspects one session, kills exactly one `session_id`, or resets all bash sessions. `inspect` and `kill` require a single `session_id`; `reset` ignores `session_id` (`reset(clear_logs=true)` also deletes stored logs).
- Log files: `bash_log_file(action="read", session_id=...)` reads a session's full output file; `write`/`append` annotate it once the session has exited.
- Spill files: full output is always stored under `~/.vibe/shell-tool/sessions/`.

**Prefer dedicated tools when available:**
- Read files with `read`, not `cat`, `head`, `tail`, or `sed` through bash.
- Search files with `grep`, not `grep`, `find`, or `rg` through bash.
- Edit files with `edit` or `write_file`, not shell redirection or `sed -i`.

**Good uses:**
- Build and test commands such as `npm run build`, `uv run pytest`, and `cargo test`.
- Dev servers and watchers such as `npm run dev`.
- Commands that ask for confirmation or provide a REPL.
- System checks, package manager inspection, and git commands.

**Examples:**
- Bounded build: `bash(command="npm run build", timeout_seconds=300)` kills a hung build after five minutes so the agent can recover.
- Dev server: `bash(command="npm run dev", background=true)`, then poll with `bash_output(wait_seconds=30)`.
- Prompt: `bash(command="some-installer", timeout_seconds=10)`, then `bash_stdin(text="y\n")` and `bash_output(wait_seconds=10)`.
- Interrupt: `bash_stdin(control=["ctrl_c"])` sends Ctrl-C to the PTY session.
