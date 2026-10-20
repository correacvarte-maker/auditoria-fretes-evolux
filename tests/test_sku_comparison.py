import json
from decimal import Decimal

from sqlalchemy import select
from test_app import app, client, login, post
from test_catalog import connect, Response
from models import AccountItem, Product, SkuQuote, ItemPrice


def add_price(db, item, price=Decimal(200)):
    db.add(ItemPrice(mlb=item.mlb,seller_id=item.seller_id,full=price,effective=price,promotional=None,
                     state='standard',note='Preço unitário da API',seen_run=item.seen_run,consulted_at=1))


def seed(app):
    connect(app)
    with app.extensions['db_factory'].begin() as db:
        db.add(AccountItem(mlb='MLB123456789', seller_id='456', title='Variações', status='active',
            listing_type='gold_pro', skus_json='["A", "B"]', custom_fields_json='[]',
            dimensions='SELLER_PACKAGE_WIDTH: 999 cm\nSELLER_PACKAGE_WEIGHT: 9999 g',
            shipping_mode='me2', logistic_type='cross_docking', free_shipping=1,
            price=200, quote=30, quote_note='Cotação atual', reference_note='none', seen_run='initial', synced_at=1))
        add_price(db, type('Item',(),{'mlb':'MLB123456789','seller_id':'456','seen_run':'initial'})())


def save(client, sku, width='20', weight='1.200'):
    return post(client, '/catalog/sku-measures', {'sku': sku, 'width': width, 'height': '10', 'length': '30', 'weight': weight})


def test_user_measures_required_validated_and_never_filled_from_api(app, client):
    login(client)
    seed(app)
    page = client.get('/catalog/measures').get_data(as_text=True)
    assert 'Minhas medidas por SKU' in page and 'Aguardando suas medidas' in page
    assert 'value="999"' not in page and 'value="9999"' not in page
    assert client.post('/catalog/sku-measures', data={'sku': 'A'}).status_code == 400
    assert save(client, 'OTHER').status_code == 400
    for value in ('0', '-1', 'NaN', '10001', '1.234'):
        assert save(client, 'A', width=value).status_code == 400
    assert save(client, 'A').status_code == 302
    with app.extensions['db_factory']() as db:
        p = db.scalar(select(Product).where(Product.sku == 'A'))
        assert (p.width, p.height, p.length, p.weight) == (Decimal(20), Decimal(10), Decimal(30), Decimal('1.2'))
        assert db.scalar(select(Product).where(Product.sku == 'B')) is None
    assert 'R$ 25,45' in client.get('/catalog/comparison?q=A').get_data(as_text=True)


def test_per_sku_quotes_for_variations_and_invalidate_on_edit(app, client, monkeypatch):
    login(client)
    seed(app)
    save(client, 'A')
    save(client, 'B', width='40', weight='2.500')
    scanned = False
    calls = []
    item = {'id': 'MLB123456789', 'seller_id': 456, 'title': 'Variações', 'price': 100,
            'currency_id': 'BRL', 'listing_type_id': 'gold_pro', 'status': 'active',
            'shipping': {'mode': 'me2', 'logistic_type': 'cross_docking', 'free_shipping': True},
            'variations': [{'attributes': [{'id': 'SELLER_SKU', 'value_name': sku}]} for sku in ('A', 'B')]}
    def get(url, params=None, **kwargs):
        nonlocal scanned
        if url.endswith('/items/search'):
            ids = [] if scanned else [item['id']]
            scanned = True
            return Response({'results': ids, 'scroll_id': 'scan', 'paging': {'total': 1}})
        if url.endswith('/items'):
            return Response([{'code': 200, 'body': item}])
        if url.endswith('/sale_price'):
            assert params == {'context':'channel_marketplace','quantity':1}
            return Response({'currency_id':'BRL','amount':200,'regular_amount':None})
        calls.append(params)
        cost = 30 if 'item_id' in params else 10 if params['dimensions'] == '10.00x20.00x30.00,1200' else 15
        return Response({'coverage': {'all_country': {'currency_id': 'BRL', 'list_cost': cost, 'billable_weight': 2000}}})
    monkeypatch.setattr('catalog_import.requests.get', get)
    assert post(client, '/catalog/import', {}).status_code == 302
    with app.extensions['db_factory']() as db:
        assert db.get(SkuQuote, ('MLB123456789', 'A')) is None
        assert db.get(SkuQuote, ('MLB123456789', 'B')) is None
        assert db.scalar(select(Product).where(Product.sku == 'B')).weight == Decimal('2.500')
    assert not any('dimensions' in c for c in calls)
    assert all(c['item_price'] == 200 and c['logistic_type'] == 'cross_docking' for c in calls)
    page_a = client.get('/catalog/comparison?q=A').get_data(as_text=True)
    assert 'R$ 30,00' in page_a and 'R$ 25,45' in page_a and 'R$ 4,55' in page_a
    page_b = client.get('/catalog/comparison?q=B').get_data(as_text=True)
    assert 'R$ 27,05' in page_b
    save(client, 'A', width='41')
    page_a = client.get('/catalog/comparison?q=A').get_data(as_text=True)
    assert 'R$ 27,05' in page_a and 'R$ 25,45' not in page_a
    assert 'R$ 27,05' in client.get('/catalog/comparison?q=B').get_data(as_text=True)


def test_current_cost_export_requires_login_and_preserves_zero(app, client):
    assert client.get('/catalog/costs.csv').status_code == 302
    login(client)
    seed(app)
    with app.extensions['db_factory'].begin() as db:
        item = db.get(AccountItem, 'MLB123456789')
        item.title, item.quote = '=unsafe()', Decimal(0)
    response = client.get('/catalog/costs.csv')
    assert response.status_code == 200 and 'attachment' in response.headers['Content-Disposition']
    text = response.get_data(as_text=True)
    assert "'=unsafe()" in text and ';0,00;' in text and 'A, B' in text
    with app.extensions['db_factory'].begin() as db:
        db.get(AccountItem, 'MLB123456789').quote = None
    text = client.get('/catalog/costs.csv').get_data(as_text=True)
    assert ';0,00;' not in text
