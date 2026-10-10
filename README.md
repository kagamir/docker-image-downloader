# Docker Image Downloader

[![CI](https://github.com/kagamir/docker-image-downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/kagamir/docker-image-downloader/actions/workflows/ci.yml)

通过 Python 从 **Docker Hub / GHCR** 并发下载镜像层，对支持 HTTP Range 的大层进行多线程分片下载，校验后生成 Docker 归档或直接导入 Docker Engine。

适合需要临时使用代理下载镜像、无法修改或重启 Docker 服务的场景。下载流量由本程序发起，随后通过本地 `docker load` 导入。

## 功能

- 支持 `nginx:latest`、`docker.io/user/image:tag`、`ghcr.io/owner/image:tag`。
- 支持公开镜像和通过环境变量提供凭据的私有镜像。
- 多架构镜像选择：`linux/amd64`、`linux/arm64`、`linux/arm/v7` 等。
- 对大 Blob 探测 Range 支持；不支持时自动使用完整 Blob 下载，各层仍可并发。
- HTTP / HTTPS / SOCKS 代理，同时覆盖 Registry、认证端点和重定向后的 CDN 请求。
- 校验下载 Blob SHA256 和解压后的 Layer DiffID，支持 gzip、未压缩 tar 和可选 zstd。
- 可输出 `.tar` 归档供离线导入，也可直接通过管道执行 `docker load`。

## 安装

需要 **Python 3.10+**。直接导入需要可用的 Docker CLI、Docker Engine，以及当前用户访问 Engine 的权限；只生成归档无需安装 Docker。

```bash
git clone https://github.com/kagamir/docker-image-downloader.git
cd docker-image-downloader
python -m venv .venv
```

激活虚拟环境：

```bash
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

安装项目并启用全部可选依赖：

```bash
python -m pip install ".[all]"
docker-image-downloader --help
```

也可只安装核心依赖：`python -m pip install .`。SOCKS 与 zstd 可分别通过 `".[socks]"` 和 `".[zstd]"` 安装。

只使用脚本时：

```bash
python -m pip install -r requirements.txt
python registry_parallel_pull.py --help
# 按需启用 SOCKS / zstd
python -m pip install "requests[socks]" zstandard
```

目前无需从 PyPI 下载本项目，以上命令从当前仓库安装。

## 快速开始

下载并直接导入：

```bash
docker-image-downloader nginx:latest --workers 12
docker images nginx
```

通过临时 HTTP 代理下载并导入，Docker 服务无需重启：

```bash
docker-image-downloader nginx:latest --proxy http://127.0.0.1:7890
```

下载 GHCR 镜像（将示例路径替换为实际镜像）：

```bash
docker-image-downloader ghcr.io/owner/image:latest --proxy http://127.0.0.1:7890
```

通过 SOCKS5 代理并由代理端解析目标域名：

```bash
docker-image-downloader nginx:latest --proxy socks5h://127.0.0.1:1080
```

仅生成归档，再复制到其他机器导入：

```bash
docker-image-downloader alpine:latest --output alpine.tar
docker load -i alpine.tar
```

选择平台、线程数、分片大小和临时目录：

```bash
mkdir -p /data/image-tmp
docker-image-downloader nginx:latest \
  --platform linux/arm64 \
  --workers 8 \
  --chunk-mb 16 \
  --workdir /data/image-tmp \
  --output nginx-arm64.tar
```

以上示例均可用 `python registry_parallel_pull.py` 替换 `docker-image-downloader`。

## 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `image` | 必填 | Docker Hub 或 GHCR 镜像；省略 tag 时使用 `latest` |
| `--platform` | `linux/amd64` | `os/arch` 或 `os/arch/variant`，不自动跟随本机架构 |
| `--workers` | `8` | 下载线程池的并行请求上限，正整数 |
| `--chunk-mb` | `16` | 分片大小，单位 MiB；Blob 大小至少为两倍分片大小才探测 Range |
| `--proxy` | 环境变量 | 显式代理 URL；支持 `http`、`https`、`socks4`、`socks4a`、`socks5`、`socks5h` |
| `--output FILE` | 未设置 | 只生成 Docker 归档；未设置时直接导入 |
| `--workdir DIR` | 系统临时目录 | 已存在的临时文件父目录 |
| `--docker PATH` | `docker` | Docker CLI 路径，例如 Windows 的 `docker.exe` |

退出码：成功为 `0`，运行失败为 `1`，参数错误为 `2`。

## 代理配置

`--proxy` 会为 HTTP 与 HTTPS 请求指定同一个代理。未指定时，Requests 使用 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 等环境配置。

Linux / macOS 可以只对一次命令设置代理：

```bash
HTTPS_PROXY=http://127.0.0.1:7890 docker-image-downloader nginx:latest
```

Windows PowerShell 可以直接使用参数：

```powershell
docker-image-downloader nginx:latest --proxy http://127.0.0.1:7890
```

代理地址指向**运行 Python 的环境**。在 WSL 或容器中，`127.0.0.1` 未必是宿主机的代理地址。
HTTP 代理也可以转发 HTTPS 请求，例如使用 `http://127.0.0.1:7890` 下载 HTTPS Registry。
代理若需要账号密码，可使用 `http://user:password@host:port`；特殊字符需 URL 编码。

## 私有镜像认证

Docker Hub 设置 `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN`，GHCR 设置 `GHCR_USERNAME` / `GHCR_TOKEN`，同一组必须同时设置。
本程序不读取 `docker login` 保存的 `~/.docker/config.json`。

Linux / macOS 示例，交互输入令牌以避免写入命令历史：

```bash
export GHCR_USERNAME=your-github-username
read -r -s -p 'GHCR token: ' GHCR_TOKEN; echo
export GHCR_TOKEN
docker-image-downloader ghcr.io/owner/private-image:latest
unset GHCR_TOKEN
```

PowerShell 示例：

```powershell
$env:GHCR_USERNAME = 'your-github-username'
$env:GHCR_TOKEN = [System.Net.NetworkCredential]::new('', (Read-Host 'GHCR token' -AsSecureString)).Password
docker-image-downloader ghcr.io/owner/private-image:latest
Remove-Item Env:GHCR_TOKEN
```

GHCR 使用具有 `read:packages` 权限且能访问目标包的 classic PAT；组织启用 SSO 时还需对应授权。
Docker Hub 使用具备镜像读取权限的访问令牌。匿名下载仍受上游访问与限流规则约束。

## 工作流程与限制

1. 获取 Manifest，根据指定平台选择具体镜像。
2. 下载 Config 和各层 Blob；大层支持 Range 时分片并发，不支持时按完整层下载。
3. 校验 Blob SHA256，解压各层并校验 Config 中的 DiffID。
4. 生成含 `manifest.json`、Config 和 `layer.tar` 的 Docker 归档；写入文件或传给 `docker load`。

- 目前仅支持 Docker Hub 和 GHCR、tag 引用、Docker schema2 / OCI image；不支持 `@sha256` 引用、其他 Registry 或 OCI 任意制品。
- 导入单个所选平台；不会把完整多架构索引、签名或附件一同导入，也不会验证镜像签名。
- 分片并发是否提速取决于上游 Range 支持、代理、网络和限流。默认仅打印阶段状态，没有实时速度或百分比进度条。
- 临时目录保存下载的压缩层和解压层。导出时还需保存最终归档，应预留这些文件的总空间及余量。
- 临时目录在正常完成或异常退出后清理；当前没有跨运行缓存或断点续传。
- `--output` 成功后会替换同名文件；失败时保留原文件，不留下半成品归档。
- 若只生成归档，可在 Windows、macOS、Linux 运行；实际导入与运行仍要求目标 Docker Engine 支持所选平台。

## 常见问题

| 现象 | 处理方式 |
| --- | --- |
| 找不到 Docker CLI / 无法连接 Engine | 使用 `--output image.tar` 先生成文件；检查 Docker 安装、运行状态和权限 |
| 401 / 403 / 404 | 检查镜像路径、tag、公开状态和令牌权限；私有镜像可能返回 404 |
| 429 或重试后失败 | 上游限流；降低 `--workers`、使用有权限的账号，稍后重试 |
| 大层显示“普通下载” | 上游未按要求返回 Range，或层大小未达到分片阈值 |
| SHA256 / DiffID 校验失败 | 数据不完整或发生变化；检查代理与存储后重新下载 |
| `Missing dependencies for SOCKS support` | 安装 `python -m pip install ".[socks]"` |
| 提示需要 zstd | 安装 `python -m pip install ".[zstd]"` |
| 临时目录空间不足 | 用 `--workdir` 指定空间足够的已存在目录 |
| 平台不存在 | 按镜像实际提供的架构选择 `--platform` |

## 开发

见 [CONTRIBUTING.md](CONTRIBUTING.md)。GitHub Actions 检查 Linux / Windows / macOS，覆盖 Python 3.10、3.12、3.14；Linux Python 3.12 额外验证真实 Docker Engine 导入。

## 许可证

[MIT](LICENSE)。
