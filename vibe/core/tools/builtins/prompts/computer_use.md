Control the local Windows desktop through structured observation and bounded input.

Start with `observe`. Use the returned top-level windows, foreground window, child
controls, screen bounds, and cursor position to choose exact targets. Use `focus`
with a returned `hwnd` before typing. After every mutating action, inspect the new
state and verify that the intended window is still foreground.

Actions:

- `observe`: return visible windows and controls without changing the desktop.
- `screenshot`: capture the virtual desktop into one bounded scratchpad PNG. Vibe
  attaches successful captures to the next model call when the active model supports
  images; text-only models still receive the structured window/control state and path.
- `focus`: restore and focus a window by `hwnd` from the latest observation.
- `click`: click absolute virtual-screen coordinates with left, right, or middle.
- `type`: enter Unicode text into the focused control.
- `key`: press one key or a chord such as `["ctrl", "s"]`.
- `scroll`: send vertical wheel steps, optionally after moving to `x`, `y`.

Never infer coordinates outside the returned screen bounds. Never type passwords,
API keys, payment data, or other secrets: tool arguments and session records may
retain text. Do not confirm purchases, publish content, delete data, change account
security, or perform another consequential external action without explicit user
authorization. Prefer keyboard navigation and named controls over uncertain clicks.
Never automate terminals, the Windows Run dialog, authentication prompts, password
managers, antivirus/security tools, or Windows security/privacy settings. Terminal
window input and Windows-key shortcuts are also rejected by the backend; use the
permissioned shell tools for commands.
