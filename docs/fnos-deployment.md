# 飞牛 NAS（fnOS）部署指南

本文提供两种部署方式：

1. **原生飞牛应用（推荐）**：安装 `wy-music-cloud.fpk`，在应用中心完成安装向导、启停和升级；
2. **Docker Compose**：直接从源码构建，适合开发、调试或自行改代码。

两种方式使用同一套程序，去重状态都保存在 Docker 数据卷/目录中。命令以 `/vol1/1000` 为示例，`1000` 是 fnOS 用户 ID，请按实际修改。

## 方式一：原生飞牛应用（fpk）

### 1. 获取 fpk

从 [Releases](https://github.com/jiu-zui-90817/wy-music-cloud/releases) 下载 `wy-music-cloud.fpk`。

也可以自行构建：

```bash
# 需要 fnOS 官方 fnpack，放到 .tools/ 或加入 PATH
bash scripts/build-fnos.sh
# 产物：dist/wy-music-cloud.fpk
```

### 2. 安装

打开飞牛 NAS 应用中心 → 手动安装 → 选择 `wy-music-cloud.fpk`。

安装向导会要求填写：

| 项目 | 默认值 | 说明 |
| --- | --- | --- |
| 音乐目录 | `/vol1/1000/Music` | NAS 上的绝对路径，必须已存在；容器内只读挂载到 `/music` |
| 扫描间隔 | `3600` | 秒，最小 60 |
| 时区 | `Asia/Shanghai` | 传给容器 |
| 试运行 | `false` | 第一次可先设为 `true` |
| 运行 UID / GID | `1000` / `1000` | 用 `id` 查看音乐目录属主 |

安装后配置写入应用目录的 `docker/.env`，卸载/升级不会删除。

### 3. 首次启动

应用中心启动后，Docker Compose 会在 NAS 上构建镜像（基础镜像 `python:3.12-slim` 需要联网拉取）。第一次可能需要几分钟，期间应用状态可能短暂显示未运行，构建完成后容器会自动启动。

```bash
sudo docker logs -f wy-music-cloud
```

如果 NAS 无法访问 Docker Hub，构建会失败。请给 Docker 配置镜像加速或代理后，在应用中心停止再启动。

### 4. 登录（SSH，一次即可）

应用没有桌面界面，首次需要在 NAS 上执行一次登录。应用安装目录通常是 `/var/apps/wy-music-cloud`，以实际为准；安装回调也会把登录提示写到应用数据目录的 `login.txt`。

```bash
cd /var/apps/wy-music-cloud/docker

# 扫码：终端会显示二维码，同时写入容器内 /data/qr.png
sudo docker compose -p wy-music-cloud run --rm wy-music-cloud login --qr-png /data/qr.png
```

用网易云音乐 App 扫码并确认。登录态保存在 Docker 数据卷 `wy-music-cloud-data` 中，升级/重装应用不会丢失。

如果找不到应用目录：

```bash
sudo find /var/apps -maxdepth 5 -name docker-compose.yaml | grep wy-music-cloud
```

也可以粘贴 Cookie：

```bash
sudo docker compose -p wy-music-cloud run --rm wy-music-cloud login --cookie "MUSIC_U=...; __csrf=..."
```

或手机号登录（密码会进入 shell 历史，不推荐）：

```bash
sudo docker compose -p wy-music-cloud run --rm wy-music-cloud login --phone 13800000000 --password '你的密码'
```

### 5. 验证登录

```bash
sudo docker compose -p wy-music-cloud run --rm wy-music-cloud check
```

成功时会打印昵称、用户 ID 和云盘歌曲数。

### 6. 小流量试跑

```bash
sudo docker compose -p wy-music-cloud run --rm wy-music-cloud sync --dry-run --limit 5
```

确认输出中的路径、大小、去重判断都正常，再真正上传：

```bash
sudo docker compose -p wy-music-cloud run --rm wy-music-cloud sync --limit 5
```

第一次建议只传几首，确认云盘里能看到、能播放，再回到应用中心把“试运行”改为 `false`。

### 7. 常驻与日志

应用中心启动应用后，容器以 `restart: unless-stopped` 常驻，默认启动后立刻跑一轮，之后每隔配置的秒数跑一轮。

```bash
sudo docker ps --filter name=wy-music-cloud
sudo docker logs -f --tail=200 wy-music-cloud
```

停止/启动请优先用应用中心；也可以：

```bash
cd /var/apps/wy-music-cloud/docker
sudo docker compose -p wy-music-cloud stop
sudo docker compose -p wy-music-cloud up -d
```

### 8. 修改配置

在应用中心打开本应用的“设置”，修改音乐目录、扫描间隔、时区或试运行，然后**停止再启动应用**让容器重建。配置会写入 `docker/.env`，不会丢。

如果手动编辑了 `docker/.env`，同样需要停止/启动应用。

修改 UID/GID 后，已有镜像不会自动重建。需要：

```bash
cd /var/apps/wy-music-cloud/docker
sudo docker compose -p wy-music-cloud build --no-cache
sudo docker compose -p wy-music-cloud up -d
```

### 9. 升级

下载新版本 `.fpk`，在应用中心手动安装覆盖。安装/升级回调会保留：

- `docker/.env` 中的配置；
- Docker 数据卷 `wy-music-cloud-data` 中的登录态和去重状态。

升级后停止/启动一次应用。

### 10. 备份与恢复

需要备份的是 Docker 数据卷（登录态 + 去重状态库）和 `docker/.env`。

```bash
cd /var/apps/wy-music-cloud/docker
sudo tar czf wy-music-cloud-env-$(date +%F).tar.gz .env

sudo docker run --rm \
  -v wy-music-cloud-data:/data:ro \
  -v "$PWD":/backup \
  alpine tar czf /backup/wy-music-cloud-data-$(date +%F).tar.gz -C /data .
```

恢复：

```bash
cd /var/apps/wy-music-cloud/docker
sudo tar xzf wy-music-cloud-env-YYYY-MM-DD.tar.gz
sudo docker run --rm \
  -v wy-music-cloud-data:/data \
  -v "$PWD":/backup \
  alpine sh -c 'rm -rf /data/* && tar xzf /backup/wy-music-cloud-data-YYYY-MM-DD.tar.gz -C /data'
```

然后启动应用。恢复前请先停止应用。

### 11. 卸载

在应用中心卸载。卸载回调**故意保留**数据卷 `wy-music-cloud-data`，避免误删登录态和去重状态。

确认不再需要后，手动删除：

```bash
sudo docker volume rm wy-music-cloud-data
```

### 12. 常见问题（fpk）

#### 首次启动一直显示未运行

先看构建日志：

```bash
sudo docker logs wy-music-cloud
```

如果容器还不存在，说明镜像仍在构建或构建失败。到应用目录手动执行：

```bash
cd /var/apps/wy-music-cloud/docker
sudo docker compose -p wy-music-cloud build
```

根据报错检查 Docker Hub 网络、DNS、代理或磁盘空间。

#### 找不到登录入口

本应用没有 Web UI。按第 4 节用 SSH 进入应用目录执行 `docker compose run ... login`。登录提示也会写到应用数据目录的 `login.txt`，可执行：

```bash
sudo find /var/apps/wy-music-cloud -name login.txt
```

#### 二维码看不到

`docker compose run` 的终端可能不显示 ASCII 二维码。用 `--qr-png /data/qr.png` 保存到数据卷后，再从宿主机取出：

```bash
sudo docker run --rm -v wy-music-cloud-data:/data:ro alpine cat /data/qr.png > qr.png
```

然后把 `qr.png` 下载到手机能访问的位置扫码。

#### 上传失败 / 无法出网

确认容器能访问：

- `music.163.com`
- `interface.music.163.com`
- `wanproxy.127.net`

NAS 上有代理时，在 `docker/.env` 加入 `HTTP_PROXY` / `HTTPS_PROXY`，或修改 compose 的 `environment`，然后重启应用。

## 方式二：Docker Compose（源码部署）

适合开发、调试或想自行改代码。需要 NAS 已安装 Docker / Docker Compose，并能 SSH。

### 1. 确认环境

```bash
ssh 你的用户名@NAS地址
sudo -i
docker --version
docker compose version   # 如果报错，试 docker-compose --version
id 你的用户名
```

`id` 输出里的 `uid=` 是 PUID，`gid=` 是 PGID。飞牛常见是 `1000:1000`。

### 2. 建目录并获取项目

```bash
mkdir -p /vol1/1000/docker
cd /vol1/1000/docker
git clone https://github.com/jiu-zui-90817/wy-music-cloud.git wy-music-cloud-src
cd wy-music-cloud-src
```

也可以把整个项目目录（不含 `data/`、`.deps/`、`.venv/`）通过文件管理器上传到 `/vol1/1000/docker/wy-music-cloud-src/`。

### 3. 配置

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`：

```yaml
music_dirs:
  - /music

data_dir: /data

interval: 3600
```

这里的路径是**容器内路径**，不是 NAS 上的 `/vol1/...`。宿主机目录在 compose 中挂载。

### 4. 修改 compose 挂载

编辑根目录的 `docker-compose.yml`，把音乐目录换成你的：

```yaml
    volumes:
      - ./config.yaml:/config/config.yaml:ro
      - ./data:/data
      - /vol1/1000/Music:/music:ro
```

如果 UID/GID 不是 1000，在 `docker-compose.yml` 同目录建 `.env`：

```bash
PUID=1001
PGID=1001
```

或构建时显式传参：

```bash
docker compose build --build-arg PUID=1001 --build-arg PGID=1001
```

### 5. 构建

```bash
docker compose build
```

第一次构建要拉取 `python:3.12-slim` 并安装依赖，几分钟属正常。

### 6. 数据目录权限

容器默认以镜像内 `app` 用户（默认 1000:1000）运行。`data/` 必须可写：

```bash
mkdir -p /vol1/1000/docker/wy-music-cloud-src/data
chown -R 1000:1000 /vol1/1000/docker/wy-music-cloud-src/data
chmod 700 /vol1/1000/docker/wy-music-cloud-src/data
```

如果 UID 不是 1000，把上面的 1000 换掉。**不要**用 `chmod 777` 图省事。

### 7. 登录

```bash
docker compose run --rm wy-music-cloud login --qr-png /data/qr.png
```

打开 `data/qr.png`，用网易云音乐 App 扫码并在手机上确认。成功后：

```text
data/session.json   # 登录态，权限 0600
```

也可以粘贴 Cookie：

```bash
docker compose run --rm wy-music-cloud login --cookie "MUSIC_U=...; __csrf=..."
```

或手机号登录：

```bash
docker compose run --rm wy-music-cloud login --phone 13800000000 --password '你的密码'
```

密码会进入 shell 历史，不推荐；扫码最稳。

验证登录：

```bash
docker compose run --rm wy-music-cloud check
```

### 8. 小流量试跑

```bash
docker compose run --rm wy-music-cloud sync --dry-run --limit 5
```

确认输出正常后，再真正上传：

```bash
docker compose run --rm wy-music-cloud sync --limit 5
```

### 9. 常驻运行

```bash
docker compose up -d
docker compose ps
docker compose logs -f --tail=100
```

默认启动后立刻跑一轮，之后每隔 `interval` 秒跑一轮。停止：

```bash
docker compose down
```

### 10. 用飞牛“计划任务”代替常驻（可选）

如果不想一直开着容器，可以在 fnOS 的计划任务里定时执行一次 `sync`。

在计划任务里新建一个任务，例如每 30 分钟：

```bash
cd /vol1/1000/docker/wy-music-cloud-src && /usr/bin/docker compose run --rm wy-music-cloud -c /config/config.yaml sync >> data/cron.log 2>&1
```

注意事项：

- **不要同时**运行 daemon 和计划任务，两个进程可能同时处理同一个文件；
- 计划任务需要能访问 Docker socket，通常用 root 或 docker 组用户；
- 日志写到 `data/cron.log`，注意定期清理；
- 计划任务里的 `docker` 路径用 `which docker` 确认，可能是 `/usr/bin/docker`。

### 11. 升级

```bash
cd /vol1/1000/docker/wy-music-cloud-src
git pull
docker compose build
docker compose up -d
```

`data/` 不受影响。升级前建议备份。

### 12. 备份与恢复

需要备份的只有：

```text
config.yaml
data/state.db
data/session.json
data/last_run.json   # 可选，只是统计
```

```bash
docker compose down
tar czf wy-music-cloud-backup-$(date +%F).tar.gz config.yaml data
```

恢复时解压回原目录，确认 `data/` 属主正确，再 `docker compose up -d`。

### 13. 常见问题（Docker）

#### `Permission denied` / 无法写 `/data`

容器用户 UID 与宿主机目录属主不一致。执行 `id` 看 UID，然后：

```bash
chown -R 你的UID:你的GID data
```

或用 `.env` 里的 PUID/PGID 重新构建。

#### 登录失效（错误码 301 / 提示未登录）

Cookie 过期或被踢下线，重新执行第 7 步扫码。

#### 二维码看不到

`docker compose run` 的终端可能不显示 ASCII 二维码；用 `--qr-png /data/qr.png` 保存成图片再打开。

#### 上传失败 `needUpload` / token / NOS 相关错误

先看日志：

```bash
docker compose logs --tail=200
```

确认容器能出网：`music.163.com`、`interface.music.163.com`、`wanproxy.127.net`。NAS 上有代理时，给容器加 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量（httpx 默认读取）。

#### 云盘里已有歌，但本地还显示待上传

默认会在上传前调用云端 check；如果希望强制重新检查，用 `sync --force`。

#### 文件在云盘被删了，本地不补传

本地状态还是 `uploaded` 且文件没变，默认跳过。用 `--force` 重新检查，或删除 `state.db` 里对应记录。

#### 容器启动就退出

```bash
docker compose logs wy-music-cloud
```

常见原因是 `config.yaml` 路径不对、YAML 缩进错误、`music_dirs` 为空、登录态缺失。

## 安全建议

- `data/` 权限 700，`session.json` 含登录态，不要提交到 Git；
- 不要把 Cookie 写进公开的 compose 文件；
- 音乐目录只读挂载；
- 先用小号或低频账号验证，再考虑主账号；
- 定期备份数据卷或 `data/`。
