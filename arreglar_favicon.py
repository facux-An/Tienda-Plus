import os
from PIL import Image

src = r'static\img\favicon.png'
if not os.path.exists(src):
    print('No se encontró el favicon original.')
    exit(1)

img = Image.open(src).convert('RGBA')
width, height = img.size
max_dim = max(width, height)
square_img = Image.new('RGBA', (max_dim, max_dim), (255, 255, 255, 0))

offset_x = (max_dim - width) // 2
offset_y = (max_dim - height) // 2
square_img.paste(img, (offset_x, offset_y))

# Generamos TRES tamaños estrictos.
img_64 = square_img.resize((64, 64), Image.Resampling.NEAREST)
img_192 = square_img.resize((192, 192), Image.Resampling.NEAREST)
img_512 = square_img.resize((512, 512), Image.Resampling.NEAREST)

img_64.save(r'static\img\favicon-64.png', 'PNG')
img_192.save(r'static\img\favicon-192.png', 'PNG')
img_512.save(r'static\img\favicon-512.png', 'PNG')

print('Íconos generados con dimensiones matemáticamente perfectas.')