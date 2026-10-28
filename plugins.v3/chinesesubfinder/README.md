# ChineseSubFinder 字幕（MoviePilot V3 插件）

把 [ChineseSubFinder](https://github.com/ChineseSubFinder/ChineseSubFinder) 的核心能力直接做进 MoviePilot：
**找字幕 → 语言匹配 → 下载 → 解压 → 转码 → 调轴 → 落盘**，不需要额外部署服务端或容器。

与同仓库的 `CSFReborn` 的区别：`CSFReborn` 是「插件 + 独立 SubtitleForge 服务」的两段式架构，本插件是**单文件、纯内嵌**的独立实现，字幕站适配器跑在 MoviePilot 进程里。两者功能有重叠，**装一个即可**。

## 字幕来源

| 来源 | 匹配方式 | 凭据 | 默认 |
| --- | --- | --- | --- |
| SubtitleBest | IMDb ID，电影 / 单集 / 整季包 | API Key | 开 |
| Assrt | 中英文片名 + 年份校验 | Token | 关 |
| 迅雷字幕 | 视频 cid 指纹（3×20KB SHA1） | 无 | 开 |
| Shooter | 视频 hash 指纹（4×4KB MD5） | 无 | 关（站点年久失修） |

## 触发方式

- **整理完成事件**：监听 `TransferComplete`，处理目标视频（含季包批量整理、蓝光原盘目录）。
- **定时扫描**：cron 遍历媒体库目录，补齐历史遗留视频；同一文件 7 天内不重复处理。
- **手动 API**：
  - `POST /download?path=/media/.../xxx.mkv`
  - `POST /scan`
  - `GET /history`、`GET /sources`

## 行为说明

- **语言以内容为准**：站点标签只参与排序与预筛，是否落盘由字幕正文判定（繁体特征字占比 + 中英双语行占比）。
- **已有字幕检测**：外挂字幕（含 `Subs/`、`subtitles/` 子目录、短文件名匹配）+ `ffprobe` 内封字幕流。
- **指纹来源做名称校验**：迅雷/Shooter 按文件指纹匹配，接口偶尔返回无关字幕，插件会校验集数、年份、片名交集，明显不符直接丢弃。
- **编码转换**：GB18030 / BIG5 自动转 UTF-8（可关）。
- **调轴**：可选调用 `ffsubsync`（以视频音轨为基准），支持关键字豁免。
- **解包**：zip 内置支持；rar/7z 需要容器内有 `7z`、`unrar` 或 `bsdtar`。
- **命名**：`<视频名>.<语言标签>.<扩展名>`，标签可选 `zh-CN/zh-TW`（Emby、Jellyfin）、`chs/cht`、`zh-Hans/zh-Hant`（Plex）。

## 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| 启用插件 | 关 | 总开关 |
| 整理完成后自动处理 | 开 | 关闭后只响应定时扫描/手动 API |
| 发送通知 | 关 | 每成功下载一条字幕发一条插件消息 |
| 字幕转 UTF-8 | 开 | GBK/BIG5 自动转码 |
| 需要的字幕语言 | 简体中文 | 可多选，缺哪种补哪种 |
| 字幕语言标签 | `zh-CN/zh-TW` | Emby/Jellyfin 用 ISO，Plex 用 `zh-Hans/zh-Hant` |
| 每个视频最多下载条数 | 2 | 防止反复下不同版本 |
| 请求间隔（秒） | 1 | 对字幕站友好 |
| 自动调轴 | 关 | 需容器内可执行 `ffsubsync` |
| SubtitleBest API Key | 空 | subtitle.best 申请 |
| Assrt Token | 空 | assrt.net 用户中心获取 |
| 迅雷字幕 / Shooter | 开 / 关 | 按文件指纹匹配，无需账号 |
| 字幕存放位置 | 视频同目录 | 或自定义模板 `{type}`、`{title}`、`{year}`、`{season}` |
| 定时扫描（cron） | 空 | 如 `0 3 * * *`，留空不启用 |
| 每次扫描视频数上限 | 30 | 控制单次耗时 |
| 排除路径正则 | 空 | 如 `/Adult/|/预告/` |
| 扫描目录 | 空 | 逗号分隔，留空自动读取 MoviePilot 媒体库目录 |

## 安装

把 `plugins.v3/chinesesubfinder` 复制到 MoviePilot 插件目录（通常为 `/app/app/plugins/chinesesubfinder/`）后重启；
或把本仓库地址加入 MoviePilot 插件市场源，直接安装「ChineseSubFinder 字幕」。

## 自测

```bash
python3 tests/v3/chinesesubfinder/check_chinesesubfinder.py
```

脚本会伪造 `app.sdk.*` 宿主模块，覆盖语言判定、两种文件指纹、片名/年份/集数校验、各来源响应解析，
以及「下载 → 解压 → 转码 → 命名落盘 → 历史 → 去重」完整链路（用本地 HTTP 服务模拟字幕站）。
