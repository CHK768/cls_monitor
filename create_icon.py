#!/usr/bin/env python3
"""
生成应用图标
需要: pip install pillow
"""
import os
from PIL import Image, ImageDraw, ImageFont

def create_icon(size=256):
    """创建应用图标"""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # 背景色 - 蓝色渐变效果
    for i in range(size):
        alpha = int(255 * (1 - i / size * 0.3))
        draw.line([(0, i), (size, i)], fill=(0, 122, 255, alpha))
    
    # 绘制圆角矩形背景
    margin = size // 8
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=size // 10,
        fill=(255, 255, 255, 240)
    )
    
    # 绘制 "财" 字
    try:
        # 尝试使用系统字体
        font_size = size // 2
        font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", font_size)
    except:
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", font_size)
        except:
            font = ImageFont.load_default()
    
    text = "财"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    x = (size - text_width) // 2
    y = (size - text_height) // 2 - size // 20
    
    draw.text((x, y), text, fill=(0, 122, 255, 255), font=font)
    
    return img

def create_windows_icon():
    """创建 Windows ICO 文件"""
    print("创建 Windows 图标...")
    sizes = [256, 128, 64, 48, 32, 16]
    images = []
    
    for size in sizes:
        img = create_icon(size)
        images.append(img)
    
    # 保存为 ICO
    images[0].save(
        "assets/icon.ico",
        format='ICO',
        sizes=[(s, s) for s in sizes],
        append_images=images[1:]
    )
    print("✓ 已创建 assets/icon.ico")

def create_macos_icon():
    """创建 macOS ICNS 文件（需要 iconutil 或转换为 PNG 集合）"""
    print("创建 macOS 图标...")
    sizes = [1024, 512, 256, 128, 64, 32, 16]
    
    # macOS 需要特殊处理，这里先创建各个尺寸的 PNG
    import tempfile
    import subprocess
    
    with tempfile.TemporaryDirectory() as tmpdir:
        iconset = os.path.join(tmpdir, "icon.iconset")
        os.makedirs(iconset)
        
        for size in sizes:
            # 标准分辨率
            img = create_icon(size)
            img.save(os.path.join(iconset, f"icon_{size}x{size}.png"))
            
            # Retina 分辨率 (@2x)
            if size <= 512:
                img2x = create_icon(size * 2)
                img2x.save(os.path.join(iconset, f"icon_{size}x{size}@2x.png"))
        
        # 使用 iconutil 转换为 icns
        try:
            subprocess.run([
                "iconutil", "-c", "icns", "-o", "assets/icon.icns", iconset
            ], check=True)
            print("✓ 已创建 assets/icon.icns")
        except (subprocess.CalledProcessError, FileNotFoundError):
            # 如果没有 iconutil (非 macOS 系统)，保存为 1024 PNG
            img = create_icon(1024)
            img.save("assets/icon_1024.png")
            print("⚠ iconutil 不可用，已创建 assets/icon_1024.png")
            print("  在 macOS 上运行以下命令生成 icns:")
            print("  iconutil -c icns -o assets/icon.icns icon.iconset")

def main():
    """主函数"""
    os.makedirs("assets", exist_ok=True)
    
    create_windows_icon()
    
    if os.name == 'posix':
        create_macos_icon()
    else:
        print("\n跳过 macOS 图标创建 (请在 macOS 上运行)")
        # 仍然创建一个大的 PNG 备用
        img = create_icon(1024)
        img.save("assets/icon_1024.png")
        print("✓ 已创建 assets/icon_1024.png")

if __name__ == "__main__":
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("请先安装 Pillow: pip install pillow")
        exit(1)
    
    main()
