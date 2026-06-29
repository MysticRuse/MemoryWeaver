import os
from PIL import Image
from PIL.ExifTags import TAGS

filepath = '/Users/mitilroy/Dev/src/MysticRuse/MemoryWeaver/memoryweaver-mvp/uploads/FIFA26/IMG_0625_1782695853_72f3e3.jpeg'
try:
    img = Image.open(filepath)
    exif = img._getexif()
    if exif:
        for tag, value in exif.items():
            decoded = TAGS.get(tag, tag)
            if decoded in ['DateTimeOriginal', 'DateTimeDigitized', 'DateTime']:
                print(f"EXIF {decoded}: {value}")
except Exception as e:
    print(f"Error: {e}")
