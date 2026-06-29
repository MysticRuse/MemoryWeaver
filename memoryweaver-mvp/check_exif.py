import os
import pipeline as pl

upload_dir = '/Users/mitilroy/Dev/src/MysticRuse/MemoryWeaver/memoryweaver-mvp/uploads/FIFA26'
files = [f for f in os.listdir(upload_dir) if os.path.isfile(os.path.join(upload_dir, f)) and not f.startswith('.')]
for f in files:
    cat, ts = pl.classify_by_metadata_only(upload_dir, f)
    print(f"{f}: cat={cat}, ts={ts}")

