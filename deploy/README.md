# RGBMoCap-Server 离线部署

这份说明随服务端一起交付。已有完整交付文件夹时，直接从第一步开始。目标机需要 Windows x64、本地 NTFS 磁盘，以及支持 CUDA 12.8 的 NVIDIA 驱动。交付包自带服务用 Python、Git、推理运行时、模型、FFmpeg、FFprobe 和 Blender 4.5 导出程序。

## 1. 把整个文件夹复制到服务器

停用的原始交付包可以重复用于有相应模型许可的服务器。通过 RDP 把整个文件夹复制到服务器的本地磁盘，例如 `D:\RGBMoCap-Server`。保留 `engine\.git`、`engine\.setup` 和模型旁的 `.receipt.json`。资源管理器显示的重定向盘仅用于传输。参考交付包约 41.72 GiB，另外为上传的视频、推理缓存和结果预留空间。

| 包内位置 | 用途 |
| --- | --- |
| `engine\` | 固定提交的上游源码、独立运行时、导出工具和 `models\` 原始权重 |
| `python\`、`git\` | 供服务启动、账号管理及上游版本核验使用 |
| `service\`、`poc\` | HTTP 服务、队列和推理步骤 |
| `assets.json` | 27 项模型资产的路径、字节数、SHA-256 和上游收据使用的修改时间 |
| `client\` | 可以复制到工作站、从磁盘安装的 Blender 5.3 扩展 ZIP |

## 2. 首次验收并创建账号

在服务器本机打开 PowerShell，进入刚复制的目录。以下命令的路径可以替换。

```powershell
cd D:\RGBMoCap-Server
.\Check-Server.cmd
if ($LASTEXITCODE -ne 0) { throw '请先修复检查报告的文件或环境' }
.\Create-User.cmd artist
```

完整检查会读取所有模型并核对 SHA-256，检查上游提交、安装记录和六个工作环境，在 shared 和 ExMR 两套 Python 中实际运行 CUDA 张量运算，再执行 FFmpeg、FFprobe 和服务端 Blender。以退出码 0 和最后的 `Passed` 为准。若 RDP 复制改变了模型修改时间，检查器会在文件哈希一致后恢复时间；哈希不一致时，按报错路径从原包重拷对应文件或收据，再运行检查。CUDA 检查失败时，先在服务器运行 `nvidia-smi` 核对显卡和驱动。

创建账号时令牌只显示一次，保存给该用户在客户端登录。默认账号库、上传和结果保存在包内的 `data\`。如需单独存放，请在**创建账号和每次启动之前**在 PowerShell 中设置同一个绝对路径，例如 `$env:RGBMOCAP_REMOTE_STATE = 'D:\RGBMoCap-Data'`。服务进程需要读写状态目录，以及上游的缓存、锁和模型工作目录。

## 3. 启动并验证任务

```powershell
.\Start-Server.cmd
```

启动器先快速检查模型，再以前台进程监听本机。保持窗口打开，用服务器浏览器访问 [本机 API 文档](http://127.0.0.1:8765/docs)。按 Ctrl+C 停止。若要从局域网工作站连接，先准备与服务器名称匹配的证书与私钥，让工作站信任签发机构，并在防火墙放行相应端口。停止本机监听后运行以下命令。

```powershell
.\Start-Server.cmd --host 0.0.0.0 --port 8765 --cert D:\certs\server.pem --key D:\certs\server-key.pem
```

在工作站 Blender 5.3 中通过 **Install from Disk** 安装 `client\` 下的 ZIP，启用扩展及 **Allow Online Access**。在 3D 视图按 **N → RGBMoCap**，使用与证书匹配的 HTTPS 地址和个人令牌连接，由账号本人确认许可。提交一段短的单人视频，等任务完成后导入 FBX，在时间线上确认帧范围并播放骨架动作。做到这一步才覆盖了该服务器和该工作站之间的完整链路。

## 下次复用

留一份尚未运行过服务的完整交付包作为母本。已运行的服务器会有账号、任务、缓存和生成的模型副本。从这种目录制作新交付时，在开发机使用仓库 README 中的 `deploy/build_bundle.py --from-bundle` 命令。新服务器单独建账号，沿用原服务时则继续使用原来的私有状态目录。上游软件与权重的许可见 `engine\THIRD_PARTY_NOTICES.md` 及各提供方条款。
