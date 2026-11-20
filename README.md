# RGBMoCap Remote

本项目验证如何在 Windows 上通过脚本调用 RGBMoCap 的单人动捕流程，为局域网任务服务和 Blender 扩展做准备。当前代码支持 GVHMR、ExMR 和 FlowHMR，可以从视频导出 FBX，并将中间结果与原版界面独立运行的结果比较。

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
