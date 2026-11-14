# 网易云音乐云盘上传协议笔记

> 本文是逆向整理的实现笔记，不是官方文档。接口、参数、风控都可能变化。代码实现见 [`src/wy_music_cloud/client.py`](../src/wy_music_cloud/client.py)。

## 1. 会话与请求头

登录后 Cookie 至少包含：

```text
MUSIC_U=...; __csrf=...
```

每次 weapi 请求：

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

## 4. 云盘列表

```http
POST https://music.163.com/api/v1/cloud/get
```

weapi 明文：

```json
{"limit": 100, "offset": 0}
```

用于 `check` 命令统计云盘歌曲数。

## 5. 上传前检查（去重核心）

```http
POST https://interface.music.163.com/api/cloud/upload/check
```

weapi 明文：

```json
{"bitrate":"999000","ext":"","length":<文件字节数>,"md5":"<32位小写md5>","songId":"0","version":1}
```

返回重点：

- `body.needUpload == false`：云端已有，直接跳过；
- `body.songId`：云端已有歌曲 ID，后续 finalize 会用到；
- `body.needUpload == true`：继续分配上传 token。

## 6. 分配 resourceId（token A）

```http
POST https://music.163.com/weapi/nos/token/alloc
```

weapi 明文：

```json
{"bucket":"","ext":"mp3","filename":"<不含扩展名的文件名>","local":false,"nos_product":3,"type":"audio","md5":"<md5>"}
```

返回 `result.resourceId`。

## 7. 分配 NOS 对象（token B）

同样接口，但参数不同：

```json
{"bucket":"jd-musicrep-privatecloud-audio-public","ext":"<实际扩展名>","filename":"<清洗后的文件名>","local":false,"nos_product":3,"type":"audio","md5":"<md5>"}
```

文件名清洗：去掉扩展名、去掉空白、把 `.` 换成 `_`。

返回 `result.objectKey` 和 `result.token`。

## 8. 获取 NOS 上传节点

```http
GET https://wanproxy.127.net/lbs?version=1.0&bucketname=jd-musicrep-privatecloud-audio-public
```

返回 `data.upload[0]`，取其中的 host。

> 旧代码里硬编码的 `http://45.127.129.8` 已过时；现在用 LBS 返回的节点。参考 [NeteaseCloudMusicApi#1851](https://github.com/Binaryify/NeteaseCloudMusicApi/pull/1851)。

## 9. 上传文件到 NOS

```http
POST http://<host>/<bucket>/<objectKey>
```

- `objectKey` 中的 `/` 要转成 `%2F`；
- Query：`offset=0&complete=true&version=1.0`；
- Header `x-nos-token: <token>`；
- Header `Content-MD5: <hex md5>`；
- Header `Content-Type: audio/mpeg`（按实际格式可调整）；
- Body 是文件原始字节，`Content-Length` 必须准确。

## 10. 写入云盘信息

```http
POST https://music.163.com/api/upload/cloud/info/v2
```

weapi 明文：

```json
{"md5":"<md5>","songid":"<check 返回的 songId>","filename":"<文件名>","song":"<标题>","album":"<专辑>","artist":"<艺术家>","bitrate":"999000","resourceId":"<token A 的 resourceId>"}
```

标签缺失时回退：`song` 用文件名，`album` 用“未知专辑”，`artist` 用“未知艺术家”。

返回新的 `songId`。

## 11. 发布到云盘

```http
POST https://interface.music.163.com/api/cloud/pub/v2
```

weapi 明文：

```json
{"songid":"<info v2 返回的 songId>"}
```

成功后可被云盘列表看到。

## 12. 完整顺序

```text
check(md5,size)
  ├─ needUpload=false ──► 结束（skipped_remote）
  └─ needUpload=true
       ├─ token/alloc bucket=""                -> resourceId
       ├─ token/alloc bucket=jd-musicrep-...   -> objectKey, token
       ├─ GET lbs                              -> host
       ├─ POST NOS (文件字节)                   -> 上传完成
       ├─ info/v2 (md5,songid,resourceId,...)  -> songId
       └─ pub/v2 (songid)                      -> 发布
```

## 13. 常见错误

| 现象 | 可能原因 |
| --- | --- |
| `code=301/302` | Cookie 失效、未登录、风控 |
| `code=400` | 参数不合法（不是登录问题） |
| NOS 403 / token 无效 | token 过期、objectKey 路径不对、Content-MD5 不匹配 |
| `needUpload=false` 但列表看不到 | 已上传未发布，或客户端缓存；检查 pub/v2 是否成功 |
| 上传成功但云盘不显示 | 发布失败、审核/格式限制、客户端刷新延迟 |

## 14. 稳定性建议

- 所有请求设置超时；网络错误和 5xx 才重试，4xx 不盲目重试；
- token 分配和 NOS 上传之间不要长时间停顿；
- 上传前先 `check`，这是最便宜、最有效的去重；
- 把协议封装在一个 client 模块里，接口变化时只改一处；
- 保留 `--dry-run`，先用小流量验证；
- 不要并发上传大量文件，适当 `rate_limit`。

## 15. 参考

- [Binaryify/NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi)
- [lulu-ls/cloud-uploader](https://github.com/lulu-ls/cloud-uploader)
- [Binaryify/NeteaseCloudMusicApi#1851](https://github.com/Binaryify/NeteaseCloudMusicApi/pull/1851)