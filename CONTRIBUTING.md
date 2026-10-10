# 开发与贡献

需要 Python 3.10 或更新版本。建议先创建虚拟环境：

```bash
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[all]"
python -m unittest discover -s tests -v
```

测试默认不访问 Docker Hub、GHCR 或代理服务，集成测试使用本机模拟 Registry。
已安装且可访问 Docker Engine 时，可以验证实际导入：

```bash
TEST_DOCKER_LOAD=1 python -m unittest discover -s tests -p test_integration.py -v
```

PowerShell 对应写法：先执行 `$env:TEST_DOCKER_LOAD = '1'`，再执行测试命令。
Docker 测试会创建一个随机命名的测试镜像，并在完成后清理它。

提交修改时请说明使用场景、行为变化和验证结果；增加 Registry、认证或层格式支持时，
补充成功和失败路径测试。不要提交访问令牌、代理密码或下载的镜像归档。
