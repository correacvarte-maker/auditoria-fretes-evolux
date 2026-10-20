from decimal import Decimal
from html.parser import HTMLParser
from types import SimpleNamespace

from test_app import app, client, login, post
from test_sku_comparison import seed, save
from test_catalog import Response
from models import AccountItem, ItemPrice
from listing_prices import read_prices, price_columns
from freight_table import calculate


def test_price_parser_promotion_standard_missing_and_invalid():
    result=read_prices({'currency_id':'BRL','amount':199.99,'regular_amount':299.90})
    assert (result['full'],result['promotional'],result['effective'],result['state']) == (
        Decimal('299.90'),Decimal('199.99'),Decimal('199.99'),'promotion')
    result=read_prices({'currency_id':'BRL','amount':100,'regular_amount':None})
    assert result['full']==100 and result['promotional'] is None and result['state']=='standard'
    for bad in (None,[],{'currency_id':'ARS','amount':10}, {'currency_id':'BRL','amount':'NaN'},
                {'currency_id':'BRL','amount':0}, {'currency_id':'BRL','amount':'10.001'},
                {'currency_id':'BRL','amount':10,'metadata':{'is_price_per_quantity':True}}):
        assert read_prices(bad)['effective'] is None
    for regular in ({}, {'regular_amount':50}):
        result=read_prices({'currency_id':'BRL','amount':100,**regular})
        assert result['effective']==100 and result['full'] is None and result['state']=='partial'


def test_all_price_thresholds_and_half_price_cap():
    product=SimpleNamespace(width=Decimal(120),height=Decimal(10),length=Decimal(10),weight=Decimal(1))
    for price,expected in [('18.99','6.25'),('19','7.45'),('48.99','7.45'),('49','8.75'),
                           ('78.99','8.75'),('79','15.05'),('99.99','15.05'),('100','17.65'),
                           ('119.99','17.65'),('120','19.85'),('149.99','19.85'),('150','22.25'),
                           ('199.99','22.25'),('200','25.55')]:
        assert calculate(product,Decimal(price))['expected']==Decimal(expected)
    assert calculate(product,Decimal('10.01'))['expected']==Decimal('5.00')
    assert calculate(product)['expected'] is None


def test_snapshot_requires_same_seller_and_run():
    item=SimpleNamespace(mlb='MLB1',seller_id='1',seen_run='new')
    snapshot=SimpleNamespace(seller_id='1',seen_run='old')
    assert price_columns(item,snapshot)['effective_price'] is None
    snapshot.seen_run,snapshot.seller_id='new','other'
    assert price_columns(item,snapshot)['effective_price'] is None


class TableCells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows=[]
        self.current=None
    def handle_starttag(self,tag,attrs):
        if tag=='tr': self.current=0
        elif tag in ('td','th') and self.current is not None: self.current+=1
    def handle_endtag(self,tag):
        if tag=='tr' and self.current is not None:
            self.rows.append(self.current)
            self.current=None


def test_import_current_promotion_drives_band_and_failure_clears_old_price(app,client,monkeypatch):
    login(client)
    seed(app)
    save(client,'A',width='120',weight='1')  # height 10, length 30 -> 6 kg
    with app.extensions['db_factory'].begin() as db:
        from sqlalchemy import select
        from models import Product
        db.get(AccountItem,'MLB123456789').skus_json='["A"]'
        db.scalar(select(Product).where(Product.sku=='A')).length=10  # 2 kg
    live_price={'currency_id':'BRL','amount':199.99,'regular_amount':299.90}
    fail=False
    scan=0
    quote_prices=[]
    item={'id':'MLB123456789','seller_id':456,'title':'Oferta','currency_id':'BRL','price':999,
          'status':'active','listing_type_id':'gold_special','catalog_listing':False,
          'attributes':[{'id':'SELLER_SKU','value_name':'A'}],
          'shipping':{'mode':'me2','logistic_type':'cross_docking','free_shipping':True}}
    def get(url,params=None,**kwargs):
        nonlocal scan
        if url.endswith('/items/search'):
            scan+=1
            return Response({'results':[item['id']] if scan%2 else [],'scroll_id':'scan','paging':{'total':1}})
        if url.endswith('/items'): return Response([{'code':200,'body':item}])
        if url.endswith('/sale_price'):
            assert params=={'context':'channel_marketplace','quantity':1}
            return Response({'error':'forbidden'},403) if fail else Response(live_price)
        quote_prices.append(params['item_price'])
        return Response({'coverage':{'all_country':{'currency_id':'BRL','list_cost':30}}})
    monkeypatch.setattr('catalog_import.requests.get',get)
    post(client,'/catalog/import',{})
    with app.extensions['db_factory']() as db:
        price=db.get(ItemPrice,item['id'])
        assert price.full==Decimal('299.90') and price.promotional==Decimal('199.99')
        assert db.get(AccountItem,item['id']).price==Decimal('199.99')
    assert quote_prices==[Decimal('199.99')]
    page=client.get('/catalog/comparison?q=A').get_data(as_text=True)
    assert 'R$ 22,25' in page and 'R$ 7,75' in page and 'Faixa: R$ 150 a R$ 199,99' in page
    assert 'R$ 299,90' in page and 'R$ 199,99' in page
    cells=TableCells(); cells.feed(page)
    assert cells.rows[:2]==[10,10]
    catalog=client.get('/catalog?q=A').get_data(as_text=True)
    cells=TableCells(); cells.feed(catalog)
    assert cells.rows[:2]==[9,9] and 'R$ 22,25' in catalog
    live_price['amount']=200
    post(client,'/catalog/import',{})
    assert 'R$ 25,55' in client.get('/catalog/comparison?q=A').get_data(as_text=True)
    fail=True
    post(client,'/catalog/import',{})
    with app.extensions['db_factory']() as db:
        price=db.get(ItemPrice,item['id'])
        assert price.promotional is None and price.effective is None and price.state=='unavailable'
    page=client.get('/catalog/comparison?q=A').get_data(as_text=True)
    assert 'Preço vigente indisponível' in page and 'R$ 25,55' not in page
    assert 'Sem promoção' not in page
