# 打包发布指南

## 自动打包 (GitHub Actions)

项目已配置 GitHub Actions，推送标签 `v*` 时会自动构建 Windows 和 macOS 版本。

```bash
# 创建标签并推送
git tag v1.0.0
git push origin v1.0.0
```

GitHub Actions 会自动:
1. 构建 Windows 可执行文件 (.exe)
2. 构建 macOS 应用 (.app)
3. 创建发布包 (.zip / .dmg)
4. 上传到 GitHub Releases

## 本地打包

### 前置要求

```bash
pip install pyinstaller pillow
```

### 1. 生成图标

```bash
python create_icon.py
```

这会生成:
- `assets/icon.ico` - Windows 图标
- `assets/icon.icns` - macOS 图标 (仅在 macOS 上)

### 2. Windows 打包

```bash
# 使用打包脚本
python build.py

# 或者手动打包
pyinstaller --onefile --windowed --name "财联社监控" --icon=assets/icon.ico cls_app.py
```

输出: `dist/财联社监控.exe`

### 3. macOS 打包

```bash
# 使用打包脚本
python build.py

# 或者手动打包
pyinstaller --windowed --name "财联社监控" --icon=assets/icon.icns cls_app.py

# 创建 DMG
create-dmg --volname "财联社监控" dist/财联社监控.app
```

输出: `dist/财联社监控.app`

## 手动打包

### Windows (PyInstaller)

```bash
pyinstaller \
  --onefile \
  --windowed \
  --name "财联社监控" \
  --icon=assets/icon.ico \
  --clean \
  --noconfirm \
  cls_app.py
```

### macOS (PyInstaller)

```bash
pyinstaller \
  --windowed \
  --name "财联社监控" \
  --icon=assets/icon.icns \
  --clean \
  --noconfirm \
  cls_app.py
```

## 发布流程

### 1. 更新版本号

在 `cls_app.py` 中更新版本信息（如果有）

### 2. 创建标签

```bash
git add .
git commit -m "准备发布 v1.0.0"
git tag v1.0.0
git push origin main --tags
```

### 3. 等待 GitHub Actions 完成

访问: `https://github.com/CHK768/cls_monitor/actions`

### 4. 检查 Releases

自动发布到: `https://github.com/CHK768/cls_monitor/releases`

## 注意事项

### Windows
- 目标系统需要安装 Chrome 浏览器
- 首次运行可能需要允许 Windows Defender 放行
- 需要 Claude Code CLI (`npm install -g @anthropic-ai/claude-code`)

### macOS
- 应用需要签名才能正常打开
- 用户首次打开需要在 系统设置 > 隐私与安全性 中允许
- 需要安装 Chrome: `brew install --cask google-chrome`
- 需要安装 Claude Code CLI: `npm install -g @anthropic-ai/claude-code`

## 故障排除

### Windows 打包错误
```bash
# 如果出现权限错误，使用管理员权限运行 PowerShell
# 或者添加 --hidden-import
pyinstaller --hidden-import PyQt6.sip cls_app.py
```

### macOS 签名问题
```bash
# 手动签名
codesign --force --deep --sign - "dist/财联社监控.app"
```

### 图标不显示
- 确保图标文件存在且格式正确
- Windows: ICO 格式，包含多个尺寸
- macOS: ICNS 格式
