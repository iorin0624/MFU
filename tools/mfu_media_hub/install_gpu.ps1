param(
    [string]$Python = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe')
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python 3.13 が見つかりません: $Python（-Python で指定してください）"
}
$runtime = Join-Path $env:LOCALAPPDATA 'MFU\MediaHubGPU'
& $Python -m venv $runtime
if ($LASTEXITCODE -ne 0) { throw 'GPU用仮想環境の作成に失敗しました。' }
$runtimePython = Join-Path $runtime 'Scripts\python.exe'
& $runtimePython -m pip install --index-url https://download.pytorch.org/whl/cu130 'torch==2.14.1+cu130'
if ($LASTEXITCODE -ne 0) { throw 'CUDA版PyTorchのインストールに失敗しました。' }
& $runtimePython -m pip install 'transformers==4.57.6' pillow numpy
if ($LASTEXITCODE -ne 0) { throw '分類依存ライブラリのインストールに失敗しました。' }
& $runtimePython -c 'import torch; assert torch.cuda.is_available(), "CUDA GPU が見つかりません"; print(torch.cuda.get_device_name(0))'
if ($LASTEXITCODE -ne 0) { throw 'GPUの動作確認に失敗しました。' }
Write-Host "GPU分類ランタイムを準備しました: $runtime"
