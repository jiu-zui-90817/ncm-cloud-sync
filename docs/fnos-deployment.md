# 飞牛 NAS 部署指南

本项目现在提供**原生 fnOS 应用**：不依赖 Docker，安装后在应用中心启动，通过内置 Web UI 完成扫码登录、同步、设置和日志查看。下面先讲原生应用；最后保留 Docker 方式，供非 fnOS 环境或喜欢容器的用户使用。

## 一、原生应用（推荐）

### 1. 环境要求

- fnOS **1.2.0+**；
- x86_64 或 aarch64；
- 应用中心能安装官方 `python312` 依赖。

原生包已经内置 `httpx`、`pycryptodome`、`mutagen`、`PyYAML`、`qrcode`、`waitress` 的 cp312 离线依赖（x86_64 / aarch64 两套），运行时不需要 Docker、PyPI 或 NAS 上额外 `pip install`。

### 2. 获取 `.fpk`

从 [Releases](https://github.com/jiu-zui-90817/ncm-cloud-sync/releases) 下载最新的版本化安装包（例如 `ncm-cloud-sync-0.3.0.fpk`）。

自行构建：

```bash
git clone https://github.com/jiu-zui-90817/ncm-cloud-sync.git
cd ncm-cloud-sync
# 需要官方 fnpack：放到 .tools/fnpack 或加入 PATH
bash scripts/build-fnos.sh
```

产物为 `dist/ncm-cloud-sync-<版本>.fpk`。构建机需要网络下载 wheel；NAS 运行时不需要。

### 3. 安装

1. 在飞牛 NAS 文件管理里上传 `.fpk`；
2. 打开应用中心 → 手动安装 → 选择文件；
3. 按向导填写音乐目录、同步间隔、演练模式和日志级别；
4. 安装完成后启动应用。

安装向导字段：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| 音乐目录 | `/vol1/1000/Music` | 必须是已存在的绝对路径；应用只读扫描 |
| 同步间隔 | `3600` | 秒，60–86400 |
| 演练模式 | `false` | 首次建议先开，只检查不上传 |
| 日志级别 | `INFO` | DEBUG / INFO / WARNING / ERROR |

如果音乐目录对应用包用户不可读，请在应用中心授权目录，或在文件管理里给应用用户增加读取权限。应用不会修改音乐文件。

### 从 0.2.x 迁移

0.3.0 改了应用标识、包用户、数据目录和 Python 包名，应用中心不会把它当成 0.2.x 的升级。旧版本可以继续保留，确认新版本可用后再卸载。

1. 停止旧应用，备份 `/var/apps/wy-music-cloud/var`；
2. 安装 `ncm-cloud-sync-0.3.0.fpk`，启动一次让新数据目录生成，然后停止；
3. 把旧目录里的 `settings.json` 和 `data/` 复制到 `/var/apps/ncm-cloud-sync/var/`，确认属主是新的应用用户 `ncm-cloud-sync`；
4. 启动新应用，检查账号、音乐目录、排除目录和通知配置；登录态失效时重新扫码；
5. 确认同步正常后再卸载旧应用，卸载前不要删除旧数据目录。

环境变量前缀也从 `WY_MUSIC_` 改为 `NCM_CLOUD_SYNC_`，Web 请求头改为 `X-NCM-Cloud-Sync`，Docker 服务名/容器名改为 `ncm-cloud-sync`；旧 `.env` 不会自动迁移。

### 4. 打开 Web UI

启动后点击桌面上的「NCM 云盘同步」图标，或应用中心的“打开”。页面通过飞牛统一网关加载，不需要开放端口，也不需要 SSH。右上角「关于」页可查看当前版本，并手动检查 GitHub 是否有新版本。

如果页面暂时打不开：

- 等 5–10 秒后刷新；
- 确认应用状态是“运行中”；
- 查看数据目录的 `app.log`。

### 5. 扫码登录

1. 点击「生成登录二维码」；
2. 打开网易云音乐手机 App 扫码；
3. 在手机上确认登录；
4. 页面显示昵称和云盘歌曲数后即可使用。

二维码约 4 分钟有效，过期后重新生成。登录态保存在 `data/session.json`，权限 0600。

### 6. 首次同步

推荐顺序：

1. 点「演练」：只扫描、计算 MD5、检查本地和云端，不上传；
2. 看统计里的“待上传 / 跳过 / 失败”是否符合预期；
3. 确认后点「立即同步」；
4. 后续交给后台按间隔自动执行。

同步过程中可以点「暂停」：当前文件处理完后停下，并暂停后台自动同步，点「继续」恢复；点「终止」尽快结束当前一轮，但不会关闭自动同步，下一轮仍按计划运行。两者都不会删除已上传记录，未处理文件下一轮继续。

「演练」不会分配上传 token，也不会写上传状态，不会污染去重记录。

### 7. 设置与日志

Web UI 设置项：

- **音乐目录**：一行一个绝对路径，可多个；
- **排除目录**：一行一个绝对路径，必须位于音乐目录内，整棵目录跳过；
- **自动同步**：关闭后只保留手动同步，后台调度不会自动触发；
- **同步间隔**：60–86400 秒；
- **请求间隔**：0–10 秒，网易云接口请求的最小间隔，默认 1 秒；遇到 405 限流时调大到 1.5～3 秒；
- **日志级别**：DEBUG / INFO / WARNING / ERROR；
- **通知**：在「通知」页添加渠道、启用/停用、发送测试，并按成功 / 失败 / 始终发送；支持 Telegram、Bark、ntfy、Gotify、Discord、Slack、钉钉机器人、Server酱、PushPlus、Pushover、PushDeer、Webhook、SMTP；
- **钉钉机器人安全设置**：支持加签（`secret`）和自定义关键词（`keyword`，多个用逗号分隔；消息不含关键词时程序会自动补第一个）。IP 地址段白名单只能在钉钉机器人安全设置里填 NAS 的出口公网 IP，程序无法代填；同时启用加签、关键词和 IP 白名单时，三项都要满足。
- **演练模式**：开启后自动同步也只检查不上传。

保存后对下一轮同步生效。日志卡片显示最近日志；更多历史在数据目录的 `app.log`。通知失败只记日志，不会中断同步；`settings.json` 里的通知 token 为敏感信息，请勿分享。

### 8. 数据目录

原生应用的数据目录由 fnOS 的 `TRIM_PKGVAR` 提供，通常在：

```text
/var/apps/ncm-cloud-sync/var/
├── settings.json
├── app.log
├── app.pid
├── login.txt
└── data/
    ├── state.db
    ├── session.json
    ├── last_run.json
    └── qr.png
```

以应用中心显示的实际路径为准。

- `settings.json`：音乐目录、排除目录、自动同步、通知配置、同步间隔、请求间隔、日志级别；
- `data/state.db`：SQLite 同步状态，去重核心；
- `data/session.json`：登录态，敏感；
- `app.log`：运行日志。

### 9. 升级

在应用中心用新 `.fpk` 覆盖安装即可。升级回调会保留设置、状态和登录态，只替换程序文件。升级前建议备份 `settings.json`、`data/state.db`、`data/session.json`。

### 10. 备份与恢复

备份：

```bash
# 路径以应用中心显示为准
sudo tar czf ncm-cloud-sync-backup.tgz \
  -C /var/apps/ncm-cloud-sync/var \
  settings.json data/state.db data/session.json
```

恢复：停止应用，把文件放回对应位置，再启动。只恢复 `state.db` 也能工作；最坏情况下所有文件重新计算 MD5，云端 check 仍会挡住重复上传。

### 11. 卸载

在应用中心卸载。卸载回调默认保留数据目录，方便重装后继续使用。若确定不再使用，可手动删除：

```bash
sudo rm -rf /var/apps/ncm-cloud-sync/var
```

删除前请确认路径；不同 fnOS 版本的目录可能不同。

### 12. 排错

**应用启动失败**

优先在页面日志卡片点“诊断信息”或“复制诊断”，那里会显示真实的 `app.log` 路径和内存日志。常见原因：

- 官方 `python312` 依赖没有装好：重新安装应用或先安装依赖；
- CPU 架构不匹配：确认 NAS 是 x86_64 或 aarch64；
- 数据目录权限不对：确认应用包用户可写 `TRIM_PKGVAR`。

**Web UI 502 / 空白**

应用进程没起来或 socket 没就绪。重启应用；仍失败则看 `app.log`。不要手动删除 `app.sock` 以外的程序文件。

**安装时提示“音乐目录不可用”**

- `目录不存在`：到文件管理里确认实际路径，注意 `Music` 等目录名大小写；
- `应用用户没有读取权限`：在应用中心给本应用授权该目录，或在文件管理里给应用用户 `ncm-cloud-sync` 增加读取权限；
- `必须是绝对路径`：路径要以 `/` 开头；
- 路径里不要带换行；从聊天或文件管理器粘贴时如果被拆成两行，删掉换行即可。0.2.1 起会自动去掉粘贴时带入的换行。

**扫描不到文件**

- 路径必须是绝对路径；
- 目录必须存在且应用用户可读；
- 检查扩展名是否在允许列表；
- 检查是否被 `exclude` 规则排除。

**登录失效**

重新扫码；扫码失败时用登录卡片的 Cookie 登录兜底。若提示接口错误，可能是网易云接口变动，查看 [docs/PROTOCOL.md](PROTOCOL.md) 和项目 issue。

**扫码后手机提示“数据加载失败”或“服务器内部错误”**

扫码接口经历过多次风控升级。0.2.6 的 eapi 扫码已经能拿到 `MUSIC_U`，但轮询在吸收 `Set-Cookie` 时可能因同名 Cookie 多 Path 抛异常并回到登录页；0.2.7 已改为直接解析原始 `Set-Cookie`，保留最后一个非空值。0.2.6 起使用网易云 PC 客户端的 `eapi` 接口（`interfacepc.music.163.com`，`type=3`），并带 `deviceId`、PC 版本号和完整设备请求头；二维码内容仍只带 `codekey`。0.2.5 的 weapi 方案仍可能被部分账号风控拦截，0.2.4 的公开 `/api` 方案缺少设备上下文，手机确认后会报“数据加载失败”。升级到 **0.3.0**，停止并重新启动应用，刷新页面后重新生成二维码。若仍失败：展开登录卡片里的“查看诊断 / 日志（登录失败时先看这里）”，点“复制诊断”反馈；同时可用“扫码失败？改用 Cookie 登录”，从已登录的 music.163.com 复制包含 `MUSIC_U` 的 Cookie 粘贴登录。

**没有日志文件 / 日志是空的**

0.2.4 起服务端带内存日志缓冲。登录前可以在登录卡片展开“查看诊断 / 日志（登录失败时先看这里）”，登录后也可以在同步控制里点“复制诊断”，或在页面底部日志卡片点“诊断信息 / 复制诊断”。它会列出真实的 `app.log` 路径、文件是否存在、数据目录、Python、PID、Socket、`TRIM_*` 环境变量和最近 120 行日志；即使 `app.log` 因权限没写成，也能看到内存日志。反馈问题时点“复制诊断”即可。

**同步失败 / 上传时提示 [400] 参数错误**

日志会记录具体文件与错误。0.2.7 及更早版本把带 weapi 加密的表单发到了 /api/cloud/upload/check，网易云网关按明文接口处理会返回 [400] 参数错误；0.2.8 已统一把 /api/... 改写成 /weapi/...，并校正 ext、length 字段。0.2.16 起查重改走 eapi。只改查重并不能解决写入阶段的 400：0.2.17 已把云盘信息提交改到 `musicupload.netease.com` 并使用鲸鱼 token，但 eapi body 还带着网页端的 `e_r`/12 字段 header。0.2.18 按原版 `cloudmusic_util` 改为 5 字段 header、无 `e_r`、数字 `resourceId` 和原始 `songid`，并跳过 `needUpload=false` 时多余的 token。升级到 0.3.0，停止再启动应用后重试。页面底部日志卡片点“诊断信息”，会显示真实的 `app.log` 路径、数据目录、Python、PID、Socket、`TRIM_*` 环境变量和最近 120 行日志；即使日志文件因权限没写成，内置日志缓冲仍可查看。其他常见原因：网络、Cookie、云盘容量、单文件限制、接口变动。失败文件会保留状态，修好后可重试。

**上传时提示 [400] 接口调用失败（提交云盘信息）**

0.2.13 及更早版本用 weapi 提交云盘信息，2026-11 起会返回 [400] 接口调用失败。0.2.14-0.2.16 只换了 eapi 域名、header 和查重，写入链路仍然是旧的；0.2.17 换了鲸鱼 token 和 `musicupload.netease.com`，但 eapi body 仍带 `e_r=false` 和网页端 12 字段 header，`resourceId`/`songid` 还是字符串，所以仍然 400。0.2.18 按本机原版 `cloudmusic_util` 的实际请求修正：先 POST `http://musicupload.netease.com/api/whale/token/alloc` 申请鲸鱼 token（`channel=3`、真实文件名、md5、`bucket=jd-musicrep-privatecloud-audio-public`），用返回的 `token`/`objectKey` 上传字节，再用同一个 `resourceId` 和 `objectKey={bucket}/{objectKey}` 提交 info/v2；eapi body 只有业务字段 + 5 字段 header，不发 `e_r`，`resourceId` 为数字，`songid` 用查重返回的原始值（新文件是数字 0）；`needUpload=false` 时不申请 token。`pub/v2` 返回 201（重复发布）也按成功处理。升级到 0.3.0，停止再启动应用后重试；若仍失败，复制诊断信息，日志里的“提交云盘信息失败”会带上 filename、songid、resourceId、host、appver 和是否有 MUSIC_U。

**上传时提示 Connection refused / 无法获取上传节点**

0.2.10 及更早版本仅用 HTTPS 请求 NOS LBS，并按旧版 `data.upload[0].host` 解析。NAS 到 `https://wanproxy.127.net` 被拒绝，或接口改成顶层 `upload` 字符串列表时，每个文件都会在获取上传节点处失败。0.2.11 先 HTTPS 后 HTTP，兼容两种响应，LBS 全失败时回退 `nosup-jd1.127.net` / `nosup-hz1.127.net`；全部失败会暂停本轮并保留待处理。升级后停止再启动应用，并检查 NAS 能否解析/访问 `wanproxy.127.net`；仍失败请复制诊断信息反馈。

**同步时提示 [405] 操作频繁 / 上传接口被限流**

0.2.9 及更早版本遇到 405 后仍会继续请求每个文件，短时间内大量分配上传 token 会触发风控，最后整库都显示失败。0.2.10 起所有网易云接口请求都按“请求间隔”节流（默认 1 秒）；第一次遇到 405 就暂停本轮，当前文件标记为待处理，其余计入“待续”，下一轮自动继续。0.2.19 起鲸鱼 token 申请失败不再回退旧的 `/weapi/nos/token/alloc`，所以日志里不应再出现这个旧地址；如果仍看到 405，说明是鲸鱼 token 接口本身被限流。遇到 405 时等 10～30 分钟让风控冷却，在设置里把“请求间隔”调到 1.5～3 秒，不要连续点“立即同步”。页面会显示暂停原因和待续数量，也可以手动暂停或终止本轮；0.2.20 起终止只结束当前一轮，不会关闭自动同步。

**很多文件提示没有读取权限 / 失败里“无权限”很多**

应用以包用户 `ncm-cloud-sync` 运行。音乐文件属于其他用户且权限是 600 时无法读取。到应用中心给本应用授权音乐目录，或在文件管理里给应用用户 `ncm-cloud-sync` 增加读取权限。0.2.10 会把这类错误单独计入“无权限”，日志每个文件只记一行警告；0.2.20 起可在概览点“无权限”卡片查看具体文件，授权后点“重置并重试”重新排队。0.2.21 起，排除规则命中的旧记录会自动转为 `ignored` 并从统计和问题列表隐藏。

**忽略 / 排除的文件为什么消失了**

0.2.21 起，命中小文件/扩展名过滤或 `exclude` / `exclude_dirs` 的记录会标记为 `ignored`，不计入总数，也不出现在失败、无权限和待处理列表；这不会删除磁盘文件。取消排除后重新同步会恢复正常处理。

**提示“云端已有”，但云盘里是空的**

0.2.8 及更早版本把 needUpload=false 直接当成“云盘已有”并跳过。该字段只表示文件字节已经在网易云对象存储，云盘条目还需要 eapi info/v2 + eapi pub/v2 才会出现。0.2.9 改为继续补建条目，并且不再信任旧的 skipped_remote 状态，会自动重新检查；0.2.18 起 `needUpload=false` 不再申请 token，直接 info/v2 + pub/v2 补建。如果旧记录仍然干扰，勾选同步控制里的“强制重新检查”再点“立即同步”。

## 二、Docker（可选）

仅在你不想用原生应用，或不在 fnOS 上时使用。Docker 方式与原生应用共用同步核心和状态格式。

### 1. 准备

```text
ncm-cloud-sync/
├── config.yaml
├── data/
└── docker-compose.yml
```

```bash
cp config.example.yaml config.yaml
```

至少设置：

```yaml
music_dirs:
  - /music
data_dir: /data
```

`docker-compose.yml` 默认把宿主音乐目录挂到容器 `/music`（只读），把 `./data` 挂到 `/data`。

### 2. 构建与登录

```bash
docker compose build
docker compose run --rm ncm-cloud-sync login --qr-png /data/qr.png
```

二维码会打印在终端并保存到 `data/qr.png`。扫码确认后会话写入 `data/session.json`。

也可以：

```bash
docker compose run --rm ncm-cloud-sync login --cookie "MUSIC_U=...; __csrf=..."
```

### 3. 试跑与常驻

```bash
docker compose run --rm ncm-cloud-sync sync --dry-run --limit 5
docker compose run --rm ncm-cloud-sync sync
docker compose up -d
docker compose logs -f --tail=100
```

### 4. 权限

容器默认以 UID/GID 1000 运行。用 `id` 查看音乐目录属主，必要时在 `docker-compose.yml` 的 `build.args` 里改 `PUID` / `PGID`，并确保 `./data` 对该 UID 可写。

### 5. 数据

Docker 数据在宿主 `./data`：`state.db`、`session.json`、`last_run.json`、`qr.png`。备份这些文件即可。

## 三、安全建议

- Web UI 只对管理员开放，但仍不要把 NAS 管理端口直接暴露到公网；
- `session.json` 等同账号登录凭证，备份和分享时注意脱敏；
- 只同步你有权上传的音乐；
- 网易云上传是非公开接口，避免高频、大批量操作；
- 定期备份 `state.db` 和 `session.json`，接口或程序升级后可以快速恢复。
