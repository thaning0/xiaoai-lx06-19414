# 本地 HTTP API 与界面

这是 LX06 1.94.14 单设备适配所用的控制界面，源于上游 core API。
独立显示设备信息、三路 ALSA mixer 和显式音量应用按钮；Home Assistant 发现为空时仍可使用本地功能。
请求分别显示错误，静态页面不依赖外部 CDN。端到端音频服务的验证范围见[实测记录](../docs/compatibility.md)。

入口是 `main.py` 和 `static/`。固件中的服务路径为 `/usr/share/api/main.py`。
离线构建会以当前目录源码覆盖复用 release 的旧 API/UI；Python 运行库和依赖仍来自 overlay，未宣称重新从源码编译。

这是可信局域网的设备控制接口，未提供公网部署所需的身份认证。不要转发端口到公网。
不要把运行时配置、访问令牌、页面中的设备标识或诊断输出提交到仓库。
构建方法与依赖见[离线复现](../docs/offline-build.md)，本地语法检查不启动 API 或设备服务。
