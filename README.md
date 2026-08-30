# emby-infuse-proxy

部署在 Cloudflare Workers 上的 Emby 反向代理，专为 **emos 类「虚拟 ID」媒体库**（条目 ID 形如 `vl-` / `ve-` / `vs-` / `vb-` / `pinf-`）设计，目标是让 **Infuse** 在这类源上获得完整体验：可播放、有海报/背景/演员表/简介/评分/时长，主屏「继续观看」正常工作。

## 功能一览

| 功能 | 说明 |
|---|---|
| 视频流代理 | 拦截 `/Videos/{vl\|ve}-*/stream`，自动向上游换取带签名的直链（修复电影秒退） |
| 海报/背景修复 | 列表接口的虚拟 ID 图片 tag 自动替换为真实 tag（剧集 + 电影） |
| 元数据增肥 | 列表行自动合并详情数据：演员表、简介、时长、首播日期、分级、文件属性（MediaStreams） |
| 演员表修复 | 演员头像 tag 规范化为 32 位 hex；重复演员 Id 自动去重（上游常见缺陷） |
| 评分注入 | 上游无评分时，从 bangumi.tv 免鉴权 API 按「片名 + 年份 ±1 + 评分人数 ≥ 50」匹配并注入 CommunityRating |
| 继续观看修复 | Resume 列表的剧集行自动补真实时长（上游给 0，导致 Infuse 主屏卡片不显示） |
| 图片镜像链 | 图片经镜像站拉取并缓存在 Cloudflare 边缘，单点故障自动切换 |
| 超清原画 | 海报/背景图自动请求 `original` 原画档（等价于 emos 账号的"超清原画"开关，但无需开启、对所有客户端生效）；Logo 保持 w1280 控制体积 |
| 响应缓存 | 无状态接口（预告片探测/媒体库目录）边缘缓存，降低上游压力 |
| 首页请求去重 | `Items/Latest` 使用 5 秒账号隔离微缓存，并合并同一时刻的重复请求，降低 Infuse 首页刷新突发流量 |
| 播放线路缓存 | 从 `PlaybackInfo` 预先记录播放线路，30 分钟内起播入口不再重复查询相同线路 |
| 稳定性 | 增肥带 6.5 秒时间预算与每请求 45 条目上限，列表永不超时；结果渐进缓存 |

## 部署步骤

### 1. 前置条件
- Cloudflare 账号（Workers 免费版可跑，付费版子请求额度更宽裕）
- 一个托管在 Cloudflare 的域名（用于 Workers 自定义域）

### 2. 创建 Worker 并上传
方式 A（网页）：Dashboard → Workers → Create → 粘贴 `worker.js` 内容（Module 语法）。

方式 B（脚本，推荐）：
```bash
python cf_deploy.py --script emby-proxy --domain emby.example.com
```
未设置 `CF_API_TOKEN` 时，脚本会安全提示输入，不会把 Token 放在命令行参数中。脚本会：明确定位账号和脚本 → 校验当前活动版本及完整 bindings → 真正部署前提取可回滚源码 → 上传单模块正式入口 → 探测验证。任一绑定或备份步骤失败都会停止上传。

只读预检会核对 bindings，并在内存中验证线上源码能否提取为单模块备份；不写备份、不创建版本、不上传：

```bash
python cf_deploy.py --script emby-proxy --domain emby.example.com --dry-run
```

### 3. 绑定（Settings → Variables & Bindings）
| 名称 | 类型 | 说明 |
|---|---|---|
| `ADMIN_PASS` | plain_text | 管理页密码 |
| `ADMIN_PATH` | plain_text | 管理页路径（如 `admin-x7f`） |
| `DEFAULT_NODE` | plain_text | 默认上游地址 |
| `HOST` | plain_text | 本 Worker 对外域名 |
| `JWT_SECRET` | plain_text | 管理页 JWT 密钥（随机长字符串） |
| `ENI_KV` | KV Namespace | 节点/配置存储 |
| `DB` | D1 Database（可选） | 管理页日志、统计和诊断；不绑定时不影响基础代理功能 |

### 4. 兼容性设置

- 升级已有 Worker 时，部署脚本会读取并保留活动版本实际返回的 compatibility 设置；如果活动版本没有显式设置，不会在同一次代码升级中擅自新增日期或 flags。
- 新建 Worker 时应在 Cloudflare 中设置近期 `compatibility_date`，之后把兼容日期升级作为独立变更验证。当前单文件源码不依赖 Node.js 内置模块，不强制要求 `nodejs_compat`。

### 5. 自定义域
Workers → 你的脚本 → Settings → Domains → 绑定域名（如 `emby.example.com`）。

### 6. KV 节点配置
在 `ENI_KV` 中写入 key `node:emby`，value（JSON）：
```json
{
  "target": "https://上游地址:443/emby",
  "lines": [{ "id": "line-1", "name": "线路1", "target": "https://上游地址:443/emby" }],
  "activeLineId": "line-1",
  "entryMode": "kv_route",
  "tag": "EMOS",
  "playbackInfoMode": "rewrite",
  "mediaAuthMode": "emby",
  "mainVideoStreamMode": "direct",
  "schemaVersion": 6
}
```

### 6.1 视频带宽优化（建议先开启）
`mainVideoStreamMode` 决定大文件的传输数据面：

| 值 | 行为 | 适用场景 |
|---|---|---|
| `direct` | Worker 仅处理 Emby API / 鉴权；`/Videos/*` 等主视频请求返回 302，让 Infuse 直接跟随上游（或上游签发的网盘）下载地址 | 追求播放与测速带宽；这是默认推荐值 |
| `proxy` | Worker 持续中转主视频数据，包括上游的外部重定向 | 必须隐藏源站或统一审计 / 访问控制 |
| `inherit`（或省略） | 兼容旧配置：由节点遗留的 `direct` / 标签配置决定 | 旧部署平滑迁移 |

`direct` 仍使用本 Worker 作为 Emby 接入地址；只是不让媒体字节流经过 Worker。对于能“跑满带宽”的同类反代，这通常正是差异所在：外部 302/307 被交给客户端，而非由 Worker 继续拉取再转发。

Infuse 的 `vl-*` / `ve-*` 虚拟媒体会先读取一次轻量 `PlaybackInfo`，以兼容上游的 `/emya/video` 播放地址。v31 会把这次返回的线路决策按账号和媒体源缓存 30 分钟；随后到达 `/Videos/*/stream` 时不再重复查询线路。开启 `direct` 后，该地址以 `302` 交给客户端，不会重新落回 Worker 中转。

如果上游或网盘地址仅允许 Worker IP 访问，或客户端的鉴权无法随重定向传递，请改回：

```json
"mainVideoStreamMode": "proxy"
```

验证方法：在管理台访问日志中查看视频请求；开启后应显示 `deliveryMode=direct`、`playback_entry_direct` 或 `client_redirect`，而非 `worker_proxy`。视频仍显示为约 100 Mbps 时，说明瓶颈已经在客户端到上游（或上游到其网盘）的链路上，而不是这段 Worker 代码。

### 6.2 请求数优化边界

- Worker 能减少：重复 `Items/Latest`、重复播放线路查询、可安全缓存的图片和无状态接口请求。
- Worker 不能减少：客户端跟随 302 后直接向网盘/CDN 发出的 Range 请求。该阶段不经过 Worker，因此 Worker 看不到也无法合并这些请求。
- 播放线路缓存使用 Cloudflare 单机房边缘缓存；客户端切换到其他机房时，首次请求仍可能回源一次。
- 本项目不启用媒体字节分块缓存候选方案：当前网盘二跳会拒绝 Cloudflare 拉取，强行启用会重新造成播放失败。

### 7. Infuse 接入
Infuse → 设置 → 存储 → 添加 Emby → 服务器地址填 **Worker 的自定义域** → 用上游账号密码登录。首次同步会做元数据增肥（较慢），同步 2 次后全部就位。

## 注意事项
- 图片镜像列表在 `worker.js` 的 `_mirrors` 数组中，可按需增删
- 评分匹配未命中的条目（多为综艺/冷门真人影视）保持无评分，不会错配
- CloudflareSpeedTest 只能帮助客户端挑选更合适的 Cloudflare Anycast 入口 IP；它不能提升 Worker 到上游或网盘的带宽。要让 Infuse 使用测试结果，需在播放设备实际使用的 DNS 中完成域名到该 IP 的映射，并保留原域名的 TLS SNI。
- Infuse 主屏「继续观看」遵循其自身规则：进度过短（<约 2%）不显示、接近看完（>约 90%）且已是最后一集会移出——这是客户端行为，与代理无关
- 回滚：真正部署前，`cf_deploy.py` 会提取线上单模块源码到 `worker-backup-<脚本>-<时间>.js`，同时保存包含活动版本 ID 与 SHA-256 的 JSON 清单；备份失败则不会部署。可在 Cloudflare 部署历史切回清单记录的版本，或用 `--source <备份.js>` 重新上传源码。

## 文件说明
- `worker.js` — Worker 完整源码（Module 格式，可直接部署）
- `cf_deploy.py` — 无依赖部署脚本（Python 3，仅需 CF API Token）
- `tests/infuse-stream-rewrite-route-cache.test.mjs` — 播放线路缓存隔离与命中测试
- `tests/items-latest-microcache.test.mjs` — 首页微缓存、并发合并与过期测试
