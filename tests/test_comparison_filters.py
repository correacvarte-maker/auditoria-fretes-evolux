from decimal import Decimal
from sqlalchemy import select
from test_app import app, client, login, post
from test_sku_comparison import seed
from models import AccountItem, Product


def test_filters_search_zero_missing_and_cent_accuracy(app, client):
    login(client)
    seed(app)
    with app.extensions['db_factory'].begin() as db:
        source = db.get(AccountItem, 'MLB123456789')
        source.skus_json = '["A"]'
        for suffix, sku, quote in [('1','B',Decimal('25.54')), ('2','C',Decimal('25.55')),
                                   ('3','D',None), ('4','E',Decimal(0)), ('5','F',Decimal('25.56'))]:
            values = {column.name: getattr(source,column.name) for column in AccountItem.__table__.columns}
            values.update(mlb='MLB12345678'+suffix, skus_json='["'+sku+'"]', quote=quote)
            db.add(AccountItem(**values))
        for sku in 'ABCDEF':
            db.add(Product(sku=sku,name=sku,width=120,height=10,length=10,weight=1))
    cases = {'more':['A','F'], 'less':['B','E'], 'correct':['C'], 'pending':['D']}
    for result, skus in cases.items():
        text = client.get('/catalog/comparison?result='+result).get_data(as_text=True)
        for sku in 'ABCDEF':
            assert (f'>{sku}<small>' in text) == (sku in skus)
    text = client.get('/catalog/comparison?result=more&q=F').get_data(as_text=True)
    assert '>F<small>' in text and '>A<small>' not in text and 'R$ 0,01' in text
    assert client.get('/catalog/comparison?result=wrong').status_code == 400


def test_filter_applied_before_pagination_and_kept_in_links(app, client):
    login(client)
    seed(app)
    with app.extensions['db_factory'].begin() as db:
        item = db.get(AccountItem,'MLB123456789')
        codes=[f'SKU-{n:03}' for n in range(101)]
        import json
        item.skus_json=json.dumps(codes)
        for sku in codes:
            db.add(Product(sku=sku,name=sku,width=120,height=10,length=10,weight=1))
    page1=client.get('/catalog/comparison?result=more&q=SKU-').get_data(as_text=True)
    assert 'result=more' in page1 and 'q=SKU-' in page1 and 'Página 1 de 2' in page1
    page2=client.get('/catalog/comparison?result=more&q=SKU-&page=2').get_data(as_text=True)
    assert '>SKU-100<small>' in page2 and '>SKU-000<small>' not in page2
