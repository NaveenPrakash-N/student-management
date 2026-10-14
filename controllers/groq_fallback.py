import json
import logging
import os

from odoo import http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

try:
    from groq import Groq
except ImportError:  # pragma: no cover - handled at runtime with a clear message
    Groq = None


_logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'openai/gpt-oss-20b'
MAX_HISTORY_TURNS = 12
MAX_TOOL_ROUNDS = 4

CONFIRM_WORDS = {
    'yes', 'y', 'yes please', 'confirm', 'confirmed', 'do it', 'go ahead',
    'proceed', 'ok', 'okay', 'sure', 'create it', 'update it',
}
CANCEL_WORDS = {
    'no', 'n', 'cancel', 'stop', 'dont', "don't", 'do not', 'never mind',
    'nevermind',
}

SYSTEM_PROMPT = """
You are Groq Backup AI inside Odoo 19.

Your job has two parts:
1. Answer small Odoo doubts clearly and concisely.
2. Use the supplied Odoo tools whenever the user asks about ACTUAL records in
   this database or wants to create/edit supported records.

Critical rules:
- Never invent database records, counts, field names, IDs, prices, states, or
  other database-specific facts. Use tools for them.
- Never claim that a create/edit succeeded unless the corresponding tool result
  says it succeeded.
- Do not ask for raw database access and never write SQL.
- Do not expose or request passwords, API keys, tokens, session cookies,
  private keys, hashes, or secrets.
- Generic deletion is intentionally unavailable. Explain that deletion must be
  done manually in Odoo if asked.
- Normal Odoo ACLs and record rules apply to every tool call.
- Friendly model names supported by the generic tools include student,
  teacher, department, subject, fee type, semester, semester fee, mark,
  contact/customer/vendor, product, quotation/sale order, and helpdesk ticket.
- For relations such as department_id, teacher_id, partner_id, use a unique
  human-readable name when possible. The Odoo tool resolves it.
- For create/edit, call the tool with only the fields the user actually gave.
  The controller will require user confirmation before executing the write.
- find_records and find_helpdesk_tickets accept limit between 1 and 100 only.
  Never pass a limit above 100 (not 1000, not "all") -- if the user wants
  everything and there may be more than 100, say so and offer to narrow the
  search (by department, date range, status, etc.) instead of requesting a
  huge limit.
- When a request is ambiguous, search/inspect first or ask one concise question.
- If a normal conceptual question does not need database information, answer it
  directly without a tool.
""".strip()


def _tool_definitions():
    return [
        {
            'type': 'function',
            'function': {
                'name': 'inspect_model',
                'description': 'Inspect real fields/types/relations for one supported Odoo record type.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'model_name': {'type': 'string'},
                    },
                    'required': ['model_name'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'find_records',
                'description': 'Search real supported Odoo records with text and optional exact field filters.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'model_name': {'type': 'string'},
                        'query': {'type': 'string'},
                        'filters': {'type': 'object'},
                        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
                    },
                    'required': ['model_name'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'create_record',
                'description': 'Prepare creation of one supported Odoo record. The user must confirm before execution.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'model_name': {'type': 'string'},
                        'values': {'type': 'object'},
                    },
                    'required': ['model_name', 'values'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'update_record',
                'description': 'Prepare editing one supported Odoo record by ID or unique name/reference. User confirmation is required.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'model_name': {'type': 'string'},
                        'record_ref': {'type': 'string'},
                        'values': {'type': 'object'},
                    },
                    'required': ['model_name', 'record_ref', 'values'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'find_helpdesk_tickets',
                'description': 'Find official Odoo Helpdesk tickets using optional text/person/priority/status filters.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'query': {'type': 'string'},
                        'person_name': {'type': 'string'},
                        'priority': {'type': 'string'},
                        'status': {'type': 'string'},
                        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
                    },
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'create_helpdesk_ticket',
                'description': 'Prepare creation of an official Helpdesk ticket. User confirmation is required.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'title': {'type': 'string'},
                        'details': {'type': 'string'},
                        'person_name': {'type': 'string'},
                        'team_name': {'type': 'string'},
                        'priority': {'type': 'string'},
                    },
                    'required': ['title'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'assign_helpdesk_ticket',
                'description': 'Prepare assigning an official Helpdesk ticket to an internal Odoo user. Confirmation required.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'ticket_ref': {'type': 'string'},
                        'person_name': {'type': 'string'},
                    },
                    'required': ['ticket_ref', 'person_name'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'set_helpdesk_priority',
                'description': 'Prepare changing a Helpdesk ticket priority. Confirmation required.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'ticket_ref': {'type': 'string'},
                        'priority': {'type': 'string'},
                    },
                    'required': ['ticket_ref', 'priority'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'set_helpdesk_status',
                'description': 'Prepare changing a Helpdesk ticket status/stage. Confirmation required.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'ticket_ref': {'type': 'string'},
                        'status': {'type': 'string'},
                    },
                    'required': ['ticket_ref', 'status'],
                    'additionalProperties': False,
                },
            },
        },
        {
            'type': 'function',
            'function': {
                'name': 'update_helpdesk_ticket',
                'description': 'Prepare editing a Helpdesk ticket title and/or description. Confirmation required.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'ticket_ref': {'type': 'string'},
                        'new_title': {'type': 'string'},
                        'new_description': {'type': 'string'},
                    },
                    'required': ['ticket_ref'],
                    'additionalProperties': False,
                },
            },
        },
    ]


WRITE_TOOLS = {
    'create_record',
    'update_record',
    'create_helpdesk_ticket',
    'assign_helpdesk_ticket',
    'set_helpdesk_priority',
    'set_helpdesk_status',
    'update_helpdesk_ticket',
}


def _api_key():
    Config = request.env['ir.config_parameter'].sudo()
    return (
        Config.get_param('students_management.groq_api_key')
        or Config.get_param('odoo_help_chat.groq_api_key')
        or os.environ.get('GROQ_API_KEY')
        or ''
    ).strip()


def _model_name():
    Config = request.env['ir.config_parameter'].sudo()
    return (
        Config.get_param('students_management.groq_model')
        or os.environ.get('GROQ_MODEL')
        or DEFAULT_MODEL
    ).strip()


def _history_messages(history):
    messages = []
    if not isinstance(history, list):
        return messages
    for item in history[-MAX_HISTORY_TURNS:]:
        if not isinstance(item, dict):
            continue
        role = item.get('role')
        content = item.get('message') or item.get('content')
        if role not in {'user', 'assistant'} or not isinstance(content, str):
            continue
        content = content.strip()
        if content:
            messages.append({'role': role, 'content': content[:6000]})
    return messages


def _tool_args(tool_call):
    raw = getattr(tool_call.function, 'arguments', None) or '{}'
    try:
        args = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise UserError('Groq returned invalid tool arguments.') from exc
    if not isinstance(args, dict):
        raise UserError('Groq returned invalid tool arguments.')
    return args


def _assistant_tool_message(message):
    calls = []
    for tool_call in getattr(message, 'tool_calls', None) or []:
        calls.append({
            'id': tool_call.id,
            'type': 'function',
            'function': {
                'name': tool_call.function.name,
                'arguments': tool_call.function.arguments,
            },
        })
    return {
        'role': 'assistant',
        'content': getattr(message, 'content', None) or '',
        'tool_calls': calls,
    }


def _json_text(value):
    return json.dumps(value or {}, ensure_ascii=False)


def _execute_tool(name, args):
    service = request.env['helpdesk.native.ai.service']

    if name == 'inspect_model':
        return service.ai_inspect_model(args.get('model_name', ''))
    if name == 'find_records':
        return service.ai_find_records(
            args.get('model_name', ''),
            args.get('query', ''),
            _json_text(args.get('filters')),
            str(args.get('limit') or 10),
        )
    if name == 'create_record':
        return service.ai_create_record(
            args.get('model_name', ''),
            _json_text(args.get('values')),
        )
    if name == 'update_record':
        return service.ai_update_record(
            args.get('model_name', ''),
            args.get('record_ref', ''),
            _json_text(args.get('values')),
        )
    if name == 'find_helpdesk_tickets':
        return service.ai_find_tickets(
            args.get('query', ''),
            args.get('person_name', ''),
            args.get('priority', ''),
            args.get('status', ''),
            str(args.get('limit') or 10),
        )
    if name == 'create_helpdesk_ticket':
        return service.ai_create_ticket(
            args.get('title', ''),
            args.get('details', ''),
            args.get('person_name', ''),
            args.get('team_name', ''),
            args.get('priority', '') or 'low',
        )
    if name == 'assign_helpdesk_ticket':
        return service.ai_assign_ticket(args.get('ticket_ref', ''), args.get('person_name', ''))
    if name == 'set_helpdesk_priority':
        return service.ai_set_priority(args.get('ticket_ref', ''), args.get('priority', ''))
    if name == 'set_helpdesk_status':
        return service.ai_set_status(args.get('ticket_ref', ''), args.get('status', ''))
    if name == 'update_helpdesk_ticket':
        return service.ai_update_ticket(
            args.get('ticket_ref', ''),
            args.get('new_title', ''),
            args.get('new_description', ''),
        )
    raise UserError("Unknown Groq tool '%s'." % name)


def _confirmation_text(name, args):
    if name == 'create_record':
        return 'Create %s with %s' % (args.get('model_name', 'record'), _json_text(args.get('values')))
    if name == 'update_record':
        return 'Update %s %s with %s' % (
            args.get('model_name', 'record'), args.get('record_ref', ''), _json_text(args.get('values')),
        )
    if name == 'create_helpdesk_ticket':
        return "Create Helpdesk ticket '%s' (priority: %s)" % (
            args.get('title', ''), args.get('priority') or 'low',
        )
    if name == 'assign_helpdesk_ticket':
        return 'Assign ticket %s to %s' % (args.get('ticket_ref', ''), args.get('person_name', ''))
    if name == 'set_helpdesk_priority':
        return 'Set ticket %s priority to %s' % (args.get('ticket_ref', ''), args.get('priority', ''))
    if name == 'set_helpdesk_status':
        return 'Set ticket %s status to %s' % (args.get('ticket_ref', ''), args.get('status', ''))
    if name == 'update_helpdesk_ticket':
        return 'Update ticket %s' % args.get('ticket_ref', '')
    return 'Run the requested Odoo change'


class GroqFallbackController(http.Controller):

    @http.route(
        '/students_management/ai/groq_status',
        type='jsonrpc', auth='user', methods=['POST'], csrf=False,
    )
    def groq_status(self, **_kwargs):
        return {
            'ok': bool(Groq and _api_key()),
            'package_installed': bool(Groq),
            'api_key_configured': bool(_api_key()),
            'model': _model_name(),
        }

    @http.route(
        '/students_management/ai/groq_ask',
        type='jsonrpc', auth='user', methods=['POST'], csrf=False,
    )
    def groq_ask(self, message=None, history=None, **_kwargs):
        text = (message or '').strip()
        if not text:
            return {'error': 'No message provided.'}
        if Groq is None:
            return {'error': 'Groq Python package is missing. Run: pip install -U groq'}

        api_key = _api_key()
        if not api_key:
            return {
                'error': (
                    "Groq API key is not configured. Create Odoo System Parameter "
                    "'students_management.groq_api_key' or set GROQ_API_KEY."
                )
            }

        pending = request.session.get('students_management_groq_pending_write')
        normalized = ' '.join(text.lower().split())
        if pending:
            if normalized in CONFIRM_WORDS:
                request.session.pop('students_management_groq_pending_write', None)
                try:
                    result = _execute_tool(pending['name'], pending['args'])
                    return {'reply': result, 'executed': True, 'provider': 'groq'}
                except (UserError, AccessError, ValidationError) as exc:
                    return {'error': str(exc)}
                except Exception as exc:  # keep stack in Odoo log, not browser
                    _logger.exception('Groq confirmed write failed')
                    return {'error': 'The Odoo operation failed: %s' % exc}
            if normalized in CANCEL_WORDS:
                request.session.pop('students_management_groq_pending_write', None)
                return {'reply': 'Cancelled. No Odoo data was changed.', 'provider': 'groq'}
            return {
                'reply': (
                    "A change is waiting for confirmation:\n%s\n\n"
                    "Choose Confirm or Cancel below. You can still type 'confirm' or 'cancel' if needed."
                ) % pending.get('summary', 'Odoo change'),
                'pending_confirmation': True,
                'provider': 'groq',
            }

        client = Groq(api_key=api_key)
        messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
        messages.extend(_history_messages(history))
        messages.append({'role': 'user', 'content': text})

        try:
            for _round in range(MAX_TOOL_ROUNDS):
                try:
                    response = client.chat.completions.create(
                        model=_model_name(),
                        messages=messages,
                        tools=_tool_definitions(),
                        tool_choice='auto',
                        temperature=0.1,
                    )
                except Exception as exc:
                    # Groq validates the model's generated tool-call arguments
                    # against our JSON schema *before* it ever reaches our
                    # code (e.g. the model asking for limit=1000 when the
                    # schema caps it at 100). That surfaces as a plain
                    # exception here, with no assistant message and no
                    # tool_call_id to reply to -- so instead of failing the
                    # whole turn, tell the model what it did wrong as a user
                    # message and let it retry within the same round budget.
                    detail = str(exc)
                    if 'tool_use_failed' in detail or 'did not match schema' in detail:
                        _logger.info('Groq tool-call schema validation failed, retrying once: %s', detail)
                        messages.append({
                            'role': 'user',
                            'content': (
                                'Your last tool call was rejected because its arguments did not '
                                'match the tool schema (%s). Retry with corrected, in-range '
                                'arguments -- for example, limit must be between 1 and 100.'
                            ) % detail,
                        })
                        continue
                    raise

                answer = response.choices[0].message
                tool_calls = getattr(answer, 'tool_calls', None) or []
                if not tool_calls:
                    content = (getattr(answer, 'content', None) or '').strip()
                    return {
                        'reply': content or 'No response was returned.',
                        'provider': 'groq',
                        'model': _model_name(),
                    }

                messages.append(_assistant_tool_message(answer))

                for tool_call in tool_calls:
                    name = tool_call.function.name
                    args = _tool_args(tool_call)

                    if name in WRITE_TOOLS:
                        summary = _confirmation_text(name, args)
                        request.session['students_management_groq_pending_write'] = {
                            'name': name,
                            'args': args,
                            'summary': summary,
                        }
                        return {
                            'reply': "%s\n\nChoose Confirm or Cancel below. You can still type 'confirm' or 'cancel' if needed." % summary,
                            'pending_confirmation': True,
                            'provider': 'groq',
                            'model': _model_name(),
                        }

                    try:
                        tool_result = _execute_tool(name, args)
                    except (UserError, AccessError, ValidationError) as exc:
                        tool_result = 'Odoo tool error: %s' % exc
                    messages.append({
                        'role': 'tool',
                        'tool_call_id': tool_call.id,
                        'content': str(tool_result),
                    })

            return {
                'error': 'Groq used too many tool steps for one request. Please make the request more specific.'
            }

        except Exception as exc:
            _logger.exception('Groq backup assistant error')
            message_text = str(exc)
            if '429' in message_text or 'rate limit' in message_text.lower():
                return {
                    'error': 'Groq rate limit reached. Use Odoo Ask AI/Gemini for now, or try Groq again after its quota resets.'
                }
            return {'error': 'Groq assistant error: %s' % message_text}
