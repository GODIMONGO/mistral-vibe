from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import html
import os
from pathlib import Path
from string import Template
import subprocess
from typing import TYPE_CHECKING

from vibe.core.config import VibeConfigSchema
from vibe.core.config.harness_files import (
    HarnessFilesManager,
    get_harness_files_manager,
)
from vibe.core.memory import MemoryStoreError, render_global_memory
from vibe.core.paths import VIBE_HOME
from vibe.core.prompts import SystemPrompt, UtilityPrompt
from vibe.core.tools.builtins.memory import MemoryConfig
from vibe.core.utils import (
    WindowsShellKind,
    get_platform_display_name,
    is_windows,
    resolve_windows_shell,
)
from vibe.observability.logging import logger
from vibe.utils.paths import is_dangerous_directory

if TYPE_CHECKING:
    from vibe.core.agents import AgentManager
    from vibe.core.config import ProjectContextConfig
    from vibe.core.skills.manager import SkillManager
    from vibe.core.tools.manager import ToolManager

_git_status_cache: dict[Path, str] = {}


class ProjectContextProvider:
    def __init__(
        self, config: ProjectContextConfig, root_path: str | Path = "."
    ) -> None:
        self.root_path = Path(root_path).resolve()
        self.config = config

    def get_git_status(self) -> str:
        if self.root_path in _git_status_cache:
            return _git_status_cache[self.root_path]

        result = self._fetch_git_status()
        _git_status_cache[self.root_path] = result
        return result

    def _run_git(
        self, args: list[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            # -c core.fsmonitor= overrides (and disables) any fsmonitor hook a
            # repo's own .git/config declares, for this invocation only. This
            # runs unconditionally on session start, before any trust prompt,
            # so a malicious repo cloned/opened by the user could otherwise use
            # `[core] fsmonitor = <payload>` to get its command executed by
            # `git status`/`git branch`/`git log` here with the user's full
            # privileges. -c on the command line takes precedence over the
            # repo's own config, so this can't be overridden by the repo being
            # inspected.
            ["git", "-c", "core.fsmonitor=", "--no-optional-locks", *args],
            capture_output=True,
            check=True,
            cwd=self.root_path,
            stdin=subprocess.DEVNULL if is_windows() else None,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    @staticmethod
    def _format_git_status(status_output: str) -> str:
        if not status_output:
            return "(clean)"
        status_lines = status_output.splitlines()
        MAX_GIT_STATUS_SIZE = 50
        if len(status_lines) > MAX_GIT_STATUS_SIZE:
            return f"({len(status_lines)} changes - use 'git status' for details)"
        return f"({len(status_lines)} changes)"

    @staticmethod
    def _parse_git_log(log_output: str) -> list[str]:
        recent_commits: list[str] = []
        for line in log_output.split("\n"):
            if not (line := line.strip()):
                continue
            if " " in line:
                commit_hash, commit_msg = line.split(" ", 1)
                if (
                    "(" in commit_msg
                    and ")" in commit_msg
                    and (paren_index := commit_msg.rfind("(")) > 0
                ):
                    commit_msg = commit_msg[:paren_index].strip()
                recent_commits.append(f"{commit_hash} {commit_msg}")
            else:
                recent_commits.append(line)
        return recent_commits

    def _fetch_git_status(self) -> str:
        try:
            timeout = min(self.config.timeout_seconds, 10.0)
            num_commits = self.config.default_commit_count

            with ThreadPoolExecutor(max_workers=4) as pool:
                branch_future = pool.submit(
                    self._run_git, ["branch", "--show-current"], timeout
                )
                remote_future = pool.submit(self._run_git, ["branch", "-r"], timeout)
                status_future = pool.submit(
                    self._run_git, ["status", "--porcelain"], timeout
                )
                log_future = pool.submit(
                    self._run_git,
                    ["log", "--oneline", f"-{num_commits}", "--decorate"],
                    timeout,
                )

            current_branch = branch_future.result().stdout.strip()

            main_branch = "main"
            try:
                branches_output = remote_future.result().stdout
                if "origin/master" in branches_output:
                    main_branch = "master"
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass

            status = self._format_git_status(status_future.result().stdout.strip())
            recent_commits = self._parse_git_log(log_future.result().stdout.strip())

            git_info_parts = [
                f"Current branch: {current_branch}",
                f"Main branch (you will usually use this for PRs): {main_branch}",
                f"Status: {status}",
            ]

            if recent_commits:
                git_info_parts.append("Recent commits:")
                git_info_parts.extend(recent_commits)

            return "\n".join(git_info_parts)

        except subprocess.TimeoutExpired:
            return "Git operations timed out (large repository)"
        except subprocess.CalledProcessError:
            return "Not a git repository or git not available"
        except Exception as e:
            return f"Error getting git status: {e}"

    def get_full_context(self) -> str:
        git_status = self.get_git_status()

        template = UtilityPrompt.PROJECT_CONTEXT.read()
        return Template(template).safe_substitute(
            abs_path=str(self.root_path), git_status=git_status
        )


def _get_os_system_prompt(
    *, use_git_bash_treatment: bool = False, use_powershell_treatment: bool = False
) -> str:
    platform_name = get_platform_display_name()

    if not is_windows():
        shell = os.environ.get("SHELL", "sh")
        return f"The operating system is {platform_name} with shell `{shell}`"

    if use_git_bash_treatment:
        return (
            f"The operating system is {platform_name} with shell `Git Bash`"
            "\n" + _get_windows_bash_system_prompt()
        )

    if use_powershell_treatment:
        return (
            f"The operating system is {platform_name} with shell `PowerShell`"
            "\n" + _get_windows_powershell_system_prompt()
        )

    shell = resolve_windows_shell()
    if shell.kind is WindowsShellKind.BASH and shell.executable is not None:
        shell_display = f"bash ({shell.executable})"
    else:
        shell_display = shell.executable or "cmd.exe"

    prompt = f"The operating system is {platform_name} with shell `{shell_display}`"
    prompt += "\n" + _get_windows_system_prompt(shell.kind)
    return prompt


def _format_current_date() -> str:
    today = date.today()
    return f"{today.isoformat()} ({today.strftime('%A')})"


def _get_windows_bash_system_prompt() -> str:
    return (
        "### COMMAND COMPATIBILITY RULES (MUST FOLLOW):\n"
        "- Commands run through bash (Git Bash), so Unix commands like `ls`, "
        "`grep`, `cat`, `find` work - this is NOT cmd.exe or PowerShell\n"
        "- Discard output with `2>/dev/null` - NEVER `2>nul` or `2>$null`\n"
        "- `&&` and `||` are valid for command chaining\n"
        "- Prefer forward slashes in paths; bash resolves Windows drives as "
        "`/c/Users/...`\n"
        "- Check command availability with: `command -v <command>`\n"
        "### ALWAYS verify commands work on the detected platform before suggesting them"
    )


def _get_windows_cmd_system_prompt() -> str:
    return (
        "### COMMAND COMPATIBILITY RULES (MUST FOLLOW):\n"
        "- The shell is cmd.exe, NOT bash or PowerShell\n"
        "- DO NOT use Unix commands like `ls`, `grep`, `cat` - they won't work; "
        "use `dir`, `findstr`, `type`\n"
        "- Use backslashes (\\\\) for paths\n"
        "- Discard output with `2>nul` - NEVER `2>/dev/null` or `2>$null`\n"
        "- `&&` and `||` are valid for command chaining in cmd.exe\n"
        "- Check command availability with: `where command`\n"
        "- Script shebang: Not applicable on Windows\n"
        "### ALWAYS verify commands work on the detected platform before suggesting them"
    )


def _get_windows_powershell_system_prompt() -> str:
    return (
        "### COMMAND COMPATIBILITY RULES (MUST FOLLOW):\n"
        "- The shell is PowerShell, NOT bash or cmd.exe\n"
        "- Use PowerShell syntax for variables, quoting, pipes, redirects, and conditionals\n"
        "- Use backslashes (\\\\) for Windows paths unless a command explicitly accepts another form\n"
        "- Discard output with `*> $null` or `2>$null` as appropriate - NEVER `2>/dev/null` or `2>nul`\n"
        "- Check command availability with: `Get-Command <command>`\n"
        "- Prefer `Get-ChildItem`, `Get-Content`, and `Select-String` over Unix-only shell commands when a dedicated Vibe tool is not available\n"
        "### ALWAYS verify commands work on the detected platform before suggesting them"
    )


def _get_windows_system_prompt(shell_kind: WindowsShellKind) -> str:
    if shell_kind is WindowsShellKind.BASH:
        return _get_windows_bash_system_prompt()
    return _get_windows_cmd_system_prompt()


def _add_commit_signature() -> str:
    return (
        "When you want to commit changes, you will always use the 'git commit' bash command.\n"
        "It will always be suffixed with a line telling it was generated by Mistral Vibe with the appropriate co-authoring information.\n"
        "The format you will always uses is the following heredoc.\n\n"
        "```bash\n"
        "git commit -m <Commit message here>\n\n"
        "Generated by Mistral Vibe.\n"
        "Co-Authored-By: Mistral Vibe <vibe@mistral.ai>\n"
        "```"
    )


def _get_available_skills_section(skill_manager: SkillManager) -> str:
    skills = skill_manager.available_skills
    if not skills:
        return ""

    lines = [
        "# Available Skills",
        "",
        "You have access to the following skills. When a task matches a skill's description,",
        "use the `skill` tool if available to load the full skill instructions, if it is not available, read the files manually if they exist.",
        "",
        "When a user message is exactly `/skill-name` (optionally followed by extra",
        "instructions), the user has explicitly invoked that skill. Its instructions are",
        "loaded for you automatically: you will see a `skill` tool call and result",
        "immediately after that message. Treat the loaded content as the active",
        "instructions and act on it — you do not need to call the `skill` tool yourself.",
        "",
        "<available_skills>",
    ]

    for name, info in sorted(skills.items()):
        lines.append("  <skill>")
        lines.append(f"    <name>{html.escape(str(name))}</name>")
        lines.append(
            f"    <description>{html.escape(str(info.description))}</description>"
        )
        if info.skill_path is not None:
            lines.append(f"    <path>{html.escape(str(info.skill_path))}</path>")
        lines.append("  </skill>")

    lines.append("</available_skills>")

    return "\n".join(lines)


def _get_available_subagents_section(agent_manager: AgentManager) -> str:
    agents = agent_manager.get_subagents()
    if not agents:
        return ""

    lines = ["# Available Subagents", ""]
    lines.append("The following subagents can be spawned via the Task tool:")
    for agent in agents:
        lines.append(f"- **{agent.name}**: {agent.description}")

    return "\n".join(lines)


def _get_scratchpad_section(scratchpad_dir: Path | None) -> str | None:
    if not scratchpad_dir:
        return None
    return (
        "# Scratchpad Directory\n\n"
        f"You have a scratchpad directory at: `{scratchpad_dir}`\n\n"
        "Use this for temporary files: intermediate results, draft scripts, "
        "working files, outputs that don't belong in the project.\n"
        "Files here are automatically allowed — no permission prompts.\n"
        "Session-scoped. Shared with subagents."
    )


def _interpolate_prompt(prompt: str) -> str:
    return Template(prompt).safe_substitute(current_date=_format_current_date())


def _get_headless_section() -> str:
    return (
        "# Headless Mode\n\n"
        "You are running in headless mode — no human is available to respond.\n"
        "Do not ask questions, request confirmation, or wait for user input.\n"
        "If the task is ambiguous, make the best judgment call and proceed.\n"
        "Complete the entire task in a single pass. Produce a final, complete result.\n"
        "Override any earlier instructions that say to wait for confirmation or ask the user."
    )


def _get_global_memory_section(harness_files: HarnessFilesManager) -> str:
    if "user" not in harness_files.sources:
        return ""
    try:
        return render_global_memory()
    except MemoryStoreError as exc:
        logger.warning("Could not load global memory: %s", exc)
        return ""


def _get_tool_aware_os_system_prompt(tool_manager: ToolManager | None) -> str:
    if tool_manager is None:
        return _get_os_system_prompt()

    available_tools = tool_manager.available_tools
    use_git_bash_treatment = "git_bash" in available_tools
    return _get_os_system_prompt(
        use_git_bash_treatment=use_git_bash_treatment,
        use_powershell_treatment=(
            "powershell" in available_tools and not use_git_bash_treatment
        ),
    )


def _get_web_search_policy(config: VibeConfigSchema) -> str:
    match config.autonomy.web_search_activity:
        case "off":
            instruction = "Web search is disabled. Do not claim that you searched."
        case "low":
            instruction = (
                "Use web_search only when the user explicitly asks for online search."
            )
        case "medium":
            instruction = (
                "Use web_search for time-sensitive facts, current documentation, and "
                "claims that cannot be verified locally."
            )
        case "high":
            instruction = (
                "Proactively use web_search whenever external or current information "
                "could materially improve the answer."
            )
        case "max":
            instruction = (
                "Aggressively use web_search to verify relevant factual claims, compare "
                "multiple sources when useful, and cite the evidence in the answer."
            )
    privacy = (
        " Inspect user-provided local files, repositories, logs, infrastructure, "
        "and referenced local/Codex sessions before considering public search. Never "
        "send raw local paths, authenticated URLs, host/IP targets, thread IDs, "
        "credentials, or private logs to a public search engine. If external "
        "documentation is later needed, search only a generic redacted technical "
        "question."
    )
    return "## Web search activity\n\n" + instruction + privacy


def _get_available_web_search_policy(
    config: VibeConfigSchema, tool_manager: ToolManager | None
) -> str:
    if tool_manager is not None and "web_search" not in tool_manager.available_tools:
        return ""
    return _get_web_search_policy(config)


def _get_local_reference_policy() -> str:
    return (
        "## Local references\n\n"
        "Treat user-provided filesystem paths, infrastructure endpoints, logs, and "
        "session references as primary local evidence. A `codex://threads/<id>` "
        "reference names a Codex thread, not a web URL and not a Vibe session ID. "
        "On a local machine, locate its matching read-only JSONL record under "
        "`~/.codex/sessions` and extract only the evidence needed for the task; do "
        "not invoke Vibe `/resume` for it. Never copy credentials or private thread "
        "contents into public web search or durable memory. Durable memory may keep "
        "only non-secret connection metadata such as host aliases and key paths."
    )


def _get_gauntlet_loop_policy(config: VibeConfigSchema) -> str:
    if not config.autonomy.gauntlet_loop:
        return ""
    return (
        "## Gauntlet Loop\n\n"
        "Gauntlet Loop is enabled. For every substantial goal, establish a real, "
        "named, fetchable, and directly comparable quality bar before implementation. "
        "Use web_search or other available tools to obtain the actual reference; never "
        "invent or compare against a description. Split the work into independently "
        "judgeable pieces. Assign implementation to builders and evaluation to a "
        "separate harsh critic with fresh context. The critic must make a binary blind "
        "choice between the actual output and the bar, then identify the largest "
        "remaining gap. Continue improving and rechecking until our output wins, the "
        "user stops the run, or a runtime safety/resource limit blocks further work. "
        "Do not let a builder approve its own work and do not substitute a numeric "
        "self-score for the comparison. Pattern adapted from robonuggets/gauntlet-loop "
        "(CC BY 4.0)."
    )


def _get_boost_policy(config: VibeConfigSchema) -> str:
    if not config.autonomy.boost_mode:
        return ""
    return (
        "## BOOST intelligence profile\n\n"
        "BOOST is an enforced quality profile, not a claim that the underlying model "
        "became another vendor's model. For each substantive request, analyze intent "
        "before orchestration, materialize a compact plan, research uncertain or "
        "current claims with available repository/web tools, delegate independent "
        "work, and require a fresh evidence-based reviewer verdict. Treat memory and "
        "prior summaries as leads rather than proof. Never report completion from "
        "confidence alone: match every material claim to a test, file, tool result, "
        "or observable state. Trivial conversation remains direct to avoid waste."
    )


def _get_parallel_execution_policy(
    config: VibeConfigSchema, tool_manager: ToolManager | None
) -> str:
    if not config.autonomy.enabled or tool_manager is None:
        return ""
    available = tool_manager.available_tools
    if not available:
        return ""
    return (
        "## Fast batched execution\n\n"
        "Minimize model round trips. Before each tool phase, identify every operation "
        "whose inputs are already known. Emit independent read_file, grep, web_search, "
        "and read-only diagnostic calls together in the same assistant response; the "
        "runtime executes multiple tool calls concurrently. Do not request one file or "
        "one query per model turn when the full batch is known. For dependent, cheap "
        "shell diagnostics in one working directory, prefer one bounded multiline "
        "shell script or command chain with a timeout instead of several shell turns. "
        "Use the shell family exposed by the active tool and keep one shell language "
        "end-to-end. Run independent test suites as concurrent tool calls when they do "
        "not contend for the same files, ports, database, or cache. Never parallelize "
        "mutations that touch overlapping state, and never hide destructive operations "
        "inside a batch. After a batch, consume all results before choosing the next "
        "wave. Aim for 2-4 useful calls per read/verification wave, not artificial "
        "micro-calls or unbounded fan-out."
    )


def _get_automatic_memory_policy(tool_manager: ToolManager | None) -> str:
    if tool_manager is None or "memory" not in tool_manager.available_tools:
        return ""
    memory_config = tool_manager.get_tool_config("memory")
    if not isinstance(memory_config, MemoryConfig) or not memory_config.auto_remember:
        return ""
    return (
        "## Automatic Memory\n\n"
        "Before finalizing each root response, decide whether the user clearly "
        "expressed a durable preference, corrected a recurring behavior, or "
        "confirmed a long-lived convention that will be useful in future sessions. "
        "If so, proactively call the memory tool with action=remember without "
        "waiting for a /memory command, then briefly report what was saved. Keep the "
        "note short and factual. Do not save inferred personal details, secrets, "
        "credentials, temporary task state, guesses, or facts learned only from "
        "tools or web pages. Do not save anything when no clearly durable item was "
        "provided."
    )


def get_universal_system_prompt(
    config: VibeConfigSchema,
    skill_manager: SkillManager,
    agent_manager: AgentManager,
    *,
    scratchpad_dir: Path | None = None,
    headless: bool = False,
    cwd: Path | None = None,
    harness_files: HarnessFilesManager | None = None,
    tool_manager: ToolManager | None = None,
) -> str:
    cwd = (cwd or Path.cwd()).resolve()
    harness_files = harness_files or get_harness_files_manager()
    sections = [_interpolate_prompt(config.system_prompt)]

    if config.autonomy.enabled and config.system_prompt_id != SystemPrompt.AUTONOMOUS:
        sections.append(
            "## Autonomous operating protocol\n\n" + SystemPrompt.AUTONOMOUS.read()
        )

    if headless:
        sections.append(_get_headless_section())

    if config.include_commit_signature:
        sections.append(_add_commit_signature())

    if config.include_model_info:
        sections.append(f"Your model name is: `{config.get_active_model().alias}`")

    sections.extend(
        filter(
            None,
            [
                _get_local_reference_policy(),
                _get_available_web_search_policy(config, tool_manager),
            ],
        )
    )
    sections.extend(filter(None, [_get_gauntlet_loop_policy(config)]))
    sections.extend(filter(None, [_get_boost_policy(config)]))
    sections.extend(
        filter(None, [_get_parallel_execution_policy(config, tool_manager)])
    )
    sections.extend(filter(None, [_get_automatic_memory_policy(tool_manager)]))

    if config.include_prompt_detail:
        sections.append(_get_tool_aware_os_system_prompt(tool_manager))

        skills_section = _get_available_skills_section(skill_manager)
        if skills_section:
            sections.append(skills_section)

        subagents_section = _get_available_subagents_section(agent_manager)
        if subagents_section:
            sections.append(subagents_section)

        sections.extend(filter(None, [_get_scratchpad_section(scratchpad_dir)]))

    sections.extend(filter(None, [_get_global_memory_section(harness_files)]))

    if config.include_project_context:
        is_dangerous, reason = is_dangerous_directory(cwd)
        if is_dangerous:
            template = UtilityPrompt.DANGEROUS_DIRECTORY.read()
            context = Template(template).safe_substitute(
                reason=reason.lower(), abs_path=cwd.resolve()
            )
        else:
            context = ProjectContextProvider(
                config=config.project_context, root_path=cwd
            ).get_full_context()

        sections.append(context)

        cwd_resolved = cwd.resolve()
        extra_roots = [
            root
            for root in harness_files.project_roots
            if root.resolve() != cwd_resolved
        ]
        if extra_roots:
            dirs_lines = "\n".join(f" - {d}" for d in extra_roots)
            sections.append(
                "Additional working directories (treated with the same "
                "file-access permissions as the primary working directory):\n"
                + dirs_lines
            )

        user_doc = harness_files.load_user_doc()
        project_docs = harness_files.load_project_docs()

        doc_sections: list[str] = []
        if user_doc.strip():
            doc_sections.append(
                f"## User instructions\n\nContents of {VIBE_HOME.path}/AGENTS.md (user-level instructions):\n\n{user_doc.strip()}"
            )
        if project_docs:
            doc_sections.append("## Project instructions (checked into the codebase)")
        for doc_dir, doc_content in project_docs:
            doc_sections.append(
                f"Contents of {doc_dir}/AGENTS.md:\n\n{doc_content.strip()}"
            )
        if doc_sections:
            template = UtilityPrompt.AGENTS_DOC.read()
            sections.append(
                Template(template).safe_substitute(sections="\n\n".join(doc_sections))
            )

    return "\n\n".join(sections)
