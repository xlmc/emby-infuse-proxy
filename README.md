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
| 响应缓存 | 无状态接口（预告片探测/媒体库目录）边缘缓存，降低上游压力 |
| 稳定性 | 增肥带 6.5 秒时间预算与每请求 45 条目上限，列表永不超时；结果渐进缓存 |

## 部署步骤

### 1. 前置条件
- Cloudflare 账号（Workers 免费版可跑，付费版子请求额度更宽裕）
- 一个托管在 Cloudflare 的域名（用于 Workers 自定义域）

### 2. 创建 Worker 并上传
方式 A（网页）：Dashboard → Workers → Create → 粘贴 `worker.js` 内容（Module 语法）。

方式 B（脚本，推荐）：
```bash
python cf_deploy.py <你的CF_API_TOKEN>
```
脚本会自动：定位脚本名 → 备份线上版本 → 保留绑定与兼容性设置 → 上传 → 探测验证。

### 3. 绑定（Settings → Variables & Bindings）
| 名称 | 类型 | 说明 |
|---|---|---|
| `ADMIN_PASS` | plain_text | 管理页密码 |
| `ADMIN_PATH` | plain_text | 管理页路径（如 `admin-x7f`） |
| `DEFAULT_NODE` | plain_text | 默认上游地址 |
| `HOST` | plain_text | 本 Worker 对外域名 |
| `JWT_SECRET` | plain_text | 管理页 JWT 密钥（随机长字符串） |
| `ENI_KV` | KV Namespace | 节点/配置存储 |

### 4. 兼容性设置
- `compatibility_date`: 与线上保持一致（当前 `2026-08-20`）
- `compatibility_flags`: `enable_request_signal`, `nodejs_compat`

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
  "schemaVersion": 6
}
```

### 7. Infuse 接入
Infuse → 设置 → 存储 → 添加 Emby → 服务器地址填 **Worker 的自定义域** → 用上游账号密码登录。首次同步会做元数据增肥（较慢），同步 2 次后全部就位。

## 注意事项
- 图片镜像列表在 `worker.js` 的 `_mirrors` 数组中，可按需增删
- 评分匹配未命中的条目（多为综艺/冷门真人影视）保持无评分，不会错配
- Infuse 主屏「继续观看」遵循其自身规则：进度过短（<约 2%）不显示、接近看完（>约 90%）且已是最后一集会移出——这是客户端行为，与代理无关
- 回滚：`cf_deploy.py` 每次部署前自动备份线上版本到本地 `worker-backup-<时间>.js`

## 文件说明
- `worker.js` — Worker 完整源码（Module 格式，可直接部署）
- `cf_deploy.py` — 无依赖部署脚本（Python 3，仅需 CF API Token）
