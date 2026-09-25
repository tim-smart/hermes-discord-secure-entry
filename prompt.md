Build a Hermes plugin that lets me enter a website one-time verification code from my phone using a Discord button and modal, without posting the code in chat.

Work against the latest hermes repo in `vendor/hermes`, not assumptions from older docs. First inspect the plugin API, Discord adapter, browser_vault_enter_code, and its per-thread prompt callbacks. Prefer a standalone plugin over a custom Hermes build. Hermes supports native Discord handler registration and consent-gated built-in tool overrides; determine whether those are sufficient. If a small core extension is genuinely necessary, explain why before making it.

Required behavior:
- When browser_vault_enter_code needs a user-supplied code in a Discord session, send a button in that session. Tapping it opens a Discord modal with a single code field.
- Deliver the submitted code directly to the waiting vault operation. Never turn it into a chat message, model input, tool-result text, transcript entry, or logged value. Never echo it in an acknowledgement.
- Preserve the existing authenticator-key path: if Hermes can generate the code, do not prompt.
- Bind each prompt to the initiating Discord user, session, and pending tool call. Reject other users, stale or duplicate submissions, and cross-session responses. Handle cancellation, /stop, timeout, disconnect, plugin unload, and concurrent prompts without leaving a blocked worker.
- Keep Discord’s event loop responsive while the tool waits. Acknowledge interactions within Discord’s required window. Give the user an ephemeral success, expiry, or error acknowledgement that contains no code.
- Do not accept codes through ordinary channel messages or DMs as a fallback. Keep existing non-Discord behavior unchanged. Make clear in the UX/docs that the modal hides the code from the channel but Discord still processes the submission.
