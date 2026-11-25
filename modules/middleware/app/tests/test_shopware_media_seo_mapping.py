from services.shopware_mapping_engine import build_shopware_product_payload


def test_build_shopware_payload_has_seo_and_prices_and_cover():
    payload = build_shopware_product_payload({
        'sku':'SKU1','title':'Produkt','seo_title':'SEO Titel','meta_description':'Meta Beschreibung','keywords':['a','b'],
        'purchase_price':10.0,'sale_price':19.99,'tax_rate':19.0,'stock_quantity':5
    }, media_ids=['m1','m2'], cover_id='m1')
    assert payload['metaTitle'] == 'SEO Titel'
    assert payload['metaDescription'] == 'Meta Beschreibung'
    assert payload['keywords'] == 'a, b'
    assert payload['purchasePrices'][0]['net'] == 10.0
    assert payload['price'][0]['gross'] == 19.99
    assert payload['media'][0]['mediaId'] == 'm1'
    assert payload['coverId'] == 'm1'
