import json
import time
from decimal import Decimal

import requests
from sqlalchemy import select

from test_app import app, client, login, post, catalog
from models import AccountItem, CatalogImport, Connection, Product, ItemPublication
from catalog_import import item_fields, read_quote, package_dimensions, safe_api_error


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
            'currency_id': 'BRL', 'price': 100, 'listing_type_id': 'gold_pro', 'condition': 'new', 'catalog_listing': True,
            'shipping': {'mode': 'me2', 'logistic_type': 'drop_off', 'free_shipping': True},
            'attributes': [{'id': 'SELLER_SKU', 'value_name': 'EV-001'},
                           {'id': 'SELLER_PACKAGE_WEIGHT', 'value_name': '3000 g'}]},
        'MLB222222222': {'id': 'MLB222222222', 'seller_id': 456, 'title': 'Zero quote', 'status': 'paused',
            'shipping': {'mode': 'me2'}, 'attributes': [], 'seller_custom_field': 'NOT-A-SKU', 'catalog_listing': False, 'catalog_product_id': 'MLB123'},
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
        assert record.quote == Decimal('15') and record.expected_quote is None
        assert db.get(Product, 1).weight == Decimal('1.2')
        assert db.get(AccountItem, 'MLB222222222').quote == 0
        assert json.loads(db.get(AccountItem, 'MLB222222222').skus_json) == []
        assert db.get(AccountItem, 'MLB333333333').quote is None
        assert db.get(ItemPublication, 'MLB123456789').catalog_listing == 1
        assert db.get(ItemPublication, 'MLB222222222').catalog_listing == 0
        assert db.get(ItemPublication, 'MLB333333333').catalog_listing is None
        assert db.get(ItemPublication, 'MLB123456789').seen_run == record.seen_run
    assert pages[1]['scroll_id'] == 'cursor' and 'offset' not in pages[1]
    assert not any('dimensions' in x for x in quote_calls)
    page = client.get('/catalog').get_data(as_text=True)
    assert 'R$ 25,45' in page and 'NOT-A-SKU' in page and 'SKU não informado' in page
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


def test_shipping_units_and_safe_error_metadata():
    dimensions = package_dimensions({'attributes': [
        {'id': 'SELLER_PACKAGE_HEIGHT', 'value_name': '1,4 m'},
        {'id': 'SELLER_PACKAGE_WIDTH', 'value_name': '15 cm'},
        {'id': 'SELLER_PACKAGE_LENGTH', 'value_name': '30 mm'},
        {'id': 'SELLER_PACKAGE_WEIGHT', 'value_name': '2,05 kg'}]})
    assert dimensions == '140.0x15x3.0,2050'
    assert package_dimensions({'attributes': []}) is None
    error = requests.HTTPError(response=Response({'code': 'PA_UNAUTHORIZED_RESULT_FROM_POLICIES',
                                                  'message': 'secret must not be exposed'}, 403))
    assert safe_api_error(error) == 'PA_UNAUTHORIZED_RESULT_FROM_POLICIES'
    assert safe_api_error(requests.HTTPError(response=Response({'code': 'private-value'}, 403))) == 'sem código público de diagnóstico'


def test_contextual_quote_after_item_quote_rejection(app, client, monkeypatch):
    login(client)
    connect(app)
    scanned = False
    item = {'id': 'MLB123456789', 'seller_id': 456, 'title': 'Item', 'price': 100,
            'currency_id': 'BRL', 'listing_type_id': 'gold_special',
            'shipping': {'mode': 'me2', 'logistic_type': 'cross_docking', 'free_shipping': True},
            'attributes': [{'id': 'SELLER_PACKAGE_' + key, 'value_name': value}
                for key, value in [('HEIGHT', '10 cm'), ('WIDTH', '20 cm'), ('LENGTH', '30 cm'), ('WEIGHT', '1200 g')]]}
    def get(url, params=None, **kwargs):
        nonlocal scanned
        if url.endswith('/items/search'):
            result = [] if scanned else [item['id']]
            scanned = True
            return Response({'results': result, 'scroll_id': 'scan', 'paging': {'total': 1}})
        if url.endswith('/items'):
            return Response([{'code': 200, 'body': item}])
        if 'item_id' in params:
            return Response({'code': 'bad_request'}, 400)
        assert params['dimensions'] == '10x20x30,1200'
        assert params['free_shipping'] == 'True' and params['item_price'] == 100
        return Response({'coverage': {'all_country': {'currency_id': 'BRL', 'list_cost': 19.99}}})
    monkeypatch.setattr('catalog_import.requests.get', get)
    post(client, '/catalog/import', {})
    with app.extensions['db_factory']() as db:
        assert db.get(AccountItem, item['id']).quote == Decimal('19.99')
