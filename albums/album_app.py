# albums/routes.py （Blueprint版）

from flask import Blueprint, render_template, request, redirect, url_for, session, send_from_directory, abort
import os
import uuid
import json
import bcrypt
import secrets
from werkzeug.utils import secure_filename

album_bp = Blueprint('album', __name__, template_folder='templates')

ALBUM_ROOT = '/mnt/maildata/mfu/albums'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'heic'}

ADMIN_PASSWORD = 'adminpass'  # 簡易的な管理者用パスワード（後で強化可）

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def load_meta(album_id):
    meta_path = os.path.join(ALBUM_ROOT, album_id, 'meta.json')
    if os.path.exists(meta_path):
        with open(meta_path, 'r') as f:
            return json.load(f)
    return None

def save_meta(album_id, data):
    meta_path = os.path.join(ALBUM_ROOT, album_id, 'meta.json')
    with open(meta_path, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

@album_bp.route('/access/<album_id>', methods=['GET', 'POST'])
def album_access(album_id):
    meta = load_meta(album_id)
    if not meta:
        return 'アルバムが存在しません', 404
    if request.method == 'POST':
        password = request.form['password'].encode('utf-8')
        if bcrypt.checkpw(password, meta['password_hash'].encode('utf-8')):
            session[f'auth_{album_id}'] = True
            return redirect(url_for('album.album_home', album_id=album_id))
        else:
            return 'パスワードが違います', 403
    return render_template('access.html', album_id=album_id)

@album_bp.route('/<album_id>/')
def album_home(album_id):
    if not session.get(f'auth_{album_id}'):
        return redirect(url_for('album.album_access', album_id=album_id))
    meta = load_meta(album_id)
    return render_template('album_home.html', album_id=album_id, meta=meta)

@album_bp.route('/<album_id>/create_child', methods=['POST'])
def create_child(album_id):
    if not session.get(f'auth_{album_id}'):
        return redirect(url_for('album.album_access', album_id=album_id))
    folder_name = request.form['child_name']
    child_uuid = str(uuid.uuid4())
    meta = load_meta(album_id)
    for child in meta['children']:
        if child['name'] == folder_name:
            return '既に存在します', 400
    os.makedirs(os.path.join(ALBUM_ROOT, album_id, child_uuid), exist_ok=True)
    meta['children'].append({"name": folder_name, "folder": child_uuid})
    save_meta(album_id, meta)
    return redirect(url_for('album.album_home', album_id=album_id))

@album_bp.route('/<album_id>/upload/<child_id>', methods=['GET', 'POST'])
def upload(album_id, child_id):
    if not session.get(f'auth_{album_id}'):
        return redirect(url_for('album.album_access', album_id=album_id))
    child_path = os.path.join(ALBUM_ROOT, album_id, child_id)
    if not os.path.exists(child_path):
        return '子アルバムが存在しません', 404

    if request.method == 'POST':
        files = request.files.getlist('file')
        count = len(os.listdir(child_path))
        for file in files:
            if file and allowed_file(file.filename):
                ext = file.filename.rsplit('.', 1)[1].lower()
                filename = f"{child_id}_{count+1:04d}.{ext}"
                file.save(os.path.join(child_path, filename))
                count += 1
        return redirect(url_for('album.view_child', album_id=album_id, child_id=child_id))

    return render_template('upload.html', album_id=album_id, child_id=child_id)

@album_bp.route('/<album_id>/view/<child_id>')
def view_child(album_id, child_id):
    if not session.get(f'auth_{album_id}'):
        return redirect(url_for('album.album_access', album_id=album_id))
    child_path = os.path.join(ALBUM_ROOT, album_id, child_id)
    files = sorted(f for f in os.listdir(child_path) if allowed_file(f))
    return render_template('view.html', album_id=album_id, child_id=child_id, files=files)

@album_bp.route('/<album_id>/download/<child_id>/<filename>')
def download(album_id, child_id, filename):
    if not session.get(f'auth_{album_id}'):
        return redirect(url_for('album.album_access', album_id=album_id))
    return send_from_directory(os.path.join(ALBUM_ROOT, album_id, child_id), filename, as_attachment=True)

@album_bp.route('/admin/create_album', methods=['GET', 'POST'])
def admin_create_album():
    if session.get('user') != 'admin':
        return '管理者のみ作成可能です', 403

    if request.method == 'POST':
        album_name = request.form['album_name']
        album_id = str(uuid.uuid4())
        password_raw = secrets.token_hex(8)  # 16文字の16進パスワード
        password_hash = bcrypt.hashpw(password_raw.encode(), bcrypt.gensalt()).decode('utf-8')
        os.makedirs(os.path.join(ALBUM_ROOT, album_id), exist_ok=True)
        meta = {
            "album_name": album_name,
            "password_hash": password_hash,
            "children": [],
            "owner": "admin"  # オーナー情報も明示的に入れておくとよい
        }
        save_meta(album_id, meta)
        return render_template('admin_created.html', album_id=album_id, password=password_raw)

    # 📌 一覧の取得とソート処理
    album_list = []
    for aid in os.listdir(ALBUM_ROOT):
        meta = load_meta(aid)
        if meta:
            album_list.append({
                'id': aid,
                'name': meta.get('album_name', '（無名）'),
                'owner': 'admin'
            })

    # ✅ アルバム名順にソート（あいうえお順）
    album_list.sort(key=lambda x: x['name'])

    return render_template('admin_create_album.html', album_list=album_list)
