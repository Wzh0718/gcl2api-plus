"""
配置路由模块 - 处理 /config/* 相关的HTTP请求
"""

import math
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

import config
from log import log
from src.keeplive import keepalive_service
from src.models import ConfigSaveRequest
from src.storage_adapter import get_storage_adapter
from src.utils import verify_panel_token
from .utils import get_env_locked_keys
from src.capture_config import CAPTURE_FIELDS, capture_settings, validate_capture_value


# 创建路由器
router = APIRouter(prefix="/config", tags=["config"])


@router.get("/get")
async def get_config(token: str = Depends(verify_panel_token)):
    """获取当前配置"""
    try:


        # 读取当前配置（包括环境变量和TOML文件中的配置）
        current_config = {}

        # 基础配置
        current_config["code_assist_endpoint"] = await config.get_code_assist_endpoint()
        current_config["credentials_dir"] = await config.get_credentials_dir()
        current_config["proxy"] = await config.get_proxy_config() or ""

        # 代理端点配置
        current_config["oauth_proxy_url"] = await config.get_oauth_proxy_url()
        current_config["googleapis_proxy_url"] = await config.get_googleapis_proxy_url()
        current_config["resource_manager_api_url"] = await config.get_resource_manager_api_url()
        current_config["service_usage_api_url"] = await config.get_service_usage_api_url()
        current_config["antigravity_api_url"] = await config.get_antigravity_api_url()

        # 自动封禁配置
        current_config["auto_ban_enabled"] = await config.get_auto_ban_enabled()
        current_config["auto_ban_error_codes"] = await config.get_auto_ban_error_codes()

        # 429重试配置
        current_config["retry_429_max_retries"] = await config.get_retry_429_max_retries()
        current_config["retry_429_enabled"] = await config.get_retry_429_enabled()
        current_config["retry_429_interval"] = await config.get_retry_429_interval()
        # 抗截断配置
        current_config["anti_truncation_max_attempts"] = await config.get_anti_truncation_max_attempts()

        # 敏感词替换配置
        current_config["sensitive_word_replace_enabled"] = await config.get_sensitive_word_replace_enabled()
        current_config["sensitive_word_replace_rules"] = await config.get_sensitive_word_replace_rules()
        current_config["sensitive_word_filter_mode"] = await config.get_sensitive_word_filter_mode()
        current_config["sensitive_word_use_default_keywords"] = await config.get_sensitive_word_use_default_keywords()
        current_config["system_prompt_append"] = await config.get_system_prompt_append()

        # 兼容性配置
        current_config["compatibility_mode_enabled"] = await config.get_compatibility_mode_enabled()

        # 思维链返回配置
        current_config["return_thoughts_to_frontend"] = await config.get_return_thoughts_to_frontend()

        # Antigravity流式转非流式配置
        current_config["antigravity_stream2nostream"] = await config.get_antigravity_stream2nostream()
        current_config["antigravity_switch_credential_enabled"] = await config.get_antigravity_switch_credential_enabled()

        # Antigravity hub 轨迹指纹（UA 版本/CL，2026-10-21 起）
        current_config["antigravity_hub_version"] = await config.get_antigravity_hub_version()
        current_config["antigravity_hub_cl"] = await config.get_antigravity_hub_cl()

        # Antigravity 请求节奏与 429 治理（2026-10-21 起）
        current_config["antigravity_pacing_enabled"] = await config.get_antigravity_pacing_enabled()
        current_config["antigravity_pacing_base_ms"] = await config.get_antigravity_pacing_base_ms()
        current_config["antigravity_pacing_jitter_pct"] = await config.get_antigravity_pacing_jitter_pct()
        current_config["antigravity_pacing_concurrency"] = await config.get_antigravity_pacing_concurrency()
        current_config["antigravity_pacing_slot_wait_seconds"] = await config.get_antigravity_pacing_slot_wait_seconds()
        current_config["antigravity_quota_guard_enabled"] = await config.get_antigravity_quota_guard_enabled()
        current_config["antigravity_quota_guard_threshold"] = await config.get_antigravity_quota_guard_threshold()

        # Antigravity 403封禁账号定期复检配置
        current_config["antigravity_403_recheck_enabled"] = await config.get_antigravity_403_recheck_enabled()
        current_config["antigravity_403_recheck_interval"] = await config.get_antigravity_403_recheck_interval()

        # Antigravity 模型配额降级链
        current_config["antigravity_model_fallback_chain"] = ",".join(
            await config.get_antigravity_model_fallback_chain()
        )

        # 保活配置
        current_config["keepalive_url"] = await config.get_keepalive_url()
        current_config["keepalive_interval"] = await config.get_keepalive_interval()

        # 钉钉 Webhook 频率告警配置（SQLite 优先于 .env）
        current_config["dingtalk_webhook_url"] = await config.get_dingtalk_webhook_url()
        current_config["dingtalk_alert_status_codes"] = await config.get_dingtalk_alert_status_codes()
        current_config["dingtalk_alert_window_minutes"] = await config.get_dingtalk_alert_window_minutes()
        current_config["dingtalk_alert_threshold"] = await config.get_dingtalk_alert_threshold()
        current_config["dingtalk_alert_cooldown_minutes"] = await config.get_dingtalk_alert_cooldown_minutes()
        current_config["dingtalk_alert_template"] = await config.get_dingtalk_alert_template()
        current_config["dingtalk_all_accounts_alert_enabled"] = await config.get_dingtalk_all_accounts_alert_enabled()
        current_config["dingtalk_alert_enabled"] = await config.get_dingtalk_alert_enabled()
        current_config["dingtalk_all_accounts_alert_cooldown_minutes"] = await config.get_dingtalk_all_accounts_alert_cooldown_minutes()
        current_config["dingtalk_all_accounts_alert_template"] = await config.get_dingtalk_all_accounts_alert_template()

        # 调用监控配置
        current_config["call_records_retention_days"] = await config.get_call_records_retention_days()

        # 服务器配置
        current_config["host"] = await config.get_server_host()
        current_config["port"] = await config.get_server_port()
        current_config["api_password"] = await config.get_api_password()
        current_config["panel_password"] = await config.get_panel_password()
        current_config["password"] = await config.get_server_password()

        # 从存储系统读取配置
        storage_adapter = await get_storage_adapter()
        storage_config = await storage_adapter.get_all_config()

        # 获取环境变量锁定的配置键
        env_locked_keys = get_env_locked_keys()

        # 合并存储系统配置（不覆盖环境变量）
        for key, value in storage_config.items():
            if key not in env_locked_keys:
                current_config[key] = value

        current_config.update(capture_settings())
        webhook_source = (
            "sqlite" if storage_config.get("dingtalk_webhook_url") is not None else
            "environment" if os.getenv("DINGTALK_WEBHOOK_URL", "").strip() else "unset"
        )
        return JSONResponse(content={"config": current_config, "env_locked": list(env_locked_keys),
                                     "webhook_source": webhook_source, "capture_schema": CAPTURE_FIELDS})

    except Exception as e:
        log.error(f"获取配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/save")
async def save_config(request: ConfigSaveRequest, token: str = Depends(verify_panel_token)):
    """保存配置"""
    try:

        new_config = request.config

        for key in new_config:
            if key.startswith("request_capture_"):
                if key not in CAPTURE_FIELDS:
                    raise HTTPException(400, f"未知采集配置: {key}")
                try:
                    new_config[key] = validate_capture_value(key, new_config[key])
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc

        log.debug(f"收到的配置数据: {list(new_config.keys())}")
        log.debug(f"收到的password值: {new_config.get('password', 'NOT_FOUND')}")

        # 验证配置项
        if "retry_429_max_retries" in new_config:
            if (
                not isinstance(new_config["retry_429_max_retries"], int)
                or new_config["retry_429_max_retries"] < 0
            ):
                raise HTTPException(status_code=400, detail="最大429重试次数必须是大于等于0的整数")

        if "retry_429_enabled" in new_config:
            if not isinstance(new_config["retry_429_enabled"], bool):
                raise HTTPException(status_code=400, detail="429重试开关必须是布尔值")

        # 验证新的配置项
        if "retry_429_interval" in new_config:
            try:
                interval = float(new_config["retry_429_interval"])
                if interval < 0.01 or interval > 10:
                    raise HTTPException(status_code=400, detail="429重试间隔必须在0.01-10秒之间")
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="429重试间隔必须是有效的数字")

        if "anti_truncation_max_attempts" in new_config:
            if (
                not isinstance(new_config["anti_truncation_max_attempts"], int)
                or new_config["anti_truncation_max_attempts"] < 1
                or new_config["anti_truncation_max_attempts"] > 10
            ):
                raise HTTPException(
                    status_code=400, detail="抗截断最大重试次数必须是1-10之间的整数"
                )

        if "compatibility_mode_enabled" in new_config:
            if not isinstance(new_config["compatibility_mode_enabled"], bool):
                raise HTTPException(status_code=400, detail="兼容性模式开关必须是布尔值")

        if "sensitive_word_replace_enabled" in new_config:
            if not isinstance(new_config["sensitive_word_replace_enabled"], bool):
                raise HTTPException(status_code=400, detail="敏感词替换开关必须是布尔值")

        if "sensitive_word_filter_mode" in new_config:
            if new_config["sensitive_word_filter_mode"] not in ("rewrite", "block"):
                raise HTTPException(status_code=400, detail="过滤模式必须是 rewrite 或 block")
        if "sensitive_word_use_default_keywords" in new_config:
            if not isinstance(new_config["sensitive_word_use_default_keywords"], bool):
                raise HTTPException(status_code=400, detail="内置词库开关必须是布尔值")

        if "sensitive_word_replace_rules" in new_config:
            rules_value = new_config["sensitive_word_replace_rules"]
            if not isinstance(rules_value, str):
                raise HTTPException(status_code=400, detail="敏感词替换规则必须是字符串")
            if len(rules_value) > 20000:
                raise HTTPException(status_code=400, detail="敏感词替换规则过长（最多 20000 字符）")

        if "system_prompt_append" in new_config:
            value = new_config["system_prompt_append"]
            if not isinstance(value, str) or len(value) > 20000:
                raise HTTPException(status_code=400, detail="自定义系统提示必须是字符串，最多 20000 字符")

        if "return_thoughts_to_frontend" in new_config:
            if not isinstance(new_config["return_thoughts_to_frontend"], bool):
                raise HTTPException(status_code=400, detail="思维链返回开关必须是布尔值")

        if "antigravity_stream2nostream" in new_config:
            if not isinstance(new_config["antigravity_stream2nostream"], bool):
                raise HTTPException(status_code=400, detail="Antigravity流式转非流式开关必须是布尔值")

        if "antigravity_switch_credential_enabled" in new_config:
            if not isinstance(new_config["antigravity_switch_credential_enabled"], bool):
                raise HTTPException(status_code=400, detail="Antigravity切换凭证开关必须是布尔值")

        for hub_key, hub_label in (
            ("antigravity_hub_version", "Antigravity hub 版本号"),
            ("antigravity_hub_cl", "Antigravity hub CL"),
        ):
            if hub_key in new_config:
                hub_value = new_config[hub_key]
                if not isinstance(hub_value, str) or not hub_value.strip():
                    raise HTTPException(status_code=400, detail=f"{hub_label}必须是非空字符串")
                if len(hub_value.strip()) > 64:
                    raise HTTPException(status_code=400, detail=f"{hub_label}过长（最多 64 字符）")
                new_config[hub_key] = hub_value.strip()

        for pacing_flag, flag_label in (
            ("antigravity_pacing_enabled", "Antigravity请求节奏开关"),
            ("antigravity_quota_guard_enabled", "Antigravity额度避让开关"),
        ):
            if pacing_flag in new_config:
                if not isinstance(new_config[pacing_flag], bool):
                    raise HTTPException(status_code=400, detail=f"{flag_label}必须是布尔值")

        if "antigravity_pacing_base_ms" in new_config:
            try:
                base_ms = int(new_config["antigravity_pacing_base_ms"])
                if base_ms < 0 or base_ms > 600000:
                    raise ValueError
                new_config["antigravity_pacing_base_ms"] = base_ms
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="请求间隔基准必须是0-600000之间的整数毫秒")

        if "antigravity_pacing_jitter_pct" in new_config:
            try:
                jitter_pct = int(new_config["antigravity_pacing_jitter_pct"])
                if jitter_pct < 0 or jitter_pct > 95:
                    raise ValueError
                new_config["antigravity_pacing_jitter_pct"] = jitter_pct
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="抖动幅度必须是0-95之间的整数百分比")

        if "antigravity_pacing_concurrency" in new_config:
            try:
                concurrency = int(new_config["antigravity_pacing_concurrency"])
                if concurrency < 1 or concurrency > 64:
                    raise ValueError
                new_config["antigravity_pacing_concurrency"] = concurrency
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="每账号并发上限必须是1-64之间的整数")

        if "antigravity_pacing_slot_wait_seconds" in new_config:
            try:
                wait_seconds = float(new_config["antigravity_pacing_slot_wait_seconds"])
                if not math.isfinite(wait_seconds) or wait_seconds < 0:
                    raise ValueError
                new_config["antigravity_pacing_slot_wait_seconds"] = wait_seconds
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="并发排队放行等待必须是大于等于0的有限秒数")

        if "antigravity_quota_guard_threshold" in new_config:
            try:
                threshold = float(new_config["antigravity_quota_guard_threshold"])
                if threshold < 0 or threshold > 0.5:
                    raise ValueError
                new_config["antigravity_quota_guard_threshold"] = threshold
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="额度避让阈值必须是0-0.5之间的数值")

        if "antigravity_403_recheck_enabled" in new_config:
            if not isinstance(new_config["antigravity_403_recheck_enabled"], bool):
                raise HTTPException(status_code=400, detail="403定期复检开关必须是布尔值")

        if "antigravity_403_recheck_interval" in new_config:
            try:
                interval = int(new_config["antigravity_403_recheck_interval"])
                if interval < 60 or interval > 86400:
                    raise HTTPException(status_code=400, detail="403复检间隔必须在 60-86400 秒之间")
                new_config["antigravity_403_recheck_interval"] = interval
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="403复检间隔必须是有效整数")

        if "antigravity_model_fallback_chain" in new_config:
            chain_value = new_config["antigravity_model_fallback_chain"]
            if not isinstance(chain_value, str):
                raise HTTPException(status_code=400, detail="模型配额降级链必须是逗号分隔的字符串")
            # 规范化：去空白、去空项、去重；空串表示关闭降级
            chain_items = []
            for item in chain_value.split(","):
                item = item.strip()
                if item and item not in chain_items:
                    chain_items.append(item)
            new_config["antigravity_model_fallback_chain"] = ",".join(chain_items)

        # 验证保活配置
        if "keepalive_url" in new_config:
            if not isinstance(new_config["keepalive_url"], str):
                raise HTTPException(status_code=400, detail="保活URL必须是字符串")

        # 验证调用监控配置
        if "call_records_retention_days" in new_config:
            try:
                days = int(new_config["call_records_retention_days"])
                if days < 1 or days > 365:
                    raise HTTPException(status_code=400, detail="调用记录保留天数必须在 1-365 之间")
                new_config["call_records_retention_days"] = days
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="调用记录保留天数必须是有效整数")

        if "keepalive_interval" in new_config:
            try:
                interval = int(new_config["keepalive_interval"])
                if interval < 5 or interval > 86400:
                    raise HTTPException(status_code=400, detail="保活间隔必须在 5-86400 秒之间")
                new_config["keepalive_interval"] = interval
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="保活间隔必须是有效整数")

        if "dingtalk_webhook_url" in new_config and new_config["dingtalk_webhook_url"] is not None and not isinstance(new_config["dingtalk_webhook_url"], str):
            raise HTTPException(status_code=400, detail="钉钉 Webhook 必须是字符串")
        if "dingtalk_webhook_url" in new_config and new_config["dingtalk_webhook_url"] is not None:
            from src.dingtalk_alert import valid_dingtalk_webhook
            webhook = new_config["dingtalk_webhook_url"].strip()
            if webhook and not valid_dingtalk_webhook(webhook):
                raise HTTPException(status_code=400, detail="Webhook 必须是 HTTPS 钉钉机器人地址")
            new_config["dingtalk_webhook_url"] = webhook
        if "dingtalk_alert_status_codes" in new_config:
            codes = new_config["dingtalk_alert_status_codes"]
            if isinstance(codes, str):
                codes = [x.strip() for x in codes.split(",") if x.strip()]
            if not isinstance(codes, list):
                raise HTTPException(status_code=400, detail="告警返回码必须是逗号分隔的数字")
            try:
                if any(isinstance(x, bool) or not str(x).isdigit() for x in codes):
                    raise ValueError
                codes = sorted({int(x) for x in codes})
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="告警返回码必须是数字")
            if any(code < 100 or code > 599 for code in codes):
                raise HTTPException(status_code=400, detail="告警返回码必须在 100-599 之间")
            new_config["dingtalk_alert_status_codes"] = codes
        for key, label, minimum, maximum in (
            ("dingtalk_alert_window_minutes", "告警时间窗口", 1, 1440),
            ("dingtalk_alert_threshold", "告警次数阈值", 1, 1000),
            ("dingtalk_alert_cooldown_minutes", "告警冷却时间", 0, 1440),
            ("dingtalk_all_accounts_alert_cooldown_minutes", "账号不可用告警冷却时间", 0, 1440),
        ):
            if key in new_config:
                try:
                    if isinstance(new_config[key], bool) or not str(new_config[key]).isdigit():
                        raise ValueError
                    value = int(new_config[key])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"{label}必须是整数")
                if value < minimum or value > maximum:
                    raise HTTPException(status_code=400, detail=f"{label}必须在 {minimum}-{maximum} 之间")
                new_config[key] = value
        from src.dingtalk_alert import validate_alert_template, STATUS_TEMPLATE_FIELDS, ACCOUNT_TEMPLATE_FIELDS
        for key, fields in (("dingtalk_alert_template", STATUS_TEMPLATE_FIELDS),
                            ("dingtalk_all_accounts_alert_template", ACCOUNT_TEMPLATE_FIELDS)):
            if key in new_config:
                template = new_config[key]
                if not isinstance(template, str) or len(template) > 5000:
                    raise HTTPException(status_code=400, detail="告警模板必须是 5000 字符以内的字符串")
                try:
                    validate_alert_template(template, fields)
                except ValueError:
                    raise HTTPException(status_code=400, detail="告警模板包含未知变量或无效大括号，请使用页面列出的变量")
        for key in ("dingtalk_all_accounts_alert_enabled", "dingtalk_alert_enabled"):
            if key in new_config and not isinstance(new_config[key], bool):
                raise HTTPException(status_code=400, detail="告警开关必须是布尔值")
        # 验证服务器配置
        if "host" in new_config:
            if not isinstance(new_config["host"], str) or not new_config["host"].strip():
                raise HTTPException(status_code=400, detail="服务器主机地址不能为空")

        if "port" in new_config:
            if (
                not isinstance(new_config["port"], int)
                or new_config["port"] < 1
                or new_config["port"] > 65535
            ):
                raise HTTPException(status_code=400, detail="端口号必须是1-65535之间的整数")

        if "api_password" in new_config:
            if not isinstance(new_config["api_password"], str):
                raise HTTPException(status_code=400, detail="API访问密码必须是字符串")

        if "panel_password" in new_config:
            if not isinstance(new_config["panel_password"], str):
                raise HTTPException(status_code=400, detail="控制面板密码必须是字符串")

        if "password" in new_config:
            if not isinstance(new_config["password"], str):
                raise HTTPException(status_code=400, detail="访问密码必须是字符串")

        # 获取环境变量锁定的配置键
        env_locked_keys = get_env_locked_keys()

        # 直接使用存储适配器保存配置
        storage_adapter = await get_storage_adapter()
        for key, value in new_config.items():
            if key not in env_locked_keys:
                if key == "dingtalk_webhook_url" and value is None:
                    saved = await storage_adapter.delete_config(key)
                else:
                    saved = await storage_adapter.set_config(key, value)
                if not saved:
                    raise HTTPException(status_code=500, detail="配置写入 SQLite 失败，请重试")
                if key in ("password", "api_password", "panel_password"):
                    log.debug(f"设置{key}字段为: {value}")

        # 重新加载配置缓存（关键！）
        await config.reload_config()

        if set(new_config) & CAPTURE_FIELDS.keys():
            from src.request_capture import maintain_capture
            await maintain_capture()

        # 如果保活相关配置发生变化，立即重启保活服务
        keepalive_keys = {"keepalive_url", "keepalive_interval"}
        if keepalive_keys & set(new_config.keys()):
            try:
                await keepalive_service.restart()
            except Exception as e:
                log.warning(f"重启保活服务失败: {e}")

        # 如果403复检配置发生变化，立即重启复检服务
        recheck_keys = {"antigravity_403_recheck_enabled", "antigravity_403_recheck_interval"}
        if recheck_keys & set(new_config.keys()):
            try:
                from src.antigravity_403_recheck import recheck_service
                await recheck_service.restart()
            except Exception as e:
                log.warning(f"重启403复检服务失败: {e}")

        # 调用记录保留天数变化后，立即按新保留期清理一次
        if "call_records_retention_days" in new_config:
            try:
                from src.call_monitor import get_call_monitor
                await (await get_call_monitor(storage_adapter)).cleanup()
            except Exception as e:
                log.warning(f"按新保留期清理调用记录失败: {e}")

        # 验证保存后的结果
        test_api_password = await config.get_api_password()
        test_panel_password = await config.get_panel_password()
        test_password = await config.get_server_password()
        log.debug(f"保存后立即读取的API密码: {test_api_password}")
        log.debug(f"保存后立即读取的面板密码: {test_panel_password}")
        log.debug(f"保存后立即读取的通用密码: {test_password}")

        # 构建响应消息
        response_data = {
            "message": "配置保存成功",
            "saved_config": {k: v for k, v in new_config.items() if k not in env_locked_keys},
        }

        return JSONResponse(content=response_data)

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"保存配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
