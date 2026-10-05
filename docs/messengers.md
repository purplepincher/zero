# Messengers

One workflow hears every messenger. Each messenger is a thin bridge that speaks the same protocol. Porting to a new platform is a small, mechanical job — Telegram proved the path; the rest is translation.

## The shape

```
Your phone ──► messenger's servers ──► bridge worker ──► GitHub ──► message.yml
                                                                      │
                                                                      ▼
Your phone ◄── messenger's servers ◄── bridge worker ◄── POST /send ◄──┘
```

- **Inbound**: the bridge receives the platform's webhook, translates it into a GitHub `repository_dispatch` event, and the `message` workflow wakes.
- **Outbound**: the workflow POSTs the reply to the bridge's `/send` endpoint, and the bridge delivers it.

The workflow never knows which messenger it's talking to. That's the whole trick.

## The bridge contract

A bridge is a small Cloudflare Worker (~150 lines; see the reference `telegram-bridge.js`) with two endpoints:

**`POST /webhook`** — receives the platform's events. For each incoming text message it:
1. Extracts `user_id`, `chat_id`, `username`, `text`.
2. Optionally checks an allowlist.
3. Fires a dispatch:
   ```
   POST https://api.github.com/repos/{OWNER}/{REPO}/dispatches
   Authorization: Bearer {github token with repo scope}
   {
     "event_type": "message",
     "client_payload": {
       "messenger": "telegram",   // or "discord", "whatsapp", "slack", "signal"
       "user_id": "...",
       "chat_id": "...",
       "username": "...",
       "text": "...",
       "message_id": "...",
       "timestamp": "..."
     }
   }
   ```

**`POST /send`** — receives `{chat_id, text}` from the workflow and delivers it through the platform's send API.

**Config per bridge** (plain vars): which repo it serves. **Secrets** (never in the repo): the platform token, the GitHub token.

**Repo config** (one variable): `BRIDGE_URL` → the bridge's URL. The workflow reads it; the default points at the reference bridge.

## Porting checklist

What it actually took, end to end, for Telegram — the same steps apply everywhere:

1. Create the bot/app on the platform, get the token.
2. Copy the reference bridge, deploy it as a new worker with the repo's owner/name as plain vars.
3. Place the two secrets on the worker (platform token + GitHub token). Values never travel through chat.
4. Register the webhook URL (`https://{bridge}/webhook`) with the platform.
5. Set the fork's `BRIDGE_URL` variable to the new bridge.
6. Send the first message. If the reply comes back, the port is done.

## Platform notes

- **Telegram** — the reference. BotFather → token → webhook. Easiest port; start here to learn the shape.
- **Discord** — bot token via the developer portal; DMs arrive over the gateway (websocket), so the bridge holds a lightweight gateway connection or uses interaction webhooks for servers. Rich formatting in, rich formatting out.
- **WhatsApp** — Business API via Meta: app setup, phone number, webhook verification handshake. The bridge answers the verification challenge, then it's the same contract.
- **Slack** — Events API: app with `message.im` subscription, signing-secret verification on the webhook. Straightforward.
- **Signal** — linked-device approach (no official bot API); the bridge runs a Signal client. Heaviest lift of the set.

Each port is a bridge translation plus the six steps above. The workflow, the vault, and the agent don't change — they already speak messenger.
