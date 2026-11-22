from django.conf import settings
from django.test import TestCase

from data_food_consortium.enums import ResourceImportSource
from data_food_consortium.models import Offer, Price, SuppliedProduct
from data_food_consortium.proxy.resource import ProxyRefreshParser

SOURCE = "https://data-server.example"


def catalogue(context, prices=None):
    """An offer with its price embedded as a blank node, the way OFN and others emit it."""
    offer = {
        "@id": f"{SOURCE}/offers/1",
        "@type": "dfc-b:Offer",
        "dfc-b:hasPrice": prices
        if prices is not None
        else {"@type": "dfc-b:Price", "dfc-b:value": 19.99, "dfc-b:hasUnit": "dfc-m:Euro"},
    }
    product = {
        "@id": f"{SOURCE}/supplied_products/1",
        "@type": "dfc-b:SuppliedProduct",
        "dfc-b:name": "Pesto",
        # Unknown type as a blank node: must stay harmless.
        "dfc-b:hasQuantity": {"@type": "dfc-b:QuantitativeValue", "dfc-b:value": 1.0},
    }
    return {"@context": context, "@graph": [product, offer]}


def parse(document):
    ProxyRefreshParser(SOURCE, ResourceImportSource.COMMAND_LINE).parse(document)


class TestNestedBlankNodes(TestCase):
    def assert_price_imported(self, context):
        parse(catalogue(context))

        self.assertEqual(SuppliedProduct.objects.count(), 1)
        price = Price.objects.get()
        self.assertEqual(price.proxy_of, f"{SOURCE}/offers/1#hasPrice")
        self.assertAlmostEqual(price.value, 19.99)
        offer = Offer.objects.get(proxy_of=f"{SOURCE}/offers/1")
        self.assertEqual(offer.offered_for, price)

    def test_embedded_price_v1(self):
        self.assert_price_imported(settings.LDP_RDF_CONTEXT_V1)

    def test_embedded_price_v2(self):
        self.assert_price_imported(settings.LDP_RDF_CONTEXT)

    def test_reimport_does_not_duplicate(self):
        parse(catalogue(settings.LDP_RDF_CONTEXT))
        parse(catalogue(settings.LDP_RDF_CONTEXT))

        self.assertEqual(Price.objects.count(), 1)

    def test_several_blank_nodes_under_one_predicate_get_distinct_names(self):
        prices = [
            {"@type": "dfc-b:Price", "dfc-b:value": 1.0},
            {"@type": "dfc-b:Price", "dfc-b:value": 2.0},
        ]
        parse(catalogue(settings.LDP_RDF_CONTEXT, prices))

        self.assertEqual(
            sorted(Price.objects.values_list("proxy_of", flat=True)),
            [f"{SOURCE}/offers/1#hasPrice-1", f"{SOURCE}/offers/1#hasPrice-2"],
        )
