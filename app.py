import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import urlencode, urlparse

import requests
from cryptography.fernet import Fernet
from flask import Flask, abort, g, redirect, render_template, request, url_for
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from werkzeug.security import check_password_hash

from models import Base, BrowserSession, LoginGuard, OAuthState, Connection, Product, AdTemplate, Listing, Freight


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(APP_ENV=os.getenv('APP_ENV', 'production'),
                      PUBLIC_BASE_URL=os.getenv('PUBLIC_BASE_URL', ''),
                      DATABASE_URL=os.getenv('DATABASE_URL', ''),
                      ADMIN_USERNAME=os.getenv('ADMIN_USERNAME', 'admin'),
                      ADMIN_PASSWORD_HASH=os.getenv('ADMIN_PASSWORD_HASH', ''),
                      TOKEN_ENCRYPTION_KEY=os.getenv('TOKEN_ENCRYPTION_KEY', ''),
                      MELI_CLIENT_ID=os.getenv('MELI_CLIENT_ID', ''),
                      MELI_CLIENT_SECRET=os.getenv('MELI_CLIENT_SECRET', ''),
                      MELI_SELLER_ID=os.getenv('MELI_SELLER_ID', ''),
                      MAX_CONTENT_LENGTH=64 * 1024)
    if config:
        app.config.update(config)
    production = app.config['APP_ENV'] == 'production'
    base = app.config['PUBLIC_BASE_URL'].rstrip('/')
    parsed = urlparse(base)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or
            (production and parsed.scheme != 'https')):
        raise RuntimeError('Configure PUBLIC_BASE_URL com a origem pública exata (HTTPS em produção).')
    if not app.config['ADMIN_USERNAME'] or not app.config['ADMIN_PASSWORD_HASH'].startswith('scrypt:'):
        raise RuntimeError('Configure ADMIN_PASSWORD_HASH com hash scrypt; não use senha em texto.')
    cipher = Fernet(app.config['TOKEN_ENCRYPTION_KEY'].encode())
    database_url = app.config['DATABASE_URL']
    if database_url.startswith(('postgres://', 'postgresql://')):
        database_url = 'postgresql+psycopg://' + database_url.split('://', 1)[1]
    if production and not database_url.startswith('postgresql+psycopg://'):
        raise RuntimeError('Produção exige DATABASE_URL PostgreSQL persistente.')
    engine = create_engine(database_url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as db:
        if not db.get(LoginGuard, 1):
            db.add(LoginGuard(id=1, failures=0, locked_until=0))
    app.extensions.update(db_factory=factory, cipher=cipher)

    @app.before_request
    def prepare():
        if request.host != parsed.netloc:
            abort(400)
        g.db = factory()
        g.cookie = None
        if request.path == '/health':
            return
        now = int(time.time())
        raw = request.cookies.get('evolux_session', '')
        browser = g.db.get(BrowserSession, digest(raw)) if raw else None
        if not browser or browser.expires <= now:
            if browser:
                g.db.delete(browser)
            raw = secrets.token_urlsafe(32)
            browser = BrowserSession(id=digest(raw), csrf=secrets.token_urlsafe(32),
                                     authenticated=0, expires=now + 3600)
            g.db.add(browser)
            g.db.execute(delete(BrowserSession).where(BrowserSession.expires <= now))
            g.db.execute(delete(OAuthState).where(OAuthState.expires <= now))
            g.db.commit()
            g.cookie = raw
        g.browser = browser
        if request.method == 'POST':
            if not hmac.compare_digest(digest(request.form.get('csrf', '')), digest(browser.csrf)):
                abort(400, 'Formulário expirado. Recarregue a página.')
            origin = request.headers.get('Origin')
            if origin and origin != base:
                origin_parts = urlparse(origin)
                app.logger.warning('Form origin mismatch: opaque=%s, scheme=%r, host=%r',
                                   origin == 'null', origin_parts.scheme, origin_parts.hostname)
                abort(403)

    @app.after_request
    def secure_response(response):
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                 'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
                                 'Content-Security-Policy': "default-src 'self'; style-src 'self'; script-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if production:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        if getattr(g, 'cookie', None):
            response.set_cookie('evolux_session', g.cookie, max_age=3600,
                                secure=production, httponly=True, samesite='Lax', path='/')
        return response

    @app.teardown_request
    def close(_):
        if hasattr(g, 'db'):
            g.db.close()

    def protected(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not g.browser.authenticated:
                return redirect(url_for('login'))
            return fn(*args, **kwargs)
        return wrapper

    def page_error(message, status=400):
        g.db.rollback()
        return render_template('error.html', message=message), status

    @app.errorhandler(IntegrityError)
    def duplicate(_):
        return page_error('Registro duplicado ou vínculo inválido. Confira SKU, MLB e referência.', 409)

    @app.errorhandler(ValueError)
    def invalid(_):
        return page_error('Dados inválidos. Confira os campos, limites e números informados.')

    @app.errorhandler(requests.RequestException)
    def remote_error(_):
        return page_error('O Mercado Livre não respondeu ou recusou a consulta. Tente novamente; se necessário, reconecte a conta.', 502)

    @app.get('/health')
    def health():
        g.db.execute(select(LoginGuard.id).limit(1))
        return {'status': 'ok'}

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        error = None
        status = 200
        if request.method == 'POST':
            guard = g.db.execute(select(LoginGuard).where(LoginGuard.id == 1).with_for_update()).scalar_one()
            now = int(time.time())
            if guard.locked_until > now:
                error, status = 'Acesso temporariamente bloqueado. Aguarde 15 minutos.', 429
            else:
                if guard.locked_until:
                    guard.failures, guard.locked_until = 0, 0
                password = request.form.get('password', '')
                valid = check_password_hash(app.config['ADMIN_PASSWORD_HASH'], password[:1024])
                if valid and len(password) <= 1024 and hmac.compare_digest(digest(request.form.get('username', '')), digest(app.config['ADMIN_USERNAME'])):
                    guard.failures = 0
                    g.db.execute(delete(OAuthState).where(OAuthState.session_id == g.browser.id))
                    g.db.delete(g.browser)
                    raw = secrets.token_urlsafe(32)
                    g.browser = BrowserSession(id=digest(raw), csrf=secrets.token_urlsafe(32), authenticated=1, expires=now + 3600)
                    g.db.add(g.browser)
                    g.db.commit()
                    g.cookie = raw
                    return redirect(url_for('dashboard'))
                guard.failures += 1
                if guard.failures >= 5:
                    guard.locked_until = now + 900
                error, status = 'Usuário ou senha inválidos.', 401
            g.db.commit()
        return render_template('login.html', error=error), status

    @app.post('/logout')
    @protected
    def logout():
        g.db.execute(delete(OAuthState).where(OAuthState.session_id == g.browser.id))
        g.db.delete(g.browser)
        g.db.commit()
        response = redirect(url_for('login'))
        response.delete_cookie('evolux_session', path='/')
        return response

    @app.get('/')
    @protected
    def dashboard():
        listings = g.db.scalars(select(Listing).order_by(Listing.mlb)).all()
        freights = g.db.scalars(select(Freight).order_by(Freight.date.desc(), Freight.id.desc())).all()
        return render_template('dashboard.html', listings=listings, freights=freights,
                               products=g.db.scalars(select(Product).order_by(Product.sku)).all(),
                               templates=g.db.scalars(select(AdTemplate).order_by(AdTemplate.name)).all(),
                               connection=g.db.get(Connection, 1),
                               charged=sum((f.charged for f in freights), Decimal(0)),
                               overcharge=sum((max(f.difference, Decimal(0)) for f in freights), Decimal(0)),
                               divergences=sum(f.difference != 0 for f in freights),
                               message=request.args.get('message'), oauth_ready=oauth_ready())

    def text_field(name, maximum):
        value = request.form.get(name, '').strip()
        if not value or len(value) > maximum:
            raise ValueError(name)
        return value

    def number(name, maximum, places=2, zero=False):
        try:
            value = Decimal(text_field(name, 30).replace(',', '.'))
            if not value.is_finite() or value < 0 or (value == 0 and not zero) or value > maximum:
                raise ValueError(name)
            if value != value.quantize(Decimal(10) ** -places):
                raise ValueError(name)
            return value
        except InvalidOperation:
            raise ValueError(name)

    @app.post('/products')
    @protected
    def product_save():
        sku = text_field('sku', 80)
        product = g.db.scalar(select(Product).where(Product.sku == sku)) or Product(sku=sku)
        product.name = text_field('name', 180)
        for name in ('length', 'width', 'height'):
            setattr(product, name, number(name, 10000))
        product.weight = number('weight', 10000, 3)
        g.db.add(product)
        g.db.commit()
        return redirect(url_for('dashboard', message='Produto salvo.'))

    @app.post('/templates')
    @protected
    def template_save():
        name = text_field('name', 100)
        template = g.db.scalar(select(AdTemplate).where(AdTemplate.name == name)) or AdTemplate(name=name)
        template.title, template.body = text_field('title', 180), text_field('body', 5000)
        g.db.add(template)
        g.db.commit()
        return redirect(url_for('dashboard', message='Modelo salvo para uso interno.'))

    @app.post('/listings')
    @protected
    def listing_save():
        mlb = text_field('mlb', 32).upper()
        if not re.fullmatch(r'MLB\d{6,20}', mlb):
            raise ValueError('mlb')
        product = g.db.get(Product, int(request.form.get('product_id', '0')))
        if not product:
            raise ValueError('sku')
        listing = g.db.scalar(select(Listing).where(Listing.mlb == mlb)) or Listing(mlb=mlb)
        # Avoid rewriting product association once financial records exist.
        if listing.id and listing.product_id != product.id and g.db.scalar(select(Freight.id).where(Freight.listing_id == listing.id).limit(1)):
            return page_error('Este MLB já tem fretes. Mantenha seu SKU para preservar o histórico.', 409)
        template_id = request.form.get('template_id', '')
        template = g.db.get(AdTemplate, int(template_id)) if template_id else None
        if template_id and not template:
            raise ValueError('template')
        listing.product_id, listing.template_id = product.id, template.id if template else None
        listing.title = text_field('title', 180)
        listing.listing_type = text_field('listing_type', 40)
        if not listing.id:
            listing.reported_dimensions = 'Não consultadas'
        g.db.add(listing)
        g.db.commit()
        return redirect(url_for('dashboard', message='Anúncio salvo.'))

    @app.post('/freights')
    @protected
    def freight_save():
        listing = g.db.get(Listing, int(request.form.get('listing_id', '0')))
        if not listing:
            raise ValueError('listing')
        freight_date = date.fromisoformat(text_field('date', 10)).isoformat()
        g.db.add(Freight(listing_id=listing.id, reference=text_field('reference', 100), date=freight_date,
                         expected=number('expected', 999999999, zero=True), charged=number('charged', 999999999, zero=True),
                         source=text_field('source', 180)))
        g.db.commit()
        return redirect(url_for('dashboard', message='Frete registrado.'))

    def oauth_ready():
        return all(app.config[k] for k in ('MELI_CLIENT_ID', 'MELI_CLIENT_SECRET', 'MELI_SELLER_ID'))

    def token_request(data):
        response = requests.post('https://api.mercadolibre.com/oauth/token', data={
            'client_id': app.config['MELI_CLIENT_ID'], 'client_secret': app.config['MELI_CLIENT_SECRET'], **data}, timeout=20)
        response.raise_for_status()
        result = response.json()
        if not result.get('access_token') or not result.get('refresh_token') or not 0 < int(result.get('expires_in', 0)) <= 86400:
            raise ValueError('invalid token response')
        return result

    def store_tokens(connection, tokens):
        connection.access = cipher.encrypt(tokens['access_token'].encode()).decode()
        connection.refresh = cipher.encrypt(tokens['refresh_token'].encode()).decode()
        connection.expires = int(time.time()) + int(tokens['expires_in'])

    @app.post('/oauth/start')
    @protected
    def oauth_start():
        if not oauth_ready():
            return page_error('Configure os dados da aplicação Mercado Livre e o ID do vendedor no Render.', 503)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        g.db.execute(delete(OAuthState).where(OAuthState.session_id == g.browser.id))
        g.db.add(OAuthState(id=digest(state), session_id=g.browser.id,
                            verifier=cipher.encrypt(verifier.encode()).decode(), expires=int(time.time()) + 600))
        g.db.commit()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        return redirect('https://auth.mercadolivre.com.br/authorization?' + urlencode({
            'response_type': 'code', 'client_id': app.config['MELI_CLIENT_ID'],
            'redirect_uri': base + '/oauth/callback', 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256'}))

    @app.get('/oauth/callback')
    @protected
    def oauth_callback():
        state = g.db.execute(select(OAuthState).where(OAuthState.id == digest(request.args.get('state', ''))).with_for_update()).scalar_one_or_none()
        if not state or state.session_id != g.browser.id or state.expires <= int(time.time()):
            return page_error('Autorização inválida ou expirada. Inicie uma nova conexão.', 400)
        verifier = cipher.decrypt(state.verifier.encode()).decode()
        g.db.delete(state)
        g.db.commit()  # consume once, including cancellation or failed exchange
        if request.args.get('error') or not request.args.get('code') or not oauth_ready():
            return page_error('Autorização cancelada ou não configurada.', 400)
        tokens = token_request({'grant_type': 'authorization_code', 'code': request.args['code'],
                                'redirect_uri': base + '/oauth/callback', 'code_verifier': verifier})
        response = requests.get('https://api.mercadolibre.com/users/me', headers={'Authorization': 'Bearer ' + tokens['access_token']}, timeout=20)
        response.raise_for_status()
        seller = str(response.json().get('id', ''))
        if seller != app.config['MELI_SELLER_ID']:
            return page_error('A conta autorizada não é o vendedor configurado. Conecte a conta correta.', 403)
        connection = g.db.get(Connection, 1) or Connection(id=1)
        connection.seller_id = seller
        store_tokens(connection, tokens)
        g.db.add(connection)
        g.db.commit()
        return redirect(url_for('dashboard', message='Conta Mercado Livre conectada.'))

    @app.post('/oauth/disconnect')
    @protected
    def disconnect():
        g.db.execute(delete(Connection))
        g.db.execute(delete(OAuthState))
        g.db.commit()
        return redirect(url_for('dashboard', message='Credenciais removidas deste sistema. Revogue também o acesso no Mercado Livre, se necessário.'))

    @app.post('/listings/<int:listing_id>/sync')
    @protected
    def sync(listing_id):
        listing = g.db.get(Listing, listing_id)
        if not listing:
            abort(404)
        # PostgreSQL row lock serializes rotating refresh tokens across requests.
        connection = g.db.execute(select(Connection).where(Connection.id == 1).with_for_update()).scalar_one_or_none()
        if not connection or not oauth_ready() or connection.seller_id != app.config['MELI_SELLER_ID']:
            return page_error('Conecte o vendedor configurado antes de consultar.', 409)
        if connection.expires <= int(time.time()) + 60:
            tokens = token_request({'grant_type': 'refresh_token', 'refresh_token': cipher.decrypt(connection.refresh.encode()).decode()})
            store_tokens(connection, tokens)
            g.db.commit()  # persist rotated refresh even if the subsequent item request fails
        access = cipher.decrypt(connection.access.encode()).decode()
        response = requests.get('https://api.mercadolibre.com/items/' + listing.mlb,
                                headers={'Authorization': 'Bearer ' + access}, timeout=20)
        response.raise_for_status()
        item = response.json()
        if item.get('id') != listing.mlb or str(item.get('seller_id', '')) != connection.seller_id:
            return page_error('Este anúncio não pertence ao vendedor conectado.', 403)
        listing.title = str(item.get('title', listing.title))[:180]
        listing.listing_type = str(item.get('listing_type_id', listing.listing_type))[:40]
        dimensions = [f"{a['id']}: {a.get('value_name') or 'não informado'}" for a in item.get('attributes', [])
                      if a.get('id') in ('SELLER_PACKAGE_LENGTH', 'SELLER_PACKAGE_WIDTH', 'SELLER_PACKAGE_HEIGHT', 'SELLER_PACKAGE_WEIGHT')]
        listing.reported_dimensions = '\n'.join(dimensions) if dimensions else 'API não retornou dimensões da embalagem'
        listing.synced_at = int(time.time())
        g.db.commit()
        return redirect(url_for('dashboard', message='Título, modalidade e dimensões consultados. SKU interno e fretes preservados.'))

    @app.template_filter('brl')
    def brl(value):
        return f'R$ {value:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')

    return app


if not os.getenv('EVOLUX_APP_FACTORY_ONLY'):
    app = create_app()
