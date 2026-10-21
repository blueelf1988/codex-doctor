---
name: codex-doctor
description: Diagnose and fix "Codex desktop client won't connect" issues (endless Reconnecting, Work browser access error, 401 auth errors, unknown model, post-upgrade breakage) caused by corrupted config.toml / .env / CC Switch provider routing. Use when the user reports Codex or ChatGPT desktop app connection failures, especially with CC Switch routing third-party models like MiniMax or OpenCode Go.
---

# Codex Doctor — Skill

## When to use

User reports any of:
- Codex / ChatGPT desktop app stuck on `Reconnecting... waiting for network`
- `Work browser access requires an authenticated account`
- 401 `已切换到 OpenAI 官方供应商，但这个请求仍按第三方供应商的配置发出`
- `unknown model 'xxx'` after switching CC Switch routes
- Everything breaks after upgrading the desktop client
- Using CC Switch to route third-party model providers (MiniMax, OpenCode Go, etc.) with Codex

## What it does

The Codex desktop client reads `~/.codex/config.toml` on every start.
CC Switch rewrites that file on every route switch — and sometimes corrupts it.
This skill diagnoses the corruption and fixes it at three layers:

1. **Source layer (persistent)** — fix the `common_config_codex` setting inside
   `~/.cc-switch/cc-switch.db` so every future rewrite produces a correct file.
   NEVER write to the `providers` table (user-owned assets).
2. **Active layer** — fix `~/.codex/config.toml` + `~/.codex/.env` directly,
   mode-aware (read `~/.cc-switch/live-state.json` → `apps.codex.mode`).
3. **Watch layer** — FSEvents listener (sub-second) + 3s polling fallback,
   managed by launchd with KeepAlive.

## Commands

```bash
python3 scripts/codex_guard.py doctor     # diagnose only, no changes
python3 scripts/codex_guard.py fix        # fix everything + persist to source
python3 scripts/codex_guard.py install    # fix + install auto-guard (macOS)
python3 scripts/codex_guard.py uninstall  # remove guard, leave configs intact
```

## Root causes covered (all reproduced & A/B verified)

| # | Root cause | Symptom |
|---|---|---|
| 1 | `.env` contains dead proxy (e.g. `HTTP_PROXY=127.0.0.1:7890` with nothing listening) | Endless Reconnecting — **the decisive factor** |
| 2 | `conversationDetailMode = "STEPS_PROSE"` (Work mode requires ChatGPT account auth) | Work browser access error |
| 3 | `model_provider = "custom"` residue while in direct mode | 401 auth error on official route |
| 4 | Model name mismatch with current route | `unknown model` |
| 5 | Stale paths pointing to relocated client bundle (`/Applications/Codex.app` → `/Applications/ChatGPT.app`) | Post-upgrade breakage |
| 6 | Responses-over-WebSocket bypassing local route | websocket connect refused / 426 |
| 7 | `model_provider` key placed inside a section instead of top-level | Silent fallback to official endpoint |
| 8 | UTF-8 BOM / unrecognized `type = "stdio"` keys | parse errors / warning spam |

## Hard safety rules

- NEVER modify the `providers` table of cc-switch.db (user-created providers).
- NEVER change the user's selected mode (proxy vs direct). Only keep each
  mode's config internally correct.
- ALWAYS back up before writing (`*.bak-<timestamp>`).
- Verify with `doctor` after every `fix`.
