# PC-REHD Code X

PC-REHD Code X 是一个面向 Windows 的 Resident Evil 6 / PC-REHD 工作流工具，用于处理 `.MOD`、`.MRL` 与 FBX 文件，并支持 3ds Max 和 Blender 的导入、导出及往返编辑流程。

本仓库发布当前版本的源码和依赖快照；可直接运行的完整程序包请从 GitHub Releases 页面下载。

## 发布内容

- Python Launcher、导入导出桥接程序与界面代码。
- 3ds Max、Blender 的 Agent 及 FBX 工作流代码。
- 贴图、材质、MRL、网格、法线与骨骼编辑相关功能代码。
- 固定版本的 Python 依赖快照：`PY依赖 PY Libs`。
- Windows 启动脚本与发布包使用的 Python 安装程序。

仓库和 Release 包不会包含以下内容：内嵌 Python 运行时、生成的字节码、诊断日志、V4 备份、游戏文件、用户模型、场景文件或其他私人数据。

## 下载与安装

1. 打开本项目的 [Releases](https://github.com/Criticise/PC-REHD-1.2.8-Scrpit-rewritten-in-Python-by-Code-X-GPT-5.6/releases) 页面。
2. 下载 `RE6-PC-REHD-Code-X-v1.0.0.7z`。
3. 解压到一个具有写入权限且路径较短的文件夹。
4. 先运行 `先点Bat文件 - Click Bat First.ps1`。如果尚未安装 Python，请先运行 `一定要先点我安装Python - Click to Install Python First.bat`。
5. 使用导入、导出或骨骼编辑前，请备份 `.MOD`、`.MRL`、FBX 和 DCC 场景文件。

## 重要说明

- 本工具为非官方社区工具，与 CAPCOM 无任何隶属、合作或授权关系。
- 仓库和 Release 资源均不包含《生化危机 6》游戏资产。
- 本工具可修改 3D 场景并生成 `.MOD` 文件。请始终保留原始文件，并通过自己的游戏测试流程验证生成结果。
- 3ds Max 与 Blender 是相互独立的模式，各自使用独立的 Agent 和状态缓存。
- Blender 导入 FBX 时可能出现层级关系变化。未经过层级修复的 Blender 导入结果不能作为 Round Trip 往返验证依据；建议使用脚本的层级修复功能，或通过 3ds Max 进行验证。

## 源码结构

| 路径 | 作用 |
| --- | --- |
| `PC-REHD Code X Launcher.py` | 主启动器、界面、Agent 调度与工具入口。 |
| `codex_python_export_bridge.py` | 导出桥接与 `.MOD` 写入流程。 |
| `codex_re6_mod_import_fbx.py` | `.MOD` 转 FBX 与导入准备流程。 |
| `codex_fbx_probe.py` | FBX 检查与数据合同解析。 |
| `codex_python_runtime_bootstrap.py` | Python 运行环境安装与初始化。 |
| `PY依赖 PY Libs/` | 固定版本的 Python 依赖及供应商源码快照。 |
| `先点Bat文件 - Click Bat First.ps1` | 启动前的环境初始化与检查入口。 |

## 文件校验

每个 GitHub Release 都会在压缩包旁提供 `SHA256SUMS.txt`。下载后建议先校验 SHA-256，再解压或运行程序。

当前 `RE6-PC-REHD-Code-X-v1.0.0.7z` 的 SHA-256：

```text
BBCF637B93E83CF8BBE5DFED21B781A409B19721E71AAA5BF80BC60416127E77
```

在 PowerShell 中可使用以下命令校验：

```powershell
Get-FileHash .\RE6-PC-REHD-Code-X-v1.0.0.7z -Algorithm SHA256
```

## 许可证与第三方组件

项目所有者目前尚未为本项目选择许可证。源码公开不代表获得再发布、重新授权或商业使用许可。

第三方组件仍适用各自的许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
