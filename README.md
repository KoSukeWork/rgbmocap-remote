# RGBMoCap Remote

在 Blender 5.3 中选择一段单人视频，交给 Windows 服务器上的 ExMR、GVHMR 或 FlowHMR 重建，再把带关键帧的 FBX 骨架导入当前场景。客户端只需要 Blender。服务端复用上游固定版本的 Python、CUDA 模型和导出工具。

## 复用 RGBMoCap-Server 离线包

**已经拷完的服务器可以直接验收。** 在服务器本地的 `RGBMoCap-Server` 文件夹打开 PowerShell，运行 `.\Check-Server.cmd`。通过后用 `.\Create-User.cmd artist` 创建自己的账号，再用 `.\Start-Server.cmd` 启动。跨机连接、证书和短视频验收见[离线部署说明](deploy/README.md)。

**下次原样部署**，只需把未启用过的完整交付包复制到另一台有相应模型许可的 Windows x64 GPU 主机。**需要更新服务或从用过的服务器制作干净交付包**，在此仓库根目录运行下列第一条命令。它从旧包复用模型、运行时、服务 Python 和 Git，用当前仓库的源码重新生成服务、启动器和 Blender 扩展。仅有上游安装时，改用第二条。两条命令任选一条。

```powershell
python -B deploy/build_bundle.py --from-bundle 'D:\RGBMoCap-Server'
python -B deploy/build_bundle.py --upstream '..\toyxyz_RGBMocap' --offline
```

制作机使用 Windows x64、Python 3.12、完整的 Git for Windows（`git` 在 PATH 中）和可运行 CUDA 12.8 的显卡。请在源安装和服务空闲时制作。默认输出到本仓库的 `.local\RGBMoCap-Server`。用 `--output 'D:\Releases\RGBMoCap-Server-v2'` 可指定新目录。构建器在相邻的临时目录组装，完整检查通过后才将成品移到指定位置。每次制作使用新的输出目录。

- **从现成包制作**需要旧包完整保留 `engine\.git`、`assets.json`、模型及其收据。所有输入都从本地读取。
- **从上游安装制作**需要已完成固定提交的 Windows Git 安装，模型管理器里已备齐 ExMR、GVHMR、FlowHMR 及其细化功能所需的 27 项资产，并保留安装器的便携 Python 3.12.10。制作机还需 `uv`。`--offline` 使用 uv 本机已缓存的服务依赖轮子，首次缓存缺失时可去掉该参数联网安装这些 Python 依赖。目标服务器直接使用包内的 Python。
- **体积与去重**。参考交付包约 41.72 GiB。构建器从固定 Git 提交检出源码，按安装清单补齐外部资源，复制八个独立运行时目录及 `tools/bin`，按模型注册表从 `engine/models` 对应的源目录各取一份。上游运行时会在 `third_party` 下重建检查点别名，也可能生成 `cache/exmr`。制作新包时跳过这些目录和原服务的账号、项目、上传、结果及日志。
- **模型核验**。26 项资产采用上游固定 SHA-256，SMPL-X Neutral 采用制作时从已获授权的本地文件计算的 SHA-256，以便目标机验证传输完整性。从旧包重建时会沿用它记录的哈希。GitHub 只保存构建代码、启动器、依赖版本和说明，模型与运行时保存在私有交付目录。上游及模型条款见[上游许可说明](https://github.com/toyxyz/toyxyz_RGBMocap/blob/42844135d0612c7e574fd43f6c42414f5fce1219/THIRD_PARTY_NOTICES.md)。

在 RTX 5060 Ti 开发机上，使用 `--upstream --offline` 从本机安装和缓存重新制作的交付包通过了 27 项文件校验、六个工作环境检查和两套 CUDA 实测。更换目录后，随包启动器完成了建号、登录、视频上传和项目创建，ExMR 工作进程准备了 48 帧、30 fps 的输入。客户端 ZIP 通过了 Blender 5.3 的扩展校验。每台新服务器的重建、FBX 导出与工作站导入，按离线部署说明中的短视频流程验收。

## 启动任务服务

1. 在 Windows GPU 主机上完成上游安装，关闭上游界面，并在模型管理器里准备要使用的模型。服务会核对上游提交和运行环境，并在运行期间独占该安装。所用版本和提供方条款见 [上游固定版本](https://github.com/toyxyz/toyxyz_RGBMocap/tree/42844135d0612c7e574fd43f6c42414f5fce1219) 和 [模型许可](https://github.com/toyxyz/toyxyz_RGBMocap/blob/42844135d0612c7e574fd43f6c42414f5fce1219/THIRD_PARTY_NOTICES.md)。
2. 在本仓库根目录打开 PowerShell。以下示例使用 Python 3.12 和 uv，假设上游目录与本仓库相邻。首次创建账号时会显示一次个人令牌，用于 Blender 登录。

```powershell
uv venv --python 3.12 .venv
uv pip install --python .\.venv\Scripts\python.exe -r service\requirements.txt
$python = '.\.venv\Scripts\python.exe'
$upstream = (Resolve-Path '..\toyxyz_RGBMocap').Path
$data = Join-Path $PWD '.local\service-data'
& $python -m service --data $data create-user artist
& $python -m service --data $data serve --upstream $upstream
```

3. 本机默认连接 `http://127.0.0.1:8765`。从另一台电脑使用时，准备与服务器主机名匹配的 TLS 证书，让客户端信任签发机构。以如下方式监听局域网，扩展里填入相应的 `https://主机名:8765`。将证书和私钥路径换成本机文件。

```powershell
& $python -m service --data $data serve --upstream $upstream --host 0.0.0.0 --port 8765 --cert D:\certs\server.pem --key D:\certs\server-key.pem
```

服务以前台进程运行。按 Ctrl+C 可以停止，用同一数据目录重启会恢复排队任务，从中断的步骤重新开始。该目录包含原视频、工作文件、结果和账号数据库，请放在服务账号可读写的私有位置，与上游安装目录分开。需要撤销令牌时运行 `disable-user artist`，用法同 `create-user`。

## 安装和使用 Blender 扩展

从源码构建可安装的 ZIP。在本仓库根目录运行以下命令，把 `$blender` 换成 Blender 5.3 的程序路径。

```powershell
$blender = 'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe'
New-Item -ItemType Directory -Force .local\dist | Out-Null
& $blender --command extension build --source-dir addon\rgbmocap_remote --output-file .local\dist\rgbmocap_remote-0.1.0.zip
```

1. 在 Blender 偏好设置的扩展菜单中选 **Install from Disk**，安装 ZIP 并启用 **RGBMoCap Remote**。在系统偏好设置中启用 **Allow Online Access**。
2. 在 3D 视图按 **N**，打开 **RGBMoCap** 标签。填入服务器地址和个人令牌，点击 **连接**。首次使用时阅读提供方条款，由账号本人确认非商用用途。令牌只在当前会话的内存中使用。
3. 选取 MP4 或 MOV，选择模型，按秒填写要处理的起止位置。结束填 0 表示片尾。展开 **重建选项** 可调整接触修正、低显存模式以及所选模型的参数，然后点击 **提交重建**。FlowHMR 当前使用上游默认相机和采样设置。
4. 面板会刷新各任务的状态和当前步骤。任务提交后可以关闭 Blender，重新连接后仍能查看自己的任务。排队或运行中的任务可取消，失败或取消的任务可以重新排队。
5. 选中已完成的任务，点击 **导入动作并设置场景帧率和范围**。扩展先校验下载文件的 SHA-256，再导入骨架和动作，将场景帧率及播放范围设为该片段。再次导入相同结果时会复用场景内已有的骨架。

当前版本支持单人骨架动画。多人选取、身体网格、视频与相机叠加、接触区间编辑和角色重定向仍待实现。扩展清单的 GPL-3.0-or-later 标识适用于 Blender 扩展代码。上游软件和各模型采用各自的许可。

## 队列与验证

每步独立调度，服务器最多同时执行一个 GPU 步骤和两个 CPU 步骤。一个账号最多占一个 CPU 位置，同一项目内的任务串行。挑选下一步时优先考虑近一小时 GPU 用时较少的用户。任务所有权、步骤进度和重试状态保存在 SQLite 中。

在 RTX 5060 Ti 开发机上，三个测试账号分别用 ExMR、GVHMR 和 FlowHMR 重建了同一段 2.8 秒视频。Blender 5.3.0 导入的三个动作均为 65 根骨骼、84 帧、30 fps，首帧与中间帧的骨骼姿态有变化。另一个通过扩展提交、取消并重试的 ExMR 任务导入了 48 帧。测试也覆盖了服务重启恢复、局部下载续传和本机 HTTPS 连接。5090 服务器的跨机部署仍待验证。

运行接口和调度回归测试可使用以下命令。测试使用临时数据目录。

```powershell
uv pip install --python .\.venv\Scripts\python.exe httpx2==2.16.2
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

若已安装并启用扩展，还可用后台 Blender 对已完成的任务做导入回归。给该进程设置环境变量 `RGBMOCAP_TEST_TOKEN`，使用已确认许可的测试账号。把任务 ID 换成该账号的真实任务。使用 `--video D:\clips\walk.mp4 --model exmr --end 2.8` 可提交新任务并等待导入。

```powershell
& $blender --background --online-mode --python-exit-code 1 --python tests\blender_smoke.py -- --server http://127.0.0.1:8765 --job 'j_替换为实际任务ID'
```

## 运行一次动捕

脚本复用上游安装中的 Python、模型和 Blender。请按上游说明完成安装，并在模型管理器中准备所选模型及其依赖。验证使用的上游版本为 [toyxyz_RGBMocap 的 `4284413`](https://github.com/toyxyz/toyxyz_RGBMocap/tree/42844135d0612c7e574fd43f6c42414f5fce1219)。

以下 PowerShell 示例假设两个仓库位于同一父目录。在上游 RGBMoCap 的安装目录执行，先关闭 RGBMoCap 界面，将视频路径换成自己的文件。

```powershell
$poc = (Resolve-Path '..\rgbmocap-remote\poc').Path
$video = 'D:\clips\walk.mp4'
$out = Join-Path $poc 'runs\walk-gvhmr'

.\runtime\python.exe -I -B -X utf8 -u "$poc\poc_headless.py" --root . --video $video --model gvhmr --out $out
```

成功后，输出目录包含 FBX、工作区和 `poc_report.json`。运行其他模型时，将 `--model` 改为 `exmr` 或 `flowhmr`，并使用各自的输出目录。

## 与原版界面对照

沿用上例的变量。界面对照需要一个新的输出目录。比较程序会核对源视频、帧数、模型选项、关节位置、骨骼旋转及 BVH 通道。

```powershell
$gui = Join-Path $poc 'runs\walk-gvhmr-gui'
.\runtime\python.exe -I -B -X utf8 -u "$poc\gui_baseline.py" --root . --video $video --model gvhmr --out $gui
if ($LASTEXITCODE -ne 0) { throw 'GUI baseline failed' }
$reference = (Get-Content -LiteralPath "$gui\gui_report.json" -Raw -Encoding UTF8 | ConvertFrom-Json).project
.\runtime\python.exe -I -B -X utf8 -u "$poc\poc_headless.py" --root . --compare-only --out $out --gui-project $reference
```

比较结果写入 `$out\compare_report.json`。使用同一段视频再次运行界面基准，可以测量模型自身的重复运行差异。

## Windows 服务会话

在管理员 PowerShell 中运行以下命令，可以测试当前安装在 Windows Session 0 中使用 CUDA 和后台 Blender 的能力。脚本会创建临时 LocalService 服务，检查一次 GPU 张量运算和场景保存、重开，然后注销服务并输出报告位置。

```powershell
& '..\rgbmocap-remote\poc\Test-ServiceSession.ps1' -Root (Get-Location).Path
```
