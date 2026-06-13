"""Action nodes are normalized onto connectable native providers."""

from __future__ import annotations

from agents.native_providers import list_providers
from agents.provider_normalizer import _map_provider_slug, normalize_action_providers
from schemas.workflow import ActionNode, AgentNode, TriggerNode, WorkflowDefinition

_CATALOG = {p.id for p in list_providers()}


def test_saas_verbs_map_to_catalog_providers() -> None:
    assert _map_provider_slug("gmail", "send_email") == ("gmail", "gmail_send")
    assert _map_provider_slug("email", "send") == ("gmail", "gmail_send")
    assert _map_provider_slug("gmail", "fetch_emails") == ("gmail", "gmail_list_messages")
    assert _map_provider_slug("sendgrid", "send") == ("sendgrid", "sendgrid_send")
    assert _map_provider_slug("slack", "post_message") == ("slack", "slack_post_message")
    assert _map_provider_slug("jira", "create_issue") == ("jira", "jira_create_issue")
    assert _map_provider_slug("postgres", "read_rows") == ("postgres", "sql_query")
    assert _map_provider_slug("database", "write_row") == ("postgres", "sql_execute")
    assert _map_provider_slug("twilio", "sms") == ("twilio", "twilio_message_create")


def test_internal_actions_route_to_http_bearer_keeping_slug() -> None:
    # Internal/custom verbs become connectable via http_bearer, but keep their
    # descriptive slug so realistic demo records still fire.
    assert _map_provider_slug("http_bearer", "create_ticket") == ("http_bearer", "create_ticket")
    assert _map_provider_slug("crm", "register_customer") == ("http_bearer", "register_customer")
    assert _map_provider_slug("internal", "escalate_complaint") == ("http_bearer", "escalate_complaint")


def test_no_native_equivalent_left_untouched() -> None:
    # Google Sheets / Salesforce have no native catalog entry — don't force.
    assert _map_provider_slug("googlesheets", "read_range") == ("googlesheets", "read_range")
    assert _map_provider_slug("salesforce", "create_lead") == ("salesforce", "create_lead")


def test_every_mapped_saas_provider_is_in_catalog() -> None:
    for prov, slug in [("email", "send"), ("slack", "x"), ("jira", "y"), ("db", "read")]:
        p, _ = _map_provider_slug(prov, slug)
        assert p in _CATALOG


def test_normalize_definition_rewrites_action_nodes_only() -> None:
    defn = WorkflowDefinition(
        name="W",
        nodes=[
            TriggerNode(id="t", name="T", trigger_type="manual"),
            AgentNode(id="a", name="A", depends_on=["t"], tools=["knowledge_base"]),
            ActionNode(id="mail", name="Mail", depends_on=["a"], provider="email", action_slug="send_email"),
            ActionNode(id="tkt", name="Ticket", depends_on=["a"], provider="helpdesk", action_slug="create_ticket"),
        ],
    )
    normalize_action_providers(defn)
    by_id = {n.id: n for n in defn.nodes}
    assert (by_id["mail"].provider, by_id["mail"].action_slug) == ("gmail", "gmail_send")
    # helpdesk → connectable http_bearer, descriptive slug kept (mock still fires)
    assert by_id["tkt"].provider == "http_bearer"
    assert by_id["tkt"].action_slug == "create_ticket"
    # agent + trigger untouched
    assert by_id["a"].kind == "agent" and by_id["a"].tools == ["knowledge_base"]
