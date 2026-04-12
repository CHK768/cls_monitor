# 图标资源

## Windows 图标
- 文件: `icon.ico`
- 尺寸: 256x256, 128x128, 64x64, 32x32, 16x16
- 格式: ICO

## macOS 图标
- 文件: `icon.icns`
- 尺寸: 1024x1024, 512x512, 256x256, 128x128, 64x64, 32x32, 16x16
- 格式: ICNS

## 生成图标

可以使用 Python 脚本生成简单图标:

```python
from PIL import Image, ImageDraw, ImageFont

def create_icon():
    # 创建 256x256 图像
    img = Image.new('RGBA', (256, 256), (0, 122, 255, 255))
    draw = ImageDraw.Draw(img)
    
    # 绘制简单图形
    draw.ellipse([40, 40, 216, 216], fill=(255, 255, 255, 255))
    
    # 保存
    img.save('icon_256.png')

if __name__ == "__main__":
    create_icon()
```

或者使用在线工具:
- [ICO Convert](https://icoconvert.com/)
- [App Icon Generator](https://appicon.co/)
