Control a Chrome instance through its local DevTools Protocol endpoint.

Use `list_tabs` to discover target IDs and `snapshot` to obtain accessibility nodes before `click` or `type`. Pass the returned `node_id` for those actions. Use `open` for a new HTTP(S) tab, `navigate` for an existing tab, and `screenshot` when visual state matters. Verify state after mutations.

`evaluate` executes arbitrary JavaScript in the selected page and can read or change page content, credentials, and signed-in session data. Its expression and returned value are size-limited, but its capabilities are otherwise those of page JavaScript. It uses the normal `chrome_cdp` tool permission.

The CDP endpoint and target WebSocket must use explicit loopback IP addresses. Chrome must already be running with remote debugging enabled; this tool never exposes CDP on the network.
