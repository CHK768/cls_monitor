#!/usr/bin/env python3
"""
生成应用图标
需要: pip install pillow
"""
import os
import sys
from PIL import Image, ImageDraw, ImageFont

def get_font(size):
    """获取可用的字体"""
    # 尝试的中文字体列表
    font_paths = [
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        # Windows
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        # Linux
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except:
            continue
    
    # 如果都没有，使用默认字体
    return ImageFont.load_default()

def create_icon_simple(size=256):
    """创建简单图标（无文字，适合 CI 环境）"""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # 蓝色渐变背景
    for i in range(size):
        alpha = int(255 * (1 - i / size * 0.2))
        draw.line([(0, i), (size, i)], fill=(0, 122, 255, alpha))
    
    # 绘制白色圆角矩形
    margin = size // 8
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=size // 8,
        fill=(255, 255, 255, 230)
    )
    
    # 尝试绘制 "财" 字
    try:
        font = get_font(size // 2)
        text = "财"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        
        x = (size - text_width) // 2
        y = (size - text_height) // 2 - size // 25
        
        draw.text((x, y), text, fill=(0, 122, 255, 255), font=font)
    except Exception as e:
        # 如果没有中文字体，绘制一个 "C" 字母代表 Cai
        print(f"无法绘制中文字体: {e}")
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size // 2)
        except:
            font = ImageFont.load_default()
        
        bbox = draw.textbbox((0, 0), "C", font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        
        x = (size - text_width) // 2
        y = (size - text_height) // 2
        draw.text((x, y), "C", fill=(0, 122, 255, 255), font=font)
    
    return img

def create_windows_icon():
    """创建 Windows ICO 文件"""
    print("创建 Windows 图标...")
    os.makedirs("assets", exist_ok=True)
    
    sizes = [256, 128, 64, 48, 32, 16]
    images = []
    
    for size in sizes:
        img = create_icon_simple(size)
        images.append(img)
    
    # 保存为 ICO
    output_path = "assets/icon.ico"
    images[0].save(
        output_path,
        format='ICO',
        sizes=[(s, s) for s in sizes],
        append_images=images[1:]
    )
    print(f"✓ 已创建 {output_path}")

def create_macos_icon():
    """创建 macOS ICNS 文件"""
    print("创建 macOS 图标...")
    os.makedirs("assets", exist_ok=True)
    
    sizes = [1024, 512, 256, 128, 64, 32, 16]
    
    if sys.platform == "darwin":
        # macOS 系统：使用 iconutil
        import tempfile
        import subprocess
        
        with tempfile.TemporaryDirectory() as tmpdir:
            iconset = os.path.join(tmpdir, "icon.iconset")
            os.makedirs(iconset)
            
            for size in sizes:
                # 标准分辨率
                img = create_icon_simple(size)
                img.save(os.path.join(iconset, f"icon_{size}x{size}.png"))
                
                # Retina 分辨率 (@2x)
                if size <= 512:
                    img2x = create_icon_simple(size * 2)
                    img2x.save(os.path.join(iconset, f"icon_{size}x{size}@2x.png"))
            
            # 使用 iconutil 转换为 icns
            try:
                subprocess.run([
                    "iconutil", "-c", "icns", "-o", "assets/icon.icns", iconset
                ], check=True)
                print("✓ 已创建 assets/icon.icns")
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                print(f"⚠ iconutil 失败: {e}")
                # 创建备用 PNG
                img = create_icon_simple(1024)
                img.save("assets/icon_1024.png")
                print("✓ 已创建 assets/icon_1024.png")
    else:
        # 非 macOS 系统：只创建大 PNG
        print("⚠ 非 macOS 系统，跳过 .icns 创建")
        img = create_icon_simple(1024)
        img.save("assets/icon_1024.png")
        print("✓ 已创建 assets/icon_1024.png")

def main():
    """主函数"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("请先安装 Pillow: pip install pillow")
        sys.exit(1)
    
    create_windows_icon()
    create_macos_icon()
    print("\n✓ 图标生成完成！")

if __name__ == "__main__":
    main()
