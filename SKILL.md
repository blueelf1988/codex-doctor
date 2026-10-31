---
name: codex-doctor
description: 诊断并修复 Codex 桌面端（ChatGPT.app 内嵌）连接故障——Reconnecting 无限转圈、Work browser access 报错、切 CC Switch 路由后 401/429/503、持续重连 stream disconnected、升级客户端后失灵。当用户提到 Codex 连不上、Codex 重连、CC Switch 切换后 Codex 报错、model_provider 残留、未知模型、websocket_not_supported 等问题时使用。
---

# Codex Doctor

Codex 桌面端连接故障的体检与修复工具。四层机制：

1. **体检（doctor）**——只看不改，输出大白话问题清单
2. **修复（fix）**——当场修好 + 固化进 CC Switch 模板库
3. **守护（install）**——FSEvents 秒级监听 + 3 秒轮询兜底 + launchd 开机自启
4. **提醒**——路由切换时弹系统通知，提醒"旧会话粘住旧供应商，请新建会话"

## 触发条件

用户描述以下任一症状时启用：
- Codex 一直转圈 / "Reconnecting... waiting for network"
- "Work browser access requires an authenticated account"
- 切 CC Switch 路由后 Codex 报 401 / 429 / 503 / unknown model
- "正在重新连接 5/5"、"stream disconnected"、"idle timeout waiting for SSE"
- 升级 Codex/ChatGPT 客户端后功能失灵

## 三层原理

```
L1 持久层  CC Switch 模板库 common_config_codex   ← 治本：以后写出来的菜单天然就对
L2 生效层  config.toml + .env                     ← 治标：当场修好
L3 兜底层  FSEvents 秒级 + 3 秒轮询 + launchd      ← 保险：写坏了自动修
```

## 模式感知（关键）

读 `~/.cc-switch/live-state.json` 的 `apps.codex.mode` 判断当前模式：
- `proxy`（第三方路由）：需要 `model_provider = "custom"` + `[model_providers.custom]` 段 + `supports_websockets = false` + 等待耐心值
- `direct`（官方直连）：必须**清掉**上述第三方写法，否则请求错送本地代理 → 401

绝不在两种模式之间乱改——守护只保证"当前模式下写法正确"。

## 硬安全规则

1. **绝不写 CC Switch 的 `providers` 表**——用户手动建的供应商一个字都不动（只读取用）
2. **绝不改用户选择的模式**——切路由是用户自己的决定
3. **改任何文件前自动备份**（带时间戳）
4. **补建供应商小节时必须照搬** CC Switch 数据库里的原文，不编造 base_url / key
5. **无法从客户端侧修复的故障要如实告知**（如 Codex thread 会话粘性——客户端已固化的会话元数据，脚本改不了）

## 常用命令

```bash
python3 scripts/codex_guard.py doctor     # 体检
python3 scripts/codex_guard.py fix        # 修复
python3 scripts/codex_guard.py install    # 修复 + 装守护
python3 scripts/codex_guard.py uninstall  # 卸载
```

## 诊断捷径

CC Switch 的 `proxy_request_logs` 表能一眼分清责任方：

```sql
SELECT provider_id, status_code, COUNT(*)
FROM proxy_request_logs WHERE app_type='codex'
GROUP BY provider_id, status_code;
```

- 4xx（401/400/404）→ 配置或模型名问题 → 能修
- 429（额度用完 / All available accounts rate-limited）→ **服务商限额** → 换路由或等重置，脚本无解
- 500 + "Token Plan 上限" → 服务商套餐耗尽 → 同上
- 503 "No available channel for model xxx" → 上游分组没有该模型 → 对齐模型名 + ⌘Q 重启客户端
- 502 + "Broken pipe / connection closed / dns error" → **网络层**，不是配置
- 504 + "Connect: operation timed out" → TCP 建连超时 → 链路质量问题
- 200 但客户端仍重连 → 链路慢 → 调大 `stream_idle_timeout_ms`

### 延迟画像（判断"慢"还是"断"）

```sql
SELECT first_token_ms, latency_ms, model, status_code
FROM proxy_request_logs WHERE provider_id=? ORDER BY rowid DESC LIMIT 60;
```

- **首 token 中位 > 10s** → 客户端必然 idle timeout 重连（改超时参数 + 换模型）
- **同机对照**：拿另一个端点跑同一个 `curl -w`（TLS/首字节/总耗时）。若别的端点亚秒级、只有这一条慢 → **是这条线路的问题，不是你的网络出口**

### 错误根因归并（避免被表面状态码误导）

502 里混着三类根因，必须拆开看 error_message 原文：
`dns error` / `failed to lookup` → DNS；`operation timed out` → 建连超时；`Broken pipe` / `connection closed` → 传输中断。

### 上游模型清单（别猜）

判定"模型名对不对"的权威来源是**直接拉上游 `/v1/models`**（带上该 provider 的 key），而不是 CC Switch 里配的列表——两者经常不一致（CC Switch 里手动加的映射可能上游早就没有通道）。

### 必须走代理层判定服务商状态

直连 curl 打上游会拿到**误导性的前置错误**（例：OpenCode Go 直连返回 `400 MissingSessionID`，掩盖了真实的 `402 余额不足`——`x-opencode-session` 头由 CC Switch 注入）。**判定额度/可用性必须看 `proxy_request_logs` 的 error_message 全文。**
