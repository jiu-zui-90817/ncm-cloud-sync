# 飞牛 NAS 部署指南

本项目现在提供**原生 fnOS 应用**：不依赖 Docker，安装后在应用中心启动，通过内置 Web UI 完成扫码登录、同步、设置和日志查看。下面先讲原生应用；最后保留 Docker 方式，供非 fnOS 环境或喜欢容器的用户使用。

## 一、原生应用（推荐）

### 1. 环境要求

- fnOS **1.2.0+**；
- x86_64 或 aarch64；
- 应用中心能安装官方 `python312` 依赖。

原生包已经内置 `httpx`、`pycryptodome`、`mutagen`、`PyYAML`、`qrcode`、`waitress` 的 cp312 离线依赖（x86_64 / aarch64 两套），运行时不需要 Docker、PyPI 或 NAS 上额外 `pip install`。

### 2. 获取 `.fpk`

从 [Releases](https://github.com/jiu-zui-90817/wy-music-cloud/releases) 下载最新的 `wy-music-cloud.fpk`。

自行构建：

```bash
git clone https://github.com/jiu-zui-90817/wy-music-cloud.git
cd wy-music-cloud
# 需要官方 fnpack：放到 .tools/fnpack 或加入 PATH
bash scripts/build-fnos.sh
```

产物为 `dist/wy-music-cloud.fpk`。构建机需要网络下载 wheel；NAS 运行时不需要。

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

### 4. 打开 Web UI

启动后点击桌面上的「网易云音乐云盘同步」图标，或应用中心的“打开”。页面通过飞牛统一网关加载，不需要开放端口，也不需要 SSH。

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

「演练」不会分配上传 token，也不会写上传状态，不会污染去重记录。

### 7. 设置与日志

Web UI 设置项：

- **音乐目录**：一行一个绝对路径，可多个；
- **同步间隔**：60–86400 秒；
- **日志级别**：DEBUG / INFO / WARNING / ERROR；
- **演练模式**：开启后自动同步也只检查不上传。

保存后对下一轮同步生效。日志卡片显示最近日志；更多历史在数据目录的 `app.log`。

### 8. 数据目录

原生应用的数据目录由 fnOS 的 `TRIM_PKGVAR` 提供，通常在：

```text
/var/apps/wy-music-cloud/var/
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

- `settings.json`：音乐目录、间隔、日志级别；
- `data/state.db`：SQLite 同步状态，去重核心；
- `data/session.json`：登录态，敏感；
- `app.log`：运行日志。

### 9. 升级

在应用中心用新 `.fpk` 覆盖安装即可。升级回调会保留设置、状态和登录态，只替换程序文件。升级前建议备份 `settings.json`、`data/state.db`、`data/session.json`。

### 10. 备份与恢复

备份：

```bash
# 路径以应用中心显示为准
sudo tar czf wy-music-cloud-backup.tgz \
  -C /var/apps/wy-music-cloud/var \
  settings.json data/state.db data/session.json
```

恢复：停止应用，把文件放回对应位置，再启动。只恢复 `state.db` 也能工作；最坏情况下所有文件重新计算 MD5，云端 check 仍会挡住重复上传。

### 11. 卸载

在应用中心卸载。卸载回调默认保留数据目录，方便重装后继续使用。若确定不再使用，可手动删除：

```bash
sudo rm -rf /var/apps/wy-music-cloud/var
```

删除前请确认路径；不同 fnOS 版本的目录可能不同。

### 12. 排错

**应用启动失败**

查看 `app.log`。常见原因：

- 官方 `python312` 依赖没有装好：重新安装应用或先安装依赖；
- CPU 架构不匹配：确认 NAS 是 x86_64 或 aarch64；
- 数据目录权限不对：确认应用包用户可写 `TRIM_PKGVAR`。

**Web UI 502 / 空白**

应用进程没起来或 socket 没就绪。重启应用；仍失败则看 `app.log`。不要手动删除 `app.sock` 以外的程序文件。

**扫描不到文件**

- 路径必须是绝对路径；
- 目录必须存在且应用用户可读；
- 检查扩展名是否在允许列表；
- 检查是否被 `exclude` 规则排除。

**登录失效**

重新扫码。若提示接口错误，可能是网易云接口变动，查看 [docs/PROTOCOL.md](PROTOCOL.md) 和项目 issue。

**同步失败**

日志会记录具体文件与错误。常见原因：网络、Cookie、云盘容量、单文件限制、接口变动。失败文件会保留状态，修好后可重试。

## 二、Docker（可选）

仅在你不想用原生应用，或不在 fnOS 上时使用。Docker 方式与原生应用共用同步核心和状态格式。

### 1. 准备

```text
wy-music-cloud/
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
docker compose run --rm wy-music-cloud login --qr-png /data/qr.png
```

二维码会打印在终端并保存到 `data/qr.png`。扫码确认后会话写入 `data/session.json`。

也可以：

```bash
docker compose run --rm wy-music-cloud login --cookie "MUSIC_U=...; __csrf=..."
```

### 3. 试跑与常驻

```bash
docker compose run --rm wy-music-cloud sync --dry-run --limit 5
docker compose run --rm wy-music-cloud sync
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
