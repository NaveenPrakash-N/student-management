import logging

from odoo import api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PREFIX = '[Students Native AI]'
LEGACY_PREFIX = '[Students Native Helpdesk AI]'
TOPIC_NAME = 'Student & Odoo Record Operations'
LEGACY_TOPIC_NAME = 'Official Helpdesk Ticket Operations'
# Keep the old marker names so upgrading replaces the previous Helpdesk-only
# prompt block instead of leaving two conflicting instruction blocks behind.
PROMPT_MARKER_START = '\n\n<!-- STUDENTS_NATIVE_HELPDESK_AI_START -->\n'
PROMPT_MARKER_END = '\n<!-- STUDENTS_NATIVE_HELPDESK_AI_END -->'

GENERAL_INSTRUCTIONS = """
You are the user's practical Odoo assistant. Besides answering normal Odoo
questions, you have permission-aware tools for reading and changing selected
Odoo records.

What you can do:
- Answer small doubts in clear, simple language: Odoo concepts, fields, models,
  views, ORM, common errors, and how this Student Management module works.
- Inspect supported models/fields when you need the database's real schema.
- Search supported records.
- Create and edit supported records when the user asks.
- Continue to manage official Odoo Helpdesk tickets with the dedicated tools.

Supported generic record types include students, teachers, departments,
subjects, fee types, semesters, semester fees, marks, contacts/customers,
products, quotations/sales orders, and official Helpdesk tickets.

Rules:
- Use a database tool only when the request needs current Odoo data or a write.
  For a simple conceptual doubt, answer directly without calling a write tool.
- Generic create/update field values are passed as valid JSON text, using either
  technical field names or exact field labels. Example:
  {"name": "Anu", "email": "anu@example.com", "department_id": "ECE"}
- For Many2one fields, pass a unique record name/reference or numeric ID.
- For Many2many fields, pass a JSON list of unique names/IDs.
- Never invent a successful create/update. Report the backend tool result.
- Respect the current logged-in user's Odoo ACLs and record rules. The tools do
  not sudo and must not bypass permissions.
- If a requested model or field is unclear, use Inspect Odoo Model before asking
  the user to repeat information that can be discovered from Odoo.
- If a record name is ambiguous, ask for an ID or a more specific reference.
- Do not delete records through these generic tools.

Official Helpdesk rules:
- Operate only on Odoo's official helpdesk.ticket model. Never use helpdesk_mgmt.
- Use dedicated Helpdesk tools for create/find/assign/priority/status/update when
  they fit better than the generic tools.
- Priority values are low, medium, high, urgent; default new tickets to low.
- Ticket references may be a numeric record ID, ticket 12, #12, a human ticket
  reference when available, or a unique title fragment.
""".strip()



def _selection_value(model, field_name, wanted=('string', 'text', 'char')):
    field = model._fields.get(field_name)
    if not field or field.type != 'selection':
        return None
    selection = field.selection(model.env) if callable(field.selection) else field.selection
    keys = [item[0] for item in (selection or [])]
    for item in wanted:
        if item in keys:
            return item
    return keys[0] if keys else None


def _schema_relation(Action):
    """Return an x2many AI schema relation when this Odoo build uses one."""
    candidates = []
    for fname, field in Action._fields.items():
        if field.type not in ('one2many', 'many2many') or not getattr(field, 'comodel_name', None):
            continue
        lname = fname.lower()
        if 'schema' in lname or ('ai' in lname and any(token in lname for token in ('arg', 'param', 'tool'))):
            score = 0
            if 'schema' in lname:
                score -= 30
            if 'ai' in lname:
                score -= 20
            if 'tool' in lname:
                score -= 10
            candidates.append((score, fname, field))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[0], item[1]))[0][1:]


def _schema_json_field(Action):
    """Find the JSON/text field used by Odoo 19 to store an AI tool schema.

    Some Odoo 19 Enterprise builds expose AI Schema as a JSON field rather than
    an x2many relation.  The previous integration only looked for relational
    fields, which is why installation failed even though ai_server_actions and
    use_in_ai were present.
    """
    candidates = []
    for fname, field in Action._fields.items():
        lname = fname.lower()
        if field.type not in ('json', 'text', 'char'):
            continue
        if 'schema' not in lname:
            continue
        score = 0
        if lname == 'ai_schema':
            score -= 100
        if 'ai' in lname:
            score -= 50
        if 'tool' in lname:
            score -= 20
        if field.type == 'json':
            score -= 10
        candidates.append((score, fname, field))
    if not candidates:
        return None

    # Prefer the candidate which is already populated on a native Odoo AI tool.
    existing_tools = Action.search([('use_in_ai', '=', True)], limit=30) if 'use_in_ai' in Action._fields else Action.browse()
    scored = []
    for score, fname, field in candidates:
        populated = any(bool(rec[fname]) for rec in existing_tools if rec.id)
        scored.append((score - (25 if populated else 0), fname, field))
    _, fname, field = sorted(scored, key=lambda item: (item[0], item[1]))[0]
    return fname, field


def _clear_existing_schema(env, action, rel_name, rel_field):
    try:
        related = action[rel_name]
        if rel_field.type == 'one2many':
            related.unlink()
        elif rel_field.type == 'many2many':
            action.write({rel_name: [(5, 0, 0)]})
    except Exception:
        _logger.exception('Could not clear existing AI schema for action %s', action.id)
        raise


def _schema_value_from_sample(sample, args):
    """Build the schema in the same shape as native Odoo actions when possible."""
    import json

    if isinstance(sample, str):
        try:
            parsed = json.loads(sample)
        except Exception:
            parsed = None
        value = _schema_value_from_sample(parsed, args)
        return json.dumps(value)

    # Odoo builds that render AI Schema as editable rows commonly store a list
    # of small dictionaries.  Preserve any native key naming we can infer.
    if isinstance(sample, list):
        template = next((item for item in sample if isinstance(item, dict)), {})
        name_key = next((k for k in ('name', 'argument_name', 'parameter_name') if k in template), 'name')
        desc_key = next((k for k in ('description', 'help', 'argument_description') if k in template), 'description')
        required_key = next((k for k in ('required', 'is_required') if k in template), None)
        type_key = next((k for k in ('type', 'value_type', 'field_type') if k in template), None)
        rows = []
        for arg in args:
            row = {name_key: arg['name'], desc_key: arg['description']}
            if required_key:
                row[required_key] = bool(arg.get('required', True))
            if type_key:
                row[type_key] = 'string'
            rows.append(row)
        return rows

    # Standard JSON-Schema shape (also accepted by bridge implementations that
    # read the Enterprise action's schema as JSON schema).
    if isinstance(sample, dict) and ('properties' in sample or sample.get('type') == 'object'):
        properties = {
            arg['name']: {'type': 'string', 'description': arg['description']}
            for arg in args
        }
        return {
            'type': 'object',
            'properties': properties,
            'required': [arg['name'] for arg in args if arg.get('required', True)],
        }

    # Some wrappers store the JSON Schema under a parameters key.
    if isinstance(sample, dict) and 'parameters' in sample:
        return {
            'parameters': {
                'type': 'object',
                'properties': {
                    arg['name']: {'type': 'string', 'description': arg['description']}
                    for arg in args
                },
                'required': [arg['name'] for arg in args if arg.get('required', True)],
            }
        }

    # With no native sample available, prefer the row format documented by the
    # Odoo UI (Name + Description rows).  This is intentionally simple.
    return [
        {
            'name': arg['name'],
            'description': arg['description'],
            'required': bool(arg.get('required', True)),
            'type': 'string',
        }
        for arg in args
    ]


def _create_external_schema_rows(env, action, args):
    """Fallback for builds storing schema rows in a separate model without x2many."""
    registry_models = getattr(env.registry, 'models', {})
    candidates = []
    for model_name in registry_models:
        lname = model_name.lower()
        if 'ai' not in lname or not any(token in lname for token in ('schema', 'argument', 'parameter', 'tool')):
            continue
        try:
            Model = env[model_name]
        except Exception:
            continue
        action_field = next((
            fname for fname, field in Model._fields.items()
            if field.type == 'many2one' and getattr(field, 'comodel_name', None) == 'ir.actions.server'
        ), None)
        name_field = next((k for k in ('name', 'argument_name', 'parameter_name') if k in Model._fields), None)
        if action_field and name_field:
            score = 0
            if 'schema' in lname:
                score -= 20
            if 'argument' in lname or 'parameter' in lname:
                score -= 10
            candidates.append((score, model_name, action_field, name_field))
    if not candidates:
        return False

    _score, model_name, action_field, name_field = sorted(candidates)[0]
    Schema = env[model_name]
    Schema.search([(action_field, '=', action.id)]).unlink()
    vals_list = []
    for sequence, arg in enumerate(args, start=1):
        vals = {action_field: action.id, name_field: arg['name']}
        desc_field = next((k for k in ('description', 'help', 'argument_description') if k in Schema._fields), None)
        if desc_field:
            vals[desc_field] = arg['description']
        req_field = next((k for k in ('required', 'is_required') if k in Schema._fields), None)
        if req_field:
            vals[req_field] = bool(arg.get('required', True))
        if 'sequence' in Schema._fields:
            vals['sequence'] = sequence
        type_field = next((k for k in ('type', 'value_type', 'field_type') if k in Schema._fields), None)
        if type_field:
            fld = Schema._fields[type_field]
            if fld.type == 'selection':
                vals[type_field] = _selection_value(Schema, type_field, ('string', 'text', 'char'))
            elif fld.type in ('char', 'text'):
                vals[type_field] = 'string'
        vals_list.append(vals)
    Schema.create(vals_list)
    return True


def _create_schema_rows(env, action, args):
    Action = env['ir.actions.server']

    # 1) Newer/alternate Odoo 19 builds: schema stored directly as JSON/text.
    json_storage = _schema_json_field(Action)
    if json_storage:
        field_name, field = json_storage
        sample = None
        native_samples = Action.search([
            ('use_in_ai', '=', True),
            ('id', '!=', action.id),
        ], limit=30) if 'use_in_ai' in Action._fields else Action.browse()
        for rec in native_samples:
            if rec[field_name]:
                sample = rec[field_name]
                break
        value = _schema_value_from_sample(sample, args)
        if field.type == 'json':
            action.write({field_name: value})
        else:
            import json
            action.write({field_name: value if isinstance(value, str) else json.dumps(value)})
        _logger.info('Configured AI schema for action %s using %s (%s)', action.id, field_name, field.type)
        return

    # 2) Odoo builds using an x2many schema relation.
    relation = _schema_relation(Action)
    if relation:
        rel_name, rel_field = relation
        Schema = env[rel_field.comodel_name]
        _clear_existing_schema(env, action, rel_name, rel_field)

        rows = []
        for sequence, arg in enumerate(args, start=1):
            vals = {}
            if rel_field.type == 'one2many':
                vals[rel_field.inverse_name] = action.id

            name_field = next((key for key in ('name', 'argument_name', 'parameter_name') if key in Schema._fields), None)
            if not name_field:
                raise UserError('Could not determine the AI schema argument-name field on %s.' % Schema._name)
            vals[name_field] = arg['name']

            desc_field = next((key for key in ('description', 'help', 'argument_description') if key in Schema._fields), None)
            if desc_field:
                vals[desc_field] = arg['description']

            required_field = next((key for key in ('required', 'is_required') if key in Schema._fields), None)
            if required_field:
                vals[required_field] = bool(arg.get('required', True))
            if 'sequence' in Schema._fields:
                vals['sequence'] = sequence

            type_field = next((key for key in ('type', 'value_type', 'field_type') if key in Schema._fields), None)
            if type_field:
                field = Schema._fields[type_field]
                if field.type == 'selection':
                    vals[type_field] = _selection_value(Schema, type_field, ('string', 'text', 'char'))
                elif field.type in ('char', 'text'):
                    vals[type_field] = 'string'
            rows.append(vals)

        created = Schema.create(rows)
        if rel_field.type == 'many2many':
            action.write({rel_name: [(6, 0, created.ids)]})
        return

    # 3) Rare variant: separate schema model with only an m2o back to the action.
    if _create_external_schema_rows(env, action, args):
        return

    # Give a diagnostic that reflects the actual situation.  use_in_ai may be
    # present even when our schema-storage discovery cannot identify the build.
    relevant = []
    for fname, field in Action._fields.items():
        lname = fname.lower()
        if any(token in lname for token in ('ai', 'schema', 'tool', 'arg', 'param')):
            relevant.append('%s:%s%s' % (
                fname,
                field.type,
                ('->' + field.comodel_name) if getattr(field, 'comodel_name', None) else '',
            ))
    raise UserError(
        'Odoo AI tools are installed (use_in_ai exists), but this build stores the AI Schema '
        'in an unrecognised field. Relevant ir.actions.server fields: %s' % ', '.join(sorted(relevant))
    )

def _create_or_replace_tool(env, model_id, name, description, code, args):
    Action = env['ir.actions.server']
    action_name = '%s %s' % (PREFIX, name)
    existing = Action.search([('name', '=', action_name)])
    if existing:
        existing.unlink()

    if 'use_in_ai' not in Action._fields:
        raise UserError(
            'The Use in AI field is missing on Server Actions. '
            'Install/upgrade Odoo 19 Enterprise ai_server_actions.'
        )

    vals = {
        'name': action_name,
        'model_id': model_id,
        'state': 'code',
        'code': code,
        'use_in_ai': True,
    }
    for field_name in ('description', 'ai_description', 'tool_description'):
        if field_name in Action._fields:
            vals[field_name] = description
            break
    action = Action.create(vals)
    _create_schema_rows(env, action, args)
    return action


def _get_or_create_topic(env):
    Topic = env['ai.topic']
    topic = Topic.search([('name', '=', TOPIC_NAME)], limit=1)
    if not topic:
        topic = Topic.search([('name', '=', LEGACY_TOPIC_NAME)], limit=1)
    vals = {'name': TOPIC_NAME}
    instruction_field = next((field_name for field_name in ('instructions', 'prompt', 'description') if field_name in Topic._fields), None)
    if instruction_field:
        vals[instruction_field] = GENERAL_INSTRUCTIONS
    if topic:
        topic.write(vals)
    else:
        topic = Topic.create(vals)
    return topic


def _link_actions_to_topic(env, topic, actions):
    Topic = env['ai.topic']
    direct = []
    for fname, field in Topic._fields.items():
        if field.type in ('one2many', 'many2many') and getattr(field, 'comodel_name', None) == 'ir.actions.server':
            score = 0
            lname = fname.lower()
            if 'tool' in lname:
                score -= 20
            if 'action' in lname:
                score -= 10
            direct.append((score, fname, field))
    if direct:
        _, fname, _field = sorted(direct, key=lambda item: (item[0], item[1]))[0]
        topic.write({fname: [(6, 0, actions.ids)]})
        return fname

    for fname, field in Topic._fields.items():
        if field.type != 'one2many' or not getattr(field, 'comodel_name', None):
            continue
        Link = env[field.comodel_name]
        action_field = next((
            name for name, rel in Link._fields.items()
            if rel.type == 'many2one' and getattr(rel, 'comodel_name', None) == 'ir.actions.server'
        ), None)
        if not action_field:
            continue
        # Remove prior links for this topic if possible, then rebuild them.
        Link.search([(field.inverse_name, '=', topic.id)]).unlink()
        rows = []
        for sequence, action in enumerate(actions, start=1):
            vals = {field.inverse_name: topic.id, action_field: action.id}
            if 'sequence' in Link._fields:
                vals['sequence'] = sequence
            rows.append(vals)
        Link.create(rows)
        return fname

    raise UserError('Could not locate the native ai.topic -> AI tool relation on this Odoo 19 build.')


def _attach_topic_to_ask_ai(env, topic):
    Agent = env['ai.agent']
    agent = Agent.search([('name', '=ilike', 'Ask AI')], limit=1)
    if not agent and 'is_system_agent' in Agent._fields:
        system_agents = Agent.search([('is_system_agent', '=', True)])
        matching = system_agents.filtered(lambda item: 'ask' in (item.name or '').lower())
        agent = matching[:1] or system_agents[:1]
    if not agent:
        raise UserError(
            'Built-in Ask AI agent was not found. Open/install the official Odoo AI app, then upgrade students_management again.'
        )

    topic_field = 'topic_ids' if 'topic_ids' in Agent._fields else next((
        name for name, field in Agent._fields.items()
        if field.type in ('many2many', 'one2many') and getattr(field, 'comodel_name', None) == 'ai.topic'
    ), None)
    if not topic_field:
        raise UserError('Could not locate the Topics field on ai.agent.')
    if topic.id not in agent[topic_field].ids:
        agent.write({topic_field: [(4, topic.id)]})

    prompt_field = next((field_name for field_name in ('system_prompt', 'prompt', 'instructions') if field_name in Agent._fields), None)
    if prompt_field:
        prompt = agent[prompt_field] or ''
        start = PROMPT_MARKER_START.strip()
        end = PROMPT_MARKER_END.strip()
        if start in prompt:
            before, remainder = prompt.split(start, 1)
            if end in remainder:
                _, after = remainder.split(end, 1)
                prompt = (before.rstrip() + '\n' + after.lstrip()).strip()
            else:
                prompt = before.rstrip()
        agent.write({
            prompt_field: prompt.rstrip() + PROMPT_MARKER_START + GENERAL_INSTRUCTIONS + PROMPT_MARKER_END,
        })
    return agent


class StudentsHelpdeskNativeAISetup(models.AbstractModel):
    _name = 'students.helpdesk.native.ai.setup'
    _description = 'Configure Odoo Built-in Ask AI for Student and Odoo Operations'

    @api.model
    def setup_native_helpdesk_ai(self):
        env = self.env
        required_models = ('helpdesk.ticket', 'helpdesk.team', 'ai.agent', 'ai.topic')
        missing = [model for model in required_models if model not in env]
        if missing:
            raise UserError('Missing required Odoo models: %s' % ', '.join(missing))

        # Using res.users as the Server Action model keeps these tools callable
        # from the global Ask AI panel, not only from an already-open ticket.
        user_model = env['ir.model']._get('res.users')
        if not user_model:
            raise UserError('Could not resolve res.users for native AI tool registration.')

        def arg(name, description, required=True):
            return {'name': name, 'description': description, 'required': required}

        # Remove tools created by the older Helpdesk-only build.  Otherwise the
        # global Ask AI tool registry can expose duplicate operations after -u.
        old_tools = env['ir.actions.server'].search([('name', 'ilike', LEGACY_PREFIX + '%')])
        if old_tools:
            old_tools.unlink()

        tools = env['ir.actions.server']
        tools |= _create_or_replace_tool(
            env, user_model.id, 'Inspect Odoo Model',
            'Show the real fields, labels, types, relations, required flags, and AI-editable fields for one supported Odoo record type.',
            "ai['result'] = env['helpdesk.native.ai.service'].ai_inspect_model(model_name)",
            [
                arg('model_name', 'Friendly record type or technical model, for example student, teacher, contact, product, sale.order.'),
            ],
        )
        tools |= _create_or_replace_tool(
            env, user_model.id, 'Find Odoo Records',
            'Search supported Odoo records using free text plus exact JSON field filters.',
            "ai['result'] = env['helpdesk.native.ai.service'].ai_find_records(model_name, query, filters_json, limit_text)",
            [
                arg('model_name', 'Friendly record type or technical model.'),
                arg('query', 'Optional free-text name/reference/email/phone search; empty string if none.'),
                arg('filters_json', 'Optional JSON object of exact field filters; use {} if none.'),
                arg('limit_text', 'Maximum results as text; use 10 when omitted.'),
            ],
        )
        tools |= _create_or_replace_tool(
            env, user_model.id, 'Create Odoo Record',
            'Create one supported Odoo record with permission-aware ORM. Values must be a JSON object.',
            "ai['result'] = env['helpdesk.native.ai.service'].ai_create_record(model_name, values_json)",
            [
                arg('model_name', 'Friendly record type or technical model.'),
                arg('values_json', 'JSON object of field values. Relations may use a unique name or numeric ID.'),
            ],
        )
        tools |= _create_or_replace_tool(
            env, user_model.id, 'Update Odoo Record',
            'Edit one supported Odoo record using its ID or unique name/reference. Values must be a JSON object.',
            "ai['result'] = env['helpdesk.native.ai.service'].ai_update_record(model_name, record_ref, values_json)",
            [
                arg('model_name', 'Friendly record type or technical model.'),
                arg('record_ref', 'Numeric ID or unique record name/reference.'),
                arg('values_json', 'JSON object containing only fields that should change.'),
            ],
        )

        tools |= _create_or_replace_tool(
            env, user_model.id, 'Create Helpdesk Ticket',
            'Create one official Odoo Helpdesk ticket. Optional arguments must use empty strings; priority defaults to low.',
            "ai['result'] = env['helpdesk.native.ai.service'].ai_create_ticket(title, details, person_name, team_name, priority)",
            [
                arg('title', 'Concise ticket title. Required.'),
                arg('details', 'Extra description/details; empty string if omitted.'),
                arg('person_name', 'Person named in the request; empty string if omitted.'),
                arg('team_name', 'Official Helpdesk team; empty string if omitted.'),
                arg('priority', 'low, medium, high, or urgent; use low when omitted.'),
            ],
        )
        tools |= _create_or_replace_tool(
            env, user_model.id, 'Find Helpdesk Tickets',
            'Find official Odoo Helpdesk tickets by text, assignee, priority, and open/closed status.',
            "ai['result'] = env['helpdesk.native.ai.service'].ai_find_tickets(query, person_name, priority, status, limit_text)",
            [
                arg('query', 'Search text in title/description; empty string if none.'),
                arg('person_name', 'Assignee name/login; empty string if none.'),
                arg('priority', 'low/medium/high/urgent; empty string if none.'),
                arg('status', 'open/closed/all; empty string means all.'),
                arg('limit_text', 'Maximum number of results as text; use 10 if omitted.'),
            ],
        )
        tools |= _create_or_replace_tool(
            env, user_model.id, 'Assign Helpdesk Ticket',
            'Assign an official Helpdesk ticket to an active internal Odoo user.',
            "ai['result'] = env['helpdesk.native.ai.service'].ai_assign_ticket(ticket_ref, person_name)",
            [
                arg('ticket_ref', 'Ticket ID/reference/title fragment.'),
                arg('person_name', 'Internal Odoo user name/login.'),
            ],
        )
        tools |= _create_or_replace_tool(
            env, user_model.id, 'Set Helpdesk Priority',
            'Change an official Helpdesk ticket priority.',
            "ai['result'] = env['helpdesk.native.ai.service'].ai_set_priority(ticket_ref, priority)",
            [
                arg('ticket_ref', 'Ticket ID/reference/title fragment.'),
                arg('priority', 'low, medium, high, or urgent.'),
            ],
        )
        tools |= _create_or_replace_tool(
            env, user_model.id, 'Set Helpdesk Ticket Status',
            'Close/solve or reopen an official Helpdesk ticket using configured Helpdesk stages.',
            "ai['result'] = env['helpdesk.native.ai.service'].ai_set_status(ticket_ref, status)",
            [
                arg('ticket_ref', 'Ticket ID/reference/title fragment.'),
                arg('status', 'open/reopen or close/solved/resolved/done.'),
            ],
        )
        tools |= _create_or_replace_tool(
            env, user_model.id, 'Update Helpdesk Ticket',
            'Change an official Helpdesk ticket title and/or description.',
            "ai['result'] = env['helpdesk.native.ai.service'].ai_update_ticket(ticket_ref, new_title, new_description)",
            [
                arg('ticket_ref', 'Ticket ID/reference/title fragment.'),
                arg('new_title', 'New title; empty string means unchanged.'),
                arg('new_description', 'New description; empty string means unchanged.'),
            ],
        )

        topic = _get_or_create_topic(env)
        relation = _link_actions_to_topic(env, topic, tools)
        agent = _attach_topic_to_ask_ai(env, topic)
        _logger.info(
            'Built-in Odoo Ask AI general setup complete: agent=%s topic=%s relation=%s tools=%s',
            agent.display_name, topic.display_name, relation, len(tools),
        )
        return True
