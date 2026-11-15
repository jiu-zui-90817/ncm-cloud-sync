# 网易云音乐云盘上传协议笔记

> 本文是逆向整理的实现笔记，不是官方文档。接口、参数、风控都可能变化。代码实现见 [`src/wy_music_cloud/client.py`](../src/wy_music_cloud/client.py)。

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

2026-11 起，`/api/upload/cloud/info/v2` 和 `/api/cloud/pub/v2` 通过 weapi 调用会返回 `[400] 接口调用失败`，必须改用 eapi：

```http
POST https://interfacepc.music.163.com/eapi/upload/cloud/info/v2
Content-Type: application/x-www-form-urlencoded
User-Agent: NeteaseMusic 9.0.90/5038 (iPhone; iOS 16.2; zh_CN)
Cookie: <header map 的 URL 编码 k=v 对>

params=<AES-128-ECB 十六进制大写>
```

加密前的 JSON 是业务字段加上 `header` 和 `e_r=false`。`header` 至少包含 `osver`、`deviceId`（52 位大写 hex）、`os`、`appver`、`versioncode`、`mobilename`、`buildver`、`resolution`、`__csrf`、`channel`、`requestId`，登录后还要带 `MUSIC_U`（有匿名 token 时带 `MUSIC_A`），并带上 `NMTID`。云盘写入使用 PC 身份：`appver=3.1.29.205117`、`osver=Microsoft-Windows-10-Professional-build-19045-64bit`，域名是 `interfacepc.music.163.com`。Cookie 头由同一份 `header` 按 `k=v` 做 URL 编码后拼接，不是直接复用登录 Cookie。0.2.14 漏了 `NMTID` 且用了 `interface.music.163.com`，info/v2 仍会返回 [400] 接口调用失败。

加密步骤：

```text
uri       = /api/upload/cloud/info/v2
text      = 紧凑 JSON
md5_text  = md5("nobody" + uri + "use" + text + "md5forencrypt")
plain     = uri + "-36cd479b6b5-" + text + "-36cd479b6b5-" + md5_text
params    = AES-128-ECB(plain, "e82ckenh8dichen8") -> hex 大写
```

请求 URL 是 `https://interfacepc.music.163.com/eapi/` 加上 `uri` 去掉 `/api/` 后的部分；加密和签名仍然使用完整的 `/api/...` uri。

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
POST https://music.163.com/weapi/cloud/upload/check
```

weapi 明文：

```json
{"bitrate":"999000","ext":"","length":<文件字节数，数字>,"md5":"<32位小写md5>","songId":"0","version":1}
```

返回重点：

- `body.needUpload == false`：文件字节已在网易云对象存储，但仍需申请 token 并用 info/v2 + pub/v2 补建云盘条目；
- `body.songId`：云端已有歌曲 ID，后续 finalize 会用到；
- `body.needUpload == true`：申请上传 token 并上传文件字节。

## 6. 申请上传 token（单个 NOS 桶 token）

```http
POST https://music.163.com/weapi/nos/token/alloc
```

weapi 明文：

```json
{"bucket":"jd-musicrep-privatecloud-audio-public","ext":"<实际扩展名>","filename":"<清洗后的文件名>","local":false,"nos_product":3,"type":"audio","md5":"<md5>"}
```

文件名清洗：去掉扩展名、去掉空白、把 `.` 换成 `_`。

返回 `result.resourceId`、`result.objectKey` 和 `result.token`。三者必须来自同一个 token：上传字节用 `objectKey`/`token`，写入云盘信息用同一个 `result.resourceId`。不要先申请空桶 token 再用它的 `resourceId`，否则 info/v2 会返回 [400] 接口调用失败。

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

- `objectKey` 中的 `/` 要转成 `%2F`；
- Query：`offset=0&complete=true&version=1.0`；
- Header `x-nos-token: <token>`；
- Header `Content-MD5: <hex md5>`；
- Header `Content-Type: audio/mpeg`（按实际格式可调整）；
- Body 是文件原始字节，`Content-Length` 必须准确。

## 9. 写入云盘信息（eapi）

```http
POST https://interfacepc.music.163.com/eapi/upload/cloud/info/v2
```

eapi 业务明文（加密前还会加上 `header` 和 `e_r=false`）：

```json
{"md5":"<md5>","songid":<check 返回的 songId 数字>,"filename":"<真实文件名，含扩展名>","song":"<标题>","album":"<专辑>","artist":"<艺术家>","bitrate":"999000","resourceId":<同一个 NOS token 的 resourceId 数字>}
```

字段要求：`songid` 用 check 返回的 `songId`，按 JSON 数字提交，返回 0 时提交 `0`，只有响应里完全没有 `songId` 时才省略；`resourceId` 必须是来自第 6 步实际用于上传的那个 NOS token 的 `resourceId`，按 JSON 数字提交，空桶 token 或字符串都可能返回 [400] 接口调用失败；`filename` 用真实文件名（含扩展名），`song` 用标题。不要发送 `objectKey`，否则会返回 `{"msg":"rep create failed","code":404}`。

标签缺失时回退：`song` 用文件名，`album` 用“未知专辑”，`artist` 用“未知艺术家”。

返回新的 `songId`。

## 10. 发布到云盘（eapi）

```http
POST https://interfacepc.music.163.com/eapi/cloud/pub/v2
```

eapi 业务明文（同样带 `header`、`e_r=false` 并加密）：

```json
{"songid":<info v2 返回的 songId 数字>}
```

成功后可在云盘列表看到。

## 11. 完整顺序

```text
check(md5,size)
  ├─ needUpload=false：跳过字节上传，但仍要走完下面的补建流程
  └─ token/alloc bucket=jd-musicrep-...   -> resourceId, objectKey, token
       ├─ GET lbs                              -> host
       ├─ needUpload=true: POST NOS (文件字节) -> 上传完成
       ├─ info/v2 (eapi, md5,songid,resourceId) -> songId
       └─ pub/v2 (eapi, songid)                -> 发布
```

## 12. 常见错误

| 现象 | 可能原因 |
| --- | --- |
| `code=301/302` | Cookie 失效、未登录、风控 |
| `code=400` / `[400] 参数错误` | 参数不合法，或把 weapi 加密表单发到了 `/api/...` 而不是 `/weapi/...`（不是登录问题） |
| `info/v2` 返回 `[400] 接口调用失败` | 云盘写入仍走 weapi；2026-11 起必须改用 `interfacepc.music.163.com` 的 eapi，PC appver `3.1.29.205117`，header/Cookie 带 `NMTID`，`resourceId`、`songid` 用 JSON 数字 |
| `code=405` | 操作频繁，被限流；应停止本轮并等待冷却，不能按文件重试刷请求 |
| `code=429` | 请求过多，与 405 同类，按限流处理 |
| NOS 403 / token 无效 | token 过期、objectKey 路径不对、Content-MD5 不匹配 |
| `needUpload=false` 但列表看不到 | 已上传未发布，或客户端缓存；检查 pub/v2 是否成功 |
| 上传成功但云盘不显示 | 发布失败、审核/格式限制、客户端刷新延迟 |

## 13. 稳定性建议

- 所有请求设置超时；网络错误和 5xx 才重试，4xx 不盲目重试；
- token 分配和 NOS 上传之间不要长时间停顿；
- 上传前先 `check`，这是最便宜、最有效的去重；
- 把协议封装在一个 client 模块里，接口变化时只改一处；
- 保留 `--dry-run`，先用小流量验证；
- 不要并发上传大量文件，适当 `rate_limit`。

## 14. 参考

- [Binaryify/NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi)
- [lulu-ls/cloud-uploader](https://github.com/lulu-ls/cloud-uploader)
- [Binaryify/NeteaseCloudMusicApi#1851](https://github.com/Binaryify/NeteaseCloudMusicApi/pull/1851)
- [SPlayer-Dev/ncm-api-rs](https://github.com/SPlayer-Dev/ncm-api-rs)：eapi 云盘写入、header/Cookie 构造的当前参考
- [SPlayer-Dev/SPlayer-Next](https://github.com/SPlayer-Dev/SPlayer-Next)：单个 NOS token 的完整上传顺序