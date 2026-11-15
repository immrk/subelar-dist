# Subelar Public Distribution (`subelar-dist`)

<p align="left">
  <strong><a href="./README.md">English</a></strong> | <a href="./README.zh-CN.md">简体中文</a>
</p>

Public binary distribution and hot-update manifest repository for **Subelar** — the local-first, privacy-preserving AI Subscription & Multi-Account Quota Tracker (`subelar` CLI + Native iOS/watchOS Companion App & Widgets).

---

## 🚀 One-Line Installation

- **macOS / Linux**:
  ```bash
  curl -fsSL https://raw.githubusercontent.com/immrk/subelar-dist/main/install.sh | sh
  ```
- **Windows (PowerShell)**:
  ```powershell
  irm https://raw.githubusercontent.com/immrk/subelar-dist/main/install.ps1 | iex
  ```

---

## 📦 Hosted Static Manifests

| Manifest Path | Purpose |
| :--- | :--- |
| `manifests/version-manifest.json` | Latest CLI release version, download URLs, and SHA-256 checksums |
| `manifests/exchange-rates.json` | Daily currency exchange rates for multi-currency subscription tracking |
| `manifests/catalog.json` | Built-in AI & SaaS subscription preset templates |
| `manifests/plugins/registry.json` | Official & community quota collector plugin registry (`codex`, `agy`, `claude`) |
