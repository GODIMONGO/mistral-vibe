Use `powershell` to run a one-off PowerShell command on native Windows and
capture its output.

Usage:
- Each command runs independently in a fresh, stateless PowerShell process.
- Vibe resolves `pwsh.exe`, then `powershell.exe`, unless a PowerShell `shell` override is provided.
- `cmd.exe` is not used by this tool.
- Separate streams: this shell captures two pipes and reports them as `stdout` and `stderr`, so they are not interleaved. Read `exit_code` to tell success from failure.
- Set a task-appropriate `timeout_seconds` on every potentially slow command (for example `300` for five minutes). The default is 300 seconds and the process tree is killed when the limit expires.
- After a timeout, treat the command as failed or hung: inspect its partial output, fix or split the operation, and retry with a bounded timeout. Do not repeatedly rerun the same unchanged command.
- Prefer the dedicated tools over their shell equivalents:
  - reading files -> `read_file`
  - creating files -> `write_file`; modifying files -> `edit`
  - searching -> `grep`
- Appropriate uses: git operations, running tests and build tools, package management, and quick system checks.
