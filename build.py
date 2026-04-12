#!/usr/bin/env python3
"""
跨平台打包脚本 for 财联社监控
"""
import os
import sys
import platform
import subprocess
from pathlib import Path

def get_platform():
    """获取平台标识"""
    system = platform.system()
    if system == "Windows":
        return "windows"
    elif system == "Darwin":
        return "macos"
    else:
        return "linux"

def build_windows():
    """构建 Windows 可执行文件"""
    print("=" * 50)
    print("开始构建 Windows 版本...")
    print("=" * 50)
    
    cmd = [
        "pyinstaller",
        "--onefile",
        "--windowed",
        "--name", "财联社监控",
        "--clean",
        "--noconfirm",
    ]
    
    # 如果有图标文件
    icon_path = Path("assets/icon.ico")
    if icon_path.exists():
        cmd.extend(["--icon", str(icon_path)])
    
    cmd.append("cls_app.py")
    
    subprocess.run(cmd, check=True)
    
    # 创建 zip 文件
    print("\n创建压缩包...")
    dist_path = Path("dist/财联社监控.exe")
    if dist_path.exists():
        import zipfile
        with zipfile.ZipFile("财联社监控-Windows.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(dist_path, "财联社监控.exe")
        print("✓ 已创建 财联社监控-Windows.zip")
    
    print("\n✓ Windows 构建完成!")
    print(f"输出文件: {Path('财联社监控-Windows.zip').absolute()}")

def build_macos():
    """构建 macOS 应用"""
    print("=" * 50)
    print("开始构建 macOS 版本...")
    print("=" * 50)
    
    cmd = [
        "pyinstaller",
        "--windowed",
        "--name", "财联社监控",
        "--clean",
        "--noconfirm",
    ]
    
    # 如果有图标文件
    icon_path = Path("assets/icon.icns")
    if icon_path.exists():
        cmd.extend(["--icon", str(icon_path)])
    
    cmd.append("cls_app.py")
    
    subprocess.run(cmd, check=True)
    
    app_path = Path("dist/财联社监控.app")
    if app_path.exists():
        # 创建 dmg
        dmg_path = Path("财联社监控-macOS.dmg")
        if dmg_path.exists():
            dmg_path.unlink()
        
        # 尝试使用 create-dmg
        try:
            subprocess.run([
                "create-dmg",
                "--volname", "财联社监控",
                "--window-pos", "200", "120",
                "--window-size", "600", "400",
                "--icon-size", "100",
                "--app-drop-link", "450", "185",
                str(dmg_path),
                str(app_path),
            ], check=True)
            print(f"\n✓ 已创建 {dmg_path}")
        except (subprocess.CalledProcessError, FileNotFoundError):
            # 如果 create-dmg 不可用，直接压缩 app
            print("\ncreate-dmg 不可用，创建 zip 文件...")
            import zipfile
            with zipfile.ZipFile("财联社监控-macOS.zip", "w", zipfile.ZIP_DEFLATED) as zf:
                for file in app_path.rglob("*"):
                    arcname = file.relative_to(app_path.parent)
                    zf.write(file, arcname)
            print("✓ 已创建 财联社监控-macOS.zip")
    
    print("\n✓ macOS 构建完成!")

def main():
    """主函数"""
    plat = get_platform()
    
    # 确保依赖已安装
    print("检查依赖...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pyinstaller"], check=True)
    
    if plat == "windows":
        build_windows()
    elif plat == "macos":
        build_macos()
    else:
        print(f"不支持的平台: {plat}")
        sys.exit(1)

if __name__ == "__main__":
    main()
