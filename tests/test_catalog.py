import json
import time
from decimal import Decimal

import requests
from sqlalchemy import select

from test_app import app, client, login, post, catalog
from models import AccountItem, CatalogImport, Connection, Product
from catalog_import import item_fields, read_quote


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.headers = body, status, {}
    def json(self):
        return self.body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('private API error must not reach the page', response=self)


def connect(app):
    app.config['CATALOG_IMPORT_INLINE'] = True
    cipher = app.extensions['cipher']
    with app.extensions['db_factory'].begin() as db:
        db.add(Connection(id=1, seller_id='456', access=cipher.encrypt(b'access').decode(),
                          refresh=cipher.encrypt(b'refresh').decode(), expires=int(time.time()) + 3600))


def test_import_pages_quotes_zero_currency_and_preserves_reference(app, client, monkeypatch):
    login(client)
    catalog(client)
    connect(app)
    pages, quote_calls = [], []
    items = {
        'MLB123456789': {'id': 'MLB123456789', 'seller_id': 456, 'title': 'Remote title', 'status': 'active',
            'currency_id': 'BRL', 'price': 100, 'listing_type_id': 'gold_pro', 'condition': 'new',
            'shipping': {'mode': 'me2', 'logistic_type': 'drop_off', 'free_shipping': True},
            'attributes': [{'id': 'SELLER_SKU', 'value_name': 'EV-001'},
                           {'id': 'SELLER_PACKAGE_WEIGHT', 'value_name': '3000 g'}]},
        'MLB222222222': {'id': 'MLB222222222', 'seller_id': 456, 'title': 'Zero quote', 'status': 'paused',
            'shipping': {'mode': 'me2'}, 'attributes': [], 'seller_custom_field': 'NOT-A-SKU'},
        'MLB333333333': {'id': 'MLB333333333', 'seller_id': 456, 'title': 'Foreign currency', 'status': 'closed',
            'shipping': {'mode': 'me2'}, 'variations': [{'attributes': [{'id': 'SELLER_SKU', 'value_name': 'VAR-1'}]}]},
    }
    def get(url, params=None, **kwargs):
        assert kwargs['headers']['Authorization'] == 'Bearer access'
        if url.endswith('/items/search'):
            pages.append(dict(params))
            batch = list(items)[:2] if len(pages) == 1 else list(items)[2:] if len(pages) == 2 else []
            return Response({'results': batch, 'scroll_id': 'cursor', 'paging': {'total': 3}})
        if url.endswith('/items'):
            return Response([{'code': 200, 'body': items[x]} for x in params['ids'].split(',')])
        if url.endswith('/shipping_options/free'):
            quote_calls.append(params)
            if params.get('item_id') == 'MLB333333333':
                return Response({'coverage': {'all_country': {'list_cost': 99, 'currency_id': 'ARS'}}})
            cost = 10 if 'dimensions' in params else 0 if params.get('item_id') == 'MLB222222222' else 15
            return Response({'coverage': {'all_country': {'list_cost': cost, 'currency_id': 'BRL', 'billable_weight': 1200}}})
        raise AssertionError(url)
    monkeypatch.setattr('catalog_import.requests.get', get)
    assert post(client, '/catalog/import', {}).status_code == 302
    with app.extensions['db_factory']() as db:
        job = db.get(CatalogImport, 1)
        assert (job.status, job.total, job.processed, job.quotes) == ('complete', 3, 3, 2)
        record = db.get(AccountItem, 'MLB123456789')
        assert record.quote == Decimal('15') and record.expected_quote == Decimal('10')
        assert db.get(Product, 1).weight == Decimal('1.2')
        assert db.get(AccountItem, 'MLB222222222').quote == 0
        assert json.loads(db.get(AccountItem, 'MLB222222222').skus_json) == []
        assert db.get(AccountItem, 'MLB333333333').quote is None
    assert pages[1]['scroll_id'] == 'cursor' and 'offset' not in pages[1]
    assert any(x.get('dimensions') == '30.00x20.00x10.00,1200' for x in quote_calls)
    page = client.get('/catalog').get_data(as_text=True)
    assert 'R$ 5,00' in page and 'NOT-A-SKU' in page and 'SKU não informado' in page
    assert 'R$ 0,00' in page and 'não comprova' in page
    assert post(client, '/catalog/import', {}).status_code == 302
    with app.extensions['db_factory']() as db:
        # A new empty scan keeps previous observations with a visible old-run label.
        assert len(db.scalars(select(AccountItem)).all()) == 3
    assert 'Resultado de importação anterior' in client.get('/catalog').get_data(as_text=True)


def test_import_can_scan_more_than_1000_and_rejects_foreign_items(app, client, monkeypatch):
    login(client)
    connect(app)
    ids = ['MLB' + str(100000000 + x) for x in range(1101)]
    offset = 0
    def get(url, params=None, **kwargs):
        nonlocal offset
        if url.endswith('/items/search'):
            page = ids[offset:offset + 50]
            offset += 50
            return Response({'results': page, 'scroll_id': 'scan', 'paging': {'total': len(ids)}})
        return Response([{'code': 200, 'body': {'id': mlb, 'seller_id': 999 if mlb == ids[0] else 456,
                         'title': 'Item', 'shipping': {'mode': 'custom'}}} for mlb in params['ids'].split(',')])
    monkeypatch.setattr('catalog_import.requests.get', get)
    post(client, '/catalog/import', {})
    with app.extensions['db_factory']() as db:
        job = db.get(CatalogImport, 1)
        assert (job.status, job.processed, job.failures) == ('partial', 1101, 1)
        assert db.get(AccountItem, ids[0]) is None
        assert len(db.scalars(select(AccountItem)).all()) == 1100
    assert 'Página 11 de 11' in client.get('/catalog?page=11').get_data(as_text=True)


def test_failed_import_and_csrf_and_cancel(app, client, monkeypatch):
    login(client)
    connect(app)
    assert client.post('/catalog/import').status_code == 400
    monkeypatch.setattr('catalog_import.requests.get', lambda *a, **k: Response({}, 403))
    post(client, '/catalog/import', {})
    page = client.get('/catalog/import').get_data(as_text=True)
    assert 'Revise as permissões' in page and 'private API error' not in page
    with app.extensions['db_factory'].begin() as db:
        job = db.get(CatalogImport, 1)
        job.status, job.updated_at = 'running', int(time.time())
    assert post(client, '/catalog/import', {}).status_code == 302
    post(client, '/catalog/import/cancel', {})
    with app.extensions['db_factory']() as db:
        assert db.get(CatalogImport, 1).status == 'interrupted'


def test_variation_skus_and_custom_fields_remain_separate():
    skus, custom, _ = item_fields({'attributes': [{'id': 'SELLER_SKU', 'value_name': 'A'}],
        'seller_custom_field': 'legacy', 'variations': [{'attributes': [{'id': 'SELLER_SKU', 'value_name': 'B'}],
                                                       'seller_custom_field': 'legacy-variation'}]})
    assert skus == ['A', 'B'] and custom == ['legacy', 'legacy-variation']
    assert read_quote({'coverage': {'all_country': {'currency_id': 'BRL', 'list_cost': None}}})[0] is None
    assert read_quote({'coverage': {'all_country': {'currency_id': 'BRL', 'list_cost': 0}}})[0] == 0
