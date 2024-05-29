#  IRIS Source Code
#  Copyright (C) 2021 - Airbus CyberSecurity (SAS)
#  ir@cyberactionlab.net
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 3 of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
import base64

import io

import pyotp
import qrcode
from urllib.parse import urlsplit

# IMPORTS ------------------------------------------------

from flask import Blueprint, flash
from flask import redirect
from flask import render_template
from flask import request
from flask import session
from flask import url_for
from flask_login import current_user, login_required
from flask_login import login_user

from app import app
from app import bc
from app import db

from app.forms import LoginForm, MFASetupForm
from app.iris_engine.access_control.ldap_handler import ldap_authenticate
from app.iris_engine.access_control.utils import ac_get_effective_permissions_of_user
from app.iris_engine.utils.tracker import track_activity
from app.models.cases import Cases
from app.util import is_authentication_ldap
from app.datamgmt.manage.manage_users_db import get_active_user_by_login


login_blueprint = Blueprint(
    'login',
    __name__,
    template_folder='templates'
)

log = app.logger


# filter User out of database through username
def _retrieve_user_by_username(username):
    user = get_active_user_by_login(username)
    if not user:
        track_activity("someone tried to log in with user '{}', which does not exist".format(username),
                       ctx_less=True, display_in_ui=False)
    return user


def _render_template_login(form, msg):
    organisation_name = app.config.get('ORGANISATION_NAME')
    login_banner = app.config.get('LOGIN_BANNER_TEXT')
    ptfm_contact = app.config.get('LOGIN_PTFM_CONTACT')

    return render_template('login.html', form=form, msg=msg, organisation_name=organisation_name,
                           login_banner=login_banner, ptfm_contact=ptfm_contact)


def _authenticate_ldap(form, username, password, local_fallback=True):
    try:
        if ldap_authenticate(username, password) is False:
            if local_fallback is True:
                track_activity("wrong login password for user '{}' using LDAP auth - falling back to local based on settings".format(username),
                                 ctx_less=True, display_in_ui=False)
                
                return _authenticate_password(form, username, password)
            
            track_activity("wrong login password for user '{}' using LDAP auth".format(username),
                           ctx_less=True, display_in_ui=False)
            return _render_template_login(form, 'Wrong credentials. Please try again.')

        user = _retrieve_user_by_username(username)
        if not user:
            return _render_template_login(form, 'Wrong credentials. Please try again.')

        return wrap_login_user(user)
    except Exception as e:
        log.error(e.__str__())
        return _render_template_login(form, 'LDAP authentication unavailable. Check server logs')


def _authenticate_password(form, username, password):
    user = _retrieve_user_by_username(username)
    if not user or user.is_service_account:
        return _render_template_login(form, 'Wrong credentials. Please try again.')

    if bc.check_password_hash(user.password, password):
        return wrap_login_user(user)

    track_activity("wrong login password for user '{}' using local auth".format(username), ctx_less=True,
                   display_in_ui=False)
    return _render_template_login(form, 'Wrong credentials. Please try again.')


# CONTENT ------------------------------------------------
# Authenticate user
if app.config.get("AUTHENTICATION_TYPE") in ["local", "ldap"]:
    @login_blueprint.route('/login', methods=['GET', 'POST'])
    def login():
        session.permanent = True

        if current_user.is_authenticated:
            return redirect(url_for('index.index'))

        form = LoginForm(request.form)

        # check if both http method is POST and form is valid on submit
        if not form.is_submitted() and not form.validate():
            return _render_template_login(form, None)

        # assign form data to variables
        username = request.form.get('username', '', type=str)
        password = request.form.get('password', '', type=str)

        if is_authentication_ldap() is True:
            return _authenticate_ldap(form, username, password, app.config.get('AUTHENTICATION_LOCAL_FALLBACK'))

        return _authenticate_password(form, username, password)


def wrap_login_user(user):

    session['username'] = user.user

    if "mfa_verified" not in session or session["mfa_verified"] is False:
        return redirect(url_for('mfa_verify'))

    login_user(user)

    track_activity("user '{}' successfully logged-in".format(user.user), ctx_less=True, display_in_ui=False)
    caseid = user.ctx_case
    session['permissions'] = ac_get_effective_permissions_of_user(user)

    if caseid is None:
        case = Cases.query.order_by(Cases.case_id).first()
        user.ctx_case = case.case_id
        user.ctx_human_case = case.name
        db.session.commit()

    session['current_case'] = {
        'case_name': user.ctx_human_case,
        'case_info': "",
        'case_id': user.ctx_case
    }

    track_activity("user '{}' successfully logged-in".format(user), ctx_less=True, display_in_ui=False)

    next_url = None
    if request.args.get('next'):
        next_url = request.args.get('next') if 'cid=' in request.args.get('next') else request.args.get('next') + '?cid=' + str(user.ctx_case)

    if not next_url or urlsplit(next_url).netloc != '':
        next_url = url_for('index.index', cid=user.ctx_case)

    return redirect(next_url)


@app.route('/auth/mfa-setup', methods=['GET', 'POST'])
@login_required
def mfa_setup():
    user = current_user
    form = MFASetupForm()
    if form.submit() and form.validate():
        if not user.mfa_secrets:
            user.mfa_secrets = pyotp.random_base32()
            db.session.commit()

        token = form.token.data
        totp = pyotp.TOTP(user.mfa_secrets)
        if totp.verify(token):
            user.mfa_enabled = True
            db.session.commit()
            track_activity(f'MFA setup successful for user {current_user.user}', ctx_less=True, display_in_ui=False)
            return wrap_login_user(user)
        else:
            flash('Invalid token. Please try again.', 'danger')

    otp_uri = pyotp.TOTP(user.mfa_secrets).provisioning_uri(user.email, issuer_name="IRIS")
    img = qrcode.make(otp_uri)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    img_str = base64.b64encode(buf.getvalue()).decode()

    return render_template('mfa_setup.html', form=form, img_data=img_str)


@app.route('/auth/mfa-verify', methods=['GET', 'POST'])
def mfa_verify():
    if 'username' not in session:
        return redirect(url_for('login'))

    session['mfa_verified'] = False

    user = _retrieve_user_by_username(username=session['username'])
    if not user.mfa_secrets:
        track_activity(f'MFA required but not enabled for user {current_user.user}', ctx_less=True, display_in_ui=False)
        login_user(user)
        return redirect(url_for('mfa_setup'))

    form = MFASetupForm()
    if form.submit() and form.validate():
        token = form.token.data
        if not token:
            flash('Token is required.', 'danger')
            return render_template('mfa_verify.html', form=form)

        totp = pyotp.TOTP(user.mfa_secrets)
        if totp.verify(token):
            session.pop('username', None)
            session['mfa_verified'] = True
            track_activity(f'MFA verified for user {current_user.user}', ctx_less=True,
                           display_in_ui=False)

            return wrap_login_user(user)
        else:
            track_activity(f'MFA invalid for user {current_user.user}. Login aborted', ctx_less=True,
                           display_in_ui=False)

            flash('Invalid token. Please try again.', 'danger')

    return render_template('mfa_verify.html', form=form)