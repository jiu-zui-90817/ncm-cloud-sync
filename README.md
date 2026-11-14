# wy-music-cloud

把本地音乐自动同步到**网易云音乐「音乐云盘」**的轻量工具，专为飞牛 NAS（fnOS）/ Docker 长期运行设计，带**本地 MD5 + 云端 upload/check 双重去重**。

> 非官方项目。网易云没有公开上传 API，本项目依赖从桌面客户端逆向出的 weapi / NOS 上传链路，接口随时可能变化。请先看[风险与限制](#风险与限制)。

## 为什么做这个

现有工具大多是桌面 GUI 或一次性上传脚本：

- [lulu-ls/cloud-uploader](https://github.com/lulu-ls/cloud-uploader)：Electron 桌面工具，适合 Mac 手动上传；
- [ovnrain/netease-music-cloud-uploader](https://github.com/ovnrain/netease-music-cloud-uploader)：项目说明中提到网易云 3.0 客户端已原生支持上传，因此停更；
- 其他 Python / GUI 工具通常没有“NAS 常驻 + 持久化去重 + 失败重试”的组合。

本项目把上传流程做成一个无界面服务：扫描目录、算 MD5、查本地状态库、查云端是否已有、上传、落库，循环执行。

## 特性

- **增量同步**：SQLite 记录 `path + size + mtime_ns + md5 + status`，文件没变就直接跳过，不重复计算、不重复上传。
- **双重去重**：
  1. **本地去重**：内容相同（MD5 相同）的多个文件只上传一份，其余标记为 `skipped_local`；
  2. **云端去重**：每次真正上传前调用 `/api/cloud/upload/check`，`needUpload=false` 说明云盘已有，标记为 `skipped_remote`，不再走上传。
- **元数据写入**：用 mutagen 读取标题 / 艺术家 / 专辑，上传时一并提交；没有标签时回退到文件名 / “未知艺术家”。
- **多种登录**：扫码登录、手机号密码登录、直接粘贴 Cookie；会话保存在 `data/session.json`（权限 0600）。
- **安全试跑**：`--dry-run` 只检查并打印计划，不分配 token、不上传、不写上传状态。
- **失败隔离**：单个文件失败只标记该文件，下一轮继续；支持 `reset-failed` 重试。
- **常驻 / 定时**：内置轮询 daemon，也可以交给飞牛“计划任务”定时执行一次 `sync`。
- **原生飞牛应用**：提供 `deploy/fnos/wy-music-cloud` 应用工程，可打包成 `.fpk`，在 fnOS 应用中心手动安装，带安装向导、启停和升级回调。
- **容器友好**：非 root 运行，`/music` 只读挂载，状态与会话都在 `/data`。
- **中文日志与状态**：`status` 直接看扫描 / 上传 / 跳过 / 失败统计和最近运行。

## 工作流程

```text
扫描 music_dirs
   │
   ├─ 扩展名 / 排除目录 / 大小过滤
   ├─ size+mtime 未变且已处理 ──► up_to_date
   ├─ 计算 MD5
   │    ├─ 本地已有同 MD5 ──► skipped_local
   │    └─ 云端 check: needUpload=false ──► skipped_remote
   └─ 上传：token alloc A → token alloc B → NOS → info/v2 → pub/v2
          └─ 成功 ──► uploaded（记录 songId）
```

去重顺序是“先本地、再云端”。云端 check 是最终权威：即使本地状态库丢失，只要云端已有同 MD5 的歌曲，也不会重复上传。

## 飞牛 NAS 原生应用（推荐）

仓库里的 `deploy/fnos/wy-music-cloud` 是标准 fnOS Docker 应用工程，打包后得到 `.fpk`。

### 1. 安装

从 [Releases](https://github.com/jiu-zui-90817/wy-music-cloud/releases) 下载 `wy-music-cloud.fpk`，打开飞牛 NAS 应用中心 → 手动安装 → 选择该文件。

也可以自行构建：

```bash
# 需要 fnOS 官方 fnpack，放到 .tools/ 或加入 PATH
bash scripts/build-fnos.sh
# 产物：dist/wy-music-cloud.fpk
```

### 2. 安装向导

安装时会要求填写：

| 项目 | 默认值 | 说明 |
| --- | --- | --- |
| 音乐目录 | `/vol1/1000/Music` | NAS 上的绝对路径，必须已存在；容器内只读挂载到 `/music` |
| 扫描间隔 | `3600` | 秒，最小 60 |
| 时区 | `Asia/Shanghai` | 传给容器 |
| 试运行 | `false` | 第一次可先设为 `true` |
| 运行 UID / GID | `1000` / `1000` | 用 `id` 查看音乐目录属主，避免权限问题 |

安装后配置写入应用目录的 `docker/.env`，升级和修改配置会保留。

### 3. 首次启动

应用中心启动后，Docker Compose 会在 NAS 上构建镜像（基础镜像 `python:3.12-slim` 需要联网拉取）。第一次可能需要几分钟，期间应用状态可能短暂显示未运行，构建完成后容器会自动启动。

日志：

```bash
sudo docker logs -f wy-music-cloud
```

### 4. 登录（SSH，一次即可）

应用没有桌面界面，首次需要在 NAS 上执行一次登录。应用安装目录通常是 `/var/apps/wy-music-cloud`，以实际为准；安装回调也会把登录提示写到应用数据目录的 `login.txt`。

```bash
cd /var/apps/wy-music-cloud/docker
sudo docker compose -p wy-music-cloud run --rm wy-music-cloud login --qr-png /data/qr.png
```

终端会显示二维码；用网易云音乐 App 扫码并确认。登录态保存在 Docker 数据卷 `wy-music-cloud-data` 中，升级/重装应用不会丢失。

也可以粘贴 Cookie：

```bash
sudo docker compose -p wy-music-cloud run --rm wy-music-cloud login --cookie "MUSIC_U=...; __csrf=..."
```

### 5. 验证与试跑

```bash
sudo docker compose -p wy-music-cloud run --rm wy-music-cloud check
sudo docker compose -p wy-music-cloud run --rm wy-music-cloud sync --dry-run --limit 5
```

确认输出符合预期后，可以直接在应用中心把“试运行”改为 `false`，或手动跑一次：

```bash
sudo docker compose -p wy-music-cloud run --rm wy-music-cloud sync --limit 5
```

更完整的安装、升级、备份和排错说明见 [docs/fnos-deployment.md](docs/fnos-deployment.md)。

## Docker 运行（通用 / 开发）

### 1. 准备目录

```text
wy-music-cloud/
├── config.yaml          # 配置（只读挂载）
├── data/                # state.db、session.json、qr.png、last_run.json
└── docker-compose.yml

/vol1/1000/Music/        # 你的音乐目录（只读挂载到 /music）
```

### 2. 配置

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml，至少设置 music_dirs
```

容器内建议：

```yaml
music_dirs:
  - /music
data_dir: /data
```

### 3. 构建并登录

```bash
docker compose build
docker compose run --rm wy-music-cloud login --qr-png /data/qr.png
```

终端会显示二维码，同时把二维码保存到 `data/qr.png`。用网易云音乐 App 扫码确认；登录成功后会话写入 `data/session.json`。

也可以直接粘贴 Cookie：

```bash
docker compose run --rm wy-music-cloud login --cookie "MUSIC_U=...; __csrf=..."
```

### 4. 试跑

```bash
docker compose run --rm wy-music-cloud sync --dry-run --limit 5
```

确认输出里的“待上传”符合预期后，再真正执行：

```bash
docker compose run --rm wy-music-cloud sync
```

### 5. 常驻

```bash
docker compose up -d
docker compose logs -f --tail=100
```

默认每 `interval` 秒跑一轮。飞牛 NAS 的部署细节、权限、计划任务和排错见 [docs/fnos-deployment.md](docs/fnos-deployment.md)。

## 本机运行（Windows / Linux / macOS）

需要 Python 3.10+。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e .

wy-music-cloud login
wy-music-cloud --music-dir "D:\Music" sync --dry-run --limit 5
wy-music-cloud --music-dir "D:\Music" sync
```

注意：`--music-dir`、`--config`、`--log-level` 是**全局参数，要写在子命令前面**。

## 配置

完整注释版见 [config.example.yaml](config.example.yaml)。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `music_dirs` | `[]` | 要扫描的音乐目录，可多个；相对路径相对于配置文件所在目录 |
| `data_dir` | `data` | 状态库、会话、二维码、最近运行记录目录 |
| `cookie` | `""` | 直接写 Cookie 字符串（不推荐，优先用登录命令） |
| `cookie_file` | `null` | 从文件读取 Cookie |
| `extensions` | 见示例 | 扫描的音频扩展名 |
| `upload_extensions` | 见示例 | 允许上传的扩展名；不在列表里的只统计、不上传 |
| `exclude` | `.*`, `@eaDir`, `#recycle`, `lost+found` | 跳过的目录 / 文件通配符 |
| `interval` | `3600` | daemon 轮询间隔（秒） |
| `rate_limit` | `0.5` | 两次上传之间的额外等待（秒） |
| `timeout` | `30` | 单次 HTTP 超时（秒） |
| `retries` | `3` | 网络 / 5xx 重试次数 |
| `min_size` | `0` | 小于该字节数的文件跳过 |
| `max_upload_size` | `0` | 大于该字节数的文件标记失败；0 表示不限制 |
| `dry_run` | `false` | 全局试跑；命令行 `--dry-run` 也会打开 |
| `log_level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

环境变量优先级高于 YAML，低于命令行参数。支持：

`WY_MUSIC_CONFIG`、`WY_MUSIC_DIRS`、`WY_MUSIC_DATA_DIR`、`WY_MUSIC_COOKIE`、`WY_MUSIC_COOKIE_FILE`、`WY_MUSIC_EXTENSIONS`、`WY_MUSIC_UPLOAD_EXTENSIONS`、`WY_MUSIC_EXCLUDE`、`WY_MUSIC_INTERVAL`、`WY_MUSIC_RATE_LIMIT`、`WY_MUSIC_TIMEOUT`、`WY_MUSIC_RETRIES`、`WY_MUSIC_MIN_SIZE`、`WY_MUSIC_MAX_UPLOAD_SIZE`、`WY_MUSIC_DRY_RUN`、`WY_MUSIC_LOG_LEVEL`。

`WY_MUSIC_DIRS` 可以用系统路径分隔符、逗号或空格分隔多个目录。

## 命令

```text
wy-music-cloud [-c CONFIG] [--music-dir DIR ...] [--log-level LEVEL] <命令>
```

| 命令 | 作用 |
| --- | --- |
| `login` | 扫码 / 手机号 / Cookie 登录并保存会话 |
| `logout` | 删除本地会话文件 |
| `check` | 检查登录状态，打印云盘歌曲数 |
| `scan [--json]` | 只扫描本地文件，不上传 |
| `sync [--dry-run] [--force] [--limit N]` | 执行一轮同步 |
| `daemon [--dry-run] [--force] [--no-run-now]` | 常驻轮询 |
| `status [--json]` | 查看状态库统计、最近运行、最近文件 |
| `config` | 打印生效配置（Cookie 已隐藏） |
| `reset-failed` | 把 `failed` 重置为待处理，下一轮重试 |

退出码：`0` 成功；`1` 本轮有文件失败；`2` 配置 / 登录 / API 错误；`130` 被 Ctrl-C 中断。

## 去重与状态

状态库是 `data/state.db`（SQLite，WAL 模式），核心表 `files` 记录：

- `path`：文件绝对路径（主键）；
- `size` / `mtime_ns`：判断文件是否变化；
- `md5`：本地去重与云端 check 的依据；
- `status`：`uploaded`、`skipped_local`、`skipped_remote`、`failed`、`missing` 等；
- `song_id` / `cloud_name`：上传成功后的云盘歌曲信息；
- `attempts` / `last_error`：失败次数与原因。

几个实际行为：

- **同一首歌放到两个目录**：只上传第一份，第二份标记 `skipped_local`；
- **本地状态库丢了**：重新算 MD5 后，云端 check 仍能挡住重复上传；
- **文件被替换（同名不同内容）**：size 或 mtime 变化会触发重新计算和上传；
- **云盘里手动删了歌**：本地状态仍是 `uploaded` 且文件没变，默认不会自动补传；用 `--force` 或删除对应状态行后重跑；
- **文件被移动/改名**：会当作新路径处理，但本地/云端 MD5 去重仍会拦住重复上传。

## 风险与限制

- 使用的是**非公开接口**，随时可能失效；协议集中在 [src/wy_music_cloud/client.py](src/wy_music_cloud/client.py)，细节见 [docs/PROTOCOL.md](docs/PROTOCOL.md)。
- 不要用主账号做高频、大批量上传；建议先 `--dry-run --limit` 小流量验证。
- 云盘容量、单文件大小、格式支持由网易云账号决定；本项目不做转码。
- 云盘是扁平结构，本项目不创建文件夹层级。
- 本项目不会修改你的本地音频标签，也不会下载歌曲。
- 飞牛应用首次启动需要 NAS 能访问 Docker Hub / PyPI 来构建镜像；构建完成后离线运行。
- 请只同步你有权上传的音乐。

## 开发与测试

```bash
pip install -e ".[dev]"
pytest
```

当前测试覆盖加密参数、Cookie、配置、扫描、标签、状态库、上传协议顺序、去重、失败隔离、dry-run、缺失文件、QR 登录、daemon 和 CLI。

构建飞牛应用：

```bash
bash scripts/build-fnos.sh
```

脚本需要 Python 3 和 [fnpack](https://developer.fnnas.com/)。Windows 下 fnpack 默认把文件打成 0666，构建脚本会调用 `scripts/fix-fpk-modes.py` 把 `cmd/` 下的脚本改为 0755；`app.tgz` 保持不变，因此 `manifest` 里的校验和仍然有效。

## 目录结构

```text
src/wy_music_cloud/
├── cli.py          # 命令行
├── client.py       # 网易云 weapi + NOS 上传（最脆弱的部分）
├── syncer.py       # 扫描 → 去重 → 上传 → 落库
├── state.py        # SQLite 状态库
├── scanner.py      # 目录扫描 / 过滤
├── metadata.py     # mutagen 标签
├── crypto.py       # weapi AES/RSA 加密
├── cookies.py      # Cookie 解析 / 脱敏
├── session.py      # 会话持久化
├── login.py        # 扫码 / 手机号登录
├── daemon.py       # 轮询循环
└── config.py       # YAML + 环境变量配置
deploy/docker/      # Dockerfile、entrypoint
deploy/fnos/        # 飞牛应用工程（manifest、wizard、生命周期脚本、compose）
scripts/            # 图标生成、fpk 构建与权限修复
docs/               # 协议说明、飞牛部署指南
tests/              # pytest
```

## 参考

- [Binaryify/NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi)：weapi 加密、云盘上传接口的主要参考；
- [lulu-ls/cloud-uploader](https://github.com/lulu-ls/cloud-uploader)：桌面端上传流程参考；
- [Binaryify/NeteaseCloudMusicApi#1851](https://github.com/Binaryify/NeteaseCloudMusicApi/pull/1851)：NOS 上传节点从硬编码 IP 切换到 LBS 的改动。

## License

MIT。
