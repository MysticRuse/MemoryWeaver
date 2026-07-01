from flask import Flask, request, render_template, redirect, url_for, send_from_directory, session
import os, threading, time, json
from dotenv import load_dotenv
from PIL import Image
from pillow_heif import register_heif_opener
register_heif_opener()
load_dotenv()

THUMB_MAX_PX = 1024  # max dimension for display thumbnails

# Pipeline state — shared between request thread and background worker
pipeline_status = {
    'running': False,
    'error': None,
    'stage': None,       # current step shown in UI
    'log': [],           # rolling log lines shown in UI
    'started_at': None,
}
PIPELINE_TIMEOUT = 300  # 5 minutes hard timeout

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'memoryweaver-super-secret-key-12984')
SESSION_ID = 'FIFA26'
UPLOAD_DIR = f'uploads/{SESSION_ID}'
THUMB_DIR  = f'uploads/{SESSION_ID}/thumbs'
OUTPUT_DIR = f'outputs/{SESSION_ID}'
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(THUMB_DIR,  exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024


def _make_thumbnail(src_path, filename):
    """Resize to max 1024px, save as JPEG in thumbs dir. Silently skips on error."""
    try:
        img = Image.open(src_path).convert("RGB")
        img.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX), Image.LANCZOS)
        thumb_name = os.path.splitext(filename)[0] + '.jpg'
        img.save(os.path.join(THUMB_DIR, thumb_name), format="JPEG", quality=82)
    except Exception as e:
        print(f'Thumbnail error for {filename}: {e}')


def pipeline_log(msg):
    print(msg)
    pipeline_status['log'].append(msg)
    if len(pipeline_status['log']) > 50:
        pipeline_status['log'].pop(0)


PUBLIC_URL = os.getenv('PUBLIC_URL', '')

@app.route('/')
def index():
    all_files = [f for f in os.listdir(UPLOAD_DIR) if os.path.isfile(os.path.join(UPLOAD_DIR, f)) and not f.startswith('.')]
    count = len(all_files)
    
    # Filter session uploaded files to ensure they actually exist on disk
    my_uploads = session.get('my_uploads', [])
    my_uploads = [f for f in my_uploads if f in all_files]
    session['my_uploads'] = my_uploads
    
    story_ready = os.path.exists(f'{OUTPUT_DIR}/story.txt')
    success = request.args.get('success')
    return render_template('uploads.html', count=count, public_url=PUBLIC_URL, story_ready=story_ready, success=success, uploaded_files=my_uploads)


@app.route('/clear')
def clear_all():
    import shutil
    try:
        # Clear uploads and thumbs
        if os.path.exists(UPLOAD_DIR):
            shutil.rmtree(UPLOAD_DIR)
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        os.makedirs(THUMB_DIR, exist_ok=True)
        
        # Clear outputs
        if os.path.exists(OUTPUT_DIR):
            shutil.rmtree(OUTPUT_DIR)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        
        # Reset current session uploads
        session['my_uploads'] = []
        
        # Reset pipeline status
        pipeline_status.update({
            'running': False,
            'error': None,
            'stage': 'Starting',
            'log': [],
            'started_at': None,
            'api_calls': []
        })
        
        success_msg = "Successfully cleared all photos and generated stories!"
    except Exception as e:
        success_msg = f"Error clearing: {e}"
        
    return redirect(url_for('index', success=success_msg))


@app.route('/delete/<path:filename>')
def delete_file(filename):
    try:
        # Delete original file
        orig_path = os.path.join(UPLOAD_DIR, filename)
        if os.path.exists(orig_path):
            os.remove(orig_path)
            
        # Delete thumbnail
        thumb_name = os.path.splitext(filename)[0] + '.jpg'
        thumb_path = os.path.join(THUMB_DIR, thumb_name)
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
            
        # Remove from current session uploads
        my_uploads = session.get('my_uploads', [])
        if filename in my_uploads:
            my_uploads.remove(filename)
            session['my_uploads'] = my_uploads
            
        # If output files exist, remove them since album has changed
        for artefact in ('story.txt', 'stats.json', 'highlights.json'):
            path = os.path.join(OUTPUT_DIR, artefact)
            if os.path.exists(path):
                os.remove(path)
                
        # Reset pipeline status
        pipeline_status.update({
            'running': False,
            'error': None,
            'stage': 'Starting',
            'log': [],
            'started_at': None,
            'api_calls': []
        })
        success_msg = f"Deleted memory: {filename}"
    except Exception as e:
        success_msg = f"Error deleting file: {e}"
    
    return redirect(url_for('index', success=success_msg))


@app.route('/upload', methods=['POST'])
def upload():
    files = request.files.getlist('photos')
    saved_files = []
    import uuid
    for f in files:
        if f.filename:
            base, ext = os.path.splitext(f.filename)
            unique_name = f"{base}_{int(time.time())}_{uuid.uuid4().hex[:6]}{ext.lower()}"
            dest = os.path.join(UPLOAD_DIR, unique_name)
            f.save(dest)
            _make_thumbnail(dest, unique_name)
            saved_files.append(unique_name)
            
    # Append to current session uploads
    my_uploads = session.get('my_uploads', [])
    for sf in saved_files:
        if sf not in my_uploads:
            my_uploads.append(sf)
    session['my_uploads'] = my_uploads
    
    if len(saved_files) > 0:
        for artefact in ('story.txt', 'stats.json', 'highlights.json'):
            path = os.path.join(OUTPUT_DIR, artefact)
            if os.path.exists(path):
                os.remove(path)
                
    all_files = [f for f in os.listdir(UPLOAD_DIR) if os.path.isfile(os.path.join(UPLOAD_DIR, f)) and not f.startswith('.')]
    count = len(all_files)
    
    # Filter to make sure session files exist
    session_files = [f for f in my_uploads if f in all_files]
    session['my_uploads'] = session_files
    
    story_ready = os.path.exists(f'{OUTPUT_DIR}/story.txt')
    return render_template('uploads.html', count=count, success=f'Added {len(saved_files)} photo(s)!', public_url=PUBLIC_URL, story_ready=story_ready, uploaded_files=session_files)


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(os.path.abspath(UPLOAD_DIR), filename)


@app.route('/thumbs/<filename>')
def thumb_file(filename):
    thumb_name = os.path.splitext(filename)[0] + '.jpg'
    thumb_path = os.path.join(THUMB_DIR, thumb_name)
    if os.path.exists(thumb_path):
        return send_from_directory(os.path.abspath(THUMB_DIR), thumb_name)
    # Fall back to original if thumbnail wasn't generated
    return send_from_directory(os.path.abspath(UPLOAD_DIR), filename)


@app.route('/generate')
def generate():
    # If already running, check for timeout
    if pipeline_status['running']:
        elapsed = time.time() - (pipeline_status['started_at'] or time.time())
        if elapsed > PIPELINE_TIMEOUT:
            pipeline_status['running'] = False
            pipeline_status['error'] = f'Pipeline timed out after {int(elapsed)}s. Last stage: {pipeline_status["stage"]}'
        else:
            return render_template('processing.html')

    # Reset state
    pipeline_status.update({'running': True, 'error': None, 'stage': 'Starting', 'log': [], 'started_at': time.time(), 'api_calls': []})

    def run():
        try:
            import pipeline as pl

            def log_callback(msg):
                pipeline_log(msg)
                if 'Moderating and scoring' in msg or 'Scoring' in msg:
                    pipeline_status['stage'] = msg
                elif 'cached photo scores' in msg:
                    pipeline_status['stage'] = 'Loading cached scores'
                elif 'Generating story' in msg:
                    pipeline_status['stage'] = 'Generating story & stats'
                elif 'Done.' in msg:
                    pipeline_status['stage'] = 'Done'
                
                pipeline_status['api_calls'] = list(pl.api_calls_log)

            pl.run_pipeline(SESSION_ID, log=log_callback)
            pipeline_log('Pipeline complete!')
            pipeline_status['stage'] = 'Done'
            pipeline_status['api_calls'] = list(pl.api_calls_log)

        except Exception as e:
            err = str(e)
            pipeline_log(f'ERROR at stage "{pipeline_status["stage"]}": {err}')
            pipeline_status['error'] = f'[{pipeline_status["stage"]}] {err}'
        finally:
            pipeline_status['running'] = False

    threading.Thread(target=run, daemon=True).start()
    return render_template('processing.html')


@app.route('/status')
def status():
    elapsed = int(time.time() - pipeline_status['started_at']) if pipeline_status['started_at'] else 0

    # Auto-detect timeout in status poll
    if pipeline_status['running'] and elapsed > PIPELINE_TIMEOUT:
        pipeline_status['running'] = False
        pipeline_status['error'] = f'Timed out after {elapsed}s at stage: {pipeline_status["stage"]}'

    done = os.path.exists(f'{OUTPUT_DIR}/story.txt')
    response = app.response_class(
        response=json.dumps({
            'running': pipeline_status['running'],
            'done': done,
            'error': pipeline_status['error'],
            'stage': pipeline_status['stage'],
            'log': pipeline_status['log'],
            'elapsed': elapsed,
            'api_calls': pipeline_status.get('api_calls', []),
        }),
        status=200,
        mimetype='application/json',
    )
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, public, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/results')
def results():
    from flask import make_response
    story, stats, highlights, cost = '', '', [], None
    for attr, path, loader in [
        ('story',      f'{OUTPUT_DIR}/story.txt',       lambda f: f.read()),
        ('stats',      f'{OUTPUT_DIR}/stats.json',      json.load),
        ('highlights', f'{OUTPUT_DIR}/highlights.json', lambda f: json.load(f).get('photos', [])),
        ('cost',       f'{OUTPUT_DIR}/cost.json',       json.load),
    ]:
        try:
            with open(path) as f:
                val = loader(f)
            if attr == 'story': story = val
            elif attr == 'stats': stats = val
            elif attr == 'cost': cost = val
            else: highlights = val
        except Exception as e:
            print(f'Results load error ({attr}): {e}')
    
    response = make_response(render_template('result.html', story=story, stats=stats, highlights=highlights, cost=cost))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, public, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
