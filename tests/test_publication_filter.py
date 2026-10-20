from test_app import app, client, login
from test_sku_comparison import seed, add_price
from models import ItemPublication, AccountItem, Product


def test_type_filter_combines_with_cost_search_and_preserves_links(app, client):
    login(client)
    seed(app)
    with app.extensions['db_factory'].begin() as db:
        source=db.get(AccountItem,'MLB123456789')
        source.skus_json='["A"]'
        db.add(Product(sku='A',name='A',width=120,height=10,length=10,weight=1))
        for n, kind in enumerate((1,0,None)):
            mlb='MLB12345678'+str(n)
            values={column.name:getattr(source,column.name) for column in AccountItem.__table__.columns}
            values.update(mlb=mlb)
            item=AccountItem(**values)
            db.add(item)
            add_price(db,item)
            db.add(ItemPublication(mlb=mlb,seller_id='456',catalog_listing=kind,seen_run='initial'))
    for kind, mlb in [('catalog','MLB123456780'),('traditional','MLB123456781'),('unknown','MLB123456782')]:
        page=client.get('/catalog/comparison?result=more&publication='+kind+'&q=A').get_data(as_text=True)
        assert mlb in page
        assert 'publication='+kind in page
        for other in ('MLB123456780','MLB123456781','MLB123456782'):
            if other != mlb:
                assert other not in page
    assert 'MLB123456780' not in client.get('/catalog/comparison?result=less&publication=catalog').get_data(as_text=True)
    assert client.get('/catalog/comparison?publication=wrong').status_code==400


def test_old_or_foreign_classification_is_not_assumed_traditional(app, client):
    login(client)
    seed(app)
    with app.extensions['db_factory'].begin() as db:
        db.add(ItemPublication(mlb='MLB123456789',seller_id='456',catalog_listing=1,seen_run='old'))
    assert 'MLB123456789' not in client.get('/catalog/comparison?publication=catalog').get_data(as_text=True)
    assert 'MLB123456789' in client.get('/catalog/comparison?publication=unknown').get_data(as_text=True)
