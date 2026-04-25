# 1Password Git SSH 修復備註（2026-04-19）

## 結論
1Password SSH Agent 已可正常用於 GitHub SSH。

## 根因
`%LOCALAPPDATA%\1Password\config\ssh\agent.toml` 混入 OpenSSH 的 `Host ...` 區塊，造成 1Password SSH Agent TOML 解析錯誤。

## 修正
1. 在 `agent.toml` 移除錯誤 `Host ...` 區塊（僅保留 TOML）。
2. 在 `agent.toml` 新增 GitHub key 白名單：
   - `[[ssh-keys]]`
   - `item = "githubcli"`
   - `vault = "Personal"`
3. 在 `~/.ssh/config` 的 GitHub host 使用：
   - `IdentityAgent //./pipe/openssh-ssh-agent`
4. 移除 `IdentitiesOnly yes`，確保 agent key 會被嘗試。

## 驗證
- `ssh -T git@github.com` 成功顯示 `Hi Edgars-tool!`。
- `git ls-remote git@github.com:Edgars-tool/haodai-linebot.git HEAD` 成功。

## 快速自檢
1. `Test-Path \\.\pipe\openssh-ssh-agent`
2. `ssh -T git@github.com`
3. 若失敗，先檢查 `agent.toml` 是否仍為合法 TOML。
