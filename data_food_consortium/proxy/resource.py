import logging
import re
import urllib
import uuid

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from djangoldp import fields
from djangoldp.models import Model
from rdflib import BNode, Graph, URIRef
from rdflib.exceptions import ParserError

from data_food_consortium.enums import (
    PermissioningScope,
    ResourceImportFailure,
    ResourceImportSource,
)
from data_food_consortium.models import EndpointImportState, ResourceImportRecord
from data_food_consortium.models_common import DataServer
from data_food_consortium.proxy.errors import ResourceAnnexationError
from data_food_consortium.proxy.keycloak import (
    KeycloakAuthenticationException,
    KeycloakClient,
)
from data_food_consortium.utils import get_serializer_class

RDF_TYPE_PREDICATE = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")


logger = logging.getLogger(__name__)


class ProxyRefreshParser:
    """
    Responsible for parsing an RDF-graph containing data to be proxied by a DjangoLDP application.
    """

    data_server_source = None
    data_server_source_urlid = None
    source = None
    import_started_at = None
    imported_models = None
    imported_subjects = None
    deleted_subjects = None
    data_batches = None
    unknown_model_types = None

    _dfc_v1_container_properties = [  # noqa: RUF012
        "dfc-b:manages",
        "dfc-b:supplies",
        "dfc-b:hasAddress",
        "dfc-b:hasSocialMedia",
        "dfc-b:referencedBy",
        "dfc-b:offeredThrough",
        "dfc-b:affiliatedTo",
        "dfc-b:hasReference",
        "dfc-b:defines",
        "dfc-b:offers",
        "dfc-b:priceOf",
        "dfc-b:mainContactOf",
        "dfc-b:coordinatedBy",
        "dfc-b:hasObject",
        "dfc-b:hosts",
        "cqcm:services",
        "cqcm:suppliers",
        "cqcm:shippingOptions",
        "dfc-b:hasOption",
        "dfc-b:deliveries",
        "dfc-b:collections",
    ]

    def __init__(self, data_server_source, source: ResourceImportSource, endpoint=None):
        """
        :param data_server_source: any IRI of the data-server; only its domain is kept
        :param source: how the import was triggered
        :param endpoint: the endpoint being read, when this is a cache refresh of one
            endpoint. Required for DFC_PROXY_CLEANUP = "endpoint".
        """
        self.endpoint = endpoint
        dss = urllib.parse.urlparse(data_server_source)
        self.data_server_source_urlid = f"{dss.scheme}://{dss.netloc}"
        self.data_server_source = Model.get_or_create(
            DataServer, self.data_server_source_urlid
        )
        self.source = source
        self.import_started_at = timezone.now()
        self.imported_models = set()
        self.imported_subjects = []
        self.deleted_subjects = []
        self.data_batches = []
        self.unknown_model_types = set()

    def _cache_data_batch(self, data):
        self.data_batches.append(data)

    def get_serializer_class(self, model, depth=2, extra_fields=None):
        return get_serializer_class(model, depth, extra_fields)

    def resolve_model_for_subject(self, graph, subject):
        """Discovers a DjangoLDP model for a given RDF subject."""
        model_types = []
        for type_uri in graph.objects(subject, RDF_TYPE_PREDICATE):
            try:
                model_types += [str(type_uri), graph.qname(type_uri)]
            except (ValueError, KeyError) as e:
                logger.warning(
                    f"Unable to use compacted form of {type_uri}. RDFLib error: {e}"
                )

        for type_uri in model_types:
            resolved_model = Model.get_subclass_with_rdf_type(type_uri)
            if resolved_model is not None:
                return resolved_model, type_uri

        # Unable to resolve model, log error if necessary.
        if len(
            set(model_types).difference(
                {
                    "http://www.w3.org/ns/ldp#Container",
                    "ldp:Container",
                    "ns1:Container",
                    "ns2:Container",
                    *self.unknown_model_types,
                }
            )
        ):
            logger.warning(
                f"Unable to resolve a model with configured type_uri {model_types}."
                "If this is a container or a sequence, it is not a problem."
            )
            self.unknown_model_types = self.unknown_model_types.union(set(model_types))

        return None, None

    def _has_resolvable_type(self, graph, subject):
        """True when one of the subject's types maps to a DjangoLDP model, without logging."""
        for type_uri in graph.objects(subject, RDF_TYPE_PREDICATE):
            candidates = [str(type_uri)]
            try:
                candidates.append(graph.qname(type_uri))
            except (ValueError, KeyError):
                pass
            if any(Model.get_subclass_with_rdf_type(c) is not None for c in candidates):
                return True
        return False

    def skolemize_nested_blank_nodes(self, graph):
        """
        Give a stable IRI to blank nodes that describe a resource we store as a model.

        Data servers commonly embed some resources without an @id, for instance a Price
        inside an Offer: {"dfc-b:hasPrice": {"@type": "dfc-b:Price", "dfc-b:value": 10}}.
        That is valid JSON-LD, but a blank node has no IRI within the data server's domain,
        so it cannot become a proxied instance, and a single one used to abort the whole
        import.

        Each such node is renamed <parent IRI>#<predicate local name>, with a numeric
        suffix when the same parent holds several of them under the same predicate. The
        name only depends on the parent and on the node's content, so it is the same at
        every import: re-importing updates the instance instead of duplicating it. It lies
        within the parent's domain, so the usual annexation check still applies.

        Blank nodes of unknown types (QuantitativeValue, PhoneNumber...) and blank nodes
        with no IRI parent are left untouched, as before. Nested blank nodes are handled
        from the outside in.
        """
        changed = True
        while changed:
            changed = False
            names = {}
            for node in sorted({s for s in graph.subjects() if isinstance(s, BNode)}, key=str):
                if not self._has_resolvable_type(graph, node):
                    continue
                parents = sorted(
                    (
                        (parent, predicate)
                        for parent, predicate in graph.subject_predicates(node)
                        if isinstance(parent, URIRef)
                    ),
                    key=lambda pair: (str(pair[0]), str(pair[1])),
                )
                if not parents:
                    continue
                parent, predicate = parents[0]
                local_name = re.split(r"[#/]", str(predicate))[-1] or "node"
                content = sorted((str(p), str(o)) for p, o in graph.predicate_objects(node))
                names.setdefault(f"{parent}#{local_name}", []).append((content, node))

            for base, nodes in names.items():
                nodes.sort(key=lambda item: item[0])
                for index, (_content, node) in enumerate(nodes):
                    iri = URIRef(base if len(nodes) == 1 else f"{base}-{index + 1}")
                    for predicate, obj in list(graph.predicate_objects(node)):
                        graph.remove((node, predicate, obj))
                        graph.add((iri, predicate, obj))
                    for subject, predicate in list(graph.subject_predicates(node)):
                        graph.remove((subject, predicate, node))
                        graph.add((subject, predicate, iri))
                    changed = True

    def reformat_list_to_container(self, key, jsonld_data):
        if isinstance(jsonld_data, str):
            container = [{"@id": jsonld_data}]
        elif isinstance(jsonld_data, list):
            container = [
                {"@id": x}
                for x in jsonld_data
                if type(x) is not dict or ("@id" not in x and "@context" not in x)
            ]
        elif isinstance(jsonld_data, dict):
            return jsonld_data
        else:
            logger.error(
                f"Unexpected value for property {key} ({type(jsonld_data)}). {jsonld_data}"
            )
            return jsonld_data
        return {
            "@id": f"_:generated-container-{uuid.uuid4()}",
            "@type": "ldp:Container",
            "ldp:contains": container,
        }

    def transform_dfc_v1(self, jsonld_data):
        """
        DFC v1 does not require data-servers to send us their data in containers,
        which causes problems with RDFLib's graph parser and DjangoLDP's serializer.
        Our workaround is to transform the lists of string IDs we receive into proper
        containers.

        This is achieved with implicit knowledge of the DFC fields, and a recursive search of
        the data received.
        """
        if isinstance(jsonld_data, list):
            for i, ele in enumerate(jsonld_data):
                jsonld_data[i] = self.transform_dfc_v1(ele)
        elif isinstance(jsonld_data, dict):
            for key in jsonld_data:
                if key in self._dfc_v1_container_properties:
                    jsonld_data[key] = self.reformat_list_to_container(
                        key, jsonld_data[key]
                    )
                else:
                    jsonld_data[key] = self.transform_dfc_v1(jsonld_data[key])

        return jsonld_data

    def _serialize_data_server_source(self):
        return {"@id": self.data_server_source_urlid}

    def _serialize_related_instance(self, field, instance, related_instance_urlid):
        related_instance = field.related_model.objects.filter(
            proxy_of=related_instance_urlid
        ).first()

        if related_instance is None:
            related_instance = field.related_model.objects.create(
                proxy_of=related_instance_urlid
            )

        if field.one_to_many or field.many_to_many:
            getattr(instance, field.name).add(related_instance)

        return {
            "@id": related_instance.urlid,
            "data_server_source": self._serialize_data_server_source(),
            "proxy_of": related_instance_urlid,
            "allow_create_backlink": False,
        }

    def _recursively_force_data_server_source(self, value):
        if isinstance(value, dict):
            if (
                "proxy_of" in value
                and not self.data_server_source.resource_within_domain(
                    value["proxy_of"]
                )
            ):
                raise ResourceAnnexationError()
            if "data_server_source" not in value:
                value["data_server_source"] = self._serialize_data_server_source()
            for key in value:
                if isinstance(value[key], (dict, list)) and key != "data_server_source":
                    value[key] = self._recursively_force_data_server_source(value[key])
        elif isinstance(value, list):
            for i in range(len(value)):
                if isinstance(value[i], (dict, list)):
                    value[i] = self._recursively_force_data_server_source(value[i])
        return value

    def _do_parse(self, jsonld_data, base_iri):
        jsonld_data = self.transform_dfc_v1(jsonld_data)
        self._cache_data_batch(jsonld_data)

        graph = Graph()
        # RDFLib resolves relative IRIs against the base IRI, or against the current
        # directory when none is given: only pass a base when we have one.
        parse_kwargs = {"base": base_iri} if base_iri else {}
        graph.parse(data=jsonld_data, format="json-ld", **parse_kwargs)
        self.skolemize_nested_blank_nodes(graph)
        subjects = set(graph.subjects())

        for subject in subjects:
            resolved_model, resolved_type_uri = self.resolve_model_for_subject(
                graph, subject
            )
            if resolved_model is None:
                continue

            # Parsing the graph into serializable data for our model
            resource_data = {
                "@type": resolved_type_uri,
                "data_server_source": self._serialize_data_server_source(),
                "proxy_of": str(subject),
                "allow_create_backlink": False,
            }
            source_data = {}
            for pred, obj in graph.predicate_objects(subject):
                if pred == RDF_TYPE_PREDICATE:
                    continue

                # Map RDF values to Python values.
                # Containers, serialize strings directly to be reformatted later.
                if (obj, URIRef("ldp:contains"), None) in graph:
                    value = [str(v) for v in graph.objects(obj, URIRef("ldp:contains"))]
                elif (
                    obj,
                    URIRef("http://www.w3.org/ns/ldp#contains"),
                    None,
                ) in graph:
                    value = [
                        str(v)
                        for v in graph.objects(
                            obj, URIRef("http://www.w3.org/ns/ldp#contains")
                        )
                    ]
                elif isinstance(obj, BNode):
                    # Blank nodes serialized into JSON.
                    value = {p: o for p, o in graph.predicate_objects(obj)}
                elif isinstance(obj, URIRef):
                    value = str(obj)
                else:
                    value = obj.toPython()
                source_data[str(pred)] = value

                # Create a copy so that we can tolerate compacted and expanded forms
                # LDPSerializer will ignore non-mapped values later
                try:
                    source_data[graph.qname(pred)] = value
                except (ValueError, KeyError) as e:
                    logger.warning(
                        f"Unable to use compacted form of {pred}. RDFLib error: {e}"
                    )

            # Resolve a local instance of the model, so that we know the urlid
            instance = resolved_model.objects.filter(proxy_of=str(subject)).first()
            if instance is None:
                # Must set urlid before
                instance = resolved_model.objects.create(
                    proxy_of=str(subject), allow_create_backlink=False
                )
            instance.data_server_source = self.data_server_source
            self.imported_models.add(resolved_model)
            self.imported_subjects.append(instance.proxy_of)

            # Map the RDF types to local model names where possible, and resolve foreign keys
            serialize_fields_extra = []
            json_fields = {}
            for field in resolved_model._meta._get_fields(forward=True, reverse=True):
                field_path = f"{resolved_model}.{field.name}"
                rdf_type = None

                # Prioritise related RDF type on a relation
                if field.remote_field is not None and hasattr(
                    field.remote_field, "related_rdf_type"
                ):
                    rdf_type = field.remote_field.related_rdf_type
                # Otherwise the field must have an RDF type configured to be considered
                elif not hasattr(field, "rdf_type") or field.rdf_type is None:
                    logger.debug(
                        f"Skipping field import {field_path} because it lacks rdf_type"
                    )
                    continue
                else:
                    rdf_type = field.rdf_type

                # Attempt to fall back on expanded form, if compacted form is not in the dataset
                try:
                    rdf_type = (
                        graph.namespace_manager.expand_curie(rdf_type)
                        if rdf_type not in source_data
                        else rdf_type
                    )
                except ValueError as e:
                    logger.debug(
                        f"Ignorable while expanding property {rdf_type} on {field_path}"
                    )
                    logger.error(str(e))

                if rdf_type not in source_data:
                    logger.debug(
                        f"Skipping field import {field_path} because {rdf_type} is not present in data"
                    )
                    continue

                # Foreign Keys
                if field.related_model is not None:
                    if field.related_model._meta.abstract:
                        logger.debug(
                            f"Skipping field import {field_path} because the related model ({field.related_model}) is abstract"
                        )
                        continue
                    if field.one_to_many or field.many_to_many:
                        resource_data[field.name] = {
                            "@type": "ldp:Container",
                            "ldp:contains": [
                                self._serialize_related_instance(field, instance, x)
                                for x in source_data.pop(rdf_type)
                            ],
                        }
                    else:
                        resource_data[field.name] = self._serialize_related_instance(
                            field, instance, source_data.pop(rdf_type)
                        )
                else:
                    resource_data[field.name] = source_data.pop(rdf_type)

                # Workaround for lack of JSONField support in DjangoLDP.
                if isinstance(field, fields.JSONField):
                    json_fields[field.name] = resource_data[field.name]
                    resource_data.pop(field.name)

                if (
                    hasattr(resolved_model._meta, "serializer_fields")
                    and field.name not in resolved_model._meta.serializer_fields
                ):
                    serialize_fields_extra.append(field.name)

            # Use LDPSerializer with the resolved model to save it in our database.
            serializer_class = self.get_serializer_class(
                resolved_model, 10, serialize_fields_extra
            )
            resource_data["@id"] = instance.urlid
            resource_data = self._recursively_force_data_server_source(resource_data)

            serializer = serializer_class(instance, data=resource_data)
            if not serializer.is_valid():
                for err in serializer.errors:
                    resource_data.pop(err)
                logger.warning(
                    f"Received invalid data, errors are as follows: {serializer.errors}. "
                    f"Resource: {resource_data['proxy_of']}. Handling by omitting erroneous fields..."
                )
                serializer = serializer_class(instance, data=resource_data)
                serializer.is_valid(raise_exception=True)
            instance = serializer.save()

            # Workaround for lack of JSONField support in DjangoLDP.
            if len(json_fields):
                for field_name, value in json_fields.items():
                    setattr(instance, field_name, value)
                instance.save()

        logger.info(f"Finished importing {len(self.imported_subjects)} subjects")
        self.clean_up()
        self.create_record()

    def parse(self, jsonld_data, base_iri=None):
        """
        Parses and imports one JSON-LD document.

        :param base_iri: the base IRI against which the document's relative IRIs resolve.
            For a fetched document this is the URL it was fetched from; for a pushed
            document (update webhook) it is the data-server's urlid, because a pushed
            document has no URL of its own. Relative IRIs then resolve inside the
            data-server's domain, so the annexation check still applies to them. A
            document declaring its own @base keeps precedence, as JSON-LD requires.
            Without a base IRI, relative IRIs stay unresolved (previous behaviour).
        """
        with transaction.atomic():
            self._do_parse(jsonld_data, base_iri)

    def clean_up(self):
        """
        LDPSerializer creates objects implicitly that in our case become duplicate objects, because of the requirement that
        proxy_of define the original resource, not a urlid (DjangoLDP is not built with proxies in mind).

        Similarly, objects may have been previously cached and since removed, either because they were deleted or because
        we no longer have permission to proxy them.

        This method finds and deletes those objects.
        """
        ldp_serializer_created = Q(
            urlid__startswith=self.data_server_source_urlid, proxy_of__isnull=True
        )
        missing_from_new_import = Q(
            updated_at__lt=self.import_started_at,
            data_server_source__urlid__startswith=self.data_server_source_urlid,
        )
        # An update webhook carries only the resources that changed: it says nothing about
        # the others, so it must not discard them. Neither does an endpoint-scoped refresh,
        # whose discarding happens once the whole endpoint is read (finish_endpoint).
        sweep_unseen = (
            self.source != ResourceImportSource.UPDATE_WEBHOOK
            and getattr(settings, "DFC_PROXY_CLEANUP", "domain") != "endpoint"
        )
        condition = (
            ldp_serializer_created | missing_from_new_import
            if sweep_unseen
            else ldp_serializer_created
        )

        for imported_model in self.imported_models:
            deleted = imported_model.objects.filter(condition)
            self.deleted_subjects += [d.proxy_of for d in deleted]
            deleted.delete()
            logger.info(
                f"Deleted {deleted} instances of {imported_model} during cleanup on data source {self.data_server_source_urlid}"
            )

    @staticmethod
    def _proxied_models():
        from django.apps import apps

        return [
            model
            for model in apps.get_app_config("data_food_consortium").get_models()
            if any(f.name == "proxy_of" for f in model._meta.concrete_fields)
            and any(f.name == "data_server_source" for f in model._meta.concrete_fields)
        ]

    def _discard(self, subjects):
        """Delete the proxied instances of these IRIs that came from this data-server."""
        if not subjects:
            return
        for model in self._proxied_models():
            stale = model.objects.filter(
                proxy_of__in=subjects, data_server_source=self.data_server_source
            )
            self.deleted_subjects += [d.proxy_of for d in stale]
            stale.delete()

    def finish_endpoint(self, reachable=True):
        """
        After every page of an endpoint was read, discard what the previous import of this
        endpoint had and this one no longer has. With reachable=False (the endpoint
        refused access), discard everything the previous import had.

        Only acts when DFC_PROXY_CLEANUP is "endpoint" and the endpoint is known.
        """
        if getattr(settings, "DFC_PROXY_CLEANUP", "domain") != "endpoint" or not self.endpoint:
            return
        with transaction.atomic():
            state = EndpointImportState.objects.filter(endpoint=self.endpoint).first()
            previous = state.subject_set if state is not None else set()
            current = set(self.imported_subjects) if reachable else set()
            self._discard(previous - current)
            EndpointImportState.objects.update_or_create(
                endpoint=self.endpoint,
                defaults={
                    "data_server": self.data_server_source,
                    "subjects": "\n".join(sorted(current)),
                    "imported_at": self.import_started_at,
                },
            )
        if self.deleted_subjects:
            logger.info(
                f"Discarded {len(self.deleted_subjects)} subjects no longer served by {self.endpoint}"
            )

    def create_record(self):
        self.imported_subjects.sort()
        if settings.DFC_STORE_IMPORT_REPORTS is True:
            ResourceImportRecord.objects.create(
                import_started_at=self.import_started_at,
                data_batches=self.data_batches,
                data_server=self.data_server_source,
                imported_models="\n".join([str(m) for m in self.imported_models]),
                imported_subjects="\n".join(self.imported_subjects),
                deleted_subjects="\n".join(self.deleted_subjects),
                source=self.source,
            )
        logger.info(f"Import finished at {timezone.now()}. Report created in database")


class ResourceServerClient:
    """
    Manages the proxy's connection to a data source, authenticated with Keycloak.
    """

    dataserver_url = ""
    scope_config = None
    source = None
    parser = None

    def __init__(self, dataserver_url, source: ResourceImportSource):
        self.dataserver_url = dataserver_url
        if not self.dataserver_url.endswith("/"):
            self.dataserver_url += "/"
        self.source = source
        self.scope_config = settings.DFC_KEYCLOAK_READ_SCOPES.copy()

        discovery_endpoint = f"{self.dataserver_url}.well-known/dfc/"
        response = requests.get(discovery_endpoint)
        if response.status_code == 200:
            data_server_endpoints = response.json()
            for full, short in PermissioningScope.short_values_mapping.items():
                if (
                    full in data_server_endpoints
                    and data_server_endpoints[full] is not None
                ):
                    val = data_server_endpoints[full]
                    val = val.removeprefix("/")
                    self.scope_config[short] = val
            logger.debug(
                f"Configured ResourceServerClient with discovered config {self.scope_config}"
            )
        else:
            logger.warning(
                "Configured ResourceServerClient with default config, "
                f"discovery endpoint {discovery_endpoint} responded {response.status_code}"
            )

    def _log_dataserver_import_error(
        self, msg: str, e: Exception, failure_kind: ResourceImportFailure
    ):
        msg = f"{msg}\n{e.__class__.__name__}: {e}"
        logger.error(msg)

        # Create database record.
        if settings.DFC_STORE_IMPORT_REPORTS is not False:  # True, or "error"
            ResourceImportRecord.objects.create(
                import_started_at=self.parser.import_started_at,
                data_server=Model.get_or_create(DataServer, self.dataserver_url),
                data_batches=self.parser.data_batches,
                source=self.source,
                error_type=failure_kind,
                error_message=msg,
            )
        logger.info(f"Failure logged at {timezone.now()}. Report created in database")

    def request_all_scopes(self):
        for scope in settings.DFC_KEYCLOAK_READ_SCOPES:
            try:
                self.request_scope(scope)
            except KeycloakAuthenticationException as e:
                msg = f"ERR authenticating dataserver {self.dataserver_url} with Keycloak, while requesting {scope}"
                self._log_dataserver_import_error(
                    msg, e, ResourceImportFailure.AUTHENTICATION_ERROR
                )
            except requests.exceptions.RequestException as e:
                msg = f"ERR requesting a scope {scope} from dataserver {self.dataserver_url}"
                self._log_dataserver_import_error(
                    msg, e, ResourceImportFailure.NETWORK_ERROR
                )
            except (ParserError, ValueError, TypeError) as e:
                msg = f"ERR parsing response from dataserver {self.dataserver_url} on scope {scope}"
                self._log_dataserver_import_error(
                    msg, e, ResourceImportFailure.PARSE_ERROR
                )

    def _get_auth_headers_with_token_for_scope(self, scope: str):
        token = KeycloakClient(scope).get_access_token()
        return {"Authorization": f"Bearer {token}"}

    def _request_and_process_scope_at_endpoint(self, scope: str, endpoint: str):
        """
        Requests an access token from Keycloak for a given scope,
        and then recursively requests from the dataserver the associated endpoint,
        scraping all available data until complete.
        """
        headers = self._get_auth_headers_with_token_for_scope(scope)
        response = requests.get(endpoint, headers=headers)
        response.raise_for_status()
        # Some data-servers answer an empty body when nothing is granted any more (OFN
        # renders nothing for an empty list). That is an empty import, not an error:
        # with endpoint-scoped cleanup it discards what the endpoint used to return.
        if not response.content.strip():
            data = {"@graph": []}
        else:
            data = response.json()

        # Parse the returned graph, resolve and import to the relevant models.
        # The URL of this page is the document's base IRI: it is what the data-server's
        # relative IRIs (e.g. "#certification-4") are meant to resolve against.
        self.parser.parse(data, base_iri=endpoint)

        # If there is more data, continue.
        if "next" in data and data["next"] is not None:
            self._request_and_process_scope_at_endpoint(scope, data["next"])

    def request_scope(self, scope: str):
        """
        Discovers the appropriate endpoint for a scope, and then processes it.

        :raises KeycloakAuthenticationException: if authentication with Keycloak is unsuccessful
        :raises RequestException: if dataserver request is unsuccessful
        """
        # Each scope has an associated endpoint.
        endpoint = f"{self.dataserver_url}{self.scope_config[scope]}"
        self.parser = ProxyRefreshParser(endpoint, self.source, endpoint=endpoint)
        try:
            self._request_and_process_scope_at_endpoint(scope, endpoint)
        except requests.exceptions.HTTPError as e:
            forbidden = e.response is not None and e.response.status_code == 403
            if forbidden and getattr(settings, "DFC_PROXY_FORGET_ON_FORBIDDEN", False):
                self.parser.finish_endpoint(reachable=False)
            raise
        self.parser.finish_endpoint()
