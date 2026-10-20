import base64
import hashlib
import hmac
import json
import csv
import io
import os
import re
import secrets
import time
from threading import Thread
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import urlencode, urlparse

import requests
from cryptography.fernet import Fernet
from flask import Flask, Response, abort, g, redirect, render_template, request, url_for
from sqlalchemy import create_engine, delete, select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from werkzeug.security import check_password_hash

from models import Base, BrowserSession, LoginGuard, OAuthState, Connection, PendingConnection, Product, AdTemplate, Listing, Freight, AccountItem, CatalogImport, SkuQuote
from catalog_import import run_import
from package_view import package_rows, sku_catalog, reference_signature
from models import MeasureUpload, ItemPublication, ItemPrice
from listing_prices import price_columns
from measure_import import parse as parse_measures, ImportValidationError
from freight_table import calculate as table_calculation, SOURCE_URL as TABLE_URL, CHECKED_ON as TABLE_DATE


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
                      MAX_CONTENT_LENGTH=3 * 1024 * 1024,
                      MAX_FORM_MEMORY_SIZE=64 * 1024,
                      MAX_FORM_PARTS=20)
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
        if request.path != '/catalog/measures/import' and request.content_length and request.content_length > 64 * 1024:
            abort(413)
        # Assets/404s can finish after login rotates the cookie. They must never
        # replace the authenticated cookie with a new anonymous session.
        if request.path == '/health' or request.endpoint in (None, 'static'):
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
            g.db.execute(delete(PendingConnection).where(PendingConnection.confirm_until <= now))
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
                                 'Referrer-Policy': 'strict-origin', 'X-Frame-Options': 'DENY',
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

    @app.errorhandler(413)
    def oversized(_):
        return page_error('Arquivo muito grande. Envie até 2 MB e 10.000 linhas.', 413)

    @app.errorhandler(requests.RequestException)
    def remote_error(_):
        return page_error('O Mercado Livre não respondeu ou recusou a consulta. Tente novamente; se necessário, reconecte a conta.', 502)

    @app.get('/health')
    def health():
        g.db.execute(select(LoginGuard.id).limit(1))
        return {'status': 'ok'}

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'GET' and g.browser.authenticated:
            return redirect(url_for('dashboard'))
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
                    g.db.execute(delete(PendingConnection).where(PendingConnection.session_id == g.browser.id))
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
        g.db.execute(delete(PendingConnection).where(PendingConnection.session_id == g.browser.id))
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
                               catalog_count=g.db.scalar(select(func.count()).select_from(AccountItem).where(
                                   AccountItem.seller_id == (g.db.get(Connection, 1).seller_id if g.db.get(Connection, 1) else ''))),
                               import_job=g.db.get(CatalogImport, 1),
                               measure_rows=package_rows(g.db.scalars(select(AccountItem).where(
                                   AccountItem.seller_id == (g.db.get(Connection, 1).seller_id if g.db.get(Connection, 1) else '')
                               ).order_by(AccountItem.mlb).limit(20)).all())[:20],
                               message=request.args.get('message'), oauth_ready=oauth_ready())

    @app.get('/catalog/api-measures')
    @protected
    def catalog_measures():
        connection = g.db.get(Connection, 1)
        if not connection:
            return page_error('Conecte a conta para ver as medidas importadas.', 409)
        page = max(1, min(100000, int(request.args.get('page', '1'))))
        search = request.args.get('q', '').strip()[:80]
        query = select(AccountItem).where(AccountItem.seller_id == connection.seller_id)
        if search:
            encoded = json.dumps(search)[1:-1]
            query = query.where(or_(AccountItem.skus_json.contains(encoded, autoescape=True),
                                    AccountItem.mlb.contains(search.upper(), autoescape=True)))
        total = g.db.scalar(select(func.count()).select_from(query.subquery()))
        records = g.db.scalars(query.order_by(AccountItem.mlb).offset((page - 1) * 100).limit(100)).all()
        return render_template('measures.html', rows=package_rows(records), total=total,
                               page=page, pages=max(1, (total + 99) // 100), search=search)

    def connected_items():
        connection = g.db.get(Connection, 1)
        if not connection:
            abort(409, 'Conecte a conta para consultar os SKUs.')
        return g.db.scalars(select(AccountItem).where(AccountItem.seller_id == connection.seller_id).order_by(AccountItem.mlb)).all()

    @app.get('/catalog/costs.csv')
    @protected
    def export_current_costs():
        def safe_cell(value):
            value = str(value or '')
            return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';', lineterminator='\r\n')
        writer.writerow(['SKU no Mercado Livre', 'MLB', 'Título', 'Status', 'Preço cheio (BRL)',
                         'Preço promocional (BRL)', 'Preço vigente (BRL)', 'Situação do preço',
                         'Custo atual de frete (BRL)', 'Envio', 'Logística', 'Grátis ao comprador',
                         'Consulta (Brasília)', 'Fonte / disponibilidade'])
        items = connected_items()
        prices = {p.mlb: p for p in g.db.scalars(select(ItemPrice).where(ItemPrice.seller_id == g.db.get(Connection, 1).seller_id)).all()}
        for item in items:
            price_data = price_columns(item, prices.get(item.mlb))
            writer.writerow([safe_cell(', '.join(json.loads(item.skus_json))), item.mlb, safe_cell(item.title),
                safe_cell(item.status), *[format(price_data[key], '.2f').replace('.', ',') if price_data[key] is not None else ''
                                          for key in ('full_price', 'promotional_price', 'effective_price')],
                safe_cell(price_data['price_note']),
                format(item.quote, '.2f').replace('.', ',') if item.quote is not None else '',
                safe_cell(item.shipping_mode), safe_cell(item.logistic_type), 'Sim' if item.free_shipping else 'Não',
                time.strftime('%d/%m/%Y %H:%M', time.gmtime(item.synced_at - 10800)), safe_cell(item.quote_note)])
        return Response('\ufeff' + output.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment; filename="custos-frete-atuais.csv"'})

    @app.get('/catalog/measures')
    @protected
    def my_measures():
        entries = sku_catalog(connected_items())
        products = {p.sku: p for p in g.db.scalars(select(Product)).all()}
        search = request.args.get('q', '').strip()[:80]
        entries = [value for key, value in sorted(entries.items()) if not search or search.casefold() in key.casefold()
                   or any(search.upper() in mlb for mlb in value['mlbs'])]
        page = max(1, min(100000, int(request.args.get('page', '1'))))
        rows = [{**entry, 'product': products.get(entry['sku'])} for entry in entries[(page - 1) * 50:page * 50]]
        return render_template('my_measures.html', rows=rows, total=len(entries), page=page,
                               pages=max(1, (len(entries) + 49) // 50), search=search,
                               message=request.args.get('message'))

    @app.post('/catalog/sku-measures')
    @protected
    def save_sku_measures():
        sku = text_field('sku', 80)
        entry = sku_catalog(connected_items()).get(sku)
        if not entry:
            return page_error('Este SKU não foi importado da conta conectada.', 400)
        product = g.db.scalar(select(Product).where(Product.sku == sku)) or Product(sku=sku, name=entry['name'][:180])
        for key in ('width', 'height', 'length'):
            setattr(product, key, number(key, 10000))
        product.weight = number('weight', 10000, 3)
        g.db.add(product)
        g.db.commit()
        return redirect(url_for('my_measures', q=sku, message='Medidas salvas. A comparação com a tabela foi recalculada automaticamente.'))

    @app.get('/catalog/measures/model.csv')
    @protected
    def measures_model():
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';', lineterminator='\r\n')
        writer.writerow(['sku', 'largura', 'altura', 'comprimento', 'peso bruto'])
        for sku in sorted(sku_catalog(connected_items())):
            safe_sku = "'" + sku if sku.lstrip().startswith(('=', '+', '-', '@')) or sku.startswith(('\t', '\r', '\n')) else sku
            writer.writerow([safe_sku, '', '', '', ''])
        return Response('\ufeff' + output.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': 'attachment; filename="modelo-medidas-sku.csv"'})

    @app.route('/catalog/measures/import', methods=['GET', 'POST'])
    @protected
    def import_measures():
        entries = sku_catalog(connected_items())
        products = {p.sku: p for p in g.db.scalars(select(Product)).all()}
        errors, preview = [], []
        if request.method == 'POST':
            upload = request.files.get('file')
            if not upload:
                errors = ['Selecione a planilha .xlsx ou .csv.']
            else:
                try:
                    parsed_rows = parse_measures(upload, entries)
                    pending = g.db.get(MeasureUpload, g.browser.id) or MeasureUpload(session_id=g.browser.id)
                    pending.seller_id = g.db.get(Connection, 1).seller_id
                    pending.rows_json = json.dumps(parsed_rows)
                    pending.expires = int(time.time()) + 600
                    g.db.execute(delete(MeasureUpload).where(MeasureUpload.expires <= int(time.time())))
                    g.db.add(pending)
                    g.db.commit()
                except ImportValidationError as error:
                    errors = error.errors
                    stale = g.db.get(MeasureUpload, g.browser.id)
                    if stale:
                        g.db.delete(stale)
                        g.db.commit()
        pending = g.db.get(MeasureUpload, g.browser.id)
        if errors and pending:
            g.db.delete(pending)
            g.db.commit()
            pending = None
        rows = json.loads(pending.rows_json) if pending and pending.expires > int(time.time()) and pending.seller_id == g.db.get(Connection, 1).seller_id else []
        for row in rows[:100]:
            product = Product(**row)
            for key in ('width', 'height', 'length', 'weight'):
                setattr(product, key, Decimal(row[key]))
            preview.append({**row, **table_calculation(product), 'existing': row['sku'] in products})
        return render_template('import_measures.html', errors=errors, rows=preview, total=len(rows),
                               updated=sum(row['sku'] in products for row in rows)), (400 if errors else 200)

    @app.post('/catalog/measures/confirm')
    @protected
    def confirm_measures():
        entries = sku_catalog(connected_items())
        pending = g.db.execute(select(MeasureUpload).where(MeasureUpload.session_id == g.browser.id).with_for_update()).scalar_one_or_none()
        if not pending or pending.expires <= int(time.time()) or pending.seller_id != g.db.get(Connection, 1).seller_id:
            return page_error('Prévia expirada. Envie a planilha novamente.', 409)
        rows = json.loads(pending.rows_json)
        if any(row['sku'] not in entries for row in rows):
            return page_error('Os SKUs da conta mudaram. Envie a planilha novamente.', 409)
        for row in rows:
            product = g.db.scalar(select(Product).where(Product.sku == row['sku'])) or Product(sku=row['sku'], name=entries[row['sku']]['name'][:180])
            for key in ('width', 'height', 'length', 'weight'):
                setattr(product, key, Decimal(row[key]))
            g.db.add(product)
        g.db.delete(pending)
        g.db.commit()
        return redirect(url_for('my_measures', message=f'{len(rows)} SKUs importados. A comparação com a tabela foi recalculada automaticamente.'))

    @app.get('/catalog/comparison')
    @protected
    def current_comparison():
        items = connected_items()
        current_job = g.db.get(CatalogImport, 1)
        prices = {p.mlb: p for p in g.db.scalars(select(ItemPrice).where(ItemPrice.seller_id == g.db.get(Connection, 1).seller_id)).all()}
        publication_filter = request.args.get('publication', 'all')
        if publication_filter not in ('all', 'catalog', 'traditional', 'unknown'):
            return page_error('Tipo de anúncio inválido.')
        publications = {p.mlb: p for p in g.db.scalars(select(ItemPublication).where(
            ItemPublication.seller_id == g.db.get(Connection, 1).seller_id)).all()}
        publication_labels = {'catalog': 'Catálogo', 'traditional': 'Tradicional', 'unknown': 'Não informado'}
        products = {p.sku: p for p in g.db.scalars(select(Product)).all()}
        search = request.args.get('q', '').strip()[:80]
        rows = []
        for item in items:
            publication = publications.get(item.mlb)
            flag = publication.catalog_listing if publication and publication.seen_run == item.seen_run else None
            publication_kind = 'catalog' if flag == 1 else 'traditional' if flag == 0 else 'unknown'
            if publication_filter != 'all' and publication_filter != publication_kind:
                continue
            for sku in json.loads(item.skus_json) or ['']:
                if search and search.casefold() not in sku.casefold() and search.upper() not in item.mlb:
                    continue
                product = products.get(sku)
                price_data = price_columns(item, prices.get(item.mlb))
                calculation = table_calculation(product, price_data['effective_price']) if product else {}
                expected = calculation.get('expected')
                note = (calculation.get('band', '') if expected is not None else 'Preço vigente indisponível; atualize os anúncios') if product else 'Preencha suas medidas para este SKU'
                fresh_quote = not current_job or item.seen_run == current_job.run_id
                current_quote = item.quote if fresh_quote else None
                difference = current_quote - expected if current_quote is not None and expected is not None else None
                rows.append({'sku': sku, 'item': item, 'product': product, 'expected': expected,
                             'publication_label': publication_labels[publication_kind],
                             **price_data,
                             'difference': difference, 'note': note,
                             'current_quote': current_quote,
                             'current_quote_note': item.quote_note if fresh_quote else 'Sem consulta nesta atualização; custo anterior não usado',
                             **calculation})
        complete = [row for row in rows if row['difference'] is not None]
        counts = {'more': 0, 'less': 0, 'correct': 0, 'pending': 0}
        labels = {'more': 'Pagando mais', 'less': 'Pagando menos', 'correct': 'Correto', 'pending': 'Pendente'}
        for row in rows:
            difference = row['difference']
            result = ('pending' if difference is None else 'more' if difference > 0 else 'less' if difference < 0 else 'correct')
            row['result'], row['result_label'] = result, labels[result]
            counts[result] += 1
        result_filter = request.args.get('result', 'all')
        if result_filter not in ('all', *counts):
            return page_error('Filtro de comparação inválido.')
        filtered = [row for row in rows if result_filter == 'all' or row['result'] == result_filter]
        filtered.sort(key=lambda row: (row['difference'] is None, -(row['difference'] or Decimal(0)), row['sku'], row['item'].mlb))
        page = max(1, min(100000, int(request.args.get('page', '1'))))
        return render_template('comparison.html', rows=filtered[(page - 1) * 100:page * 100], total=len(rows),
                               compared=len(complete), higher=sum(row['difference'] > 0 for row in complete),
                               pending=len(rows) - len(complete), page=page,
                               pages=max(1, (len(filtered) + 99) // 100), search=search, table_url=TABLE_URL, table_date=TABLE_DATE,
                               counts=counts, result_filter=result_filter, filtered_total=len(filtered),
                               publication_filter=publication_filter)

    @app.get('/catalog')
    @protected
    def account_catalog():
        connection = g.db.get(Connection, 1)
        if not connection:
            return page_error('Conecte a conta antes de consultar os anúncios.', 409)
        page = max(1, min(100000, int(request.args.get('page', '1'))))
        query = select(AccountItem).where(AccountItem.seller_id == connection.seller_id)
        search = request.args.get('q', '').strip()[:80]
        records = g.db.scalars(query.order_by(AccountItem.mlb)).all()
        if search:
            needle = search.casefold()
            records = [item for item in records if needle in item.mlb.casefold()
                       or any(needle in sku.casefold() for sku in json.loads(item.skus_json))]
        total = len(records)
        records = records[(page - 1) * 100:page * 100]
        prices = {p.mlb: p for p in g.db.scalars(select(ItemPrice).where(ItemPrice.seller_id == connection.seller_id)).all()}
        products = {p.sku: p for p in g.db.scalars(select(Product)).all()}
        rows = []
        for item in records:
            skus = json.loads(item.skus_json)
            product = products.get(skus[0]) if len(skus) == 1 else None
            price_data = price_columns(item, prices.get(item.mlb))
            calculation = table_calculation(product, price_data['effective_price']) if product else {}
            expected = calculation.get('expected')
            rows.append({'item': item, 'skus': ', '.join(skus), 'custom': ', '.join(json.loads(item.custom_fields_json)),
                         **price_data, 'price_band': calculation.get('price_band'),
                         'expected': expected, 'reference_note': ('Tabela ' + calculation['price_band'] + ' · ' + TABLE_DATE) if expected is not None else 'Consulte preços e informe suas medidas para calcular por faixa',
                         'difference': item.quote - expected if item.quote is not None and expected is not None else None})
        return render_template('catalog.html', rows=rows, total=total, page=page,
                               pages=max(1, (total + 99) // 100), job=g.db.get(CatalogImport, 1), search=search)

    @app.post('/catalog/import')
    @protected
    def import_catalog():
        connection = g.db.execute(select(Connection).where(Connection.id == 1).with_for_update()).scalar_one_or_none()
        if not connection or not oauth_ready():
            return page_error('Conecte a conta antes de importar.', 409)
        job = g.db.execute(select(CatalogImport).where(CatalogImport.id == 1).with_for_update()).scalar_one_or_none()
        now = int(time.time())
        if job and job.status == 'running' and job.updated_at > now - 300:
            return redirect('/catalog/import')
        job = job or CatalogImport(id=1)
        run_id = secrets.token_urlsafe(32)
        seller_id = connection.seller_id
        job.seller_id, job.run_id, job.status = seller_id, run_id, 'running'
        job.phase, job.total, job.processed, job.quotes, job.failures = 'Iniciando importação de todos os anúncios', 0, 0, 0, 0
        job.started_at = job.updated_at = now
        g.db.add(job)
        g.db.commit()
        args = (factory, cipher, token_request, store_tokens, run_id, seller_id)
        if app.config.get('CATALOG_IMPORT_INLINE'):
            run_import(*args)
        else:
            Thread(target=run_import, args=args, daemon=True, name='seller-catalog-import').start()
        return redirect('/catalog/import')

    @app.get('/catalog/import')
    @protected
    def import_progress():
        job, connection = g.db.get(CatalogImport, 1), g.db.get(Connection, 1)
        if not job or not connection or job.seller_id != connection.seller_id:
            return redirect('/')
        stale = job.status == 'running' and job.updated_at <= int(time.time()) - 300
        return render_template('import_progress.html', job=job, stale=stale)

    @app.post('/catalog/import/cancel')
    @protected
    def cancel_import():
        job = g.db.execute(select(CatalogImport).where(CatalogImport.id == 1).with_for_update()).scalar_one_or_none()
        if job and job.status == 'running':
            job.status, job.phase, job.updated_at = 'interrupted', 'Importação interrompida. Dados já consultados preservados.', int(time.time())
        g.db.commit()
        return redirect('/catalog')

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
        return all(app.config[k] for k in ('MELI_CLIENT_ID', 'MELI_CLIENT_SECRET'))

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
            return page_error('Configure os dados da aplicação Mercado Livre no Render.', 503)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        g.db.execute(delete(OAuthState).where(OAuthState.session_id == g.browser.id))
        g.db.execute(delete(PendingConnection).where(PendingConnection.session_id == g.browser.id))
        g.db.add(OAuthState(id=digest(state), session_id=g.browser.id,
                            verifier=cipher.encrypt(verifier.encode()).decode(), expires=int(time.time()) + 600))
        g.db.commit()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        authorization_url = 'https://auth.mercadolivre.com.br/authorization?' + urlencode({
            'response_type': 'code', 'client_id': app.config['MELI_CLIENT_ID'],
            'redirect_uri': base + '/oauth/callback', 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256'})
        # A navigation link avoids browser CSP form-action checks on cross-origin
        # redirects after POST, while keeping all form submissions same-origin.
        return render_template('authorize_connection.html', authorization_url=authorization_url)

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
        identity = response.json()
        seller = str(identity.get('id', ''))
        if not seller.isdigit():
            raise ValueError('seller')
        connection = g.db.get(Connection, 1)
        configured_seller = app.config['MELI_SELLER_ID']
        pinned_seller = configured_seller or (connection.seller_id if connection else '')
        if pinned_seller and seller != pinned_seller:
            return page_error('A conta autorizada não é o vendedor configurado. Conecte a conta correta.', 403)
        if not configured_seller:
            candidate = g.db.get(PendingConnection, g.browser.id) or PendingConnection(session_id=g.browser.id)
            candidate.seller_id = seller
            candidate.nickname = str(identity.get('nickname') or seller)[:120]
            candidate.confirm_until = int(time.time()) + 600
            store_tokens(candidate, tokens)
            g.db.add(candidate)
            g.db.commit()
            return redirect(url_for('confirm_connection'))
        connection = connection or Connection(id=1)
        connection.seller_id = seller
        store_tokens(connection, tokens)
        g.db.add(connection)
        g.db.commit()
        return redirect(url_for('dashboard', message='Conta Mercado Livre conectada.'))

    @app.route('/oauth/confirm', methods=['GET', 'POST'])
    @protected
    def confirm_connection():
        candidate = g.db.execute(select(PendingConnection).where(
            PendingConnection.session_id == g.browser.id).with_for_update()).scalar_one_or_none()
        now = int(time.time())
        if not candidate or candidate.confirm_until <= now or candidate.expires <= now:
            return page_error('Confirmação expirada ou já concluída. Inicie uma nova conexão.', 409)
        if request.method == 'GET':
            return render_template('confirm_connection.html', candidate=candidate)
        connection = g.db.execute(select(Connection).where(Connection.id == 1).with_for_update()).scalar_one_or_none()
        allowed_seller = app.config['MELI_SELLER_ID'] or (connection.seller_id if connection else '')
        if allowed_seller and candidate.seller_id != allowed_seller:
            return page_error('A conexão existente pertence a outro vendedor. Desconecte antes de trocar a conta.', 409)
        connection = connection or Connection(id=1)
        connection.seller_id, connection.access = candidate.seller_id, candidate.access
        connection.refresh, connection.expires = candidate.refresh, candidate.expires
        g.db.add(connection)
        g.db.delete(candidate)
        g.db.commit()
        return redirect(url_for('dashboard', message='Conta vendedora confirmada e conectada.'))

    @app.post('/oauth/cancel')
    @protected
    def cancel_connection():
        g.db.execute(delete(PendingConnection).where(PendingConnection.session_id == g.browser.id))
        g.db.commit()
        return redirect(url_for('dashboard', message='Conexão pendente cancelada.'))

    @app.post('/oauth/disconnect')
    @protected
    def disconnect():
        g.db.execute(delete(Connection))
        g.db.execute(delete(OAuthState))
        g.db.execute(delete(PendingConnection))
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
        if not connection or not oauth_ready() or (app.config['MELI_SELLER_ID'] and connection.seller_id != app.config['MELI_SELLER_ID']):
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

    @app.template_filter('consulted_at')
    def consulted_at(value):
        return time.strftime('%d/%m/%Y %H:%M (Brasília)', time.gmtime(value - 10800))

    return app


if not os.getenv('EVOLUX_APP_FACTORY_ONLY'):
    app = create_app()
