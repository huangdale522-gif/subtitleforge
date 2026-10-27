# 已观看媒体清理（WatchedCleaner）

MoviePilot V3 插件。它从 Emby、Jellyfin 或 Plex 读取指定用户的观看状态，按电影、整季、整剧或单集列出可删除内容，并在候选列表和 Telegram 推送中显示媒体海报；用户勾选后再通过媒体服务器 API 删除。

插件支持带海报的 Telegram 扫描/删除逐条推送，以及“已观看内容入库超过一年后逐条询问是否删除”的定时提醒。逐条消息会在后台限速发送，避免多张图片触发 Telegram 限流。定时任务只读扫描并发送询问，绝不会自动删除；也不会直接操作 NAS 文件系统。

## 安全设计

- 插件默认关闭。
- 首次扫描默认只预演，不删除任何媒体。
- 实际删除必须同时满足：
  - 关闭“仅预演，不执行删除”；
  - 开启“允许实际删除”；
  - 配置至少一个有效的媒体服务器路径白名单；
  - 请求提交 `confirmation=DELETE`。
- 每次执行前重新读取媒体服务器状态，不使用过期扫描结果直接删除。
- 收藏、近期入库、近期播放、未达到观看阈值的内容会自动跳过。
- “整季删除”只有在同一季所有已入库分集都达到观看阈值时才允许。
- “整剧删除”只有在整部剧所有已入库分集都达到观看阈值时才允许。
- 第 0 季特别篇不会参与按季清理。
- 单次最多处理数量有独立上限，默认 20。
- 扫描结果默认 24 小时过期。
- 每次预演、删除、跳过和失败都会写入插件审计记录。
- Telegram 删除按钮仍会重新读取媒体服务器状态，并再次校验预演开关、实际删除开关、路径白名单、收藏、播放时间和观看进度。
- Telegram 删除按钮只接受配置的 Telegram 数字用户 ID，其他用户点击会被拒绝。

## 支持范围

| 媒体服务器 | 观看用户 | 删除方式 |
| --- | --- | --- |
| Emby | 指定用户或服务器当前管理用户 | `DELETE Items/{id}` |
| Jellyfin | 指定用户或服务器当前管理用户 | `DELETE Items/{id}` |
| Plex | 当前 Token 对应用户 | `DELETE /library/metadata/{id}` |

Plex Home 的多用户观看状态无法仅凭一个服务器 Token 完整区分，因此首版只使用 Token 当前用户的状态。

## 安装

这是 MoviePilot V3 插件源码，目录结构为：

```text
plugins.v3/watchedcleaner/
├── __init__.py
├── adapters.py
├── policy.py
└── README.md
```

本插件已经包含在仓库
<https://github.com/huangdale522-gif/subtitleforge> 中。MoviePilot V3 的安装方式：

1. 打开“设置 -> 插件 -> 插件市场”。
2. 添加插件仓库 `https://github.com/huangdale522-gif/subtitleforge`。
3. 同步插件市场，找到“已观看媒体清理”。
4. 安装并启用插件。

首次使用时保持“仅预演，不执行删除”开启，先扫描并勾选条目完成预演。

如果直接使用本地插件仓库，可配置：

```env
PLUGIN_LOCAL_REPO_PATHS=/path/to/your/plugin-repo
PLUGIN_AUTO_RELOAD=true
```

修改源码后重启 MoviePilot，确保新插件模块和配置表单重新加载。

### NAS Docker 本地仓库

假设 NAS 上的插件仓库目录为 `/volume1/docker/moviepilot/plugin-repos/watchedcleaner`，容器内挂载为 `/plugin-repos/watchedcleaner`：

```yaml
services:
  moviepilot:
    volumes:
      - /volume1/docker/moviepilot/config:/config
      - /volume1/docker/moviepilot/plugin-repos:/plugin-repos
    environment:
      - PLUGIN_LOCAL_REPO_PATHS=/plugin-repos/watchedcleaner
      - PLUGIN_AUTO_RELOAD=true
```

宿主机目录结构必须是：

```text
/volume1/docker/moviepilot/plugin-repos/watchedcleaner/
├── package.v3.json
└── plugins.v3/
    └── watchedcleaner/
```

重启 MoviePilot 后，在插件市场同步本地插件并安装。不要把源码直接复制到正在运行的 MoviePilot 容器内部；容器升级后可能丢失。

## NAS 路径白名单

删除由媒体服务器执行，所以白名单必须填写**媒体服务器看到的路径**，而不是 MoviePilot 容器内的路径。

例如：

- Emby/Jellyfin 容器把 NAS 的 `/volume1/Media` 映射为 `/media`，服务器条目路径显示 `/media/Movies/...`，白名单应填 `/media`。
- Plex 直接看到 NAS 的 `/volume1/Media`，白名单应填 `/volume1/Media`。

不要填写 `/`、`/mnt` 这类过宽根目录。插件会拒绝根目录级别的白名单。

白名单校验是路径边界校验；真正的文件删除仍由媒体服务器执行，因此不会受 MoviePilot 容器挂载差异影响。

## 配置

1. 在 MoviePilot 的媒体服务器设置中启用 Emby、Jellyfin 或 Plex。
2. 启用插件，保持“仅预演，不执行删除”开启。
3. 选择媒体服务器。留空会扫描全部受支持服务器。
4. Emby/Jellyfin 可填写观看状态用户；留空使用服务器当前解析到的管理用户。
5. 选择“电影”“剧集”“动漫”分类。
6. 选择剧集/动漫清理粒度：
   - 按季：同一季所有已入库分集都达到阈值时，只列出一个季级候选；
   - 按整剧：整部剧所有已入库分集都达到阈值时，只列出一个整剧候选；
   - 按单集：只列出达到阈值的已看单集。
7. 设置观看阈值、播放后保护天数、入库后保护天数。
8. 保存配置，进入插件详情页点击“扫描已观看媒体”。
9. 在候选表中勾选条目，点击“预演删除选中项”。
10. 核对结果后，才考虑关闭预演并开启实际删除。

## Telegram 推送与年度询问

### 1. 配置 MoviePilot Telegram

先在 MoviePilot 的“设置 -> 通知 -> Telegram”中配置 Bot Token、Chat ID 和管理员用户，并确认普通通知可以正常送达。

### 2. 获取 Telegram 数字用户 ID

插件要求填写 Telegram 数字用户 ID，例如 `123456789`。可向 Telegram 的 `@userinfobot` 发送任意消息获取。

不要填写用户名、群组 ID 或 Bot ID。该限制用于防止群聊中的其他成员点击删除按钮。

### 3. 启用插件推送

在插件配置中：

1. 开启“启用 Telegram 推送”。
2. 填写“Telegram 用户 ID”。
3. 按需开启“推送扫描结果”和“推送删除结果”。
4. 开启“启用超过保留期限的年度询问”。
5. 设置入库天数，默认 `365` 天。
6. 设置自动检查间隔，默认 `168` 小时（7 天）。
7. 设置每轮最多询问条数，默认 `10` 条，避免大型媒体库一次刷屏。
8. 设置未处理提醒的重复间隔，默认 `30` 天。

保存后，MoviePilot 的“设置 -> 服务”中会出现“已观看媒体年度清理提醒”。也可以在插件详情页点击“立即检查超期内容”手动触发一次。

### 4. 消息中的操作

每条提醒包含：

- 电影、剧集或动漫分类；
- 对应电影、剧集或动漫海报；
- 标题与季集信息；
- 媒体服务器、入库时间、已入库天数和观看进度；
- 媒体服务器看到的文件路径；
- “删除电影/删除这一季/删除整部剧/删除这一集”按钮；
- “保留，不再询问”按钮。

点击“保留”后，同一媒体不会再发送提醒。点击删除时，插件会重新执行完整安全校验；当前仍是预演模式、未开启实际删除或白名单无效时，只回复拒绝原因，不会删除文件。

年度询问使用媒体服务器返回的 `DateCreated`（Emby/Jellyfin）或 `addedAt`（Plex）判断入库时间，不读取 NAS 文件系统的创建时间。没有入库时间的条目不会触发询问。

## 删除权限

### Emby

- 使用具有管理权限的 API Key。
- 确认媒体库和对应账号允许删除媒体。
- 某些版本可能仅移除媒体库记录，具体文件删除行为取决于服务器设置和 API 权限，请先用预演与测试媒体验证。

### Jellyfin

- 使用管理员 API Key。
- 建议在测试库中先验证 `DELETE /Items/{id}` 会删除实际文件。

### Plex

- Token 对应用户需要拥有服务器管理权限。
- 在 Plex 服务器设置中开启允许删除媒体。
- 插件把 Plex 的正向用户评分也视为受保护内容，因为不同 Plex 版本没有统一可靠的“收藏”字段。可通过关闭“收藏条目永不删除”改变此行为。

## API

所有接口都需要 MoviePilot Bearer 鉴权。

### 扫描

```http
POST /api/v1/plugin/WatchedCleaner/scan
Content-Type: application/json

{}
```

可以在请求体覆盖本次扫描参数，例如：

```json
{
  "server": "Emby",
  "categories": ["movie", "tv", "anime"],
  "tv_mode": "season",
  "watched_threshold": 90,
  "min_days_since_played": 7,
  "min_days_since_added": 7
}
```

### 预演删除

```json
{
  "candidate_keys": ["0123456789abcdef01234567"],
  "confirmation": "DRY_RUN"
}
```

### 实际删除

```json
{
  "candidate_keys": ["0123456789abcdef01234567"],
  "confirmation": "DELETE"
}
```

`candidate_keys` 只能来自最近一次扫描。插件不接受任意媒体服务器 ID 或文件路径。

### 立即执行年度询问

```http
POST /api/v1/plugin/WatchedCleaner/age-reminders/run
Content-Type: application/json

{}
```

该接口只读扫描已观看媒体，并通过 Telegram 发送确认消息，不会自动删除。

### 审计

```http
GET /api/v1/plugin/WatchedCleaner/history?limit=100
```

## 已知限制

- 仅支持 Emby、Jellyfin、Plex。
- Plex 多用户观看状态不在首版支持范围内。
- 媒体服务器必须允许通过 API 删除文件；仅移除媒体库记录不算插件能够控制的文件删除。
- 扫描不读取外部字幕、NFO 或关联文件的大小；这些文件是否一并删除取决于媒体服务器行为。
- 路径白名单是媒体服务器返回路径的文本边界校验，无法远程解析 NAS 上的符号链接。不要让媒体库路径中包含指向白名单外部的符号链接。
- 大型媒体库首次扫描可能需要较长时间，可先用较小分类或较严格的保护条件测试。
- 年度提醒不是自动清理计划：插件只负责询问，必须由配置的 Telegram 用户点击删除按钮，且实际删除开关全部满足后才会执行。
- 同一媒体点击“保留”后会永久跳过；如需重新询问，需要清除插件的 `age_reminders` 数据。

## 测试

如果使用带 MoviePilot V3 宿主测试桩的插件仓库，可在仓库根目录执行：

```bash
pytest tests/v3/watchedcleaner
```

测试依赖 MoviePilot V3 的 `app` 模块或等价的宿主测试桩。
