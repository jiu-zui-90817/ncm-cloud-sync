# 网易云音乐云盘上传协议笔记

> 本文是逆向整理的实现笔记，不是官方文档。接口、参数、风控都可能变化。代码实现见 [`src/ncm_cloud_sync/client.py`](../src/ncm_cloud_sync/client.py)。

## 1. 会话与请求头

登录后 Cookie 至少包含：

```text
MUSIC_U=...; __csrf=...
```

除云盘写入外，每次 weapi 请求：

- URL 追加 `csrf_token=<__csrf 的值>`（没有就空字符串）；
- Header `Referer: https://music.163.com`；
- Header `User-Agent` 使用桌面版 UA；
- Header `Content-Type: application/x-www-form-urlencoded`；
- Body 为 `params=<加密串>&encSecKey=<加密串>`。

建议再带 `os=pc`、`appver=2.7.1.198277` 等参数，降低被识别为脚本的概率。

## 2. weapi 加密

固定值：

```text
iv        = 0102030405060708
presetKey = 0CoJUm6Qyw8W8jud
```

随机生成 16 位 `secret`，字符集：

```text
abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789
```

每个字符取一个随机字节 `n`，用 `n % 62` 选字符。

加密步骤：

```text
json_text = 紧凑 JSON（无多余空格）
first     = AES-128-CBC(json_text, presetKey, iv)  -> base64
params    = AES-128-CBC(first, secret, iv)         -> base64
encSecKey = RSA_NO_PADDING(secret 反转后左侧补零到 128 字节) -> hex
```

RSA 公钥（1024 位，`e=65537`）：

```text
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDgtQn2JZ34ZC28NWYpAUd98iZ37BUrX/aKzmFbt7clFSs6sXqHauqKWqdtLkF2KexO40H1YTX8z2lSgBBOAxLsvaklV8k4cBFK9snQXE9/DDaFt6Rr7iVZMldczhC0JNgTz+SHXT6CBHuX3e9SdB1Ua44oncaTWz7OBGLbCiK45wIDAQAB
```

Python 里用 `Crypto.Cipher.AES`（CBC、PKCS#7）和 `Crypto.PublicKey.RSA` + `Crypto.Util.number` 即可；注意 RSA 输出必须是 **128 字节定长 hex**。

## 3. 登录检查

```http
POST https://music.163.com/weapi/w/nuser/account/get
```

weapi 明文：`{}`

返回 `code=200` 表示有效；`301` / `302` 通常表示未登录。

## 3.1 weapi 路径约定

weapi 请求的 URL 必须使用 `/weapi/...`。NCMApi 在发送前会把代码里的 `/api/...` 改写成 `/weapi/...`；如果直接把加密表单发到 `/api/...`，网关会按明文参数校验并返回 `[400] 参数错误`。本文件下面的路径都已经写成实际请求路径。

## 3.2 eapi 云盘写入

2026-11 起，`/api/upload/cloud/info/v2` 和 `/api/cloud/pub/v2` 通过 weapi 调用会返回 `[400] 接口调用失败`，必须改用 eapi。不同接口的 eapi 域名不同：

```text
check        https://interfacepc.music.163.com/eapi/cloud/upload/check
info/v2      http://musicupload.netease.com/eapi/upload/cloud/info/v2
pub/v2       https://interface.music.163.com/eapi/cloud/pub/v2
whale token  http://musicupload.netease.com/api/whale/token/alloc  （普通 form，不是 eapi）
```

eapi 有两种请求形状，不能混用：

- **查重 check / 发布 pub**：网页端 eapi。body 带 `header` 和 `e_r=false`；`header` 包含 `osver`、`deviceId`（52 位大写 hex）、`os`、`appver=3.1.29.205117`、`versioncode`、`mobilename`、`buildver`、`resolution`、`__csrf`、`channel`、`requestId`；UA 用 iPhone 客户端，Cookie 用完整登录 Cookie。
- **云盘信息 info/v2**：原版 `cloudmusic_util` 的 eapi。body 只有业务字段 + `header`，`header` 恰好 5 个字段：`os="pc"`、`appver="3.1.42.205594"`、`deviceId`、`requestId`、`clientSign`；**不发 `e_r`**；UA 用桌面客户端 `NeteaseMusicDesktop/3.1.42.205594`，Cookie 同样用完整登录 Cookie。

关键：**Cookie 头必须是完整登录 Cookie**。0.2.15/0.2.16 把 NMTID 同时塞进 header 和 Cookie、0.2.17 继续用网页端 12 字段 header 和 `e_r`，info/v2 都会 400；0.2.18 按原版桌面客户端收窄成 5 字段 header 后才成功。

加密步骤：

```text
uri       = /api/upload/cloud/info/v2
text      = 紧凑 JSON
md5_text  = md5("nobody" + uri + "use" + text + "md5forencrypt")
plain     = uri + "-36cd479b6b5-" + text + "-36cd479b6b5-" + md5_text
params    = AES-128-ECB(plain, "e82ckenh8dichen8") -> hex 大写
```

请求 URL 是各接口 eapi 域名加上 `/eapi/`，再接 `uri` 去掉 `/api/` 后的部分；加密和签名仍然使用完整的 `/api/...` uri。

## 4. 云盘列表

```http
POST https://music.163.com/weapi/v1/cloud/get
```

weapi 明文：

```json
{"limit": 100, "offset": 0}
```

用于 `check` 命令统计云盘歌曲数。

## 5. 上传前检查（去重核心）

```http
POST https://interfacepc.music.163.com/eapi/cloud/upload/check
Content-Type: application/x-www-form-urlencoded
User-Agent: NeteaseMusic 9.0.90/5038 (iPhone; iOS 16.2; zh_CN)
Cookie: <完整登录 Cookie>

params=<AES-128-ECB 十六进制大写>
```

eapi 明文（网页端形状，带 12 字段 `header`、`e_r=false`；0.2.16 起查重走这里）：

```json
{"bitrate":"999000","ext":"","length":<文件字节数，数字>,"md5":"<32位小写md5>","songId":"0","version":1}
```

0.2.16 起查重使用 eapi，响应可能带 `Set-Cookie: NMTID=...`，客户端会吸收它并放进完整 Cookie。info/v2 的 400 不是单纯缺 `NMTID` 导致，关键是 info/v2 必须使用原版桌面客户端的 5 字段 header、无 `e_r`、数字 id。

返回重点：

- `body.needUpload == false`：文件字节已在网易云对象存储，但仍需申请 token 并用 info/v2 + pub/v2 补建云盘条目；
- `body.songId`：云端已有歌曲 ID，后续 finalize 会用到；
- `body.needUpload == true`：申请上传 token 并上传文件字节。

## 6. 申请鲸鱼上传 token（新版主流程）

```http
POST http://musicupload.netease.com/api/whale/token/alloc
Content-Type: application/x-www-form-urlencoded
Cookie: <完整登录 Cookie>

channel=3&filename=<真实文件名，含扩展名>&md5=<md5>&type=audio&bucket=jd-musicrep-privatecloud-audio-public&bizKey=<随机 hex>
```

返回 `data.bucket`、`data.objectKey`、`data.token`、`data.resourceId`、`data.endpoint` 等字段。`resourceId` 服务端可能返回字符串或数字，info/v2 提交时统一转成数字。

这个 token 必须同时用于三处：上传字节用 `data.token` + `data.objectKey`；info/v2 的 `resourceId` 用同一个 `data.resourceId`；info/v2 的 `objectKey` 用 `{bucket}/{data.objectKey}`。不要再用老 `/weapi/nos/token/alloc` 的 `result.resourceId` 去提交 info/v2，这是 0.2.13-0.2.16 一直 400 的关键原因。

0.2.19 起鲸鱼 token 申请失败**不再回退**旧 `/weapi/nos/token/alloc`：客户端把 405/429 转成 `RateLimitError`，其他失败转成 `UploadError`，由 Syncer 暂停整轮并保留待续。旧接口已经从上传主流程移除，避免它被调用时产生新的 405 风暴。

## 7. 获取 NOS 上传节点

```http
GET https://wanproxy.127.net/lbs?version=1.0&bucketname=jd-musicrep-privatecloud-audio-public
```

实际返回形如：

```json
{"lbs":"https://wanproxy.127.net/lbs","upload":["http://nosup-jd1.127.net"]}
```

取顶层 `upload[0]`，并兼容旧版 `data.upload[0].host`。0.2.11 起先试 HTTPS，失败再试 HTTP；LBS 都不通时依次探测 `nosup-jd1.127.net`、`nosup-hz1.127.net`。仍失败则抛 `NosUnavailableError` 并暂停整轮，而不是给每个文件打印一次 traceback。

> 旧代码里硬编码的 `http://45.127.129.8` 已过时；现在用 LBS 返回的节点。参考 [NeteaseCloudMusicApi#1851](https://github.com/Binaryify/NeteaseCloudMusicApi/pull/1851)。

## 8. 上传文件到 NOS

```http
POST http://<host>/<bucket>/<objectKey>
```

- `objectKey` 来自第 6 步的鲸鱼 token；
- `objectKey` 中的 `/` 要转成 `%2F`；
- Query：`offset=0&complete=true&version=1.0`；
- Header `x-nos-token: <token>`；
- Header `Content-MD5: <hex md5>`；
- Header `Content-Type: audio/mpeg`（按实际格式可调整）；
- Body 是文件原始字节，`Content-Length` 必须准确。

本项目当前按整文件一次 POST；大文件分片可参考 HyPlayer 的 1 MiB 分片和 `context` 续传，但不是 0.2.19 的必要条件。

## 9. 写入云盘信息（原生桌面 eapi，musicupload 域名）

```http
POST http://musicupload.netease.com/eapi/upload/cloud/info/v2
Content-Type: application/x-www-form-urlencoded
User-Agent: Mozilla/5.0 (Windows NT 10.0; WOW64) ... NeteaseMusicDesktop/3.1.42.205594
Cookie: <完整登录 Cookie>

params=<AES-128-ECB 十六进制大写>
```

eapi 业务明文（加密前加上 5 字段 `header`，**不加 `e_r`**）：

```json
{"album":"","artist":"","bitrate":999000,"filename":"<真实文件名，含扩展名>","md5":"<md5>","objectKey":"jd-musicrep-privatecloud-audio-public/<objectKey>","resourceId":84294414064,"song":"<标题>","songid":0,"header":{"os":"pc","appver":"3.1.42.205594","deviceId":"<52位hex>","requestId":"<8位>","clientSign":"<设备签名>"}}
```

字段要求：

- `resourceId`：必须来自第 6 步实际用于上传的鲸鱼 token，**数字**；老 NOS token 的 resourceId 不行。
- `objectKey`：必须是 `{bucket}/{objectKey}`，与鲸鱼 token 对应；仅 `needUpload=true` 时发送。
- `needUpload=false`：不申请 token，也不发送 `objectKey`/`resourceId`，只提交 `md5`、`filename`、`song`、`album`、`artist`、`bitrate`、`songid`。
- `filename`：真实文件名（含扩展名）；`song`：标题。
- `songid`：check 返回的 `songId` **原样**提交；新文件是数字 0，不要转成字符串。
- `bitrate`：整数。
- `album` / `artist`：直接用标签值，缺失时为空字符串。

返回新的 `songId`（字符串或数字）和 `privateCloud`。

## 10. 发布到云盘（eapi）

```http
POST https://interface.music.163.com/eapi/cloud/pub/v2
```

eapi 业务明文（网页端形状，带 `header`、`e_r=false` 并加密）：

```json
{"songid":"<info v2 返回的 songId，字符串>"}
```

返回 `200` 表示发布成功；`201` 表示重复发布（`CloudPublishResp` 注释为“貌似重复上传”），同步时同样按成功处理。成功后可在云盘列表看到。

## 11. 完整顺序

```text
check(eapi, md5,size)                         -> needUpload/songId
  ├─ needUpload=false：不申请 token，直接 info/v2（不带 objectKey/resourceId）
  │    └─ info/v2 -> songId -> pub/v2
  └─ needUpload=true：whale token alloc (musicupload)
       -> resourceId, objectKey, token
       ├─ GET lbs                             -> NOS host
       ├─ POST NOS (bytes)                    -> 上传完成
       ├─ info/v2 (5字段 header, 无 e_r, 数字 resourceId) -> songId
       └─ pub/v2 (interface eapi, 201 也算成功)          -> 发布
```

鲸鱼 token 申请失败时整轮暂停，不再回退老 `/weapi/nos/token/alloc`，未处理文件保留为待续。

## 12. 常见错误

| 现象 | 可能原因 |
| --- | --- |
| `code=301/302` | Cookie 失效、未登录、风控 |
| `code=400` / `[400] 参数错误` | 参数不合法，或把 weapi 加密表单发到了 `/api/...` 而不是 `/weapi/...`（不是登录问题） |
| `info/v2` 返回 `[400] 接口调用失败` | 写入链路或请求形状不对：info/v2 必须用 `musicupload.netease.com` 的原生桌面 eapi（5 字段 header、无 `e_r`）+ 完整 Cookie，`resourceId` 必须来自同一个鲸鱼 token 且为数字；老 NOS token、interfacepc 域名、缺 objectKey、`e_r`/12 字段 header 或字符串 id 都会 400。0.2.18 已按此修复 |
| `code=405` | 操作频繁，被限流；应停止本轮并等待冷却，不能按文件重试刷请求 |
| `code=429` | 请求过多，与 405 同类，按限流处理 |
| NOS 403 / token 无效 | token 过期、objectKey 路径不对、Content-MD5 不匹配 |
| `needUpload=false` 但列表看不到 | 已上传未发布，或客户端缓存；检查 pub/v2 是否成功 |
| 上传成功但云盘不显示 | 发布失败、审核/格式限制、客户端刷新延迟 |

## 13. 稳定性建议

- 所有请求设置超时；网络错误和 5xx 才重试，4xx 不盲目重试；
- 鲸鱼 token 的 `resourceId`、`objectKey`、`token` 必须成组使用，不要跨 token 混用；`needUpload=false` 时不要申请 token；
- token 分配和 NOS 上传之间不要长时间停顿；
- 上传前先 `check`，这是最便宜、最有效的去重；
- 把协议封装在一个 client 模块里，接口变化时只改一处；
- 正式上传前先做一次完整预检（登录态、云盘列表、上传查重），接口不可用就中止本轮，不要带着失效接口逐文件重试；
- 不要并发上传大量文件，适当 `rate_limit`；遇到 405/429 暂停整轮，等冷却后再继续。
- 0.2.19 起支持在 Web UI 暂停、终止、继续；暂停在当前文件处理完后生效，终止会尽快结束本轮。
- 0.2.20 起终止只结束当前一轮，自动同步仍按计划运行；暂停才会暂停自动同步直到点“继续”；自动同步可在设置里单独关闭。通知只观察同步结果，发送失败不会影响同步流程。
- 不想上传的目录用 `exclude_dirs` 整棵排除，不要靠扩展名或文件名通配符硬凑。
- 0.2.21 起，命中 `exclude` / `exclude_dirs` 或小文件/扩展名过滤的状态记录会标记为 `ignored` 并从总数和问题列表隐藏；「关于」页只读检查 GitHub Release，通知（含钉钉）只观察同步结果，均不影响同步流程。
- 0.2.22 起，通知配置兼容 tuple/list（修复同步结束通知被静默跳过）；Web 图标与应用图标一致；覆盖安装或在应用中心修改设置时合并保留 `exclude_dirs`、`auto_sync`、`notify_on`、`notifications`。
- 0.3.0 起项目更名为 NCM Cloud Sync，应用标识/包名/CLI 改为 `ncm-cloud-sync`，Python 包改为 `ncm_cloud_sync`，环境变量前缀改为 `NCM_CLOUD_SYNC_`；钉钉新增自定义关键词支持，IP 地址段白名单需在钉钉侧配置 NAS 出口公网 IP。
- 0.3.1 起每轮同步前先读云盘列表，用 `privateCloud.md5` 做 MD5 对账，已在云盘的直接 `skipped_remote`；`needUpload=false` 的补建单独记为 `linked`，不计入 `uploaded` 和字节数；进程 stdout/stderr 写到 `app.out`，`app.log` 只由日志 handler 写，避免重复。
- 0.3.21 起 Web 签到按钮在成功/已签到后变为“今日已签到”并禁用；请求进行中显示“签到中…”并忽略重复点击。服务端仍以 state 中的当天签到状态去重，不会重复请求签到接口。
- 0.3.20 起每日签到改为随机时刻：每天在本地时间 08:00–23:00 内随机抽取（分钟 1–59，避开整点），目标写入 state meta 的 `signin_target`，同一日期不会重抽；Web 调度线程和 `daemon` 每 60 秒检查一次，到点且未签到才请求，与同步间隔解耦，错过时刻后下一次检查补签。`signin_status` 返回 `target/window_start/window_end`，`POST /api/signin` 手动签到忽略随机时刻。
- 0.3.19 起新增 `daily_signin`（默认 `false`，环境变量 `NCM_CLOUD_SYNC_DAILY_SIGNIN`，Web 设置同名字段）；开启后每轮 `run_once` 在 `_load_cloud` 后、`_preflight` 前尝试调用（0.3.20 起还要等到当天随机时刻） `GET https://music.163.com/api/point/dailyTask?type=1` 签到，每天最多自动尝试一次，失败只记日志、不影响同步。返回 `-2` 视为已签到，`301/302` 视为登录失效，`405/429` 按限流处理。签到结果写入 state meta 的 `signin_date/signin_status/signin_message/signin_points/signin_at`，`GET /api/status` 返回 `signin`，`POST /api/signin` 可手动签到；签到只领取云贝，不增加云盘空间。
- 0.3.18 起同步后处理只对本次上传成功（`uploaded`）或补建云盘条目成功（`linked`）的文件执行；`up_to_date`、`skipped_remote`、`remote_exists` 不再触发 delete/move，避免开启删除后误删已同步的旧文件。移动目录输入框补齐文本输入样式。
- 0.3.17 起 `move_target` 可以位于音乐目录内的子目录：该目录会通过 `Config.scan_exclude_dirs` 自动加入扫描排除，归档文件不会被再次扫描或移动；`move_target` 仍不能等于任一音乐根目录。Web UI 提示同步更新。
- 0.3.16 起新增同步后处理配置 `post_sync_action`（`none`/`delete`/`move`，默认 `none`）、`move_target`（move 时必填，绝对路径、不能位于音乐目录内，不存在会自动创建；0.3.17 起允许音乐目录内子目录并自动跳过扫描）和 `check_missing`（默认 `true`）；若未显式配置 `check_missing` 且 `post_sync_action != none`，默认改为 `false`。环境变量为 `NCM_CLOUD_SYNC_POST_SYNC_ACTION`、`NCM_CLOUD_SYNC_MOVE_TARGET`、`NCM_CLOUD_SYNC_CHECK_MISSING`，Web 设置返回和保存同样的三项。同步成功且确认云盘已有该 MD5 后，delete 删除本地文件并记 `deleted`，move 移动到 `move_target` 并记 `archived`；失败只记 `post_action_failed`，不影响上传结果（0.3.18 起仅本次上传/补建的文件）。`check_missing=false` 时跳过缺失检查，`/api/status.stats.missing` 归零，`GET /api/files?kind=missing` 返回 `disabled=true,total=0`。缺失语义改为只相对于云盘：云盘仍有该 MD5 且本地不存在才记 `missing`；本地和云端都没有的记录改记 `deleted`，不计缺失。`GET /api/permissions/check` 移除 `commands`，新增 `guidance[]`，改为引导在飞牛文件管理中授权。
- 0.3.15 起通知标题品牌改为“NCM 云盘同步”，DingTalk 默认 `keyword` 改为 `NCM`；读取配置时只把旧默认值“网易云”迁移为 `NCM`，其他自定义关键词不变。
- 0.3.14 起同步的 `up_to_date` 快速路径会校验记录中的 MD5 是否仍在云盘列表；不在时记录日志并走完整对账/上传流程，避免云盘条目被删除或替换后永久跳过。
- 0.3.13 起 `missing_candidates(..., include_missing=True)` 会重新检查已标记 `missing` 的记录：若同一 MD5 出现在新路径，旧记录改为 `moved` 并计入 `stats.moved`，不再重复计入 `missing`；仅有移动的运行也会保留。
- 0.3.12 起通知默认跳过空跑：`success` 事件在 `uploaded + linked == 0` 时不发送，配置新增 `notify_empty`（默认 false，环境变量 `NCM_CLOUD_SYNC_NOTIFY_EMPTY`）；失败、暂停、终止不受影响。DingTalk 未配置 `keyword` 时默认“网易云”，通知标题改为“网易云云盘任务…”。文件移动后旧记录状态改为 `moved`，新路径按 MD5 复用，不计入 `missing`。
- 0.3.11 起新增 `GET /api/files?kind=uploaded|pending|missing|skipped&limit=`，返回 `{kind,total,items,counts}`；`items[]` 含 `path/name,size,status,kind,song_id,duplicate_of,last_error,updated_at`。若最近一条运行是 `mode=scan`，`kind=pending` 改用该 run 的 `to_upload` 明细，并返回 `source=scan,run_id,finished_at`，保证与 `pending_scan` 数量一致。`StateStore.finish_run(..., keep=False)` 会删除该 run 和 run_items；同步仅在有上传、补建、失败、缺失或待续时保留，仅扫描仅在有待上传、失败、云端独有或本地重复时保留。
- 0.3.10 仅整理文档与更新日志，无协议变化。
- 0.3.9 起云盘容量来自 `/api/v1/cloud/get` 顶层 `maxSize`（总字节）、`size`（已用字节）、`count`（歌曲数），由 `parse_cloud_capacity()` 解析为 `supported/total_bytes/used_bytes/free_bytes/percent/songs`；字段缺失时 `supported=false`，不把 0 当已满。新增 `GET /api/capacity`，只请求一页 `limit=1` 读取容量，不拉取全部歌曲。`Syncer` 在云盘列表加载后记录容量，每个文件上传前用 `total_bytes - used_bytes - bytes_uploaded` 校验；空间不足时该文件标记失败并记录“云盘剩余空间不足”，不调用上传接口。
- 0.3.8 修复 0.3.7 的启动回归：`web.py` 丢失 `if __name__ == "__main__": raise SystemExit(main())`，`python -m ncm_cloud_sync.web` 只导入模块后退出 0，应用中心报“Web 服务启动失败”；已恢复入口并新增 `tests/test_web.py::test_web_module_entrypoint` 回归测试。
- 0.3.7 起新增 `GET /api/permissions/check`：只读检查当前 `music_dirs` 中配置扩展名的音频文件，返回 `ok/user/files/dirs/unreadable_files/unreadable_dirs/samples/directories/truncated/limit/commands/notes`（0.3.16 起移除 `commands`，新增 `guidance`，见上）；`directories[]` 含 `path/exists/readable/files/dirs/unreadable_files/unreadable_dirs/truncated`；`commands` 为去重后的 `setfacl` 修复命令，不会执行。单次最多 200000 个文件、30 秒，超限时 `truncated=true`。
- 0.3.7 起 `/api/status` 新增 `pending_scan`：最近一条运行记录为 `mode=scan` 时返回 `{run_id,finished_at,to_upload,incomplete}`，否则为 `null`；Web 概览「待处理」优先显示该 `to_upload`，不写 `files` 表。
- 0.3.6 修复 `CloudClient.preflight()`：`cloud_song_md5_map()` 的 value 是 songId（int），旧代码按 dict 调 `info.get("size")` 会抛 `'int' object has no attribute 'get'`，导致每轮同步在预检阶段中止。现在兼容 `md5 -> songId` 与 `md5 -> 元数据 dict` 两种映射。
- 0.3.5 起移除 `dry_run` / 演练（与仅扫描重叠），同步模式只剩 `sync` 和 `scan`；每轮上传前调用新增的 `CloudClient.preflight()`：检查 `login_status()`、读取云盘列表并调用一次 `/api/cloud/upload/check`（不申请 whale token）。预检失败时本轮直接中止、`stats.error` 记录“上传前接口检查失败：…”，运行记录写入错误，`notify_run` 会把 `stats.error` 视为失败事件推送；云盘列表读取失败也不再用空映射继续，避免重复上传。旧历史里的 `mode=dry_run` 和 item kind `planned` 仅保留只读展示兼容。
- 0.3.4 起 `runs` 新增 `mode`（sync/scan）和 `stats_json`，新增 `run_items` 表保存逐文件结果（kind/path/name/detail/size，按 run 索引）；`POST /api/sync` 支持 `{"scan_only": true}`，只读扫描、不写 `files`、不上传、不发通知；`GET /api/runs/<id>?kind=&limit=&offset=` 返回 `{run,counts,items,total,limit,offset,kind}`，`limit` 1–500，记录不存在返回 404；`/api/status` 的 `runs[].mode`/`runs[].stats` 可直接读取；`finish_run` 清理旧记录时先删对应 `run_items`，历史仍只保留 5 次。
- 0.3.3 起 `/api/status.stats` 新增 `problems_total`（失败 + 无权限）；`GET /api/problems` 默认 `kind=all`，每行新增 `kind`（failed/permission）。最近一次运行统计新增 `processed`、`cloud_songs`、`scan_unsupported`、`scan_excluded`、`scan_unreadable`、`scan_missing_roots`、`unsupported_extensions` 和 `filtered`；`scanned` 改为本地候选数，实际处理数看 `processed`。
- 0.3.2 起 `runs` 表只保留最近 5 次执行记录，`finish_run` 写入后自动删除更早行；`GET /api/runs?limit=` 返回记录（limit 上限 5），`/api/status` 也带 `runs`。`app.log` 单文件 2 MB、保留 2 个备份，`/api/logs` 返回 `log_files`（path/exists/size）和 `log_limit_bytes`。

## 14. 参考

- [Binaryify/NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi)
- [lulu-ls/cloud-uploader](https://github.com/lulu-ls/cloud-uploader)
- [Binaryify/NeteaseCloudMusicApi#1851](https://github.com/Binaryify/NeteaseCloudMusicApi/pull/1851)
- [SPlayer-Dev/ncm-api-rs](https://github.com/SPlayer-Dev/ncm-api-rs)：eapi 加密、header 构造参考
- [SPlayer-Dev/SPlayer-Next](https://github.com/SPlayer-Dev/SPlayer-Next)：eapi 云盘写入参考
- [HyPlayer/HyPlayer.NeteaseProvider](https://github.com/HyPlayer/HyPlayer.NeteaseProvider)：鲸鱼 token + musicupload info/v2 + objectKey 的当前参考
- [magic-akari/ncm_api](https://github.com/magic-akari/ncm_api)：musicupload info/v2 的另一份实现