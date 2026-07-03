from flask import Flask, request, render_template, redirect, url_for, send_from_directory, session, make_response
import os, threading, time, json
from dotenv import load_dotenv
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
register_heif_opener()
load_dotenv()

THUMB_MAX_PX = 1024  # max dimension for display thumbnails


# Pipeline state — shared between request thread and background worker
pipeline_statuses = {}
PIPELINE_TIMEOUT = 300  # 5 minutes hard timeout


status_lock = threading.Lock()


def load_pipeline_status(session_id=None):
    if not session_id: session_id = g.session_id
    global pipeline_statuses
    with status_lock:
        if session_id not in pipeline_statuses:
            pipeline_statuses[session_id] = {
                'running': False, 'error': None, 'stage': None,
                'log': [], 'started_at': None, 'api_calls': []
            }
        sf = get_status_file(session_id)
        if os.path.exists(sf):
            try:
                with open(sf, 'r') as f:
                    pipeline_statuses[session_id] = json.load(f)
            except Exception:
                pass
    return pipeline_statuses[session_id]

def save_pipeline_status(session_id=None):
    if not session_id: session_id = g.session_id
    with status_lock:
        try:
            sf = get_status_file(session_id)
            os.makedirs(os.path.dirname(sf), exist_ok=True)
            with open(sf, 'w') as f:
                json.dump(pipeline_statuses[session_id], f, indent=2)
        except Exception as e:
            print(f"Error saving pipeline status: {e}")


app = Flask(__name__)

from flask import Blueprint, g

match_bp = Blueprint('match', __name__, url_prefix='/<session_id>')

@match_bp.url_value_preprocessor
def pull_session_id(endpoint, values):
    raw_session = values.pop('session_id')
    if raw_session == 'FIFA26':
        g.session_id = 'FIFA26_PARAGUAY_AUSTRALIA'
    elif raw_session.startswith('FIFA_'):
        g.session_id = 'FIFA26_' + raw_session[5:]
    else:
        g.session_id = raw_session

@match_bp.url_defaults
def add_session_id(endpoint, values):
    if 'session_id' not in values and hasattr(g, 'session_id'):
        values['session_id'] = g.session_id

def get_upload_dir(session_id=None):
    if not session_id: session_id = g.session_id
    d = f'uploads/{session_id}'
    os.makedirs(d, exist_ok=True)
    return d

def get_thumb_dir(session_id=None):
    if not session_id: session_id = g.session_id
    d = f'uploads/{session_id}/thumbs'
    os.makedirs(d, exist_ok=True)
    return d

def get_output_dir(session_id=None):
    if not session_id: session_id = g.session_id
    d = f'outputs/{session_id}'
    os.makedirs(d, exist_ok=True)
    return d

def get_log_file(session_id=None):
    if not session_id: session_id = g.session_id
    return f'outputs/{session_id}/app_logs.json'

def get_status_file(session_id=None):
    if not session_id: session_id = g.session_id
    return f'outputs/{session_id}/pipeline_status.json'

@app.route('/')
def lobby():
    return render_template('lobby.html')

app.secret_key = os.getenv('SECRET_KEY', 'memoryweaver-super-secret-key-12984')






app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024


from concurrent.futures import ThreadPoolExecutor
# Background worker pool for CPU-heavy thumbnail scaling tasks
thumb_executor = ThreadPoolExecutor(max_workers=3)
# Global thread lock to serialize app log writes and prevent JSON corruption under load spikes
log_lock = threading.Lock()

def log_event(event_type, message, level="INFO", details=None, session_id=None):
    if not session_id: session_id = getattr(g, "session_id", "UNKNOWN")
    """Log structured diagnostics for cofounders."""
    import datetime
    with log_lock:
        try:
            os.makedirs(os.path.dirname(get_log_file(session_id)), exist_ok=True)
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
            if os.path.exists(get_log_file(session_id)):
                try:
                    with open(get_log_file(session_id), 'r') as f:
                        logs = json.load(f)
                except Exception:
                    logs = []
                    
            logs.append(log_entry)
            if len(logs) > 200:
                logs = logs[-200:]
                
            with open(get_log_file(session_id), 'w') as f:
                json.dump(logs, f, indent=2)
        except Exception as e:
            print(f"Logging system error: {e}")



def _make_thumbnail(session_id, src_path, filename):
    """Resize to max 1024px, save as JPEG in thumbs dir. Silently skips on error."""
    try:
        img = Image.open(src_path)
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX), Image.LANCZOS)
        thumb_name = os.path.splitext(filename)[0] + '.jpg'
        img.save(os.path.join(get_thumb_dir(g.session_id), thumb_name), format="JPEG", quality=82)
    except Exception as e:
        print(f'Thumbnail error for {filename}: {e}')


def pipeline_log(session_id, msg):
    print(msg)
    load_pipeline_status(session_id)
    pipeline_statuses[session_id]['log'].append(msg)
    if len(pipeline_statuses[session_id]['log']) > 50:
        pipeline_statuses[session_id]['log'].pop(0)
    save_pipeline_status(session_id)


PUBLIC_URL = os.getenv('PUBLIC_URL', '')


def get_match_metadata(team1, team2):

    metadata = {
        'kickoff': '2026-06-25T19:00:00-07:00',
        'end': '2026-06-25T21:00:00-07:00',
        'flag1': '🏳️',
        'flag2': '🏳️',
        'kickoff_ts': 1782439200.0,
        'team1_name': team1.replace('_', ' ').title() if (team1 and team1 != 'Team 1') else 'Paraguay',
        'team2_name': team2.replace('_', ' ').title() if (team2 and team2 != 'Team 2') else 'Australia',
        'stadium_name': "Levi's Stadium",
        'stadium_location': "Santa Clara, CA",
        'date_str': "Thursday, June 25, 2026",
        'short_date': "June 25, 2026",
        'time_str': "7:00 PM - 9:00 PM PST"
    }
    
    metadata['squad1'] = {
        'GK': 'G. Fernández, O. Gill, G. Olveira',
        'DF': 'J. Gómez (C), J. Alonso, Fabián Balbuena, O. Alderete, J. Cáceres, G. Velázquez, J. Canale, A. Maidana',
        'MF': 'M. Almirón, Kaku, A. Cubas, R. Sosa, Diego Gómez, D. Bobadilla, B. Ojeda, M. Galarza, Maurício',
        'FW': 'Antonio Sanabria, Julio Enciso, G. Ávalos, Álex Arce, Isidro Pitta'
    }
    metadata['squad2'] = {
        'GK': 'Mathew Ryan, Paul Izzo, Patrick Beach',
        'DF': 'A. Behich, Jordan Bos, C. Burgess, A. Circati, M. Degenek, J. Geria, L. Herrington, H. Souttar, K. Trewin',
        'MF': 'Jackson Irvine, N. Irankunda, C. Volpato, M. Balard, C. Metcalfe, A. O\'Neill, K. Baccus',
        'FW': 'M. Touré, Tete Yengi, Mitch Duke, B. Borrello, K. Yeboah, A. Goodwin'
    }
    
    if team1.lower() == 'paraguay' and team2.lower() == 'australia':
        metadata['flag1'] = '🇵🇾'
        metadata['flag2'] = '🇦🇺'
    elif team1.lower() == 'switzerland' and team2.lower() == 'algeria':
        metadata['kickoff'] = '2026-07-02T20:00:00-07:00'
        metadata['end'] = '2026-07-02T22:00:00-07:00'
        metadata['flag1'] = '🇨🇭'
        metadata['flag2'] = '🇩🇿'
        metadata['kickoff_ts'] = 1783047600
        metadata['stadium_name'] = "BC Place"
        metadata['stadium_location'] = "Vancouver, BC"
        metadata['date_str'] = "Thursday, July 2, 2026"
        metadata['short_date'] = "July 2, 2026"
        metadata['time_str'] = "8:00 PM - 10:00 PM PDT"
        metadata['squad1'] = {
            'GK': 'Yann Sommer, Gregor Kobel, Jonas Omlin',
            'DF': 'Manuel Akanji, Ricardo Rodriguez, Fabian Schär, Nico Elvedi, Silvan Widmer, Kevin Mbabu, Cédric Zesiger',
            'MF': 'Granit Xhaka (C), Remo Freuler, Xherdan Shaqiri, Denis Zakaria, Djibril Sow, Michel Aebischer, Fabian Rieder',
            'FW': 'Breel Embolo, Noah Okafor, Ruben Vargas, Zeki Amdouni, Haris Seferovic'
        }
        metadata['squad2'] = {
            'GK': 'Anthony Mandrea, Moustapha Zeghba, Oussama Benbot',
            'DF': 'Ramy Bensebaini, Aïssa Mandi, Youcef Atal, Rayan Aït-Nouri, Ahmed Touba, Kevin Van Den Kerkhof',
            'MF': 'Ismaël Bennacer, Ramiz Zerrouki, Houssem Aouar, Nabil Bentaleb, Sofiane Feghouli (C), Hicham Boudaoui, Farès Chaïbi',
            'FW': 'Riyad Mahrez, Islam Slimani, Baghdad Bounedjah, Amine Gouiri, Saïd Benrahma, Youssef Belaïli, Mohamed Amoura'
        }        
    # Calculate quest timestamps based on kickoff
    kts = metadata['kickoff_ts']
    metadata['quest_kickoff'] = kts
    metadata['quest_24'] = kts + (24 * 60)
    metadata['quest_38'] = kts + (38 * 60)
    metadata['quest_45'] = kts + (45 * 60)
    metadata['quest_67'] = kts + (67 * 60)
    metadata['quest_78'] = kts + (78 * 60)
    metadata['quest_90'] = kts + (90 * 60)
    
    return metadata


@match_bp.route('/')
def index():
    all_files = [f for f in os.listdir(get_upload_dir()) if os.path.isfile(os.path.join(get_upload_dir(), f)) and not f.startswith('.')]
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
        cat, ts = pl.classify_by_metadata_only(get_upload_dir(), f, kickoff_ts=get_match_metadata(*g.session_id.split('_')[1:3]).get('kickoff_ts', 1782439200), fulltime_ts=get_match_metadata(*g.session_id.split('_')[1:3]).get('kickoff_ts', 1782439200) + 10800)
        item = {'filename': f, 'timestamp': ts}
        if cat in my_uploads_classified:
            my_uploads_classified[cat].append(item)
        else:
            my_uploads_classified['in-match'].append(item)
            
    story_ready = os.path.exists(f'{get_output_dir()}/story.txt') and os.path.getsize(f'{get_output_dir()}/story.txt') > 0
    success = request.args.get('success')
    
    import pipeline as pl
    max_ts = 0
    for f in all_files:
        if f.startswith('.'): continue
        try:
            _, ts = pl.classify_by_metadata_only(get_upload_dir(), f, kickoff_ts=get_match_metadata(*g.session_id.split('_')[1:3]).get('kickoff_ts', 1782439200), fulltime_ts=get_match_metadata(*g.session_id.split('_')[1:3]).get('kickoff_ts', 1782439200) + 10800)
            if ts > max_ts: max_ts = ts
        except Exception:
            pass

    detected_score = None
    highlights_path = f'{get_output_dir()}/highlights.json'
    if os.path.exists(highlights_path):
        try:
            with open(highlights_path) as f:
                detected_score = json.load(f).get('detected_score')
        except Exception:
            pass

    override_score = os.getenv('OVERRIDE_SCORE') or detected_score
    override_ts = os.getenv('OVERRIDE_TIMESTAMP')
    if override_ts:
        try:
            now_ts = float(override_ts)
        except ValueError:
            now_ts = max_ts if max_ts > 0 else time.time()
    else:
        now_ts = max_ts if max_ts > 0 else time.time()

    if override_score == 'HIDDEN':
        score_text = "VS"
        quest_score = ""
    elif override_score:
        score_text = override_score.replace('-', ' - ')
        quest_score = f"SCORE: {score_text}"
    else:
        score_text = "VS"
        quest_score = ""
    

    raw_id = g.session_id
    if raw_id.startswith('FIFA26_'):
        raw_id = raw_id[7:]
    elif raw_id.startswith('FIFA_'):
        raw_id = raw_id[5:]
        
    if raw_id in ('FIFA', 'FIFA26'):
        team1 = 'Paraguay'
        team2 = 'Australia'
    elif '_' in raw_id:
        parts = raw_id.split('_', 1)
        team1 = parts[0].replace('-', ' ').title()
        team2 = parts[1].replace('-', ' ').title()
    else:
        team1 = 'Team 1'
        team2 = 'Team 2'

    metadata = get_match_metadata(team1, team2)

    response = make_response(render_template(
        'uploads.html',
        team1=team1, team2=team2, metadata=metadata,
        count=count,
        public_url=PUBLIC_URL,
        story_ready=story_ready,
        success=success,
        uploaded_files=my_uploads,
        uploaded_files_classified=my_uploads_classified,
        score_text=score_text,
        quest_score=quest_score,
        override_score=override_score or '',
        override_timestamp=str(now_ts)
    ))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, public, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@match_bp.route('/clear')
def clear_all():
    import shutil
    try:
        # Clear uploads and thumbs
        if os.path.exists(get_upload_dir()):
            shutil.rmtree(get_upload_dir())
        
        os.makedirs(get_thumb_dir(g.session_id), exist_ok=True)
        
        # Clear outputs
        if os.path.exists(get_output_dir()):
            shutil.rmtree(get_output_dir())
        
        
        # Reset current session uploads
        session['my_uploads'] = []
        
        # Reset pipeline status
        pipeline_statuses[session_id].update({
            'running': False,
            'error': None,
            'stage': 'Starting',
            'log': [],
            'started_at': None,
            'api_calls': []
        })
        save_pipeline_status(session_id)
        
        success_msg = "Successfully cleared all photos and generated stories!"
        log_event("CLEAR", "Cleared all photo uploads and generated highlights/stories", level="INFO")
    except Exception as e:
        success_msg = f"Error clearing: {e}"
        log_event("CLEAR", f"Error clearing: {e}", level="ERROR")
        
    return redirect(url_for('match.index', success=success_msg))


@match_bp.route('/delete/<path:filename>')
def delete_file(filename):
    try:
        # Delete original file
        orig_path = os.path.join(get_upload_dir(), filename)
        if os.path.exists(orig_path):
            os.remove(orig_path)
            
        # Delete thumbnail
        thumb_name = os.path.splitext(filename)[0] + '.jpg'
        thumb_path = os.path.join(get_thumb_dir(g.session_id), thumb_name)
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
            
        # Remove from current session uploads
        my_uploads = session.get('my_uploads', [])
        if filename in my_uploads:
            my_uploads.remove(filename)
            session['my_uploads'] = my_uploads
            
        # If output files exist, remove them since album has changed
        for artefact in ('story.txt', 'stats.json', 'highlights.json'):
            path = os.path.join(get_output_dir(), artefact)
            if os.path.exists(path):
                os.remove(path)
                
        # Reset pipeline status
        pipeline_statuses[session_id].update({
            'running': False,
            'error': None,
            'stage': 'Starting',
            'log': [],
            'started_at': None,
            'api_calls': []
        })
        save_pipeline_status(session_id)
        success_msg = f"Deleted memory: {filename}"
        log_event("DELETE", f"Deleted photo memory: {filename}", level="INFO", details={"filename": filename})
    except Exception as e:
        success_msg = f"Error deleting file: {e}"
        log_event("DELETE", f"Error deleting memory {filename}: {e}", level="ERROR", details={"filename": filename})
    
    return redirect(url_for('match.index', success=success_msg))


@match_bp.route('/upload', methods=['POST'])
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
            dest = os.path.join(get_upload_dir(), unique_name)
            f.save(dest)
            # Asynchronously scale and save display thumbnails in the background
            thumb_executor.submit(_make_thumbnail, g.session_id, dest, unique_name)
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
            path = os.path.join(get_output_dir(), artefact)
            if os.path.exists(path):
                os.remove(path)
                
    all_files = [f for f in os.listdir(get_upload_dir()) if os.path.isfile(os.path.join(get_upload_dir(), f)) and not f.startswith('.')]
    count = len(all_files)
    
    # Filter to make sure session files exist
    session_files = [f for f in my_uploads if f in all_files]
    session['my_uploads'] = session_files
    
    filenames_str = ",".join(saved_files)
    log_event("UPLOAD", f"Uploaded {len(saved_files)} photo memory/memories", level="INFO", details={"files": saved_files})
    return redirect(url_for('match.index', success=f'Added {len(saved_files)} photo(s)!', new_files=filenames_str))


@match_bp.route('/uploads/<filename>')
def uploaded_file(filename):
    base, _ = os.path.splitext(filename)
    dir_path = os.path.abspath(get_upload_dir())
    if os.path.exists(dir_path):
        for f in os.listdir(dir_path):
            if f.startswith(base + '.') and os.path.isfile(os.path.join(dir_path, f)):
                return send_from_directory(dir_path, f)
    return send_from_directory(dir_path, filename)


@match_bp.route('/thumbs/<filename>')
def thumb_file(filename):
    thumb_name = os.path.splitext(filename)[0] + '.jpg'
    thumb_path = os.path.join(get_thumb_dir(g.session_id), thumb_name)
    if os.path.exists(thumb_path):
        return send_from_directory(os.path.abspath(get_thumb_dir(g.session_id)), thumb_name)
    # Fall back to original if thumbnail wasn't generated
    return send_from_directory(os.path.abspath(get_upload_dir()), filename)


@match_bp.route('/generate', methods=['GET', 'POST'])
def generate():
    session_id = g.session_id
    if request.method == 'GET':
        
        files = [f for f in os.listdir(get_upload_dir()) if os.path.isfile(os.path.join(get_upload_dir(), f)) and not f.startswith('.')]
        photo_count = sum(1 for f in files if f.lower().endswith(('.jpg', '.jpeg', '.png', '.heic')))
        video_count = sum(1 for f in files if f.lower().endswith(('.mp4', '.mov')))
        
        import re, hashlib
        seen_bases = set()
        seen_hashes = set()
        duplicate_count = 0
        for f in files:
            base_match = re.search(r'^(.*?)_\d{10}_[0-9a-f]{6}(\.[a-zA-Z0-9]+)$', f)
            original_base = (base_match.group(1) + base_match.group(2)).lower() if base_match else f.lower()
            if original_base in seen_bases:
                duplicate_count += 1
                continue
            try:
                with open(os.path.join(get_upload_dir(), f), 'rb') as fh:
                    f_hash = hashlib.md5(fh.read()).hexdigest()
                if f_hash in seen_hashes:
                    duplicate_count += 1
                    continue
                seen_hashes.add(f_hash)
            except Exception:
                pass
            seen_bases.add(original_base)
            
        return render_template('admin_generate.html', photo_count=photo_count, video_count=video_count, duplicate_count=duplicate_count)

    limit = request.form.get('limit', default=10, type=int)
    consider_limit = request.form.get('consider_limit', default=100, type=int)
    include_videos = request.form.get('include_videos') == 'on'
    
    load_pipeline_status(session_id)
    # If already running, check for timeout
    if pipeline_statuses[session_id]['running']:
        elapsed = time.time() - (pipeline_statuses[session_id]['started_at'] or time.time())
        if elapsed > PIPELINE_TIMEOUT:
            pipeline_statuses[session_id]['running'] = False
            pipeline_statuses[session_id]['error'] = f'Pipeline timed out after {int(elapsed)}s. Last stage: {pipeline_status["stage"]}'
            save_pipeline_status(session_id)
        else:
            return render_template('processing.html')

    # Reset state
    pipeline_statuses[session_id].update({'running': True, 'error': None, 'stage': 'Starting', 'log': [], 'started_at': time.time(), 'api_calls': []})
    save_pipeline_status(session_id)
    log_event("PIPELINE", f"Started AI Story Weaver Pipeline with limit {limit} and consider_limit {consider_limit}", level="INFO")

    def run(session_id):
      try:
        import pipeline as pl

        def log_callback(msg):
          pipeline_log(session_id, msg)
          load_pipeline_status(session_id)
          if 'Moderating and scoring' in msg or 'Scoring' in msg:
            pipeline_statuses[session_id]['stage'] = msg
          elif 'cached photo scores' in msg:
            pipeline_statuses[session_id]['stage'] = 'Loading cached scores'
          elif 'Generating story' in msg:
            pipeline_statuses[session_id]['stage'] = 'Generating story & stats'
          elif 'Done.' in msg:
            pipeline_statuses[session_id]['stage'] = 'Done'
          
          pipeline_statuses[session_id]['api_calls'] = list(pl.api_calls_log)
          save_pipeline_status(session_id)

        parts = session_id.split('_')
        meta = get_match_metadata(parts[1], parts[2]) if len(parts) >= 3 else None
        pl.run_pipeline(session_id, log=log_callback, limit=limit, consider_limit=consider_limit, include_videos=include_videos, metadata=meta)
        pipeline_log(session_id, 'Pipeline complete!')
        load_pipeline_status(session_id)
        pipeline_statuses[session_id]['stage'] = 'Done'
        pipeline_statuses[session_id]['api_calls'] = list(pl.api_calls_log)
        save_pipeline_status(session_id)
        
        # Log successful completion and API call logs (costing and details)
        log_event("PIPELINE", "AI Story Weaver Pipeline completed successfully", level="INFO", details={
            "api_calls": list(pl.api_calls_log),
            "total_cost_usd": sum(x["cost_usd"] for x in pl.api_calls_log)
        })

      except Exception as e:
        err = str(e)
        pipeline_log(session_id, f'ERROR at stage "{pipeline_status["stage"]}": {err}')
        load_pipeline_status(session_id)
        pipeline_statuses[session_id]['error'] = f'[{pipeline_status["stage"]}] {err}'
        save_pipeline_status(session_id)
        log_event("ERROR", f"Pipeline error at stage '{pipeline_statuses[session_id]['stage']}': {err}", level="ERROR")
      finally:
        load_pipeline_status(session_id)
        pipeline_statuses[session_id]['running'] = False
        save_pipeline_status(session_id)

    threading.Thread(target=run, args=(g.session_id,), daemon=True).start()
    return render_template('processing.html')


@match_bp.route('/status')
def status():
    session_id = g.session_id
    load_pipeline_status(session_id)
    elapsed = int(time.time() - pipeline_statuses[session_id]['started_at']) if pipeline_statuses[session_id]['started_at'] else 0

    # Auto-detect timeout in status poll
    if pipeline_statuses[session_id]['running'] and elapsed > PIPELINE_TIMEOUT:
        pipeline_statuses[session_id]['running'] = False
        pipeline_statuses[session_id]['error'] = f'Timed out after {elapsed}s at stage: {pipeline_status["stage"]}'
        save_pipeline_status(session_id)

    done = os.path.exists(f'{get_output_dir()}/story.txt')
    response = app.response_class(
        response=json.dumps({
            'running': pipeline_statuses[session_id]['running'],
            'done': done,
            'error': pipeline_statuses[session_id]['error'],
            'stage': pipeline_statuses[session_id]['stage'],
            'log': pipeline_statuses[session_id]['log'],
            'elapsed': elapsed,
            'api_calls': pipeline_statuses[session_id].get('api_calls', []),
        }),
        status=200,
        mimetype='application/json',
    )
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, public, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@match_bp.route('/album-count')
def album_count():
    all_files = [f for f in os.listdir(get_upload_dir()) if os.path.isfile(os.path.join(get_upload_dir(), f)) and not f.startswith('.')]
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
@match_bp.route('/album-files')
def album_files():
    all_files = [f for f in os.listdir(get_upload_dir()) if os.path.isfile(os.path.join(get_upload_dir(), f)) and not f.startswith('.')]
    response = app.response_class(
        response=json.dumps({'files': all_files}),
        status=200,
        mimetype='application/json'
    )
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, public, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@match_bp.route('/album-classified')
def album_classified():
    import pipeline as pl
    all_files = [f for f in os.listdir(get_upload_dir()) if os.path.isfile(os.path.join(get_upload_dir(), f)) and not f.startswith('.')]
    
    classified = {
        'pre-match': [],
        'in-match': [],
        'post-match': []
    }
    
    for f in all_files:
        cat, ts = pl.classify_by_metadata_only(get_upload_dir(), f, kickoff_ts=get_match_metadata(*g.session_id.split('_')[1:3]).get('kickoff_ts', 1782439200), fulltime_ts=get_match_metadata(*g.session_id.split('_')[1:3]).get('kickoff_ts', 1782439200) + 10800)
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


@match_bp.route('/results')
def results():
    from flask import make_response
    story, stats, highlights, cost = '', '', [], None
    for attr, path, loader in [
        ('story',      f'{get_output_dir()}/story.txt',       lambda f: f.read()),
        ('stats',      f'{get_output_dir()}/stats.json',      json.load),
        ('highlights', f'{get_output_dir()}/highlights.json', lambda f: json.load(f).get('photos', [])),
        ('cost',       f'{get_output_dir()}/cost.json',       json.load),
    ]:
        try:
            with open(path) as f:
                val = loader(f)
            if attr == 'story':
                # Treat whitespace-only content as no story
                story = val.strip()
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
    

    raw_id = g.session_id
    if raw_id.startswith('FIFA26_'):
        raw_id = raw_id[7:]
    elif raw_id.startswith('FIFA_'):
        raw_id = raw_id[5:]
        
    if raw_id in ('FIFA', 'FIFA26'):
        team1 = 'Paraguay'
        team2 = 'Australia'
    elif '_' in raw_id:
        parts = raw_id.split('_', 1)
        team1 = parts[0].replace('-', ' ').title()
        team2 = parts[1].replace('-', ' ').title()
    else:
        team1 = 'Team 1'
        team2 = 'Team 2'

    metadata = get_match_metadata(team1, team2)

    response = make_response(render_template(
        'result.html',
        team1=team1, team2=team2, metadata=metadata, 
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


@match_bp.route('/share-consent', methods=['POST'])
def share_consent():
    choice = request.form.get('consent_choice')
    if choice in ['CONFIRMED', 'PRIVATE']:
        session['hitl_consent'] = choice
        
        # Update stats.json on disk dynamically to synchronize security_check status
        try:
            stats_path = f'{get_output_dir()}/stats.json'
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
        return redirect(url_for('match.results'))
    return redirect(url_for('match.index', error="Invalid consent option"))


@match_bp.route('/logs')
def show_logs():
    logs = []
    if os.path.exists(get_log_file(session_id)):
        try:
            with open(get_log_file(session_id), 'r') as f:
                logs = json.load(f)
        except Exception:
            logs = []
            
    all_files = [f for f in os.listdir(get_upload_dir()) if os.path.isfile(os.path.join(get_upload_dir(), f)) and not f.startswith('.')]
    total_uploads = len(all_files)
    
    total_cost = 0.0
    cost_path = f'{get_output_dir()}/cost.json'
    if os.path.exists(cost_path):
        try:
            with open(cost_path) as f:
                cost_data = json.load(f)
                total_cost = cost_data.get('total_cost_usd', 0.0)
        except Exception:
            pass
            
    total_errors = sum(1 for log in logs if log.get('level') == 'ERROR')
    

    raw_id = g.session_id
    if raw_id.startswith('FIFA26_'):
        raw_id = raw_id[7:]
    elif raw_id.startswith('FIFA_'):
        raw_id = raw_id[5:]
        
    if raw_id in ('FIFA', 'FIFA26'):
        team1 = 'Paraguay'
        team2 = 'Australia'
    elif '_' in raw_id:
        parts = raw_id.split('_', 1)
        team1 = parts[0].replace('-', ' ').title()
        team2 = parts[1].replace('-', ' ').title()
    else:
        team1 = 'Team 1'
        team2 = 'Team 2'

    metadata = get_match_metadata(team1, team2)

    response = make_response(render_template(
        'logs.html',
        team1=team1, team2=team2, metadata=metadata,
        logs=reversed(logs),
        total_uploads=total_uploads,
        total_cost=total_cost,
        total_errors=total_errors
    ))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, public, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@match_bp.route('/admin')
def admin():
    story_ready = os.path.exists(f'{get_output_dir()}/story.txt') and os.path.getsize(f'{get_output_dir()}/story.txt') > 0
    return render_template('admin.html', story_ready=story_ready)

app.register_blueprint(match_bp)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
