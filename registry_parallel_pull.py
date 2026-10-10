#!/usr/bin/env python3
"""Parallel Docker Hub / GHCR image downloader (Docker/OCI -> docker load archive).

Requirements: pip install requests  (pip install zstandard for zstd layers).
SOCKS proxies: pip install "requests[socks]".
Without --proxy, requests automatically honors HTTP_PROXY / HTTPS_PROXY / NO_PROXY.
Public images are anonymous. For private images set one credential pair:
  Docker Hub: DOCKERHUB_USERNAME / DOCKERHUB_TOKEN
  GitHub CR:  GHCR_USERNAME / GHCR_TOKEN  (PAT classic with read:packages)

Examples:
  python registry_parallel_pull.py nginx:latest --workers 12
  python registry_parallel_pull.py ghcr.io/owner/image:latest --output image.tar
  python registry_parallel_pull.py ghcr.io/owner/image:latest --proxy http://127.0.0.1:7890
  python registry_parallel_pull.py nginx:latest --proxy socks5h://127.0.0.1:1080
"""
from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from typing import BinaryIO
from urllib.parse import quote, urlsplit

import requests
from urllib3.exceptions import HTTPError as StreamError

REGISTRIES = {
    "docker.io": "https://registry-1.docker.io",
    "ghcr.io": "https://ghcr.io",
}
TOKEN_HOSTS = {
    "docker.io": {"auth.docker.io"},
    "ghcr.io": {"ghcr.io"},
}
ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
RETRIES = 4
BUFFER = 1024 * 1024
TRANSIENT = {429, 500, 502, 503, 504}
RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


def die(message: str):
    raise RuntimeError(message)


def parse_image(image: str):
    """Return (registry_name, repository, reference, docker_archive_tag)."""
    if image.startswith("ghcr.io/"):
        registry = "ghcr.io"
        image = image[len("ghcr.io/"):]
    else:
        registry = "docker.io"
        for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
            if image.startswith(prefix):
                image = image[len(prefix):]
                break
        else:
            first = image.split("/", 1)[0]
            if "/" in image and ("." in first or ":" in first or first == "localhost"):
                die(f"当前仅支持 docker.io 和 ghcr.io，不支持其他 Registry: {first}")
    if "@" in image:
        die("当前版本仅支持 repo:tag，不支持 @sha256 引用")
    leaf = image.rsplit("/", 1)[-1]
    if ":" in leaf:
        repo, tag = image.rsplit(":", 1)
    else:
        repo, tag = image, "latest"
    component = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
    if (not repo or not tag
            or not re.fullmatch(rf"{component}(?:/{component})*", repo)
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag)):
        die("镜像格式无效，例如 nginx:latest 或 ghcr.io/owner/image:latest")
    if registry == "ghcr.io" and "/" not in repo:
        die("GHCR 镜像必须包含 owner/image，例如 ghcr.io/owner/image:latest")
    if registry == "docker.io" and "/" not in repo:
        repo = "library/" + repo
    if registry == "ghcr.io":
        local_name = f"ghcr.io/{repo}:{tag}"
    else:
        local_name = (repo[len("library/"):] if repo.startswith("library/") else repo) + ":" + tag
    return registry, repo, tag, local_name


def parse_platform(platform: str) -> list[str]:
    components = platform.split("/")
    if (len(components) not in (2, 3)
            or any(not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", part) for part in components)):
        die("平台格式必须为 os/arch 或 os/arch/variant，如 linux/amd64")
    return components


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for data in iter(lambda: f.read(BUFFER), b""):
            digest.update(data)
    return "sha256:" + digest.hexdigest()


def validate_digest(digest: str):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        die(f"仅支持 sha256 digest: {digest}")


class RegistryClient:
    def __init__(self, registry: str, repository: str, proxy: str | None = None):
        self.registry = registry
        self.registry_url = REGISTRIES[registry]
        self.repository = repository
        # Apply the same proxy to Registry, authentication and redirected CDN Blob URLs.
        # When unset, requests uses HTTP_PROXY/HTTPS_PROXY/NO_PROXY from environment.
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.lock = threading.Lock()
        # A registry advertises its own token endpoint via WWW-Authenticate.
        # This is required for GHCR and also works with Docker Hub.
        self.token = None
        self.challenge = None

    def new_token(self, challenge: str) -> str:
        scheme, _, raw_params = challenge.partition(" ")
        if scheme.lower() != "bearer":
            die(f"不支持的 Registry 认证方式: {scheme}")
        params = requests.utils.parse_dict_header(raw_params)
        realm = params.get("realm")
        if not realm:
            die("Registry Bearer challenge 缺少 realm")
        parsed = urlsplit(realm)
        if (parsed.scheme != "https" or parsed.hostname not in TOKEN_HOSTS[self.registry]
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            die(f"Registry 返回了不受信任的认证端点: {realm}")

        prefix = "GHCR" if self.registry == "ghcr.io" else "DOCKERHUB"
        user = os.environ.get(f"{prefix}_USERNAME")
        pat = os.environ.get(f"{prefix}_TOKEN")
        auth = None
        if user and pat:
            auth = (user, pat)
        elif bool(user) != bool(pat):
            die(f"{prefix}_USERNAME 和 {prefix}_TOKEN 必须同时设置")

        query = {
            "scope": f"repository:{self.repository}:pull",
        }
        if params.get("service"):
            query["service"] = params["service"]
        with requests.get(realm, params=query, auth=auth, timeout=(15, 40),
                          proxies=self.proxies) as r:
            r.raise_for_status()
            data = r.json()
        token = data.get("token") or data.get("access_token")
        if not token:
            die("认证端点没有返回 Bearer token")
        return token

    def get(self, url: str, *, headers=None, stream=False):
        # Acquiring the initial token is one extra request; allow separate
        # retries for transient errors and an expired token.
        auth_retries = 0
        transient_retries = 0
        while transient_retries < RETRIES and auth_retries < 3:
            with self.lock:
                old = self.token
            request_headers = {"Accept-Encoding": "identity"}
            if old is not None:
                request_headers["Authorization"] = f"Bearer {old}"
            if headers:
                request_headers.update(headers)
            try:
                r = requests.get(url, headers=request_headers, stream=stream,
                                 timeout=(15, 120), allow_redirects=True,
                                 proxies=self.proxies)
                if r.status_code == 401:
                    challenge = r.headers.get("WWW-Authenticate")
                    r.close()
                    with self.lock:
                        if self.token == old:
                            if challenge:
                                self.challenge = challenge
                            if not self.challenge:
                                die("Registry 返回 401 但没有 WWW-Authenticate")
                            self.token = self.new_token(self.challenge)
                    auth_retries += 1
                    continue
                if r.status_code in TRANSIENT and transient_retries + 1 < RETRIES:
                    r.close()
                    time.sleep(min(2 ** transient_retries, 8))
                    transient_retries += 1
                    continue
                try:
                    r.raise_for_status()
                except requests.RequestException:
                    r.close()
                    raise
                return r
            except requests.RequestException:
                transient_retries += 1
                if transient_retries == RETRIES:
                    raise
                time.sleep(min(2 ** (transient_retries - 1), 8))
        die(f"请求重试失败（认证或网络错误）：{url}")

    def manifest(self, reference: str):
        url = f"{self.registry_url}/v2/{self.repository}/manifests/{quote(reference, safe=':')}"
        with self.get(url, headers={"Accept": ACCEPT}) as response:
            result = response.json()
        if "errors" in result:
            die(f"Manifest 错误: {result['errors']}")
        return result

    def blob_url(self, digest: str):
        validate_digest(digest)
        return f"{self.registry_url}/v2/{self.repository}/blobs/{digest}"


def pick_manifest(client: RegistryClient, tag: str, platform: str):
    components = parse_platform(platform)
    m = client.manifest(tag)
    if "manifests" in m:
        matches = []
        for desc in m["manifests"]:
            p = desc.get("platform") or {}
            if p.get("os") == components[0] and p.get("architecture") == components[1] and (
                    len(components) < 3 or p.get("variant") == components[2]):
                matches.append(desc)
        if not matches:
            die(f"镜像中没有匹配的平台: {platform}")
        m = client.manifest(matches[0]["digest"])
    if "layers" not in m or "config" not in m:
        die("不支持的 Manifest 类型（仅支持 Docker schema2/OCI image）")
    return m


def check_range(client: RegistryClient, digest: str, size: int) -> bool:
    # Registry/CDN may ignore Range. Probe without buffering the whole response.
    with client.get(client.blob_url(digest), headers={"Range": "bytes=0-0"}, stream=True) as r:
        if r.status_code != 206:
            return False
        m = RANGE_RE.fullmatch(r.headers.get("Content-Range", ""))
        return bool(m and tuple(map(int, m.groups())) == (0, 0, size))


def transfer(client: RegistryClient, digest: str, file: Path, total: int,
             start: int | None = None, end: int | None = None):
    url = client.blob_url(digest)
    headers = {"Range": f"bytes={start}-{end}"} if start is not None else None
    expected = end - start + 1 if start is not None else total
    for attempt in range(RETRIES):
        try:
            with client.get(url, headers=headers, stream=True) as r:
                if start is not None:
                    if r.status_code != 206:
                        die(f"服务端未按 Range 返回 206: {digest} HTTP {r.status_code}")
                    m = RANGE_RE.fullmatch(r.headers.get("Content-Range", ""))
                    if not m or tuple(map(int, m.groups())) != (start, end, total):
                        die(f"Content-Range 与请求不符: {r.headers.get('Content-Range')}")
                elif r.status_code != 200:
                    die(f"完整 Blob 下载应返回 200，实际 {r.status_code}")
                received = 0
                # Separate file descriptors let multiple workers write disjoint byte ranges.
                with file.open("r+b" if start is not None else "wb") as output:
                    if start is not None:
                        output.seek(start)
                    # raw.stream(..., decode_content=False) prevents HTTP transparent
                    # decompression from corrupting content-addressed Blob bytes.
                    for buf in r.raw.stream(BUFFER, decode_content=False):
                        received += len(buf)
                        if received > expected:
                            die(f"Blob 响应超出预期长度: {digest}")
                        output.write(buf)
                if received != expected:
                    die(f"Blob 长度不正确: {digest}, {received} != {expected}")
            return
        except (requests.RequestException, StreamError, OSError, RuntimeError) as exc:
            if attempt + 1 == RETRIES:
                raise RuntimeError(f"分片下载失败 {digest} {start}-{end}: {exc}") from exc
            time.sleep(min(2 ** attempt, 8))


def acquire_blobs(client: RegistryClient, descriptors: list, directory: Path,
                  workers: int, chunk_size: int):
    directory.mkdir(parents=True, exist_ok=True)
    all_descriptors = {desc["digest"]: desc for desc in descriptors}
    tasks = []
    waiting = []
    for digest, desc in all_descriptors.items():
        validate_digest(digest)
        size = int(desc["size"])
        if size < 0:
            die(f"Blob 大小不能为负数: {digest}")
        target = directory / digest.replace(":", "_")
        if target.is_file() and target.stat().st_size == size and sha256_file(target) == digest:
            print(f"复用缓存 {digest[7:19]} ({size / 1048576:.1f} MiB)", flush=True)
            continue
        target.unlink(missing_ok=True)
        partial = Path(str(target) + ".partial")
        partial.unlink(missing_ok=True)
        use_range = size >= chunk_size * 2 and check_range(client, digest, size)
        if use_range:
            with partial.open("wb") as f:
                f.truncate(size)
            for start in range(0, size, chunk_size):
                end = min(size - 1, start + chunk_size - 1)
                tasks.append((digest, partial, size, start, end))
            print(f"分片下载 {digest[7:19]} ({size / 1048576:.1f} MiB)", flush=True)
        else:
            tasks.append((digest, partial, size, None, None))
            print(f"普通下载 {digest[7:19]} ({size / 1048576:.1f} MiB)", flush=True)
        waiting.append((digest, partial, target, size))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(transfer, client, *job) for job in tasks]
        try:
            for f in concurrent.futures.as_completed(futures):
                f.result()
        except Exception:
            for f in futures:
                f.cancel()
            raise

    for digest, partial, target, size in waiting:
        if partial.stat().st_size != size or sha256_file(partial) != digest:
            die(f"SHA256 校验失败: {digest}")
        partial.replace(target)
        print(f"校验通过 {digest[7:19]}", flush=True)
    return {d: directory / d.replace(":", "_") for d in all_descriptors}


def unpack_layer(source: Path, dest: Path, media_type: str, diff_id: str):
    validate_digest(diff_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256()
    if media_type.endswith("tar+gzip") or media_type.endswith("tar.gzip"):
        reader = gzip.open(source, "rb")
    elif media_type.endswith("tar+zstd"):
        try:
            import zstandard  # type: ignore
        except ImportError:
            die("当前镜像使用 zstd 层，请先 pip install zstandard")
        compressed_file = source.open("rb")
        # Python zstandard reader owns the source through the context manager.
        reader = zstandard.ZstdDecompressor().stream_reader(compressed_file)
    elif media_type.endswith(".tar") or media_type.endswith("+tar"):
        reader = source.open("rb")
    else:
        die(f"不支持的层压缩格式: {media_type}")
    with reader as inp, dest.open("wb") as out:
        while True:
            data = inp.read(BUFFER)
            if not data:
                break
            sha.update(data)
            out.write(data)
    if "sha256:" + sha.hexdigest() != diff_id:
        dest.unlink(missing_ok=True)
        die(f"解压层 DiffID 校验失败: {source.name}")


def make_archive(output: BinaryIO, config_file: Path, config_arcname: str,
                 layers: list[Path], local_tag: str):
    manifest = [{
        "Config": config_arcname,
        "RepoTags": [local_tag],
        "Layers": [f"layers/{i}/layer.tar" for i in range(len(layers))],
    }]
    body = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    with tarfile.open(fileobj=output, mode="w|") as archive:
        archive.add(config_file, arcname=config_arcname, recursive=False)
        for i, layer in enumerate(layers):
            archive.add(layer, arcname=f"layers/{i}/layer.tar", recursive=False)
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(body)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(body))


def run(args):
    components = parse_platform(args.platform)
    registry, repo, tag, local_tag = parse_image(args.image)
    if not args.output and shutil.which(args.docker) is None:
        die(f"找不到 Docker CLI: {args.docker}；请安装 Docker，或使用 --output 生成归档")
    client = RegistryClient(registry, repo, proxy=args.proxy)
    manifest = pick_manifest(client, tag, args.platform)
    print(f"镜像: {local_tag}  平台: {args.platform}", flush=True)
    config_desc = manifest["config"]
    layers = manifest["layers"]
    with tempfile.TemporaryDirectory(prefix="dh-parallel-", dir=args.workdir) as tmp:
        tmp_dir = Path(tmp)
        files = acquire_blobs(client, [config_desc, *layers], tmp_dir / "blobs",
                              args.workers, args.chunk_mb * 1048576)
        cfg_path = files[config_desc["digest"]]
        cfg = json.loads(cfg_path.read_bytes())
        diff_ids = cfg.get("rootfs", {}).get("diff_ids", [])
        if len(diff_ids) != len(layers):
            die("Config 的 rootfs.diff_ids 与 Manifest Layers 数量不匹配")
        if cfg.get("os") != components[0] or cfg.get("architecture") != components[1]:
            die("Config 中的 OS/Architecture 与要求的平台不符")
        if len(components) == 3 and cfg.get("variant") and cfg["variant"] != components[2]:
            die("Config 中的 Variant 与要求的平台不符")
        tar_layers = []
        for i, desc in enumerate(layers):
            path = tmp_dir / "uncompressed" / f"{i}.tar"
            unpack_layer(files[desc["digest"]], path, desc["mediaType"], diff_ids[i])
            tar_layers.append(path)
            print(f"解压并校验 Layer {i + 1}/{len(layers)}", flush=True)
        config_name = config_desc["digest"].split(":", 1)[1] + ".json"
        if args.output:
            destination = Path(args.output).expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Replace only a completed archive, preserving an existing file on failure.
            pending = None
            try:
                with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.",
                                                 suffix=".tmp", delete=False) as out:
                    pending = Path(out.name)
                    make_archive(out, cfg_path, config_name, tar_layers, local_tag)
                pending.replace(destination)
            finally:
                if pending is not None:
                    pending.unlink(missing_ok=True)
            print(f"已生成 Docker Archive: {destination}")
        else:
            print("开始传输归档到 docker load ...", flush=True)
            proc = subprocess.Popen([args.docker, "load"], stdin=subprocess.PIPE)
            try:
                assert proc.stdin is not None
                make_archive(proc.stdin, cfg_path, config_name, tar_layers, local_tag)
                proc.stdin.close()
                result = proc.wait()
                if result != 0:
                    die(f"docker load 执行失败，退出代码 {result}")
            except BaseException:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                raise
            print(f"已导入镜像: {local_tag}")


def main():
    parser = argparse.ArgumentParser(description="Docker Hub/GHCR 并发分片下载，生成 docker save 兼容归档并 docker load")
    parser.add_argument("image", help="如 nginx:latest、user/repo:tag、ghcr.io/owner/image:tag")
    parser.add_argument("--platform", default="linux/amd64", help="默认 linux/amd64")
    parser.add_argument("--workers", type=int, default=8, help="并行请求数，默认 8")
    parser.add_argument("--chunk-mb", type=int, default=16, help="分片大小 MiB，默认 16")
    parser.add_argument("--output", metavar="FILE", help="仅生成归档文件，而非直接 docker load")
    parser.add_argument("--workdir", metavar="DIR", help="临时文件上级目录（需足够空闲空间）")
    parser.add_argument("--docker", default="docker", help="Docker CLI 可执行文件路径")
    parser.add_argument("--proxy", metavar="URL",
                        help="所有 Registry、Token、CDN 下载请求均经此代理，支持 http(s):// 与 socks5(h)://；默认读取 HTTP_PROXY/HTTPS_PROXY 环境变量")
    args = parser.parse_args()
    if args.workers < 1 or args.chunk_mb < 1:
        parser.error("--workers 和 --chunk-mb 必须为正整数")
    try:
        parse_platform(args.platform)
    except RuntimeError as exc:
        parser.error(str(exc))
    if args.proxy:
        parsed = urlsplit(args.proxy)
        if parsed.scheme not in ("http", "https", "socks4", "socks4a", "socks5", "socks5h") or not parsed.hostname:
            parser.error("--proxy 必须为完整代理 URL，例如 http://127.0.0.1:7890 或 socks5h://127.0.0.1:1080")
        try:
            _ = parsed.port  # Verify port syntax before starting downloads.
        except ValueError as exc:
            parser.error(f"--proxy 端口错误: {exc}")
    if args.workdir and not Path(args.workdir).is_dir():
        parser.error("--workdir 必须是存在的目录")
    try:
        run(args)
    except (RuntimeError, requests.RequestException, OSError, ValueError, KeyError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
