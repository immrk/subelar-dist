# Subelar 公开产物与热更分发仓 (`subelar-dist`)

<p align="left">
  <a href="./README.md">English</a> | <strong><a href="./README.zh-CN.md">简体中文</a></strong>
</p>

**Subelar**（“凭证不出本机，额度随身可见”的全品类订阅与多账号 AI 配额管理套件）的公开单文件二进制分发与静态热更清单仓库。

---

## 🚀 一行命令安装 / 更新

- **macOS / Linux**：
  ```bash
  curl -fsSL https://raw.githubusercontent.com/immrk/subelar-dist/main/install.sh | sh
  ```
- **Windows (PowerShell)**：
  ```powershell
  irm https://raw.githubusercontent.com/immrk/subelar-dist/main/install.ps1 | iex
  ```

---

## 📦 静态热更清单说明

| 清单路径 | 说明 |
| :--- | :--- |
| `manifests/version-manifest.json` | 跨平台 CLI 最新版本号、二进制下载链接与 SHA-256 校验值 |
| `manifests/exchange-rates.json` | 多币种订阅折算汇率热更清单 |
| `manifests/catalog.json` | AI 与主流 SaaS 订阅预设模板目录 |
| `manifests/plugins/registry.json` | 官方与社区配额采集插件注册表（`codex`、`agy`、`claude`） |
