import os
from PIL import Image

# Rutas de los archivos
src = r'static\img\favicon.png'
dst_192 = r'static\img\favicon-192.png'
dst_512 = r'static\img\favicon-512.png'

if not os.path.exists(src):
    print('Error: No se encontró el favicon original.')
    exit(1)

# Abrir imagen original
img = Image.open(src).convert('RGBA')

# 1. Crear un fondo cuadrado perfecto basado en el lado más grande
width, height = img.size
max_dim = max(width, height)
square_img = Image.new('RGBA', (max_dim, max_dim), (255, 255, 255, 0))

# 2. Centrar el gatito en el cuadrado
offset_x = (max_dim - width) // 2
offset_y = (max_dim - height) // 2
square_img.paste(img, (offset_x, offset_y))

# 3. Redimensionar usando NEAREST para mantener los bordes pixel art nítidos
img_192 = square_img.resize((192, 192), Image.Resampling.NEAREST)
img_512 = square_img.resize((512, 512), Image.Resampling.NEAREST)

# 4. Guardar
img_192.save(dst_192, 'PNG')
img_512.save(dst_512, 'PNG')

print('¡Éxito! favicon-192.png y favicon-512.png generados profesionalmente.')