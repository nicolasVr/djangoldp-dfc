from unittest import mock

import requests
from django.conf import settings
from django.test import TestCase

from data_food_consortium.enums import ResourceImportSource
from data_food_consortium.models import Certification, Organization
from data_food_consortium.proxy.errors import ResourceAnnexationError
from data_food_consortium.proxy.resource import ProxyRefreshParser, ResourceServerClient

SOURCE = "https://data-server.example"
DOCUMENT_URL = f"{SOURCE}/organizations/1"
CERTIFICATION_IRI = "#certification-4"
CERTIFICATION_URL = f"{DOCUMENT_URL}{CERTIFICATION_IRI}"


def certified_organization(organization_url, certification_iri, context=None):
    """
    One enterprise with its certification, as Open Food Network publishes them: the
    certification is identified by a relative IRI (e.g. "#certification-4").
    """
    return {
        "@context": context if context is not None else settings.LDP_RDF_CONTEXT,
        "@graph": [
            {
                "@id": organization_url,
                "@type": "dfc-b:Organization",
                "dfc-b:name": "Farm",
                "dfc-b:isCertifiedBy": {
                    "@type": "ldp:Container",
                    "ldp:contains": [{"@id": certification_iri}],
                },
            },
            {
                "@id": certification_iri,
                "@type": "dfc-b:Certification",
                "dfc-b:certificationScore": "3",
            },
        ],
    }


def response(status, document=None):
    resp = mock.Mock()
    resp.status_code = status
    resp.content = b"{}"
    resp.json.return_value = document or {}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def refresh(path, http_responses):
    """Run the real cache refresh of one endpoint against a mocked data-server."""
    with mock.patch(
        "data_food_consortium.proxy.resource.requests.get"
    ) as get, mock.patch(
        "data_food_consortium.proxy.resource.KeycloakClient"
    ) as keycloak:
        keycloak.return_value.get_access_token.return_value = "token"
        get.side_effect = [response(404)] + http_responses  # discovery, then the pages
        client = ResourceServerClient(SOURCE, ResourceImportSource.COMMAND_LINE)
        client.scope_config = {"ReadEnterprise": path}
        client.request_scope("ReadEnterprise")


class TestRelativeIris(TestCase):
    def test_relative_iri_resolves_against_fetched_url(self):
        """A "#certification-4" IRI is imported as <fetched URL>#certification-4."""
        document = certified_organization(DOCUMENT_URL, CERTIFICATION_IRI)

        ProxyRefreshParser(SOURCE, ResourceImportSource.COMMAND_LINE).parse(
            document, base_iri=DOCUMENT_URL
        )

        self.assertEqual(
            list(Organization.objects.values_list("proxy_of", flat=True)),
            [DOCUMENT_URL],
        )
        certification = Certification.objects.get()
        self.assertEqual(certification.proxy_of, CERTIFICATION_URL)
        # The enterprise is linked to the certification it declares.
        self.assertEqual(
            list(Organization.objects.get().certifications.all()), [certification]
        )

    def test_relative_iri_without_base_is_still_refused(self):
        """Unchanged behaviour when no base IRI is known: the IRI stays relative."""
        document = certified_organization(DOCUMENT_URL, CERTIFICATION_IRI)

        with self.assertRaises(ResourceAnnexationError):
            ProxyRefreshParser(SOURCE, ResourceImportSource.COMMAND_LINE).parse(document)

        self.assertEqual(Certification.objects.count(), 0)

    def test_iri_resolved_outside_the_domain_is_refused(self):
        """
        A network-path reference ("//other.example/x") is the only way a relative IRI can
        designate another domain; it resolves there and the annexation check rejects it.
        """
        document = certified_organization(
            DOCUMENT_URL, "//other.example/certification-4"
        )

        with self.assertRaises(ResourceAnnexationError):
            ProxyRefreshParser(SOURCE, ResourceImportSource.COMMAND_LINE).parse(
                document, base_iri=DOCUMENT_URL
            )

        self.assertEqual(Certification.objects.count(), 0)

    def test_document_base_takes_precedence(self):
        """A document declaring its own @base keeps it: the JSON-LD rule."""
        declared_base = f"{SOURCE}/api/"
        context = [settings.LDP_RDF_CONTEXT, {"@base": declared_base}]
        document = certified_organization(DOCUMENT_URL, CERTIFICATION_IRI, context)

        ProxyRefreshParser(SOURCE, ResourceImportSource.COMMAND_LINE).parse(
            document, base_iri=DOCUMENT_URL
        )

        self.assertEqual(
            Certification.objects.get().proxy_of, f"{declared_base}{CERTIFICATION_IRI}"
        )

    def test_refresh_resolves_against_the_url_of_each_page(self):
        """Each fetched page, including "next" pages, is its own base IRI."""
        first_page_url = DOCUMENT_URL
        second_page_url = f"{DOCUMENT_URL}?page=2"
        first_page = certified_organization(first_page_url, CERTIFICATION_IRI)
        first_page["next"] = second_page_url
        second_page = certified_organization(second_page_url, "#certification-5")

        refresh(
            "organizations/1",
            [
                response(200, first_page),
                response(200, second_page),
            ],
        )

        self.assertEqual(
            sorted(Certification.objects.values_list("proxy_of", flat=True)),
            [CERTIFICATION_URL, f"{second_page_url}#certification-5"],
        )
