# PDFScan 一键发布到 GitHub（公开仓库）
# 前提步骤（本机尚未安装 gh 时先装并登录）：
#   1) 安装 GitHub CLI:  winget install --id GitHub.cli   (或 scoop install gh)
#   2) 登录:             gh auth login     （浏览器 OAuth 授权，我无法代登）
# 然后在本目录运行:  .\publish.ps1   （加 -Private 可建私有仓库）
param(
    [switch]$Private
)
$ErrorActionPreference = "Stop"
$repo   = "LumberWG/PDFScan"
$branch = "main"
$visFlag = if ($Private) { "--private" } else { "--public" }

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host "未检测到 gh，请先安装: winget install --id GitHub.cli，重开终端后再运行。" -ForegroundColor Red
    exit 1
}
gh auth status | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "请先运行: gh auth login" -ForegroundColor Red
    exit 1
}

# 创建仓库并推送当前分支（若已存在远程则直接推送）
$create = gh repo create $repo $visFlag --source . --push --remote origin `
            --description "离线 PDF 红头文档自动切分 + 本地 HTTP 接口（FastAPI）"
if ($LASTEXITCODE -ne 0) {
    if (-not (git remote get-url origin 2>$null)) {
        git remote add origin "https://github.com/$repo.git"
    }
    git push -u origin $branch
}
Write-Host "完成 -> https://github.com/$repo" -ForegroundColor Green
