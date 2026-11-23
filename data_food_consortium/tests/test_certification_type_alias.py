"""
The DFC ontology spells the Certification class "Certfication", and Open Food Network
emits that spelling: the node must map to the Certification model like any other node,
not only be created through the organisation which links to it.
"""

from django.test import TestCase

from data_food_consortium.enums import ResourceImportSource
from data_food_consortium.models import Certification
from data_food_consortium.proxy.resource import ProxyRefreshParser

SOURCE = "https://openfoodnetwork.example"
CERTIFICATION_URL = f"{SOURCE}/api/dfc/enterprises/1#certification-4"
V2_CONTEXT = "https://w3id.org/dfc/ontology/context/context_2.0.0.json"


def document():
    """The certification node as Open Food Network serves it."""
    return {
        "@context": V2_CONTEXT,
        "@graph": [
            {
                "@id": CERTIFICATION_URL,
                "@type": "dfc-b:Certfication",
                "dfc-b:name": "Organic",
                "dfc-b:certiferReference": "Islands Organic Producers Association",
            },
        ],
    }


class TestCertificationTypeAlias(TestCase):
    def test_a_node_typed_dfccertfication_is_imported(self):
        ProxyRefreshParser(SOURCE, ResourceImportSource.COMMAND_LINE).parse(document())

        certification = Certification.objects.get()
        self.assertEqual(certification.proxy_of, CERTIFICATION_URL)
        self.assertEqual(
            certification.certification_reference,
            "Islands Organic Producers Association",
        )
