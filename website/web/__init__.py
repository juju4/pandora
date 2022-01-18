#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import functools
import pkg_resources
import traceback

from collections import defaultdict
from datetime import datetime

import flask_session  # type: ignore
import flask_moment  # type: ignore
import flask_login  # type: ignore
import flask_wtf  # type: ignore

from flask import (Flask, request, session, abort, render_template,
                   redirect, send_file, url_for)
from flask_restx import Api  # type: ignore
from flask_bootstrap import Bootstrap5  # type: ignore
from werkzeug.security import check_password_hash

from pandora.default import get_config
from pandora.helpers import workers, get_homedir, Status
from pandora.pandora import Pandora
from pandora.role import Action
from pandora.user import User

from .generic_api import api as generic_api
from .generic_api import ApiRole, ApiObservable, ApiSubmit, ApiTaskAction
from .helpers import (get_secret_key, update_user_role, admin_required,
                      src_request_ip, load_user_from_request, build_users_table,
                      )
from .proxied import ReverseProxied

pandora: Pandora = Pandora()

app: Flask = Flask(__name__)

app.wsgi_app = ReverseProxied(app.wsgi_app)  # type: ignore

app.config['SECRET_KEY'] = get_secret_key()
app.config['UPLOAD_FOLDER'] = get_homedir() / 'upload'
app.config['MAX_CONTENT_PATH'] = get_config('generic', 'max_file_size')
app.config['CACHE_TYPE'] = 'simple'
app.config['SESSION_TYPE'] = 'redis'
app.config['SESSION_REDIS'] = pandora.redis_bytes
app.config['SESSION_KEY_PREFIX'] = 'session:'

Bootstrap5(app)
app.config['BOOTSTRAP_SERVE_LOCAL'] = True
app.config['SESSION_COOKIE_NAME'] = 'pandora'
app.config['SESSION_COOKIE_SAMESITE'] = 'Strict'
app.debug = get_config('generic', 'debug_web')
API_LOG_TRACEBACK = get_config('generic', 'debug_web')

flask_session.Session(app=app)
login_manager = flask_login.LoginManager(app=app)
flask_moment.Moment(app=app)
flask_wtf.CSRFProtect(app=app)


api = Api(title='Pandora API',
          description='API to query Pandora.',
          doc='/doc/',
          version=pkg_resources.get_distribution('pandora').version)

api.add_namespace(generic_api)


def default_icon():
    return 'question'


status_icons = defaultdict(default_icon, {
    Status.ERROR: 'exclamation-octagon',
    Status.ALERT: 'x-circle',
    Status.WARN: 'exclamation-triangle',
    Status.OKAY: 'check-circle'
})


@app.context_processor
def inject_enums():
    '''All the templates have the Action enum'''
    return dict(action=Action, status=Status, status_icons=status_icons)


@login_manager.user_loader
def load_user(user_id):
    return pandora.get_user(user_id)


@login_manager.request_loader
def _load_user_from_request(request):
    matching_username = load_user_from_request(request)
    if matching_username:
        flask_login.current_user.name = matching_username
        flask_login.current_user.store
        flask_login.login_user(flask_login.current_user)
    return None


@app.before_request
def update_user():
    if flask_login.current_user.is_authenticated:
        flask_login.current_user.last_ip = src_request_ip(request)
        flask_login.current_user.last_seen = datetime.now()
        flask_login.current_user.store
        return
    # Note: session.sid comes from flask_session
    user = User(session_id=session.sid, last_ip=src_request_ip(request))  # type: ignore
    user.store
    flask_login.login_user(user)


@app.template_filter()
def to_datetime(iso):
    return datetime.fromisoformat(str(iso)) if iso else datetime.now()


def html_answer(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            res = func(*args, **kwargs)
        except (AssertionError, BaseException):
            if API_LOG_TRACEBACK:
                traceback.print_exc()
            return abort(404)
        else:
            return res

    return wrapper


@app.errorhandler(404)
def api_error_404(_):
    return render_template('error.html', status=404), 404


@app.errorhandler(403)
def api_error_403(_):
    return render_template('error.html', status=403), 403


@app.route('/', strict_slashes=False)
def api_root():
    return redirect(url_for('api_submit_page'), 301)


@app.route('/submit', methods=['GET'], strict_slashes=False)
@html_answer
def api_submit_page():
    assert flask_login.current_user.role.can(Action.submit_file), 'forbidden'

    return render_template(
        'submit.html', error=request.args.get('error', ''),
        max_file_size=app.config['MAX_CONTENT_PATH'] // 10 ** 6, workers=workers(),
        api=api,
        api_resource=ApiSubmit
    )


@app.route('/analysis/<task_id>', methods=['GET'], strict_slashes=False)
@app.route('/analysis/<task_id>/seed-<seed>', methods=['GET'], strict_slashes=False)
@html_answer
def api_analysis(task_id, seed=None):
    task = pandora.get_task(task_id=task_id)
    assert task is not None, 'analysis not found'
    task.seed = seed
    update_user_role(pandora, task)

    assert flask_login.current_user.role.can(Action.read_analysis), 'forbidden'

    # task.reports = pandora.get_task_reports(task=task)
    # task.linked_tasks = pandora.get_related_tasks(linked_with=task, user=flask_login.current_user)
    # task.extracted_tasks = pandora.get_related_tasks(extracted_from=task, user=flask_login.current_user)
    task.reports = []
    task.linked_tasks = []
    task.extracted_tasks = []

    # return render_template('analysis.html', task=task, zip_pass=setting.ZIP_PASS)
    return render_template('analysis.html', task=task, api=api, api_resource=ApiTaskAction)


@app.route('/task-download/<task_id>/seed-<seed>/<source>', methods=['GET'], strict_slashes=False)
@app.route('/task-download/<task_id>/seed-<seed>/<source>/<int:idx>', methods=['GET'], strict_slashes=False)
@app.route('/task-download/<task_id>/<source>', methods=['GET'], strict_slashes=False)
@app.route('/task-download/<task_id>/<source>/<int:idx>', methods=['GET'], strict_slashes=False)
@html_answer
def api_task_download(task_id, source, seed=None, idx=None):
    assert source in ('img', 'pdf', 'txt', 'zip'), f"unexpected source '{source}'"
    task = pandora.get_task(task_id=task_id)
    assert task is not None, 'analysis not found'
    task.seed = seed
    update_user_role(pandora, task)

    if source == 'img' and flask_login.current_user.role.can(Action.download_images):
        if not task.file.previews:
            raise AssertionError('content not available')
        if idx is not None:
            return send_file(task.file.previews[idx])
        else:
            return send_file(task.file.previews_archive)

    if source == 'pdf' and flask_login.current_user.role.can(Action.download_pdf):
        # NOTE: need to also return a PDF of office doc.
        assert task.file.is_pdf, 'PDF not available'
        return send_file(task.file.path)

    if source == 'txt' and flask_login.current_user.role.can(Action.download_text):
        assert task.file.text, 'text content not available'
        return send_file(task.file.data, mimetype=task.file.mime_type)

    if source == 'zip' and flask_login.current_user.role.can(Action.download_zip):
        if task.file.zip_name is None:
            raise AssertionError('content not available')
        return send_file(task.file.zip_path)

    raise AssertionError('forbidden')


@app.route('/admin/<int:error>', methods=['GET'], strict_slashes=False)
@app.route('/admin', methods=['GET'], strict_slashes=False)
@html_answer
def api_admin_page(error=None):
    error_messages = {1: 'Invalid Credentials', 2: 'Unable to initialize credentials, see logs.'}
    return render_template('admin.html', error=error_messages.get(error))


@app.route('/admin', methods=['POST'], strict_slashes=False)
@html_answer
def api_admin_submit():
    if flask_login.current_user.is_admin:
        return redirect(url_for('api_admin_page'))

    try:
        assert 'username' in request.form, "missing mandatory key 'username'"
        assert 'password' in request.form, "missing mandatory key 'password'"
        try:
            users_table = build_users_table()
        except Exception as e:
            # FIXME add logging
            print(e)
            return redirect(url_for('api_admin_page', error=2), 302)
        username = request.form['username']
        if username in users_table and check_password_hash(users_table[username]['password'], request.form['password']):
            flask_login.current_user.name = username
            flask_login.current_user.role = pandora.get_role('admin')
            flask_login.current_user.store
            flask_login.login_user(flask_login.current_user)
            return redirect(url_for('api_admin_page'))
        else:
            return redirect(url_for('api_admin_page', error=1), 302)

    except AssertionError:
        return redirect(url_for('api_admin_page', error=1), 302)


@app.route('/admin/logout', methods=['GET'], strict_slashes=False)
@html_answer
def api_logout():
    assert flask_login.current_user.is_admin, 'forbidden'
    flask_login.logout_user()
    return redirect(url_for('api_root'), 302)


@app.route('/tasks', methods=['GET'], strict_slashes=False)
@app.route('/tasks/search-<search>', methods=['GET'], strict_slashes=False)
@html_answer
def api_tasks(search=None):

    assert flask_login.current_user.role.can([Action.list_own_tasks, Action.list_all_tasks], 'or'), 'forbidden'
    search = search.strip() if search is not None else None
    tasks = pandora.get_tasks(user=flask_login.current_user)
    return render_template('tasks.html', tasks=tasks, search=search or '', status=Status, api=api, api_resource=ApiTaskAction)


@app.route('/observables', methods=['GET'], strict_slashes=False)
@admin_required
@html_answer
def api_observables():

    assert flask_login.current_user.role.can(Action.list_observables), 'forbidden'
    observables = pandora.get_observables()
    return render_template('observables.html', observables=observables, api=api, api_resource=ApiObservable)


@app.route('/users', methods=['GET'], strict_slashes=False)
@admin_required
@html_answer
def api_users():

    assert flask_login.current_user.role.can(Action.list_users), 'forbidden'
    users = pandora.get_users()
    return render_template('users.html', users=users)


@app.route('/roles', methods=['GET'], strict_slashes=False)
@admin_required
@html_answer
def api_roles():

    assert flask_login.current_user.role.can(Action.list_roles), 'forbidden'
    roles = pandora.get_roles()
    return render_template('roles.html', roles=roles, api=api, api_resource=ApiRole)


@app.route('/stats', methods=['GET'], strict_slashes=False)
@admin_required
@html_answer
def api_stats():
    assert flask_login.current_user.role.can(Action.list_stats), 'forbidden'
    return render_template('stats.html')


# NOTE: this one must be at the end, it adds a route to / that will break the default one.
api.init_app(app)