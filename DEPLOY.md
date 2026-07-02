# Deploy — Natural Nutrition Voice Agent

Target: **Fly.io**, single machine in `iad` (Ashburn, VA), no scale-to-zero.
Stack: `Dockerfile` + `fly.toml`. Secrets live in Fly secrets, never in the image.

## Prerequisites
- `flyctl` installed (`brew install flyctl` on macOS) and `fly auth login` completed.
- Docker Desktop running locally (only needed for the optional local smoke test — Fly builds remotely by default).
- The three secrets on hand: `OPENAI_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`.

## 1. (Optional) Local smoke test in Docker
Confirms the image builds and the app boots on the same port Fly will use.
```bash
docker build -t nn-voice-agent .
docker run --rm -p 8080:8080 --env-file .env nn-voice-agent
# In another shell:
curl http://localhost:8080/
# Expect: <h1>Natural Nutrition Voice Agent</h1><p>Server is running.</p>
```

## 2. First-time Fly deploy

### 2a. Create the app on Fly
Uses the existing `fly.toml`. If the app name `nn-voice-agent` is taken, flyctl will
suggest an alternative — accept it, then update the `app = "..."` line in `fly.toml`
so future `fly deploy`s target the same app.
```bash
fly apps create nn-voice-agent
```

### 2b. Set secrets
Secrets are encrypted, injected as env vars at boot, and never appear in the image.
```bash
fly secrets set \
  OPENAI_API_KEY=sk-... \
  TWILIO_ACCOUNT_SID=AC... \
  TWILIO_AUTH_TOKEN=...
```
Setting secrets on an app with no machines yet just stores them — no restart happens.
They'll be present the first time the machine boots in step 2c.

### 2c. Deploy
```bash
fly deploy
```
This builds the image remotely, pushes it, and boots one machine in `iad`. Watch the
build logs for the uvicorn startup line.

### 2d. Get the public URL and point Twilio at it
```bash
fly status              # shows the app hostname, e.g. nn-voice-agent.fly.dev
fly logs                # tail runtime logs
curl https://nn-voice-agent.fly.dev/
```
In the Twilio console → Phone Numbers → your `+1 956-906-8451` number:
- **Voice → A call comes in → Webhook**: `https://nn-voice-agent.fly.dev/incoming-call`
- **HTTP method**: POST
- Save.

Cold-call the number. Expected server-log sequence:
```
Twilio WebSocket connected
Connected to OpenAI Realtime API
OpenAI event: session.created
OpenAI event: session.updated
Stream started — stream_sid=...
```

If `session.updated` appears, the GA schema is accepted. If you see `OpenAI error:` instead,
the session-config regressed — check the Realtime GA schema notes in Risk #13 of `PLAN.md`.

## 3. Redeploy after code changes
```bash
fly deploy
```
Rolling replacement; a single in-flight call will drop when the old machine drains.
For the demo window, deploy when the number is not being actively called.

## 4. Mock backend co-located on Fly (done end-of-Day-2)
Deployed as a **second Fly app**, `nn-mock-backend`, in the same org and region (`iad`) as the bridge.
Reachable only over Fly's private 6PN — no public IPs.

- **App name:** `nn-mock-backend`
- **Source dir:** `../rtp-ashley-voice/mock-backend/` (has its own `Dockerfile` + `fly.toml` + `.dockerignore`)
- **Internal address the bridge uses:** `http://nn-mock-backend.internal:8001`
- **Bridge env var:** `MOCK_BACKEND_URL` (set via `fly secrets set MOCK_BACKEND_URL=http://nn-mock-backend.internal:8001 -a nn-voice-agent`)
- **Chaos config:** left at defaults — 300–1500ms + 1200ms on `/subscriptions` + 7% 503s. That's the demo contract, not a bug.

### Redeploy the mock
```bash
cd ../rtp-ashley-voice/mock-backend
fly deploy
```
⚠️ Every redeploy resets the mock's in-memory state back to `seed_data.json`. Do not redeploy the mock in the 30 min before a demo/evaluator call — you'll wipe any account state a caller has been editing.

### Verify bridge → mock 6PN plumbing
```bash
fly ssh console -a nn-voice-agent -C \
  "python -c 'import httpx; print(httpx.get(\"http://nn-mock-backend.internal:8001/health\").text)'"
# expected: {"ok":true}
```
Or watch bridge startup logs after a redeploy: `Mock backend reachable at http://nn-mock-backend.internal:8001/health — 200 {"ok":true}`.

### First-time setup (already done — kept for reference)
```bash
cd ../rtp-ashley-voice/mock-backend
fly apps create nn-mock-backend
fly deploy
fly ips list -a nn-mock-backend                          # note the v4 + v6 addresses
fly ips release <v6> -a nn-mock-backend                  # release both to go private
fly ips release <v4> -a nn-mock-backend
fly secrets set MOCK_BACKEND_URL=http://nn-mock-backend.internal:8001 -a nn-voice-agent
```

### Gotcha — do NOT change the mock's uvicorn bind
The mock's `Dockerfile` runs `uvicorn --host ::`. This is intentional: Fly's `.internal` DNS resolves to IPv6 only, so the mock must listen on IPv6 or the bridge can't reach it. asyncio (uvicorn's async loop) sets `IPV6_V6ONLY=1` on the socket unconditionally, so `--host ::` is v6-only — that's also why the mock's `fly.toml` omits `[[http_service.checks]]` (Fly's HTTP checker uses v4 loopback and would fail). Changing to `--host 0.0.0.0` will "fix" the health check while silently breaking every bridge→mock call.

## Useful ops
```bash
fly logs                          # tail logs
fly status                        # machine + region + hostname
fly ssh console                   # shell into the running machine
fly secrets list                  # names only, values never shown
fly secrets unset OPENAI_API_KEY  # rotate: unset then set again
fly scale count 1                 # ensure exactly one machine (belt-and-suspenders)
```

## Known deploy-time gotchas
- **Port mismatch.** `Dockerfile` `ENV PORT=8080` and `fly.toml` `internal_port = 8080` must agree. If you change one, change both.
- **Websocket idle disconnects.** Fly's edge does not idle-close WS; if the OpenAI socket closes ~2s in, that's the GA-schema issue (Risk #13), not Fly.
- **Cold-start dead air.** `auto_stop_machines = "off"` + `min_machines_running = 1` in `fly.toml` prevent this. Don't change either without a reason.
- **Secrets in the image.** The `.dockerignore` excludes `.env` — verify with `docker run --rm nn-voice-agent env | grep -E 'OPENAI|TWILIO'` on a locally-built image: should be empty.
