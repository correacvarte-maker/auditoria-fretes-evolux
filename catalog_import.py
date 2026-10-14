"""Read-only seller catalog and current shipping quotations, no customer data."""
import json
import re
import time
from decimal import Decimal, InvalidOperation

import requests
from sqlalchemy import select

from models import AccountItem, CatalogImport, Connection, Listing, Product

API = 'https://api.mercadolibre.com'


class ImportStopped(Exception):
    pass


def money(value):
    try:
        value = Decimal(str(value))
        if value.is_finite() and 0 <= value <= Decimal('9999999999.99'):
            return value.quantize(Decimal('.01'))
    except (InvalidOperation, ValueError, TypeError):
        pass
    return None


def item_fields(item):
    # SELLER_SKU and seller_custom_field are distinct API fields.
    skus, custom = set(), set()
    for part in [item, *item.get('variations', [])]:
        for attribute in part.get('attributes', []):
            if attribute.get('id') == 'SELLER_SKU' and attribute.get('value_name'):
                skus.add(str(attribute['value_name'])[:80])
        if part.get('seller_custom_field'):
            custom.add(str(part['seller_custom_field'])[:180])
    dimensions = []
    for attribute in item.get('attributes', []):
        if attribute.get('id') in ('SELLER_PACKAGE_LENGTH', 'SELLER_PACKAGE_WIDTH',
                                   'SELLER_PACKAGE_HEIGHT', 'SELLER_PACKAGE_WEIGHT'):
            dimensions.append(attribute['id'] + ': ' + str(attribute.get('value_name') or 'não informado')[:120])
    shipping = item.get('shipping') or {}
    if shipping.get('dimensions'):
        dimensions.append('shipping.dimensions (cm e g): ' + str(shipping['dimensions'])[:120])
    return sorted(skus), sorted(custom), '\n'.join(dimensions) or 'API não informou a embalagem'


def read_quote(body):
    coverage = ((body.get('coverage') or {}).get('all_country') or {})
    if coverage.get('currency_id') != 'BRL':
        return None, None, 'Cotação em reais não disponível'
    cost = money(coverage.get('list_cost'))
    if cost is None:
        return None, None, 'API não retornou custo de envio'
    weight = money(coverage.get('billable_weight'))
    return cost, weight, 'Cotação atual da API; não é débito de uma venda'


def run_import(factory, cipher, token_request, store_tokens, run_id, seller_id):
    def progress(**values):
        with factory.begin() as db:
            job = db.execute(select(CatalogImport).where(CatalogImport.id == 1).with_for_update()).scalar_one()
            if job.run_id != run_id or job.status != 'running':
                raise ImportStopped()
            for key, value in values.items():
                setattr(job, key, value)
            job.updated_at = int(time.time())

    def api_get(path, params=None):
        # Check the pinned account on every call. Refresh tokens are serialized.
        with factory.begin() as db:
            connection = db.execute(select(Connection).where(Connection.id == 1).with_for_update()).scalar_one_or_none()
            job = db.get(CatalogImport, 1)
            if not connection or connection.seller_id != seller_id or job.run_id != run_id or job.status != 'running':
                raise ImportStopped()
            if connection.expires <= int(time.time()) + 60:
                store_tokens(connection, token_request({'grant_type': 'refresh_token',
                    'refresh_token': cipher.decrypt(connection.refresh.encode()).decode()}))
            access = cipher.decrypt(connection.access.encode()).decode()
        for attempt in range(3):
            response = requests.get(API + path, params=params,
                headers={'Authorization': 'Bearer ' + access}, timeout=20)
            if response.status_code == 429 and attempt < 2:
                progress(phase='Aguardando o limite de consultas do Mercado Livre')
                time.sleep(min(30, max(1, int(response.headers.get('Retry-After', '5')))))
                continue
            response.raise_for_status()
            return response.json()

    def quote(item, reference=None):
        shipping = item.get('shipping') or {}
        if shipping.get('mode') != 'me2':
            return None, None, 'Cotação indisponível para esta modalidade de envio'
        params = {'item_id': item['id'], 'verbose': 'true',
                  'free_shipping': 'true' if shipping.get('free_shipping') else 'false'}
        if reference:
            params.pop('item_id')
            params.update(dimensions=f'{reference.height}x{reference.width}x{reference.length},{int(reference.weight * 1000)}',
                          item_price=item.get('price'), listing_type_id=item.get('listing_type_id'),
                          mode=shipping.get('mode'), logistic_type=shipping.get('logistic_type'),
                          condition=item.get('condition', 'new'))
        try:
            return read_quote(api_get('/users/' + seller_id + '/shipping_options/free', params))
        except requests.HTTPError as error:
            code = error.response.status_code if error.response is not None else 0
            if code in (400, 403, 404):
                return None, None, 'API não disponibilizou esta cotação (HTTP ' + str(code) + ')'
            raise

    try:
        ids, seen, scroll = [], set(), None
        while True:
            progress(phase=f'Localizando todos os anúncios: {len(ids)} encontrados')
            params = {'search_type': 'scan', 'limit': 50}
            if scroll:
                params['scroll_id'] = scroll
            result = api_get('/users/' + seller_id + '/items/search', params)
            page = result.get('results') or []
            if not page:
                break
            if not isinstance(page, list) or any(not re.fullmatch(r'MLB\d{6,20}', str(value)) for value in page):
                raise ValueError('Invalid item IDs')
            fresh = [value for value in page if value not in seen]
            if not fresh:
                raise ValueError('Repeated scan page')
            seen.update(fresh)
            ids.extend(fresh)
            scroll = result.get('scroll_id') or scroll
            total = int((result.get('paging') or {}).get('total', 0))
            progress(total=max(total, len(ids)))
            if not scroll:
                if total > len(ids):
                    raise ValueError('Incomplete scan')
                break
        progress(total=len(ids), phase='Consultando anúncios e fretes atuais')
        processed = failures = quotes = 0
        for offset in range(0, len(ids), 20):
            batch = ids[offset:offset + 20]
            bodies = api_get('/items', {'ids': ','.join(batch), 'include_attributes': 'all'})
            if not isinstance(bodies, list):
                raise ValueError('Invalid multiget response')
            available = {str(row.get('body', {}).get('id')): row['body'] for row in bodies
                         if row.get('code') == 200 and isinstance(row.get('body'), dict)}
            for mlb in batch:
                item = available.get(mlb)
                if not item or str(item.get('seller_id', '')) != seller_id:
                    failures += 1
                    processed += 1
                    progress(processed=processed, failures=failures)
                    continue
                progress(phase=f'Consultando frete de {mlb} ({processed + 1}/{len(ids)})')
                skus, custom, dimensions = item_fields(item)
                with factory() as db:
                    listing = db.scalar(select(Listing).where(Listing.mlb == mlb))
                    reference = listing.product if listing else (
                        db.scalar(select(Product).where(Product.sku == skus[0])) if len(skus) == 1 else None)
                    # Detach complete product before network work, with its measured dimensions.
                    if reference:
                        db.expunge(reference)
                cost, weight, quote_note = quote(item)
                expected, _, reference_note = quote(item, reference) if reference else (
                    None, None, 'Cadastre a embalagem correta do SKU para calcular a referência')
                if reference and expected is not None:
                    reference_note = 'Cotação com embalagem interna do SKU ' + reference.sku
                with factory.begin() as db:
                    connection = db.get(Connection, 1)
                    job = db.get(CatalogImport, 1)
                    if not connection or connection.seller_id != seller_id or job.run_id != run_id or job.status != 'running':
                        raise ImportStopped()
                    record = db.get(AccountItem, mlb) or AccountItem(mlb=mlb)
                    shipping = item.get('shipping') or {}
                    record.seller_id, record.title = seller_id, str(item.get('title') or mlb)[:180]
                    record.status = str(item.get('status') or 'não informado')[:40]
                    record.listing_type = str(item.get('listing_type_id') or 'não informado')[:40]
                    record.skus_json, record.custom_fields_json = json.dumps(skus), json.dumps(custom)
                    record.dimensions = dimensions
                    record.shipping_mode = str(shipping.get('mode') or 'não informado')[:40]
                    record.logistic_type = str(shipping.get('logistic_type') or 'não informado')[:40]
                    record.free_shipping = int(bool(shipping.get('free_shipping')))
                    record.price = money(item.get('price')) if item.get('currency_id') == 'BRL' else None
                    record.quote, record.expected_quote, record.billable_weight = cost, expected, weight
                    record.quote_note, record.reference_note = quote_note, reference_note[:180]
                    record.synced_at, record.seen_run = int(time.time()), run_id
                    db.add(record)
                processed += 1
                quotes += int(cost is not None)
                progress(processed=processed, quotes=quotes, failures=failures)
        progress(status='partial' if failures else 'complete',
                 phase='Importação concluída' if not failures else 'Importação concluída com anúncios indisponíveis')
    except ImportStopped:
        with factory.begin() as db:
            job = db.get(CatalogImport, 1)
            if job and job.run_id == run_id and job.status == 'running':
                job.status, job.phase = 'interrupted', 'A conexão mudou. Inicie uma nova importação.'
                job.updated_at = int(time.time())
    except Exception as error:
        code = error.response.status_code if isinstance(error, requests.HTTPError) and error.response is not None else None
        message = ('Mercado Livre negou a consulta. Revise as permissões de leitura e reconecte a conta.' if code in (401, 403)
                   else 'Consulta interrompida. Os dados já importados foram preservados; tente novamente.')
        with factory.begin() as db:
            job = db.get(CatalogImport, 1)
            if job and job.run_id == run_id:
                job.status, job.phase, job.updated_at = 'failed', message, int(time.time())
