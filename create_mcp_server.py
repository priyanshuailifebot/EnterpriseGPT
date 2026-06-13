from composio import Composio



c = Composio(api_key="ak_b2io2LWkWXCbeHemGg8l")  



server = c.mcp.create(

    name="enterprisegpt1",

    toolkits=[

        {"toolkit": "googlesheets", "auth_config": "ac_9cmvnCXjj1QY"},

        {"toolkit": "gmail",        "auth_config": "ac_DPnGLBFZJO39"},

        {"toolkit": "googledrive",  "auth_config": "ac_5RRCmYqw6k_6"},

        {"toolkit": "sendgrid",     "auth_config": "ac_56bU1ziZCu18"},

    ],

    allowed_tools=[
        # Direct tools
        "GOOGLESHEETS_VALUES_GET",
        "GOOGLESHEETS_BATCH_GET",
        "GMAIL_SEND_EMAIL",
        "GMAIL_FETCH_EMAILS",
        "GOOGLEDRIVE_FIND_FILE",
        "SENDGRID_SEND_EMAIL",
        # Meta-tools — REQUIRED for the action_runner's fallback path
        "COMPOSIO_SEARCH_TOOLS",
        "COMPOSIO_MULTI_EXECUTE_TOOL",
    ],
)
print("server.id:", server.id)

instance = c.mcp.generate(user_id="default", mcp_config_id=server.id)
print("COMPOSIO_MCP_URL=" + instance["url"])

#Enterprisegpt
#server.id: 91649947-66f2-4c5c-8a00-19a115d407c0
#COMPOSIO_MCP_URL=https://backend.composio.dev/v3/mcp/91649947-66f2-4c5c-8a00-19a115d407c0?include_composio_helper_actions=true&user_id=default



#name="enterprisegpt1"
#server.id: 71d7a63a-5db8-4c9b-8d4b-fda99a869b28
#COMPOSIO_MCP_URL=https://backend.composio.dev/v3/mcp/71d7a63a-5db8-4c9b-8d4b-fda99a869b28/mcp?include_composio_helper_actions=true&user_id=default