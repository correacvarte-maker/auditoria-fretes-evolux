import os
import html
import re
import time
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

os.environ['EVOLUX_APP_FACTORY_ONLY'] = '1'

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from werkzeug.security import generate_password_hash

from app import create_app, digest
from models import BrowserSession, Connection, PendingConnection, Freight, Listing, OAuthState


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'APP_ENV': 'development', 'PUBLIC_BASE_URL': 'http://localhost',
                       'DATABASE_URL': 'sqlite:///' + str(tmp_path / 'test.db'),
                       'ADMIN_USERNAME': 'admin', 'ADMIN_PASSWORD_HASH': generate_password_hash('test-password-long'),
                       'TOKEN_ENCRYPTION_KEY': Fernet.generate_key().decode(),
                       'MELI_CLIENT_ID': '123', 'MELI_CLIENT_SECRET': 'fake-test-secret', 'MELI_SELLER_ID': '456'})


@pytest.fixture
def client(app):
    return app.test_client()


def csrf(client, path='/login'):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client):
    return client.post('/login', data={'csrf': csrf(client), 'username': 'admin', 'password': 'test-password-long'})


def post(client, path, data):
    return client.post(path, data={'csrf': csrf(client, '/'), **data})


def catalog(client):
    assert post(client, '/products', {'sku': 'EV-001', 'name': 'Evolux', 'length': '10', 'width': '20', 'height': '30', 'weight': '1.200'}).status_code == 302
    assert post(client, '/templates', {'name': 'Padrão', 'title': 'Título', 'body': '<script>alert(1)</script>'}).status_code == 302
    assert post(client, '/listings', {'mlb': 'MLB123456789', 'product_id': '1', 'template_id': '1', 'title': 'Produto', 'listing_type': 'gold_special'}).status_code == 302


def test_login_session_rotation_logout_and_headers(app, client):
    assert client.get('/').status_code == 302
    client.get('/login')
    old = client.get_cookie('evolux_session').value
    response = login(client)
    assert response.status_code == 302
    assert client.get_cookie('evolux_session').value != old
    with app.extensions['db_factory']() as db:
        assert db.get(BrowserSession, digest(old)) is None
    page = client.get('/')
    assert 'script-src' in page.headers['Content-Security-Policy']
    assert page.headers['Cache-Control'] == 'no-store'
    assert page.headers['Referrer-Policy'] == 'strict-origin'
    assert 'HttpOnly' in response.headers['Set-Cookie']
    assert post(client, '/logout', {}).status_code == 302
    assert client.get('/').status_code == 302


def test_csrf_origin_host_and_lockout(client):
    assert client.post('/login', data={'username': 'admin'}).status_code == 400
    assert client.post('/login', data={'csrf': 'á'}).status_code == 400
    token = csrf(client)
    assert client.post('/login', data={'csrf': token}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/login', data={'csrf': token}, headers={'Origin': 'null'}).status_code == 403
    assert client.get('/login', headers={'Host': 'evil.example'}).status_code == 400
    for _ in range(5):
        assert client.post('/login', data={'csrf': token, 'username': 'admin', 'password': 'wrong'}).status_code == 401
    assert client.post('/login', data={'csrf': token, 'username': 'admin', 'password': 'test-password-long'}).status_code == 429


def test_session_expiry(app, client):
    login(client)
    with app.extensions['db_factory'].begin() as db:
        session = db.get(BrowserSession, digest(client.get_cookie('evolux_session').value))
        session.expires = int(time.time()) - 1
    assert client.get('/').status_code == 302


def test_freight_decimal_duplicate_and_escaping(app, client):
    login(client)
    catalog(client)
    freight = {'listing_id': '1', 'reference': 'ENV-001', 'date': '2026-10-14', 'expected': '10.10', 'charged': '12.30', 'source': 'Extrato'}
    assert post(client, '/freights', freight).status_code == 302
    assert post(client, '/freights', freight).status_code == 409
    with app.extensions['db_factory']() as db:
        stored = db.scalar(select(Freight))
        assert stored.difference == Decimal('2.20')
    html = client.get('/').get_data(as_text=True)
    assert 'R$ 2,20' in html and 'EV-001' in html and 'MLB123456789' in html
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html
    for invalid in ('NaN', 'Infinity', '-1', '1.234', '1000000000'):
        assert post(client, '/freights', {**freight, 'reference': 'other', 'charged': invalid}).status_code == 400


def test_multiple_mlb_per_sku_and_history_protection(app, client):
    login(client)
    catalog(client)
    assert post(client, '/listings', {'mlb': 'MLB987654321', 'product_id': '1', 'title': 'Outro anúncio', 'listing_type': 'gold_pro'}).status_code == 302
    with app.extensions['db_factory']() as db:
        assert len(db.scalars(select(Listing).where(Listing.product_id == 1)).all()) == 2
    assert post(client, '/freights', {'listing_id': '1', 'reference': 'A', 'date': '2026-10-14', 'expected': '0', 'charged': '0', 'source': 'Extrato'}).status_code == 302
    assert post(client, '/products', {'sku': 'OTHER', 'name': 'Outro', 'length': '10', 'width': '10', 'height': '10', 'weight': '1'}).status_code == 302
    assert post(client, '/listings', {'mlb': 'MLB123456789', 'product_id': '2', 'title': 'Outro', 'listing_type': 'gold_pro'}).status_code == 409


class FakeResponse:
    def __init__(self, data):
        self.data = data
    def raise_for_status(self):
        pass
    def json(self):
        return self.data


def oauth_state(client):
    response = post(client, '/oauth/start', {})
    assert response.status_code == 200
    assert "form-action 'self'" in response.headers['Content-Security-Policy']
    authorization_url = html.unescape(re.search(r'href="(https://auth\.mercadolivre\.com\.br/authorization\?[^"]+)"', response.get_data(as_text=True)).group(1))
    args = parse_qs(urlparse(authorization_url).query)
    assert args['redirect_uri'] == ['http://localhost/oauth/callback']
    assert args['code_challenge_method'] == ['S256']
    return args['state'][0]


def test_oauth_state_binding_single_use_encryption(app, client, monkeypatch):
    login(client)
    state = oauth_state(client)
    other = app.test_client()
    login(other)
    assert other.get('/oauth/callback?code=fake&state=' + state).status_code == 400
    assert client.get('/oauth/callback?code=fake&state=invalid').status_code == 400
    exchanges = []
    def exchange(url, data, timeout):
        exchanges.append(data)
        return FakeResponse({'access_token': 'access-test', 'refresh_token': 'refresh-test', 'expires_in': 21600})
    monkeypatch.setattr('app.requests.post', exchange)
    monkeypatch.setattr('app.requests.get', lambda *a, **k: FakeResponse({'id': 456}))
    assert client.get('/oauth/callback?code=fake&state=' + state).status_code == 302
    assert client.get('/oauth/callback?code=fake&state=' + state).status_code == 400
    assert len(exchanges) == 1 and exchanges[0]['code_verifier']
    with app.extensions['db_factory']() as db:
        connection = db.get(Connection, 1)
        assert 'access-test' not in connection.access
        assert 'refresh-test' not in connection.refresh
        assert app.extensions['cipher'].decrypt(connection.access.encode()) == b'access-test'
    assert 'access-test' not in client.get('/').get_data(as_text=True)


def test_oauth_wrong_seller_and_expiry(app, client, monkeypatch):
    login(client)
    state = oauth_state(client)
    with app.extensions['db_factory'].begin() as db:
        db.get(OAuthState, digest(state)).expires = 0
    assert client.get('/oauth/callback?code=x&state=' + state).status_code == 400
    state = oauth_state(client)
    monkeypatch.setattr('app.requests.post', lambda *a, **k: FakeResponse({'access_token': 'test', 'refresh_token': 'test', 'expires_in': 21600}))
    monkeypatch.setattr('app.requests.get', lambda *a, **k: FakeResponse({'id': 999}))
    assert client.get('/oauth/callback?code=x&state=' + state).status_code == 403
    with app.extensions['db_factory']() as db:
        assert db.get(Connection, 1) is None


def test_refresh_rotation_and_listing_ownership(app, client, monkeypatch):
    login(client)
    catalog(client)
    cipher = app.extensions['cipher']
    with app.extensions['db_factory'].begin() as db:
        db.add(Connection(id=1, seller_id='456', access=cipher.encrypt(b'old').decode(), refresh=cipher.encrypt(b'old-refresh').decode(), expires=0))
    def exchange(url, data, timeout):
        assert data['refresh_token'] == 'old-refresh'
        return FakeResponse({'access_token': 'new', 'refresh_token': 'new-refresh', 'expires_in': 21600})
    monkeypatch.setattr('app.requests.post', exchange)
    monkeypatch.setattr('app.requests.get', lambda *a, **k: FakeResponse({'id': 'MLB123456789', 'seller_id': 999, 'title': 'Wrong seller'}))
    assert post(client, '/listings/1/sync', {}).status_code == 403
    with app.extensions['db_factory']() as db:
        assert cipher.decrypt(db.get(Connection, 1).refresh.encode()) == b'new-refresh'
        assert db.get(Listing, 1).title == 'Produto'
    monkeypatch.setattr('app.requests.get', lambda *a, **k: FakeResponse({'id': 'MLB123456789', 'seller_id': 456, 'title': 'API title', 'listing_type_id': 'gold_pro', 'attributes': [{'id': 'SELLER_PACKAGE_WEIGHT', 'value_name': '1200 g'}]}))
    assert post(client, '/listings/1/sync', {}).status_code == 302
    assert '1200 g' in client.get('/').get_data(as_text=True)


def test_production_rejects_sqlite(app):
    with pytest.raises(RuntimeError, match='PostgreSQL'):
        create_app({**app.config, 'APP_ENV': 'production', 'PUBLIC_BASE_URL': 'https://example.com'})


def test_first_seller_requires_session_confirmation_and_stays_pinned(app, client, monkeypatch):
    app.config['MELI_SELLER_ID'] = ''
    login(client)
    monkeypatch.setattr('app.requests.post', lambda *a, **k: FakeResponse({
        'access_token': 'pending-access', 'refresh_token': 'pending-refresh', 'expires_in': 21600}))
    monkeypatch.setattr('app.requests.get', lambda *a, **k: FakeResponse({'id': 456, 'nickname': 'EVOLUX'}))
    state = oauth_state(client)
    response = client.get('/oauth/callback?code=x&state=' + state)
    assert response.status_code == 302 and response.location.endswith('/oauth/confirm')
    page = client.get('/oauth/confirm').get_data(as_text=True)
    assert 'EVOLUX' in page and '456' in page and 'pending-access' not in page
    with app.extensions['db_factory']() as db:
        assert db.get(Connection, 1) is None
        candidate = db.get(PendingConnection, digest(client.get_cookie('evolux_session').value))
        assert candidate.access != 'pending-access'
    other = app.test_client()
    login(other)
    assert other.get('/oauth/confirm').status_code == 409
    assert client.post('/oauth/confirm').status_code == 400
    assert post(client, '/oauth/confirm', {}).status_code == 302
    assert post(client, '/oauth/confirm', {}).status_code == 409
    catalog(client)
    monkeypatch.setattr('app.requests.get', lambda *a, **k: FakeResponse({
        'id': 'MLB123456789', 'seller_id': 456, 'title': 'Consulta autorizada'}))
    assert post(client, '/listings/1/sync', {}).status_code == 302
    state = oauth_state(client)
    monkeypatch.setattr('app.requests.get', lambda *a, **k: FakeResponse({'id': 999, 'nickname': 'OUTRO'}))
    assert client.get('/oauth/callback?code=x&state=' + state).status_code == 403
    with app.extensions['db_factory']() as db:
        assert db.get(Connection, 1).seller_id == '456'
        assert db.get(PendingConnection, digest(client.get_cookie('evolux_session').value)) is None


def test_pending_seller_expiry_and_cancellation(app, client, monkeypatch):
    app.config['MELI_SELLER_ID'] = ''
    login(client)
    monkeypatch.setattr('app.requests.post', lambda *a, **k: FakeResponse({
        'access_token': 'pending-access', 'refresh_token': 'pending-refresh', 'expires_in': 21600}))
    monkeypatch.setattr('app.requests.get', lambda *a, **k: FakeResponse({'id': 456, 'nickname': 'EVOLUX'}))
    state = oauth_state(client)
    assert client.get('/oauth/callback?code=x&state=' + state).status_code == 302
    session_id = digest(client.get_cookie('evolux_session').value)
    with app.extensions['db_factory'].begin() as db:
        db.get(PendingConnection, session_id).confirm_until = int(time.time()) - 1
    assert post(client, '/oauth/confirm', {}).status_code == 409
    state = oauth_state(client)
    assert client.get('/oauth/callback?code=x&state=' + state).status_code == 302
    assert post(client, '/oauth/cancel', {}).status_code == 302
    with app.extensions['db_factory']() as db:
        assert db.get(PendingConnection, session_id) is None
        assert db.get(Connection, 1) is None
