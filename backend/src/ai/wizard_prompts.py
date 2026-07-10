WIZARD_DETECTION_KEYWORDS = [
    "setup wizard",
    "configuration wizard",
    "config wizard",
    "setup form",
    "onboarding wizard",
    "installation wizard",
]


WIZARD_GENERATION_PROMPT = """You are generating a setup wizard JSON schema for an EveriApp app. A setup wizard allows users to configure the app when they install it from the marketplace.

## Wizard Schema Format
The wizard is a JSON object with the following structure:

```json
{
  "title": "App Setup",
  "description": "Configure your app settings",
  "steps": [
    {
      "title": "Step 1: Basic Setup",
      "description": "Configure the basic settings",
      "fields": [
        {
          "key": "api_key",
          "label": "API Key",
          "type": "secret",
          "description": "Your API key for the data service",
          "required": true,
          "placeholder": "sk-..."
        },
        {
          "key": "company_name",
          "label": "Company Name",
          "type": "string",
          "description": "Your company or organization name",
          "required": true
        },
        {
          "key": "refresh_interval",
          "label": "Data Refresh Interval",
          "type": "select",
          "options": ["5 minutes", "15 minutes", "1 hour", "Daily"],
          "default": "15 minutes"
        }
      ]
    },
    {
      "title": "Step 2: Appearance",
      "description": "Customize how the app looks",
      "fields": [
        {
          "key": "theme",
          "label": "Color Theme",
          "type": "select",
          "options": ["Dark", "Light", "System"],
          "default": "Dark"
        },
        {
          "key": "show_logo",
          "label": "Show Company Logo",
          "type": "boolean",
          "default": true
        }
      ]
    }
  ]
}
```

## Field Types
- `string` — Text input
- `secret` — Password/API key (will be encrypted)
- `number` — Numeric input
- `boolean` — Toggle switch
- `select` — Dropdown with options
- `url` — URL input with validation

## Rules
1. Group related fields into logical steps (2-4 steps is ideal)
2. Put required fields first in each step
3. Include descriptions and placeholders to guide users
4. Use appropriate field types (secrets for keys, selects for choices)
5. Each field's `key` should be a valid JS identifier (snake_case preferred)
6. Include sensible defaults where appropriate

Now generate a wizard for the described app. Respond with ONLY the JSON — no explanation needed.
"""


def is_wizard_request(message: str) -> bool:
    """Check if the user's message is asking for a setup wizard."""
    lower = message.lower()
    return any(keyword in lower for keyword in WIZARD_DETECTION_KEYWORDS)
