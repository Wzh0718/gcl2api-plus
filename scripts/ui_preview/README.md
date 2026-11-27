# 本地界面预览

在仓库根目录执行：

```bash
python3 scripts/ui_preview/server.py --port 18761
```

- 桌面：<http://127.0.0.1:18761/>
- 移动端：<http://127.0.0.1:18761/mobile>
- 直达模块示例：<http://127.0.0.1:18761/?tab=model-stats>

这是仅监听本机的前端设计预览。页面使用真实 HTML、样式与公共交互脚本；服务额外注入 `preview.js`，以浏览器内的演示数据替代后端请求。不会启动网关、读取生产配置或访问生产凭证/数据库。演示更改刷新后恢复。不支持的服务操作会明确提示需要连接实际服务。

可体验：模块导航、配置分类、密钥检索/创建/修改/启停、批量设置面板、统计筛选、监控视图和设置弹窗。完整密钥检索示例：`gcli_00000000_preview-only`。日志为固定演示内容，授权和实际凭证导入不执行。

配色与全局组件样式在 `front/studio.css`，覆盖冷白、银灰底色与蓝色常规操作；正红用于告警和危险操作。桌面和移动模板共用；既有业务 ID、回调与表单字段保留。`header.css`、`api_keys.css` 继续负责已确认的头部与紧凑密钥布局。

关闭运行该命令的进程即可停止预览。正常网关页面不会加载预览脚本。

## 前端交互回归

先启动上述本地预览，再运行（需要 Playwright 与 Chromium）：

```bash
node scripts/ui_preview/checks/dialog-failures.cjs
node scripts/ui_preview/checks/monitor-regression.cjs
node scripts/ui_preview/checks/billing-period.cjs
```

Playwright 未安装到项目时，可用 `PLAYWRIGHT_MODULE` 指向外部安装的模块目录；`CHROMIUM_PATH` 可指定现有 Chromium 可执行文件。检查默认使用本机 18761 端口的演示环境；可用 `PREVIEW_URL` 指定另一端口的本地预览。弹窗用例模拟服务失败，覆盖创建/管理/批量设置的保存中禁用、失败重试、焦点恢复，以及保存后进入监控的连续操作；监控覆盖三种视图；计费覆盖周期切换和过期响应。

这些检查不替代真实服务集成验收：OAuth 授权、凭证上传到服务端、代理连接、WebSocket 实时日志、真实账单和证据包导出仍需真实测试环境。
