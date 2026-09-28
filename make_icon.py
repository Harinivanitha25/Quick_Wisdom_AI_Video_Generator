from PIL import Image

img = Image.open("logo.png").convert("RGBA")
size = max(img.size)
square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
square.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
square.save("logo.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])