from flask import Flask, request, render_template, redirect, url_for, send_from_directory
import os, threading
from dotenv import load_dotenv
load_dotenv()

pipeline_status = {'running': False, 'error': None}

app = Flask(__name__)
SESSION_ID = 'FIFA26'
UPLOAD_DIR = f'uploads/{SESSION_ID}'
OUTPUT_DIR = f'outputs/{SESSION_ID}'
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max per upload

@app.route('/')
def index():
    count = len(os.listdir(UPLOAD_DIR))
    return render_template('uploads.html', count=count)

@app.route('/upload', methods=['POST'])
def upload():
    files = request.files.getlist('photos')
    saved = 0
    for f in files:
        if f.filename:
            f.save(os.path.join(UPLOAD_DIR, f.filename))
            saved += 1
    count = len(os.listdir(UPLOAD_DIR))
    return render_template('uploads.html', count=count, success=f'Added {saved} photo(s)!')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(os.path.abspath(UPLOAD_DIR), filename)

@app.route('/generate')
def generate():
    if pipeline_status['running']:
        return render_template('processing.html')
    def run():
        pipeline_status['running'] = True
        pipeline_status['error'] = None
        try:
            from pipeline import run_pipeline
            run_pipeline(SESSION_ID)
        except Exception as e:
            pipeline_status['error'] = str(e)
        finally:
            pipeline_status['running'] = False
    threading.Thread(target=run, daemon=True).start()
    return render_template('processing.html')

@app.route('/status')
def status():
    import json
    done = os.path.exists(f'{OUTPUT_DIR}/story.txt')
    return json.dumps({'running': pipeline_status['running'], 'done': done, 'error': pipeline_status['error']})

@app.route('/results')
def results():
    import json
    story, stats, highlights = '', '', []
    for attr, path, loader in [
        ('story',      f'{OUTPUT_DIR}/story.txt',       lambda f: f.read()),
        ('stats',      f'{OUTPUT_DIR}/stats.txt',       lambda f: f.read()),
        ('highlights', f'{OUTPUT_DIR}/highlights.json', lambda f: json.load(f)),
    ]:
        try:
            with open(path) as f:
                val = loader(f)
            if attr == 'story': story = val
            elif attr == 'stats': stats = val
            else: highlights = val
        except Exception as e:
            print(f'Results load error ({attr}): {e}')
    return render_template('result.html', story=story, stats=stats, highlights=highlights)

if __name__ == '__main__':
    app.run(debug=True, port=5001)