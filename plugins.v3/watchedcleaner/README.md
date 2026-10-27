# 已观看媒体清理（WatchedCleaner）

MoviePilot V3 插件。它从 Emby、Jellyfin 或 Plex 读取指定用户的观看状态，列出可删除的电影、剧集和动漫，由用户勾选后再通过媒体服务器 API 删除。

插件不会定时自动删除，也不会直接操作 NAS 文件系统。

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
- “整剧删除”只有在所有已入库分集都达到观看阈值时才允许。
- 单次最多处理数量有独立上限，默认 20。
- 扫描结果默认 24 小时过期。
- 每次预演、删除、跳过和失败都会写入插件审计记录。

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
6. 选择电视剧粒度：
   - 单集：只列出达到阈值的已看单集；
   - 整剧：仅当所有已入库分集都达到阈值时列出整部剧。
7. 设置观看阈值、播放后保护天数、入库后保护天数。
8. 保存配置，进入插件详情页点击“扫描已观看媒体”。
9. 在候选表中勾选条目，点击“预演删除选中项”。
10. 核对结果后，才考虑关闭预演并开启实际删除。

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
  "tv_mode": "episode",
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
- 不提供自动清理计划，避免无人值守误删。

## 测试

如果使用带 MoviePilot V3 宿主测试桩的插件仓库，可在仓库根目录执行：

```bash
pytest tests/v3/watchedcleaner
```

测试依赖 MoviePilot V3 的 `app` 模块或等价的宿主测试桩。
