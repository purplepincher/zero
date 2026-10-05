# Plugins — Adding a New Service

A plugin gives the agent a new capability: a new API it can call through the
vault. Plugins are how this system grows. Each one follows the same three-part
pattern, and each one is documented so that an agent who has never seen the
underlying API can use it correctly on the first try — because that is exactly
what will happen.

## The pattern

1. **Secret**: stored in the vault worker as `VAULT_<SERVICE>_TOKEN`
   (Cloudflare dashboard → Workers → vault worker → Settings → Variables and
   Secrets). Never in the repo. Ever.
2. **Handler**: a `proxy<Service>` function in the vault worker, registered in
   the `proxyAction` switch. It takes `(action, params, secret, env)` and
   returns JSON.
3. **Call**: the agent POSTs to the vault `/proxy` endpoint:
   ```json
   {
     "oidc_token": "<github-actions-oidc-jwt, audience=vault>",
     "service": "myservice",
     "action": "do_thing",
     "params": { "key": "value" }
   }
   ```
   The vault verifies the OIDC token, checks the repo/workflow allowlist,
   looks up the secret, runs the handler, and returns
   `{ "success": true, "result": {...} }`. The secret never leaves the worker.

## Worked example: the LLM service

- **Secret**: `VAULT_LLM_TOKEN` — an API key for an LLM provider.
- **Handler**: `proxyLLM` — action `complete`. Supports `anthropic` natively
  and any OpenAI-compatible API via the `openai` provider setting.
- **Config** (plain worker variables, not secrets): `LLM_PROVIDER`
  (`anthropic` or `openai`), `LLM_BASE_URL` (for OpenAI-compatible endpoints),
  `LLM_MODEL` (default model).
- **Call**: `{ "service": "llm", "action": "complete",
  "params": { "messages": [{ "role": "user", "content": "..." }],
  "max_tokens": 500 } }`
- **Returns**: `{ "content": "...", "model": "...", "usage": {...} }`

## Worked example: Telegram

Telegram is a plugin with its own small worker (the bridge):

- **Secret**: `TELEGRAM_BOT_TOKEN` on the bridge worker.
- **In**: Telegram sends updates to the bridge `/webhook`; the bridge fires a
  `repository_dispatch` (`message`) to wake the repo. Needs
  `VAULT_GITHUB_TOKEN` on the bridge worker to dispatch.
- **Out**: the agent POSTs `{ "chat_id": ..., "text": "..." }` to the bridge
  `/send`; the bridge delivers it via the Telegram API.
- The agent never touches the Telegram API directly.

## Checklist for a new plugin

- [ ] Secret stored in the vault worker as `VAULT_<SERVICE>_TOKEN`
- [ ] `proxy<Service>` handler written, registered in `proxyAction`
- [ ] Vault worker redeployed
- [ ] This file (or a new doc under `docs/`) updated: what the service does,
      every action, every param, what comes back, and one worked example

## Design notes

- Keep handlers small and explicit: one service, a few named actions, JSON in
  and out.
- Name actions as verbs (`complete`, `send`, `get_repo`), not endpoints.
- The agent reasons about your plugin from its documentation alone. Write the
  doc for a stranger.
- If a plugin needs configuration that is not secret (base URLs, default
  models), use plain worker variables, not secrets.
