from __future__ import annotations

from vibe import __version__
from vibe.core.skills.models import SkillInfo, SkillSource

_PROMPT_TEMPLATE = """# Powerstral CLI Self-Awareness

You are running inside **Powerstral**, an autonomous fork of Mistral Vibe.
This skill gives you full knowledge of the application internals so you can help
the user understand, configure, and troubleshoot their Powerstral installation.

## Going Deeper

For Powerstral-specific facts not covered here, inspect the local README first,
then https://github.com/GODIMONGO/powerstral. Upstream Mistral documentation may
describe the base product but is not authoritative for fork-only capabilities.

## VIBE_HOME

The user's Vibe home directory defaults to `~/.vibe` but can be overridden via
the `VIBE_HOME` environment variable. All user-level configuration, skills, tools,
agents, prompts, logs, and session data live here.

### Directory Structure

```
~/.vibe/
  config.toml          # Optional user configuration, created on first saved setting
  hooks.toml           # User-level hook definitions
  .env                 # API keys and credentials (dotenv format)
  memory.json          # Bounded user-approved memory shared across projects
  vibehistory          # Command history
  trusted_folders.toml # Trust database for project folders
  connector_bootstrap_cache.json # Short-lived connector discovery cache
  agents/              # Custom agent profiles (*.toml)
  prompts/             # Custom prompts (*.md)
  skills/              # User-level skills (each skill is a subdirectory with SKILL.md)
  tools/               # Custom tools (<name>.py); descriptions & overrides in tools/prompts/<name>.md
  logs/
    vibe.log           # Main log file
    session/           # Session log files
  plans/               # Session plans

~/.agents/
  skills/              # Additional user-level skills directory
```

### Project-Local Configuration

When in a trusted folder, Vibe also looks for project-local configuration:
- `.vibe/config.toml` - Project-specific config (overrides user config)
- `.vibe/hooks.toml` - Project-specific hooks (requires trusted folder)
- `.vibe/skills/` - Project-specific skills
- `.vibe/tools/` - Project-specific tools (`<name>.py`); a `prompts/<name>.md` beside them sets or overrides the description of the tool named `<name>` — builtin, MCP, or custom (e.g. `.vibe/tools/prompts/bash.md` re-describes `bash`). Same `tools/*.py` + `tools/prompts/*.md` layout as the builtins.
- `.vibe/agents/` - Project-specific agents
- `.vibe/prompts/` - Project-specific prompts
- `.agents/skills/` - Standard agent skills directory

Custom Python tools will be deprecated in a future release. Recommend skills
for new extensions. When a user asks for migration help, inspect the custom
tool's behavior and replace it with an equivalent skill.

### AGENTS.md Discovery

`AGENTS.md` files provide directory-scoped instructions to the model. At startup,
Vibe loads `~/.vibe/AGENTS.md` and every `AGENTS.md` from the project root up
through the trust chain. `AGENTS.md` files in subdirectories are discovered
lazily: when `read_file` reads a file below the project root, any `AGENTS.md`
between the file's parent and the project root is injected into
context.

## Lifecycle: Exit, Update, Version, Resume

### Exit

Chat input (case-insensitive): `/exit`, `exit`, `quit`, `:q`, `:quit`.
Keyboard: `Ctrl+C` / `Ctrl+D` — press twice within ~1s to quit. For `Ctrl+C`,
the first press instead interrupts the running job or clears the input if either
is present. Set `ask_confirmation_on_exit = false` to make `Ctrl+D` quit on the
first press (also toggleable in `/config`); `Ctrl+C` always requires a second
press. `Ctrl+Z` suspends on POSIX (resume with `fg`).

The chat input remains active while an agent or shell command runs. Submitted
prompts, shell commands, and non-side-channel slash commands are queued in order.
On Windows, Vibe decodes ConPTY Win32 input packets so typing during a managed
Git Bash command produces normal characters instead of numeric escape sequences.

### Update

Powerstral never upgrades from the upstream `mistral-vibe` PyPI package because
that would remove fork functionality. Automatic update execution is disabled
until Powerstral has a fork-owned immutable release channel. `vibe
--check-upgrade` prints the safe repository location. Review local changes and
update from https://github.com/GODIMONGO/powerstral explicitly.

### Version

`vibe --version` (or `-v`) prints it and exits. Not shown anywhere in-session.

### Resume

- `vibe -c` / `--continue`: most recent session in this terminal (TTY-scoped;
  falls back to latest in cwd).
- `vibe --resume [SESSION_ID]`: specific session; without an id, opens a picker.
- In-session: `/resume` (alias `/continue`).

#### Session storage & folder scoping

Local sessions are written under `~/.vibe/logs/session/` (override with
`session_logging.save_dir`). Each session records the `cwd` it ran in. The
`/resume` picker, `--continue`, and bare `--resume` (no id) are **scoped to the
current folder**: only sessions whose `cwd` matches where Vibe is launched are
listed, so the same directory shows its own history and nothing else. Switch
folders to see a different set. The explicit `--resume <SESSION_ID>` form is
**not** folder-scoped: it resolves the session by id regardless of which folder
it ran in.

## Configuration (config.toml)

The configuration file uses TOML format. When it does not exist, Vibe uses its
built-in defaults and creates a sparse file on the first persisted setting.
Settings can also be overridden via environment variables with the `VIBE_`
prefix (e.g., `VIBE_ACTIVE_MODEL=local`).

Custom prompt IDs are resolved from project-local `.vibe/prompts/` first, then
from `~/.vibe/prompts/`, and finally from the built-in bundled prompts.

### Key Settings

```toml
# Model selection
active_model = "mistral-medium-3.5"  # Model alias to pin; omit or set "" to follow the server-routed default
fallback_model = ""                  # User-funded fallback alias; empty disables

# UI preferences
theme = "auto"  # Follow terminal background, then OS light/dark preference
disable_welcome_banner_animation = false
autocopy_to_clipboard = true  # Enable automatic copying of selected text to clipboard
file_watcher_for_autocomplete = false
ask_confirmation_on_exit = true  # Require a second Ctrl+D to quit (Ctrl+C always confirms)
show_greeting = true  # Show "Hello {name}" greeting below the banner at startup (Mistral providers, once per 24h)
log_level = "WARNING"  # Optional. DEBUG | INFO | WARNING | ERROR | CRITICAL — log level for ~/.vibe/logs/vibe.log
```

### Copy and Text Selection

- **Copy shortcuts**: `Ctrl+Y` and `Ctrl+Shift+C` both copy the current selection to the clipboard. When autocopy is enabled (default), releasing the mouse over a selection also copies automatically. Each successful copy flashes a brief inline "Copied to clipboard" notice.
- **Multi-click selection**: Double-click selects a word, triple-click selects the current paragraph; dragging extends the selection at the same granularity.

```toml
# Behavior
bypass_tool_permissions = false    # Skip tool approval prompts
system_prompt_id = "cli"          # System prompt: "cli", "lean", or custom .md filename
compaction_prompt_id = "compact"  # Compaction prompt: built-in "compact" or custom .md filename
enable_telemetry = true
enable_update_checks = true       # Daily PyPI check; prompts on next launch when a newer release exists
enable_notifications = true
enable_system_trust_store = false  # Use OS trust store for outbound HTTPS
api_timeout = 720.0               # API request timeout in seconds
api_retry_max_elapsed_time = 300.0  # Retry budget for retryable API failures in seconds
auto_compact_threshold = 200000   # Token count before auto-compaction

# Git commit behavior
include_commit_signature = true   # Add "Co-Authored-By" to commits

# System prompt composition
include_model_info = true         # Include model name in system prompt
include_project_context = true    # Include project context (git info, cwd) in system prompt
include_prompt_detail = true      # Include OS info, tool prompts, skills, and agents in system prompt

# Voice features
voice_mode_enabled = false
narrator_enabled = false
active_transcribe_model = "voxtral-realtime"
active_tts_model = "voxtral-tts"

# Autonomous goal mode (also enabled by `--agent autonomous`)
[autonomy]
enabled = false
aggressiveness = "medium"         # low | medium | high | max
goal_advisor_model = ""           # Empty means the active model
reviewer_model = ""               # Empty means advisor, then active model
vibe_thinking = "off"             # 0..4 independent candidates + compact synthesis
web_search_activity = "medium"     # off | low | medium | high | max
gauntlet_loop = false              # real bar + separate builder/critic loop
boost_mode = false                 # enforced Sonnet 5-class orchestration profile
personal_experience = true         # local SQLite RAG before each root decision
max_review_retries = 3
max_parallel_subagents = 4        # 0 disables subagents; maximum 16
max_live_child_runtimes = 8
max_subagent_result_chars = 8192
require_worker = true
require_review = true

# Native Windows desktop observation and input. The root agent owns the desktop;
# worker subagents intentionally do not receive this tool.
[tools.computer_use]
permission = "ask"
max_windows = 24
max_controls = 80
max_text_chars = 4000
```

The root runtime first assigns a zero-token local performance profile. Mini,
medium, and large classifications control orchestration depth only; they do not
impose a total wall-clock, token, or main-turn limit. Verification remains a
mandatory completion gate. Medium plans are capped at 6 tasks, large plans at 12; review retries
are capped at 1 and 2 respectively. The selected Vibe-thinking level is literal:
low/medium/high/max always run 1/2/3/4 visible passes and are not silently reduced
by the performance profile. A full multi-route cycle runs at task start, every ten
completed main turns, and immediately after a tool failure. Between full cycles a
visible 192-token `Fast thinking` call checks direction, strongest evidence,
largest proof gap, and the next action using low provider reasoning. `task` accepts optional
`max_turns` (1-64) and `timeout_seconds` (up to 5400), and automatic advisor,
worker, and reviewer calls always receive profile-derived limits so a hung child is
interrupted.

Autonomous root and child agents use fast batched execution: issue 2-4 independent
reads, searches, or verification calls in one model response so the runtime runs
them concurrently. Combine dependent cheap diagnostics into one bounded multiline
shell script, but keep overlapping mutations and contended tests sequential. Read
all results from the current wave before deciding the next one.

The active main model then performs a bounded intent analysis. Direct questions
and simple task-management requests bypass the advisor. A locally recognized code
action cannot be downgraded to direct by that classifier: mini work receives a
compact advisor/worker/reviewer pipeline, and medium work can dispatch up to three
independent explore or worker tasks concurrently. Parallel mutating workers must
own disjoint files or components; dependencies keep overlapping work serialized.
The runtime validates and writes the advisor's todo dependency graph (`depends_on` contains prerequisite
todo IDs), then automatically delegates ready work in dependency waves. Independent
read-only and disjoint mutating tasks can run concurrently. After the root integrates results, the runtime automatically
starts a fresh reviewer and requires specific `EVIDENCE_CHECKED: <claim> =>
<evidence>` records followed by `VERDICT: PASS`. The plan, subagent calls,
advisor status, and reviewer status are visible in the CLI.
Its read-only `swarm` is concurrency-bounded. Child output and resident runtimes
are bounded; persisted child sessions reload on demand. Use
`vibe --add-api-key ALIAS` to configure the exact provider credential for
an advisor/reviewer model without exposing the key. A configured Devstral alias is
the recommended Mistral-backed advisor; an empty alias safely follows the active
model. Advisor and reviewer child profiles force that selected model to
`thinking = "max"` without changing the main agent's thinking level.

Autonomous context is compacted before it is repeated across advisor, worker,
and reviewer calls. Scheduled autonomy refreshes compact only after 90% of the
configured context threshold is used; they never compact merely because a small
number of turns elapsed. The dedicated compaction model profiles the smallest
working set: objective, constraints, plan/file state, verified evidence, failed
routes, reload map, and next action. It drops stale tool output and narration.
The generated profile is capped at 4096 tokens, while verbatim prior user
messages have a separate 12000-token budget. Oversized subagent results retain
their head and final verdict, and reviewer evidence is deduplicated. Lower a
model's `auto_compact_threshold` below the 200000 default only when earlier
compaction is intentionally preferred.

`/effort` independently controls provider-model thinking, Vibe thinking, the 0-16
subagent limit, answer accuracy, web-search activity, and personal experience.
Personal experience is a bounded local SQLite RAG: it retrieves redacted relevant
code, test, advisor/reviewer, and web-search outcomes before every root decision
and updates them once per tool batch. Retrieval requires lexical or related-stem
relevance, then prioritizes the current project and repeated experience. It adapts
harness context rather than model weights and uses no embedding/API call.
Vibe thinking is a
harness-level preflight layer: `off|low|medium|high|max` runs `0|1|2|3|4` rigorous
independent deliberation calls and passes the refined brief to the main agent.
Each call requests the matching native model reasoning effort when the provider
supports it, plus a level-scaled
768/1024/1280/1536 output budget, and up to 24,000 characters of current evidence.
The final brief must separate observations from assumptions, compare competing
routes, state the strongest counterargument, define an observable verification,
and name a concrete pivot trigger. It
runs before every root-agent decision, including after tool, command, and web-search
results, but not recursively in subagents. Each pass and decision uses additional
tokens; its brief is removed immediately after the decision to avoid growing the
session context. The loading line first displays `Vibe thinking N/M`, then a bounded
decision summary with `CONTINUE`/`PIVOT`, the next action, and the largest proof gap.
The same summary remains on the collapsed thought row; expanding it shows only the
operational goal, evidence, plan, completion gate, and pivot trigger rather than raw
hidden chain-of-thought. The refined brief flags claims that need web verification and directs the main
agent to ask one focused clarification instead of guessing when an ambiguity is
material. Accuracy levels `low|medium|high|max` map to
temperatures `1.0|0.7|0.2|0.0`. Web activity `off|low|medium|high|max` disables
search at `off`; `low` pre-searches explicit research/search requests; `medium`
also pre-searches time-sensitive, version, documentation, API, comparison, and
verification requests; `high` and `max` pre-search substantive root requests except
source-bound local/private work. Automatic search never submits raw local paths,
Codex thread references, authenticated URLs, host/IP targets, credentials, or private
logs. The agent inspects user-provided local sources first and, when external docs are
still needed, uses a generic redacted query.
At `high|max`, a failed non-web tool also triggers one deduplicated visible recovery
search for current primary documentation. Obvious greetings are skipped.
The normal tool event shows the query, result, and sources or an explicit failure.
The direct form is
`/effort MODEL_THINKING SUBAGENTS ACCURACY WEB VIBE_THINKING GAUNTLET BOOST EXPERIENCE`,
where the last three switches are `off|on`. BOOST is a persisted, enforced quality
profile targeting Sonnet 5-class behavior through orchestration rather than
claiming to replace the underlying model. It atomically selects max model/Vibe
thinking, deterministic accuracy, visible max web verification, 16 subagents,
Gauntlet, required workers, and a fresh evidence-based reviewer. Its advisor
separates facts from assumptions and defines observable proof; its reviewer
re-opens artifacts, runs relevant checks, challenges the approach against an
alternative, and rejects unsupported claims. Trivial greetings skip automatic
search and the four extra deliberation calls to avoid wasting tokens. Gauntlet
Loop adapts the CC BY 4.0 pattern from
`robonuggets/gauntlet-loop`: acquire a named fetchable comparable quality bar,
separate builders from a fresh harsh critic, compare actual outputs blindly, and
repeat until ours wins, the user stops, or a runtime safety/resource limit blocks
progress. UltraCode remains a separate maximum-swarm execution mode and enables
BOOST as one component. Neither BOOST nor a thinking control proves equivalence
to another closed model or guarantees certainty without task-specific evals.

The Textual UI keeps a persistent task status bar above the input while a todo
plan exists. It shows the current, completed, waiting, and cancelled tasks,
updates from live todo events, and restores the latest plan after resume.
`/tasks` prints the same plan status in the transcript. `/tasks clear` dismisses
the current plan locally without an LLM call and remembers that dismissal for the
session until a new todo plan is created. Natural-language
task-management requests go through the main model's intent analysis and bypass the
advisor when classified as direct.

On Windows, `computer_use` provides bounded structured observation, one
overwriting PNG screenshot in the session scratchpad, focus, click, Unicode typing,
key chords, and scrolling. Completed captures are attached to the next request for
vision-capable models; text-only models retain the structured state. Observation
and screenshots are read-only; mutating actions follow
`[tools.computer_use].permission`. Never pass secrets in `text`, because tool
arguments are session-visible. Win32 imports are lazy and the tool is unavailable
on other operating systems. Worker subagents intentionally do not receive desktop
control. Advisor plans must assign desktop interaction and observable-state
verification to `root`, preventing parallel agents from racing for the mouse and
keyboard. Pure capability questions bypass the autonomous advisor and are answered
directly.

`chrome_cdp` controls Chrome through an explicit loopback DevTools endpoint. It
lists tabs, opens/navigates pages, snapshots the accessibility tree, clicks/types
by node ID, captures model-visible screenshots, and executes arbitrary page
JavaScript with `evaluate`. Configure `[tools.chrome_cdp]` with endpoint
`http://127.0.0.1:9222`; Chrome must already be running with remote debugging.
Endpoints, inputs, outputs, and images are bounded. `evaluate` can read or mutate
signed-in page data and follows the normal tool permission.

Telegram remote control is opt-in. Set `TELEGRAM_BOT_TOKEN` and the strict
comma-separated `VIBE_TELEGRAM_ALLOWED_CHAT_IDS`, then locally run `/telegram
start`. It never auto-starts, ignores unauthorized chats, bounds messages, and
does not expose the token. `/telegram status|chats|stop` manages it.

### Automatic Model Fallback

`fallback_model = "ALIAS"` treats the active Mistral model as enhanced access and
uses the configured fallback model when Mistral credentials are unavailable or
Mistral returns HTTP 401/402 or a quota-specific HTTP 403/429. The switch is sticky
for the root session and its subagents. Ordinary rate limits and other API errors
are not hidden. Streaming switches only before the first chunk, so two providers'
answers are never combined. Empty disables fallback.

Advisor orchestration also fails open: if the dedicated goal-advisor subagent cannot
complete (for example because its separate API is exhausted), Vibe visibly retries
planning with the active main model. If that utility call also fails, Vibe materializes
a conservative root-owned plan and lets the main agent continue; it does not abandon
the user's turn solely because the optional advisor is unavailable.

The alias must refer to a `[[models]]` entry whose `[[providers]]` entry uses a
separate `api_key_env_var`. Run `vibe --add-api-key ALIAS` to enter or replace
that credential through the masked keyring/`.env` flow. It is the short form of
`vibe --setup --setup-model ALIAS`. Never put the key itself in `config.toml` or
as a command-line argument.

### OpenTelemetry Tracing

Set `enable_otel = true` to export traces for agent, model, and tool operations
over OTLP/HTTP. `enable_telemetry` must also be enabled. With no explicit
endpoint, Vibe derives the telemetry endpoint and API key from the configured
Mistral provider, except public regional API hosts that do not serve telemetry.

To use another collector, set `otel_endpoint` to its base URL; Vibe appends
`/v1/traces`. Configure custom-collector authentication through the standard
`OTEL_EXPORTER_OTLP_*` environment variables.

`otel_redaction` controls client-side span attribute redaction: `default`
redacts sensitive values, `strict` redacts sensitive attributes entirely, and
`none` disables redaction. Use `none` only for a collector trusted to receive
potentially sensitive prompt, response, and tool data.

```toml
enable_otel = true
otel_endpoint = "https://collector.example.com:4318"
otel_redaction = "default"
```

### Providers

```toml
[[providers]]
name = "mistral"
api_base = "https://api.mistral.ai/v1"
api_key_env_var = "MISTRAL_API_KEY"
backend = "mistral"

[[providers]]
name = "llamacpp"
api_base = "http://127.0.0.1:8080/v1"
api_key_env_var = ""
extra_headers = { "X-Custom-Header" = "value" }  # optional per-provider HTTP headers
emits_finish_reason = false  # set false for OpenAI-compatible endpoints that end
                             # streams without a finish reason; avoids spurious
                             # "incomplete stream" errors and retries (default true)
enable_streaming = false  # use one non-streaming request when the endpoint cannot stream
supports_stream_options = false  # omit OpenAI stream_options when unsupported
supports_reasoning_effort = false  # omit reasoning_effort when unsupported
```

The three capability switches default to `true`. Set only the unsupported
features to `false` for local or partially OpenAI-compatible servers; Powerstral
then changes the actual request instead of repeatedly sending rejected fields.

### Models

```toml
[[models]]
name = "mistral-vibe-cli-latest"
provider = "mistral"
alias = "mistral-medium-3.5"
temperature = 1.0
input_price = 1.5
output_price = 7.5
cached_input_price = 0.15         # per million cached input tokens; omit to bill at input_price
thinking = "high"                 # "off", "low", "medium", "high", "max"
auto_compact_threshold = 200000
supports_images = true            # vision-capable; allows @-mentioned images

[[models]]
name = "devstral-small-latest"
provider = "mistral"
alias = "devstral-small"
input_price = 0.1
output_price = 0.3
cached_input_price = 0.01

[[models]]
name = "devstral"
provider = "llamacpp"
alias = "local"
```

### Tool Configuration

```toml
# Additional tool search paths
tool_paths = ["/path/to/custom/tools"]

# Enable only specific tools (glob and regex supported)
enabled_tools = ["bash", "read_file", "grep"]

# Disable specific tools after enabled_tools filtering
disabled_tools = ["web_fetch"]

# Per-tool configuration
[tools.bash]
allowlist = ["git", "npm", "python"]

[tools.git_bash]
permission = "ask"
shell = "C:\\Program Files\\Git\\bin\\bash.exe"

[tools.powershell]
permission = "ask"
shell = "powershell.exe"

[tools.web_search]
permission = "ask"
engine = "auto"                   # "auto", "mistral", or no-key "public"
timeout = 30
max_results = 5                   # 1-10; public response body is capped

[tools.memory]
permission = "ask"                # "always" permits autonomous memory writes
auto_remember = false              # Proactively save clear durable preferences
```

`web_search` is available without a separate API key through the bounded public
engine. `auto` tries hosted Mistral search when credentials are available and falls
back to public search on quota, rate-limit, or service errors. Autonomous explore,
advisor, reviewer, and worker profiles include the tool. Use `engine = "public"`
to avoid hosted search token usage entirely.

`memory` stores explicit user-approved notes in `~/.vibe/memory.json` and injects
a bounded global-memory section into future sessions across all projects. Actions
are `remember`, `list`, and `forget` (the latter requires the stable entry ID).
The store is capped at 100 entries, 2,000 characters per entry, and 12,000 prompt
characters; exact notes are deduplicated. It rejects likely credentials and must
not be used for secrets, payment data, transient task state, or unverified
assumptions. Saved facts are potentially stale and must be verified before use.
With `[tools.memory].auto_remember = true`, the root model proactively calls
`remember` when the user clearly expresses a durable preference, corrects a
recurring behavior, or confirms a long-lived convention. It does not require a
`/memory` command and must not infer or save personal details, secrets, temporary
task state, guesses, or facts learned only from tools/web pages. The default is
off; writes still follow the memory tool permission.
The first root request visibly reports whether memory was loaded, empty, skipped,
or invalid. A loaded report means the entries are in every model request for that
session; their contents remain hidden from the status message.

Fast working memory is separate from global memory. After each root tool batch,
Powerstral appends one bounded session ledger containing up to 12 deduplicated
action fingerprints, safe action summaries, statuses, and actual results. Only the
latest ledger is sent to the model, but it remains append-only in the session log
and survives compaction and resume. The model must consult it before acting: do not
repeat a successful action without fresh need, and do not repeat a failed action
without changing inputs or conditions. The ledger is capped at 8,000 characters,
redacts likely credentials, and is never promoted to cross-project global memory.

The built-in shell surface is controlled by the `managed_shell_tools_enabled` config
field and the `vibe_cli_managed_shell_tools` GrowthBook experiment. The default variant
keeps the legacy one-shot `bash` tool, including its existing Windows behavior.
The managed variant exposes OS-native shell tools:
POSIX systems, including WSL where Vibe runs as Linux, get managed `bash`,
`bash_output`, `bash_stdin`, `bash_sessions`, and `bash_log_file`; native Windows
gets `git_bash`, `git_bash_output`, `git_bash_stdin`, `git_bash_sessions`, and
`git_bash_log_file` when Git Bash is available. If Git Bash is unavailable,
native Windows falls back to `powershell`, `powershell_output`,
`powershell_stdin`, `powershell_sessions`, and `powershell_log_file`.

Managed shell sessions return a `session_id`, inline output, a cursor for polling
more output, and a log path under `~/.vibe/shell-tool/sessions/`. Long-running
commands can be left alive with `background = true`, and interactive commands can
be driven with the matching stdin tool.
Foreground commands use hard timeouts by default. The model should set a bounded
`timeout_seconds` appropriate to the task (for example `300` for five minutes); on
expiry Vibe terminates the process tree and returns a timeout error so the agent can
inspect partial output, fix or split the operation, and retry. Set
`hard_timeout = false` only to intentionally hand a live session over for polling.

POSIX `bash` reads permissions, allowlists, and denylists from `[tools.bash]`.
Native Windows `git_bash` reads them from `[tools.git_bash]`; native Windows
`powershell` reads them from `[tools.powershell]`. Neither Windows tool reads
`[tools.bash]`. Git Bash is preferred when Vibe can resolve a usable `bash.exe`
from PATH, Git for Windows, or standard Git install locations. If Git Bash is
unavailable, the PowerShell resolution order is `pwsh.exe`, then
`powershell.exe`. `cmd.exe` is not used by the managed Windows shell tools.
Output polling uses byte offset cursors
(`cursor` / `next_cursor`), `max_bytes` caps per-call inline output, and
`max_inline_bytes` configures the default cap.

**Special case — `find` command:** Even if `find` is in the bash allowlist,
Vibe detects `-exec`, `-execdir`, `-ok`, and `-okdir` predicates and will
prompt for user permission before running the command.

#### File Tool Permission Resolution

File-based tools (`read`, `grep`, `write_file`, `edit`) resolve
permissions in this order (first match wins):

1. **Scratchpad** path → always allowed
2. **denylist** glob match → always denied
3. **allowlist** glob match → always allowed
4. **sensitive_patterns** match → requires approval
5. **Outside workdir** → requires approval (or denied if `permission = "never"`)
6. **Default** → uses the tool's `permission` setting

The **denylist** is checked before the allowlist — a path matching both lists
is denied. Both are checked before the outside-workdir boundary, so the
allowlist can still auto-approve access to directories outside the project.

### Skill Configuration

```toml
# Additional skill search paths
skill_paths = ["/path/to/custom/skills"]

# Enable only specific skills
enabled_skills = ["vibe", "custom-*"]

# Disable specific skills
disabled_skills = ["experimental-*"]
```

### Agent Configuration

```toml
# Additional agent search paths
agent_paths = ["/path/to/custom/agents"]

# Enable/disable agents
enabled_agents = ["ask", "plan"]
disabled_agents = ["auto-approve"]

# Opt-in builtin agents (only affects agents with install_required=True, e.g. lean)
installed_agents = ["lean"]

# Agent profile to use when --agent is not passed
# (default: "accept-edits"). Valid values: "ask", "plan", "accept-edits",
# "auto-approve", "lean" (only when listed in installed_agents), or any
# custom agent name from ~/.vibe/agents/ or .vibe/agents/. Subagents
# (e.g. "explore") are rejected. Applies in both interactive and programmatic
# (-p/--prompt) mode.
default_agent = "plan"
```

### MCP Servers

Remote MCP servers can be added non-interactively from the shell:

```bash
vibe mcp add mistralai \\
  --url https://api.mistral.ai/mcp \\
  --transport streamable-http \\
  --api-key-env MISTRAL_API_KEY

vibe mcp add linear \\
  --url https://mcp.linear.app/mcp

vibe mcp remove mistralai
```

Static auth is selected when `--api-key-env` or `--header` is provided.
Otherwise the server uses OAuth and starts browser login by default. Pass
`--no-login` to only persist the OAuth configuration. Run
`vibe mcp add --help` for all supported authentication and timeout options.
Use `vibe mcp remove <name>` to remove a server from the user configuration;
stored OAuth credentials are deleted when available.

Hosted OAuth MCP servers can also be added from inside Vibe:

```text
/mcp add https://mcp.linear.app/mcp
/mcp add https://mcp.example.com/mcp --name docs --scope read --transport http --no-login
```

`/mcp add` is OAuth-only. It writes `auth.type = "oauth"` with optional
scopes and starts login by default. It uses `transport = "streamable-http"`
unless you pass `--transport http`. Pass `--no-login` to add the server without
starting OAuth login. The shortcut supports `streamable-http` and `http`
transports.

```toml
[[mcp_servers]]
name = "my-server"
transport = "stdio"
command = "npx"
args = ["-y", "@my/mcp-server"]

[[mcp_servers]]
name = "remote-server"
transport = "http"
url = "https://mcp.example.com"

[mcp_servers.auth]
type = "static"
api_key_env = "MCP_API_KEY"
api_key_header = "Authorization"
api_key_format = "Bearer {token}"

[[mcp_servers]]
name = "linear"
transport = "streamable-http"
url = "https://mcp.linear.app/mcp"

[mcp_servers.auth]
type = "oauth"
scopes = ["read", "write"]
# Optional: client_id = "pre-registered-public-client"
# Optional: client_metadata_url = "https://example.com/client-metadata.json"
# Optional: redirect_port = 47823
```

HTTP MCP servers can use either static auth or OAuth:

- Static auth: legacy `api_key_env` / `headers` keys still work and are
  promoted to `auth.type = "static"` internally.
- OAuth auth: use `auth.type = "oauth"` with `scopes`. Vibe stores tokens
  in the OS keyring under `mcp-oauth:<alias>:tokens`, dynamic client info
  under `mcp-oauth:<alias>:client_info`, and config drift fingerprints under
  `mcp-oauth:<alias>:fingerprint`.
- Headless environments without an OS keyring cannot store OAuth tokens; use
  static auth via `api_key_env` instead.
- For SSH/remote browser callbacks, forward the loopback port:
  `ssh -L 47823:127.0.0.1:47823 <host>`.

### Connectors

Mistral connectors are auto-discovered when the active provider is Mistral
and the API key env var is set. Toggle the master switch or hide individual
connectors / tools:

```toml
enable_connectors = true          # Master switch (default: true)

[[connectors]]
name = "github"
disabled = true                   # Hide all tools from this connector

[[connectors]]
name = "linear"
disabled_tools = ["delete_issue"] # Hide selected tools only
```

### Session Logging

```toml
[session_logging]
enabled = true
save_dir = ""                     # Defaults to ~/.vibe/logs/session
session_prefix = "session"
```

### Browser Sign-In

Browser sign-in lets users authenticate through the browser during onboarding.
Mistral providers use default browser sign-in URLs (`console.mistral.ai` /
`api.mistral.ai`). Custom or renamed providers must configure both URLs:

```toml
[[providers]]
browser_auth_base_url = "https://console.mistral.ai"
browser_auth_api_base_url = "https://console.mistral.ai/api"
```

Self-hosted deployments can point Vibe CLI upgrade and API-key links to their
Le Chat web deployment, where the Vibe API key is managed:

```toml
vibe_base_url = "https://chat.mistral.ai"
```

Interactive setup can target a Mistral-compatible deployment instead of the
default `console.mistral.ai` / `api.mistral.ai`. The final credential is always a
Mistral API key. On the auth-method screen pick **Sign in with link or QR**, then
**Other** on the sign-in-target screen, enter a login domain, and open the
displayed link or scan its QR code. This sets `browser_auth_base_url` (the entered domain) and
derives `browser_auth_api_base_url` (`DOMAIN/api`). The overridden `mistral`
provider is persisted to user config so subsequent runs reuse it.

The wizard reads any custom `browser_auth_base_url` already in `config.toml`:
choosing **Other** pre-fills that configured domain so it can be confirmed or
edited. Choosing **Mistral AI** while a custom domain is configured warns first
and requires pressing **Enter** again to confirm the reset to the default
domain, which is then persisted.

### Hooks

Hooks let users run shell commands automatically at lifecycle events. They
are always available — no flag is required; dropping a `hooks.toml` in place
is enough.

#### Config and hook types

Hooks live in `hooks.toml` files (separate from `config.toml`), discovered in
this order:

1. `<project>/.vibe/hooks.toml` — loaded first, only when the folder is
   trusted.
2. `~/.vibe/hooks.toml` — loaded second.

A duplicate `name` across the two files is reported as a config issue and the
project entry wins. Config-load errors (invalid TOML, missing required
fields) surface in the TUI as warnings and the offending hook is skipped.

```toml
[[hooks]]
name = "lint"                       # Required: unique within the file.
type = "post_agent"                 # Required: post_agent | pre_tool | post_tool.
command = "eslint --quiet ."        # Required: shell command run in cwd.
timeout = 60.0                      # Default: 60s for all hooks.
description = "Run ESLint"          # Optional.

[[hooks]]
name = "deny-rm-rf"
type = "pre_tool"
match = "bash"                      # Tool-name matcher (tool hooks only, default "*").
strict = true                       # Tool hooks only: escalate any failure to deny/clear.
command = "uv run python /path/to/guard-bash"
```

| Type | When it runs |
|---|---|
| `post_agent` | Once per turn, after the agent finishes responding (no pending tool calls). |
| `pre_tool` | Per tool call, before the user permission prompt. |
| `post_tool` | Per tool call, **iff the tool body actually ran**. `tool_status` is `success`, `failure`, or `cancelled`. Does not fire when the tool never executed (`pre_tool` denial, user denial at the approval prompt, permission `NEVER`, or cancellation before the body started). |

**Matcher syntax** (same as `enabled_tools`): fnmatch glob by default
(`"bash"`, `"read_*"`, case-insensitive), or a regex full-match when the
pattern starts with `re:` (`"re:(read_file|grep)"`). `match` is forbidden on
`post_agent`.

**Tool name conventions** for matchers:
- Built-in tools use their bare name (`bash`, `read_file`, …); see the Tools
  section above for the full list.
- MCP tools: `{server-name}_{raw-tool-name}` (e.g. `linear_create-issue`).
- Connector tools: `connector_{normalized-name}_{remote-tool-name}` (e.g.
  `connector_Google_Drive_search_files`).
- Subagents all route through `task`. Match with `match = "task"` and read
  `tool_input.agent` to discriminate by subagent.

Subagent invocations inherit the parent's hook config. Their hook events are
logged to the subagent's session log and don't propagate to the parent's UI.

#### Wire protocol

Every hook is spawned in `cwd` and receives a JSON object on **stdin**
discriminated by `hook_event_name`:

```json
// post_agent
{"hook_event_name": "post_agent", "session_id": "...",
 "parent_session_id": null, "transcript_path": "...", "cwd": "..."}

// pre_tool
{"hook_event_name": "pre_tool", "session_id": "...", "parent_session_id": null,
 "transcript_path": "...", "cwd": "...",
 "tool_name": "bash", "tool_call_id": "call_42",
 "tool_input": {"command": "ls"}}

// post_tool
{"hook_event_name": "post_tool", "session_id": "...", "parent_session_id": null,
 "transcript_path": "...", "cwd": "...",
 "tool_name": "bash", "tool_call_id": "call_42",
 "tool_input": {"command": "ls"},
 "tool_status": "success",         // success | failure | cancelled
 "tool_output": {"output": "..."},  // the tool's serialized result (success/cancelled); null otherwise
 "tool_output_text": "...",         // current text the LLM will see; mutable by prior hooks
 "tool_error": null,                // populated on failure/skipped
 "duration_ms": 42.5}
```

`parent_session_id` is set when running inside a subagent. Exceeding
`timeout` kills the whole process tree.

A hook signals back via its **exit code** and **stdout** (stderr is reserved
for diagnostics — Vibe never parses it for control):

| Exit | Stdout | Behavior |
|---|---|---|
| `0` | empty | Pass through (no action). |
| `0` | valid structured-response JSON object (schema below) | Act per the JSON fields. |
| `0` | anything else (free-form text, broken JSON, scalar/array, schema mismatch) | Failure path (see below). The parse error is in the message. |
| non-zero / timeout / spawn failure | — | Failure path. Reason taken from stderr, then stdout, then the exit code. |

Structured-response schema:

```json
{
  "decision": "allow" | "deny",          // optional; default "allow"
  "reason": "string",                     // required when decision == "deny"
  "system_message": "string",             // optional UI note
  "hook_specific_output": {
    "tool_input": { ... },                // pre_tool only
    "additional_context": "string"        // post_tool only
  }
}
```

Unknown fields are tolerated at every level. Fields that aren't meaningful
for the current hook type are silently ignored.

**Don't self-name in `system_message` or `reason`** — the UI prefixes
hook-end-event content with `[hook-name]` automatically, and `pre_tool`
denials are wrapped as ``Tool 'X' was denied by hook 'Y': {reason}`` before
the LLM sees them. A hook that writes ``"reason": "guard: refused..."``
will produce ``hook 'guard': guard: refused...`` downstream.

`decision: "deny"` per hook type:

| Hook | Effect of `decision: "deny"` |
|---|---|
| `pre_tool` | Deny the tool call; `reason` is the tool error returned to the LLM. First deny short-circuits the remaining `pre_tool` hooks for this call. |
| `post_tool` | Replace `tool_output_text` with `reason`. Pipeline continues; subsequent hooks see the replacement. |
| `post_agent` | Inject `reason` as a retry user message. Capped at 3 retries per hook per user turn. |

Event-specific payloads:

- `hook_specific_output.tool_input` (`pre_tool`): full replacement of the
  model's arguments. Vibe re-validates against the tool's schema **after each
  rewriting hook** — the first invalid rewrite aborts the chain and
  synthesizes a denial attributing the failure to that hook. Rewrites
  compose: hook N receives `tool_input` as rewritten by hooks 1..N-1.
- `hook_specific_output.additional_context` (`post_tool`): text appended
  (with `\n`) to the current `tool_output_text`. Composes with a same-hook
  `decision: "deny"`: deny replaces first, then `additional_context` is
  appended to the replacement.

**Failure path.** Any failure (non-zero exit, timeout, spawn failure,
non-conforming stdout) emits a UI warning and lets the gated action proceed
(fail open). With `strict = true` on a tool hook:

| Hook | Strict failure escalates to |
|---|---|
| `pre_tool` | Deny the tool call with the failure reason. |
| `post_tool` | Clear `tool_output_text` (replace with empty). |

`strict` is forbidden on `post_agent`.

#### Execution semantics

- Hooks of the same type fire sequentially in load order (project file first,
  then user file; declaration order within each file).
- Tool calls within a single LLM turn run **concurrently**; each call's hook
  chain runs serially but the chains run in parallel across calls. Hooks
  that touch shared state (filesystem, env) must coordinate themselves.
- `pre_tool` rewrites take effect everywhere downstream: the user
  permission prompt sees the rewritten arguments, the tool runs with them,
  and the assistant message is patched so subsequent LLM turns reflect what
  actually ran.

### Pattern Matching

Tool, skill, and agent names support three matching modes:
- **Exact**: `"bash"`, `"read_file"`
- **Glob**: `"bash*"`, `"mcp_*"`
- **Regex**: `"re:^serena_.*$"` (full match, case-insensitive)

## CLI Parameters

```
vibe [PROMPT]                       # Start interactive session with optional prompt
vibe -p TEXT / --prompt TEXT         # Programmatic mode using `default_agent`, one-shot, exit
vibe -p TEXT --auto-approve          # Programmatic mode with all tool calls approved
vibe -p TEXT --agent lean --yolo      # Lean mode with all tool calls approved
vibe --agent NAME                   # Select agent profile (falls back to `default_agent` config)
vibe --auto-approve / --yolo         # Approve all tool calls for the selected agent
vibe --workdir DIR                  # Change working directory
vibe --worktree NAME                # Create/reuse a git worktree under $VIBE_HOME/worktrees on branch NAME and run inside it. Auto-cleanup only for worktrees Vibe created this run and only after a session started; reused worktrees and attached (pre-existing) branches are kept unless confirmed. -p sessions keep worktrees. Ignored with --setup/--check-upgrade.
vibe --worktree                     # Same, but Vibe picks an unused name from the prompt (a random slug when there is no prompt) on a vibe/<name> branch, and never reuses an existing worktree. The prompt must precede the flag or follow a `--`, since --worktree otherwise reads it as NAME.
vibe --add-dir DIR                  # Extra working dir loaded for context (repeatable). Implicitly trusted.
vibe --trust                        # Trust cwd for this invocation only (not persisted)
vibe -c / --continue                # Continue most recent session in this terminal (TTY-scoped, falls back to latest in cwd)
vibe --resume [SESSION_ID]          # Resume a specific session
vibe -v / --version                 # Show version
vibe --setup                        # Run onboarding/setup
vibe --check-upgrade                # Check for a Vibe update now, prompt to install it, and exit
vibe --max-turns N                  # Max assistant turns (programmatic mode)
vibe --max-price DOLLARS            # Max cost limit (programmatic mode)
vibe --max-tokens N                 # Max total session tokens (programmatic mode)
vibe --enabled-tools TOOL           # Enable specific tools (repeatable)
vibe --disabled-tools TOOL          # Disable specific tools (repeatable)
vibe --output text|json|streaming   # Output format (programmatic mode)
```

## Built-in Agents

There are two kinds of agents:
- **Agents** are user-facing profiles selectable via `--agent` or `Shift+Tab`.
  They configure the model's behavior, tools, and system prompt.
- **Subagents** are model-facing: the model can spawn them autonomously to delegate
  subtasks (e.g. exploring the codebase). Users cannot select subagents directly.

### Agents

- **ask**: Requests approval for tool executions
- **plan**: Planning-focused agent
- **accept-edits**: Default agent; auto-approves file edits but asks for other tools
- **auto-approve**: Auto-approves all tool calls
- **autonomous**: Goal advisor, mandatory todo planning and worker delegation,
  bounded read-only swarm, fresh verification, and final reviewer. Uses managed
  shell and native Windows `computer_use` control with permission bypass while
  retaining critical safety instructions.
- **lean**: Specialized Lean 4 proof assistant. Not available by default — must be
  installed with `/leanstall` (removed with `/unleanstall`). Use `--agent lean
  --auto-approve` or `--agent lean --yolo` to run Lean mode without tool prompts.

### Subagents

- **explore**: Read-only codebase exploration subagent with grep, file reading,
  and skill loading. Spawned by the model, not selectable by the user.
- **goal-advisor**: Read-only acceptance-criteria and dependency-plan advisor.
- **reviewer**: Read-only evidence reviewer; records checked claim/evidence pairs,
  then emits `VERDICT: PASS` or `FAIL`.
- **worker**: Implementation subagent with file tools and permissioned shell access.

Custom agents are TOML files in `~/.vibe/agents/NAME.toml`.

## Built-in Slash Commands

- `/harness` - Show the active session harness phase, event sequence, plugin
  composition, and capabilities. Powerstral's normal Python runtime is a
  session-scoped plugin harness: model, tools, permissions, orchestration,
  subagents, memory, web research, compaction, review, and hooks remain outside
  the replaceable LLM backend. Plugins may intercept typed turn/step/model/tool
  lifecycle phases through an ordered waterfall and can require another
  verification step before completion.
- `/api-key` - Select a configured model and securely add or replace its API
  key in a masked field. Pass an alias (`/api-key ALIAS`) to skip the picker.
- `/opencode-go` - Securely connect an OpenCode Go subscription. The command
  keeps the main Mistral worker selected with thinking `medium` and assigns
  `opencode-go/deepseek-v4-flash` (thinking `max`, temperature `0.0`) to both
  goal advisor and reviewer. The built-in OpenCode Go catalog covers Chat Completions,
  Anthropic Messages, and OpenAI Responses models.
- `/goal <objective>` - Switch to the autonomous agent and run the objective with
  mandatory planning, worker delegation, fresh verification, and final review.
  When the session is busy, the command waits in the main input queue.
- `/help` - Show help message
- `/config` - Full-screen settings browser. Fields show their value and origin layer (default / TOML / env / override). Type to filter, arrows to move, Enter to edit; booleans toggle, closed-set fields (theme, models) pick from a list, scalars edit inline, complex fields open a JSON editor. The edit modal shows an inspector of the layers setting the field; edits persist to the TOML layer by default, `Tab` targets the ephemeral session override (until restart), and `Ctrl+R` clears the field one writable layer at a time toward the default. The `tools` field opens a grouped tool list with a per-tool config editor (permission, allow/deny lists, `Ctrl+E` for raw JSON). Enabling/disabling whole MCP servers or connectors stays in `/mcp`.
- `/model` - Open the model control center for the main model, goal advisor,
  reviewer, model thinking, effort, and OpenCode Go. Use `/model main`,
  `/model advisor`, or `/model reviewer` to jump directly to one role picker.
- `/thinking` - Select provider-native internal reasoning. Higher levels reason
  more thoroughly but do not guarantee certainty.
- `/effort` - Open the slider panel for independent model thinking, Vibe
  strategic reflection (`0` to `4` independent candidates plus compact-model
  synthesis with a mandatory plan and completion gate), subagent
  limit (`0` to `16`), accuracy, web search, and Gauntlet Loop. Direct form:
  `/effort MODEL_THINKING SUBAGENTS ACCURACY WEB VIBE_THINKING GAUNTLET BOOST EXPERIENCE`.
  The BOOST row enforces the complete research/planning/worker/reviewer quality
  pipeline. The separate UltraCode row enables BOOST plus maximum swarm execution.
- `/boost [objective]` - Enable the BOOST intelligence profile or run an
  objective through its evidence-driven pipeline.
- `/subagents` - Show active and recent child-agent tasks.
- `/ultracode [objective]` - Open the UltraCode preset or run a difficult
  objective with max thinking and the maximum bounded swarm.
- `/pc <objective>` - Run a root-owned computer-control objective.
- `/browser <objective>` - Run a Chrome CDP/browser-control objective.
- `/web <objective>` - Run an evidence-backed web-engineering objective.
- `/memory <request>` - Read or update bounded global memory.
- `/telegram start|stop|status|chats` - Manage Telegram remote control.
- `/theme` - Select Textual UI theme; `auto` follows terminal/OS appearance (persisted in config)
- `/reload` - Reload configuration, agent instructions, and skills from disk
- `/clear`, `/new` - Start a new conversation. Optionally pass a prompt to seed it
- `/log` - Show path to current interaction log file
- `/log-level` - Show or set the log level. `/log-level` prints the full chain
  (session, env, config, effective); `/log-level set <LEVEL>` sets a
  process-lifetime override; `/log-level set-global <LEVEL>` also persists to
  config.toml; `/log-level unset` clears the session override. LEVEL is one of
  DEBUG, INFO, WARNING, ERROR, CRITICAL.
- `/debug` - Toggle debug console
- `/compact` - Compact model context by summarizing. The session ID and visible
  conversation stay intact.
- `/retry [additional instructions]` - Continue a model response interrupted by
  a backend error without repeating text already shown. Optional instructions
  are passed to the model for the continuation. Relevant error messages also
  hint at this command.
- `/status` - Display agent statistics
- `/whoami` - Display the Mistral signed-in user, workspace, and plan
- `/voice` - Configure voice settings
- `/mcp` - Display MCP servers and connector status; pass a server or connector
  name to list its tools or open its auth panel when authentication is required
- `/mcp add <url>` - Add a hosted OAuth MCP server. Supports `--name <alias>`,
  repeatable `--scope <scope>`, `--transport <http|streamable-http>`, and
  `--no-login`. Starts OAuth login by default. OAuth-only; use
  `vibe mcp add <name> --url <url> --api-key-env <var>` for API-key/static auth.
- `vibe mcp remove <name>` - Remove an MCP server from the user configuration
  and delete its stored OAuth credentials when available.
- `/mcp status` - Display MCP auth state (`ok`, `needs_auth`, `static`, `stdio`)
- `/mcp login <alias>` - Start OAuth login for an MCP server
- `/mcp logout <alias>` - Log out from an MCP server and delete stored OAuth
  secrets
- `/resume` (or `/continue`, `/chats`) - Browse and resume past sessions for the current
  folder. The picker header shows the folder being listed. Press `d` twice to
  delete a saved session; the active session cannot be deleted here.
- `/rewind` - Rewind to a previous message. Also triggered by pressing `Esc`
  twice on an empty input; if the input has content, the first double-`Esc`
  clears it instead. In the rewind panel: `↑/↓` pick option, `Shift+↑/↓`
  scroll, `←`/`Esc` edit previous message, `→` edit next message, `Enter`
  accept, `q` quit.
- `/loop <interval> <prompt>` - Schedule a recurring prompt (e.g. `/loop 30s ping`).
  Intervals: `Ns/Nm/Nh/Nd`, minimum 30s, max 50 loops/session.
  - `/loop` (or `/loop list` / `/loop ls`) - List current scheduled loops.
  - `/loop cancel <id|all>` (aliases `rm`, `stop`, `delete`) - Cancel a loop.
  - Loops fire only when the agent is idle and the input bar is focused. At
    most one loop fires per poll. Overdue loops fire once on the next poll
    (no catch-up); `next_fire_at` advances to `now + interval`.
  - Loops are persisted in the session metadata (`loops` field of `meta.json`)
    and restored on `--resume`/`--continue`.
- `/terminal-setup` - Configure Shift+Enter for newlines
- `/proxy-setup` - Configure proxy and SSL certificate settings
- `/leanstall` - Install the Lean 4 agent (leanstral)
- `/unleanstall` - Uninstall the Lean 4 agent
- `/data-retention` - Show data retention information
- `/teleport` - Teleport session to Vibe Code Web (only available when Vibe Code is enabled)
- `/logout` - Remove the saved Mistral account credential and exit. The next
  launch starts browser sign-in. It does not open the browser automatically;
  it immediately shows an openable/copyable URL and a locally generated
  terminal QR code.
- `/exit` - Exit the application without signing out

## File Mentions (`@`)

Type `@` in the chat input to autocomplete files and folders from the
project tree. Pressing Tab/Enter inserts the chosen path. Your message text
is sent as-is (the `@path` stays in the prompt); behavior then depends on
the mention kind:

- **Text files** trigger a synthetic `read_file` tool call injected right
  after your message, so the file content arrives as a fresh tool result
  every turn (no caching/dedup). The same limits as the `read_file` tool
  apply (~2000 lines / 50 KB per call; larger files are truncated or
  reported as an error result). Re-mentioning a file always re-reads it.
- **Folders** are not read automatically — the path stays in your message
  text and the agent can `read_file`/`grep` it on demand.
- **Image files** (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`) become image
  attachments — sent alongside the prompt as native multimodal content for
  vision-capable models.

Image attachments:

- Require `supports_images = true` on the active model in `config.toml`.
  By default this is enabled only on `mistral-vibe-cli-latest`. Sending
  images to a non-vision model raises a clear error and the message is
  not added to the conversation.
- Snapshotted into `<session_dir>/attachments/<sha1>.<ext>` so that
  resumed sessions stay reproducible even if the source file is moved.
- Capped at 10 MiB per image and 8 images per message.
- Out-of-project paths work via `@/abs/path/to.png` (the picker only
  suggests project files, but the `@`-parser accepts absolute paths).
  Drag-and-drop from Finder into Terminal, iTerm2, or Ghostty is
  intercepted at paste time: if the pasted content is a single bare
  path to an image file (raw, `\\ `-escaped, or quoted), the input
  automatically prepends `@` (and quotes paths containing spaces).
  Non-image paths are pasted verbatim so non-image use cases are not
  affected.
- **Image copy/paste from the clipboard** (**macOS only** for now):
  writes the image to `<session_dir>/attachments/clipboard-<ts>.png`
  (or the system temp dir when no session is active) and inserts an
  `@<path>` token at the cursor. Two entry points:
  1. `Ctrl+V` keybinding inside the prompt.
  2. `/paste-image` slash command.

  Uses `osascript` with a TIFF→PNG fallback via `sips`. On Linux and
  Windows the binding and the slash command are not registered at all,
  so the feature is invisible to users on those platforms.
- Rendered in the chat bubble as one dim `attached image:` footer line
  per image, linking each attachment to its snapshot. Clicking opens the
  file with the OS default image viewer.

## Input Queue

Messages submitted while the agent or a `!`-bash command is running are
queued instead of cancelling the in-flight work, and drain in FIFO order
once the job finishes. Prompts (plain, `/skill ...`, `@`-mentions),
`!bash` commands, and non-side-channel slash commands can be queued;
`&teleport` is rejected with a toast. **Ctrl+C** pops the last queued
item (LIFO); **Esc** interrupts the running job and pauses the queue;
pressing Enter (empty or not) on a paused queue resumes draining.

Allowlisted slash commands (`side_channel=True`) run immediately via a
side channel while the agent or bash is busy — they open pickers,
display info, or apply visual changes without waiting. Only one
side-channel command runs at a time. Commands that persist config
changes (theme, model, thinking, voice, proxy) enqueue the persist
step on the main queue as a `COMMAND` item with a callable payload;
the queue drains when idle, so config writes never conflict.

Commands not on the side-channel allowlist (e.g. `/goal`, `/clear`, `/compact`,
`/rewind`, `/resume`, `/reload`, `/leanstall`, `/unleanstall`, `/teleport`,
`/remote-project`, `/retry`) are enqueued on the main queue and execute
when the session is idle.

## Skills System

Skills are specialized instruction sets the model can load on demand.
Each skill is a directory containing a `SKILL.md` file with YAML frontmatter.
Powerstral also bundles `software-engineering` for root-cause implementation and
evidence-based completion, plus `web-engineering` for full-stack, API, browser,
security, accessibility, performance, and primary-source verification workflows.
The `coding-deepwiki` router exposes 1,000 lazy virtual coding skills and 10,000
stable articles through the read-only `deep_wiki` search/read tool. Virtual names
are resolved by the normal `skill` tool but omitted from startup discovery output
to preserve latency and context. Use web search for unstable version/API facts.

### Skill File Format

```markdown
---
name: my-skill
description: What this skill does and when to use it.
user-invocable: true
allowed-tools: bash read
---

# Skill Instructions

Detailed instructions for the model...
```

### Skill Search Order (first match wins)

1. `skill_paths` from config.toml
2. `.vibe/skills/` in trusted project directory
3. `.agents/skills/` in trusted project directory
4. `~/.vibe/skills/` (user global)
5. `~/.agents/skills/` (user global, Agent Skills standard)

### Invoking Skills

Two entry points:
- The model loads a skill on demand via the `skill` tool.
- The user invokes a `user-invocable` skill by typing `/skill-name` (optionally
  followed by extra instructions). The user turn stays the literal `/skill-name`
  text; the skill is loaded programmatically and appears to the model as a
  synthetic `skill` tool call and result immediately after that turn — the model
  does not call the tool itself.

Skills with `user-invocable: false` are model-only: they are hidden from the
slash menu and `/skill-name` will not resolve them (it is treated as a plain
prompt). The model can still load them via the `skill` tool.

A `/` at the very start of the input opens the slash menu (commands and skills).
A `/word` typed mid-prompt (not the first word) instead shows an inline ghost-text
preview of the best-matching skill name; press `Tab` to accept it. Only skills are
offered inline, and no popup is shown.

## Environment Variables

- `VIBE_HOME` - Override the Vibe home directory (default: `~/.vibe`)
- `MISTRAL_API_KEY` - API key for Mistral provider
- `VIBE_ACTIVE_MODEL` - Override active model
- `VIBE_*` - Any config field can be overridden with the `VIBE_` prefix
- `LOG_LEVEL` - Overrides `log_level` config for `$VIBE_HOME/logs/vibe.log`.
  One of `DEBUG`, `INFO`, `WARNING` (default), `ERROR`, `CRITICAL`. Invalid values
  fall back to `WARNING`. Use `/log-level` to change at runtime.
- `LOG_MAX_BYTES` - Max size in bytes of `vibe.log` before rotation
  (default: `10485760`, i.e. 10 MiB).
- `DEBUG_MODE` - When `true`, forces `DEBUG`-level logging. Under `vibe-acp`
  it also attaches `debugpy` on `localhost:5678`.
- `VIBE_TYPING_GRACE_PERIOD_MS` - Milliseconds the agent waits for a typing
  pause before showing tool-approval / ask-user-question dialogs (default:
  `1000`). Set to `0` to disable. Negative or non-numeric values fall back
  to the default.

## API Keys (.env file)

The `.env` file in VIBE_HOME stores API keys in dotenv format:

```
MISTRAL_API_KEY=your-key-here
```

This file is loaded on startup and its values are injected into the environment.

## Trusted Folders

Vibe uses a trust system to prevent executing project-local config from untrusted
directories. The trust database is stored in `~/.vibe/trusted_folders.toml`.
Project-local config (`.vibe/` directory) is only loaded when the current
directory is explicitly trusted.

Interactive mode prompts to trust unknown folders. The prompt targets the
closest ancestor of the cwd (the cwd itself included) containing a `.git`
entry; the search excludes the user's home directory and the filesystem
root, and falls back to the cwd if no qualifying ancestor is found. Decline is
the safe default. Worktree creation/selection is rejected before Git runs until
the source workspace is trusted or the caller explicitly supplies ephemeral
trust for that session.
Programmatic mode (`-p`/`--prompt`) never prompts: the folder is untrusted.
Use `--trust` to trust cwd for the current invocation only (not persisted).

## Sensitive Files — DO NOT READ OR EDIT

NEVER read, display, or edit any of these files:
- `~/.vibe/.env` (or `$VIBE_HOME/.env`) — contains API keys and secrets
- Any `.env`, `.env.*` file in the project or VIBE_HOME

If the user asks to set or change an API key, instruct them to edit the `.env`
file themselves. Do not offer to read it, write it, or display its contents.
Do not use tools (read, write_file, bash cat/echo, etc.) to access these files.

## How to Modify Configuration

To help the user modify their Vibe configuration:

1. **Read current config when present**: Read `~/.vibe/config.toml` (or the path
   from `VIBE_HOME` if set). A missing file means Vibe is using built-in defaults.
2. **Create a backup when present**: Before editing an existing file, copy it to
   `config.toml.bak` in the same directory. This applies to any existing config
   file you are about to modify (`config.toml`, `trusted_folders.toml`, agent
   TOML files, etc.)
3. **Edit the TOML file**: Make changes using the edit tool
4. **Reload**: The user can run `/reload` to apply changes without restarting

For API keys, tell the user to edit `~/.vibe/.env` directly — never read or
write that file yourself.

For project-specific configuration, create/edit `.vibe/config.toml` in the
project root (the folder must be trusted first)."""


SKILL = SkillInfo(
    name="vibe",
    description="""Authoritative reference for Powerstral — the autonomous Mistral Vibe fork you (the model) are running inside.

LOAD when the user:
- asks anything about Vibe itself, even by indirect name ("this CLI", "this tool", "you");
- wants to change, inspect, or reset their setup;
- asks why the agent did or did not act;
- asks how to make the CLI do X, where X lives, or what a flag/command/setting does;
- asks any meta question about your own behavior;
- is unsure whether a command, flag, env var, or file is in scope — this skill is the source of truth.

SCOPE: config under `~/.vibe/` and project-local `.vibe/`; `VIBE_*` and `LOG_*` env vars; models and providers; agents and subagents; skills; tools and their permission model; every slash command and CLI flag; hooks; MCP servers; connectors; trusted folders; `@`-file mentions; logs; themes; voice.""",
    user_invocable=False,
    prompt=_PROMPT_TEMPLATE.replace("__VIBE_VERSION__", __version__),
    source=SkillSource.BUILTIN,
)
