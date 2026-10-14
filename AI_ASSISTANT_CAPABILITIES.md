# Ask AI capabilities in this build

The module extends Odoo 19 Enterprise's built-in **Ask AI** and also adds a
separate **Groq Backup AI** panel. The built-in Ask AI remains the primary path;
Groq is an independent fallback when the primary provider is unavailable.

## What Ask AI can do

- Answer normal/minor Odoo doubts in simple language.
- Inspect the real fields of supported models when it needs database-specific
  information.
- Search records.
- Create records.
- Edit existing records by ID or a unique name/reference.
- Continue using dedicated official Helpdesk tools for ticket operations.
- Use the existing Whisper microphone button for English voice input.

## Supported generic record types

- Student
- Teacher
- Department
- Subject
- Fee Type
- Semester
- Semester Fee
- Mark
- Contact / Customer / Vendor
- Product
- Quotation / Sales Order
- Official Helpdesk Ticket

The generic tools intentionally do **not** delete records.

## Safety / permissions

Every operation runs as the currently logged-in Odoo user. The implementation
does not call `sudo()`, so Odoo access-control lists and record rules still
apply. Ask AI reports the ORM result instead of pretending a write succeeded.

## Example prompts

- `Create a student named Anu, email anu@example.com, department ECE.`
- `Find students in the ECE department.`
- `Change student Anu's phone to 9876543210.`
- `Create a contact named Kumar with email kumar@example.com.`
- `Show me the fields available in teacher.`
- `What is a Many2one field in Odoo?`
- `Why do we use @api.depends?`
- `Create a high priority helpdesk ticket for Naveen about printer issue.`

If a field or record name is ambiguous, Ask AI should inspect/search first or
ask for only the missing clarification.


## Groq backup setup

1. Install the Python client in the same Odoo virtualenv:
   `pip install -U groq`
2. In developer mode open **Settings > Technical > Parameters > System Parameters**.
3. Create key `students_management.groq_api_key` with your Groq API key.
   For compatibility, the older key `odoo_help_chat.groq_api_key` is also accepted.
4. Optional model override: `students_management.groq_model`.
   Default: `openai/gpt-oss-20b`.
5. Upgrade the module and hard-refresh the browser.

The floating **G** button opens Groq Backup AI. Read operations run immediately.
Create/edit operations are staged and require an explicit `confirm`; `cancel`
discards the pending change. Generic delete is not exposed.

Voice inside the Groq backup panel uses **Groq Speech-to-Text** with
`whisper-large-v3`, English locked (`language=en`), temperature `0`, and an
Odoo terminology prompt for better recognition of technical words. The transcript
is inserted into the G chat box for review and is **not auto-sent**. The native
Odoo Ask AI microphone still uses the separate local Whisper endpoint.

Optional voice settings:
- `students_management.groq_voice_model` (default: `whisper-large-v3`)
- `students_management.groq_voice_prompt` (custom English spelling/context prompt)

This backup is deliberately separate from Odoo's native Ask AI UI. It avoids
patching Odoo Enterprise AI internals, so an Odoo upgrade is much less likely to
break it. If Gemini/OpenAI in Ask AI fails, open the **G** panel and continue there.


## Voice transcription

Both the native Odoo **Ask AI** microphone and the floating **G** backup chat
use the same Groq Speech-to-Text endpoint with `whisper-large-v3`, English
language lock, and the configured Odoo terminology prompt. The transcript is
inserted into the relevant chat input only; the user can review/correct it and
must press **Send** manually.
