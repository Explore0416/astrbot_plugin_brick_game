# AstrBot 砖厂游戏插件

适用于 NapCat / OneBot v11（AstrBot `aiocqhttp` 适配器）的 QQ 群小游戏插件。

当前版本：**1.2.0**

## 功能

- **签到 / 打卡**：每人每天一次，获得积分并持久化到服务器。
- **烧砖**：开始烧砖后，本群其他成员累计发送指定条数消息即可烧成一块砖。
- **砖块上限**：可用砖块与正在烧的砖块合计不超过配置上限，默认 3 块。
- **拍人**：消耗 1 块砖，将指定群友禁言一段时间。
- **积分兑换**：通过 Xiaoning-API 管理员访问令牌，把积分兑换为余额或重置卡。
- **兑换限次**：可分别设置每日/每月、余额/重置卡的每人限额。
- **优先于 AI**：指令命中后停止事件传播并关闭默认 LLM 请求。

## 安装

### 方式一：AstrBot WebUI 上传 zip

在插件管理页选择上传插件压缩包，上传 `astrbot_plugin_brick_game.zip`，完成后重载插件。

### 方式二：手动安装

将 zip 解压到：

```text
AstrBot/data/plugins/astrbot_plugin_brick_game/
```

解压后，以下文件必须**直接**位于该目录，不能再多套一层文件夹：

```text
data/plugins/astrbot_plugin_brick_game/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── README.md
└── pages/
    └── settings/
        ├── index.html
        ├── app.js
        └── style.css
```

然后重启 AstrBot 或重载插件。

## 设置面板

AstrBot 只有在**插件成功加载**且插件根目录存在 `_conf_schema.json` 时，才会在插件卡片上显示设置齿轮。

本插件同时提供两种设置入口：

1. 插件卡片的设置齿轮（`_conf_schema.json`）：AstrBot 原生配置面板。
2. 插件卡片进入详情页后的页面入口（`pages/settings`）：本插件提供的可视化设置页，可一次编辑全部参数。

如果安装后没有设置入口，请检查：

1. 插件目录是否为 `data/plugins/astrbot_plugin_brick_game/`；
2. `main.py`、`metadata.yaml`、`_conf_schema.json` 是否直接在该目录；
3. `pages/settings/index.html` 是否存在；
4. AstrBot 日志中是否有 `Failed to import plugin` 或 `_conf_schema.json is not valid JSON`；
5. 插件是否被禁用，重载后是否显示“已启用”。

## 指令

| 指令 | 说明 |
| --- | --- |
| `签到` / `打卡` | 每天一次，获得积分 |
| `烧砖` | 开始烧砖，等待群友消息推进 |
| `拍人 @某人` | 消耗 1 块砖禁言对方 |
| 回复消息后发送 `拍人` | 禁言被回复的人 |
| `我的砖` / `砖厂` / `积分` | 查看积分、砖块、进度 |
| `积分榜` / `排行榜` | 查看全服积分排名 |
| `绑定账号 <用户ID>` | 绑定 Xiaoning-API 用户 ID |
| `解绑` | 解除绑定 |
| `兑换查询` / `兑换` | 查看兑换比例、绑定账号与剩余限次 |
| `兑换余额 <数量>` | 兑换余额 |
| `兑换重置卡 <数量>` | 兑换重置卡 |
| `砖厂帮助` | 查看玩法说明 |

指令可直接发送，无需 @ 机器人。

## 控制面板配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `sign_points` | 10 | 每次签到获得的积分 |
| `brick_messages` | 10 | 烧成一块砖所需的其他群友消息数 |
| `max_bricks` | 3 | 每人最多同时拥有的砖块数（可用 + 正在烧） |
| `brick_start_cost_points` | 0 | 开始烧砖消耗的积分 |
| `mute_seconds` | 60 | 拍人成功后的禁言秒数 |
| `protect_admins` | true | 禁止拍群主和管理员 |
| `notify_on_complete` | true | 砖烧好时在群里 @ 砖主 |
| `leaderboard_size` | 10 | 积分榜显示人数 |
| `timezone` | Asia/Shanghai | 签到和兑换限次使用的时区 |
| `xiaoning_enabled` | false | 是否开启积分兑换 |
| `xiaoning_exchange_mode` | both | `balance` 仅余额、`reset_card` 仅重置卡、`both` 两者 |
| `xiaoning_base_url` | 空 | Xiaoning-API 地址，不含 `/v1` |
| `xiaoning_fixed_user_id` | 0 | 大于 0 时所有兑换统一发到该用户 ID |
| `xiaoning_access_token` | 空 | 管理员访问令牌 |
| `xiaoning_points_per_balance` | 10 | 多少积分兑换 1 额度 |
| `xiaoning_points_per_reset_card` | 100 | 多少积分兑换 1 张重置卡 |
| `xiaoning_daily_balance_limit` | 1 | 每人每日额度上限，0 为不限 |
| `xiaoning_monthly_balance_limit` | 10 | 每人每月额度上限，0 为不限 |
| `xiaoning_daily_reset_card_limit` | 1 | 每人每日重置卡上限，0 为不限 |
| `xiaoning_monthly_reset_card_limit` | 5 | 每人每月重置卡上限，0 为不限 |
| `xiaoning_quota_per_unit` | 0 | 1 额度对应的 API quota；0 为自动读取 |
| `xiaoning_timeout_seconds` | 15 | 请求 Xiaoning-API 的超时秒数 |

## Xiaoning-API 兑换说明

- 加余额接口：`POST /api/user/manage`，请求体使用 `action=add_quota`、`mode=add`。
- 发重置卡接口：`POST /api/subscription/admin/users/{id}/reset_cards`。
- 额度换算：默认读取 `/api/status` 的 `quota_per_unit`、`quota_display_type` 和货币汇率，自动换算“1 额度”对应的原始 quota。
- QQ 号不会自动对应 Xiaoning 用户 ID。用户可以自行 `绑定账号 <ID>`，也可以由管理员设置 `xiaoning_fixed_user_id` 统一发放。
- 新版 `nap_` PAT 加余额需要 `user:write` scope。
- 按 Xiaoning-API 当前源码，重置卡发放接口尚未声明新版 PAT 的路由 scope；使用新版 PAT 可能返回 `ACCESS_TOKEN_ROUTE_UNDECLARED`。此时请使用旧版管理员访问令牌，或在服务端为该路由补充 `billing:write` scope。
- 兑换失败会自动退回本次预扣的积分和限次。

## 数据持久化

数据保存在：

```text
data/plugin_data/astrbot_plugin_brick_game/state.json
```

包含积分、签到日期、砖块、烧砖进度、绑定账号、兑换次数与限额计数。插件使用原子写入和防抖保存，避免频繁磁盘 IO。

## 验证

已通过：

- Python 语法检查与 pyflakes；
- 模拟 AstrBot 事件的签到、烧砖、拍人回归测试；
- 本地模拟 Xiaoning-API 的余额/重置卡请求、鉴权、额度换算、日/月限额、失败退分、账号绑定和持久化测试。

真实环境使用时，需要确保 AstrBot 已载入本插件，并且 Xiaoning-API 访问令牌具有管理员权限。
