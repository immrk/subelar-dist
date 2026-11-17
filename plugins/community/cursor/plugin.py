"""Subelar Community Collector Plugin: Cursor (`cursor`).

Modeled after `@magpie-community/opencode-cursor-auth`:
- Discovers Cursor sign-in tokens from:
  1. macOS Keychain (`cursor-access-token` / `cursor-user`, used by `cursor-agent login`)
  2. `~/.cursor/auth.json` (`accessToken`) + `~/.cursor/cli-config.json` (`authInfo.activeTeamId`)
  3. Cursor Desktop SQLite database (`state.vscdb` -> `cursorAuth/accessToken`)
  4. `CURSOR_API_KEY` environment variable (via `/auth/exchange_user_api_key`)
  5. Multi-account profiles stored in Subelar `AccountVault`
- Queries Cursor ConnectRPC & Dashboard APIs in parallel:
  - `aiserver.v1.DashboardService/GetMe`
  - `aiserver.v1.DashboardService/GetPlanInfo`
  - `aiserver.v1.DashboardService/GetCurrentPeriodUsage`
  - `aiserver.v1.DashboardService/GetHardLimit`
  - `aiserver.v1.DashboardService/GetUsageLimitStatusAndActiveGrants`
  - `cursor.com/api/dashboard/get-sand-usage-status` (Grok Bot allowance)
  - `cursor.com/api/usage-summary` (Enterprise allowance fallback)
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import os
import platform
import re
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from subelar_agent.models import AccountSnapshot, ProviderSnapshot, QuotaBadge, QuotaWindow, utc_now_iso

API_BASE = "https://api2.cursor.sh"
WEBSITE_BASE = "https://cursor.com"
DEFAULT_CLIENT_VERSION = "2026.02.13-41ac335"

INCLUDED_PERIOD_MAP = {
    "INCLUDED_USAGE_PERIOD_WEEKLY": "weekly",
    "INCLUDED_USAGE_PERIOD_MONTHLY": "monthly",
    1: "monthly",
    2: "weekly",
    "1": "monthly",
    "2": "weekly",
}


def decode_jwt_claims(token: str) -> Dict[str, Any]:
    """Decode unverified JWT payload claims."""
    if not token or token.count(".") != 2:
        return {}
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def jwt_expiry_seconds(token: str) -> float:
    claims = decode_jwt_claims(token)
    exp = claims.get("exp")
    try:
        return float(exp) if exp is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def jwt_subject(token: str) -> str:
    claims = decode_jwt_claims(token)
    sub = claims.get("sub")
    return str(sub) if isinstance(sub, str) else ""


def is_token_expired(token: str) -> bool:
    exp = jwt_expiry_seconds(token)
    return bool(exp > 0 and time.time() >= exp)


def format_dollars(amount: float) -> str:
    return f"${amount:,.2f}"


def ms_to_utc_iso(ms_val: Any) -> Optional[str]:
    try:
        ms = float(ms_val)
        if ms <= 0:
            return None
        if ms > 1e11:
            sec = ms / 1000.0
        else:
            sec = ms
        return (
            dt.datetime.fromtimestamp(sec, tz=dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OSError):
        return None


def normalize_iso(raw: Any) -> Optional[str]:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def read_keychain_cursor_token() -> str:
    """Read `cursor-access-token` from macOS Keychain (`cursor-agent` storage)."""
    if platform.system() != "Darwin":
        return ""
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", "cursor-access-token", "-a", "cursor-user", "-w"],
            stderr=subprocess.DEVNULL,
            timeout=2.5,
            text=True,
        ).strip()
        return out
    except Exception:
        return ""


def read_cursor_auth_file(cursor_home: Optional[Path] = None) -> Dict[str, Any]:
    """Read `~/.cursor/auth.json` or profile `auth.json`."""
    base = cursor_home or Path(os.environ.get("CURSOR_CONFIG_DIR") or (Path.home() / ".cursor"))
    auth_file = base / "auth.json"
    if auth_file.exists():
        try:
            data = json.loads(auth_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def read_cursor_ide_vscdb_token() -> str:
    """Fallback: read `cursorAuth/accessToken` from Cursor IDE's SQLite `state.vscdb`."""
    candidates: List[Path] = []
    sys_name = platform.system()
    if sys_name == "Darwin":
        candidates.append(
            Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
        )
    elif sys_name == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb")
    else:
        candidates.append(Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb")

    for db_path in candidates:
        if not db_path.exists():
            continue
        try:
            uri = f"file:{urllib.parse.quote(str(db_path))}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=1.5)
            try:
                cur = conn.cursor()
                cur.execute("SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken' LIMIT 1")
                row = cur.fetchone()
                if row and isinstance(row[0], str) and row[0].strip():
                    return row[0].strip()
            finally:
                conn.close()
        except Exception:
            continue
    return ""


def read_active_team_id(token: str, cursor_home: Optional[Path] = None) -> Optional[int]:
    """Read `authInfo.activeTeamId` from `~/.cursor/cli-config.json` matching token subject."""
    base = cursor_home or Path(os.environ.get("CURSOR_CONFIG_DIR") or (Path.home() / ".cursor"))
    cfg_file = base / "cli-config.json"
    if not cfg_file.exists():
        return None
    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        info = data.get("authInfo") if isinstance(data, dict) else None
        if not isinstance(info, dict):
            return None
        auth_id = str(info.get("authId") or "")
        if not auth_id or auth_id != jwt_subject(token):
            return None
        team_id = info.get("activeTeamId")
        if isinstance(team_id, int) and team_id > 0:
            return team_id
    except Exception:
        pass
    return None


def discover_local_cursor_token(cursor_home: Optional[Path] = None) -> str:
    """Discover local Cursor access token following Magpie's priority order."""
    if cursor_home is not None:
        auth_data = read_cursor_auth_file(cursor_home)
        tok = str(auth_data.get("accessToken") or auth_data.get("access_token") or "").strip()
        if tok:
            return tok

    # 1. macOS Keychain (`cursor-access-token`)
    tok = read_keychain_cursor_token()
    if tok:
        return tok

    # 2. `~/.cursor/auth.json`
    auth_data = read_cursor_auth_file()
    tok = str(auth_data.get("accessToken") or auth_data.get("access_token") or "").strip()
    if tok:
        return tok

    # 3. Cursor Desktop IDE `state.vscdb`
    tok = read_cursor_ide_vscdb_token()
    if tok:
        return tok

    return ""


def build_connect_headers(token: str, team_id: Optional[int] = None) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connect-Protocol-Version": "1",
        "x-cursor-client-version": DEFAULT_CLIENT_VERSION,
        "x-cursor-client-type": "cli",
        "x-ghost-mode": "true",
        "User-Agent": "cursor-agent",
    }
    if team_id and team_id > 0:
        headers["x-cursor-team-id"] = str(team_id)
    return headers


def post_connect_json(path: str, token: str, team_id: Optional[int] = None, timeout: float = 6.0) -> Optional[Dict[str, Any]]:
    url = f"{API_BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers=build_connect_headers(token, team_id=team_id),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                raw = resp.read().decode("utf-8")
                parsed = json.loads(raw or "{}")
                return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None
    return None


def fetch_grok_bot_window(token: str, timeout: float = 4.0) -> Optional[QuotaWindow]:
    """Query `POST https://cursor.com/api/dashboard/get-sand-usage-status` for Grok Bot allowance."""
    sub = jwt_subject(token)
    if not sub:
        return None
    parts = [p for p in sub.split("|") if p]
    user_id = parts[-1] if parts else ""
    if not user_id or not re.match(r"^[a-zA-Z0-9._-]+$", user_id):
        return None

    cookie_val = urllib.parse.quote(f"{user_id}::{token}", safe="")
    req = urllib.request.Request(
        f"{WEBSITE_BASE}/api/dashboard/get-sand-usage-status",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": WEBSITE_BASE,
            "Cookie": f"WorkosCursorSessionToken={cookie_val}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                return None

            inc_zero = data.get("includedLimitZero")
            paid = (not inc_zero) if isinstance(inc_zero, bool) else (data.get("hasNonZeroIncludedLimit") is True)
            trial_iso = normalize_iso(data.get("sandTrialExpiresAt"))
            trial_active = False
            if not paid and trial_iso:
                try:
                    exp_dt = dt.datetime.fromisoformat(trial_iso.replace("Z", "+00:00"))
                    trial_active = exp_dt > dt.datetime.now(dt.timezone.utc)
                except Exception:
                    trial_active = False

            usage_pct = data.get("usagePercent")
            if (not paid and not trial_active) or not isinstance(usage_pct, (int, float)):
                return None

            used = round(max(0.0, min(100.0, float(usage_pct))), 1)
            rem = round(max(0.0, 100.0 - used), 1)
            reset_iso = normalize_iso(data.get("nextResetTimestampUtc")) if paid else trial_iso
            label = "Grok Bot (试用)" if trial_active else "Grok Bot 周限额"
            return QuotaWindow(
                id="cursor_grok_bot_7d",
                group="Grok Bot",
                label=label,
                used_percent=used,
                remaining_percent=rem,
                window_minutes=10080,
                resets_at=reset_iso,
            )
    except Exception:
        return None


def compute_on_demand_info(period_data: Dict[str, Any], hard_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Port of Magpie's `onDemandOf(data, hard)`."""
    spend = period_data.get("spendLimitUsage")
    if not isinstance(spend, dict):
        spend = {}
    used_dollars = float(spend.get("individualUsed") or 0.0) / 100.0

    ind_limit = spend.get("individualLimit")
    own: Optional[Dict[str, Any]] = None
    if ind_limit is not None:
        try:
            lim_val = float(ind_limit)
            if lim_val > 0:
                own = {"kind": "fixed", "usedDollars": used_dollars, "limitDollars": lim_val / 100.0}
            else:
                own = {"kind": "disabled", "usedDollars": used_dollars}
        except (TypeError, ValueError):
            pass

    if spend.get("limitType") == "team":
        if own is not None:
            return own
        if hard_data and (hard_data.get("noUsageBasedAllowed") or float(hard_data.get("hardLimit") or 0) <= 0):
            return {"kind": "disabled", "usedDollars": used_dollars}
        pool = float(spend.get("pooledLimit") or 0.0)
        if pool > 0:
            pooled_used = float(spend.get("pooledUsed") or 0.0) / 100.0
            return {
                "kind": "fixed",
                "usedDollars": pooled_used,
                "limitDollars": pool / 100.0,
                "mineDollars": used_dollars,
            }
        return {"kind": "unlimited" if hard_data else "unavailable", "usedDollars": used_dollars}

    if not hard_data:
        return own or {"kind": "unavailable", "usedDollars": used_dollars}
    if hard_data.get("noUsageBasedAllowed"):
        return {"kind": "disabled", "usedDollars": used_dollars}
    limit = float(hard_data.get("hardLimit") or 0.0)
    if limit >= 2147483647:
        return {"kind": "unlimited", "usedDollars": used_dollars}
    if limit > 0:
        return {"kind": "fixed", "usedDollars": used_dollars, "limitDollars": limit}
    return {"kind": "disabled", "usedDollars": used_dollars}


def parse_cursor_dashboard_payloads(
    me_data: Optional[Dict[str, Any]],
    plan_data: Optional[Dict[str, Any]],
    period_data: Dict[str, Any],
    hard_data: Optional[Dict[str, Any]] = None,
    grants_data: Optional[Dict[str, Any]] = None,
    grok_window: Optional[QuotaWindow] = None,
    account_id_fallback: str = "cursor_default",
    email_fallback: str = "",
    alias: str = "",
    is_active: bool = True,
    updated_at: Optional[str] = None,
) -> AccountSnapshot:
    """Pure parser converting Cursor ConnectRPC replies into a Subelar `AccountSnapshot`."""
    now_iso = updated_at or utc_now_iso()
    email = str((me_data or {}).get("email") or email_fallback or account_id_fallback).strip()
    account_id = email or account_id_fallback

    plan_info = (plan_data or {}).get("planInfo")
    if not isinstance(plan_info, dict):
        plan_info = {}
    plan_name = str(plan_info.get("planName") or "Pro").strip() or "Pro"
    is_merged = bool((plan_data or {}).get("isMerged") is True)
    raw_period = plan_info.get("includedUsagePeriod")
    period_kind = INCLUDED_PERIOD_MAP.get(raw_period, "")

    resets_iso = ms_to_utc_iso(period_data.get("billingCycleEnd"))
    plan_usage = period_data.get("planUsage") if isinstance(period_data.get("planUsage"), dict) else {}

    windows: List[QuotaWindow] = []
    badges: List[QuotaBadge] = []

    if plan_usage:
        if is_merged:
            total_used = round(max(0.0, min(100.0, float(plan_usage.get("totalPercentUsed") or 0.0))), 1)
            total_rem = round(max(0.0, 100.0 - total_used), 1)
            is_weekly = period_kind == "weekly"
            win_id = "cursor_weekly_10080" if is_weekly else "cursor_included_period"
            win_label = "本周限额 (Weekly)" if is_weekly else ("本月限额 (Monthly)" if period_kind == "monthly" else "套餐内限额 (Included)")
            win_mins = 10080 if is_weekly else 43200
            windows.append(
                QuotaWindow(
                    id=win_id,
                    group="Cursor",
                    label=win_label,
                    used_percent=total_used,
                    remaining_percent=total_rem,
                    window_minutes=win_mins,
                    resets_at=resets_iso,
                )
            )
        else:
            auto_used = round(max(0.0, min(100.0, float(plan_usage.get("autoPercentUsed") or 0.0))), 1)
            auto_rem = round(max(0.0, 100.0 - auto_used), 1)
            api_used = round(max(0.0, min(100.0, float(plan_usage.get("apiPercentUsed") or 0.0))), 1)
            api_rem = round(max(0.0, 100.0 - api_used), 1)
            windows.append(
                QuotaWindow(
                    id="cursor_models_pool",
                    group="Cursor Models",
                    label="Cursor Models 限额",
                    used_percent=auto_used,
                    remaining_percent=auto_rem,
                    window_minutes=43200,
                    resets_at=resets_iso,
                )
            )
            windows.append(
                QuotaWindow(
                    id="cursor_other_models_pool",
                    group="Other Models",
                    label="Other Models 限额",
                    used_percent=api_used,
                    remaining_percent=api_rem,
                    window_minutes=43200,
                    resets_at=resets_iso,
                )
            )

    # Grok Bot separate allowance
    if grok_window is not None:
        windows.append(grok_window)

    # Free credit grants (`GetUsageLimitStatusAndActiveGrants`)
    active_grants = (grants_data or {}).get("activeGrants")
    if isinstance(active_grants, list):
        now_ms = time.time() * 1000.0
        for idx, g in enumerate(active_grants):
            if not isinstance(g, dict):
                continue
            try:
                total_dollars = float(g.get("totalCents") or 0.0) / 100.0
                left_dollars = max(0.0, float(g.get("remainingCents") or 0.0) / 100.0)
                exp_ms = float(g.get("expiresAtMs") or 0.0)
            except (TypeError, ValueError):
                continue
            if total_dollars <= 0 or (exp_ms > 0 and exp_ms <= now_ms):
                continue
            used_dollars = max(0.0, total_dollars - left_dollars)
            used_pct = round(max(0.0, min(100.0, (100.0 * used_dollars) / total_dollars)), 1)
            rem_pct = round(max(0.0, 100.0 - used_pct), 1)
            camp_name = str(g.get("campaignName") or "").strip() or "Free credit"
            exp_iso = ms_to_utc_iso(exp_ms) if exp_ms > 0 else resets_iso
            disp_str = f"{format_dollars(used_dollars)} / {format_dollars(total_dollars)}"
            windows.append(
                QuotaWindow(
                    id=f"cursor_free_credit_{idx}",
                    group="Free Credit",
                    label=f"赠金 ({disp_str})",
                    used_percent=used_pct,
                    remaining_percent=rem_pct,
                    window_minutes=43200,
                    resets_at=exp_iso,
                    used_count=round(used_dollars, 2),
                    limit_count=round(total_dollars, 2),
                    unit="USD",
                )
            )
            badges.append(QuotaBadge(label=camp_name, value=disp_str))

    # On-demand spend
    od = compute_on_demand_info(period_data, hard_data)
    od_kind = od.get("kind")
    od_used = float(od.get("usedDollars") or 0.0)
    if od_kind == "fixed":
        od_limit = float(od.get("limitDollars") or 0.0)
        badges.append(
            QuotaBadge(label="On-demand", value=f"{format_dollars(od_used)} / {format_dollars(od_limit)}")
        )
    elif od_kind == "unlimited" or od_used > 0:
        badges.append(QuotaBadge(label="On-demand", value=format_dollars(od_used)))

    return AccountSnapshot(
        account_id=account_id,
        email=email,
        alias=alias,
        plan_tier=plan_name,
        status="ok" if windows else "error",
        is_active=is_active,
        updated_at=now_iso,
        windows=windows,
        badges=badges,
        error_message=None if windows else "Cursor Dashboard API 未返回有效配额窗口",
    )


def fetch_cursor_account_snapshot(
    token: str,
    account_id_fallback: str = "cursor_default",
    email_fallback: str = "",
    alias: str = "",
    is_active: bool = True,
    cursor_home: Optional[Path] = None,
) -> Optional[AccountSnapshot]:
    """Fetch all Cursor usage endpoints in parallel for a single token."""
    if not token or is_token_expired(token):
        return None
    team_id = read_active_team_id(token, cursor_home=cursor_home)

    with ThreadPoolExecutor(max_workers=6) as pool:
        f_me = pool.submit(post_connect_json, "aiserver.v1.DashboardService/GetMe", token, team_id)
        f_plan = pool.submit(post_connect_json, "aiserver.v1.DashboardService/GetPlanInfo", token, team_id)
        f_period = pool.submit(post_connect_json, "aiserver.v1.DashboardService/GetCurrentPeriodUsage", token, team_id)
        f_hard = pool.submit(post_connect_json, "aiserver.v1.DashboardService/GetHardLimit", token, team_id)
        f_grants = pool.submit(
            post_connect_json,
            "aiserver.v1.DashboardService/GetUsageLimitStatusAndActiveGrants",
            token,
            team_id,
        )
        f_grok = pool.submit(fetch_grok_bot_window, token)

        me_data = f_me.result()
        plan_data = f_plan.result()
        period_data = f_period.result()
        hard_data = f_hard.result()
        grants_data = f_grants.result()
        grok_window = f_grok.result()

    if not isinstance(period_data, dict):
        return None

    return parse_cursor_dashboard_payloads(
        me_data=me_data,
        plan_data=plan_data,
        period_data=period_data,
        hard_data=hard_data,
        grants_data=grants_data,
        grok_window=grok_window,
        account_id_fallback=account_id_fallback,
        email_fallback=email_fallback,
        alias=alias,
        is_active=is_active,
    )


def capture_account(env: Dict[str, str], vault: Any, alias: Optional[str] = None) -> Any:
    """Capture currently signed-in local Cursor account into Subelar `AccountVault`."""
    if vault is None:
        return None
    token = discover_local_cursor_token()
    if not token or is_token_expired(token):
        return None
    team_id = read_active_team_id(token)
    me_data = post_connect_json("aiserver.v1.DashboardService/GetMe", token, team_id=team_id) or {}
    plan_data = post_connect_json("aiserver.v1.DashboardService/GetPlanInfo", token, team_id=team_id) or {}
    email = str(me_data.get("email") or "").strip()
    plan_info = plan_data.get("planInfo") if isinstance(plan_data.get("planInfo"), dict) else {}
    plan_name = str(plan_info.get("planName") or "Premium").strip() or "Premium"
    sub = jwt_subject(token)
    sub_tail = sub.split("|")[-1] if sub else ""
    account_id = email or sub_tail or "cursor_primary"

    if email and hasattr(vault, "remove_account"):
        for stale_id in ("cursor_primary", sub_tail):
            if stale_id and stale_id != account_id:
                vault.remove_account("cursor", stale_id)

    profile_dir = vault.ensure_profile_dir("cursor", account_id)
    try:
        auth_file = profile_dir / "auth.json"
        auth_file.write_text(
            json.dumps({"accessToken": token, "email": email, "planTier": plan_name}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.chmod(auth_file, 0o600)
    except OSError:
        pass

    return vault.upsert_account(
        provider_id="cursor",
        account_id=account_id,
        email=email or account_id,
        plan_tier=plan_name,
        alias=alias,
        config_dir=str(profile_dir),
        auth_payload={
            "account_id": account_id,
            "email": email or account_id,
            "plan_tier": plan_name,
            "jwt_sub": sub,
        },
    )


def collect_quota(
    env: Dict[str, str],
    accounts: Optional[List[Dict[str, Any]]] = None,
    probe_dir: Optional[str] = None,
    vault: Any = None,
) -> ProviderSnapshot:
    """Main entry point invoked by Subelar `PluginRuntime`."""
    now_iso = utc_now_iso()
    active_token = discover_local_cursor_token()
    stored_records = vault.list_accounts("cursor") if vault is not None else []

    if not active_token and not stored_records and not accounts:
        return ProviderSnapshot(
            provider="cursor",
            display_name="Cursor",
            plan_tier="未登录",
            status="logged_out",
            updated_at=now_iso,
            windows=[],
            badges=[],
            error_message="未检测到 Cursor 登录凭证 (请运行 `cursor-agent login` 或在 Cursor IDE 登录)",
            accounts=[],
        )

    accounts_out: List[AccountSnapshot] = []
    seen_emails: set[str] = set()
    seen_tokens: set[str] = set()
    seen_subjects: set[str] = set()

    def _mark_seen(acc: AccountSnapshot, tok: str = "") -> None:
        if acc.email:
            seen_emails.add(acc.email.lower())
        if acc.account_id:
            seen_emails.add(acc.account_id.lower())
        if tok:
            seen_tokens.add(tok)
            subj = jwt_subject(tok)
            if subj:
                seen_subjects.add(subj)
                seen_emails.add(subj.split("|")[-1].lower())

    # 1. Collect active local Cursor account
    if active_token:
        active_sub = jwt_subject(active_token)
        fallback_id = active_sub.split("|")[-1] if active_sub else "cursor_primary"
        active_snap = fetch_cursor_account_snapshot(
            token=active_token,
            account_id_fallback=fallback_id,
            is_active=True,
        )
        if active_snap and active_snap.status == "ok":
            # Preserve user alias if already in vault (including from placeholder records)
            matched_rec = next(
                (
                    r
                    for r in stored_records
                    if r.account_id == active_snap.account_id
                    or r.email == active_snap.email
                    or (active_sub and str((r.auth_payload or {}).get("jwt_sub") or "") == active_sub)
                    or (r.account_id in ("cursor_primary", fallback_id) and r.alias)
                ),
                None,
            )
            if matched_rec and matched_rec.alias:
                active_snap = AccountSnapshot(
                    account_id=active_snap.account_id,
                    email=active_snap.email,
                    alias=matched_rec.alias,
                    plan_tier=active_snap.plan_tier,
                    status=active_snap.status,
                    is_active=True,
                    updated_at=active_snap.updated_at,
                    windows=active_snap.windows,
                    badges=active_snap.badges,
                    error_message=active_snap.error_message,
                )
            accounts_out.append(active_snap)
            _mark_seen(active_snap, active_token)
            if vault is not None:
                # Purge any legacy placeholder record (`cursor_primary` / raw user_id) for this same token
                if hasattr(vault, "remove_account"):
                    for stale_rec in list(stored_records):
                        if stale_rec.account_id == active_snap.account_id:
                            continue
                        stale_home = Path(stale_rec.config_dir) if stale_rec.config_dir else None
                        stale_tok = discover_local_cursor_token(cursor_home=stale_home) if stale_home else ""
                        stale_sub = jwt_subject(stale_tok) if stale_tok else str((stale_rec.auth_payload or {}).get("jwt_sub") or "")
                        if (
                            stale_rec.account_id in ("cursor_primary", fallback_id)
                            or (stale_tok and stale_tok == active_token)
                            or (active_sub and stale_sub and stale_sub == active_sub)
                        ):
                            vault.remove_account("cursor", stale_rec.account_id)

                profile_dir = vault.ensure_profile_dir("cursor", active_snap.account_id)
                try:
                    auth_file = profile_dir / "auth.json"
                    auth_file.write_text(
                        json.dumps(
                            {
                                "accessToken": active_token,
                                "email": active_snap.email,
                                "planTier": active_snap.plan_tier,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    os.chmod(auth_file, 0o600)
                except OSError:
                    pass
                vault.upsert_account(
                    provider_id="cursor",
                    account_id=active_snap.account_id,
                    email=active_snap.email,
                    plan_tier=active_snap.plan_tier,
                    config_dir=str(profile_dir),
                    auth_payload={
                        "account_id": active_snap.account_id,
                        "email": active_snap.email,
                        "plan_tier": active_snap.plan_tier,
                        "jwt_sub": active_sub,
                    },
                    snapshot=active_snap,
                )
                stored_records = vault.list_accounts("cursor")

    # 2. Collect any additional distinct stored accounts in AccountVault
    for rec in stored_records:
        if rec.account_id.lower() in seen_emails or (rec.email and rec.email.lower() in seen_emails):
            continue
        profile_home = Path(rec.config_dir) if rec.config_dir else None
        tok = discover_local_cursor_token(cursor_home=profile_home) if profile_home else ""
        if not tok and isinstance(rec.auth_payload, dict):
            tok = str(rec.auth_payload.get("access_token") or rec.auth_payload.get("accessToken") or "")

        tok_sub = jwt_subject(tok) if tok else str((rec.auth_payload or {}).get("jwt_sub") or "")
        if (tok and tok in seen_tokens) or (tok_sub and tok_sub in seen_subjects):
            if vault is not None and hasattr(vault, "remove_account") and rec.account_id.lower() not in seen_emails:
                vault.remove_account("cursor", rec.account_id)
            continue

        if tok:
            snap = fetch_cursor_account_snapshot(
                token=tok,
                account_id_fallback=rec.account_id,
                email_fallback=rec.email,
                alias=rec.alias,
                is_active=(len(accounts_out) == 0),
                cursor_home=profile_home,
            )
            if snap and snap.status == "ok":
                if snap.account_id.lower() in seen_emails or (snap.email and snap.email.lower() in seen_emails):
                    if vault is not None and hasattr(vault, "remove_account") and rec.account_id != snap.account_id:
                        vault.remove_account("cursor", rec.account_id)
                    continue
                accounts_out.append(snap)
                _mark_seen(snap, tok)
                if vault is not None:
                    if hasattr(vault, "remove_account") and rec.account_id != snap.account_id:
                        vault.remove_account("cursor", rec.account_id)
                    vault.upsert_account(
                        provider_id="cursor",
                        account_id=snap.account_id,
                        email=snap.email,
                        plan_tier=snap.plan_tier,
                        snapshot=snap,
                    )
                continue

        if rec.last_snapshot:
            try:
                cached = AccountSnapshot.from_dict(rec.last_snapshot)
                c_email = (rec.email or cached.email or "").lower()
                if c_email and c_email in seen_emails:
                    continue
                cached_snap = AccountSnapshot(
                    account_id=rec.account_id,
                    email=rec.email or cached.email,
                    alias=rec.alias or cached.alias,
                    plan_tier=rec.plan_tier or cached.plan_tier,
                    status="cached",
                    is_active=(len(accounts_out) == 0),
                    updated_at=cached.updated_at,
                    windows=cached.windows,
                    badges=cached.badges,
                )
                accounts_out.append(cached_snap)
                _mark_seen(cached_snap, tok)
            except Exception:
                pass

    if not accounts_out:
        return ProviderSnapshot(
            provider="cursor",
            display_name="Cursor",
            plan_tier="未知",
            status="error",
            updated_at=now_iso,
            windows=[],
            badges=[],
            error_message="无法获取 Cursor 配额数据 (Token 可能已过期)",
            accounts=[],
        )

    primary = next((a for a in accounts_out if a.is_active and a.status in ("ok", "cached")), accounts_out[0])
    return ProviderSnapshot(
        provider="cursor",
        display_name="Cursor",
        plan_tier=primary.plan_tier or "Premium",
        status="ok" if primary.status in ("ok", "cached") else primary.status,
        updated_at=primary.updated_at or now_iso,
        windows=list(primary.windows),
        badges=list(primary.badges),
        error_message=primary.error_message,
        accounts=accounts_out,
    )
