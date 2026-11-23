from django.conf import settings
from django.test import TestCase

from data_food_consortium.enums import ResourceImportSource
from data_food_consortium.models import Certification, Organization
from data_food_consortium.proxy.resource import ProxyRefreshParser

SOURCE = "https://data-server.example"
DOCUMENT_URL = f"{SOURCE}/organizations/1"
CERTIFICATION_URLS = [f"{SOURCE}/certifications/{n}" for n in (4, 5)]


def certified_organization(certified_by):
    """One enterprise declaring its certifications in the given JSON-LD form."""
    return {
        "@context": settings.LDP_RDF_CONTEXT,
        "@id": DOCUMENT_URL,
        "@type": "dfc-b:Organization",
        "dfc-b:name": "Farm",
        "dfc-b:isCertifiedBy": certified_by,
    }


class TestToManyRelations(TestCase):
    def parse(self, document):
        ProxyRefreshParser(SOURCE, ResourceImportSource.COMMAND_LINE).parse(document)

    def test_single_iri_is_imported_as_one_relation(self):
        """A relation can carry one IRI, without a container around it."""
        self.parse(certified_organization(CERTIFICATION_URLS[0]))

        self.assertEqual(
            list(Certification.objects.values_list("proxy_of", flat=True)),
            [CERTIFICATION_URLS[0]],
        )
        self.assertEqual(
            list(Organization.objects.get().certifications.all()),
            [Certification.objects.get()],
        )

    def test_several_iris_are_all_imported(self):
        """A relation can carry several IRIs as a JSON array, without a container."""
        self.parse(certified_organization(CERTIFICATION_URLS))

        self.assertEqual(
            sorted(Certification.objects.values_list("proxy_of", flat=True)),
            CERTIFICATION_URLS,
        )
        self.assertEqual(
            sorted(
                Organization.objects.get().certifications.values_list(
                    "proxy_of", flat=True
                )
            ),
            CERTIFICATION_URLS,
        )

    def test_container_form_is_unchanged(self):
        """The ldp:Container form, which DFC v1 relations use, still works."""
        container = {
            "@type": "ldp:Container",
            "ldp:contains": [{"@id": url} for url in CERTIFICATION_URLS],
        }
        self.parse(certified_organization(container))

        self.assertEqual(
            sorted(Certification.objects.values_list("proxy_of", flat=True)),
            CERTIFICATION_URLS,
        )

    def test_reimport_does_not_duplicate(self):
        document = certified_organization(CERTIFICATION_URLS)

        self.parse(document)
        self.parse(document)

        self.assertEqual(Certification.objects.count(), 2)
        self.assertEqual(Organization.objects.count(), 1)
