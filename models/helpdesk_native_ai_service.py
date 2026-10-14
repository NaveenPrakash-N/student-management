import json
import re

from odoo import _, api, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.osv import expression


class HelpdeskNativeAIService(models.AbstractModel):
    """Permission-aware worker used by Odoo's native AI server-action tools.

    This model never calls an external LLM. Odoo's built-in AI agent decides
    which tool to invoke; this worker validates names/references and executes
    normal ORM operations on an explicit allow-list of Student Management and
    common Odoo models. Dedicated official Helpdesk helpers remain available.
    """

    _name = 'helpdesk.native.ai.service'
    _description = 'Native Odoo AI Record Service'

    @api.model
    def _clean(self, value):
        return (value or '').strip() if isinstance(value, str) else value

    @api.model
    def _allowed_general_models(self):
        """Models Ask AI may manage through the generic tools.

        This is intentionally an allow-list.  The tools always run as the
        current user (never sudo), so normal Odoo ACLs and record rules still
        apply on top of this list.
        """
        aliases = {
            'student': 'students.student',
            'students': 'students.student',
            'students.student': 'students.student',
            'teacher': 'students.teacher',
            'teachers': 'students.teacher',
            'students.teacher': 'students.teacher',
            'department': 'students.department',
            'departments': 'students.department',
            'students.department': 'students.department',
            'subject': 'students.subject',
            'subjects': 'students.subject',
            'students.subject': 'students.subject',
            'fee type': 'students.fee.type',
            'fee types': 'students.fee.type',
            'students.fee.type': 'students.fee.type',
            'semester': 'students.semester',
            'semesters': 'students.semester',
            'students.semester': 'students.semester',
            'semester fee': 'students.semester.fee',
            'semester fees': 'students.semester.fee',
            'students.semester.fee': 'students.semester.fee',
            'mark': 'students.mark',
            'marks': 'students.mark',
            'students.mark': 'students.mark',
            'contact': 'res.partner',
            'contacts': 'res.partner',
            'customer': 'res.partner',
            'customers': 'res.partner',
            'vendor': 'res.partner',
            'vendors': 'res.partner',
            'partner': 'res.partner',
            'res.partner': 'res.partner',
            'product': 'product.template',
            'products': 'product.template',
            'product.template': 'product.template',
            'quotation': 'sale.order',
            'quotations': 'sale.order',
            'sale order': 'sale.order',
            'sales order': 'sale.order',
            'sale orders': 'sale.order',
            'sales orders': 'sale.order',
            'sale.order': 'sale.order',
            'ticket': 'helpdesk.ticket',
            'tickets': 'helpdesk.ticket',
            'helpdesk ticket': 'helpdesk.ticket',
            'helpdesk tickets': 'helpdesk.ticket',
            'helpdesk.ticket': 'helpdesk.ticket',
        }
        return aliases

    @api.model
    def _resolve_general_model(self, model_name):
        raw = self._clean(model_name)
        key = (raw or '').lower().replace('_', ' ').strip()
        aliases = self._allowed_general_models()
        technical = aliases.get(key) or aliases.get((raw or '').lower())
        if not technical:
            choices = sorted({
                'student', 'teacher', 'department', 'subject', 'fee type',
                'semester', 'semester fee', 'mark', 'contact', 'product',
                'quotation/sale order', 'helpdesk ticket',
            })
            raise UserError(_(
                "I cannot manage model '%s' with the general AI tools. Supported record types: %s.",
                model_name, ', '.join(choices),
            ))
        if technical not in self.env:
            raise UserError(_("The Odoo model '%s' is not installed in this database.", technical))
        return self.env[technical], technical

    @api.model
    def _field_is_ai_writable(self, field):
        return (
            not getattr(field, 'readonly', False)
            and not (getattr(field, 'compute', False) and not getattr(field, 'inverse', False))
            and field.type in {
                'char', 'text', 'html', 'integer', 'float', 'monetary',
                'boolean', 'date', 'datetime', 'selection', 'many2one', 'many2many',
            }
        )

    @api.model
    def _selection_items(self, field):
        if hasattr(field, '_description_selection'):
            return field._description_selection(self.env) or []
        selection = field.selection
        if callable(selection):
            return selection(self.env[field.model_name]) or []
        if isinstance(selection, str):
            return getattr(self.env[field.model_name], selection)() or []
        return selection or []

    @api.model
    def _resolve_general_field(self, Model, field_name, writable=False):
        requested = self._clean(field_name)
        if not requested:
            raise UserError(_('A field name is required.'))
        if requested in Model._fields:
            field = Model._fields[requested]
            if writable and not self._field_is_ai_writable(field):
                raise UserError(_("Field '%s' is not editable through Ask AI.", requested))
            return requested, field

        lowered = requested.lower()
        matches = []
        for name, field in Model._fields.items():
            label = (getattr(field, 'string', '') or '').strip().lower()
            if label == lowered:
                if not writable or self._field_is_ai_writable(field):
                    matches.append((name, field))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise UserError(_(
                "More than one field has the label '%s': %s. Use the technical field name.",
                requested, ', '.join(name for name, _field in matches),
            ))
        raise UserError(_("Unknown field '%s' on %s.", requested, Model._description or Model._name))

    @api.model
    def _parse_json_object(self, text, argument_name='values_json'):
        text = self._clean(text)
        if not text:
            return {}
        if isinstance(text, dict):
            return text
        try:
            value = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise UserError(_(
                "%s must be valid JSON, for example {\"name\": \"Anu\", \"email\": \"anu@example.com\"}.",
                argument_name,
            )) from exc
        if not isinstance(value, dict):
            raise UserError(_('%s must be a JSON object.', argument_name))
        return value

    @api.model
    def _resolve_relation_value(self, comodel_name, value):
        Related = self.env[comodel_name]
        if value in (False, None, ''):
            return False
        if isinstance(value, bool):
            raise UserError(_("Boolean value is not a valid reference for %s.", Related._description or comodel_name))
        if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
            record = Related.browse(int(value)).exists()
            if not record:
                raise UserError(_("No %s record exists with ID %s.", Related._description or comodel_name, value))
            record.check_access('read')
            return record.id

        name = self._clean(str(value))
        exact = Related.name_search(name=name, operator='=', limit=2)
        matches = exact or Related.name_search(name=name, operator='ilike', limit=3)
        if len(matches) == 1:
            return matches[0][0]
        if len(matches) > 1:
            raise UserError(_(
                "More than one %s matches '%s': %s. Please be more specific.",
                Related._description or comodel_name,
                name,
                ', '.join(display for _rid, display in matches),
            ))
        raise UserError(_("No %s matches '%s'.", Related._description or comodel_name, name))

    @api.model
    def _convert_ai_value(self, Model, field_name, field, value):
        if value is None:
            return False
        ftype = field.type
        if ftype == 'many2one':
            return self._resolve_relation_value(field.comodel_name, value)
        if ftype == 'many2many':
            if value in (False, '', []):
                return [(5, 0, 0)]
            values = value if isinstance(value, list) else [item.strip() for item in str(value).split(',') if item.strip()]
            ids = [self._resolve_relation_value(field.comodel_name, item) for item in values]
            return [(6, 0, ids)]
        if ftype == 'selection':
            selection = self._selection_items(field)
            allowed = dict(selection)
            if value in allowed:
                return value
            lookup = str(value).strip().lower()
            by_label = [key for key, label in selection if str(label).strip().lower() == lookup]
            if len(by_label) == 1:
                return by_label[0]
            raise UserError(_(
                "Invalid value '%s' for %s. Allowed values: %s.",
                value, field_name, ', '.join('%s (%s)' % (label, key) for key, label in selection),
            ))
        if ftype == 'boolean':
            if isinstance(value, bool):
                return value
            lowered = str(value).strip().lower()
            if lowered in {'true', '1', 'yes', 'y', 'on'}:
                return True
            if lowered in {'false', '0', 'no', 'n', 'off', ''}:
                return False
            raise UserError(_("Invalid boolean value '%s' for %s.", value, field_name))
        if ftype == 'integer':
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise UserError(_("'%s' is not a valid integer for %s.", value, field_name)) from exc
        if ftype in {'float', 'monetary'}:
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise UserError(_("'%s' is not a valid number for %s.", value, field_name)) from exc
        if ftype in {'char', 'text', 'html', 'date', 'datetime'}:
            return self._clean(str(value)) if value is not False else False
        raise UserError(_("Field '%s' cannot be changed through Ask AI.", field_name))

    @api.model
    def _prepare_general_values(self, Model, values):
        vals = {}
        for requested_name, value in values.items():
            field_name, field = self._resolve_general_field(Model, requested_name, writable=True)
            vals[field_name] = self._convert_ai_value(Model, field_name, field, value)
        return vals

    @api.model
    def _resolve_general_record(self, Model, reference):
        ref = self._clean(reference)
        if not ref:
            raise UserError(_('A record reference is required.'))
        match = re.fullmatch(r'(?i)\s*(?:id\s*)?#?\s*(\d+)\s*', str(ref))
        if match:
            record = Model.browse(int(match.group(1))).exists()
            if record:
                record.check_access('read')
                return record

        exact = Model.name_search(name=str(ref), operator='=', limit=2)
        matches = exact or Model.name_search(name=str(ref), operator='ilike', limit=3)
        if len(matches) == 1:
            record = Model.browse(matches[0][0]).exists()
            record.check_access('read')
            return record
        if len(matches) > 1:
            raise UserError(_(
                "More than one %s matches '%s': %s. Use an ID or a more specific name/reference.",
                Model._description or Model._name,
                ref,
                ', '.join(display for _rid, display in matches),
            ))
        raise UserError(_("No %s record matches '%s'.", Model._description or Model._name, ref))

    @api.model
    def _format_general_value(self, record, field_name, field):
        value = record[field_name]
        if field.type == 'many2one':
            return value.display_name if value else '-'
        if field.type in {'many2many', 'one2many'}:
            return ', '.join(value.mapped('display_name')[:8]) if value else '-'
        if field.type == 'selection':
            selection = self._selection_items(field)
            return dict(selection).get(value, value or '-')
        return value if value not in (False, None, '') else '-'

    @api.model
    def ai_inspect_model(self, model_name):
        Model, technical = self._resolve_general_model(model_name)
        lines = ['%s (%s)' % (Model._description or technical, technical)]
        for field_name, field in Model._fields.items():
            if field_name.startswith('__'):
                continue
            if field.type in {'binary', 'one2many'} and field_name not in {'student_ids', 'mark_ids', 'semester_fee_ids'}:
                continue
            relation = ' -> %s' % field.comodel_name if getattr(field, 'comodel_name', None) else ''
            flags = []
            if getattr(field, 'required', False):
                flags.append('required')
            if self._field_is_ai_writable(field):
                flags.append('AI-editable')
            suffix = ' [%s]' % ', '.join(flags) if flags else ''
            lines.append('- %s: %s (%s%s)%s' % (
                field_name, field.string or field_name, field.type, relation, suffix,
            ))
        return '\n'.join(lines[:80])

    @api.model
    def ai_find_records(self, model_name, query, filters_json, limit_text):
        Model, technical = self._resolve_general_model(model_name)
        domain = []
        query = self._clean(query)
        if query:
            searchable = []
            for candidate in (getattr(Model, '_rec_name', None), 'name', 'sequence', 'email', 'phone', 'description'):
                if candidate and candidate in Model._fields and Model._fields[candidate].type in {'char', 'text', 'html'} and candidate not in searchable:
                    searchable.append(candidate)
            if searchable:
                query_domain = expression.OR([[(field_name, 'ilike', query)] for field_name in searchable])
                domain = expression.AND([domain, query_domain])

        filters = self._parse_json_object(filters_json, 'filters_json')
        for requested_name, raw_value in filters.items():
            field_name, field = self._resolve_general_field(Model, requested_name, writable=False)
            if field.type == 'many2one':
                value = self._resolve_relation_value(field.comodel_name, raw_value)
            elif field.type == 'selection':
                value = self._convert_ai_value(Model, field_name, field, raw_value)
            elif field.type == 'boolean':
                value = self._convert_ai_value(Model, field_name, field, raw_value)
            elif field.type in {'integer', 'float', 'monetary', 'char', 'text', 'date', 'datetime'}:
                value = self._convert_ai_value(Model, field_name, field, raw_value)
            else:
                raise UserError(_("Filtering by field '%s' is not supported by Ask AI.", field_name))
            domain.append((field_name, '=', value))

        try:
            limit = int(self._clean(limit_text) or 10)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 25))
        records = Model.search(domain, order='id desc', limit=limit)
        if not records:
            return 'No %s records matched.' % (Model._description or technical)

        preferred = [
            name for name in ('sequence', 'name', 'email', 'phone', 'gender', 'department_id',
                              'teacher_id', 'enrollment_state', 'semester', 'status', 'priority',
                              'stage_id', 'partner_id', 'amount_total')
            if name in Model._fields
        ]
        lines = []
        for record in records:
            parts = ['ID=%s' % record.id, 'name=%s' % record.display_name]
            for field_name in preferred:
                if field_name == 'name':
                    continue
                field = Model._fields[field_name]
                parts.append('%s=%s' % (field_name, self._format_general_value(record, field_name, field)))
            lines.append(' | '.join(parts))
        return '\n'.join(lines)

    @api.model
    def ai_create_record(self, model_name, values_json):
        Model, technical = self._resolve_general_model(model_name)
        values = self._parse_json_object(values_json, 'values_json')
        if not values:
            raise UserError(_('Provide at least one field/value to create the record.'))
        vals = self._prepare_general_values(Model, values)
        try:
            record = Model.create(vals)
        except (AccessError, ValidationError) as exc:
            raise UserError(_("Odoo refused to create the %s record: %s", Model._description or technical, exc)) from exc
        return 'Created %s: ID=%s, %s.' % (Model._description or technical, record.id, record.display_name)

    @api.model
    def ai_update_record(self, model_name, record_ref, values_json):
        Model, technical = self._resolve_general_model(model_name)
        record = self._resolve_general_record(Model, record_ref)
        values = self._parse_json_object(values_json, 'values_json')
        if not values:
            return 'No changes were supplied for ID=%s, %s.' % (record.id, record.display_name)
        vals = self._prepare_general_values(Model, values)
        try:
            record.write(vals)
        except (AccessError, ValidationError) as exc:
            raise UserError(_("Odoo refused to update the %s record: %s", Model._description or technical, exc)) from exc
        return 'Updated %s: ID=%s, %s. Changed fields: %s.' % (
            Model._description or technical, record.id, record.display_name, ', '.join(vals),
        )

    @api.model
    def _priority_value(self, text):
        value = (text or '').strip().lower()
        mapping = {
            '': '0',
            '0': '0',
            'low': '0',
            'lowest': '0',
            'normal': '0',
            'none': '0',
            '1': '1',
            'medium': '1',
            'moderate': '1',
            '2': '2',
            'high': '2',
            'important': '2',
            '3': '3',
            'urgent': '3',
            'very high': '3',
            'critical': '3',
            'highest': '3',
            'emergency': '3',
        }
        if value not in mapping:
            raise UserError(_(
                "Unknown priority '%s'. Use low, medium, high, or urgent.",
                text,
            ))
        return mapping[value]

    @api.model
    def _priority_label(self, value):
        return {
            '0': 'Low',
            '1': 'Medium',
            '2': 'High',
            '3': 'Urgent',
        }.get(str(value or '0'), str(value or 'Low'))

    @api.model
    def _resolve_user(self, name, required=True):
        name = self._clean(name)
        if not name:
            if required:
                raise UserError(_('Please provide the assignee name.'))
            return self.env['res.users']

        User = self.env['res.users']
        domain = [('active', '=', True)]
        if 'share' in User._fields:
            domain.append(('share', '=', False))

        exact = User.search(domain + ['|', ('name', '=ilike', name), ('login', '=ilike', name)])
        if len(exact) == 1:
            return exact
        if len(exact) > 1:
            raise UserError(_(
                "More than one internal user exactly matches '%s': %s",
                name,
                ', '.join(exact.mapped('name')[:8]),
            ))

        fuzzy = User.search(domain + ['|', ('name', 'ilike', name), ('login', 'ilike', name)], limit=10)
        if len(fuzzy) == 1:
            return fuzzy
        if len(fuzzy) > 1:
            raise UserError(_(
                "Several internal users match '%s': %s. Please be more specific.",
                name,
                ', '.join(fuzzy.mapped('name')),
            ))
        if required:
            raise UserError(_("No active internal Odoo user matches '%s'.", name))
        return User

    @api.model
    def _resolve_partner(self, name):
        name = self._clean(name)
        if not name:
            return self.env['res.partner']
        Partner = self.env['res.partner']
        exact = Partner.search([('name', '=ilike', name)], limit=2)
        if len(exact) == 1:
            return exact
        fuzzy = Partner.search([('name', 'ilike', name)], limit=2)
        return fuzzy if len(fuzzy) == 1 else Partner

    @api.model
    def _resolve_person_for_create(self, name):
        """Prefer an internal user as assignee; otherwise use a unique contact."""
        name = self._clean(name)
        if not name:
            return self.env['res.users'], self.env['res.partner'], ''
        user = self._resolve_user(name, required=False)
        if user:
            return user, self.env['res.partner'], 'assignee'
        partner = self._resolve_partner(name)
        if partner:
            return self.env['res.users'], partner, 'customer'
        raise UserError(_("I could not find an internal user or contact matching '%s'.", name))

    @api.model
    def _resolve_team(self, name, required=False):
        Team = self.env['helpdesk.team']
        name = self._clean(name)
        if name:
            exact = Team.search([('name', '=ilike', name)], limit=2)
            if len(exact) == 1:
                return exact
            fuzzy = Team.search([('name', 'ilike', name)], limit=10)
            if len(fuzzy) == 1:
                return fuzzy
            if len(exact) > 1 or len(fuzzy) > 1:
                teams = exact or fuzzy
                raise UserError(_(
                    "Several Helpdesk teams match '%s': %s. Please be more specific.",
                    name,
                    ', '.join(teams.mapped('name')),
                ))
            raise UserError(_("No Helpdesk team matches '%s'.", name))

        team = Team.search([], order='sequence, id', limit=1)
        if not team and required:
            raise UserError(_('Create at least one official Odoo Helpdesk team first.'))
        return team

    @api.model
    def _ticket_reference_fields(self):
        Ticket = self.env['helpdesk.ticket']
        return [
            field_name
            for field_name in ('ticket_ref', 'ticket_number', 'number', 'sequence', 'name')
            if field_name in Ticket._fields
            and Ticket._fields[field_name].type in ('char', 'text')
        ]

    @api.model
    def _resolve_ticket(self, reference):
        Ticket = self.env['helpdesk.ticket']
        ref = self._clean(reference)
        if not ref:
            raise UserError(_('Please provide a ticket reference, ID, or title.'))

        digit_match = re.fullmatch(r'(?i)\s*(?:ticket\s*)?#?\s*(\d+)\s*', ref)
        if digit_match:
            ticket = Ticket.browse(int(digit_match.group(1))).exists()
            if ticket:
                ticket.check_access('read')
                return ticket

        for field_name in self._ticket_reference_fields():
            exact = Ticket.search([(field_name, '=ilike', ref)], limit=2)
            if len(exact) == 1:
                return exact

        by_name = Ticket.search([('name', 'ilike', ref)], limit=10)
        if len(by_name) == 1:
            return by_name
        if len(by_name) > 1:
            options = '; '.join(self._ticket_label(ticket) for ticket in by_name[:6])
            raise UserError(_("More than one ticket matches '%s': %s", ref, options))
        raise UserError(_("I could not find an official Odoo Helpdesk ticket matching '%s'.", ref))

    @api.model
    def _ticket_label(self, ticket):
        # Odoo Helpdesk may expose a human-readable reference in some builds.
        for field_name in ('ticket_ref', 'ticket_number', 'number'):
            if field_name in ticket._fields and getattr(ticket, field_name, False):
                return '%s — %s' % (getattr(ticket, field_name), ticket.name or 'Untitled')
        return '#%s — %s' % (ticket.id, ticket.name or 'Untitled')

    @api.model
    def ai_create_ticket(self, title, details, person_name, team_name, priority):
        title = self._clean(title)
        details = self._clean(details)
        if not title:
            raise UserError(_('A ticket title is required.'))

        Ticket = self.env['helpdesk.ticket']
        team = self._resolve_team(team_name, required=True)
        assignee, customer, person_role = self._resolve_person_for_create(person_name)
        priority_value = self._priority_value(priority)

        vals = {'name': title}
        if 'description' in Ticket._fields and details:
            vals['description'] = details
        if 'team_id' in Ticket._fields and team:
            vals['team_id'] = team.id
        if 'priority' in Ticket._fields:
            vals['priority'] = priority_value
        if 'user_id' in Ticket._fields and assignee:
            vals['user_id'] = assignee.id
        if 'partner_id' in Ticket._fields and customer:
            vals['partner_id'] = customer.id

        try:
            ticket = Ticket.create(vals)
        except (AccessError, ValidationError) as exc:
            raise UserError(_("Odoo refused to create the Helpdesk ticket: %s", exc)) from exc

        parts = [
            'Created official Odoo Helpdesk ticket %s.' % self._ticket_label(ticket),
            'Team: %s.' % (ticket.team_id.name if 'team_id' in ticket._fields and ticket.team_id else team.name),
            'Priority: %s.' % self._priority_label(getattr(ticket, 'priority', priority_value)),
        ]
        if person_role == 'assignee' and assignee:
            parts.append('Assigned to: %s.' % assignee.name)
        elif person_role == 'customer' and customer:
            parts.append('Customer: %s.' % customer.name)
        return ' '.join(parts)

    @api.model
    def ai_assign_ticket(self, ticket_ref, person_name):
        ticket = self._resolve_ticket(ticket_ref)
        assignee = self._resolve_user(person_name, required=True)
        if 'user_id' not in ticket._fields:
            raise UserError(_('This Odoo Helpdesk build does not expose a user_id assignee field.'))
        try:
            ticket.write({'user_id': assignee.id})
        except (AccessError, ValidationError) as exc:
            raise UserError(_("Odoo refused to assign the ticket: %s", exc)) from exc
        return 'Assigned %s to %s.' % (self._ticket_label(ticket), assignee.name)

    @api.model
    def ai_set_priority(self, ticket_ref, priority):
        ticket = self._resolve_ticket(ticket_ref)
        value = self._priority_value(priority)
        try:
            ticket.write({'priority': value})
        except (AccessError, ValidationError) as exc:
            raise UserError(_("Odoo refused to change the priority: %s", exc)) from exc
        return 'Set %s priority to %s.' % (self._ticket_label(ticket), self._priority_label(value))

    @api.model
    def ai_set_status(self, ticket_ref, status):
        ticket = self._resolve_ticket(ticket_ref)
        status_text = self._clean(status).lower()
        close_words = {'close', 'closed', 'solve', 'solved', 'resolve', 'resolved', 'done', 'complete', 'completed'}
        open_words = {'open', 'opened', 'reopen', 'reopened', 'new', 'active'}
        if status_text not in close_words | open_words:
            raise UserError(_("Unknown status '%s'. Use open/reopen or close/solved.", status))

        Stage = self.env['helpdesk.stage']
        want_closed = status_text in close_words
        domain = [('fold', '=', want_closed)]
        if 'team_ids' in Stage._fields and getattr(ticket, 'team_id', False):
            domain = expression.AND([
                domain,
                ['|', ('team_ids', '=', False), ('team_ids', 'in', [ticket.team_id.id])],
            ])
        stage = Stage.search(domain, order='sequence, id', limit=1)
        if not stage:
            kind = 'closed/folded' if want_closed else 'open/unfolded'
            raise UserError(_("No %s Helpdesk stage is configured for this ticket/team.", kind))
        try:
            ticket.write({'stage_id': stage.id})
        except (AccessError, ValidationError) as exc:
            raise UserError(_("Odoo refused to change the ticket stage: %s", exc)) from exc
        return 'Moved %s to stage %s.' % (self._ticket_label(ticket), stage.name)

    @api.model
    def ai_update_ticket(self, ticket_ref, new_title, new_description):
        ticket = self._resolve_ticket(ticket_ref)
        vals = {}
        new_title = self._clean(new_title)
        new_description = self._clean(new_description)
        if new_title:
            vals['name'] = new_title
        if new_description and 'description' in ticket._fields:
            vals['description'] = new_description
        if not vals:
            return 'No changes were supplied for %s.' % self._ticket_label(ticket)
        try:
            ticket.write(vals)
        except (AccessError, ValidationError) as exc:
            raise UserError(_("Odoo refused to update the ticket: %s", exc)) from exc
        return 'Updated %s.' % self._ticket_label(ticket)

    @api.model
    def ai_find_tickets(self, query, person_name, priority, status, limit_text):
        Ticket = self.env['helpdesk.ticket']
        domain = []
        query = self._clean(query)
        person_name = self._clean(person_name)
        priority = self._clean(priority)
        status = self._clean(status).lower()

        if query:
            query_domain = [('name', 'ilike', query)]
            if 'description' in Ticket._fields:
                query_domain = expression.OR([query_domain, [('description', 'ilike', query)]])
            domain = expression.AND([domain, query_domain])

        if person_name and 'user_id' in Ticket._fields:
            user = self._resolve_user(person_name, required=True)
            domain.append(('user_id', '=', user.id))
        if priority:
            domain.append(('priority', '=', self._priority_value(priority)))

        if status in {'open', 'opened', 'active'} and 'stage_id' in Ticket._fields:
            domain.append(('stage_id.fold', '=', False))
        elif status in {'closed', 'close', 'solved', 'resolved', 'done'} and 'stage_id' in Ticket._fields:
            domain.append(('stage_id.fold', '=', True))
        elif status not in {'', 'all', 'any'}:
            raise UserError(_("Unknown ticket status filter '%s'.", status))

        try:
            limit = int(self._clean(limit_text) or 10)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 25))

        tickets = Ticket.search(domain, order='priority desc, id desc', limit=limit)
        if not tickets:
            return 'No official Odoo Helpdesk tickets matched those filters.'

        lines = []
        for ticket in tickets:
            assignee = getattr(ticket, 'user_id', False)
            team = getattr(ticket, 'team_id', False)
            stage = getattr(ticket, 'stage_id', False)
            lines.append(
                '%s | priority=%s | assignee=%s | team=%s | stage=%s' % (
                    self._ticket_label(ticket),
                    self._priority_label(getattr(ticket, 'priority', '0')),
                    assignee.name if assignee else 'Unassigned',
                    team.name if team else '-',
                    stage.name if stage else '-',
                )
            )
        return '\n'.join(lines)
