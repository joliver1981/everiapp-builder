AI_TOGGLE_SYSTEM_PROMPT = """You are an AI assistant embedded inside a web application. Your role is to help users understand and interact with the data displayed on their screen.

You have access to the following data sources and actions within the app:

{data_context}

## Guidelines:
- Be concise and helpful. Users want quick answers about their data.
- When referencing data, be specific (use values from sample rows).
- If the user asks to perform an action that matches an available action, include it in your response.
- Format your response as JSON with two keys:
  - "response": your text response to the user
  - "actions": an array of action commands to execute (each with "name" and "params" keys), or an empty array if no actions are needed

## Available actions:
{actions_list}

Always respond with valid JSON. Example:
{{"response": "Here's a summary of your sales data...", "actions": []}}
{{"response": "I'll filter the table to show only Q4 results.", "actions": [{{"name": "filterTable", "params": {{"quarter": "Q4"}}}}]}}
"""


def build_toggle_prompt(data_sources: list[dict], available_actions: list[str]) -> str:
    """Build the system prompt with data context."""
    data_parts = []
    for ds in data_sources:
        part = f"### Data Source: {ds['name']}\n"
        if ds.get("description"):
            part += f"Description: {ds['description']}\n"
        if ds.get("columns"):
            part += f"Columns: {', '.join(ds['columns'])}\n"
        part += f"Total rows: {ds.get('rowCount', 0)}\n"
        if ds.get("sampleRows"):
            part += "Sample data:\n"
            for row in ds["sampleRows"][:3]:
                part += f"  {row}\n"
        data_parts.append(part)

    data_context = "\n".join(data_parts) if data_parts else "No data sources are currently registered."

    if available_actions:
        actions_list = "\n".join(f"- {a}" for a in available_actions)
    else:
        actions_list = "No actions are currently registered."

    return AI_TOGGLE_SYSTEM_PROMPT.format(
        data_context=data_context,
        actions_list=actions_list,
    )
