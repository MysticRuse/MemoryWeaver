from flask import Flask, request, render_template, redirect, url_for, send_from_directory, session, make_response
import os, threading, time, json
from dotenv import load_dotenv
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
register_heif_opener()
load_dotenv()

THUMB_MAX_PX = 1024  # max dimension for display thumbnails
SESSION_ID = 'FIFA26'

# Pipeline state — shared between request thread and background worker
pipeline_status = {
    'running': False,
    'error': None,
    'stage': None,       # current step shown in UI
    'log': [],           # rolling log lines shown in UI
    'started_at': None,
    'api_calls': []
}
PIPELINE_TIMEOUT = 300  # 5 minutes hard timeout

STATUS_FILE = f'outputs/{SESSION_ID}/pipeline_status.json'
status_lock = threading.Lock()

def load_pipeline_status():
    global pipeline_status
    with status_lock:
        if os.path.exists(STATUS_FILE):
            try:
                with open(STATUS_FILE, 'r') as f:
                    pipeline_status = json.load(f)
            except Exception:
                pass
    return pipeline_status

def save_pipeline_status():
    with status_lock:
        try:
            os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
            with open(STATUS_FILE, 'w') as f:
                json.dump(pipeline_status, f, indent=2)
        except Exception as e:
            print(f"Error saving pipeline status: {e}")

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'memoryweaver-super-secret-key-12984')
UPLOAD_DIR = f'uploads/{SESSION_ID}'
THUMB_DIR  = f'uploads/{SESSION_ID}/thumbs'
OUTPUT_DIR = f'outputs/{SESSION_ID}'
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(THUMB_DIR,  exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
LOG_FILE = f'outputs/{SESSION_ID}/app_logs.json'

from concurrent.futures import ThreadPoolExecutor
# Background worker pool for CPU-heavy thumbnail scaling tasks
thumb_executor = ThreadPoolExecutor(max_workers=3)
# Global thread lock to serialize app log writes and prevent JSON corruption under load spikes
log_lock = threading.Lock()

def log_event(event_type, message, level="INFO", details=None):
    """Log structured diagnostics for cofounders."""
    import datetime
    with log_lock:
        try:
            os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
            log_entry = {
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "level": level,
                "type": event_type,
                "message": message,
                "details": details or {}
            }
            
            from flask import has_request_context, request
            if has_request_context():
                ua = request.user_agent
                log_entry["request_info"] = {
                    "ip": request.headers.get('X-Forwarded-For', request.remote_addr),
                    "url": request.url,
                    "method": request.method,
                    "platform": ua.platform,
                    "browser": ua.browser,
                    "version": ua.version,
                    "user_agent": ua.string
                }
            
            logs = []
            if os.path.exists(LOG_FILE):
                try:
                    with open(LOG_FILE, 'r') as f:
                        logs = json.load(f)
                except Exception:
                    logs = []
                    
            logs.append(log_entry)
            if len(logs) > 200:
                logs = logs[-200:]
                
            with open(LOG_FILE, 'w') as f:
                json.dump(logs, f, indent=2)
        except Exception as e:
            print(f"Logging system error: {e}")



def _make_thumbnail(src_path, filename):
    """Resize to max 1024px, save as JPEG in thumbs dir. Silently skips on error."""
    try:
        img = Image.open(src_path)
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX), Image.LANCZOS)
        thumb_name = os.path.splitext(filename)[0] + '.jpg'
        img.save(os.path.join(THUMB_DIR, thumb_name), format="JPEG", quality=82)
    except Exception as e:
        print(f'Thumbnail error for {filename}: {e}')


def pipeline_log(msg):
    print(msg)
    load_pipeline_status()
    pipeline_status['log'].append(msg)
    if len(pipeline_status['log']) > 50:
        pipeline_status['log'].pop(0)
    save_pipeline_status()


PUBLIC_URL = os.getenv('PUBLIC_URL', '')

@app.route('/')
def index():
    all_files = [f for f in os.listdir(UPLOAD_DIR) if os.path.isfile(os.path.join(UPLOAD_DIR, f)) and not f.startswith('.')]
    count = len(all_files)
    
    # Filter session uploaded files to ensure they actually exist on disk
    my_uploads = session.get('my_uploads', [])
    my_uploads = [f for f in my_uploads if f in all_files]
    session['my_uploads'] = my_uploads
    
    import pipeline as pl
    my_uploads_classified = {
        'pre-match': [],
        'in-match': [],
        'post-match': []
    }
    for f in my_uploads:
        cat, ts = pl.classify_by_metadata_only(UPLOAD_DIR, f)
        item = {'filename': f, 'timestamp': ts}
        if cat in my_uploads_classified:
            my_uploads_classified[cat].append(item)
        else:
            my_uploads_classified['in-match'].append(item)
            
    story_ready = os.path.exists(f'{OUTPUT_DIR}/story.txt')
    success = request.args.get('success')
    
    # Calculate current score based on time and overrides
    override_score = os.getenv('OVERRIDE_SCORE')
    override_ts = os.getenv('OVERRIDE_TIMESTAMP')
    if override_ts:
        try:
            now_ts = float(override_ts)
        except ValueError:
            now_ts = time.time()
    else:
        now_ts = time.time()

    if override_score == '0-0':
        score_text = "0 - 0"
        quest_score = "SCORE: 0 - 0"
    elif now_ts < 1782439200:
        score_text = "VS"
        quest_score = "SCORE: 0 - 0"
    elif now_ts < 1782440640:
        score_text = "0 - 0"
        quest_score = "SCORE: 0 - 0"
    elif now_ts < 1782444120:
        score_text = "0 - 1"
        quest_score = "SCORE: 0 - 1"
    else:
        score_text = "1 - 1"
        quest_score = "SCORE: 1 - 1"
    
    response = make_response(render_template(
        'uploads.html',
        count=count,
        public_url=PUBLIC_URL,
        story_ready=story_ready,
        success=success,
        uploaded_files=my_uploads,
        uploaded_files_classified=my_uploads_classified,
        score_text=score_text,
        quest_score=quest_score,
        override_score=override_score or '',
        override_timestamp=override_ts or ''
    ))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, public, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


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
        save_pipeline_status()
        
        success_msg = "Successfully cleared all photos and generated stories!"
        log_event("CLEAR", "Cleared all photo uploads and generated highlights/stories", level="INFO")
    except Exception as e:
        success_msg = f"Error clearing: {e}"
        log_event("CLEAR", f"Error clearing: {e}", level="ERROR")
        
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
        save_pipeline_status()
        success_msg = f"Deleted memory: {filename}"
        log_event("DELETE", f"Deleted photo memory: {filename}", level="INFO", details={"filename": filename})
    except Exception as e:
        success_msg = f"Error deleting file: {e}"
        log_event("DELETE", f"Error deleting memory {filename}: {e}", level="ERROR", details={"filename": filename})
    
    return redirect(url_for('index', success=success_msg))


@app.route('/upload', methods=['POST'])
def upload():
    files = request.files.getlist('photos')
    quest_ts = request.form.get('quest_timestamp')
    if quest_ts:
        try:
            ts_val = int(quest_ts)
        except ValueError:
            ts_val = int(time.time())
    else:
        ts_val = int(time.time())
    
    saved_files = []
    import uuid
    for f in files:
        if f.filename:
            base, ext = os.path.splitext(f.filename)
            unique_name = f"{base}_{ts_val}_{uuid.uuid4().hex[:6]}{ext.lower()}"
            dest = os.path.join(UPLOAD_DIR, unique_name)
            f.save(dest)
            # Asynchronously scale and save display thumbnails in the background
            thumb_executor.submit(_make_thumbnail, dest, unique_name)
            saved_files.append(unique_name)
            
    # Append to current session uploads
    my_uploads = session.get('my_uploads', [])
    for sf in saved_files:
        if sf not in my_uploads:
            my_uploads.append(sf)
    session['my_uploads'] = my_uploads
    
    if len(saved_files) > 0:
        session['hitl_consent'] = 'PENDING'
        for artefact in ('story.txt', 'stats.json', 'highlights.json'):
            path = os.path.join(OUTPUT_DIR, artefact)
            if os.path.exists(path):
                os.remove(path)
                
    all_files = [f for f in os.listdir(UPLOAD_DIR) if os.path.isfile(os.path.join(UPLOAD_DIR, f)) and not f.startswith('.')]
    count = len(all_files)
    
    # Filter to make sure session files exist
    session_files = [f for f in my_uploads if f in all_files]
    session['my_uploads'] = session_files
    
    filenames_str = ",".join(saved_files)
    log_event("UPLOAD", f"Uploaded {len(saved_files)} photo memory/memories", level="INFO", details={"files": saved_files})
    return redirect(url_for('index', success=f'Added {len(saved_files)} photo(s)!', new_files=filenames_str))


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
    load_pipeline_status()
    # If already running, check for timeout
    if pipeline_status['running']:
        elapsed = time.time() - (pipeline_status['started_at'] or time.time())
        if elapsed > PIPELINE_TIMEOUT:
            pipeline_status['running'] = False
            pipeline_status['error'] = f'Pipeline timed out after {int(elapsed)}s. Last stage: {pipeline_status["stage"]}'
            save_pipeline_status()
        else:
            return render_template('processing.html')

    # Get requested limit parameter
    limit = request.args.get('limit', default=10, type=int)

    # Reset state
    pipeline_status.update({'running': True, 'error': None, 'stage': 'Starting', 'log': [], 'started_at': time.time(), 'api_calls': []})
    save_pipeline_status()
    log_event("PIPELINE", f"Started AI Story Weaver Pipeline with limit {limit}", level="INFO")

    def run():
      try:
        import pipeline as pl

        def log_callback(msg):
          pipeline_log(msg)
          load_pipeline_status()
          if 'Moderating and scoring' in msg or 'Scoring' in msg:
            pipeline_status['stage'] = msg
          elif 'cached photo scores' in msg:
            pipeline_status['stage'] = 'Loading cached scores'
          elif 'Generating story' in msg:
            pipeline_status['stage'] = 'Generating story & stats'
          elif 'Done.' in msg:
            pipeline_status['stage'] = 'Done'
          
          pipeline_status['api_calls'] = list(pl.api_calls_log)
          save_pipeline_status()

        pl.run_pipeline(SESSION_ID, log=log_callback, limit=limit)
        pipeline_log('Pipeline complete!')
        load_pipeline_status()
        pipeline_status['stage'] = 'Done'
        pipeline_status['api_calls'] = list(pl.api_calls_log)
        save_pipeline_status()
        
        # Log successful completion and API call logs (costing and details)
        log_event("PIPELINE", "AI Story Weaver Pipeline completed successfully", level="INFO", details={
            "api_calls": list(pl.api_calls_log),
            "total_cost_usd": sum(x["cost_usd"] for x in pl.api_calls_log)
        })

      except Exception as e:
        err = str(e)
        pipeline_log(f'ERROR at stage "{pipeline_status["stage"]}": {err}')
        load_pipeline_status()
        pipeline_status['error'] = f'[{pipeline_status["stage"]}] {err}'
        save_pipeline_status()
        log_event("ERROR", f"Pipeline error at stage '{pipeline_status['stage']}': {err}", level="ERROR")
      finally:
        load_pipeline_status()
        pipeline_status['running'] = False
        save_pipeline_status()

    threading.Thread(target=run, daemon=True).start()
    return render_template('processing.html')


@app.route('/status')
def status():
    load_pipeline_status()
    elapsed = int(time.time() - pipeline_status['started_at']) if pipeline_status['started_at'] else 0

    # Auto-detect timeout in status poll
    if pipeline_status['running'] and elapsed > PIPELINE_TIMEOUT:
        pipeline_status['running'] = False
        pipeline_status['error'] = f'Timed out after {elapsed}s at stage: {pipeline_status["stage"]}'
        save_pipeline_status()

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


@app.route('/album-count')
def album_count():
    all_files = [f for f in os.listdir(UPLOAD_DIR) if os.path.isfile(os.path.join(UPLOAD_DIR, f)) and not f.startswith('.')]
    count = len(all_files)
    response = app.response_class(
        response=json.dumps({'count': count}),
        status=200,
        mimetype='application/json'
    )
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, public, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response
@app.route('/album-files')
def album_files():
    all_files = [f for f in os.listdir(UPLOAD_DIR) if os.path.isfile(os.path.join(UPLOAD_DIR, f)) and not f.startswith('.')]
    response = app.response_class(
        response=json.dumps({'files': all_files}),
        status=200,
        mimetype='application/json'
    )
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, public, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/album-classified')
def album_classified():
    import pipeline as pl
    all_files = [f for f in os.listdir(UPLOAD_DIR) if os.path.isfile(os.path.join(UPLOAD_DIR, f)) and not f.startswith('.')]
    
    classified = {
        'pre-match': [],
        'in-match': [],
        'post-match': []
    }
    
    for f in all_files:
        cat, ts = pl.classify_by_metadata_only(UPLOAD_DIR, f)
        item = {'filename': f, 'timestamp': ts}
        if cat in classified:
            classified[cat].append(item)
        else:
            classified['in-match'].append(item)
            
    response = app.response_class(
        response=json.dumps(classified),
        status=200,
        mimetype='application/json'
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
            
    hitl_consent = session.get('hitl_consent', 'PENDING')
    
    # Read security check stats
    security_check = {}
    if isinstance(stats, dict):
        security_check = stats.get('security_check', {})
        
    if not security_check:
        security_check = {
            "privacy": "PROTECTED",
            "consent": hitl_consent,
            "ready_to_post": "YES" if hitl_consent == 'CONFIRMED' else "NEEDS REVIEW"
        }
    else:
        security_check['consent'] = hitl_consent
        security_check['ready_to_post'] = "YES" if hitl_consent == 'CONFIRMED' else "NEEDS REVIEW"
    
    response = make_response(render_template(
        'result.html', 
        story=story, 
        stats=stats, 
        highlights=highlights, 
        cost=cost,
        hitl_consent=hitl_consent,
        security_check=security_check
    ))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, public, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/share-consent', methods=['POST'])
def share_consent():
    choice = request.form.get('consent_choice')
    if choice in ['CONFIRMED', 'PRIVATE']:
        session['hitl_consent'] = choice
        
        # Update stats.json on disk dynamically to synchronize security_check status
        try:
            stats_path = f'{OUTPUT_DIR}/stats.json'
            if os.path.exists(stats_path):
                with open(stats_path, 'r') as f:
                    stats = json.load(f)
                if 'security_check' not in stats:
                    stats['security_check'] = {}
                stats['security_check']['consent'] = choice
                stats['security_check']['ready_to_post'] = 'YES' if choice == 'CONFIRMED' else 'NEEDS REVIEW'
                with open(stats_path, 'w') as f:
                    json.dump(stats, f, indent=2)
        except Exception as e:
            print(f"Error updating stats consent: {e}")
            
        log_event("PIPELINE", f"Human-in-the-Loop Consent updated to: {choice}", level="INFO")
        return redirect(url_for('results'))
    return redirect(url_for('index', error="Invalid consent option"))


@app.route('/logs')
def show_logs():
    logs = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, 'r') as f:
                logs = json.load(f)
        except Exception:
            logs = []
            
    all_files = [f for f in os.listdir(UPLOAD_DIR) if os.path.isfile(os.path.join(UPLOAD_DIR, f)) and not f.startswith('.')]
    total_uploads = len(all_files)
    
    total_cost = 0.0
    cost_path = f'{OUTPUT_DIR}/cost.json'
    if os.path.exists(cost_path):
        try:
            with open(cost_path) as f:
                cost_data = json.load(f)
                total_cost = cost_data.get('total_cost_usd', 0.0)
        except Exception:
            pass
            
    total_errors = sum(1 for log in logs if log.get('level') == 'ERROR')
    
    response = make_response(render_template(
        'logs.html',
        logs=reversed(logs),
        total_uploads=total_uploads,
        total_cost=total_cost,
        total_errors=total_errors
    ))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, public, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
