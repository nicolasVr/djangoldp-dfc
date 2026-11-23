"""
End-to-end import of an Open Food Network document with its certifications.

The hub reads two shapes from OFN:

* the DFC v2 profile (/api/dfc/organizations/{id}, context 2.0.0), where each
  certification is a graph node linked by an @id;
* the DFC v1 profile (/api/dfc/enterprises, context 1.16.0), where dfc-b:isCertifiedBy
  is a literal holding the IRI reference of the certification, relative to the document.

In both shapes the certification is proxied as <document URL>#certification-n.
"""

from unittest import mock

import requests
from django.test import TestCase

from data_food_consortium.enums import ResourceImportSource
from data_food_consortium.models import Certification, Organization
from data_food_consortium.proxy.resource import ResourceServerClient

SOURCE = "https://openfoodnetwork.example"
V1_CONTEXT = "https://w3id.org/dfc/ontology/context/context_1.16.0.json"
V2_CONTEXT = "https://w3id.org/dfc/ontology/context/context_2.0.0.json"


def linked(value):
    """JSON-LD compacts a one-element array to a single value."""
    return value[0] if len(value) == 1 else value


def v1_document(certification_iris, document_url):
    """The enterprises index: the enterprise alone, its certifications named by literal."""
    return {
        "@context": V1_CONTEXT,
        "@graph": [
            {
                "@id": f"{document_url}/1",
                "@type": "dfc-b:Enterprise",
                "dfc-b:name": "Farm",
                "dfc-b:isCertifiedBy": linked(certification_iris),
            }
        ],
    }


def v2_document(certification_iris, document_url):
    """The organization: its certification nodes, linked by relative IRI."""
    return {
        "@context": V2_CONTEXT,
        "@graph": [
            {
                "@id": document_url,
                "@type": "dfc-b:Organization",
                "dfc-b:name": "Farm",
                "dfc-b:isCertifiedBy": linked(certification_iris),
            },
            *[
                {
                    "@id": iri,
                    "@type": "dfc-b:Certfication",
                    "dfc-b:name": "Organic",
                }
                for iri in certification_iris
            ],
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


def import_document(path, document):
    """Read the document as the hub does: fetched from its URL, with a token."""
    with mock.patch(
        "data_food_consortium.proxy.resource.requests.get"
    ) as get, mock.patch(
        "data_food_consortium.proxy.resource.KeycloakClient"
    ) as keycloak:
        keycloak.return_value.get_access_token.return_value = "token"
        # Discovery endpoint, then the document.
        get.side_effect = [response(404), response(200, document)]
        client = ResourceServerClient(SOURCE, ResourceImportSource.COMMAND_LINE)
        client.scope_config = {"ReadEnterprise": path}
        client.request_scope("ReadEnterprise")


class TestOfnCertifications(TestCase):
    def assert_imported(self, organization_url, document_url, certification_iris):
        expected = [f"{document_url}{iri}" for iri in certification_iris]
        self.assertEqual(
            list(Organization.objects.values_list("proxy_of", flat=True)),
            [organization_url],
        )
        self.assertEqual(
            sorted(Certification.objects.values_list("proxy_of", flat=True)), expected
        )
        self.assertEqual(
            sorted(
                Organization.objects.get().certifications.values_list(
                    "proxy_of", flat=True
                )
            ),
            expected,
        )

    def test_v2_one_certification(self):
        document_url = f"{SOURCE}/api/dfc/organizations/1"
        import_document(
            "api/dfc/organizations/1", v2_document(["#certification-1"], document_url)
        )

        self.assert_imported(document_url, document_url, ["#certification-1"])

    def test_v2_two_certifications(self):
        document_url = f"{SOURCE}/api/dfc/organizations/1"
        import_document(
            "api/dfc/organizations/1",
            v2_document(["#certification-1", "#certification-2"], document_url),
        )

        self.assert_imported(
            document_url, document_url, ["#certification-1", "#certification-2"]
        )

    def test_v1_one_literal_certification(self):
        document_url = f"{SOURCE}/api/dfc/enterprises"
        import_document(
            "api/dfc/enterprises", v1_document(["#certification-4"], document_url)
        )

        self.assert_imported(
            f"{document_url}/1", document_url, ["#certification-4"]
        )

    def test_v1_two_literal_certifications(self):
        document_url = f"{SOURCE}/api/dfc/enterprises"
        import_document(
            "api/dfc/enterprises",
            v1_document(["#certification-4", "#certification-5"], document_url),
        )

        self.assert_imported(
            f"{document_url}/1", document_url, ["#certification-4", "#certification-5"]
        )
