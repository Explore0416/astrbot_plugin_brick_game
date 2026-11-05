"""砖厂游戏：QQ 群签到、烧砖、拍人、积分兑换 Xiaoning-API 余额/重置卡。

仅面向 NapCat / OneBot v11（AstrBot 的 aiocqhttp 适配器）。
数据以单个 JSON 文件持久化，消息推进使用内存状态 + 防抖落盘，避免频繁 IO。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from astrbot.api import logger

try:
    from astrbot.api import AstrBotConfig
except ImportError:  # 兼容旧版 AstrBot
    AstrBotConfig = dict

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star

try:
    from astrbot.api.star import StarTools
except ImportError:  # 旧版没有 StarTools 时使用插件数据目录回退逻辑
    StarTools = None

try:
    from astrbot.api.star import register as _astrbot_register
except ImportError:
    _astrbot_register = None

PLUGIN_NAME = "astrbot_plugin_brick_game"
PLUGIN_VERSION = "1.1.1"
PLUGIN_REPO = "https://github.com/Explore0416/astrbot_plugin_brick_game"
STATE_FILE = "state.json"
SAVE_DEBOUNCE_SECONDS = 10
MAX_BAN_SECONDS = 30 * 24 * 60 * 60
MAX_BRICK_TARGET = 1000
XIAONING_STATUS_CACHE_SECONDS = 3600
XIAONING_MAX_CARDS_PER_EXCHANGE = 1000
XIAONING_MAX_BALANCE_PER_EXCHANGE = 100000

# 指令可直接发送，不强制依赖唤醒前缀；匹配后由插件优先处理并阻止默认 LLM 回复。
COMMAND_PATTERN = (
    r"^[/!！]?(签到|打卡|烧砖|拍人|拍|我的砖|砖厂状态|砖厂帮助|砖厂玩法|砖厂|"
    r"积分榜|排行榜|我的积分|积分|"
    r"绑定账号|绑定用户|绑定|解绑账号|解绑|"
    r"兑换查询|兑换状态|兑换余额|兑换额度|兑换重置卡|兑换卡|兑换)"
    r"(?=\s|$|[，。！？、,.!?])"
)
COMMAND_RE = re.compile(COMMAND_PATTERN)

SIGN_COMMANDS = {"签到", "打卡"}
BURN_COMMANDS = {"烧砖"}
SLAP_COMMANDS = {"拍人", "拍"}
STATUS_COMMANDS = {"我的砖", "砖厂", "砖厂状态", "积分", "我的积分"}
RANK_COMMANDS = {"积分榜", "排行榜"}
HELP_COMMANDS = {"砖厂帮助", "砖厂玩法"}
BIND_COMMANDS = {"绑定", "绑定账号", "绑定用户"}
UNBIND_COMMANDS = {"解绑", "解绑账号"}
EXCHANGE_STATUS_COMMANDS = {"兑换查询", "兑换状态"}
EXCHANGE_COMMANDS = {
    "兑换",
    "兑换余额",
    "兑换额度",
    "兑换重置卡",
    "兑换卡",
}



def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_name(value: Any, fallback: str = "") -> str:
    text = " ".join(str(value or "").split())
    return text[:24] or fallback or "未知用户"


class XiaoNingError(Exception):
    """Xiaoning-API 请求失败。"""

    def __init__(self, message: str, status: int = 0, code: str = "") -> None:
        clean_message = " ".join(str(message or "").split())[:240]
        super().__init__(clean_message or "未知错误")
        self.status = int(status or 0)
        self.code = str(code or "")


class BrickStore:
    """插件状态存储：加载、原子写入、防抖保存。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()
        self._dirty = False
        self._save_task: asyncio.Task[None] | None = None

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {"version": 1, "users": {}, "groups": {}}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_state()

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("读取砖厂数据失败，将在备份损坏文件后重新开始: %s", exc)
            try:
                backup = self.path.with_name(
                    f"{self.path.stem}.corrupt-{int(time.time())}{self.path.suffix}",
                )
                self.path.replace(backup)
            except OSError:
                logger.warning("损坏的砖厂数据文件备份失败: %s", self.path)
            return self._empty_state()

        if not isinstance(raw, dict):
            return self._empty_state()

        users = raw.get("users")
        groups = raw.get("groups")
        return {
            "version": 1,
            "users": users if isinstance(users, dict) else {},
            "groups": groups if isinstance(groups, dict) else {},
        }

    def mark_dirty(self) -> None:
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.save_now()
            return

        if self._save_task is None or self._save_task.done():
            self._save_task = loop.create_task(self._debounced_save())

    async def _debounced_save(self) -> None:
        await asyncio.sleep(SAVE_DEBOUNCE_SECONDS)
        if self._dirty:
            self.save_now()

    def save_now(self) -> None:
        if not self._dirty:
            return

        tmp_path = self.path.with_name(f"{self.path.name}.tmp")
        try:
            payload = json.dumps(
                self.data,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            with open(tmp_path, "w", encoding="utf-8") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, self.path)
            self._dirty = False
        except Exception:
            logger.error("保存砖厂数据失败: %s", self.path, exc_info=True)

    async def close(self) -> None:
        task = self._save_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.save_now()


def _get_data_dir() -> Path:
    try:
        return Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            path = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        except Exception:
            path = Path("data") / "plugin_data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path


def _register_plugin(*args: Any, **kwargs: Any):
    """兼容旧版 AstrBot：旧版需要 register 装饰器，新版会自动注册。"""

    if _astrbot_register is None:
        return lambda cls: cls
    return _astrbot_register(*args, **kwargs)


def _regex_filter(pattern: str, *, priority: int = 0):
    """兼容不支持 priority 参数的旧版 AstrBot。"""

    try:
        return filter.regex(pattern, priority=priority)
    except TypeError:
        return filter.regex(pattern)


def _event_message_type_filter(event_type: Any, *, priority: int = 0):
    """兼容不支持 priority 参数的旧版 AstrBot。"""

    try:
        return filter.event_message_type(event_type, priority=priority)
    except TypeError:
        return filter.event_message_type(event_type)


@_register_plugin(
    PLUGIN_NAME,
    "TraeCode",
    "砖厂游戏：签到、烧砖、拍人、Xiaoning-API 积分兑换",
    PLUGIN_VERSION,
    PLUGIN_REPO,
)
class BrickGame(Star):
    """砖厂游戏：签到、烧砖、拍人、积分兑换。

    指令可直接发送，无需 @ 机器人：
    签到 / 打卡、烧砖、拍人 @某人、我的砖、积分榜、
    绑定账号、兑换余额、兑换重置卡、兑换查询、砖厂帮助。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context, config=config)
        self.config = config or {}
        self.store = BrickStore(_get_data_dir() / STATE_FILE)
        self._lock = asyncio.Lock()
        self._quota_cache: dict[str, tuple[float, int]] = {}

    async def terminate(self) -> None:
        await self.store.close()

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def _cfg_int(
        self,
        key: str,
        default: int,
        minimum: int,
        maximum: int | None = None,
    ) -> int:
        value = self.config.get(key, default) if self.config else default
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = default
        value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default) if self.config else default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "开启"}

    def _cfg_str(self, key: str, default: str) -> str:
        value = self.config.get(key, default) if self.config else default
        if not isinstance(value, str):
            return default
        return value.strip() or default

    def _sign_points(self) -> int:
        return self._cfg_int("sign_points", 10, 0, MAX_BRICK_TARGET)

    def _brick_messages(self) -> int:
        return self._cfg_int("brick_messages", 10, 1, MAX_BRICK_TARGET)

    def _max_bricks(self) -> int:
        return self._cfg_int("max_bricks", 3, 1, 100)

    def _brick_cost(self) -> int:
        return self._cfg_int("brick_start_cost_points", 0, 0, 1_000_000)

    def _mute_seconds(self) -> int:
        return self._cfg_int("mute_seconds", 60, 1, MAX_BAN_SECONDS)

    def _leaderboard_size(self) -> int:
        return self._cfg_int("leaderboard_size", 10, 1, 50)

    def _protect_admins(self) -> bool:
        return self._cfg_bool("protect_admins", True)

    def _notify_on_complete(self) -> bool:
        return self._cfg_bool("notify_on_complete", True)

    def _timezone_name(self) -> str:
        return self._cfg_str("timezone", "Asia/Shanghai")

    def _exchange_enabled(self) -> bool:
        return self._cfg_bool("xiaoning_enabled", False)

    def _exchange_mode(self) -> str:
        mode = self._cfg_str("xiaoning_exchange_mode", "both").lower()
        if mode not in {"balance", "reset_card", "both"}:
            return "both"
        return mode

    def _allow_balance(self) -> bool:
        return self._exchange_mode() in {"balance", "both"}

    def _allow_reset_card(self) -> bool:
        return self._exchange_mode() in {"reset_card", "both"}

    def _points_per_balance(self) -> int:
        return self._cfg_int(
            "xiaoning_points_per_balance",
            10,
            1,
            1_000_000_000,
        )

    def _points_per_reset_card(self) -> int:
        return self._cfg_int(
            "xiaoning_points_per_reset_card",
            100,
            1,
            1_000_000_000,
        )

    def _daily_balance_limit(self) -> int:
        return self._cfg_int("xiaoning_daily_balance_limit", 1, 0, 100000)

    def _monthly_balance_limit(self) -> int:
        return self._cfg_int("xiaoning_monthly_balance_limit", 10, 0, 1000000)

    def _daily_reset_card_limit(self) -> int:
        return self._cfg_int("xiaoning_daily_reset_card_limit", 1, 0, 1000)

    def _monthly_reset_card_limit(self) -> int:
        return self._cfg_int("xiaoning_monthly_reset_card_limit", 5, 0, 10000)

    def _quota_per_unit_override(self) -> int:
        return self._cfg_int("xiaoning_quota_per_unit", 0, 0, 10**12)

    def _fixed_xiao_user_id(self) -> int:
        return self._cfg_int("xiaoning_fixed_user_id", 0, 0, 10**12)

    def _xiaoning_timeout(self) -> int:
        return self._cfg_int("xiaoning_timeout_seconds", 15, 3, 120)

    def _xiaoning_token(self) -> str:
        token = self._cfg_str("xiaoning_access_token", "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token

    def _xiaoning_base_url(self) -> str:
        raw = self._cfg_str("xiaoning_base_url", "").strip()
        if not raw:
            return ""
        if raw.startswith("//"):
            raw = f"https:{raw}"
        if not re.match(r"^https?://", raw, re.IGNORECASE):
            raw = f"https://{raw}"

        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return ""

        path = parsed.path.rstrip("/")
        if path.lower().endswith("/v1"):
            path = path[:-3].rstrip("/")
        if path.lower().endswith("/api"):
            path = path[:-4].rstrip("/")

        return urlunsplit(
            (parsed.scheme.lower(), parsed.netloc, path, "", ""),
        )

    # ------------------------------------------------------------------
    # 通用工具
    # ------------------------------------------------------------------

    def _today(self) -> date:
        timezone_name = self._timezone_name()
        try:
            return datetime.now(ZoneInfo(timezone_name)).date()
        except Exception:
            if timezone_name == "Asia/Shanghai":
                return datetime.now(timezone(timedelta(hours=8))).date()
            return datetime.now().astimezone().date()

    @staticmethod
    def _format_duration(seconds: int) -> str:
        seconds = max(1, int(seconds))
        if seconds % 86400 == 0:
            return f"{seconds // 86400} 天"
        if seconds % 3600 == 0:
            return f"{seconds // 3600} 小时"
        if seconds % 60 == 0:
            return f"{seconds // 60} 分钟"
        return f"{seconds} 秒"

    def _user(self, user_id: str, name: str = "") -> dict[str, Any]:
        users = self.store.data["users"]
        user = users.get(user_id)
        if not isinstance(user, dict):
            user = {}
            users[user_id] = user

        user.setdefault("name", "")
        user.setdefault("points", 0)
        user.setdefault("bricks", 0)
        user.setdefault("total_signs", 0)
        user.setdefault("total_bricks", 0)
        user.setdefault("total_slaps", 0)
        user.setdefault("streak", 0)
        user.setdefault("last_sign_date", "")
        user.setdefault("xiao_user_id", "")
        user.setdefault("xiao_bound_at", 0)
        user.setdefault("exchange_daily_date", "")
        user.setdefault("exchange_daily_balance", 0)
        user.setdefault("exchange_daily_cards", 0)
        user.setdefault("exchange_month", "")
        user.setdefault("exchange_monthly_balance", 0)
        user.setdefault("exchange_monthly_cards", 0)
        user.setdefault("exchange_total_balance", 0)
        user.setdefault("exchange_total_cards", 0)
        user["last_seen"] = int(time.time())

        if name:
            user["name"] = _safe_name(name, user_id)
        return user

    def _exchange_usage(self, user: dict[str, Any]) -> dict[str, int | str]:
        today = self._today().isoformat()
        month = today[:7]

        if user.get("exchange_daily_date") != today:
            user["exchange_daily_date"] = today
            user["exchange_daily_balance"] = 0
            user["exchange_daily_cards"] = 0
        if user.get("exchange_month") != month:
            user["exchange_month"] = month
            user["exchange_monthly_balance"] = 0
            user["exchange_monthly_cards"] = 0

        return {
            "daily_date": str(user.get("exchange_daily_date") or ""),
            "month": str(user.get("exchange_month") or ""),
            "daily_balance": max(0, _as_int(user.get("exchange_daily_balance"))),
            "daily_cards": max(0, _as_int(user.get("exchange_daily_cards"))),
            "monthly_balance": max(0, _as_int(user.get("exchange_monthly_balance"))),
            "monthly_cards": max(0, _as_int(user.get("exchange_monthly_cards"))),
        }

    def _burns(self, group_id: str, create: bool = False) -> list[dict[str, Any]]:
        groups = self.store.data["groups"]
        group = groups.get(group_id)
        if not isinstance(group, dict):
            if not create:
                return []
            group = {}
            groups[group_id] = group

        burns = group.get("burns")
        if not isinstance(burns, list):
            if not create:
                return []
            burns = []
            group["burns"] = burns
        return burns

    def _active_burn_count(self, user_id: str) -> int:
        count = 0
        for group in self.store.data["groups"].values():
            if not isinstance(group, dict):
                continue
            burns = group.get("burns")
            if not isinstance(burns, list):
                continue
            for burn in burns:
                if isinstance(burn, dict) and str(burn.get("owner_id")) == user_id:
                    count += 1
        return count

    def _is_countable_message(self, event: AstrMessageEvent) -> bool:
        components = event.get_messages()
        if not components:
            return bool(event.message_str.strip())
        for component in components:
            if isinstance(component, Plain):
                if component.text.strip():
                    return True
            elif not isinstance(component, (At, Reply)):
                return True
        return False

    def _resolve_target(self, event: AstrMessageEvent, rest: str) -> tuple[str, str]:
        self_id = str(event.get_self_id() or "")

        for component in event.get_messages():
            if isinstance(component, At):
                target = str(component.qq)
                if target in {"", "all", self_id}:
                    continue
                return target, str(component.name or "")

        match = re.search(r"(?:^|\s)@?(\d{5,12})(?!\d)", rest)
        if match:
            return match.group(1), ""

        for component in reversed(event.get_messages()):
            if isinstance(component, Reply) and component.sender_id:
                target = str(component.sender_id)
                if target != self_id:
                    return target, str(component.sender_nickname or "")

        return "", ""

    async def _call_bot_api(
        self,
        event: AstrMessageEvent,
        action: str,
        **params: Any,
    ) -> Any:
        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None) if bot is not None else None
        if not callable(call_action):
            raise RuntimeError("当前平台不支持 OneBot 群管理 API")

        self_id = str(event.get_self_id() or "")
        if self_id:
            params.setdefault("self_id", self_id)
        return await call_action(action, **params)

    async def _member_info(
        self,
        event: AstrMessageEvent,
        group_id: int,
        user_id: int,
    ) -> dict[str, Any] | None:
        try:
            info = await self._call_bot_api(
                event,
                "get_group_member_info",
                group_id=group_id,
                user_id=user_id,
            )
        except Exception as exc:
            logger.warning(
                "获取群成员信息失败 group=%s user=%s: %s",
                group_id,
                user_id,
                exc,
            )
            return None
        return info if isinstance(info, dict) else None

    # ------------------------------------------------------------------
    # Xiaoning-API 客户端
    # ------------------------------------------------------------------

    @staticmethod
    def _blocking_xiao_request(
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout: int,
    ) -> dict[str, Any]:
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        request = Request(url, data=body, headers=headers, method=method)
        status = 0
        try:
            with urlopen(request, timeout=timeout) as response:
                status = int(response.status)
                raw_body = response.read()
        except HTTPError as exc:
            status = int(exc.code)
            raw_body = exc.read()
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise XiaoNingError(f"无法连接 Xiaoning-API：{reason}") from exc
        except Exception as exc:
            raise XiaoNingError(f"请求 Xiaoning-API 失败：{exc}") from exc

        text = raw_body.decode("utf-8", errors="replace")
        parsed: Any = {}
        if text.strip():
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise XiaoNingError(
                    f"Xiaoning-API 返回了无法解析的内容（HTTP {status}）",
                    status,
                ) from exc

        if not isinstance(parsed, dict):
            if status >= 400:
                raise XiaoNingError(f"HTTP {status}", status)
            return {"success": True, "data": parsed}

        message = str(parsed.get("message") or parsed.get("error") or "").strip()
        code = str(parsed.get("code") or "")
        if status >= 400:
            raise XiaoNingError(message or f"HTTP {status}", status, code)
        if parsed.get("success") is False:
            raise XiaoNingError(message or "Xiaoning-API 返回失败", status, code)
        return parsed

    async def _xiao_request(
        self,
        method: str,
        base_url: str,
        token: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        auth: bool = True,
    ) -> dict[str, Any]:
        if not base_url:
            raise XiaoNingError("Base URL 未配置或格式不正确")
        if auth and not token:
            raise XiaoNingError("访问令牌未配置")

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"AstrBot-BrickGame/{PLUGIN_VERSION}",
        }
        if auth:
            headers["Authorization"] = f"Bearer {token}"

        return await asyncio.to_thread(
            self._blocking_xiao_request,
            method,
            f"{base_url}{path}",
            headers,
            payload,
            self._xiaoning_timeout(),
        )

    async def _xiao_quota_per_unit(self, base_url: str) -> int:
        override = self._quota_per_unit_override()
        if override > 0:
            return override

        now = time.monotonic()
        cached = self._quota_cache.get(base_url)
        if cached and now - cached[0] < XIAONING_STATUS_CACHE_SECONDS:
            return cached[1]

        try:
            response = await self._xiao_request(
                "GET",
                base_url,
                "",
                "/api/status",
                auth=False,
            )
        except XiaoNingError as exc:
            logger.warning("读取 Xiaoning-API /api/status 失败: %s", exc)
            raise XiaoNingError(
                "无法读取额度换算比例，请在插件配置中设置“1 额度对应的 quota 值”",
            ) from exc

        data = response.get("data")
        if not isinstance(data, dict):
            raise XiaoNingError(
                "Xiaoning-API 的 /api/status 未返回有效配置，"
                "请在插件配置中手动设置额度换算比例",
            )

        display_type = str(data.get("quota_display_type") or "").upper()
        if not display_type:
            display_type = "TOKENS" if data.get("display_in_currency") is False else "USD"

        if display_type == "TOKENS":
            value = 1
        else:
            try:
                quota_value = float(data.get("quota_per_unit"))
            except (TypeError, ValueError):
                quota_value = 0.0
            if isinstance(data.get("quota_per_unit"), bool) or quota_value <= 0:
                raise XiaoNingError(
                    "Xiaoning-API 的 /api/status 未返回有效的 quota_per_unit，"
                    "请在插件配置中手动设置额度换算比例",
                )

            rate = 1.0
            if display_type == "CNY":
                try:
                    rate = float(data.get("usd_exchange_rate"))
                except (TypeError, ValueError):
                    rate = 0.0
            elif display_type == "CUSTOM":
                try:
                    rate = float(data.get("custom_currency_exchange_rate"))
                except (TypeError, ValueError):
                    rate = 0.0
            if rate <= 0:
                raise XiaoNingError(
                    "Xiaoning-API 的 /api/status 未返回有效的货币汇率，"
                    "请在插件配置中手动设置额度换算比例",
                )
            value = max(1, int(round(quota_value / rate)))

        self._quota_cache[base_url] = (now, value)
        return value

    async def _xiao_add_balance(
        self,
        base_url: str,
        token: str,
        target_user_id: int,
        raw_quota: int,
    ) -> None:
        await self._xiao_request(
            "POST",
            base_url,
            token,
            "/api/user/manage",
            {
                "id": target_user_id,
                "action": "add_quota",
                "mode": "add",
                "value": raw_quota,
            },
        )

    async def _xiao_grant_reset_cards(
        self,
        base_url: str,
        token: str,
        target_user_id: int,
        count: int,
    ) -> None:
        await self._xiao_request(
            "POST",
            base_url,
            token,
            f"/api/subscription/admin/users/{target_user_id}/reset_cards",
            {
                "count": count,
                "expires_at": 0,
                "note": "AstrBot 砖厂兑换",
            },
        )

    @staticmethod
    def _friendly_xiao_error(exc: XiaoNingError, kind: str) -> str:
        if exc.code in {"ACCESS_TOKEN_ROUTE_UNDECLARED", "AUTH_SESSION_REQUIRED"}:
            if kind == "reset_card":
                return (
                    "服务端当前未对访问令牌开放“重置卡发放”接口。"
                    "请管理员使用旧版管理员访问令牌，或更新 Xiaoning-API 路由权限。"
                )
            return "访问令牌无权访问该接口，请联系管理员。"
        if exc.code == "ACCESS_TOKEN_SCOPE_DENIED":
            return "访问令牌缺少所需权限 scope，请联系管理员重新生成。"
        if exc.code == "ACCESS_TOKEN_EXPIRED":
            return "访问令牌已过期，请联系管理员更新。"

        if exc.status == 401:
            return "访问令牌无效或已过期，请联系管理员。"
        if exc.status == 403:
            return "访问令牌权限不足，请联系管理员。"
        if exc.status == 404:
            if kind == "reset_card":
                return "服务端不存在重置卡发放接口，可能是 Xiaoning-API 版本不支持。"
            return "接口不存在，请检查 Base URL 或服务端版本。"
        if exc.status >= 500:
            return "Xiaoning-API 服务异常，请稍后再试。"
        return str(exc) or "未知错误"

    # ------------------------------------------------------------------
    # 指令入口
    # ------------------------------------------------------------------

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @_regex_filter(COMMAND_PATTERN, priority=100)
    async def on_game_command(self, event: AstrMessageEvent):
        """处理签到、烧砖、拍人、查询和帮助指令，触发时优先于 AI 回复。"""
        sender_id = str(event.get_sender_id() or "")
        if not sender_id or sender_id == str(event.get_self_id() or ""):
            return

        text = event.message_str.strip()
        matched = COMMAND_RE.match(text)
        if not matched:
            return

        command = matched.group(1)
        rest = text[matched.end() :].strip(" \t，。！？、,.!?")

        try:
            result = await self._dispatch_command(event, command, rest)
        except Exception:
            logger.error("处理砖厂指令失败: %s", command, exc_info=True)
            result = event.plain_result("砖厂出了点小故障，请稍后再试。")

        if result is None:
            return

        # 先 yield 让回复进入发送阶段，再停止事件传播，确保回复不会被拦截。
        event.should_call_llm(False)
        yield result
        event.stop_event()

    async def _dispatch_command(
        self,
        event: AstrMessageEvent,
        command: str,
        rest: str,
    ) -> Any:
        if command in SIGN_COMMANDS:
            return await self._handle_sign(event)
        if command in BURN_COMMANDS:
            return await self._handle_burn(event)
        if command in SLAP_COMMANDS:
            return await self._handle_slap_command(event, command, rest)
        if command in STATUS_COMMANDS:
            return self._handle_status(event)
        if command in RANK_COMMANDS:
            return self._handle_rank(event)
        if command in BIND_COMMANDS:
            return self._handle_bind(event, rest)
        if command in UNBIND_COMMANDS:
            return self._handle_unbind(event)
        if command in EXCHANGE_STATUS_COMMANDS:
            return self._handle_exchange_status(event)
        if command in EXCHANGE_COMMANDS:
            return await self._handle_exchange(event, command, rest)
        if command in HELP_COMMANDS:
            return self._handle_help(event)
        return None

    # ------------------------------------------------------------------
    # 签到
    # ------------------------------------------------------------------

    async def _handle_sign(self, event: AstrMessageEvent) -> Any:
        user_id = str(event.get_sender_id())
        user_name = event.get_sender_name()
        today = self._today()
        today_text = today.isoformat()
        yesterday_text = (today - timedelta(days=1)).isoformat()
        gain = self._sign_points()

        async with self._lock:
            user = self._user(user_id, user_name)
            if user.get("last_sign_date") == today_text:
                return event.plain_result(
                    "今天已经签到过了，明天再来。\n"
                    f"积分：{_as_int(user.get('points'))} | "
                    f"连续签到：{_as_int(user.get('streak'))} 天 | "
                    f"可用砖块：{_as_int(user.get('bricks'))}/{self._max_bricks()}",
                )

            streak = (
                _as_int(user.get("streak")) + 1
                if user.get("last_sign_date") == yesterday_text
                else 1
            )
            user["points"] = _as_int(user.get("points")) + gain
            user["last_sign_date"] = today_text
            user["streak"] = streak
            user["total_signs"] = _as_int(user.get("total_signs")) + 1
            self.store.mark_dirty()

        self.store.save_now()
        return event.plain_result(
            f"签到成功，获得 {gain} 积分。\n"
            f"积分：{_as_int(user.get('points'))} | "
            f"连续签到：{streak} 天 | "
            f"可用砖块：{_as_int(user.get('bricks'))}/{self._max_bricks()}",
        )

    # ------------------------------------------------------------------
    # 烧砖
    # ------------------------------------------------------------------

    async def _handle_burn(self, event: AstrMessageEvent) -> Any:
        group_id = str(event.get_group_id() or "")
        if not group_id:
            return event.plain_result("烧砖只能在 QQ 群聊里进行。")

        user_id = str(event.get_sender_id())
        user_name = event.get_sender_name()
        target = self._brick_messages()
        max_bricks = self._max_bricks()
        cost = self._brick_cost()

        async with self._lock:
            user = self._user(user_id, user_name)
            bricks = _as_int(user.get("bricks"))
            active = self._active_burn_count(user_id)
            if bricks + active >= max_bricks:
                return event.plain_result(
                    f"你已有 {bricks} 块可用砖、{active} 块正在烧，"
                    f"达到上限 {max_bricks} 块。\n"
                    "先用“拍人 @某人”消耗一块，再回来烧砖。",
                )

            points = _as_int(user.get("points"))
            if cost > 0 and points < cost:
                return event.plain_result(
                    f"烧砖需要 {cost} 积分，你当前只有 {points} 积分。\n"
                    "先发送“签到”攒积分吧。",
                )

            if cost > 0:
                user["points"] = points - cost

            burns = self._burns(group_id, create=True)
            burns.append(
                {
                    "owner_id": user_id,
                    "owner_name": _safe_name(user_name, user_id),
                    "progress": 0,
                    "target": target,
                    "created_at": int(time.time()),
                },
            )
            active_after = active + 1
            self.store.mark_dirty()

        self.store.save_now()
        cost_text = f"已扣除 {cost} 积分。" if cost else "本次烧砖免费。"
        extra_note = (
            "你还有其他砖在烧，群友每发一条消息都会同时推进它们。"
            if active
            else ""
        )
        return event.plain_result(
            f"开始烧砖！{cost_text}\n"
            f"本群其他成员累计发送 {target} 条消息即可烧成，当前进度 0/{target}。\n"
            f"你当前可用 {bricks} 块、正在烧 {active_after} 块，上限 {max_bricks} 块。"
            f"{extra_note}",
        )

    # ------------------------------------------------------------------
    # 拍人
    # ------------------------------------------------------------------

    async def _handle_slap_command(
        self,
        event: AstrMessageEvent,
        command: str,
        rest: str,
    ) -> Any:
        target_id, target_name = self._resolve_target(event, rest)
        if not target_id:
            # “拍 某个东西”这类普通聊天不劫持，交给后续流程。
            if command == "拍" and rest:
                return None
            return event.plain_result(
                "用法：拍人 @某人\n"
                "也可以回复目标的消息，发送“拍人”。\n"
                "每次消耗 1 块砖。",
            )

        return await self._perform_slap(event, target_id, target_name)

    async def _perform_slap(
        self,
        event: AstrMessageEvent,
        target_id: str,
        target_name: str,
    ) -> Any:
        group_id = str(event.get_group_id() or "")
        if not group_id or not group_id.isdigit():
            return event.plain_result("拍人只能在 QQ 群聊里进行。")

        sender_id = str(event.get_sender_id())
        if target_id == sender_id:
            return event.plain_result("不能拍自己。")

        self_id = str(event.get_self_id() or "")
        if target_id == self_id:
            return event.plain_result("不能拍机器人自己。")

        if not target_id.isdigit():
            return event.plain_result("无法识别目标 QQ 号，请使用 @ 或回复对方消息。")

        user = self._user(sender_id, event.get_sender_name())
        if _as_int(user.get("bricks")) <= 0:
            return self._no_brick_result(event, sender_id)

        group_int = int(group_id)
        target_int = int(target_id)

        member_info: dict[str, Any] | None = None
        if self._protect_admins():
            member_info = await self._member_info(event, group_int, target_int)
            role = str(member_info.get("role", "")) if member_info else ""
            if role in {"owner", "admin"}:
                return event.plain_result("对方是群主或管理员，不能拍。砖块未消耗。")

        if member_info:
            target_name = str(
                member_info.get("card")
                or member_info.get("nickname")
                or target_name
                or target_id,
            )

        async with self._lock:
            user = self._user(sender_id, event.get_sender_name())
            bricks = _as_int(user.get("bricks"))
            if bricks <= 0:
                return self._no_brick_result(event, sender_id)
            user["bricks"] = bricks - 1
            self.store.mark_dirty()

        duration = self._mute_seconds()
        try:
            await self._call_bot_api(
                event,
                "set_group_ban",
                group_id=group_int,
                user_id=target_int,
                duration=duration,
            )
        except Exception as exc:
            logger.warning(
                "拍人禁言失败 group=%s target=%s: %s",
                group_int,
                target_int,
                exc,
            )
            async with self._lock:
                user = self._user(sender_id, event.get_sender_name())
                user["bricks"] = _as_int(user.get("bricks")) + 1
                self.store.mark_dirty()
            self.store.save_now()
            return event.plain_result(
                "拍人失败，砖块已退回。\n"
                "请确认机器人是群管理员，并且目标不是群主或管理员。",
            )

        async with self._lock:
            user = self._user(sender_id, event.get_sender_name())
            user["total_slaps"] = _as_int(user.get("total_slaps")) + 1
            user["last_slap_target"] = target_id
            user["last_slap_at"] = int(time.time())
            remaining = _as_int(user.get("bricks"))
            self.store.mark_dirty()

        self.store.save_now()
        duration_text = self._format_duration(duration)
        display_name = _safe_name(target_name, target_id)
        return event.chain_result(
            [
                At(qq=target_id, name=display_name),
                Plain(
                    f" 已被禁言 {duration_text}。\n"
                    f"消耗 1 块砖，你还有 {remaining} 块可用。",
                ),
            ],
        )

    def _no_brick_result(self, event: AstrMessageEvent, user_id: str) -> Any:
        active = self._active_burn_count(user_id)
        progress_note = (
            f"你正在烧 {active} 块砖，还没完成。" if active else "你还没有开始烧砖。"
        )
        return event.plain_result(
            f"你没有可用的砖。{progress_note}\n"
            f"发送“烧砖”，让群友聊 {self._brick_messages()} 条消息帮你烧一块。",
        )

    # ------------------------------------------------------------------
    # 积分兑换 Xiaoning-API
    # ------------------------------------------------------------------

    @staticmethod
    def _limit_progress(used: int, limit: int, unit: str) -> str:
        if limit <= 0:
            return f"{used} {unit}（不限）"
        return f"{used}/{limit} {unit}"

    def _exchange_mode_text(self) -> str:
        return {
            "balance": "仅添加余额",
            "reset_card": "仅重置卡",
            "both": "余额 / 重置卡",
        }.get(self._exchange_mode(), "余额 / 重置卡")

    def _handle_bind(self, event: AstrMessageEvent, rest: str) -> Any:
        user_id = str(event.get_sender_id())
        user = self._user(user_id, event.get_sender_name())
        current = str(user.get("xiao_user_id") or "")
        fixed_id = self._fixed_xiao_user_id()
        text = rest.strip()

        if fixed_id > 0:
            return event.plain_result(
                f"管理员已设置统一兑换账号：Xiaoning 用户 ID {fixed_id}。\n"
                "所有用户的兑换都会发放到该账号，个人绑定不会改变兑换目标。",
            )

        if not text:
            lines = ["Xiaoning-API 账号绑定"]
            if current:
                lines.append(f"当前绑定：用户 ID {current}")
                lines.append("如需更换，发送“绑定账号 <新的用户ID>”。")
            else:
                lines.append("当前未绑定。")
                lines.append("发送“绑定账号 <你的 Xiaoning 用户ID>”。")
                lines.append("用户ID可在 Xiaoning-API 的用户管理页查看。")
            lines.append("兑换后的余额/重置卡会发放到该账号。")
            return event.plain_result("\n".join(lines))

        tokens = text.replace("＠", "@").split()
        if tokens and tokens[0].lower() in {"账号", "用户", "id"}:
            tokens = tokens[1:]

        if len(tokens) != 1:
            return event.plain_result(
                "用法：绑定账号 <Xiaoning 用户ID>\n"
                "例如：绑定账号 123",
            )

        raw_id = tokens[0].lstrip("@").strip()
        if not raw_id.isdigit():
            return event.plain_result(
                "Xiaoning 用户ID必须是数字。\n"
                "可在 Xiaoning-API 用户管理页查看。",
            )

        target_id = str(int(raw_id))
        if int(target_id) <= 0:
            return event.plain_result("用户ID必须大于 0。")

        old_id = str(user.get("xiao_user_id") or "")
        user["xiao_user_id"] = target_id
        user["xiao_bound_at"] = int(time.time())
        self.store.mark_dirty()
        self.store.save_now()

        if old_id == target_id:
            message = f"你已经绑定 Xiaoning 用户 ID {target_id}。"
        elif old_id:
            message = f"绑定已更新：{old_id} -> {target_id}。"
        else:
            message = f"绑定成功：Xiaoning 用户 ID {target_id}。"
        return event.plain_result(
            f"{message}\n兑换后的余额/重置卡会发放到此账号。\n"
            "请确认 ID 正确，兑换成功后无法撤回。",
        )

    def _handle_unbind(self, event: AstrMessageEvent) -> Any:
        user_id = str(event.get_sender_id())
        user = self._user(user_id, event.get_sender_name())
        fixed_id = self._fixed_xiao_user_id()
        if fixed_id > 0:
            return event.plain_result(
                f"管理员已设置统一兑换账号：Xiaoning 用户 ID {fixed_id}。\n"
                "个人绑定不影响兑换，无需解绑。",
            )

        old_id = str(user.get("xiao_user_id") or "")
        if not old_id:
            return event.plain_result("你当前没有绑定 Xiaoning-API 账号。")

        user["xiao_user_id"] = ""
        user["xiao_bound_at"] = 0
        self.store.mark_dirty()
        self.store.save_now()
        return event.plain_result(
            f"已解绑 Xiaoning 用户 ID {old_id}。\n"
            "兑换限次记录会保留，重新绑定后继续累计。",
        )

    def _parse_exchange_request(
        self,
        command: str,
        rest: str,
    ) -> tuple[str, int]:
        kind: str | None = None
        if command in {"兑换余额", "兑换额度"}:
            kind = "balance"
        elif command in {"兑换重置卡", "兑换卡"}:
            kind = "reset_card"

        tokens = rest.strip().split()
        index = 0
        if tokens and tokens[0] in {"余额", "额度"}:
            if kind is not None and kind != "balance":
                raise ValueError("兑换类型冲突，请重新输入。")
            kind = "balance"
            index += 1
        elif tokens and tokens[0] in {"重置卡", "卡", "重置"}:
            if kind is not None and kind != "reset_card":
                raise ValueError("兑换类型冲突，请重新输入。")
            kind = "reset_card"
            index += 1

        amount: int | None = None
        if index < len(tokens):
            match = re.fullmatch(r"(\d+)(?:张|个|额度)?", tokens[index])
            if not match:
                raise ValueError("兑换数量必须是正整数。")
            amount = int(match.group(1))
            index += 1
            if index < len(tokens) and tokens[index] in {"额度", "张", "个"}:
                index += 1

        if index < len(tokens):
            raise ValueError(
                "用法：兑换余额 <数量> 或 兑换重置卡 <数量>。",
            )
        if amount is None:
            amount = 1
        if amount <= 0:
            raise ValueError("兑换数量必须大于 0。")

        if kind is None:
            enabled: list[str] = []
            if self._allow_balance():
                enabled.append("balance")
            if self._allow_reset_card():
                enabled.append("reset_card")
            if not enabled:
                raise ValueError("管理员尚未开启任何兑换类型。")
            if len(enabled) > 1:
                raise ValueError(
                    "管理员同时开启了余额和重置卡兑换，请指定类型。\n"
                    "例如：兑换余额 1 / 兑换重置卡 1",
                )
            kind = enabled[0]

        return kind, amount

    def _handle_exchange_status(self, event: AstrMessageEvent) -> Any:
        user_id = str(event.get_sender_id())
        user = self._user(user_id, event.get_sender_name())
        bound_id = str(user.get("xiao_user_id") or "")
        fixed_id = self._fixed_xiao_user_id()
        target_id = str(fixed_id) if fixed_id > 0 else bound_id
        enabled = self._exchange_enabled()
        usage = self._exchange_usage(user)

        lines = [
            "积分兑换",
            f"状态：{'已开启' if enabled else '未开启'}",
            f"模式：{self._exchange_mode_text()}",
        ]
        if fixed_id > 0:
            lines.append(f"兑换账号：用户 ID {fixed_id}（管理员统一设置）")
        else:
            lines.append(f"绑定账号：{bound_id or '未绑定'}")
        lines.append(f"积分：{_as_int(user.get('points'))}")

        if self._allow_balance():
            lines.append(
                f"余额：{self._points_per_balance()} 积分 = 1 额度",
            )
            if enabled:
                lines.append(
                    "今日余额："
                    f"{self._limit_progress(usage['daily_balance'], self._daily_balance_limit(), '额度')}"
                    " | 本月："
                    f"{self._limit_progress(usage['monthly_balance'], self._monthly_balance_limit(), '额度')}",
                )
        if self._allow_reset_card():
            lines.append(
                f"重置卡：{self._points_per_reset_card()} 积分 = 1 张",
            )
            if enabled:
                lines.append(
                    "今日重置卡："
                    f"{self._limit_progress(usage['daily_cards'], self._daily_reset_card_limit(), '张')}"
                    " | 本月："
                    f"{self._limit_progress(usage['monthly_cards'], self._monthly_reset_card_limit(), '张')}",
                )

        lines.append(
            f"累计兑换：{_as_int(user.get('exchange_total_balance'))} 额度 | "
            f"{_as_int(user.get('exchange_total_cards'))} 张重置卡",
        )

        if not enabled:
            lines.append(
                "兑换功能未开启，请联系管理员在 AstrBot 插件配置中开启。",
            )
        elif not target_id:
            lines.append(
                "请先发送“绑定账号 <你的 Xiaoning 用户ID>”，"
                "用户ID可在 Xiaoning-API 用户管理页查看。",
            )
        else:
            examples = []
            if self._allow_balance():
                examples.append("兑换余额 1")
            if self._allow_reset_card():
                examples.append("兑换重置卡 1")
            lines.append("兑换指令：" + " / ".join(examples))

        return event.plain_result("\n".join(lines))

    async def _handle_exchange(
        self,
        event: AstrMessageEvent,
        command: str,
        rest: str,
    ) -> Any:
        if command == "兑换" and not rest.strip():
            return self._handle_exchange_status(event)
        if command == "兑换" and rest.strip() in {"查询", "状态"}:
            return self._handle_exchange_status(event)

        if not self._exchange_enabled():
            return event.plain_result(
                "积分兑换功能未开启，请联系管理员在 AstrBot 插件配置中开启。",
            )

        try:
            kind, amount = self._parse_exchange_request(command, rest)
        except ValueError as exc:
            return event.plain_result(str(exc))

        if kind == "balance" and not self._allow_balance():
            return event.plain_result("管理员未开启余额兑换。")
        if kind == "reset_card" and not self._allow_reset_card():
            return event.plain_result("管理员未开启重置卡兑换。")

        if kind == "balance" and amount > XIAONING_MAX_BALANCE_PER_EXCHANGE:
            return event.plain_result(
                f"单次最多兑换 {XIAONING_MAX_BALANCE_PER_EXCHANGE} 额度。",
            )
        if kind == "reset_card" and amount > XIAONING_MAX_CARDS_PER_EXCHANGE:
            return event.plain_result(
                f"单次最多兑换 {XIAONING_MAX_CARDS_PER_EXCHANGE} 张重置卡。",
            )

        base_url = self._xiaoning_base_url()
        token = self._xiaoning_token()
        if not base_url:
            return event.plain_result(
                "管理员尚未正确配置 Xiaoning-API 的 Base URL，请联系管理员。",
            )
        if not token:
            return event.plain_result(
                "管理员尚未配置 Xiaoning-API 访问令牌，请联系管理员。",
            )

        user_id = str(event.get_sender_id())
        user = self._user(user_id, event.get_sender_name())
        fixed_id = self._fixed_xiao_user_id()
        bound_id = str(user.get("xiao_user_id") or "")
        if fixed_id > 0:
            target_user_id = fixed_id
        else:
            if not bound_id:
                return event.plain_result(
                    "你还没有绑定 Xiaoning-API 账号。\n"
                    "请先发送“绑定账号 <你的用户ID>”，"
                    "用户ID可在 Xiaoning-API 用户管理页查看。",
                )
            if not bound_id.isdigit() or int(bound_id) <= 0:
                return event.plain_result(
                    "你绑定的 Xiaoning 用户ID无效，请重新发送“绑定账号 <用户ID>”。",
                )
            target_user_id = int(bound_id)
        rate = (
            self._points_per_balance()
            if kind == "balance"
            else self._points_per_reset_card()
        )
        cost = rate * amount
        unit = "额度" if kind == "balance" else "张"
        type_text = "余额" if kind == "balance" else "重置卡"

        quota_per_unit = 0
        if kind == "balance":
            try:
                quota_per_unit = await self._xiao_quota_per_unit(base_url)
            except XiaoNingError as exc:
                return event.plain_result(f"兑换失败：{exc}")

        daily_key = ""
        month_key = ""
        async with self._lock:
            user = self._user(user_id, event.get_sender_name())
            usage = self._exchange_usage(user)
            points = _as_int(user.get("points"))
            if points < cost:
                return event.plain_result(
                    f"兑换 {amount} {unit}需要 {cost} 积分，"
                    f"你当前只有 {points} 积分。",
                )

            limit_error = self._check_exchange_limits(kind, amount, usage)
            if limit_error:
                return event.plain_result(limit_error)

            current_fixed = self._fixed_xiao_user_id()
            if current_fixed > 0:
                target_user_id = current_fixed
            else:
                current_bound = str(user.get("xiao_user_id") or "")
                if (
                    not current_bound
                    or not current_bound.isdigit()
                    or int(current_bound) <= 0
                ):
                    return event.plain_result(
                        "你的 Xiaoning 账号绑定已失效，请重新发送"
                        "“绑定账号 <用户ID>”。",
                    )
                target_user_id = int(current_bound)

            user["points"] = points - cost
            if kind == "balance":
                user["exchange_daily_balance"] = (
                    _as_int(user.get("exchange_daily_balance")) + amount
                )
                user["exchange_monthly_balance"] = (
                    _as_int(user.get("exchange_monthly_balance")) + amount
                )
            else:
                user["exchange_daily_cards"] = (
                    _as_int(user.get("exchange_daily_cards")) + amount
                )
                user["exchange_monthly_cards"] = (
                    _as_int(user.get("exchange_monthly_cards")) + amount
                )
            daily_key = str(user.get("exchange_daily_date") or "")
            month_key = str(user.get("exchange_month") or "")
            self.store.mark_dirty()

        # 先落盘预扣，避免进程在请求期间退出造成重复兑换。
        self.store.save_now()

        try:
            if kind == "balance":
                raw_quota = amount * quota_per_unit
                if raw_quota <= 0:
                    raise XiaoNingError("额度换算结果无效，请联系管理员检查配置")
                await self._xiao_add_balance(
                    base_url,
                    token,
                    target_user_id,
                    raw_quota,
                )
            else:
                await self._xiao_grant_reset_cards(
                    base_url,
                    token,
                    target_user_id,
                    amount,
                )
        except XiaoNingError as exc:
            await self._refund_exchange(
                user_id,
                event.get_sender_name(),
                kind,
                amount,
                cost,
                daily_key,
                month_key,
            )
            logger.warning(
                "Xiaoning 兑换失败 QQ=%s target=%s kind=%s amount=%s "
                "status=%s code=%s: %s",
                user_id,
                target_user_id,
                kind,
                amount,
                exc.status,
                exc.code,
                exc,
            )
            return event.plain_result(
                f"兑换失败：{self._friendly_xiao_error(exc, kind)}\n"
                "本次消耗的积分已退回。",
            )
        except Exception:
            await self._refund_exchange(
                user_id,
                event.get_sender_name(),
                kind,
                amount,
                cost,
                daily_key,
                month_key,
            )
            logger.error(
                "Xiaoning 兑换异常 QQ=%s target=%s kind=%s amount=%s",
                user_id,
                target_user_id,
                kind,
                amount,
                exc_info=True,
            )
            return event.plain_result(
                "兑换失败：插件请求 Xiaoning-API 时发生异常。\n"
                "本次消耗的积分已退回。",
            )

        async with self._lock:
            user = self._user(user_id, event.get_sender_name())
            if kind == "balance":
                user["exchange_total_balance"] = (
                    _as_int(user.get("exchange_total_balance")) + amount
                )
            else:
                user["exchange_total_cards"] = (
                    _as_int(user.get("exchange_total_cards")) + amount
                )
            usage = self._exchange_usage(user)
            remaining_points = _as_int(user.get("points"))
            self.store.mark_dirty()

        self.store.save_now()
        if kind == "balance":
            limit_text = (
                "今日余额："
                f"{self._limit_progress(usage['daily_balance'], self._daily_balance_limit(), '额度')}"
                " | 本月："
                f"{self._limit_progress(usage['monthly_balance'], self._monthly_balance_limit(), '额度')}"
            )
        else:
            limit_text = (
                "今日重置卡："
                f"{self._limit_progress(usage['daily_cards'], self._daily_reset_card_limit(), '张')}"
                " | 本月："
                f"{self._limit_progress(usage['monthly_cards'], self._monthly_reset_card_limit(), '张')}"
            )

        return event.plain_result(
            "兑换成功。\n"
            f"类型：{type_text}\n"
            f"数量：{amount} {unit}\n"
            f"目标账号：Xiaoning 用户 ID {target_user_id}\n"
            f"消耗：{cost} 积分（剩余 {remaining_points}）\n"
            f"{limit_text}",
        )

    def _check_exchange_limits(
        self,
        kind: str,
        amount: int,
        usage: dict[str, int | str],
    ) -> str | None:
        if kind == "balance":
            daily_limit = self._daily_balance_limit()
            monthly_limit = self._monthly_balance_limit()
            daily_used = _as_int(usage.get("daily_balance"))
            monthly_used = _as_int(usage.get("monthly_balance"))
            unit = "额度"
            daily_remaining = max(0, daily_limit - daily_used) if daily_limit > 0 else None
            monthly_remaining = (
                max(0, monthly_limit - monthly_used) if monthly_limit > 0 else None
            )
        else:
            daily_limit = self._daily_reset_card_limit()
            monthly_limit = self._monthly_reset_card_limit()
            daily_used = _as_int(usage.get("daily_cards"))
            monthly_used = _as_int(usage.get("monthly_cards"))
            unit = "张"
            daily_remaining = max(0, daily_limit - daily_used) if daily_limit > 0 else None
            monthly_remaining = (
                max(0, monthly_limit - monthly_used) if monthly_limit > 0 else None
            )

        if daily_remaining is not None and amount > daily_remaining:
            return (
                f"今日兑换上限为 {daily_limit} {unit}，已兑换 {daily_used}，"
                f"最多还能兑换 {daily_remaining} {unit}。"
            )
        if monthly_remaining is not None and amount > monthly_remaining:
            return (
                f"本月兑换上限为 {monthly_limit} {unit}，已兑换 {monthly_used}，"
                f"最多还能兑换 {monthly_remaining} {unit}。"
            )
        return None

    async def _refund_exchange(
        self,
        user_id: str,
        user_name: str,
        kind: str,
        amount: int,
        cost: int,
        daily_key: str,
        month_key: str,
    ) -> None:
        async with self._lock:
            user = self._user(user_id, user_name)
            user["points"] = _as_int(user.get("points")) + cost
            if kind == "balance":
                daily_field = "exchange_daily_balance"
                monthly_field = "exchange_monthly_balance"
            else:
                daily_field = "exchange_daily_cards"
                monthly_field = "exchange_monthly_cards"

            if user.get("exchange_daily_date") == daily_key:
                user[daily_field] = max(
                    0,
                    _as_int(user.get(daily_field)) - amount,
                )
            if user.get("exchange_month") == month_key:
                user[monthly_field] = max(
                    0,
                    _as_int(user.get(monthly_field)) - amount,
                )
            self.store.mark_dirty()
        self.store.save_now()

    # ------------------------------------------------------------------
    # 查询与帮助
    # ------------------------------------------------------------------

    def _handle_status(self, event: AstrMessageEvent) -> Any:
        user_id = str(event.get_sender_id())
        user = self._user(user_id, event.get_sender_name())
        group_id = str(event.get_group_id() or "")
        max_bricks = self._max_bricks()

        lines = [
            "砖厂档案",
            f"昵称：{_safe_name(user.get('name'), user_id)}",
            f"积分：{_as_int(user.get('points'))}",
            f"连续签到：{_as_int(user.get('streak'))} 天"
            f"（累计 {_as_int(user.get('total_signs'))} 次）",
            f"可用砖块：{_as_int(user.get('bricks'))}/{max_bricks}",
        ]

        active_total = self._active_burn_count(user_id)
        lines.append(f"正在烧的砖：{active_total} 块")

        if group_id:
            current_burns = [
                burn
                for burn in self._burns(group_id)
                if isinstance(burn, dict)
                and str(burn.get("owner_id")) == user_id
            ]
            if current_burns:
                lines.append("本群烧砖进度：")
                for index, burn in enumerate(current_burns, start=1):
                    target = _as_int(burn.get("target"), self._brick_messages())
                    progress = min(_as_int(burn.get("progress")), target)
                    lines.append(f"  第 {index} 块：{progress}/{target}")
            elif active_total > 0:
                lines.append("（正在烧的砖在其他群）")

        lines.append(
            f"累计烧成：{_as_int(user.get('total_bricks'))} 块 | "
            f"成功拍人：{_as_int(user.get('total_slaps'))} 次",
        )

        bound_id = str(user.get("xiao_user_id") or "")
        fixed_id = self._fixed_xiao_user_id()
        if self._exchange_enabled() or bound_id or fixed_id > 0:
            target = f"{fixed_id}（统一）" if fixed_id > 0 else (bound_id or "未绑定")
            lines.append(f"兑换账号：{target}")
            if self._exchange_enabled():
                lines.append("发送“兑换查询”查看余额/重置卡兑换详情。")

        if _as_int(user.get("bricks")) > 0:
            lines.append("发送“拍人 @某人”可使用一块砖。")
        else:
            lines.append("发送“烧砖”开始攒砖。")

        return event.plain_result("\n".join(lines))

    def _handle_rank(self, event: AstrMessageEvent) -> Any:
        sender_id = str(event.get_sender_id())
        entries: list[tuple[str, dict[str, Any], int]] = []
        for user_id, user in self.store.data["users"].items():
            if not isinstance(user, dict):
                continue
            points = _as_int(user.get("points"))
            if points <= 0 and _as_int(user.get("total_signs")) <= 0:
                continue
            entries.append((str(user_id), user, points))

        entries.sort(
            key=lambda item: (
                -item[2],
                -_as_int(item[1].get("total_signs")),
                _safe_name(item[1].get("name"), item[0]),
            ),
        )

        if not entries:
            return event.plain_result("还没有人签到。发送“签到”，抢占积分榜第一。")

        size = self._leaderboard_size()
        lines = [f"全服积分榜（前 {min(size, len(entries))} 名）"]
        for index, (user_id, user, points) in enumerate(entries[:size], start=1):
            lines.append(
                f"{index}. {_safe_name(user.get('name'), user_id)} - {points} 分",
            )

        my_rank = next(
            (
                index
                for index, (user_id, _, _) in enumerate(entries, start=1)
                if user_id == sender_id
            ),
            None,
        )
        me = self._user(sender_id, event.get_sender_name())
        if my_rank is None:
            lines.append(
                f"你还没有积分（{_as_int(me.get('points'))} 分），发送“签到”开始。",
            )
        else:
            lines.append(
                f"你的排名：第 {my_rank} 名，{_as_int(me.get('points'))} 分。",
            )

        return event.plain_result("\n".join(lines))

    def _handle_help(self, event: AstrMessageEvent) -> Any:
        max_bricks = self._max_bricks()
        target = self._brick_messages()
        mute_text = self._format_duration(self._mute_seconds())
        cost = self._brick_cost()

        lines = [
            "砖厂玩法",
            f"签到 / 打卡：每天 1 次，获得 {self._sign_points()} 积分，记录在服务器",
            f"烧砖：本群其他成员累计发送 {target} 条消息即可烧成一块砖；"
            "同时烧多块时，每条群友消息会同时推进它们",
            f"砖块上限：每人最多同时拥有 {max_bricks} 块（可用砖块 + 正在烧的砖）",
            f"拍人：发送“拍人 @某人”，或回复对方消息发送“拍人”；"
            f"消耗 1 块砖，禁言 {mute_text}",
            "我的砖 / 积分：查看积分、砖块和烧砖进度",
            "积分榜 / 排行榜：查看全服积分排名",
            "绑定账号：发送“绑定账号 <Xiaoning 用户ID>”，"
            "用户ID可在 Xiaoning-API 用户管理页查看；"
            "管理员也可设置统一兑换账号",
        ]
        if self._exchange_enabled():
            if self._allow_balance():
                lines.append(
                    f"兑换余额：{self._points_per_balance()} 积分 = 1 额度，"
                    "发送“兑换余额 <数量>”",
                )
            if self._allow_reset_card():
                lines.append(
                    f"兑换重置卡：{self._points_per_reset_card()} 积分 = 1 张，"
                    "发送“兑换重置卡 <数量>”",
                )
            lines.append("兑换查询：查看兑换比例、绑定账号和今日/本月剩余额度")
        else:
            lines.append("积分兑换：管理员未开启")
        lines.append("砖厂帮助：显示本说明")
        if cost > 0:
            lines.insert(3, f"烧砖消耗：{cost} 积分")
        lines.append(
            "提示：指令可直接发送，无需 @ 机器人；"
            "拍人需要机器人在群内拥有管理员权限。",
        )
        return event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 群消息计数：推进所有正在烧的砖
    # ------------------------------------------------------------------

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @_event_message_type_filter(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=-10,
    )
    async def on_group_message(self, event: AstrMessageEvent):
        """统计群友消息，推进本群所有正在烧的砖。"""
        if event.is_stopped():
            return

        group_id = str(event.get_group_id() or "")
        if not group_id:
            return

        burns = self._burns(group_id)
        if not burns:
            return

        sender_id = str(event.get_sender_id() or "")
        if not sender_id or sender_id == str(event.get_self_id() or ""):
            return
        if not self._is_countable_message(event):
            return

        completed: dict[str, dict[str, Any]] = {}
        changed = False

        for burn in tuple(burns):
            if not isinstance(burn, dict):
                burns.remove(burn)
                changed = True
                continue

            owner_id = str(burn.get("owner_id") or "")
            if not owner_id:
                burns.remove(burn)
                changed = True
                continue
            if owner_id == sender_id:
                continue

            target = _as_int(burn.get("target"), self._brick_messages())
            target = max(1, target)
            progress = _as_int(burn.get("progress")) + 1
            burn["target"] = target
            burn["progress"] = progress
            changed = True

            if progress >= target:
                burns.remove(burn)
                entry = completed.setdefault(
                    owner_id,
                    {
                        "name": _safe_name(burn.get("owner_name"), owner_id),
                        "count": 0,
                    },
                )
                entry["count"] += 1

        if not changed:
            return

        notifications: list[Any] = []
        if completed:
            max_bricks = self._max_bricks()
            for owner_id, entry in completed.items():
                user = self._user(owner_id, str(entry["name"]))
                user["bricks"] = _as_int(user.get("bricks")) + entry["count"]
                user["total_bricks"] = (
                    _as_int(user.get("total_bricks")) + entry["count"]
                )
                entry["bricks"] = _as_int(user.get("bricks"))
                entry["max_bricks"] = max_bricks
                notifications.append((owner_id, entry))

        if not any(isinstance(burn, dict) for burn in burns):
            self.store.data["groups"].pop(group_id, None)

        self.store.mark_dirty()
        if completed:
            self.store.save_now()

        if completed and self._notify_on_complete():
            for owner_id, entry in notifications:
                count = entry["count"]
                prefix = (
                    f"你的 {count} 块砖烧好了！"
                    if count > 1
                    else "你的砖烧好了！"
                )
                if entry["bricks"] >= entry["max_bricks"]:
                    next_step = "已到上限，先用“拍人 @某人”消耗一块。"
                else:
                    next_step = "发送“烧砖”可以继续烧下一块。"
                yield event.chain_result(
                    [
                        At(
                            qq=owner_id,
                            name=_safe_name(entry["name"], owner_id),
                        ),
                        Plain(
                            f" {prefix}当前可用砖块 "
                            f"{entry['bricks']}/{entry['max_bricks']}。"
                            f"{next_step}",
                        ),
                    ],
                )


